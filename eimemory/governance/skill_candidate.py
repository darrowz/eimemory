from __future__ import annotations

from dataclasses import asdict
from hashlib import sha256
import re
from typing import Any

from eimemory.core.clock import now_iso
from eimemory.knowledge.source_trust import revalidate_source_trust_decision, source_trust_decision_from_payload
from eimemory.models.records import LinkRef, RecordEnvelope, ScopeRef, TimeRef
from eimemory.storage.runtime_store import RuntimeStore


SKILL_CANDIDATE_SOURCE = "eimemory.skill_candidate"
MAX_STORE_SCAN = 500

_TOOL_TERMS = {
    "apply_patch",
    "bash",
    "curl",
    "gh",
    "git",
    "node",
    "npm",
    "npx",
    "powershell",
    "pytest",
    "python",
    "rg",
    "uv",
}
_RISKY_TERMS = {
    "account",
    "credential",
    "delete",
    "deploy",
    "payment",
    "permission",
    "production",
    "secret",
    "token",
}


def extract_skill_candidates(
    store: RuntimeStore | None = None,
    *,
    knowledge_units: list[Any] | None = None,
    scope: ScopeRef | dict[str, Any] | None = None,
    persist: bool = False,
    limit: int = 100,
    source_registry: Any = None,
) -> dict[str, Any]:
    """Derive deterministic, inactive skill drafts from structured knowledge units."""
    scope_ref = _scope_from_inputs(scope=scope, knowledge_units=knowledge_units)
    source_units = list(knowledge_units) if knowledge_units is not None else _read_knowledge_units(store, scope_ref, limit=limit)
    candidates: list[dict[str, Any]] = []
    skipped: list[dict[str, str]] = []
    persisted_count = 0

    for unit in source_units[: max(0, int(limit))]:
        candidate, reason = _candidate_from_unit(
            unit,
            scope=scope_ref,
            source_registry=source_registry,
        )
        if candidate is None:
            skipped.append({"source_unit_id": _unit_id(unit), "reason": reason})
            continue
        candidates.append(candidate)
        if persist:
            if store is None:
                raise ValueError("store is required when persist=True")
            record = _candidate_record(candidate, unit, scope=scope_ref)
            if store.get_by_id(record.record_id, scope=scope_ref) is not None:
                continue
            store.append(record)
            persisted_count += 1

    return {
        "candidates": candidates,
        "persisted_count": persisted_count,
        "skipped_count": len(skipped),
        "explanation": _explanation(candidates=candidates, skipped=skipped, persist=persist),
        "skipped": skipped[:10],
    }


def _read_knowledge_units(store: RuntimeStore | None, scope: ScopeRef, *, limit: int) -> list[RecordEnvelope]:
    if store is None:
        return []
    return store.list_records(kinds=["knowledge_unit"], scope=scope, limit=min(MAX_STORE_SCAN, max(0, int(limit))))


def _candidate_from_unit(
    unit: Any,
    *,
    scope: ScopeRef,
    source_registry: Any = None,
) -> tuple[dict[str, Any] | None, str]:
    text = _unit_text(unit)
    quality = _quality_score(text)
    trust_decision = (
        revalidate_source_trust_decision(unit, registry=source_registry)
        if source_registry is not None
        else source_trust_decision_from_payload(unit)
    )
    source_trust = float(trust_decision.score) if trust_decision is not None else 0.0
    if quality < 0.28:
        return None, "low_quality_or_noisy"

    steps = _extract_steps(text)
    acceptance = _extract_acceptance_criteria(text)
    triggers = _extract_trigger_conditions(text, unit)
    tools = _extract_tools(text, unit)
    failure_handling = _extract_failure_handling(text)
    dependencies = _extract_dependencies(text, unit)
    risk_level = _risk_level(text=text, source_trust=source_trust, quality=quality)
    target_capability = _target_capability(unit, text)
    status = _status_for_candidate(steps=steps, acceptance=acceptance, source_trust=source_trust, risk_level=risk_level)

    candidate = {
        "trigger_conditions": triggers,
        "steps": steps,
        "tools_or_commands": tools,
        "failure_handling": failure_handling,
        "acceptance_criteria": acceptance,
        "dependencies": dependencies,
        "risk_level": risk_level,
        "source_trust": source_trust,
        "trust_authority": trust_decision.authority if trust_decision is not None else "unverified",
        "source_trust_decision": trust_decision.to_dict() if trust_decision is not None else {},
        "source_id": trust_decision.source_id if trust_decision is not None else "",
        "source_kind": _unit_source_kind(unit),
        "source_uri": trust_decision.normalized_uri if trust_decision is not None else "",
        "connector_id": trust_decision.connector_id if trust_decision is not None else "",
        "source_unit_ids": [_unit_id(unit)],
        "target_capability": target_capability,
        "status": status,
        "title": _candidate_title(unit, target_capability),
        "summary": _summary(text),
        "quality_score": quality,
        "generated_by": SKILL_CANDIDATE_SOURCE,
        "scope": asdict(scope),
    }
    return candidate, ""


