from __future__ import annotations

from hashlib import sha256
import re
from typing import Any

from eimemory.models.records import RecordEnvelope, ScopeRef, TimeRef


KNOWLEDGE_INGEST_SOURCE = "eimemory.knowledge_ingest"
SUPPORTED_SOURCE_KINDS = {
    "github_repo",
    "paper",
    "webpage",
    "docs",
    "api_docs",
    "feishu_doc",
    "manual",
    "official_docs",
    "blog",
    "summary",
}
UNIT_TYPES = {
    "concept",
    "procedure",
    "constraint",
    "use_case",
    "anti_pattern",
    "verification",
}

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
}


def ingest_knowledge_source(
    runtime: Any,
    payload: dict[str, Any],
    *,
    scope: dict[str, Any] | None = None,
    persist: bool = False,
) -> dict[str, Any]:
    """Create compact knowledge units from input source text."""
    scope_ref = scope if isinstance(scope, ScopeRef) else ScopeRef.from_dict(scope)
    source_kind = _normalize_source_kind(payload.get("source_kind", ""))
    title = _clean_text(payload.get("title", ""))
    source_uri = _clean_text(payload.get("uri", ""))
    source_text = _clean_text(payload.get("text", ""))

    if not source_kind:
        raise ValueError("source_kind is required and must be supported")
    if not source_text:
        raise ValueError("text is required")

    source_trust = _source_trust(source_kind)
    trust_tier = _trust_tier(source_trust)

    units = _extract_units(title=title, text=source_text, source_kind=source_kind)
    for unit in units:
        unit["source_kind"] = source_kind
        unit["source_uri"] = source_uri
        unit["source_trust"] = source_trust
        unit["trust_tier"] = trust_tier

    persisted_record_ids: list[str] = []
    if persist:
        for unit in units:
            record = _unit_record(unit=unit, scope=scope_ref, source_uri=source_uri, source_kind=source_kind)
            runtime.store.append(record)
            persisted_record_ids.append(record.record_id)
            unit["record_id"] = record.record_id

    return {
        "ok": True,
        "persist": bool(persist),
        "source_kind": source_kind,
        "source_uri": source_uri,
        "source_trust": source_trust,
        "trust_tier": trust_tier,
        "knowledge_units": units,
        "persisted_count": len(persisted_record_ids),
        "persisted_record_ids": persisted_record_ids,
    }


def _normalize_source_kind(value: Any) -> str:
    lowered = str(value or "").strip().lower().replace("-", "_")
    if lowered in {"", "unsupported", "unknown"}:
        return ""
    if lowered == "documentation":
        lowered = "docs"
    if lowered not in SUPPORTED_SOURCE_KINDS:
        return ""
    return lowered


def _source_trust(source_kind: str) -> float:
    return float(SOURCE_TRUST.get(source_kind, 0.5))


def _trust_tier(value: float) -> str:
    if value >= 0.9:
        return "high"
    if value >= 0.6:
        return "medium"
    return "low"


def _extract_units(*, title: str, text: str, source_kind: str) -> list[dict[str, Any]]:
    if source_kind not in SUPPORTED_SOURCE_KINDS:
        raise ValueError(f"unsupported source_kind: {source_kind}")

    parts = _candidate_texts(title=title, text=text)
    units: list[dict[str, Any]] = []
    seen: set[str] = set()
    for candidate in parts:
        body = _to_summary(candidate)
        if not body or body in seen:
            continue
        seen.add(body)
        units.append({"title": title, "unit_type": _classify_unit_type(body), "text": body})

    _ensure_minimum_coverage(units=units, seen=seen, source_text=text, title=title)
    return units


def _ensure_minimum_coverage(
    *,
    units: list[dict[str, Any]],
    seen: set[str],
    source_text: str,
    title: str,
) -> None:
    required_types = ("concept", "procedure", "verification")
    existing_types = {unit["unit_type"] for unit in units}
    cleaned_text = _clean_text(source_text)

    for unit_type in required_types:
        if unit_type in existing_types:
            continue
        fallback = _fallback_text(unit_type, text=cleaned_text, title=title)
        if fallback and fallback not in seen:
            seen.add(fallback)
            units.append({"title": title, "unit_type": unit_type, "text": fallback})


def _fallback_text(unit_type: str, *, text: str, title: str) -> str:
    if unit_type == "procedure":
        candidate = _first_match_sentence(text, ["install", "setup", "run", "execute", "configure", "step", "steps"])
        if candidate:
            return candidate
        return _to_summary(f"Follow the documented procedure for {title}.")
    if unit_type == "verification":
        candidate = _first_match_sentence(text, ["test", "verify", "validation", "ci", "assert", "check"])
        if candidate:
            return candidate
        return _to_summary("Verify the described behavior before promotion.")
    return _to_summary(title or "Core concept from source text.")


