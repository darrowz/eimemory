from __future__ import annotations

import re
from typing import Any


NEGATIVE_REPLAY_MARKERS = (
    "不是这个意思",
    "别这样",
    "不要这样",
    "不要再",
    "别再",
    "不是",
    "不应",
)

REGRESSION_SEEDS = (
    "最高星项目",
    "star ranking",
    "highest stars",
)

SAFE_ACTION_CATEGORY_RULES: dict[str, tuple[str, ...]] = {
    "external_exposure": (
        "外发",
        "发送给外部",
        "外部",
        "外发到",
        "send to",
        "upload",
        "post",
        "webhook",
        "web hook",
    ),
    "destructive_change": (
        "删除",
        "del ",
        "delete ",
        "rm ",
        "rm -",
        "rmdir",
        "remove-item",
        "remove ",
        "wipe",
        "格式化",
        "drop ",
        "重置",
        "清空",
    ),
    "financial_operation": (
        "支付",
        "扣费",
        "付费",
        "charge",
        "billing",
        "payment",
    ),
    "system_disruption": (
        "停机",
        "重启",
        "rollback",
        "回滚",
        "禁用",
        "shutdown",
        "reboot",
        "重配",
    ),
    "privilege_change": (
        "授权",
        "elevate",
        "权限",
        "降权",
    ),
}


def build_replay_case(opportunity: dict[str, Any]) -> dict[str, Any]:
    """Build replay cases with positive / negative / regression signals."""

    source_outcome_payload = dict(opportunity.get("source_outcome_payload") or {})
    replay_hints = [item for item in source_outcome_payload.get("replay_hints") or [] if isinstance(item, dict)]

    if replay_hints:
        first_hint = replay_hints[0]
        expected_text: list[str] = []
        expected_text.extend(_coerce_string_list(first_hint.get("expected_text")))
        expected_text.extend(_coerce_string_list(opportunity.get("correction_from_user")))
        if not _extract_positive_expected(expected_text):
            expected_text.extend(_coerce_string_list(opportunity.get("policy_update")))
            expected_text.extend(_coerce_string_list(opportunity.get("source_event_payload", {}).get("expected_text")))
        if not expected_text:
            expected_text = _coerce_string_list(first_hint.get("expected_text") or first_hint.get("query") or opportunity.get("trigger"))
        query = _coerce_text(first_hint.get("query") or opportunity.get("trigger") or "")
        source_url = _coerce_text(first_hint.get("source_url") or opportunity.get("source_event_payload", {}).get("source_url") or "")
    else:
        expected_text = []
        expected_text.extend(_coerce_string_list(opportunity.get("correction_from_user")))
        expected_text.extend(_coerce_string_list(opportunity.get("policy_update")))
        expected_text.extend(_coerce_string_list(opportunity.get("outcome_reason")))
        query = _coerce_text(opportunity.get("trigger") or "")
        source_url = ""

    positive_expected = _extract_positive_expected(expected_text)
    negative_expected = _extract_negative_expected(expected_text)
    combined = "\n".join(_coerce_string_list(
        [
            query,
            opportunity.get("policy_update"),
            opportunity.get("outcome_reason"),
            opportunity.get("source_event_payload", {}).get("interpreted_intent"),
            opportunity.get("source_event_payload", {}).get("user_phrase"),
        ]
    ))
    regression_seed_patterns = [item for item in REGRESSION_SEEDS if item in combined]

    return {
        "opportunity_id": _coerce_text(opportunity.get("opportunity_id") or ""),
        "query": query,
        "expected_text": positive_expected,
        "negative_expected_text": negative_expected,
        "regression_seed_patterns": regression_seed_patterns,
        "source": _coerce_text(opportunity.get("source") or ""),
        "source_url": source_url,
        "risk_level": _coerce_text(opportunity.get("risk_level") or "medium"),
        "event_type": _coerce_text(opportunity.get("event_type") or ""),
        "raw_expected_text": expected_text,
    }


