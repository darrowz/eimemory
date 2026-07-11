from __future__ import annotations

from typing import Any, Mapping

from eimemory.intake.loop import _looks_like_prompt_injection, _looks_like_secret


SOURCE_TRUST = {
    "official_docs": 1.0,
    "docs": 1.0,
    "api_docs": 0.95,
    "github_repo": 0.9,
    "paper": 0.85,
    "feishu_doc": 0.8,
    "webpage": 0.65,
    "manual": 0.6,
    "blog": 0.5,
    "summary": 0.5,
    "news": 0.5,
    "rss": 0.5,
}

EXTERNAL_KINDS = {
    "claim_card",
    "knowledge_candidate",
    "knowledge_page",
    "knowledge_unit",
    "news",
    "paper_extract",
    "paper_source",
    "source_candidate",
}
EXTERNAL_MEMORY_TYPES = {"external_knowledge", "knowledge"}
QUARANTINED_STATUSES = {"quarantined", "rejected", "rolled_back"}


def evaluate_knowledge_safety(payload_or_record: Any, *, task: str = "ingest") -> dict[str, Any]:
    """Return a deterministic safety report for external knowledge records.

    The report is intentionally a JSON-safe dict so it can be copied into
    record metadata, validation reports, and recall diagnostics.
    """

    payload = _payload(payload_or_record)
    task_name = str(task or "ingest").strip().lower()
    source_kind = _source_kind(payload)
    source_uri = _source_uri(payload)
    source_trust = _source_trust(payload, source_kind=source_kind)
    trust_tier = trust_tier_for_score(source_trust)
    external = _is_external(payload, source_kind=source_kind)
    strict_provenance = _requires_strict_provenance(payload, source_kind=source_kind)
    status = _status(payload)
    text = _screening_text(payload)
    reasons: list[str] = []

    if status in QUARANTINED_STATUSES:
        reasons.append(f"status_{status}")
    if _looks_like_prompt_injection(text):
        reasons.append("prompt_injection_detected")
    if _looks_like_secret(text):
        reasons.append("secret_detected")
    if strict_provenance and not source_uri:
        reasons.append("missing_source")
    if strict_provenance and source_trust < 0.6:
        reasons.append("source_trust_below_0_6")

    recall_allowed = not reasons
    ingest_allowed = recall_allowed
    capability_allowed = recall_allowed
    if strict_provenance and source_trust < 0.8:
        capability_allowed = False
        if task_name == "capability":
            reasons.append("source_trust_below_0_8_for_capability")

    if not external and not reasons:
        recall_allowed = True
        ingest_allowed = True
        capability_allowed = True

    status_decision = "active" if ingest_allowed and (task_name != "capability" or capability_allowed) else "quarantined"
    return {
        "ok": not reasons,
        "external": external,
        "strict_provenance": strict_provenance,
        "task": task_name,
        "status": status_decision,
        "ingest_allowed": ingest_allowed,
        "recall_allowed": recall_allowed,
        "capability_allowed": capability_allowed,
        "source_kind": source_kind,
        "source_uri": source_uri,
        "source_trust": source_trust,
        "trust_tier": trust_tier,
        "reasons": _dedupe(reasons),
        "redaction_reason": _redaction_reason(reasons),
    }


def source_trust_for_kind(source_kind: str) -> float:
    return float(SOURCE_TRUST.get(str(source_kind or "").strip().lower().replace("-", "_"), 0.5))


def trust_tier_for_score(value: float) -> str:
    if value >= 0.9:
        return "high"
    if value >= 0.6:
        return "medium"
    return "low"


def _payload(value: Any) -> dict[str, Any]:
    if hasattr(value, "to_dict"):
        return value.to_dict()
    if isinstance(value, Mapping):
        return dict(value)
    payload: dict[str, Any] = {}
    for key in ("kind", "status", "title", "summary", "detail", "source", "content", "meta", "provenance"):
        if hasattr(value, key):
            payload[key] = getattr(value, key)
    return payload


def _nested(payload: dict[str, Any], *keys: str) -> Any:
    current: Any = payload
    for key in keys:
        if not isinstance(current, Mapping):
            return None
        current = current.get(key)
    return current


