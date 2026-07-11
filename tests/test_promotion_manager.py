from __future__ import annotations

import json
import sys
import subprocess

import pytest

from eimemory.api.runtime import Runtime
from eimemory.governance.capability_distiller import distill_capability_candidate
import eimemory.governance.promotion_manager as promotion_manager
from eimemory.governance.promotion_manager import backfill_promotion_rollout_ledger, promote_candidate, _deployment_commands, _matching_code_preflight, _run_patch_commands, run_code_patch_preflight
from eimemory.governance.sandbox_lab import create_sandbox_experiment
from eimemory.governance.rollout_lifecycle import is_executed_rollback_ledger_record
from eimemory.models.records import RecordEnvelope, ScopeRef


PASSING_EVAL = {"verdict": "pass", "scores": {"capability": 0.9, "safety": 1.0, "regression": 1.0, "cost": 0.8}}


def test_patch_command_resolves_python_to_current_interpreter_when_path_lacks_python(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("PATH", "")

    report = _run_patch_commands(
        [["python", "-c", "import sys; print(sys.executable)"]],
        cwd=tmp_path,
        timeout_seconds=10,
        phase="verify",
    )

    assert report["ok"] is True
    assert report["reports"][0]["command"][0] == sys.executable
    assert sys.executable in report["reports"][0]["stdout"]


def test_patch_commands_reject_shell_string_commands(tmp_path) -> None:
    marker = tmp_path / "shell-command-ran.txt"
    command = (
        f'"{sys.executable}" -c '
        f'"from pathlib import Path; Path(r\'{marker}\').write_text(\'bad\', encoding=\'utf-8\')"'
    )

    report = _run_patch_commands([command], cwd=tmp_path, timeout_seconds=10, phase="verify")

    assert report["ok"] is False
    assert report["reports"][0]["error_type"] == "unsupported_shell_command"
    assert not marker.exists()


def test_patch_commands_fail_closed_when_verification_list_is_empty(tmp_path) -> None:
    report = _run_patch_commands([], cwd=tmp_path, timeout_seconds=10, phase="verify")

    assert report["ok"] is False
    assert report["skipped"] is True
    assert report["error_type"] == "missing_required_commands"
    assert report["reports"] == []


def test_code_preflight_persists_failure_when_sandbox_setup_raises(tmp_path, monkeypatch) -> None:
    runtime = Runtime.create(root=tmp_path / "runtime")
    scope = {"agent_id": "hongtu", "workspace_id": "code", "user_id": "darrow"}
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "module.py").write_text("VALUE = 'old'\n", encoding="utf-8")
    monkeypatch.setenv("EIMEMORY_AUTONOMOUS_CODE_REPO", str(repo))
    monkeypatch.setattr(
        promotion_manager,
        "_prepare_code_preflight_sandbox",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("sandbox unavailable")),
    )

    report = run_code_patch_preflight(
        runtime,
        {
            "summary": "Sandbox setup must fail closed",
            "repo_root": str(repo),
            "apply_to_repo": True,
            "allowed_files": ["module.py"],
            "file_updates": [{"path": "module.py", "content": "VALUE = 'new'\n"}],
            "verification_commands": [[sys.executable, "-c", "print('ok')"]],
        },
        scope=scope,
        loop_id="preflight_setup_failure",
    )

    assert report["ok"] is False
    assert report["executed"] is False
    assert report["setup"]["ok"] is False
    assert report["setup"]["error"] == "code_preflight_setup_failed"
    assert "sandbox unavailable" in report["setup"]["detail"]
    persisted = runtime.store.get_by_id(report["record_id"], scope=scope)
    assert persisted is not None
    assert persisted.content["verdict"] == "fail"


def test_non_git_preflight_is_invalidated_when_repository_state_changes(tmp_path, monkeypatch) -> None:
    runtime = Runtime.create(root=tmp_path / "runtime")
    scope = {"agent_id": "hongtu", "workspace_id": "code", "user_id": "darrow"}
    repo = tmp_path / "repo"
    repo.mkdir()
    target = repo / "module.py"
    target.write_text("VALUE = 'old'\n", encoding="utf-8")
    monkeypatch.setenv("EIMEMORY_AUTONOMOUS_CODE_REPO", str(repo))
    patch = {
        "summary": "Bind preflight to repository state",
        "repo_root": str(repo),
        "apply_to_repo": True,
        "allowed_files": ["module.py"],
        "file_updates": [{"path": "module.py", "content": "VALUE = 'new'\n"}],
        "verification_commands": [[sys.executable, "-c", "print('ok')"]],
    }
    preflight = run_code_patch_preflight(runtime, patch, scope=scope, loop_id="preflight_state")
    eval_result = {"gate_bundle": {"code_preflight": preflight}}

    assert preflight["ok"] is True
    assert preflight["subject_state_digest"]
    target.write_text("VALUE = 'drifted'\n", encoding="utf-8")

    assert _matching_code_preflight(runtime, patch, eval_result=eval_result, scope=scope) is None


