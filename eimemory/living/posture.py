from __future__ import annotations

from collections import Counter
import re
from dataclasses import dataclass
from typing import Any

from eimemory.living.schema import enrich_living_memory, get_living_memory_meta, has_living_memory_meta
from eimemory.models.records import ScopeRef


STRICT_KEYWORDS = (
    "外部订单",
    "交付",
    "验收",
    "品质",
    "清单",
    "证据",
    "不要臆测",
    "先确认",
    "delivery",
    "acceptance",
    "checklist",
)
CONCISE_KEYWORDS = (
    "no_fluff",
    "不要废话",
    "废话",
    "简洁",
    "精简",
    "极简",
    "直接",
    "concise",
    "short",
    "清楚",
)
REPAIR_KEYWORDS = (
    "repair",
    "修复",
    "trust",
    "信任",
    "boundary",
    "边界",
    "respect_boundary",
    "尊重边界",
    "broke my trust",
    "broken trust",
)
CONCERN_KEYWORDS = ("证据", "确认", "先确认", "确认后", "evidence")
RELEVANT_MEMORY_TYPES = {
    "preference",
    "operator.correction",
    "project",
    "incident",
    "incident_repair",
    "incident repair",
}
KNOWN_ACTIONS = ("act", "nudge", "wait", "let_go")
VALID_MODES = ("strict_checklist", "concise_preference", "repair_first", "balanced")


@dataclass(slots=True)
class CompiledPosture:
    recommended_action: str
    mode: str
    constraints: list[str]
    scope: dict[str, str]
    confidence: float
    source_record_ids: list[str]
    source_titles: list[str]


def compile_living_posture_report(runtime, query: str, scope: dict | ScopeRef | None, limit: int) -> dict[str, Any]:
    if limit <= 0:
        return {"ok": False, "error": "invalid_limit"}

    scope_ref = _scope_ref(scope)
    cleaned_query = _clean_text(query)

    if not cleaned_query:
        return {
            "ok": True,
            "scope": _scope_payload(scope_ref),
            "query": query,
            "record_count": 0,
            "profile": _empty_profile(),
            "items": [],
        }

    records = runtime.store.search(
        query=cleaned_query,
        kinds=["memory", "rule", "incident"],
        scope=scope_ref,
        limit=limit,
    )
    posture_records = [
        record
        for record in records
        if getattr(record, "status", "").strip().lower() != "rejected"
        if _record_is_posture_relevant(record, cleaned_query)
    ]
    if len(posture_records) < limit:
        posture_records.extend(_collect_relevant_records(runtime, cleaned_query, scope_ref, limit, posture_records))
        posture_records = posture_records[:limit]
    if not posture_records and records:
        posture_records = records[:limit]

    if not posture_records:
        return {
            "ok": True,
            "scope": _scope_payload(scope_ref),
            "query": query,
            "record_count": 0,
            "profile": {
                **_empty_profile(),
                "scope": _scope_from_query_and_records(cleaned_query, []),
            },
            "items": [],
        }

    profile = _build_profile(cleaned_query, posture_records)
    return {
        "ok": True,
        "scope": _scope_payload(scope_ref),
        "query": query,
        "record_count": len(posture_records),
        "profile": {
            "recommended_action": profile.recommended_action,
            "mode": profile.mode,
            "constraints": profile.constraints,
            "scope": profile.scope,
            "confidence": profile.confidence,
            "source_record_ids": profile.source_record_ids,
            "source_titles": profile.source_titles,
        },
        "items": [_record_item(record) for record in posture_records],
    }


