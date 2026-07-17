from __future__ import annotations

from typing import Any


PRIMARY_LABELS: tuple[str, ...] = (
    "success",
    "missing_tool_call",
    "argument_mismatch",
    "stale_context",
    "state_tracking_error",
    "recovery_failure",
    "user_correction",
    "unsafe_or_high_risk",
    "unknown_failure",
)

SIGNALS: tuple[str, ...] = (
    "missing_visual_evidence",
    "operator_gap",
    "world_state_mismatch",
    "verifier_missing",
    "low_confidence",
)

_PRIORITY: tuple[str, ...] = (
    "unsafe_or_high_risk",
    "stale_context",
    "state_tracking_error",
    "missing_tool_call",
    "argument_mismatch",
    "recovery_failure",
    "user_correction",
    "success",
    "unknown_failure",
)


def diagnose_outcome(payload: dict[str, Any]) -> dict[str, Any]:
    labels: set[str] = set()
    signals = _signals(payload)
    evidence: dict[str, list[str]] = {}

    if _unsafe_or_high_risk(payload):
        labels.add("unsafe_or_high_risk")
        evidence.setdefault("unsafe_or_high_risk", []).append("risk/safety marker")
    if _stale_context(payload):
        labels.add("stale_context")
        evidence.setdefault("stale_context", []).append("stale context marker")
    if _state_tracking_error(payload):
        labels.add("state_tracking_error")
        evidence.setdefault("state_tracking_error", []).append("world state mismatch")
    if _missing_tool_call(payload):
        labels.add("missing_tool_call")
        evidence.setdefault("missing_tool_call", []).append("expected tool was not used")
    if _argument_mismatch(payload):
        labels.add("argument_mismatch")
        evidence.setdefault("argument_mismatch", []).append("tool/action argument mismatch")
    if _recovery_failure(payload):
        labels.add("recovery_failure")
        evidence.setdefault("recovery_failure", []).append("recovery attempt failed")
    if _user_correction(payload):
        labels.add("user_correction")
        evidence.setdefault("user_correction", []).append("user correction feedback")
    if _is_success(payload):
        labels.add("success")
        evidence.setdefault("success", []).append("successful outcome")

    primary_label = next((label for label in _PRIORITY if label in labels), "unknown_failure")
    if primary_label == "unknown_failure" and not evidence:
        evidence["unknown_failure"] = ["no stable diagnosis label matched"]
    return {
        "schema_version": "outcome_diagnosis.v1",
        "primary_label": primary_label,
        "blame_layer": _blame_layer(primary_label, signals),
        "labels": [label for label in _PRIORITY if label in labels],
        "signals": [signal for signal in SIGNALS if signal in signals],
        "confidence": _diagnosis_confidence(primary_label, labels, signals, payload),
        "evidence": evidence,
    }


def _blame_layer(primary_label: str, signals: set[str]) -> str:
    by_label = {
        "unsafe_or_high_risk": "planner",
        "missing_tool_call": "planner",
        "argument_mismatch": "tool",
        "recovery_failure": "tool",
        "stale_context": "memory",
        "state_tracking_error": "device",
        "user_correction": "operator",
    }
    if primary_label in by_label:
        return by_label[primary_label]
    if primary_label == "success":
        return "unknown"
    if "world_state_mismatch" in signals or "missing_visual_evidence" in signals:
        return "device"
    if "operator_gap" in signals:
        return "operator"
    if "verifier_missing" in signals:
        return "verifier"
    return "unknown"


def _signals(payload: dict[str, Any]) -> set[str]:
    signals: set[str] = set()
    explicit = payload.get("signals")
    if isinstance(explicit, list):
        signals.update(str(item) for item in explicit if str(item) in SIGNALS)
    visual = payload.get("visual_evidence")
    if _visual_evidence_missing(payload) or (_truthy(payload.get("expected_visual_evidence")) and visual in (None, "")):
        signals.add("missing_visual_evidence")
    if _operator_gap(payload):
        signals.add("operator_gap")
    if _world_state_mismatch(payload):
        signals.add("world_state_mismatch")
    if _verifier_missing(payload.get("verifier")):
        signals.add("verifier_missing")
    confidence = payload.get("confidence")
    if confidence is None:
        confidence = _nested(payload, "verifier", "confidence")
    if isinstance(confidence, (int, float)) and confidence < 0.5:
        signals.add("low_confidence")
    return signals