def _candidate_record(candidate: dict[str, Any], unit: Any, *, scope: ScopeRef) -> RecordEnvelope:
    ts = now_iso()
    source_unit_id = _unit_id(unit)
    record_id = _stable_candidate_id(candidate, scope=scope)
    status = _safe_status(candidate.get("status"))
    risk_level = str(candidate.get("risk_level") or "medium")
    source_unit_ids = [str(item) for item in candidate.get("source_unit_ids") or []]
    target_capability = str(candidate.get("target_capability") or "skill.draft")
    return RecordEnvelope(
        record_id=record_id,
        kind="skill_candidate",
        status=status,
        title=str(candidate.get("title") or f"Skill candidate: {target_capability}"),
        summary=str(candidate.get("summary") or ""),
        detail="\n".join(str(step) for step in candidate.get("steps") or []),
        content={key: _json_safe(value) for key, value in candidate.items() if key != "scope"},
        tags=["skill-candidate", status, risk_level, target_capability],
        links=[LinkRef(relation="derived_from", target_kind="knowledge_unit", target_id=source_unit_id)] if source_unit_id else [],
        evidence=source_unit_ids,
        source=SKILL_CANDIDATE_SOURCE,
        scope=scope,
        time=TimeRef(created_at=ts, updated_at=ts, occurred_at=ts),
        provenance={
            "source": SKILL_CANDIDATE_SOURCE,
            "source_unit_ids": source_unit_ids,
            "source_unit_kind": str(getattr(unit, "kind", "") or _dict_get(unit, "kind") or "knowledge_unit"),
        },
        meta={
            "status": status,
            "risk_level": risk_level,
            "source_unit_ids": source_unit_ids,
            "source_trust": float(candidate.get("source_trust") or 0.0),
            "trust_authority": str(candidate.get("trust_authority") or "unverified"),
            "source_trust_decision": dict(candidate.get("source_trust_decision") or {}),
            "target_capability": target_capability,
        },
    )


def _scope_from_inputs(*, scope: ScopeRef | dict[str, Any] | None, knowledge_units: list[Any] | None) -> ScopeRef:
    if isinstance(scope, ScopeRef):
        return scope
    if scope is not None:
        return ScopeRef.from_dict(scope)
    for unit in knowledge_units or []:
        unit_scope = getattr(unit, "scope", None) or _dict_get(unit, "scope")
        if isinstance(unit_scope, ScopeRef):
            return unit_scope
        if isinstance(unit_scope, dict):
            return ScopeRef.from_dict(unit_scope)
    return ScopeRef()


def _unit_text(unit: Any) -> str:
    content = getattr(unit, "content", None) or _dict_get(unit, "content") or {}
    if not isinstance(content, dict):
        content = {}
    parts = [
        getattr(unit, "title", None) or _dict_get(unit, "title"),
        getattr(unit, "summary", None) or _dict_get(unit, "summary"),
        getattr(unit, "detail", None) or _dict_get(unit, "detail"),
        content.get("text"),
        content.get("summary"),
        content.get("body"),
    ]
    return _clean(" ".join(str(part or "") for part in parts))


def _unit_source_kind(unit: Any) -> str:
    content = getattr(unit, "content", None) or _dict_get(unit, "content") or {}
    meta = getattr(unit, "meta", None) or _dict_get(unit, "meta") or {}
    provenance = getattr(unit, "provenance", None) or _dict_get(unit, "provenance") or {}
    for payload in (meta, content, provenance):
        if isinstance(payload, dict) and str(payload.get("source_kind") or "").strip():
            return str(payload["source_kind"]).strip().lower().replace("-", "_")
    return ""