def test_git_preflight_is_invalidated_when_worktree_becomes_dirty(tmp_path, monkeypatch) -> None:
    runtime = Runtime.create(root=tmp_path / "runtime")
    scope = {"agent_id": "hongtu", "workspace_id": "code", "user_id": "darrow"}
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_git_repo(repo)
    (repo / "module.py").write_text("VALUE = 'old'\n", encoding="utf-8")
    subprocess.run(["git", "add", "module.py"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "seed"], cwd=repo, check=True)
    monkeypatch.setenv("EIMEMORY_AUTONOMOUS_CODE_REPO", str(repo))
    patch = {
        "summary": "Reject stale evidence after dirty worktree drift",
        "repo_root": str(repo),
        "apply_to_repo": True,
        "allowed_files": ["module.py"],
        "file_updates": [{"path": "module.py", "content": "VALUE = 'new'\n"}],
        "verification_commands": [[sys.executable, "-c", "print('ok')"]],
    }
    preflight = run_code_patch_preflight(runtime, patch, scope=scope, loop_id="preflight_git_state")
    eval_result = {"gate_bundle": {"code_preflight": preflight}}

    assert preflight["ok"] is True
    (repo / "untracked.py").write_text("DRIFT = True\n", encoding="utf-8")

    assert _matching_code_preflight(runtime, patch, eval_result=eval_result, scope=scope) is None