def evaluate_replay_gate(replay_case: dict[str, Any]) -> dict[str, Any]:
    if not replay_case.get("expected_text"):
        return {
            "ok": False,
            "allow": False,
            "case_valid": False,
            "executed": False,
            "evidence_kind": "case_definition",
            "verdict": "invalid",
            "blocked_reason": "missing_positive_replay",
            "signals": [],
        }

    if replay_case.get("negative_expected_text"):
        return {
            "ok": False,
            "allow": False,
            "case_valid": False,
            "executed": False,
            "evidence_kind": "case_definition",
            "verdict": "invalid",
            "blocked_reason": "negative_replay_signal",
            "signals": list(_coerce_string_list(replay_case.get("negative_expected_text"))),
        }

    if replay_case.get("regression_seed_patterns"):
        return {
            "ok": False,
            "allow": False,
            "case_valid": False,
            "executed": False,
            "evidence_kind": "case_definition",
            "verdict": "invalid",
            "blocked_reason": "regression_seed_pattern",
            "signals": list(_coerce_string_list(replay_case.get("regression_seed_patterns"))),
        }

    return {
        "ok": True,
        "allow": True,
        "case_valid": True,
        "executed": False,
        "evidence_kind": "case_definition",
        "verdict": "defined",
        "blocked_reason": "",
        "signals": [],
    }


def evaluate_safe_action_gate(
    *,
    patch: dict[str, Any],
) -> dict[str, Any]:
    action_texts = []
    action_texts.extend(_coerce_string_list(patch.get("execution_policy")))
    action_texts.append(_coerce_text(patch.get("pattern") or ""))
    action_texts.append(_coerce_text(patch.get("interpreted_intent") or ""))
    action_texts.append(_coerce_text(patch.get("success_criteria") or ""))
    action_texts.extend(_command_texts(patch))

    blocked = _find_action_risk_categories(action_texts)
    if blocked:
        return {
            "ok": False,
            "allow": False,
            "blocked_reason": "high_risk_action_categories",
            "blocked_categories": blocked,
        }

    return {
        "ok": True,
        "allow": True,
        "blocked_reason": "",
        "blocked_categories": [],
    }


def _coerce_string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        parts = [part.strip() for part in re.split(r"[;；|\n\r]+", value) if part.strip()]
        return parts if parts else ([value.strip()] if value.strip() else [])
    if isinstance(value, (list, tuple, set)):
        return [str(item).strip() for item in value if str(item).strip()]
    return []


def _coerce_text(value: Any) -> str:
    return str(value or "").strip()


def _command_texts(patch: dict[str, Any]) -> list[str]:
    values: list[str] = []
    for key in (
        "verification_commands",
        "verify_commands",
        "deployment_commands",
        "deploy_commands",
        "post_deploy_health_commands",
        "health_commands",
        "smoke_commands",
        "rollback_commands",
        "rollback_command",
        "canary_commands",
        "shadow_observe_commands",
    ):
        values.extend(_flatten_command_text(patch.get(key)))
    rollback_plan = patch.get("rollback_plan")
    if isinstance(rollback_plan, dict):
        values.extend(_flatten_command_text(rollback_plan.get("commands")))
        values.extend(_flatten_command_text(rollback_plan.get("command")))
    nested_code_patch = patch.get("code_patch")
    if isinstance(nested_code_patch, dict):
        values.extend(_command_texts(nested_code_patch))
    return [value for value in values if value]


def _flatten_command_text(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if isinstance(value, dict):
        values: list[str] = []
        for item in value.values():
            values.extend(_flatten_command_text(item))
        return values
    if isinstance(value, (list, tuple, set)):
        if value and all(not isinstance(item, (list, tuple, set, dict)) for item in value):
            command = " ".join(str(item).strip() for item in value if str(item).strip())
            return [command] if command else []
        values: list[str] = []
        for item in value:
            values.extend(_flatten_command_text(item))
        return values
    return []


def _extract_positive_expected(items: list[str] | tuple[str, ...] | str) -> list[str]:
    candidates = _coerce_string_list(items)
    positives: list[str] = []
    for item in candidates:
        normalized = item
        if any(marker in normalized for marker in NEGATIVE_REPLAY_MARKERS):
            # treat full sentence as negative signal only; explicit negatives should be checked separately
            continue
        if normalized:
            positives.append(normalized)
    return positives


def _extract_negative_expected(items: list[str] | tuple[str, ...] | str) -> list[str]:
    candidates = _coerce_string_list(items)
    negatives: list[str] = []
    for item in candidates:
        for marker in NEGATIVE_REPLAY_MARKERS:
            if marker in item:
                negatives.append(item.strip())
                break
    return negatives


def _find_action_risk_categories(values: list[str]) -> list[str]:
    found: list[str] = []
    for category, patterns in SAFE_ACTION_CATEGORY_RULES.items():
        if _matches_any_pattern(values, patterns):
            found.append(category)
    return found


def _matches_any_pattern(values: list[str], patterns: tuple[str, ...]) -> bool:
    lowered = [str(value or "").strip().lower() for value in values]
    for candidate in lowered:
        for marker in patterns:
            if marker in candidate:
                return True
            if re.search(re.escape(marker), candidate):
                return True
    return False
