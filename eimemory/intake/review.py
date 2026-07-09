from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from eimemory.models.records import LinkRef, RecordEnvelope, ScopeRef

KNOWLEDGE_CANDIDATE_KIND = "knowledge_candidate"
DEFAULT_REVIEW_STATUSES = ("candidate", "quarantined", "rejected")
UNSAFE_SAFETY_KEYS = {"prompt_injection", "secret", "secret_detected", "content_redacted"}

_REVIEW_DECISIONS = {
    "approve": "reviewed",
    "reject": "rejected",
    "quarantine": "quarantined",
    "deprecate": "deprecated",
}
_TERMINAL_STATUSES = {"promoted", "merged", "deprecated"}


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


def explain_candidate(
    runtime: Any,
    record_id: str,
    scope: ScopeRef | dict[str, Any] | None = None,
) -> dict[str, Any]:
    record = _candidate_by_id(runtime, record_id, scope=scope)
    _require_scope(record, scope)
    payload = _candidate_payload(record)
    source_kind = _source_kind(record, payload)
    safety = _safety_summary(record, payload)
    content = _content_summary(record, payload)
    paper_identity = _paper_identity_summary(payload)
    review = _review_summary(record)
    promotion = _promotion_summary(record, safety=safety, paper_identity=paper_identity)
    reasons = _explanation_reasons(record, safety=safety, paper_identity=paper_identity, promotion=promotion)
    blockers = [reason for reason in reasons if reason in {"unsafe_candidate", "not_paper_like", "insufficient_content", "status_not_promotable"}]
    return {
        "ok": True,
        "record_id": record.record_id,
        "kind": record.kind,
        "status": record.status,
        "title": record.title,
        "source": record.source,
        "source_kind": source_kind,
        "scope": _scope_payload(record.scope),
        "safety": safety,
        "content": content,
        "paper_identity": paper_identity,
        "review": review,
        "promotion": promotion,
        "reasons": reasons,
        "blockers": blockers,
        "meta": dict(record.meta),
    }


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
    record = _candidate_by_id(runtime, record_id, scope=scope)
    _require_scope(record, scope)
    if record.status in _TERMINAL_STATUSES:
        raise ValueError(f"cannot review terminal candidate status: {record.status}")
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
    candidate = _candidate_by_id(runtime, record_id, scope=scope)
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


def mark_candidate_paper_promoted(
    runtime: Any,
    record: RecordEnvelope,
    report: dict[str, Any],
) -> RecordEnvelope:
    record.status = "promoted"
    record.meta = {
        **dict(record.meta or {}),
        "promoted_to_paper_source_id": str(report.get("paper_source_id") or ""),
        "promotion_record_ids": list(report.get("record_ids") or []),
        "closure_review_record_id": str(report.get("closure_review_record_id") or ""),
        "closure_decision": str(report.get("closure_decision") or ""),
    }
    _append_review_history(record, decision="paper_promote", actor="pipeline", note=str(report.get("paper_source_id") or ""))
    return _save(runtime, record)


def merge_candidates(
    runtime: Any,
    source_record_id: str,
    target_record_id: str,
    reviewer: str,
    note: str = "",
    scope: ScopeRef | dict[str, Any] | None = None,
) -> RecordEnvelope:
    source = _candidate_by_id(runtime, source_record_id, scope=scope)
    target = _candidate_by_id(runtime, target_record_id, scope=scope)
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


def _candidate_by_id(
    runtime: Any,
    record_id: str,
    *,
    scope: ScopeRef | dict[str, Any] | None = None,
) -> RecordEnvelope:
    record = _store(runtime).get_by_id(str(record_id), scope=scope)
    if record is None:
        if scope is not None and _store(runtime).get_by_id(str(record_id)) is not None:
            raise ValueError("scope mismatch")
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


def _candidate_payload(record: RecordEnvelope) -> dict[str, Any]:
    payload = dict(record.content or {})
    payload.setdefault("record_id", record.record_id)
    payload.setdefault("status", record.status)
    payload.setdefault("title", record.title)
    payload.setdefault("summary", record.summary)
    payload.setdefault("content_excerpt", record.detail)
    payload.setdefault("provenance", dict(record.provenance or {}))
    payload.setdefault("meta", dict(record.meta or {}))
    return payload


def _source_kind(record: RecordEnvelope, payload: dict[str, Any]) -> str:
    for container in (payload, record.meta, record.provenance):
        if isinstance(container, dict) and container.get("source_kind"):
            return str(container.get("source_kind") or "").strip().lower()
    return ""


