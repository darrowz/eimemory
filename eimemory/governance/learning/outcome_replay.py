from __future__ import annotations

import re
from typing import Any

from eimemory.metadata import business_metadata
from eimemory.models.records import RecordEnvelope


SKIPPED_PRIMARY_LABELS = {"success", "unknown", "unknown_failure"}


def build_replay_case_from_outcome(record: RecordEnvelope) -> dict[str, Any]:
    """Convert a persisted bad outcome trace into a deterministic replay case."""
    meta = business_metadata(record.meta)
    if str(meta.get("report_type") or "") != "outcome_trace":
        return {}
    if str(meta.get("schema_version") or "") != "outcome_trace.v1":
        return {}

    primary_label = _clean_text(meta.get("primary_label") or "")
    if not primary_label or primary_label in SKIPPED_PRIMARY_LABELS:
        return {}

    content = record.content if isinstance(record.content, dict) else {}
    payload = dict(content.get("payload") or {})
    diagnosis = dict(content.get("diagnosis") or {})
    operator_gap = _dict_first(content.get("operator_gap"), payload.get("operator_gap"))
    visual_evidence = _dict_first(content.get("visual_evidence"), payload.get("visual_evidence"))
    world_state = _dict_first(content.get("world_state"), payload.get("world_state"))

    signals = _unique_strings(
        _coerce_string_list(meta.get("diagnosis_signals") or meta.get("signals"))
        + _coerce_string_list(diagnosis.get("signals"))
    )
    expected_text = _positive_expected_text(
        diagnosis=diagnosis,
        payload=payload,
        operator_gap=operator_gap,
        visual_evidence=visual_evidence,
        world_state=world_state,
    )
    negative_expected_text = _negative_expected_text(
        diagnosis=diagnosis,
        payload=payload,
        operator_gap=operator_gap,
    )

    return {
        "query": _first_text(
            payload.get("query"),
            payload.get("user_query"),
            payload.get("request"),
            payload.get("prompt"),
            payload.get("user_message"),
            diagnosis.get("query"),
            record.summary,
            record.title,
        ),
        "expected_text": expected_text,
        "negative_expected_text": negative_expected_text,
        "risk_level": _risk_level(record, payload, primary_label),
        "source_outcome_trace_id": record.record_id,
        "primary_label": primary_label,
        "signals": signals,
        "task_type": _first_text(meta.get("task_type"), payload.get("task_type"), diagnosis.get("task_type")),
        "operator_gap": operator_gap,
        "visual_evidence": visual_evidence,
        "world_state": world_state,
    }


def _positive_expected_text(
    *,
    diagnosis: dict[str, Any],
    payload: dict[str, Any],
    operator_gap: dict[str, Any],
    visual_evidence: dict[str, Any],
    world_state: dict[str, Any],
) -> list[str]:
    values: list[str] = []
    for key in ("expected_text", "expected", "expected_behavior", "correction", "fix", "policy_update"):
        values.extend(_coerce_string_list(diagnosis.get(key)))
        values.extend(_coerce_string_list(payload.get(key)))
    for key in ("expected_behavior", "correction", "required_behavior"):
        values.extend(_coerce_string_list(operator_gap.get(key)))
    for key in ("expected", "required", "requirement"):
        values.extend(_coerce_string_list(world_state.get(key)))
    for key in ("expected", "required", "required_evidence", "missing"):
        values.extend(_coerce_string_list(visual_evidence.get(key)))
    if not values:
        values.extend(_coerce_string_list(payload.get("feedback")))
        values.extend(_coerce_string_list(payload.get("input_summary")))
        primary_label = _clean_text(diagnosis.get("primary_label") or "")
        if primary_label:
            values.append(f"avoid repeated {primary_label}")
    return _unique_strings(values)


def _negative_expected_text(
    *,
    diagnosis: dict[str, Any],
    payload: dict[str, Any],
    operator_gap: dict[str, Any],
) -> list[str]:
    explicit: list[str] = []
    for key in ("negative_expected_text", "negative_expected", "avoid_text", "regression_text"):
        explicit.extend(_coerce_string_list(diagnosis.get(key)))
        explicit.extend(_coerce_string_list(payload.get(key)))
    if explicit:
        return _unique_strings(explicit)

    values: list[str] = []
    for key in ("actual_response", "actual_text", "bad_response"):
        values.extend(_coerce_string_list(payload.get(key)))
    for key in ("observed_behavior", "actual_behavior"):
        values.extend(_coerce_string_list(operator_gap.get(key)))
    return _unique_strings(values)


def _risk_level(record: RecordEnvelope, payload: dict[str, Any], primary_label: str) -> str:
    if primary_label == "unsafe_or_high_risk":
        return "high"
    meta = business_metadata(record.meta)
    for value in (
        meta.get("risk_level"),
        meta.get("safety_level"),
        meta.get("impact_level"),
        payload.get("risk_level"),
        payload.get("safety_level"),
        _nested(payload, "risk", "level"),
        _nested(payload, "risk", "severity"),
    ):
        text = _clean_text(value)
        if text:
            return text
    return "medium"


def _dict_first(*values: Any) -> dict[str, Any]:
    for value in values:
        if isinstance(value, dict):
            return dict(value)
    return {}


def _first_text(*values: Any) -> str:
    for value in values:
        text = _clean_text(value)
        if text:
            return text
    return ""


def _clean_text(value: Any) -> str:
    return " ".join(str(value or "").split())[:240]


def _coerce_string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        parts = [part.strip() for part in re.split(r"[;；|\n\r]+", value) if part.strip()]
        return parts if parts else ([value.strip()] if value.strip() else [])
    if isinstance(value, dict):
        values: list[str] = []
        for key in ("text", "summary", "expected", "observed", "value"):
            values.extend(_coerce_string_list(value.get(key)))
        return values
    if isinstance(value, (list, tuple, set)):
        values: list[str] = []
        for item in value:
            values.extend(_coerce_string_list(item))
        return values
    text = _clean_text(value)
    return [text] if text else []


def _unique_strings(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        text = _clean_text(value)
        if not text or text in seen:
            continue
        seen.add(text)
        result.append(text)
    return result


def _nested(payload: dict[str, Any], *path: str) -> Any:
    current: Any = payload
    for key in path:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current
