from __future__ import annotations

import sqlite3
from typing import Any, Mapping

from eimemory.contracts.score_numbers import finite_score_number
from eimemory.knowledge.evidence_contracts import assess_record_confidence
from eimemory.knowledge.safety import evaluate_knowledge_safety
from eimemory.models.records import ScopeRef

GATED_ANSWER_KINDS = {
    "claim_card",
    "knowledge_candidate",
    "knowledge_unit",
    "news",
    "paper_source",
    "paper_extract",
    "source_candidate",
}
GATED_SOURCE_MARKERS = ("research", "knowledge.synthesis", "daily_brief", "news", "rss", "paper")
MAX_PAPER_SOURCE_LOOKUPS = 4096
# A native digest retains every source in its scan window, not just the five
# displayed highlights. Its declaration limit must fit the shared read budget.
MAX_PAPER_SOURCE_REFS = MAX_PAPER_SOURCE_LOOKUPS


def current_evidence_reasons(records: list[Any], *, store: Any = None, deadline_at: float = 0.0) -> list[str]:
    from eimemory.knowledge.current_evidence import current_evidence_reasons as _current

    return _current(records, store=store, deadline_at=deadline_at)


def paper_source_evidence_reasons(records: list[Any], *, store: Any = None, deadline_at: float = 0.0) -> list[str]:
    """Resolve declared paper parents in one exact-owner read snapshot.

    Missing parents remain compatible with manual text extraction, whose
    public contract never required a persisted paper_source. A stored parent
    in another owner/source partition cannot authorize or revoke this one.
    Lookup failures and bounded-read exhaustion are not evidence of absence.
    """
    if store is None or not records:
        return [""] * len(records)
    payloads = [_payload(record) for record in records]
    declarations = [_paper_parent_ids(payload) for payload in payloads]
    if not any(ids or reason or _is_paper_projection(payload)
               for payload, (ids, reason) in zip(payloads, declarations)):
        return [""] * len(records)

    def read(reader):
        cache: dict[tuple, Any] = {}

        def lookup(record_id, owner, source_id):
            key = (owner.tenant_id, owner.agent_id, owner.workspace_id, owner.user_id, source_id, record_id)
            if key not in cache:
                if len(cache) >= MAX_PAPER_SOURCE_LOOKUPS:
                    raise ValueError("paper_source_lookup_limit")
                cache[key] = reader.get_by_exact_ref(record_id, scope=owner, source_id=source_id,
                                                     fail_on_unavailable=True)
            return cache[key]

        reasons = []
        for payload, (declared, reason) in zip(payloads, declarations):
            if reason:
                reasons.append(reason)
                continue
            if not declared and not _is_paper_projection(payload):
                reasons.append("")
                continue
            owner_payload = payload.get("scope")
            source_id = payload.get("source_id", "default")
            if (not isinstance(owner_payload, Mapping) or not isinstance(source_id, str) or not source_id):
                reasons.append("paper_source_identity_invalid")
                continue
            owner = ScopeRef.from_dict(dict(owner_payload))
            ids = set(declared)
            if _is_paper_projection(payload):
                # Native projectors already retain this exact parent identity.
                # One hop also covers older projections that did not copy the
                # page's paper IDs into their own provenance.
                ref = _deep(payload, "meta", "source_record_ref") or _deep(payload, "provenance", "source_record_ref")
                if ref is not None:
                    if (not isinstance(ref, Mapping) or ref.get("scope") != owner_payload
                            or ref.get("source_id") != source_id
                            or ref.get("kind") not in {"claim_card", "knowledge_page"}
                            or not isinstance(ref.get("record_id"), str) or not ref["record_id"]):
                        reasons.append("paper_source_identity_invalid")
                        continue
                    parent = lookup(ref["record_id"], owner, source_id)
                    if parent is not None:
                        if parent.kind != ref["kind"]:
                            reasons.append("paper_source_identity_invalid")
                            continue
                        more, reason = _paper_parent_ids(_payload(parent))
                        ids.update(more)
            if reason or len(ids) > MAX_PAPER_SOURCE_REFS:
                reasons.append(reason or "paper_source_reference_limit")
                continue
            for record_id in sorted(ids):
                parent = lookup(record_id, owner, source_id)
                if parent is None:
                    continue
                if (parent.kind != "paper_source" or parent.scope != owner or parent.source_id != source_id):
                    reason = "paper_source_identity_invalid"
                    break
                if parent.status != "active" or any(
                    bool(container.get(key))
                    for container in (parent.content, parent.meta, parent.provenance)
                    for key in ("deprecated", "conflict", "contradiction_ids", "contradiction_claim_ids")
                ):
                    reason = "paper_source_inactive"
                    break
            reasons.append(reason)
        return reasons

    try:
        consistent = getattr(store, "read_consistent", None)
        if deadline_at:
            from eimemory.storage.recall_deadline import recall_read_scope

            with recall_read_scope(store, {"_recall_collection_deadline_monotonic": deadline_at}):
                return consistent(read) if callable(consistent) else read(store)
        return consistent(read) if callable(consistent) else read(store)
    except (OSError, ValueError, TypeError, RuntimeError, sqlite3.Error):
        return ["paper_source_unavailable" if ids or reason or _is_paper_projection(payload) else ""
                for payload, (ids, reason) in zip(payloads, declarations)]


