from __future__ import annotations

from typing import Any

from eimemory.governance.learning_eval import REGRESSION_THRESHOLD, SAFETY_THRESHOLD
from eimemory.governance.learning_state import append_learning_record_once, stable_semantic_key
from eimemory.models.records import ScopeRef


def distill_capability_candidate(
    runtime: Any,
    *,
    scope: dict[str, Any] | ScopeRef | None,
    loop_id: str,
    experiment_id: str,
    eval_result: dict[str, Any],
    promotion_target: str,
    summary: str,
    target_capability: str = "proactive.judgment",
) -> str:
    _validate_eval(eval_result)
    scores = dict(eval_result.get("scores") or {})
    tier = _tier_for_target(promotion_target)
    semantic_key = stable_semantic_key("capability_candidate", experiment_id, promotion_target, summary)
    record = append_learning_record_once(
        runtime,
        kind="capability_candidate",
        title=f"Capability candidate: {promotion_target}",
        summary=summary,
        scope=scope,
        loop_id=loop_id,
        step_name="distill",
        semantic_key=semantic_key,
        authority_tier=tier,
        status="candidate",
        content={
            "experiment_id": experiment_id,
            "eval_result": eval_result,
            "promotion_target": promotion_target,
            "summary": summary,
            "target_capability": target_capability,
            "rollback": "Disable promoted candidate or restore previous artifact version.",
        },
        meta={
            "experiment_id": experiment_id,
            "promotion_target": promotion_target,
            "target_capability": target_capability,
            "authority_tier": tier,
            "safety": scores.get("safety"),
            "regression": scores.get("regression"),
        },
    )
    playbook = append_learning_record_once(
        runtime,
        kind="learning_playbook",
        title=f"Playbook: {target_capability}",
        summary=summary,
        scope=scope,
        loop_id=loop_id,
        step_name="playbook",
        semantic_key=stable_semantic_key("playbook", target_capability, summary),
        authority_tier="L0",
        status="active",
        content={"candidate_id": record.record_id, "target_capability": target_capability, "summary": summary},
        meta={"candidate_id": record.record_id, "target_capability": target_capability},
    )
    return record.record_id


def _validate_eval(eval_result: dict[str, Any]) -> None:
    if str(eval_result.get("verdict") or "") != "pass":
        raise ValueError("eval verdict must pass")
    scores = dict(eval_result.get("scores") or {})
    if float(scores.get("safety") or 0.0) < SAFETY_THRESHOLD:
        raise ValueError("safety score below threshold")
    if float(scores.get("regression") or 0.0) < REGRESSION_THRESHOLD:
        raise ValueError("regression score below threshold")


def _tier_for_target(target: str) -> str:
    value = str(target or "").lower()
    if value in {"memory_rule", "tool_route", "eval_case", "skill_draft", "sop_draft"}:
        return "L1"
    if value in {"source_policy", "prompt_policy", "system_prompt_patch", "scheduler_policy", "code_patch", "deployment_rollout"}:
        return "L2"
    return "L0"
