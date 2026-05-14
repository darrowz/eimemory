from __future__ import annotations

from dataclasses import asdict
from typing import Any

from eimemory.models.records import ScopeRef

SUPPORTED_PHASES = {"extraction", "update", "usage", "consistency", "temporal", "implicit"}


def normalize_memory_eval_suite(dataset: dict | list) -> dict[str, Any]:
    if isinstance(dataset, list):
        raw = {"name": "memory_eval_suite", "cases": dataset}
    elif isinstance(dataset, dict):
        raw = dict(dataset)
    else:
        raise ValueError("memory eval suite must be a JSON object or list")

    scope = asdict(ScopeRef.from_dict(raw.get("scope") or {}))
    cases = [_normalize_case(item, index=index, default_scope=scope) for index, item in enumerate(list(raw.get("cases") or raw.get("samples") or []))]
    threshold = _clamp_float(raw.get("threshold"), default=0.8)
    return {
        "schema_version": 2,
        "report_type": "memory_eval_ci",
        "name": str(raw.get("name") or "memory_eval_suite"),
        "scope": scope,
        "threshold": threshold,
        "profile": str(raw.get("profile") or "balanced"),
        "seed": list(raw.get("seed") or raw.get("seed_records") or []),
        "cases": cases,
        "emit_incidents": bool(raw.get("emit_incidents", False)),
    }


def _normalize_case(item: Any, *, index: int, default_scope: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(item, dict):
        return {
            "case_id": str(index),
            "phase": "usage",
            "scope": dict(default_scope),
            "query": "",
            "limit": 5,
            "invalid_case": "invalid_case",
        }
    phase = str(item.get("phase") or "usage").strip().lower()
    if phase not in SUPPORTED_PHASES:
        phase = "usage"
    return {
        **dict(item),
        "case_id": str(item.get("case_id") or item.get("id") or index),
        "phase": phase,
        "scope": dict(item.get("scope") or default_scope),
        "limit": max(1, min(100, _int_value(item.get("limit"), default=5))),
    }


def _int_value(value: Any, *, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _clamp_float(value: Any, *, default: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        parsed = default
    return round(max(0.0, min(1.0, parsed)), 3)