def _extract_steps(text: str) -> list[str]:
    after_steps = _section_after(text, ("steps:", "step:", "procedure:", "workflow:"))
    numbered = re.findall(r"(?:^|[;\n.]|\s)(?:\d+[\.)]\s+)([^.;\n]+)", after_steps or text, flags=re.IGNORECASE)
    if numbered:
        return [_sentence(item) for item in numbered if _sentence(item)][:6]

    imperative = []
    for sentence in _sentences(after_steps or text):
        lowered = sentence.lower()
        if any(lowered.startswith(verb) for verb in ("verify ", "inspect ", "extract ", "write ", "run ", "record ", "define ", "list ", "capture ", "summarize ")):
            imperative.append(sentence)
    if imperative:
        return imperative[:6]
    return [_sentence(item) for item in _sentences(text)[:3] if _sentence(item)]


def _extract_acceptance_criteria(text: str) -> list[str]:
    section = _section_after(text, ("acceptance criteria:", "acceptance:", "success criteria:", "verify:"))
    criteria = _split_items(section)
    if criteria:
        return criteria[:5]
    if "test" in text.lower() or "verify" in text.lower():
        return ["Verify the drafted skill against the source knowledge before promotion."]
    return []


def _extract_trigger_conditions(text: str, unit: Any) -> list[str]:
    triggers = []
    for pattern in (r"\bwhen\s+([^.;]+)", r"\btrigger(?:s| this skill)?(?: when|:)\s+([^.;]+)", r"\bif\s+([^.;]+)"):
        triggers.extend(_sentence(match) for match in re.findall(pattern, text, flags=re.IGNORECASE))
    if not triggers:
        title = str(getattr(unit, "title", "") or _dict_get(unit, "title") or "").strip()
        triggers.append(f"Use when handling {title or 'the described workflow'}.")
    return _dedupe(triggers)[:4]


def _extract_tools(text: str, unit: Any) -> list[str]:
    content = getattr(unit, "content", None) or _dict_get(unit, "content") or {}
    meta = getattr(unit, "meta", None) or _dict_get(unit, "meta") or {}
    provided = []
    for payload in (content, meta):
        if isinstance(payload, dict):
            provided.extend(_as_list(payload.get("tools") or payload.get("commands") or payload.get("tools_or_commands")))
    tokens = {term for term in _TOOL_TERMS if re.search(rf"(?<![\w-]){re.escape(term)}(?![\w-])", text, flags=re.IGNORECASE)}
    commands = re.findall(r"\b(?:python|pytest|npm|npx|git|gh|rg|uv)\b[^.;\n]*", text, flags=re.IGNORECASE)
    return _dedupe([*provided, *sorted(tokens), *commands])[:8]


def _extract_failure_handling(text: str) -> list[str]:
    section = _section_after(text, ("failure handling:", "fallback:", "rollback:", "if fails:"))
    items = _split_items(section)
    if items:
        return items[:4]
    lowered = text.lower()
    if "unsafe" in lowered or "risk" in lowered:
        return ["Keep the draft as candidate and require review when safety or provenance is uncertain."]
    return ["Keep the draft inactive and require review if extraction confidence is low."]


def _extract_dependencies(text: str, unit: Any) -> list[str]:
    content = getattr(unit, "content", None) or _dict_get(unit, "content") or {}
    meta = getattr(unit, "meta", None) or _dict_get(unit, "meta") or {}
    dependencies = []
    for payload in (content, meta):
        if isinstance(payload, dict):
            dependencies.extend(_as_list(payload.get("dependencies")))
    if "pytest" in text.lower():
        dependencies.append("pytest")
    if "github" in text.lower() or "gh " in text.lower():
        dependencies.append("github")
    return _dedupe(dependencies)


def _target_capability(unit: Any, text: str) -> str:
    content = getattr(unit, "content", None) or _dict_get(unit, "content") or {}
    meta = getattr(unit, "meta", None) or _dict_get(unit, "meta") or {}
    for payload in (content, meta):
        if isinstance(payload, dict) and payload.get("target_capability"):
            return str(payload["target_capability"])
    lowered = text.lower()
    if "skill" in lowered:
        return "skill.draft"
    if "knowledge" in lowered:
        return "knowledge.workflow"
    return "workflow.skill_candidate"


def _risk_level(*, text: str, source_trust: float, quality: float) -> str:
    lowered = text.lower()
    if source_trust < 0.35 or quality < 0.38:
        return "high"
    if any(term in lowered for term in _RISKY_TERMS):
        return "high"
    if source_trust < 0.65:
        return "medium"
    return "low"


