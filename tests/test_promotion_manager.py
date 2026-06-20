from __future__ import annotations

import sys

import pytest

from eimemory.api.runtime import Runtime
from eimemory.governance.capability_distiller import distill_capability_candidate
from eimemory.governance.promotion_manager import backfill_promotion_rollout_ledger, promote_candidate
from eimemory.governance.sandbox_lab import create_sandbox_experiment
from eimemory.models.records import RecordEnvelope, ScopeRef


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
    assert result["post_promotion_status"] == "shadow_observe"
    assert runtime.store.get_by_id(candidate_id).status == "shadow_observe"
    default_policy = runtime.search_policy("autonomous prompt policy sample", scope=scope)
    assert default_policy["policy_suggestions"] == []
    policy = runtime.search_policy("autonomous prompt policy sample", scope=scope, context={"include_shadow": True})
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


def test_l2_prompt_policy_blocks_when_prompt_safety_is_stub_notready(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    scope = {"agent_id": "hongtu"}
    gate_bundle = _l2_gate_bundle()
    gate_bundle["prompt_shadow_eval"] = {"passed": True, "notready": True}
    gate_bundle["prompt_injection_check"] = {"passed": True, "notready": True}
    eval_result = {**PASSING_EVAL, "gate_bundle": gate_bundle}
    candidate_id = distill_capability_candidate(
        runtime,
        scope=scope,
        loop_id="learn_test",
        experiment_id="exp_1",
        eval_result=eval_result,
        promotion_target="system_prompt_patch",
        summary="Prompt policy update",
    )

    result = promote_candidate(runtime, candidate_id=candidate_id, scope=scope, loop_id="learn_test", apply=False, eval_result=eval_result, health={"ok": True})

    assert result["ok"] is False
    assert "prompt_safety_gate" in result["blocked_reason"]


def test_promotion_request_records_target_metadata(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    scope = {"agent_id": "hongtu"}
    candidate_id = distill_capability_candidate(
        runtime,
        scope=scope,
        loop_id="learn_test",
        experiment_id="exp_1",
        eval_result=PASSING_EVAL,
        promotion_target="tool_route",
        summary="Use memory-first routing.",
        target_capability="tool.routing",
    )

    result = promote_candidate(runtime, candidate_id=candidate_id, scope=scope, loop_id="learn_test", apply=False, eval_result=PASSING_EVAL, health={"ok": True})
    promotion = runtime.store.get_by_id(result["promotion_request_id"], scope=scope)

    assert promotion is not None
    assert promotion.content["promotion_target"] == "tool_route"
    assert promotion.content["target_capability"] == "tool.routing"
    assert promotion.meta["promotion_target"] == "tool_route"
    assert promotion.meta["target_capability"] == "tool.routing"


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


def test_l2_code_patch_applies_repo_patch_and_deploys_after_gates(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    scope = {"agent_id": "hongtu"}
    repo = tmp_path / "repo"
    repo.mkdir()
    target = repo / "health_probe.py"
    target.write_text("VERSION = 'old'\n", encoding="utf-8")
    experiment_id = create_sandbox_experiment(
        runtime,
        scope=scope,
        loop_id="learn_test",
        learning_goal_id="goal_1",
        research_note_id="note_1",
        candidate_kind="code_patch",
        candidate_patch={
            "summary": "Patch health probe version and deploy service",
            "target_capability": "code.implementation",
            "repo_root": str(repo),
            "apply_to_repo": True,
            "deploy_to_production": True,
            "commit_to_repo": False,
            "allowed_files": ["health_probe.py"],
            "file_updates": [
                {"path": "health_probe.py", "content": "VERSION = 'new'\n"},
            ],
            "verification_commands": [
                [
                    sys.executable,
                    "-c",
                    "from pathlib import Path; assert Path('health_probe.py').read_text(encoding='utf-8') == \"VERSION = 'new'\\n\"",
                ]
            ],
            "deployment_commands": [
                [
                    sys.executable,
                    "-c",
                    "from pathlib import Path; Path('deployed.txt').write_text('ok\\n', encoding='utf-8')",
                ]
            ],
            "post_deploy_health_commands": [
                [
                    sys.executable,
                    "-c",
                    "from pathlib import Path; assert Path('deployed.txt').read_text(encoding='utf-8') == 'ok\\n'",
                ]
            ],
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
    assert result["side_effect"]["adapter"] == "direct_repo_patch"
    assert result["side_effect"]["production_applied"] is True
    assert result["side_effect"]["repo_mutated"] is True
    assert result["side_effect"]["post_deploy_health"]["ok"] is True
    assert result["side_effect"]["rollback_evidence"]["service_name"] == "eimemory-rpc.service"
    assert result["side_effect"]["rollback_evidence"]["file_backups"][0]["path"] == "health_probe.py"
    assert target.read_text(encoding="utf-8") == "VERSION = 'new'\n"
    assert (repo / "deployed.txt").read_text(encoding="utf-8") == "ok\n"
    assert runtime.store.get_by_id(candidate_id).status == "promoted"
    ledger = runtime.get_policy_rollout_ledger(scope=scope, action="capability_promotion", limit=10)
    entry = next(item for item in ledger if item["promotion_id"] == result["promotion_request_id"])
    promotion = runtime.store.get_by_id(result["promotion_request_id"], scope=scope)
    assert promotion is not None
    assert promotion.content["rollout_ledger_id"] == entry["id"]
    assert entry["source_opportunity_id"] == candidate_id
    assert entry["applied_pattern_id"] == result["applied_artifact_ids"][0]
    assert entry["budget_decision"] == "ok"
    assert entry["details"]["promotion_target"] == "code_patch"
    assert entry["details"]["rollout_action"] == "applied"


def test_l2_code_patch_blocks_when_post_deploy_health_fails(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    scope = {"agent_id": "hongtu"}
    repo = tmp_path / "repo"
    repo.mkdir()
    target = repo / "health_probe.py"
    target.write_text("VERSION = 'old'\n", encoding="utf-8")
    experiment_id = create_sandbox_experiment(
        runtime,
        scope=scope,
        loop_id="learn_test",
        learning_goal_id="goal_1",
        research_note_id="note_1",
        candidate_kind="code_patch",
        candidate_patch={
            "summary": "Patch health probe version and deploy service",
            "target_capability": "code.implementation",
            "repo_root": str(repo),
            "apply_to_repo": True,
            "deploy_to_production": True,
            "commit_to_repo": False,
            "allowed_files": ["health_probe.py"],
            "file_updates": [{"path": "health_probe.py", "content": "VERSION = 'new'\n"}],
            "deployment_commands": [[sys.executable, "-c", "print('deployed')"]],
            "post_deploy_health_commands": [[sys.executable, "-c", "raise SystemExit(7)"]],
        },
    )
    eval_result = {**PASSING_EVAL, "gate_bundle": _l2_gate_bundle()}
    candidate_id = distill_capability_candidate(
        runtime,
        scope=scope,
        loop_id="learn_test",
        experiment_id=experiment_id,
        eval_result=eval_result,
        promotion_target="code_patch",
        summary="Code patch candidate",
        target_capability="code.implementation",
    )

    result = promote_candidate(runtime, candidate_id=candidate_id, scope=scope, loop_id="learn_test", eval_result=eval_result, health={"ok": True})

    assert result["ok"] is False
    assert result["blocked_reason"] == "code_patch_post_deploy_health_failed"
    assert result["side_effect"]["post_deploy_health"]["ok"] is False
    assert result["side_effect"]["rollback_evidence"]["file_backups"][0]["path"] == "health_probe.py"
    assert runtime.store.get_by_id(candidate_id).status == "candidate"


def test_l2_code_patch_blocks_when_real_task_replay_gate_fails(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    scope = {"agent_id": "hongtu"}
    repo = tmp_path / "repo"
    repo.mkdir()
    target = repo / "health_probe.py"
    target.write_text("VERSION = 'old'\n", encoding="utf-8")
    gate_bundle = _l2_gate_bundle()
    gate_bundle["real_task_replay"] = {
        "ok": True,
        "report_type": "real_task_replay",
        "verdict": "fail",
        "pass_rate": 0.25,
        "threshold": 0.6,
        "sample_count": 4,
    }
    eval_result = {**PASSING_EVAL, "gate_bundle": gate_bundle}
    experiment_id = create_sandbox_experiment(
        runtime,
        scope=scope,
        loop_id="learn_test",
        learning_goal_id="goal_1",
        research_note_id="note_1",
        candidate_kind="code_patch",
        candidate_patch={
            "summary": "Patch health probe version",
            "target_capability": "code.implementation",
            "repo_root": str(repo),
            "apply_to_repo": True,
            "deploy_to_production": True,
            "commit_to_repo": False,
            "allowed_files": ["health_probe.py"],
            "file_updates": [{"path": "health_probe.py", "content": "VERSION = 'new'\n"}],
        },
    )
    candidate_id = distill_capability_candidate(
        runtime,
        scope=scope,
        loop_id="learn_test",
        experiment_id=experiment_id,
        eval_result=eval_result,
        promotion_target="code_patch",
        summary="Code patch candidate",
        target_capability="code.implementation",
    )

    result = promote_candidate(runtime, candidate_id=candidate_id, scope=scope, loop_id="learn_test", eval_result=eval_result, health={"ok": True})

    assert result["ok"] is False
    assert "real_task_replay_gate" in result["blocked_reason"]
    assert target.read_text(encoding="utf-8") == "VERSION = 'old'\n"
    assert runtime.store.get_by_id(candidate_id).status == "candidate"
    ledger = runtime.get_policy_rollout_ledger(scope=scope, action="capability_promotion", limit=10)
    entry = next(item for item in ledger if item["promotion_id"] == result["promotion_request_id"])
    assert entry["source_opportunity_id"] == candidate_id
    assert entry["budget_decision"] == "blocked"
    assert entry["reason"] == "real_task_replay_gate"
    assert entry["details"]["rollout_action"] == "gate_failed"


def test_backfill_promotion_rollout_ledger_from_existing_request(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    scope = {"agent_id": "hongtu", "workspace_id": "code", "user_id": "darrow"}
    candidate_id = distill_capability_candidate(
        runtime,
        scope=scope,
        loop_id="learn_test",
        experiment_id="exp_1",
        eval_result=PASSING_EVAL,
        promotion_target="tool_route",
        summary="Use memory-first routing.",
        target_capability="tool.routing",
    )
    promotion = RecordEnvelope.create(
        kind="promotion_request",
        title="Promotion applied: Use memory-first routing.",
        summary="Use memory-first routing.",
        scope=ScopeRef.from_dict(scope),
        status="promoted",
        content={
            "candidate_id": candidate_id,
            "promotion_target": "tool_route",
            "target_capability": "tool.routing",
            "action": "applied",
            "eval_result": PASSING_EVAL,
            "health": {"ok": True},
            "gate": {"ok": True, "gate_bundle": {}},
            "side_effect": {"ok": True, "adapter": "test", "applied_artifact_ids": ["policy-1"]},
        },
        meta={"candidate_id": candidate_id, "promotion_target": "tool_route", "action": "applied"},
    )
    runtime.store.append(promotion)

    report = backfill_promotion_rollout_ledger(runtime, scope=scope, limit=10)

    ledger = runtime.get_policy_rollout_ledger(scope=scope, action="capability_promotion", limit=10)
    entry = next(item for item in ledger if item["promotion_id"] == promotion.record_id)
    assert report["created_count"] == 1
    assert entry["source_opportunity_id"] == candidate_id
    assert entry["applied_pattern_id"] == "policy-1"
    assert entry["details"]["promotion_target"] == "tool_route"


def _l2_gate_bundle() -> dict:
    return {
        "evidence": [{"tier": "T0", "ref": "evt_1", "summary": "User correction verified"}],
        "rollback": {"available": True, "executable": True},
        "canary": {"passed": True, "blast_radius": "single_scope"},
        "timeout_seconds": 300,
        "audit": {"enabled": True},
        "prompt_shadow_eval": {"passed": True},
        "prompt_injection_check": {"passed": True},
        "real_task_replay": {
            "ok": True,
            "report_type": "real_task_replay",
            "verdict": "pass",
            "pass_rate": 1.0,
            "threshold": 0.6,
            "sample_count": 2,
        },
    }