def test_code_patch_rechecks_subject_state_immediately_before_repo_mutation(tmp_path, monkeypatch) -> None:
    runtime = Runtime.create(root=tmp_path / "runtime")
    scope = {"agent_id": "hongtu", "workspace_id": "code", "user_id": "darrow"}
    repo = tmp_path / "repo"
    repo.mkdir()
    target = repo / "module.py"
    target.write_text("VALUE = 'old'\n", encoding="utf-8")
    monkeypatch.setenv("EIMEMORY_AUTONOMOUS_CODE_REPO", str(repo))
    experiment_id = create_sandbox_experiment(
        runtime,
        scope=scope,
        loop_id="learn_test",
        learning_goal_id="goal_subject_race",
        research_note_id="note_subject_race",
        candidate_kind="code_patch",
        candidate_patch={
            "summary": "Reject repository drift after preflight",
            "repo_root": str(repo),
            "apply_to_repo": True,
            "commit_to_repo": False,
            "deploy_to_production": False,
            "allowed_files": ["module.py"],
            "file_updates": [{"path": "module.py", "content": "VALUE = 'new'\n"}],
            "verification_commands": [[sys.executable, "-c", "print('ok')"]],
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
    original_rollout_gate = promotion_manager._rollout_gate

    def mutate_after_gate(*args, **kwargs):
        result = original_rollout_gate(*args, **kwargs)
        if result["ok"]:
            target.write_text("VALUE = 'concurrent-drift'\n", encoding="utf-8")
        return result

    monkeypatch.setattr(promotion_manager, "_rollout_gate", mutate_after_gate)

    result = promote_candidate(
        runtime,
        candidate_id=candidate_id,
        scope=scope,
        loop_id="learn_test",
        eval_result={**PASSING_EVAL, "gate_bundle": _l2_gate_bundle()},
        health={"ok": True},
    )

    assert result["ok"] is False
    assert result["blocked_reason"] == "code_patch_subject_changed"
    assert result["side_effect"]["repo_mutated"] is False
    assert target.read_text(encoding="utf-8") == "VALUE = 'concurrent-drift'\n"


def test_deployment_commands_ignore_raw_env_shell_strings(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("EIMEMORY_AUTONOMOUS_CODE_DEPLOY_COMMAND", "echo unsafe")

    commands = _deployment_commands({}, tmp_path)

    assert commands == []


def test_deployment_commands_accept_json_argv_from_env(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv(
        "EIMEMORY_AUTONOMOUS_CODE_DEPLOY_COMMAND",
        json.dumps(["python", "-c", "print('deploy')"]),
    )

    commands = _deployment_commands({}, tmp_path)

    assert commands == [["python", "-c", "print('deploy')"]]


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


def test_distill_capability_candidate_uses_specific_readable_title(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    scope = {"agent_id": "hongtu"}

    candidate_id = distill_capability_candidate(
        runtime,
        scope=scope,
        loop_id="learn_specific_title",
        experiment_id="experiment-specific-title",
        eval_result=PASSING_EVAL,
        promotion_target="tool_route",
        summary="Use memory-first routing when the user asks for stable project facts.",
        target_capability="tool.routing",
    )

    candidate = runtime.store.get_by_id(candidate_id, scope=scope)

    assert candidate is not None
    assert candidate.title != "Capability candidate: tool_route"
    assert "tool.routing" in candidate.title
    assert "memory-first routing" in candidate.title
    assert "Generate a policy/SOP/eval case" not in candidate.summary


def test_distill_capability_candidate_dedupes_across_loops_by_semantic_key(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    scope = {"agent_id": "hongtu"}

    first = distill_capability_candidate(
        runtime,
        scope=scope,
        loop_id="learn_loop_a",
        experiment_id="experiment-a",
        eval_result=PASSING_EVAL,
        promotion_target="tool_route",
        summary="Use memory-first routing when answering stable project facts.",
        target_capability="tool.routing",
    )
    second = distill_capability_candidate(
        runtime,
        scope=scope,
        loop_id="learn_loop_b",
        experiment_id="experiment-b",
        eval_result=PASSING_EVAL,
        promotion_target="tool_route",
        summary="Use memory-first routing when answering stable project facts.",
        target_capability="tool.routing",
    )

    assert second == first
    assert len(runtime.store.list_records(kinds=["capability_candidate"], scope=scope, limit=10)) == 1


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


def test_distillation_rejects_failed_eval_even_when_verdict_text_says_pass(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)

    with pytest.raises(ValueError, match="eval ok"):
        distill_capability_candidate(
            runtime,
            scope={"agent_id": "hongtu"},
            loop_id="learn_test",
            experiment_id="exp_1",
            eval_result={"ok": False, "verdict": "pass", "scores": {"safety": 1.0, "regression": 1.0}},
            promotion_target="tool_route",
            summary="Contradictory eval must not distill.",
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


def test_l2_promotion_blocks_without_loop_doctor_and_smoke_gate(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    scope = {"agent_id": "hongtu"}
    candidate_id = distill_capability_candidate(
        runtime,
        scope=scope,
        loop_id="learn_test",
        experiment_id="exp_1",
        eval_result=PASSING_EVAL,
        promotion_target="system_prompt_patch",
        summary="Prompt policy update",
    )
    gate_bundle = _l2_gate_bundle()
    gate_bundle.pop("closed_loop", None)

    result = promote_candidate(
        runtime,
        candidate_id=candidate_id,
        scope=scope,
        loop_id="learn_test",
        eval_result={**PASSING_EVAL, "gate_bundle": gate_bundle},
        health={"ok": True},
    )

    assert result["ok"] is False
    assert "closed_loop_gate" in result["blocked_reason"]


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


def test_l1_promotion_respects_explicit_zero_safety_score(tmp_path) -> None:
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

    result = promote_candidate(
        runtime,
        candidate_id=candidate_id,
        scope=scope,
        loop_id="learn_test",
        apply=False,
        eval_result={"verdict": "pass", "scores": {"safety": 0.0, "regression": 1.0}},
        health={"ok": True},
    )

    assert result["ok"] is False
    assert "safety_gate" in result["blocked_reason"]
    assert runtime.store.get_by_id(candidate_id, scope=scope).status == "candidate"


def test_l1_promotion_respects_explicit_zero_regression_score(tmp_path) -> None:
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

    result = promote_candidate(
        runtime,
        candidate_id=candidate_id,
        scope=scope,
        loop_id="learn_test",
        apply=False,
        eval_result={"verdict": "pass", "scores": {"safety": 1.0, "regression": 0.0}},
        health={"ok": True},
    )

    assert result["ok"] is False
    assert "regression_gate" in result["blocked_reason"]
    assert runtime.store.get_by_id(candidate_id, scope=scope).status == "candidate"


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


def test_l2_promotion_blocks_malformed_evidence_score_without_crashing(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    scope = {"agent_id": "hongtu"}
    gate_bundle = _l2_gate_bundle()
    gate_bundle["evidence"] = []
    eval_result = {
        "verdict": "pass",
        "scores": {"capability": 0.9, "safety": 1.0, "regression": 1.0, "evidence": "bad"},
        "gate_bundle": gate_bundle,
    }
    candidate_id = distill_capability_candidate(
        runtime,
        scope=scope,
        loop_id="learn_test",
        experiment_id="exp_1",
        eval_result={**PASSING_EVAL, "gate_bundle": _l2_gate_bundle()},
        promotion_target="system_prompt_patch",
        summary="Prompt policy update",
    )

    result = promote_candidate(runtime, candidate_id=candidate_id, scope=scope, loop_id="learn_test", apply=False, eval_result=eval_result, health={"ok": True})

    assert result["ok"] is False
    assert "evidence_gate" in result["blocked_reason"]


def test_l2_promotion_blocks_malformed_timeout_without_crashing(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    scope = {"agent_id": "hongtu"}
    gate_bundle = _l2_gate_bundle()
    gate_bundle["timeout_seconds"] = "bad"
    eval_result = {**PASSING_EVAL, "gate_bundle": gate_bundle}
    candidate_id = distill_capability_candidate(
        runtime,
        scope=scope,
        loop_id="learn_test",
        experiment_id="exp_1",
        eval_result={**PASSING_EVAL, "gate_bundle": _l2_gate_bundle()},
        promotion_target="system_prompt_patch",
        summary="Prompt policy update",
    )

    result = promote_candidate(runtime, candidate_id=candidate_id, scope=scope, loop_id="learn_test", apply=False, eval_result=eval_result, health={"ok": True})

    assert result["ok"] is False
    assert "timeout_gate" in result["blocked_reason"]


def test_l2_code_patch_blocks_malformed_real_task_replay_without_crashing(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    scope = {"agent_id": "hongtu"}
    gate_bundle = _l2_gate_bundle()
    gate_bundle["real_task_replay"] = {
        "ok": True,
        "verdict": "pass",
        "pass_rate": 1.0,
        "threshold": 0.6,
        "sample_count": "bad",
    }
    eval_result = {**PASSING_EVAL, "gate_bundle": gate_bundle}
    candidate_id = distill_capability_candidate(
        runtime,
        scope=scope,
        loop_id="learn_test",
        experiment_id="exp_1",
        eval_result={**PASSING_EVAL, "gate_bundle": _l2_gate_bundle()},
        promotion_target="code_patch",
        summary="Code patch update",
    )

    result = promote_candidate(runtime, candidate_id=candidate_id, scope=scope, loop_id="learn_test", apply=False, eval_result=eval_result, health={"ok": True})

    assert result["ok"] is False
    assert result["blocked_reason"] == "code_patch_requires_file_updates"


def test_l2_code_patch_uses_gate_timeout_when_patch_timeout_is_malformed(tmp_path, monkeypatch) -> None:
    runtime = Runtime.create(root=tmp_path)
    scope = {"agent_id": "hongtu"}
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setenv("EIMEMORY_AUTONOMOUS_CODE_REPO", str(repo))
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
            "summary": "Patch health probe version",
            "target_capability": "code.implementation",
            "repo_root": str(repo),
            "apply_to_repo": True,
            "commit_to_repo": False,
            "allowed_files": ["health_probe.py"],
            "timeout_seconds": "bad",
            "file_updates": [{"path": "health_probe.py", "content": "VERSION = 'new'\n"}],
            "verification_commands": [[sys.executable, "-c", "from pathlib import Path; assert Path('health_probe.py').read_text(encoding='utf-8') == \"VERSION = 'new'\\n\""]],
        },
    )
    gate_bundle = _l2_gate_bundle()
    gate_bundle["timeout_seconds"] = 60
    eval_result = {**PASSING_EVAL, "gate_bundle": gate_bundle}
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

    assert result["ok"] is True
    assert result["applied"] is True
    assert target.read_text(encoding="utf-8") == "VERSION = 'new'\n"


def test_promotion_request_preserves_content_authority_tier(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    scope = {"agent_id": "hongtu"}
    candidate = RecordEnvelope.create(
        kind="capability_candidate",
        title="Content-tier prompt policy",
        summary="Content-only L2 authority must be preserved in promotion records.",
        status="candidate",
        scope=ScopeRef.from_dict(scope),
        content={
            "authority_tier": "L2",
            "promotion_target": "system_prompt_patch",
            "target_capability": "tool.routing",
        },
        meta={"promotion_target": "system_prompt_patch", "target_capability": "tool.routing"},
    )
    runtime.store.append(candidate)
    eval_result = {**PASSING_EVAL, "gate_bundle": _l2_gate_bundle()}

    result = promote_candidate(runtime, candidate_id=candidate.record_id, scope=scope, loop_id="learn_test", apply=False, eval_result=eval_result, health={"ok": True})
    promotion = runtime.store.get_by_id(result["promotion_request_id"], scope=scope)

    assert result["ok"] is True
    assert promotion is not None
    assert promotion.meta["authority_tier"] == "L2"


def test_tool_route_promotion_defaults_malformed_confidence_without_crashing(tmp_path) -> None:
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
    eval_result = {"verdict": "pass", "scores": {"safety": 1.0, "regression": 1.0, "confidence": "bad"}}

    result = promote_candidate(runtime, candidate_id=candidate_id, scope=scope, loop_id="learn_test", eval_result=eval_result, health={"ok": True})

    assert result["ok"] is True
    assert result["applied"] is True


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


def test_l2_code_patch_applies_repo_patch_and_deploys_after_gates(tmp_path, monkeypatch) -> None:
    runtime = Runtime.create(root=tmp_path)
    scope = {"agent_id": "hongtu"}
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_git_repo(repo)
    target = repo / "health_probe.py"
    target.write_text("VERSION = 'old'\n", encoding="utf-8")
    subprocess.run(["git", "add", "health_probe.py"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "seed"], cwd=repo, check=True)
    monkeypatch.setenv("EIMEMORY_AUTONOMOUS_CODE_REPO", str(repo))
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
            "commit_to_repo": True,
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
            "rollback_plan": {"commands": [[sys.executable, "-c", "print('rollback ready')"]]},
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


def test_code_patch_failing_verification_is_blocked_in_isolation_before_repo_mutation(tmp_path, monkeypatch) -> None:
    runtime = Runtime.create(root=tmp_path / "runtime")
    scope = {"agent_id": "hongtu", "workspace_id": "code", "user_id": "darrow"}
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_git_repo(repo)
    target = repo / "module.py"
    target.write_text("VALUE = 'old'\n", encoding="utf-8")
    subprocess.run(["git", "add", "module.py"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "seed"], cwd=repo, check=True)
    seed_sha = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()
    monkeypatch.setenv("EIMEMORY_AUTONOMOUS_CODE_REPO", str(repo))
    experiment_id = create_sandbox_experiment(
        runtime,
        scope=scope,
        loop_id="learn_test",
        learning_goal_id="goal_preflight",
        research_note_id="note_preflight",
        candidate_kind="code_patch",
        candidate_patch={
            "summary": "Patch must fail in isolated verification",
            "repo_root": str(repo),
            "apply_to_repo": True,
            "deploy_to_production": False,
            "commit_to_repo": False,
            "allowed_files": ["module.py"],
            "file_updates": [{"path": "module.py", "content": "VALUE = 'new'\n"}],
            "verification_commands": [[sys.executable, "-c", "raise SystemExit(9)"]],
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

    result = promote_candidate(
        runtime,
        candidate_id=candidate_id,
        scope=scope,
        loop_id="learn_test",
        eval_result={**PASSING_EVAL, "gate_bundle": _l2_gate_bundle()},
        health={"ok": True},
    )

    assert result["ok"] is False
    assert result["applied"] is False
    assert result["blocked_reason"] == "code_preflight_gate"
    assert "side_effect" not in result
    assert target.read_text(encoding="utf-8") == "VALUE = 'old'\n"
    assert subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip() == seed_sha
    preflights = runtime.store.list_records(kinds=["replay_result"], scope=scope, limit=10)
    assert len(preflights) == 1
    assert preflights[0].meta["report_type"] == "code_patch_preflight"
    assert preflights[0].content["executed"] is True
    assert preflights[0].content["verdict"] == "fail"


def test_code_patch_rollout_writes_full_lifecycle_ledger(tmp_path, monkeypatch) -> None:
    runtime = Runtime.create(root=tmp_path / "runtime")
    scope = {"agent_id": "hongtu", "workspace_id": "code", "user_id": "darrow"}
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_git_repo(repo)
    target = repo / "module.py"
    target.write_text("VALUE = 'old'\n", encoding="utf-8")
    subprocess.run(["git", "add", "module.py"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "seed"], cwd=repo, check=True)
    monkeypatch.setenv("EIMEMORY_AUTONOMOUS_CODE_REPO", str(repo))
    release_path = tmp_path / "release-150"
    release_token = release_path.as_posix()
    experiment_id = create_sandbox_experiment(
        runtime,
        scope=scope,
        loop_id="learn_test",
        learning_goal_id="goal_1",
        research_note_id="note_1",
        candidate_kind="code_patch",
        candidate_patch={
            "summary": "Patch module and deploy immutable release",
            "repo_root": str(repo),
            "apply_to_repo": True,
            "deploy_to_production": True,
            "commit_to_repo": True,
            "allowed_files": ["module.py"],
            "file_updates": [{"path": "module.py", "content": "VALUE = 'new'\n"}],
            "verification_commands": [[sys.executable, "-c", "from pathlib import Path; assert Path('module.py').read_text(encoding='utf-8') == \"VALUE = 'new'\\n\""]],
            "deployment_commands": [[sys.executable, "-c", f"from pathlib import Path; Path(r'{release_token}').mkdir(parents=True, exist_ok=True); print('release={release_token}')"]],
            "post_deploy_health_commands": [[sys.executable, "-c", "print('health ok')"]],
            "rollback_plan": {"commands": [[sys.executable, "-c", "print('rollback ready')"]]},
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
    assert result["side_effect"]["commit"]["commit_sha"]
    assert result["side_effect"]["rollback_evidence"]["release_path"] == release_token
    ledger = [
        item
        for item in runtime.get_policy_rollout_ledger(scope=scope, limit=50)
        if item["source_opportunity_id"] == candidate_id
    ]
    actions = {item["action_type"] for item in ledger}
    assert {"proposed", "gate_passed", "applied", "deployed", "health_checked"}.issubset(actions)
    required_fields = {
        "candidate_id",
        "patch_id",
        "commit_sha",
        "release_path",
        "test_result",
        "health_result",
        "rollback_command",
        "observed_count",
        "failure_rate",
    }
    for entry in ledger:
        assert required_fields.issubset(entry["details"])
    aggregate = next(item for item in ledger if item["action_type"] == "capability_promotion")
    assert aggregate["details"]["test_result"]["reports"][0]["phase"] == "verify"
    assert aggregate["details"]["health_result"]["reports"][0]["phase"] == "post_deploy_health"
    assert aggregate["details"]["rollback_command"]


def test_code_patch_rollout_auto_canary_promotes_active(tmp_path, monkeypatch) -> None:
    runtime = Runtime.create(root=tmp_path / "runtime")
    scope = {"agent_id": "hongtu", "workspace_id": "code", "user_id": "darrow"}
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_git_repo(repo)
    target = repo / "module.py"
    target.write_text("VALUE = 'old'\n", encoding="utf-8")
    subprocess.run(["git", "add", "module.py"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "seed"], cwd=repo, check=True)
    monkeypatch.setenv("EIMEMORY_AUTONOMOUS_CODE_REPO", str(repo))
    release_path = tmp_path / "release-auto-canary"
    release_token = release_path.as_posix()
    canary_file = tmp_path / "canary_count.txt"
    experiment_id = create_sandbox_experiment(
        runtime,
        scope=scope,
        loop_id="learn_test",
        learning_goal_id="goal_1",
        research_note_id="note_1",
        candidate_kind="code_patch",
        candidate_patch={
            "summary": "Patch module and auto-promote after canary",
            "repo_root": str(repo),
            "apply_to_repo": True,
            "deploy_to_production": True,
            "commit_to_repo": True,
            "allowed_files": ["module.py"],
            "file_updates": [{"path": "module.py", "content": "VALUE = 'new'\n"}],
            "verification_commands": [[sys.executable, "-c", "from pathlib import Path; assert Path('module.py').read_text(encoding='utf-8') == \"VALUE = 'new'\\n\""]],
            "deployment_commands": [[sys.executable, "-c", f"from pathlib import Path; Path(r'{release_token}').mkdir(parents=True, exist_ok=True); print('release={release_token}')"]],
            "post_deploy_health_commands": [[sys.executable, "-c", "print('health ok')"]],
            "canary_commands": [
                [
                    sys.executable,
                    "-c",
                    f"from pathlib import Path; p=Path(r'{canary_file}'); n=int(p.read_text() or '0') if p.exists() else 0; p.write_text(str(n+1)); print('canary ok')",
                ]
            ],
            "canary_required_observations": 3,
            "rollback_plan": {"commands": [[sys.executable, "-c", "print('rollback ready')"]]},
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
    assert result["side_effect"]["production_applied"] is True
    assert result["side_effect"]["canary"]["status"] == "promoted_active"
    assert result["side_effect"]["canary"]["observed_count"] == 3
    assert result["side_effect"]["canary"]["failure_rate"] == 0.0
    assert canary_file.read_text(encoding="utf-8") == "3"
    assert runtime.store.get_by_id(candidate_id).status == "promoted"
    ledger = [
        item
        for item in runtime.get_policy_rollout_ledger(scope=scope, limit=80)
        if item["source_opportunity_id"] == candidate_id
    ]
    actions = [item["action_type"] for item in ledger]
    assert actions.count("shadow_observed") == 3
    assert "promoted_active" in actions
    active = next(item for item in ledger if item["action_type"] == "promoted_active")
    assert active["details"]["observed_count"] == 3
    assert active["details"]["failure_rate"] == 0.0
    assert active["details"]["commit_sha"] == result["side_effect"]["commit"]["commit_sha"]
    assert active["details"]["release_path"] == release_token


def test_code_patch_rollout_auto_canary_failure_rolls_back(tmp_path, monkeypatch) -> None:
    runtime = Runtime.create(root=tmp_path / "runtime")
    scope = {"agent_id": "hongtu", "workspace_id": "code", "user_id": "darrow"}
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_git_repo(repo)
    target = repo / "module.py"
    target.write_text("VALUE = 'old'\n", encoding="utf-8")
    subprocess.run(["git", "add", "module.py"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "seed"], cwd=repo, check=True)
    monkeypatch.setenv("EIMEMORY_AUTONOMOUS_CODE_REPO", str(repo))
    release_path = tmp_path / "release-bad-canary"
    release_token = release_path.as_posix()
    experiment_id = create_sandbox_experiment(
        runtime,
        scope=scope,
        loop_id="learn_test",
        learning_goal_id="goal_1",
        research_note_id="note_1",
        candidate_kind="code_patch",
        candidate_patch={
            "summary": "Patch module but rollback on bad canary",
            "repo_root": str(repo),
            "apply_to_repo": True,
            "deploy_to_production": True,
            "commit_to_repo": True,
            "allowed_files": ["module.py"],
            "file_updates": [{"path": "module.py", "content": "VALUE = 'new'\n"}],
            "verification_commands": [[sys.executable, "-c", "from pathlib import Path; assert Path('module.py').read_text(encoding='utf-8') == \"VALUE = 'new'\\n\""]],
            "deployment_commands": [[sys.executable, "-c", f"from pathlib import Path; Path(r'{release_token}').mkdir(parents=True, exist_ok=True); print('release={release_token}')"]],
            "post_deploy_health_commands": [[sys.executable, "-c", "print('health ok')"]],
            "canary_commands": [[sys.executable, "-c", "raise SystemExit(2)"]],
            "canary_required_observations": 3,
            "rollback_plan": {"commands": [[sys.executable, "-c", "print('rollback ready')"]]},
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

    assert result["ok"] is False
    assert result["blocked_reason"] == "code_patch_canary_failed"
    assert result["side_effect"]["canary"]["status"] == "rolled_back"
    assert result["side_effect"]["canary"]["observed_count"] == 1
    assert result["side_effect"]["canary"]["failure_rate"] == 1.0
    assert result["side_effect"]["rollback"]["ok"] is True
    assert target.read_text(encoding="utf-8") == "VALUE = 'old'\n"
    assert runtime.store.get_by_id(candidate_id).status == "candidate"
    ledger = [
        item
        for item in runtime.get_policy_rollout_ledger(scope=scope, limit=80)
        if item["source_opportunity_id"] == candidate_id
    ]
    actions = {item["action_type"] for item in ledger}
    assert {"shadow_observed", "rolled_back"}.issubset(actions)
    rolled_back = next(item for item in ledger if item["action_type"] == "rolled_back")
    assert rolled_back["details"]["observed_count"] == 1
    assert rolled_back["details"]["failure_rate"] == 1.0
    assert is_executed_rollback_ledger_record(rolled_back) is True


def test_default_code_patch_deployment_command_uses_user_systemd_without_sudo(tmp_path, monkeypatch) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    installer = repo / "deploy" / "install_immutable_release.sh"
    installer.parent.mkdir(parents=True)
    installer.write_text("#!/usr/bin/env bash\n", encoding="utf-8")
    monkeypatch.delenv("EIMEMORY_AUTONOMOUS_CODE_DEPLOY_COMMAND", raising=False)

    commands = _deployment_commands({}, repo)

    assert commands
    command_text = " ".join(commands[0]) if isinstance(commands[0], list) else commands[0]
    assert "sudo" not in command_text
    assert "systemctl --user" in command_text
    assert "install_immutable_release.sh" in command_text


def test_code_patch_deployment_failure_reverts_created_commit(tmp_path, monkeypatch) -> None:
    runtime = Runtime.create(root=tmp_path / "runtime")
    scope = {"agent_id": "hongtu", "workspace_id": "code", "user_id": "darrow"}
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_git_repo(repo)
    target = repo / "module.py"
    target.write_text("VALUE = 'old'\n", encoding="utf-8")
    subprocess.run(["git", "add", "module.py"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "seed"], cwd=repo, check=True)
    seed_sha = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()
    monkeypatch.setenv("EIMEMORY_AUTONOMOUS_CODE_REPO", str(repo))
    experiment_id = create_sandbox_experiment(
        runtime,
        scope=scope,
        loop_id="learn_test",
        learning_goal_id="goal_1",
        research_note_id="note_1",
        candidate_kind="code_patch",
        candidate_patch={
            "summary": "Patch module but rollback failed deployment cleanly",
            "repo_root": str(repo),
            "apply_to_repo": True,
            "deploy_to_production": True,
            "commit_to_repo": True,
            "allowed_files": ["module.py"],
            "file_updates": [{"path": "module.py", "content": "VALUE = 'new'\n"}],
            "verification_commands": [[sys.executable, "-c", "print('tests ok')"]],
            "deployment_commands": [[sys.executable, "-c", "raise SystemExit(8)"]],
            "post_deploy_health_commands": [[sys.executable, "-c", "print('health ok')"]],
            "rollback_plan": {"commands": [[sys.executable, "-c", "print('rollback ready')"]]},
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

    assert result["ok"] is False
    assert result["blocked_reason"] == "code_patch_deployment_failed"
    assert target.read_text(encoding="utf-8") == "VALUE = 'old'\n"
    assert subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip() == seed_sha
    assert subprocess.check_output(["git", "status", "--porcelain"], cwd=repo, text=True).strip() == ""


def test_code_patch_blocks_dirty_repo_before_mutation_when_committing(tmp_path, monkeypatch) -> None:
    runtime = Runtime.create(root=tmp_path / "runtime")
    scope = {"agent_id": "hongtu", "workspace_id": "code", "user_id": "darrow"}
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_git_repo(repo)
    target = repo / "module.py"
    target.write_text("VALUE = 'old'\n", encoding="utf-8")
    subprocess.run(["git", "add", "module.py"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "seed"], cwd=repo, check=True)
    seed_sha = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()
    target.write_text("VALUE = 'local'\n", encoding="utf-8")
    monkeypatch.setenv("EIMEMORY_AUTONOMOUS_CODE_REPO", str(repo))
    experiment_id = create_sandbox_experiment(
        runtime,
        scope=scope,
        loop_id="learn_test",
        learning_goal_id="goal_1",
        research_note_id="note_1",
        candidate_kind="code_patch",
        candidate_patch={
            "summary": "Patch module only when repo is clean",
            "repo_root": str(repo),
            "apply_to_repo": True,
            "commit_to_repo": True,
            "allowed_files": ["module.py"],
            "file_updates": [{"path": "module.py", "content": "VALUE = 'new'\n"}],
            "verification_commands": [[sys.executable, "-c", "print('tests ok')"]],
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

    assert result["ok"] is False
    assert result["blocked_reason"] == "code_patch_repo_not_clean"
    assert target.read_text(encoding="utf-8") == "VALUE = 'local'\n"
    assert subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip() == seed_sha


def test_code_patch_requires_explicit_allowed_files_before_mutation(tmp_path, monkeypatch) -> None:
    runtime = Runtime.create(root=tmp_path / "runtime")
    scope = {"agent_id": "hongtu", "workspace_id": "code", "user_id": "darrow"}
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setenv("EIMEMORY_AUTONOMOUS_CODE_REPO", str(repo))
    target = repo / "unlisted.py"
    target.write_text("VALUE = 'old'\n", encoding="utf-8")
    experiment_id = create_sandbox_experiment(
        runtime,
        scope=scope,
        loop_id="learn_test",
        learning_goal_id="goal_1",
        research_note_id="note_1",
        candidate_kind="code_patch",
        candidate_patch={
            "summary": "Invalid missing allowed files",
            "repo_root": str(repo),
            "apply_to_repo": True,
            "deploy_to_production": False,
            "commit_to_repo": False,
            "file_updates": [{"path": "unlisted.py", "content": "VALUE = 'new'\n"}],
            "verification_commands": [[sys.executable, "-c", "print('ok')"]],
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

    result = promote_candidate(
        runtime,
        candidate_id=candidate_id,
        scope=scope,
        loop_id="learn_test",
        eval_result={**PASSING_EVAL, "gate_bundle": _l2_gate_bundle()},
        health={"ok": True},
    )

    assert result["ok"] is False
    assert result["blocked_reason"] == "code_patch_requires_allowed_files"
    assert target.read_text(encoding="utf-8") == "VALUE = 'old'\n"


def test_code_patch_requires_strict_rollout_contract(tmp_path, monkeypatch) -> None:
    runtime = Runtime.create(root=tmp_path / "runtime")
    scope = {"agent_id": "hongtu", "workspace_id": "code", "user_id": "darrow"}
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setenv("EIMEMORY_AUTONOMOUS_CODE_REPO", str(repo))
    (repo / "module.py").write_text("VALUE = 'old'\n", encoding="utf-8")
    experiment_id = create_sandbox_experiment(
        runtime,
        scope=scope,
        loop_id="learn_test",
        learning_goal_id="goal_1",
        research_note_id="note_1",
        candidate_kind="code_patch",
        candidate_patch={
            "summary": "Invalid missing rollback plan",
            "repo_root": str(repo),
            "apply_to_repo": True,
            "deploy_to_production": True,
            "commit_to_repo": True,
            "allowed_files": ["module.py"],
            "file_updates": [{"path": "module.py", "content": "VALUE = 'new'\n"}],
            "verification_commands": [[sys.executable, "-c", "print('ok')"]],
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

    assert result["ok"] is False
    assert result["blocked_reason"] == "code_patch_requires_rollback_plan"
    assert (repo / "module.py").read_text(encoding="utf-8") == "VALUE = 'old'\n"


def test_code_patch_rolls_back_when_post_deploy_health_fails(tmp_path, monkeypatch) -> None:
    runtime = Runtime.create(root=tmp_path / "runtime")
    scope = {"agent_id": "hongtu", "workspace_id": "code", "user_id": "darrow"}
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_git_repo(repo)
    target = repo / "module.py"
    target.write_text("VALUE = 'old'\n", encoding="utf-8")
    subprocess.run(["git", "add", "module.py"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "seed"], cwd=repo, check=True)
    monkeypatch.setenv("EIMEMORY_AUTONOMOUS_CODE_REPO", str(repo))
    rollback_marker = tmp_path / "rollback.txt"
    experiment_id = create_sandbox_experiment(
        runtime,
        scope=scope,
        loop_id="learn_test",
        learning_goal_id="goal_1",
        research_note_id="note_1",
        candidate_kind="code_patch",
        candidate_patch={
            "summary": "Patch module with failing health",
            "repo_root": str(repo),
            "apply_to_repo": True,
            "deploy_to_production": True,
            "commit_to_repo": True,
            "allowed_files": ["module.py"],
            "file_updates": [{"path": "module.py", "content": "VALUE = 'new'\n"}],
            "verification_commands": [[sys.executable, "-c", "print('tests ok')"]],
            "deployment_commands": [[sys.executable, "-c", "print('release=/tmp/bad-release')"]],
            "post_deploy_health_commands": [[sys.executable, "-c", "raise SystemExit(9)"]],
            "rollback_plan": {"commands": [[sys.executable, "-c", f"from pathlib import Path; Path(r'{rollback_marker}').write_text('rolled back', encoding='utf-8')"]]},
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

    assert result["ok"] is False
    assert result["blocked_reason"] == "code_patch_post_deploy_health_failed"
    assert result["side_effect"]["rollback"]["ok"] is True
    assert rollback_marker.read_text(encoding="utf-8") == "rolled back"
    ledger = runtime.get_policy_rollout_ledger(scope=scope, action="rolled_back", limit=10)
    assert ledger[0]["source_opportunity_id"] == candidate_id
    assert is_executed_rollback_ledger_record(ledger[0]) is True


def test_code_patch_marks_rollback_failed_when_rollback_command_fails(tmp_path, monkeypatch) -> None:
    runtime = Runtime.create(root=tmp_path / "runtime")
    scope = {"agent_id": "hongtu", "workspace_id": "code", "user_id": "darrow"}
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_git_repo(repo)
    target = repo / "module.py"
    target.write_text("VALUE = 'old'\n", encoding="utf-8")
    subprocess.run(["git", "add", "module.py"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "seed"], cwd=repo, check=True)
    monkeypatch.setenv("EIMEMORY_AUTONOMOUS_CODE_REPO", str(repo))
    experiment_id = create_sandbox_experiment(
        runtime,
        scope=scope,
        loop_id="learn_test",
        learning_goal_id="goal_1",
        research_note_id="note_1",
        candidate_kind="code_patch",
        candidate_patch={
            "summary": "Patch module with failing health and failing rollback command",
            "repo_root": str(repo),
            "apply_to_repo": True,
            "deploy_to_production": True,
            "commit_to_repo": True,
            "allowed_files": ["module.py"],
            "file_updates": [{"path": "module.py", "content": "VALUE = 'new'\n"}],
            "verification_commands": [[sys.executable, "-c", "print('tests ok')"]],
            "deployment_commands": [[sys.executable, "-c", "print('release=/tmp/bad-release')"]],
            "post_deploy_health_commands": [[sys.executable, "-c", "raise SystemExit(9)"]],
            "rollback_plan": {"commands": [[sys.executable, "-c", "raise SystemExit(6)"]]},
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

    result = promote_candidate(
        runtime,
        candidate_id=candidate_id,
        scope=scope,
        loop_id="learn_test",
        eval_result={**PASSING_EVAL, "gate_bundle": _l2_gate_bundle()},
        health={"ok": True},
    )

    assert result["ok"] is False
    assert result["blocked_reason"] == "code_patch_post_deploy_health_failed"
    assert result["side_effect"]["rollback"]["ok"] is False
    assert result["side_effect"]["rolled_back"] is False
    assert result["side_effect"]["rollback_failed"] is True
    ledger = runtime.get_policy_rollout_ledger(scope=scope, action="rollback_failed", limit=10)
    assert ledger[0]["source_opportunity_id"] == candidate_id


def test_l2_code_patch_blocks_when_post_deploy_health_fails(tmp_path, monkeypatch) -> None:
    runtime = Runtime.create(root=tmp_path)
    scope = {"agent_id": "hongtu"}
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_git_repo(repo)
    target = repo / "health_probe.py"
    target.write_text("VERSION = 'old'\n", encoding="utf-8")
    subprocess.run(["git", "add", "health_probe.py"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "seed"], cwd=repo, check=True)
    monkeypatch.setenv("EIMEMORY_AUTONOMOUS_CODE_REPO", str(repo))
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
            "commit_to_repo": True,
            "allowed_files": ["health_probe.py"],
            "file_updates": [{"path": "health_probe.py", "content": "VERSION = 'new'\n"}],
            "verification_commands": [[sys.executable, "-c", "print('tests ok')"]],
            "deployment_commands": [[sys.executable, "-c", "print('deployed')"]],
            "post_deploy_health_commands": [[sys.executable, "-c", "raise SystemExit(7)"]],
            "rollback_plan": {"commands": [[sys.executable, "-c", "print('rollback ready')"]]},
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


def test_l2_code_patch_ignores_untrusted_replay_and_requires_executed_preflight(tmp_path, monkeypatch) -> None:
    runtime = Runtime.create(root=tmp_path)
    scope = {"agent_id": "hongtu"}
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setenv("EIMEMORY_AUTONOMOUS_CODE_REPO", str(repo))
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
    assert result["blocked_reason"] == "code_patch_requires_verification_commands"
    assert target.read_text(encoding="utf-8") == "VERSION = 'old'\n"
    assert runtime.store.get_by_id(candidate_id).status == "candidate"
    ledger = runtime.get_policy_rollout_ledger(scope=scope, action="capability_promotion", limit=10)
    entry = next(item for item in ledger if item["promotion_id"] == result["promotion_request_id"])
    assert entry["source_opportunity_id"] == candidate_id
    assert entry["budget_decision"] == "blocked"
    assert entry["reason"] == "code_patch_requires_verification_commands"
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
        "closed_loop": {
            "doctor": {"ok": True, "source": "eimemory doctor"},
            "smoke": {"ok": True, "source": "openclaw_loop smoke"},
        },
        "real_task_replay": {
            "ok": True,
            "report_type": "real_task_replay",
            "verdict": "pass",
            "pass_rate": 1.0,
            "threshold": 0.6,
            "sample_count": 2,
        },
    }


def _init_git_repo(repo) -> None:
    subprocess.run(["git", "init"], cwd=repo, check=True, stdout=subprocess.DEVNULL)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=repo, check=True)