def _first_text(*values: Any) -> str:
    for value in values:
        text = " ".join(str(value or "").split())
        if text:
            return text
    return ""


def _source_kind(payload: dict[str, Any]) -> str:
    return _first_text(
        _nested(payload, "meta", "source_kind"),
        _nested(payload, "content", "source_kind"),
        _nested(payload, "provenance", "source_kind"),
        payload.get("source_kind"),
        _nested(payload, "meta", "fetch_source"),
        _nested(payload, "content", "fetch_source"),
    ).lower().replace("-", "_")


def _source_uri(payload: dict[str, Any]) -> str:
    return _first_text(
        _nested(payload, "meta", "source_uri"),
        _nested(payload, "meta", "source_url"),
        _nested(payload, "meta", "item_url"),
        _nested(payload, "meta", "url"),
        _nested(payload, "content", "source_uri"),
        _nested(payload, "content", "source_url"),
        _nested(payload, "content", "item_url"),
        _nested(payload, "content", "url"),
        _nested(payload, "provenance", "source_uri"),
        _nested(payload, "provenance", "source_url"),
        payload.get("source_uri"),
        payload.get("uri"),
        payload.get("url"),
    )


def _source_trust(payload: dict[str, Any], *, source_kind: str) -> float:
    for value in (
        _nested(payload, "meta", "source_trust"),
        _nested(payload, "meta", "trust"),
        _nested(payload, "meta", "confidence"),
        _nested(payload, "content", "source_trust"),
        _nested(payload, "content", "trust"),
        _nested(payload, "content", "confidence"),
        payload.get("source_trust"),
        payload.get("trust"),
        payload.get("confidence"),
    ):
        try:
            return round(max(0.0, min(1.0, float(value))), 3)
        except (TypeError, ValueError):
            continue
    return source_trust_for_kind(source_kind)


def _is_external(payload: dict[str, Any], *, source_kind: str) -> bool:
    kind = str(payload.get("kind") or "").strip().lower()
    source = str(payload.get("source") or "").strip().lower()
    memory_type = _first_text(
        _nested(payload, "meta", "memory_type"),
        _nested(payload, "content", "memory_type"),
    ).lower()
    if kind in EXTERNAL_KINDS or memory_type in EXTERNAL_MEMORY_TYPES:
        return True
    if source_kind in SOURCE_TRUST:
        return True
    return any(marker in source for marker in ("knowledge", "news", "rss", "paper", "web"))


def _requires_strict_provenance(payload: dict[str, Any], *, source_kind: str) -> bool:
    kind = str(payload.get("kind") or "").strip().lower()
    source = str(payload.get("source") or "").strip().lower()
    memory_type = _first_text(
        _nested(payload, "meta", "memory_type"),
        _nested(payload, "content", "memory_type"),
    ).lower()
    if kind in {"knowledge_unit", "knowledge_candidate", "news", "source_candidate"}:
        return True
    if memory_type in EXTERNAL_MEMORY_TYPES:
        return True
    if source_kind in SOURCE_TRUST:
        return True
    return source in {"eimemory.knowledge_ingest", "eimemory.news.collect", "eimemory.intake.collect"}


def _status(payload: dict[str, Any]) -> str:
    return _first_text(
        payload.get("status"),
        _nested(payload, "meta", "status"),
        _nested(payload, "content", "status"),
    ).lower()


def _screening_text(payload: dict[str, Any]) -> str:
    values = [
        payload.get("title"),
        payload.get("summary"),
        payload.get("detail"),
        payload.get("text"),
        _nested(payload, "content", "text"),
        _nested(payload, "content", "summary"),
        _nested(payload, "content", "content_excerpt"),
        _nested(payload, "content", "body"),
    ]
    return "\n".join(str(value or "") for value in values if str(value or "").strip())


def _redaction_reason(reasons: list[str]) -> str:
    if "prompt_injection_detected" in reasons:
        return "prompt_injection_detected"
    if "secret_detected" in reasons:
        return "secret_detected"
    return ""


def _dedupe(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result