def _verifier_missing(value: Any) -> bool:
    if not isinstance(value, dict) or not value:
        return True
    if isinstance(value.get("passed"), bool):
        return False
    status = str(value.get("status") or value.get("state") or "").strip().lower().replace("-", "_")
    return status in {"missing", "absent", "unavailable", "not_run", "not_executed", "error"} or "passed" not in value


def _missing_tool_call(payload: dict[str, Any]) -> bool:
    expected_tools = _expected_tools(payload)
    actions = payload.get("actions")
    if not isinstance(actions, list):
        actions = []
    selected_tools = _tool_names(payload.get("selected_tools"))
    action_tools = _tool_names(actions)
    if expected_tools and expected_tools.isdisjoint(selected_tools | action_tools):
        return True
    if expected_tools and actions and all(str(_action_type(action)).lower() == "reply" for action in actions):
        return True
    return False


def _is_success(payload: dict[str, Any]) -> bool:
    outcome = payload.get("outcome")
    status = str(outcome.get("status") if isinstance(outcome, dict) else outcome or "").lower()
    return status in {"success", "good"} and _nested(payload, "verifier", "passed") is not False


def _tool_names(value: Any) -> set[str]:
    if not isinstance(value, list):
        return set()
    names: set[str] = set()
    for item in value:
        if isinstance(item, dict):
            candidate = item.get("tool") or item.get("name") or item.get("id") or item.get("tool_name")
        else:
            candidate = item
        text = str(candidate or "").strip()
        if text:
            names.add(text)
    return names


def _expected_tools(payload: dict[str, Any]) -> set[str]:
    values: list[Any] = []
    for container in (payload, payload.get("context") if isinstance(payload.get("context"), dict) else {}):
        if container.get("expected_tool") is not None:
            values.append(container.get("expected_tool"))
        expected_tools = container.get("expected_tools")
        if isinstance(expected_tools, list):
            values.extend(expected_tools)
        elif expected_tools is not None:
            values.append(expected_tools)
    return {str(item).strip() for item in values if str(item).strip()}


def _unsafe_or_high_risk(payload: dict[str, Any]) -> bool:
    if _truthy(payload.get("unsafe_or_high_risk")) or _truthy(_nested(payload, "safety", "unsafe_or_high_risk")):
        return True
    if _truthy(_nested(payload, "safety", "high_risk")):
        return True
    risk_values = (
        _nested(payload, "safety", "risk_level"),
        _nested(payload, "risk", "level"),
        _nested(payload, "risk", "severity"),
        payload.get("risk_level"),
        payload.get("safety_level"),
    )
    for value in risk_values:
        text = str(value or "").strip().lower()
        if text in {"high", "unsafe", "critical", "l2", "l3", "l4", "privacy", "device", "account"}:
            return True
        if text.startswith(("l2", "l3", "l4")):
            return True
    return _truthy(_nested(payload, "risk", "requires_confirmation")) and _operator_gap(payload)


def _stale_context(payload: dict[str, Any]) -> bool:
    return (
        _truthy(payload.get("stale_context"))
        or _truthy(_nested(payload, "context", "stale"))
        or _truthy(_nested(payload, "context", "stale_assumptions"))
        or _truthy(_nested(payload, "world_state", "stale"))
    )


def _state_tracking_error(payload: dict[str, Any]) -> bool:
    return (
        _truthy(payload.get("state_tracking_error"))
        or _truthy(_nested(payload, "world_state", "state_tracking_error"))
        or _world_state_mismatch(payload)
    )


def _argument_mismatch(payload: dict[str, Any]) -> bool:
    if _truthy(payload.get("argument_mismatch")) or _truthy(_nested(payload, "tool_call", "argument_mismatch")):
        return True
    for action in payload.get("actions") or []:
        if not isinstance(action, dict):
            continue
        text = " ".join(str(action.get(key) or "") for key in ("error", "reason", "message", "status")).lower()
        if any(marker in text for marker in ("argument", "parameter", "wrong id", "wrong target", "mismatch")):
            return True
    return False


def _recovery_failure(payload: dict[str, Any]) -> bool:
    if _truthy(payload.get("recovery_failure")) or _truthy(_nested(payload, "recovery", "failed")):
        return True
    for action in payload.get("actions") or []:
        if not isinstance(action, dict):
            continue
        text = " ".join(str(action.get(key) or "") for key in ("error", "reason", "message")).lower()
        if "recover" in text and any(marker in text for marker in ("fail", "failed", "failure", "unable")):
            return True
    return False


