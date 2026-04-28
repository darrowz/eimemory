from __future__ import annotations

import json
from dataclasses import asdict, is_dataclass
from datetime import date as date_type
from datetime import datetime
from pathlib import Path
from typing import Any

from eimemory.models.records import RecordEnvelope, ScopeRef


TRACE_REQUIRED_FIELDS = (
    "trace_id",
    "task_type",
    "input_summary",
    "selected_skills",
    "actions",
    "outcome",
    "feedback",
    "latency_ms",
)


def record_skill_trace(runtime: Any, payload: dict[str, Any], scope: dict | ScopeRef | None = None) -> dict[str, Any]:
    error = _validate_skill_trace(payload)
    if error:
        return {"ok": False, "error": error}

    safe_payload = _json_safe(payload)
    selected_skill_ids = _skill_ids(safe_payload.get("selected_skills"))
    record = RecordEnvelope.create(
        kind="reflection",
        title=f"Skill execution trace: {safe_payload['trace_id']}",
        summary=str(safe_payload.get("input_summary") or ""),
        detail=_brief_detail(safe_payload),
        content=safe_payload,
        tags=["experience", "skill_trace", str(safe_payload.get("task_type") or "")],
        source="eimemory.experience.skill_trace",
        scope=_scope_ref(scope),
        provenance={
            "report_type": "skill_trace",
            "trace_id": str(safe_payload.get("trace_id") or ""),
        },
        meta={
            "report_type": "skill_trace",
            "selected_skill_ids": selected_skill_ids,
            "outcome": safe_payload.get("outcome"),
            "task_type": str(safe_payload.get("task_type") or ""),
        },
    )
    stored = runtime.store.append(record)
    return {"ok": True, "record_id": stored.record_id, "kind": stored.kind}


def record_experience_item(runtime: Any, payload: dict[str, Any], scope: dict | ScopeRef | None = None) -> dict[str, Any]:
    error = _validate_experience_item(payload)
    if error:
        return {"ok": False, "error": error}

    safe_payload = _json_safe(payload)
    record = RecordEnvelope.create(
        kind="reflection",
        title=f"Experience item: {safe_payload['experience_kind']}",
        summary=str(safe_payload.get("summary") or safe_payload.get("experience_id") or safe_payload["experience_kind"]),
        detail=_brief_detail(safe_payload),
        content=safe_payload,
        tags=["experience", "experience_item", str(safe_payload.get("experience_kind") or "")],
        source="eimemory.experience.item",
        scope=_scope_ref(scope),
        provenance={
            "report_type": "experience_item",
            "experience_id": str(safe_payload.get("experience_id") or ""),
        },
        meta={
            "experience_kind": str(safe_payload.get("experience_kind") or ""),
            "skill_ids": _string_list(safe_payload.get("skill_ids")),
            "confidence": safe_payload.get("confidence"),
            "outcome_delta": safe_payload.get("outcome_delta"),
        },
    )
    stored = runtime.store.append(record)
    return {"ok": True, "record_id": stored.record_id, "kind": stored.kind}


def _validate_skill_trace(payload: object) -> str:
    if not isinstance(payload, dict):
        return "payload must be an object"
    missing = [field for field in TRACE_REQUIRED_FIELDS if field not in payload]
    if missing:
        return f"missing required fields: {', '.join(missing)}"
    if not str(payload.get("trace_id") or "").strip():
        return "trace_id is required"
    if not str(payload.get("task_type") or "").strip():
        return "task_type is required"
    if not isinstance(payload.get("selected_skills"), list):
        return "selected_skills must be a list"
    if not isinstance(payload.get("actions"), list):
        return "actions must be a list"
    return ""


def _validate_experience_item(payload: object) -> str:
    if not isinstance(payload, dict):
        return "payload must be an object"
    if not str(payload.get("experience_kind") or "").strip():
        return "experience_kind is required"
    if "skill_ids" in payload and not isinstance(payload.get("skill_ids"), list):
        return "skill_ids must be a list"
    return ""


def _scope_ref(scope: dict | ScopeRef | None) -> ScopeRef:
    if isinstance(scope, ScopeRef):
        return scope
    return ScopeRef.from_dict(scope)


def _skill_ids(skills: object) -> list[str]:
    if not isinstance(skills, list):
        return []
    ids: list[str] = []
    for item in skills:
        if isinstance(item, dict):
            value = item.get("skill_id") or item.get("id") or item.get("name")
        else:
            value = item
        text = str(value or "").strip()
        if text:
            ids.append(text)
    return ids


def _string_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if str(item)]


def _brief_detail(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)[:1200]


def _json_safe(value: Any) -> Any:
    if is_dataclass(value) and not isinstance(value, type):
        return _json_safe(asdict(value))
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json_safe(item) for item in value]
    if isinstance(value, tuple):
        return [_json_safe(item) for item in value]
    if isinstance(value, set):
        return sorted((_json_safe(item) for item in value), key=lambda item: repr(item))
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date_type):
        return value.isoformat()
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)