def _is_paper_projection(payload: dict[str, Any]) -> bool:
    return payload.get("kind") == "memory" and any(
        _deep(payload, container, "projection_type") == "operational_knowledge"
        for container in ("content", "meta", "provenance")
    )


def _paper_parent_ids(payload: dict[str, Any]) -> tuple[set[str], str]:
    ids: set[str] = set()
    for name in ("content", "meta", "provenance"):
        container = payload.get(name)
        if not isinstance(container, Mapping):
            continue
        value = container.get("paper_source_id")
        if value not in (None, ""):
            if not isinstance(value, str) or not value.strip():
                return set(), "paper_source_identity_invalid"
            ids.add(value)
        if payload.get("kind") == "knowledge_page" or _is_paper_projection(payload):
            values = container.get("source_ids", [])
            if (not isinstance(values, (list, tuple)) or len(values) > MAX_PAPER_SOURCE_REFS
                    or any(not isinstance(item, str) or not item.strip() for item in values)):
                return set(), "paper_source_reference_limit"
            ids.update(values)
    if len(ids) > MAX_PAPER_SOURCE_REFS:
        return set(), "paper_source_reference_limit"
    return ids, ""


def grade_research_evidence(record: Any, *, paper_source_reason: str = "") -> dict[str, Any]:
    payload = _payload(record)
    source = _evidence_source(payload)
    published_at = _evidence_date(payload)
    assessment = assess_record_confidence(payload)
    # Keep the numeric report field backward-compatible without treating this
    # zero as a measured confidence.  confidence_state distinguishes the cases.
    confidence = assessment.value if assessment.value is not None else 0.0
    status = payload.get("status")
    containers = [payload.get(name) for name in ("content", "meta", "provenance")]
    containers = [container for container in containers if isinstance(container, Mapping)]
    conflict = status == "conflicted" or any(
        bool(container.get(key))
        for container in containers
        for key in ("conflict", "contradiction_ids", "contradiction_claim_ids")
    )
    deprecated = any(bool(container.get("deprecated")) for container in containers)
    reasons: list[str] = []
    if status != "active":
        reasons.append("inactive_status")
    if deprecated:
        reasons.append("deprecated_source")
    if paper_source_reason:
        reasons.append(paper_source_reason)
    if not source:
        reasons.append("missing_source")
    if not published_at:
        reasons.append("missing_date")
    if conflict:
        reasons.append("conflict_unresolved")
    if assessment.state == "missing":
        reasons.append("missing_confidence")
    elif assessment.state == "invalid":
        reasons.append("invalid_confidence")
    elif confidence < 0.5:
        reasons.append("low_confidence")
    tier = "unknown" if assessment.state != "valid" else (
        "T2" if confidence >= 0.8 else ("T3" if confidence >= 0.5 else "T5")
    )
    return {
        "ok": not reasons,
        "source": source,
        "published_at": str(published_at)[:10],
        "evidence_tier": tier,
        "confidence": confidence,
        "confidence_state": assessment.state,
        "conflict_check": "unresolved" if conflict else ("clear" if status == "active" else "not_checked"),
        "reason": reasons[0] if reasons else "",
        "reasons": reasons,
    }


def filter_answer_evidence(
    records: list[Any],
    *,
    task_type: str = "",
    registry: Any = None,
    store: Any = None,
) -> dict[str, Any]:
    kept: list[Any] = []
    excluded: list[dict[str, Any]] = []
    parent_reasons = current_evidence_reasons(records, store=store)
    for record, parent_reason in zip(records, parent_reasons):
        if parent_reason:
            excluded.append({"record_id": _record_id(record), "kind": _record_kind(record),
                             "reason": parent_reason, "reasons": [parent_reason]})
            continue
        if not _requires_answer_gate(record, task_type=task_type):
            kept.append(record)
            continue
        if _record_kind(record).lower() == "knowledge_unit":
            safety = evaluate_knowledge_safety(record, task="answer", registry=registry)
            if safety["recall_allowed"]:
                kept.append(record)
                continue
            excluded.append(
                {
                    "record_id": str(_record_id(record)),
                    "kind": str(_record_kind(record)),
                    "title": str(_record_title(record)),
                    "reason": str((safety.get("reasons") or ["knowledge_safety_reject"])[0]),
                    "reasons": list(safety.get("reasons") or []),
                }
            )
            continue
        gate = grade_research_evidence(record)
        if gate["ok"]:
            kept.append(record)
            continue
        excluded.append(
            {
                "record_id": str(_record_id(record)),
                "kind": str(_record_kind(record)),
                "title": str(_record_title(record)),
                "reason": gate["reason"],
                "reasons": list(gate.get("reasons") or []),
            }
        )
    return {
        "ok": True,
        "records": kept,
        "evidence_gate": {
            "kept_count": len(kept),
            "excluded_count": len(excluded),
            "excluded": excluded,
        },
    }