def _build_profile(query: str, records: list[Any]) -> CompiledPosture:
    action_votes: Counter[str] = Counter()
    mode_signals = {"strict": 0, "concise": 0, "repair": 0}
    constraints: set[str] = set()
    source_record_ids: list[str] = []
    source_titles: list[str] = []

    for record in records:
        source_record_ids.append(str(getattr(record, "record_id", "")))
        source_titles.append(str(getattr(record, "title", "")))
        text = _record_text(record)
        living = _resolve_living(record)
        posture = living.get("action_posture", {})
        action = str(posture.get("recommended", "wait"))
        if action in KNOWN_ACTIONS:
            action_votes[action] += 1
        if _has_any(query, STRICT_KEYWORDS) or _has_any(text, STRICT_KEYWORDS):
            mode_signals["strict"] += 1
            constraints.add("strict_acceptance")
            constraints.add("evidence_required")
        if _has_any(query, CONCERN_KEYWORDS) or _has_any(text, CONCERN_KEYWORDS):
            mode_signals["strict"] += 1
            constraints.add("ask_before_assume")
        if _has_any(query, CONCISE_KEYWORDS) or _has_any(text, CONCISE_KEYWORDS):
            mode_signals["concise"] += 1
            constraints.add("no_fluff")
        if _has_any(query, REPAIR_KEYWORDS) or _has_any(text, REPAIR_KEYWORDS):
            mode_signals["repair"] += 1
            constraints.add("repair_trust")
            constraints.add("respect_boundary")
        affective = living.get("affective") if isinstance(living.get("affective"), dict) else {}
        motive = living.get("motive") if isinstance(living.get("motive"), dict) else {}
        if affective.get("repair_needed") is True:
            mode_signals["repair"] += 1
            constraints.add("repair_trust")
        if (motive.get("trust_delta") or 0) < 0:
            mode_signals["repair"] += 1
            constraints.add("repair_trust")
        boundaries = motive.get("boundary") if isinstance(motive.get("boundary"), list) else []
        if "respect_boundary" in {str(item).strip() for item in boundaries}:
            mode_signals["repair"] += 1
            constraints.add("respect_boundary")

    strict_mode = mode_signals["strict"] >= 1
    repair_mode = mode_signals["repair"] >= 1
    concise_mode = mode_signals["concise"] >= 1 and not strict_mode

    if strict_mode:
        mode = "strict_checklist"
    elif repair_mode:
        mode = "repair_first"
    elif concise_mode:
        mode = "concise_preference"
    else:
        mode = "balanced"

    if mode not in VALID_MODES:
        mode = "balanced"

    recommended = _select_recommended_action(action_votes, mode, strict_mode, repair_mode)
    total_votes = max(1, sum(action_votes.values()))
    dominant_ratio = action_votes[recommended] / total_votes if total_votes else 0.0
    confidence = 0.5 + min(0.5, dominant_ratio * 0.45)
    if mode in {"strict_checklist", "repair_first"}:
        confidence = min(1.0, confidence + 0.15)
    confidence = round(confidence, 3)
    if strict_mode and "evidence_required" not in constraints and "strict_acceptance" not in constraints:
        constraints.add("strict_acceptance")

    return CompiledPosture(
        recommended_action=recommended,
        mode=mode,
        constraints=sorted(constraints),
        scope=_scope_from_query_and_records(query, records),
        confidence=confidence,
        source_record_ids=source_record_ids,
        source_titles=source_titles,
    )


def _select_recommended_action(action_votes: Counter[str], mode: str, strict_mode: bool, repair_mode: bool) -> str:
    if (mode in {"strict_checklist", "repair_first"} and strict_mode) or repair_mode:
        return "act"
    if mode == "concise_preference":
        return "nudge"
    if not action_votes:
        return "wait"
    return action_votes.most_common(1)[0][0]


def _record_item(record: Any) -> dict[str, Any]:
    living = _resolve_living(record)
    posture = living.get("action_posture", {})
    affective = living.get("affective", {})
    return {
        "record_id": str(getattr(record, "record_id", "")),
        "title": str(getattr(record, "title", "")),
        "kind": _record_kind(record),
        "posture": dict(posture),
        "recommended_action": str(posture.get("recommended", "wait")),
        "repair_needed": bool(affective.get("repair_needed")),
        "memory_type": _memory_type(record),
    }


def _record_is_posture_relevant(record: Any, query: str) -> bool:
    if _is_internal_audit_record(record):
        return False
    kind = _record_kind(record)
    text = _record_text(record)
    if kind == "rule":
        return True
    if kind == "incident":
        return _has_any(text + " " + str(query), REPAIR_KEYWORDS + STRICT_KEYWORDS + CONCISE_KEYWORDS + CONCERN_KEYWORDS)
    memory_type = _memory_type(record)
    if not memory_type:
        return _has_any(text + " " + str(query), STRICT_KEYWORDS + CONCISE_KEYWORDS + REPAIR_KEYWORDS + CONCERN_KEYWORDS)
    return memory_type in RELEVANT_MEMORY_TYPES


def _collect_relevant_records(
    runtime,
    query: str,
    scope_ref: ScopeRef,
    limit: int,
    existing_records: list[Any],
) -> list[Any]:
    existing_ids = {record.record_id for record in existing_records}
    collected: list[Any] = []
    for kind in ("memory", "rule", "incident"):
        records = runtime.store.list_records(kinds=[kind], scope=scope_ref, limit=limit)
        for record in records:
            if record.record_id in existing_ids:
                continue
            if getattr(record, "status", "") == "rejected":
                continue
            if not _record_is_posture_relevant(record, query):
                continue
            collected.append(record)
            if len(collected) + len(existing_records) >= limit:
                break
        if len(collected) + len(existing_records) >= limit:
            break
    return collected


def _resolve_living(record: Any) -> dict[str, Any]:
    if has_living_memory_meta(record):
        return get_living_memory_meta(record)
    payload = {
        "title": str(getattr(record, "title", "")),
        "summary": str(getattr(record, "summary", "")),
        "detail": str(getattr(record, "detail", "")),
        "content": dict(getattr(record, "content", {})),
        "meta": _record_meta(record),
    }
    return enrich_living_memory(payload, meta=_record_meta(record))


