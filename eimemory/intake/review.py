from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from eimemory.models.records import LinkRef, RecordEnvelope, ScopeRef

KNOWLEDGE_CANDIDATE_KIND = "knowledge_candidate"
DEFAULT_REVIEW_STATUSES = ("candidate", "quarantined", "rejected")

_REVIEW_DECISIONS = {
    "approve": "reviewed",
    "reject": "rejected",
    "quarantine": "quarantined",
    "deprecate": "deprecated",
}


def list_review_queue(
    runtime: Any,
    scope: ScopeRef | dict[str, Any] | None,
    status: str | Iterable[str] | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    store = _store(runtime)
    scope_ref = _scope_ref(scope)
    statuses = _statuses(status)
    records: list[RecordEnvelope] = []
    for item_status in statuses:
        records.extend(
            store.list_records(
                kinds=[KNOWLEDGE_CANDIDATE_KIND],
                scope=scope_ref,
                status=item_status,
                limit=max(0, int(limit)),
            )
        )
    records.sort(key=lambda record: (record.time.updated_at, record.record_id), reverse=True)
    return [_candidate_summary(record) for record in records[: max(0, int(limit))]]


def review_candidate(
    runtime: Any,
    record_id: str,
    decision: str,
    reviewer: str,
    note: str = "",
    scope: ScopeRef | dict[str, Any] | None = None,
) -> RecordEnvelope:
    normalized = str(decision or "").strip().lower()
    if normalized not in _REVIEW_DECISIONS:
        raise ValueError(f"unsupported review decision: {decision}")
    record = _candidate_by_id(runtime, record_id)
    _require_scope(record, scope)
    record.status = _REVIEW_DECISIONS[normalized]
    _append_review_history(record, decision=normalized, actor=reviewer, note=note)
    return _save(runtime, record)


def reject_candidate(
    runtime: Any,
    record_id: str,
    reviewer: str,
    note: str = "",
    scope: ScopeRef | dict[str, Any] | None = None,
) -> RecordEnvelope:
    return review_candidate(runtime, record_id, "reject", reviewer, note=note, scope=scope)


def quarantine_candidate(
    runtime: Any,
    record_id: str,
    reviewer: str,
    note: str = "",
    scope: ScopeRef | dict[str, Any] | None = None,
) -> RecordEnvelope:
    return review_candidate(runtime, record_id, "quarantine", reviewer, note=note, scope=scope)


def deprecate_candidate(
    runtime: Any,
    record_id: str,
    reviewer: str,
    note: str = "",
    scope: ScopeRef | dict[str, Any] | None = None,
) -> RecordEnvelope:
    return review_candidate(runtime, record_id, "deprecate", reviewer, note=note, scope=scope)


def promote_candidate(
    runtime: Any,
    record_id: str,
    promoter: str,
    note: str = "",
    scope: ScopeRef | dict[str, Any] | None = None,
) -> RecordEnvelope:
    candidate = _candidate_by_id(runtime, record_id)
    _require_scope(candidate, scope)
    if candidate.status not in {"candidate", "reviewed"}:
        raise ValueError(f"cannot promote candidate with status: {candidate.status}")

    memory = RecordEnvelope.create(
        kind="memory",
        title=candidate.title,
        summary=candidate.summary,
        detail=candidate.detail,
        content=_memory_content(candidate),
        tags=list(candidate.tags),
        links=[
            *candidate.links,
            LinkRef(relation="promoted_from", target_kind=candidate.kind, target_id=candidate.record_id),
        ],
        evidence=list(candidate.evidence),
        source=candidate.source,
        scope=candidate.scope,
        provenance={**candidate.provenance, "promoted_from": candidate.record_id, "promoted_by": str(promoter)},
        meta={
            **candidate.meta,
            "promoted_from": candidate.record_id,
            "candidate_status": candidate.status,
            "promotion_note": str(note or ""),
        },
        status="active",
    )

    store = _store(runtime)
    store.append(memory)
    candidate.status = "promoted"
    candidate.meta["promoted_record_id"] = memory.record_id
    _append_review_history(candidate, decision="promote", actor=promoter, note=note)
    _save(runtime, candidate)
    return memory


def merge_candidates(
    runtime: Any,
    source_record_id: str,
    target_record_id: str,
    reviewer: str,
    note: str = "",
    scope: ScopeRef | dict[str, Any] | None = None,
) -> RecordEnvelope:
    source = _candidate_by_id(runtime, source_record_id)
    target = _candidate_by_id(runtime, target_record_id)
    _require_scope(source, scope)
    _require_scope(target, scope)
    if not _same_scope(source.scope, target.scope):
        raise ValueError("scope mismatch")
    source.status = "merged"
    source.meta["merged_into"] = target.record_id
    _append_review_history(source, decision="merge", actor=reviewer, note=note, target_record_id=target.record_id)
    return _save(runtime, source)


def _store(runtime: Any) -> Any:
    return getattr(runtime, "store", runtime)


def _candidate_by_id(runtime: Any, record_id: str) -> RecordEnvelope:
    record = _store(runtime).get_by_id(str(record_id))
    if record is None:
        raise ValueError(f"record not found: {record_id}")
    if record.kind != KNOWLEDGE_CANDIDATE_KIND:
        raise ValueError(f"record is not a knowledge candidate: {record_id}")
    return record


def _save(runtime: Any, record: RecordEnvelope) -> RecordEnvelope:
    record.touch()
    return _store(runtime).append(record)


def _scope_ref(scope: ScopeRef | dict[str, Any] | None) -> ScopeRef | None:
    if scope is None:
        return None
    return scope if isinstance(scope, ScopeRef) else ScopeRef.from_dict(scope)


def _require_scope(record: RecordEnvelope, scope: ScopeRef | dict[str, Any] | None) -> None:
    scope_ref = _scope_ref(scope)
    if scope_ref is not None and not _same_scope(record.scope, scope_ref):
        raise ValueError("scope mismatch")


def _same_scope(left: ScopeRef, right: ScopeRef) -> bool:
    return (
        left.tenant_id == right.tenant_id
        and left.agent_id == right.agent_id
        and left.workspace_id == right.workspace_id
        and left.user_id == right.user_id
    )


def _append_review_history(
    record: RecordEnvelope,
    *,
    decision: str,
    actor: str,
    note: str,
    target_record_id: str = "",
) -> None:
    history = record.meta.get("review_history")
    if not isinstance(history, list):
        history = []
    entry = {
        "decision": str(decision),
        "reviewer": str(actor or ""),
        "note": str(note or ""),
        "at": record.time.updated_at,
    }
    if target_record_id:
        entry["target_record_id"] = str(target_record_id)
    history.append(entry)
    record.meta["review_history"] = history


def _statuses(status: str | Iterable[str] | None) -> tuple[str, ...]:
    if status is None:
        return DEFAULT_REVIEW_STATUSES
    if isinstance(status, str):
        return (status,)
    return tuple(str(item) for item in status)


def _candidate_summary(record: RecordEnvelope) -> dict[str, Any]:
    return {
        "record_id": record.record_id,
        "kind": record.kind,
        "status": record.status,
        "title": record.title,
        "summary": record.summary,
        "source": record.source,
        "scope": _scope_payload(record.scope),
        "updated_at": record.time.updated_at,
        "quality": dict(record.meta.get("quality") or {}),
        "meta": dict(record.meta),
    }


def _memory_content(candidate: RecordEnvelope) -> dict[str, Any]:
    content = dict(candidate.content)
    text = content.get("text") or content.get("content_excerpt") or candidate.detail or candidate.summary
    content["text"] = str(text or "")
    return content


def _scope_payload(scope: ScopeRef) -> dict[str, str]:
    return {
        "tenant_id": scope.tenant_id,
        "agent_id": scope.agent_id,
        "workspace_id": scope.workspace_id,
        "user_id": scope.user_id,
    }
