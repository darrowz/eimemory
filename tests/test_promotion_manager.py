from __future__ import annotations

import pytest

from eimemory.api.runtime import Runtime
from eimemory.governance.capability_distiller import distill_capability_candidate
from eimemory.governance.promotion_manager import promote_candidate
from eimemory.governance.sandbox_lab import create_sandbox_experiment


PASSING_EVAL = {"verdict": "pass", "scores": {"capability": 0.9, "safety": 1.0, "regression": 1.0, "cost": 0.8}}


def test_distill_capability_candidate_requires_passing_eval(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    candidate_id = distill_capability_candidate(
        runtime,
        scope={"agent_id": "hongtu"},
        loop_id="learn_test",
        experiment_id="exp_1",
        eval_result=PASSING_EVAL,
        promotion_target="tool_route",
        summary="Use memory-first routing for stable personal facts.",
        target_capability="tool.routing",
    )

    candidate = runtime.store.get_by_id(candidate_id)
    assert candidate is not None
    assert candidate.kind == "capability_candidate"
    assert candidate.status == "candidate"
    assert candidate.meta["authority_tier"] == "L1"


def test_distillation_rejects_low_safety_eval(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)

    with pytest.raises(ValueError, match="safety"):
        distill_capability_candidate(
            runtime,
            scope={"agent_id": "hongtu"},
            loop_id="learn_test",
            experiment_id="exp_1",
            eval_result={"verdict": "pass", "scores": {"safety": 0.5, "regression": 1.0}},
            promotion_target="tool_route",
            summary="Unsafe candidate",
        )


def test_l2_promotion_blocks_without_structured_gate_bundle(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    candidate_id = distill_capability_candidate(
        runtime,
        scope={"agent_id": "hongtu"},
        loop_id="learn_test",
        experiment_id="exp_1",
        eval_result=PASSING_EVAL,
        promotion_target="system_prompt_patch",
        summary="Prompt policy update",
    )

    result = promote_candidate(runtime, candidate_id=candidate_id, scope={"agent_id": "hongtu"}, loop_id="learn_test", eval_result=PASSING_EVAL, health={"ok": True})

    assert result["ok"] is False
    assert "gate_bundle_missing" in result["blocked_reason"]
    assert runtime.store.get_by_id(candidate_id).status == "candidate"


def test_l2_prompt_policy_applies_to_search_policy_after_gates(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    scope = {"agent_id": "hongtu"}
    experiment_id = create_sandbox_experiment(
        runtime,
        scope=scope,
        loop_id="learn_test",
        learning_goal_id="goal_1",
        research_note_id="note_1",
        candidate_kind="system_prompt_patch",
        candidate_patch={
            "pattern": "autonomous prompt policy sample",
            "default_event_type": "tool_routing",
            "interpreted_intent": "优先查策略再执行",
            "execution_policy": ["先查 memory.searchPolicy", "再选择工具路径"],
            "success_criteria": "下一次同类请求优先返回策略建议",
        },
    )
    candidate_id = distill_capability_candidate(
        runtime,
        scope=scope,
        loop_id="learn_test",
        experiment_id=experiment_id,
        eval_result={**PASSING_EVAL, "gate_bundle": _l2_gate_bundle()},
        promotion_target="system_prompt_patch",
        summary="Prompt policy update",
        target_capability="tool.routing",
    )

    result = promote_candidate(runtime, candidate_id=candidate_id, scope=scope, loop_id="learn_test", eval_result={**PASSING_EVAL, "gate_bundle": _l2_gate_bundle()}, health={"ok": True})

    assert result["ok"] is True
    assert result["applied"] is True
    assert runtime.store.get_by_id(candidate_id).status == "promoted"
    policy = runtime.search_policy("autonomous prompt policy sample", scope=scope)
    assert policy["policy_suggestions"][0]["source"] == "intent_pattern"
    assert policy["policy_suggestions"][0]["interpreted_intent"] == "优先查策略再执行"


def test_l2_promotion_blocks_without_health_gate(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    candidate_id = distill_capability_candidate(
        runtime,
        scope={"agent_id": "hongtu"},
        loop_id="learn_test",
        experiment_id="exp_1",
        eval_result=PASSING_EVAL,
        promotion_target="deployment_rollout",
        summary="Deploy rollout",
    )

    result = promote_candidate(runtime, candidate_id=candidate_id, scope={"agent_id": "hongtu"}, loop_id="learn_test", eval_result=PASSING_EVAL, health={"ok": False})

    assert result["ok"] is False
    assert "health_gate" in result["blocked_reason"]


def test_l2_deployment_rollout_blocks_without_real_adapter(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    candidate_id = distill_capability_candidate(
        runtime,
        scope={"agent_id": "hongtu"},
        loop_id="learn_test",
        experiment_id="exp_1",
        eval_result={**PASSING_EVAL, "gate_bundle": _l2_gate_bundle()},
        promotion_target="deployment_rollout",
        summary="Deploy rollout",
    )

    result = promote_candidate(runtime, candidate_id=candidate_id, scope={"agent_id": "hongtu"}, loop_id="learn_test", eval_result={**PASSING_EVAL, "gate_bundle": _l2_gate_bundle()}, health={"ok": True})

    assert result["ok"] is False
    assert result["applied"] is False
    assert result["blocked_reason"] == "unsupported_rollout_adapter:deployment_rollout"
    assert runtime.store.get_by_id(candidate_id).status == "candidate"


def test_l2_code_patch_promotes_to_reviewable_artifact(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    scope = {"agent_id": "hongtu"}
    experiment_id = create_sandbox_experiment(
        runtime,
        scope=scope,
        loop_id="learn_test",
        learning_goal_id="goal_1",
        research_note_id="note_1",
        candidate_kind="code_patch",
        candidate_patch={
            "summary": "Add compact health endpoint regression coverage",
            "target_capability": "code.implementation",
            "policy": "Prepare a patch for health endpoint review without mutating production.",
        },
    )
    candidate_id = distill_capability_candidate(
        runtime,
        scope=scope,
        loop_id="learn_test",
        experiment_id=experiment_id,
        eval_result={**PASSING_EVAL, "gate_bundle": _l2_gate_bundle()},
        promotion_target="code_patch",
        summary="Code patch candidate",
        target_capability="code.implementation",
    )

    result = promote_candidate(runtime, candidate_id=candidate_id, scope=scope, loop_id="learn_test", eval_result={**PASSING_EVAL, "gate_bundle": _l2_gate_bundle()}, health={"ok": True})

    assert result["ok"] is True
    assert result["applied"] is True
    assert result["side_effect"]["adapter"] == "reviewable_code_patch"
    assert result["side_effect"]["production_applied"] is False
    assert result["side_effect"]["artifact_path"].endswith(".patch")
    assert runtime.store.get_by_id(candidate_id).status == "promoted"


def _l2_gate_bundle() -> dict:
    return {
        "evidence": [{"tier": "T0", "ref": "evt_1", "summary": "User correction verified"}],
        "rollback": {"available": True, "executable": True},
        "canary": {"passed": True, "blast_radius": "single_scope"},
        "timeout_seconds": 300,
        "audit": {"enabled": True},
        "prompt_shadow_eval": {"passed": True},
        "prompt_injection_check": {"passed": True},
    }