def _payload(record: Any) -> dict[str, Any]:
    if hasattr(record, "to_dict"):
        return record.to_dict()
    if isinstance(record, Mapping):
        return dict(record)
    return {}


def _deep(payload: dict[str, Any], *keys: str) -> Any:
    current: Any = payload
    for key in keys:
        if not isinstance(current, Mapping):
            return None
        current = current.get(key)
    return current


def _first(*values: Any) -> str:
    for value in values:
        text = " ".join(str(value or "").split())
        if text:
            return text
    return ""


def _first_from_list(value: Any) -> str:
    if isinstance(value, list | tuple):
        return _first(*value)
    return _first(value)


def _evidence_source(payload: dict[str, Any]) -> str:
    source = _first(
        _deep(payload, "content", "canonical_url"),
        _deep(payload, "content", "source_url"),
        _deep(payload, "content", "item_url"),
        _deep(payload, "content", "url"),
        _deep(payload, "content", "uri"),
        _deep(payload, "meta", "source_url"),
        _deep(payload, "meta", "item_url"),
        _deep(payload, "meta", "url"),
        _deep(payload, "meta", "source_uri"),
        _deep(payload, "provenance", "source_uri"),
        _deep(payload, "provenance", "source_url"),
    )
    if source:
        return source
    kind = str(payload.get("kind") or "").lower()
    if kind == "news":
        return ""
    digest_items = _deep(payload, "content", "digest", "items")
    if isinstance(digest_items, list):
        source = _first(
            *(
                _first(
                    _deep(item, "url"),
                    _deep(item, "source_url"),
                    _deep(item, "canonical_url"),
                    _deep(item, "uri"),
                )
                for item in digest_items
                if isinstance(item, Mapping)
            )
        )
        if source:
            return source
    if _is_internal_research_artifact(payload):
        return _first(
            _deep(payload, "content", "paper_source_id"),
            _deep(payload, "meta", "paper_source_id"),
            _deep(payload, "provenance", "paper_source_id"),
            _first_from_list(_deep(payload, "content", "source_ids")),
            _first_from_list(payload.get("evidence")),
        )
    return ""


def _evidence_date(payload: dict[str, Any]) -> str:
    published_at = _first(
        _deep(payload, "content", "published_at"),
        _deep(payload, "content", "published"),
        _deep(payload, "meta", "published_at"),
        _deep(payload, "provenance", "published_at"),
    )
    if published_at:
        return published_at
    if _can_use_record_time_as_evidence_date(payload):
        return _first(
            _deep(payload, "time", "occurred_at"),
            _deep(payload, "time", "created_at"),
            _deep(payload, "time", "updated_at"),
        )
    return ""


def _is_internal_research_artifact(payload: dict[str, Any]) -> bool:
    kind = str(payload.get("kind") or "").lower()
    if kind in {"paper_source", "paper_extract", "claim_card", "knowledge_page"}:
        return True
    tags = {str(item).lower() for item in (payload.get("tags") or [])}
    source = str(payload.get("source") or "").lower()
    return "research_digest" in tags or "research_digest" in source


def _can_use_record_time_as_evidence_date(payload: dict[str, Any]) -> bool:
    kind = str(payload.get("kind") or "").lower()
    if kind in {"news", "paper_source", "knowledge_page"}:
        return True
    tags = {str(item).lower() for item in (payload.get("tags") or [])}
    source = str(payload.get("source") or "").lower()
    return "research_digest" in tags or "research_digest" in source


def _float(*values: Any, default: float = 0.0) -> float:
    for value in values:
        try:
            return finite_score_number(value)
        except (TypeError, ValueError, OverflowError):
            continue
    return default


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "y", "on", "conflict", "unresolved"}


def _requires_answer_gate(record: Any, *, task_type: str) -> bool:
    kind = _record_kind(record).lower()
    if kind in GATED_ANSWER_KINDS:
        return True
    payload = _payload(record)
    source = str(
        _deep(payload, "source")
        or _deep(payload, "content", "source")
        or _deep(payload, "meta", "source")
        or _deep(payload, "provenance", "source")
        or ""
    ).lower()
    text = " ".join(
        str(value or "").lower()
        for value in (
            task_type,
            kind,
            source,
            _deep(payload, "content", "page_type"),
            _deep(payload, "meta", "page_type"),
            _deep(payload, "content", "report_type"),
            _deep(payload, "meta", "report_type"),
        )
    )
    return (
        kind in {"knowledge_page", "knowledge_candidate", "source_candidate"}
        and any(marker in text for marker in GATED_SOURCE_MARKERS)
    )


def _record_id(record: Any) -> str:
    if hasattr(record, "record_id"):
        return str(record.record_id)
    payload = _payload(record)
    return str(payload.get("record_id") or payload.get("id") or "")


def _record_kind(record: Any) -> str:
    if hasattr(record, "kind"):
        return str(record.kind)
    payload = _payload(record)
    return str(payload.get("kind") or "")


def _record_title(record: Any) -> str:
    if hasattr(record, "title"):
        return str(record.title)
    payload = _payload(record)
    return str(payload.get("title") or "")
