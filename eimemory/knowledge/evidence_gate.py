from __future__ import annotations

from typing import Any, Mapping


GATED_ANSWER_KINDS = {"claim_card", "news", "paper_source", "paper_extract"}
GATED_SOURCE_MARKERS = ("research", "knowledge.synthesis", "daily_brief", "news", "rss", "paper")


def grade_research_evidence(record: Any) -> dict[str, Any]:
    payload = _payload(record)
    source = _first(
        _deep(payload, "content", "canonical_url"),
        _deep(payload, "content", "source_url"),
        _deep(payload, "content", "item_url"),
        _deep(payload, "content", "url"),
        _deep(payload, "content", "uri"),
        _deep(payload, "meta", "source_url"),
        _deep(payload, "meta", "item_url"),
        _deep(payload, "meta", "url"),
        _deep(payload, "provenance", "source_uri"),
        _deep(payload, "provenance", "source_url"),
    )
    published_at = _first(
        _deep(payload, "content", "published_at"),
        _deep(payload, "content", "published"),
        _deep(payload, "meta", "published_at"),
        _deep(payload, "provenance", "published_at"),
    )
    confidence = _float(
        _deep(payload, "content", "confidence"),
        _deep(payload, "meta", "confidence"),
        default=0.8,
    )
    conflict = _truthy(_deep(payload, "content", "conflict")) or _truthy(_deep(payload, "meta", "conflict"))
    reasons: list[str] = []
    if not source:
        reasons.append("missing_source")
    if not published_at:
        reasons.append("missing_date")
    if conflict:
        reasons.append("conflict_unresolved")
    if confidence < 0.5:
        reasons.append("low_confidence")
    tier = "T2" if confidence >= 0.8 else ("T3" if confidence >= 0.5 else "T5")
    return {
        "ok": not reasons,
        "source": source,
        "published_at": str(published_at)[:10],
        "evidence_tier": tier,
        "confidence": confidence,
        "conflict_check": "unresolved" if conflict else "clear",
        "reason": reasons[0] if reasons else "",
        "reasons": reasons,
    }


def filter_answer_evidence(records: list[Any], *, task_type: str = "") -> dict[str, Any]:
    kept: list[Any] = []
    excluded: list[dict[str, Any]] = []
    for record in records:
        if not _requires_answer_gate(record, task_type=task_type):
            kept.append(record)
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


def _float(*values: Any, default: float = 0.0) -> float:
    for value in values:
        try:
            return float(value)
        except (TypeError, ValueError):
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
    return kind == "knowledge_page" and any(marker in text for marker in GATED_SOURCE_MARKERS)


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