def _first_match_sentence(text: str, terms: list[str]) -> str:
    lowered_terms = [term.lower() for term in terms]
    for sentence in _sentences(text):
        lowered = sentence.lower()
        if any(term in lowered for term in lowered_terms):
            return _to_summary(sentence)
    return ""


def _candidate_texts(*, title: str, text: str) -> list[str]:
    parts: list[str] = [title] if title else []

    for line in text.splitlines():
        line = _clean_text(line)
        if not line:
            continue
        if _is_list_item(line) or _is_heading(line):
            parts.append(line)
            continue
        if _is_code_like(line):
            continue
        if len(line) > 24:
            parts.append(line)

    parts.extend([sentence for sentence in _sentences(text) if sentence])
    return parts


def _classify_unit_type(text: str) -> str:
    lowered = text.lower()
    if any(term in lowered for term in ("anti-pattern", "anti pattern", "don't", "do not", "never", "must not", "prohibit")):
        return "anti_pattern"
    if any(term in lowered for term in ("must", "required", "required:", "shall", "at most", "at least", "only if", "constraint")):
        return "constraint"
    if any(term in lowered for term in ("verify", "test", "assert", "check", "validation", "ci", "lints", "lint")):
        return "verification"
    if any(term in lowered for term in ("when", "if", "scenario", "for example", "use case", "in case")):
        return "use_case"
    if any(term in lowered for term in ("1.", "step", "steps", "install", "run", "configure", "set up", "create", "add", "apply")):
        return "procedure"
    return "concept"


def _sentences(text: str) -> list[str]:
    return [_clean_text(part) for part in re.split(r"\.\s+|\n+", text) if _clean_text(part)]


def _is_heading(line: str) -> bool:
    if line.startswith("#"):
        return True
    if line.endswith(":") and len(line) < 160:
        lowered = line.lower()
        return any(
            token in lowered
            for token in (
                "install",
                "procedure",
                "usage",
                "feature",
                "verify",
                "verification",
                "constraints",
                "example",
            )
        )
    return False


def _is_list_item(line: str) -> bool:
    return bool(re.match(r"^(\s*[-*+]|\s*\d+[.)])\s+.+", line))


def _is_code_like(line: str) -> bool:
    return bool(re.match(r"^(\s|`{1,3}).*|.*`.*`$|\s{4,}.*", line))


def _to_summary(text: str, max_chars: int = 220) -> str:
    clean = _clean_text(text)
    if not clean:
        return ""
    if len(clean) <= max_chars:
        return clean
    return clean[:max_chars].rstrip() + "..."


def _clean_text(text: Any) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()


def _unit_record(
    *,
    unit: dict[str, Any],
    scope: ScopeRef,
    source_uri: str,
    source_kind: str,
) -> RecordEnvelope:
    unit_type = str(unit.get("unit_type") or "concept")
    if unit_type not in UNIT_TYPES:
        unit_type = "concept"
    unit_text = _to_summary(str(unit.get("text") or ""), max_chars=210)
    unit_title = _to_summary(str(unit.get("title") or ""), max_chars=90)
    title = f"{unit_type}: {unit_title}".strip() if unit_title else f"{unit_type}: knowledge unit"
    return RecordEnvelope(
        record_id=_unit_record_id(
            source_uri=source_uri,
            source_kind=source_kind,
            unit_type=unit_type,
            text=unit_text,
            title=title,
        ),
        kind="knowledge_unit",
        status="active",
        title=title,
        summary=unit_text,
        detail=unit_text,
        content={
            "text": unit_text,
            "unit_type": unit_type,
            "source_kind": source_kind,
            "source_uri": source_uri,
        },
        tags=[unit_type],
        links=[],
        evidence=[],
        source=KNOWLEDGE_INGEST_SOURCE,
        scope=scope,
        time=TimeRef.now(),
        provenance={"source_kind": source_kind, "source_uri": source_uri, "unit_type": unit_type},
        meta={
            "source_kind": source_kind,
            "source_uri": source_uri,
            "source_trust": float(unit.get("source_trust") or 0.0),
            "trust_tier": str(unit.get("trust_tier") or "low"),
            "unit_type": unit_type,
        },
    )


def _unit_record_id(*, source_uri: str, source_kind: str, unit_type: str, text: str, title: str) -> str:
    payload = "\x1f".join([source_uri, source_kind, unit_type, text, title]).encode("utf-8")
    return f"ku_{sha256(payload).hexdigest()[:16]}"