def _safety_summary(record: RecordEnvelope, payload: dict[str, Any]) -> dict[str, Any]:
    flags: set[str] = set()
    for container in _candidate_containers(record, payload):
        safety = container.get("safety") if isinstance(container, dict) else None
        if isinstance(safety, dict):
            flags.update(key for key in UNSAFE_SAFETY_KEYS if bool(safety.get(key)))
    return {"unsafe": bool(flags), "flags": sorted(flags)}


def _content_summary(record: RecordEnvelope, payload: dict[str, Any]) -> dict[str, Any]:
    text = _content_text(record, payload)
    compact_length = len("".join(char for char in text if char.isalnum()))
    return {
        "length": len(text),
        "alnum_length": compact_length,
        "has_title": bool(record.title.strip() or str(payload.get("title") or "").strip()),
        "has_summary": bool(record.summary.strip() or str(payload.get("summary") or "").strip()),
    }


def _paper_identity_summary(payload: dict[str, Any]) -> dict[str, Any]:
    from eimemory.intake.pipeline import _is_collected_paper_like, _paper_input_from_candidate

    paper_input = _paper_input_from_candidate(payload)
    metadata = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
    arxiv_id = str(payload.get("arxiv_id") or metadata.get("arxiv_id") or "").strip()
    doi = str(payload.get("doi") or metadata.get("doi") or "").strip()
    url = str(payload.get("canonical_url") or payload.get("paper_url") or payload.get("url") or payload.get("uri") or "").strip()
    return {
        "is_paper_like": bool(_is_collected_paper_like(payload)),
        "has_promotable_paper_input": bool(paper_input),
        "source_kind": str(payload.get("source_kind") or "").strip().lower(),
        "arxiv_id": arxiv_id,
        "doi": doi,
        "url": url,
    }


def _review_summary(record: RecordEnvelope) -> dict[str, Any]:
    history = record.meta.get("review_history")
    if not isinstance(history, list):
        history = []
    latest = history[-1] if history else {}
    return {
        "reviewed": record.status in {"reviewed", "promoted", "rejected", "quarantined", "deprecated", "merged"} or bool(history),
        "history": list(history),
        "latest": dict(latest) if isinstance(latest, dict) else {},
    }


def _promotion_summary(record: RecordEnvelope, *, safety: dict[str, Any], paper_identity: dict[str, Any]) -> dict[str, Any]:
    promoted_record_id = str(record.meta.get("promoted_record_id") or "")
    promoted_paper_source_id = str(record.meta.get("promoted_to_paper_source_id") or "")
    is_promoted = record.status == "promoted" or bool(promoted_record_id or promoted_paper_source_id)
    status_allows = record.status in {"candidate", "reviewed"}
    paper_promotable = (
        status_allows
        and not bool(safety.get("unsafe"))
        and bool(paper_identity.get("is_paper_like"))
        and bool(paper_identity.get("has_promotable_paper_input"))
    )
    return {
        "promotable": paper_promotable,
        "manual_memory_promotable": status_allows and not bool(safety.get("unsafe")),
        "status": "promoted" if is_promoted else "not_promoted",
        "promoted_record_id": promoted_record_id,
        "promoted_to_paper_source_id": promoted_paper_source_id,
        "promotion_record_ids": list(record.meta.get("promotion_record_ids") or []),
    }


def _explanation_reasons(
    record: RecordEnvelope,
    *,
    safety: dict[str, Any],
    paper_identity: dict[str, Any],
    promotion: dict[str, Any],
) -> list[str]:
    reasons = [f"{record.status}_candidate"]
    if record.status in {"candidate", "reviewed"}:
        reasons.append("candidate_status_allows_promotion")
    else:
        reasons.append("status_not_promotable")
    if bool(safety.get("unsafe")):
        reasons.append("unsafe_candidate")
    if promotion.get("status") == "promoted":
        reasons.append("already_promoted")
    if not bool(paper_identity.get("is_paper_like")):
        reasons.append("not_paper_like")
    elif not bool(paper_identity.get("has_promotable_paper_input")):
        reasons.append("insufficient_content")
    if promotion.get("promotable"):
        reasons.append("paper_promotable")
    return _dedupe_strings(reasons)


def _candidate_containers(record: RecordEnvelope, payload: dict[str, Any]) -> list[dict[str, Any]]:
    containers = [payload, dict(record.meta or {}), dict(record.provenance or {})]
    metadata = payload.get("metadata")
    if isinstance(metadata, dict):
        containers.append(metadata)
    meta = payload.get("meta")
    if isinstance(meta, dict):
        containers.append(meta)
    return containers


def _content_text(record: RecordEnvelope, payload: dict[str, Any]) -> str:
    for key in ("text", "body", "abstract", "content_excerpt", "summary"):
        value = payload.get(key)
        if value:
            return str(value).strip()
    return str(record.detail or record.summary or "").strip()


def _dedupe_strings(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


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