def _scope_from_query_and_records(query: str, records: list[Any]) -> dict[str, str]:
    project = ""
    task_type = ""
    query_tokens = _split_terms(query)
    for token in query_tokens:
        if _looks_like_project_token(token):
            project = token
            break

    bucket = " ".join([str(query).lower(), *(_record_text(record).lower() for record in records)])
    if _contains_any(bucket, ("外部订单", "交付", "验收", "品质", "清单", "delivery", "acceptance", "checklist", "quality")):
        task_type = "delivery_acceptance"
    if not task_type and _contains_any(bucket, ("沟通", "沟通风格", "风格", "回复", "reply", "chat")):
        task_type = "chat_reply"

    for record in records:
        meta = _record_meta(record)
        meta_project = str(meta.get("project") or meta.get("project_name") or "").strip()
        if meta_project and not project:
            project = meta_project
        meta_task = str(meta.get("task_type") or meta.get("task") or "").strip()
        if not task_type and meta_task:
            task_type = meta_task

    if not project:
        for token in query_tokens:
            if not _contains_any(token, ("preference", "鸿哥", "质量", "修复", "边界", "信任")):
                project = token
                break
    return {"project": project, "task_type": task_type}


def _looks_like_project_token(token: str) -> bool:
    token = str(token or "").strip().strip("/,:;，。.!!?").strip()
    if not token or len(token) < 2:
        return False
    return bool(re.search(r"[A-Za-z][A-Za-z0-9_-]*|[\u4e00-\u9fff]+[A-Za-z0-9_-]*", token))


def _split_terms(text: str) -> list[str]:
    raw = re.split(r"[\s/,:;，。！？!？·、]+", str(text or "").strip())
    return [item.strip() for item in raw if item.strip()]


def _scope_ref(scope: dict | ScopeRef | None) -> ScopeRef:
    return scope if isinstance(scope, ScopeRef) else ScopeRef.from_dict(scope)


def _scope_payload(scope_ref: ScopeRef) -> dict[str, str]:
    return {
        "tenant_id": scope_ref.tenant_id,
        "agent_id": scope_ref.agent_id,
        "workspace_id": scope_ref.workspace_id,
        "user_id": scope_ref.user_id,
    }


def _record_kind(record: Any) -> str:
    return str(getattr(record, "kind", "")).strip().lower()


def _memory_type(record: Any) -> str:
    meta = _record_meta(record)
    content = getattr(record, "content", {})
    return str(meta.get("memory_type") or (content.get("memory_type") if isinstance(content, dict) else "") or "").strip().lower()


def _record_meta(record: Any) -> dict[str, Any]:
    meta = getattr(record, "meta", {})
    return meta if isinstance(meta, dict) else {}


def _record_text(record: Any) -> str:
    if isinstance(record, dict):
        content = record.get("content", {})
    else:
        content = getattr(record, "content", {})
    content_text = ""
    if isinstance(content, dict):
        content_text = str(content.get("text", ""))
    text_parts = (
        str(getattr(record, "title", "")),
        str(getattr(record, "summary", "")),
        str(getattr(record, "detail", "")),
        content_text,
    )
    return " ".join(part for part in text_parts if part.strip())


def _has_any(text: str, needles: tuple[str, ...]) -> bool:
    normalized = str(text or "").lower()
    return any(needle in normalized for needle in map(str.lower, needles))


def _contains_any(text: str, needles: tuple[str, ...]) -> bool:
    return _has_any(text, tuple(needle.lower() for needle in needles))


def _empty_profile() -> dict[str, Any]:
    return {
        "recommended_action": "wait",
        "mode": "balanced",
        "constraints": [],
        "scope": {"project": "", "task_type": ""},
        "confidence": 0.0,
        "source_record_ids": [],
        "source_titles": [],
    }


def _clean_text(value: Any) -> str:
    return str(value or "").strip()


def _is_internal_audit_record(item: Any) -> bool:
    meta = _record_meta(item)
    content = getattr(item, "content", {})
    sources = {str(getattr(item, "source", "")).strip()}
    for key in ("source", "source_channel", "communication_channel"):
        source_value = meta.get(key)
        if source_value is None and isinstance(content, dict):
            source_value = content.get(key)
        if source_value:
            sources.add(str(source_value).strip())
    memory_type = str(meta.get("memory_type") or (content.get("memory_type") if isinstance(content, dict) else "") or "").strip().lower()
    return (
        memory_type == "audit"
        or "ei_bridge.openclaw_feishu" in sources
        or str(getattr(item, "title", "")).strip().lower() == "ei-bridge openclaw command audit"
    )