def _user_correction(payload: dict[str, Any]) -> bool:
    if _truthy(payload.get("user_correction")) or _truthy(_nested(payload, "feedback", "user_correction")):
        return True
    correction = _first_text(
        payload.get("correction_from_user"),
        payload.get("correction"),
        _nested(payload, "feedback", "correction_from_user"),
        _nested(payload, "feedback", "correction"),
    )
    if correction:
        return True
    feedback = _first_text(payload.get("feedback"), _nested(payload, "outcome", "feedback"))
    compact = "".join(char for char in feedback.lower() if char.isalnum())
    return any(
        marker in compact
        for marker in (
            "不是这个意思",
            "不是让",
            "不是要",
            "不对",
            "错了",
            "理解错",
            "notwhatimeant",
            "wrong",
        )
    )


def _visual_evidence_missing(payload: dict[str, Any]) -> bool:
    visual = payload.get("visual_evidence")
    if not isinstance(visual, dict):
        return False
    if _truthy(visual.get("missing")):
        return True
    status = str(visual.get("status") or visual.get("state") or "").strip().lower()
    if status in {"missing", "absent", "unavailable", "insufficient"}:
        return True
    required = _truthy(visual.get("required")) or _truthy(visual.get("expected"))
    unavailable = visual.get("available") is False or visual.get("present") is False
    return required and unavailable


def _operator_gap(payload: dict[str, Any]) -> bool:
    value = payload.get("operator_gap")
    if value is True or _truthy(_nested(payload, "operator_gap", "detected")):
        return True
    if not isinstance(value, dict):
        return False
    for key in ("missing", "missing_confirmation", "missing_info", "needs_operator", "approval_missing"):
        item = value.get(key)
        if isinstance(item, str) and item.strip().lower() in {"", "none", "no", "false", "0"}:
            continue
        if _truthy(item):
            return True
    expected = _first_text(value.get("expected"), value.get("expected_behavior"), value.get("required_behavior"))
    observed = _first_text(value.get("actual"), value.get("observed"), value.get("observed_behavior"))
    return bool(expected and observed and expected != observed)


def _world_state_mismatch(payload: dict[str, Any]) -> bool:
    if _truthy(_nested(payload, "world_state", "mismatch")) or _truthy(payload.get("world_state_mismatch")):
        return True
    world_state = payload.get("world_state")
    if not isinstance(world_state, dict):
        return False
    status = str(world_state.get("status") or world_state.get("state") or "").strip().lower()
    if status in {"mismatch", "mismatched", "stale"}:
        return True
    expected = _first_text(world_state.get("expected"), world_state.get("target"), world_state.get("state_after"))
    observed = _first_text(world_state.get("observed"), world_state.get("actual"), world_state.get("current"))
    if expected and observed and expected != observed:
        return True
    transition = _first_text(world_state.get("transition_evidence"), world_state.get("transition"))
    return bool(expected and not transition and _truthy(world_state.get("requires_transition_evidence")))


def _diagnosis_confidence(
    primary_label: str,
    labels: set[str],
    signals: set[str],
    payload: dict[str, Any],
) -> float:
    explicit_confidence = payload.get("confidence")
    if explicit_confidence is None:
        explicit_confidence = _nested(payload, "verifier", "confidence")
    if isinstance(explicit_confidence, (int, float)):
        return round(max(0.0, min(1.0, float(explicit_confidence))), 3)
    if primary_label == "unknown_failure":
        return 0.35
    return round(min(0.95, 0.62 + len(labels) * 0.08 + len(signals) * 0.04), 3)


def _first_text(*values: Any) -> str:
    for value in values:
        if isinstance(value, dict):
            for key in ("text", "summary", "value", "reason", "message"):
                text = _first_text(value.get(key))
                if text:
                    return text
            continue
        text = str(value or "").strip()
        if text:
            return text
    return ""


def _action_type(action: Any) -> str:
    if isinstance(action, dict):
        return str(action.get("type") or action.get("action") or "")
    return str(action or "")


def _nested(payload: dict[str, Any], *path: str) -> Any:
    current: Any = payload
    for key in path:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def _truthy(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y", "on"}
    return bool(value)