def _status_for_candidate(*, steps: list[str], acceptance: list[str], source_trust: float, risk_level: str) -> str:
    if risk_level == "low" and source_trust >= 0.65 and len(steps) >= 3 and len(acceptance) >= 1:
        return "sandbox_ready"
    return "candidate"


def _quality_score(text: str) -> float:
    words = re.findall(r"[\w]+", text, flags=re.UNICODE)
    if not words:
        return 0.0
    unique_ratio = len({word.lower() for word in words}) / max(1, len(words))
    workflow_hits = sum(
        1
        for marker in ("when", "steps", "tools", "failure", "acceptance", "criteria", "verify", "test")
        if marker in text.lower()
    )
    length_score = min(0.42, len(words) / 90)
    return round(min(1.0, length_score + min(0.42, workflow_hits * 0.07) + min(0.16, unique_ratio * 0.16)), 3)


def _stable_candidate_id(candidate: dict[str, Any], *, scope: ScopeRef) -> str:
    payload = "\x1f".join(
        [
            str(candidate.get("target_capability") or ""),
            ",".join(str(item) for item in candidate.get("source_unit_ids") or []),
            str(candidate.get("summary") or ""),
            scope.tenant_id,
            scope.agent_id,
            scope.workspace_id,
            scope.user_id,
        ]
    )
    return f"skillcand_{sha256(payload.encode('utf-8')).hexdigest()[:16]}"


def _safe_status(value: Any) -> str:
    text = str(value or "candidate").strip().lower()
    if text == "active":
        return "candidate"
    if text in {"candidate", "sandbox_ready"}:
        return text
    return "candidate"


def _candidate_title(unit: Any, target_capability: str) -> str:
    title = str(getattr(unit, "title", "") or _dict_get(unit, "title") or target_capability).strip()
    return f"Skill candidate: {title[:80]}"


def _unit_id(unit: Any) -> str:
    return str(getattr(unit, "record_id", "") or _dict_get(unit, "record_id") or "").strip()


def _section_after(text: str, markers: tuple[str, ...]) -> str:
    lowered = text.lower()
    positions = [(lowered.find(marker), marker) for marker in markers if lowered.find(marker) >= 0]
    if not positions:
        return ""
    start, marker = min(positions, key=lambda item: item[0])
    section = text[start + len(marker) :]
    next_match = re.search(r"\b(?:failure handling|acceptance criteria|acceptance|tools|dependencies|trigger):", section, flags=re.IGNORECASE)
    if next_match:
        section = section[: next_match.start()]
    return section.strip()


def _split_items(text: str) -> list[str]:
    if not text:
        return []
    items = re.split(r"(?:\s*\d+[\.)]\s+|[;\n]+|\s+-\s+)", text)
    return [_sentence(item) for item in items if _sentence(item)]


def _sentences(text: str) -> list[str]:
    return [_sentence(item) for item in re.split(r"(?<=[.!?])\s+|[;\n]+", text) if _sentence(item)]


def _sentence(text: Any) -> str:
    value = _clean(text).strip(" -:,.")
    if not value:
        return ""
    return value[:240]


def _summary(text: str) -> str:
    return _clean(text)[:240]


def _clean(value: Any) -> str:
    return " ".join(str(value or "").split())


def _dedupe(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        text = _sentence(value)
        key = text.casefold()
        if not text or key in seen:
            continue
        seen.add(key)
        result.append(text)
    return result


def _as_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [item.strip() for item in re.split(r"[,;\n]+", value) if item.strip()]
    if isinstance(value, (list, tuple, set)):
        return [str(item).strip() for item in value if str(item).strip()]
    return [str(value).strip()] if str(value).strip() else []


def _clamp_float(value: Any, *, default: float) -> float:
    try:
        return round(max(0.0, min(1.0, float(value))), 3)
    except (TypeError, ValueError):
        return default


def _dict_get(value: Any, key: str) -> Any:
    if isinstance(value, dict):
        return value.get(key)
    return None


def _json_safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(item) for item in value]
    if isinstance(value, ScopeRef):
        return asdict(value)
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def _explanation(*, candidates: list[dict[str, Any]], skipped: list[dict[str, str]], persist: bool) -> str:
    sandbox_ready = sum(1 for candidate in candidates if candidate.get("status") == "sandbox_ready")
    return (
        f"Derived {len(candidates)} inactive skill candidate draft(s); "
        f"{sandbox_ready} marked sandbox_ready, {len(skipped)} skipped as low-quality/noisy, "
        f"persist={'on' if persist else 'off'}."
    )
