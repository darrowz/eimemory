from __future__ import annotations

from typing import Any


class RewardEngine:
    """Deterministic reward scorer for the lightweight RL-like loop."""

    def compute(
        self,
        experience: dict[str, Any] | None,
        eval_result: dict[str, Any] | None,
        outcome: dict[str, Any] | None,
    ) -> dict[str, Any]:
        experience = dict(experience or {})
        eval_result = dict(eval_result or {})
        outcome = dict(outcome or {})
        recall_quality = _float(eval_result.get("recall_quality"))
        success = _success_value(outcome, eval_result)
        cost = max(0.0, _float(outcome.get("cost") or experience.get("cost")))
        failure_penalty = 0.0 if success > 0 else -1.0
        eval_bonus = 0.5 if bool(eval_result.get("ok", False)) else 0.0
        components = {
            "recall_quality": recall_quality,
            "task_success": 2.0 * success,
            "eval_bonus": eval_bonus,
            "cost": -cost,
            "failure_penalty": failure_penalty,
        }
        reward = round(sum(components.values()), 3)
        return {
            "ok": True,
            "reward": reward,
            "components": components,
            "experience_type": str(experience.get("task_type") or experience.get("source") or ""),
            "primary_label": str(eval_result.get("primary_label") or ""),
        }


def _success_value(outcome: dict[str, Any], eval_result: dict[str, Any]) -> float:
    if isinstance(outcome.get("success"), bool):
        return 1.0 if outcome["success"] else 0.0
    status = _status_text(outcome.get("status") or outcome.get("outcome") or outcome.get("result"))
    if status:
        return 1.0 if status in {"success", "good", "passed", "pass", "ok"} else 0.0
    return 1.0 if bool(eval_result.get("ok", False)) else 0.0


def _status_text(value: Any) -> str:
    if isinstance(value, dict):
        value = value.get("status") or value.get("outcome") or value.get("result")
    return str(value or "").strip().lower()


def _float(value: Any) -> float:
    try:
        return round(float(value), 3)
    except (TypeError, ValueError):
        return 0.0
