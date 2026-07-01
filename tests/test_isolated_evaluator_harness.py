from __future__ import annotations

import json

from eimemory.api.runtime import Runtime
from eimemory.governance.isolated_evaluator import (
    build_evaluation_packet,
    judge_stop_condition,
    run_isolated_evaluator,
    run_isolated_evaluator_harness,
)


def test_evaluation_packet_isolates_generator_claim_from_evaluator_context(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    scope = {"agent_id": "main"}

    packet = build_evaluation_packet(
        runtime,
        scope=scope,
        loop_id="loop_iso_1",
        goal={"title": "Fix long-running drift", "target_capability": "code.implementation"},
        candidate_kind="code_patch",
        artifact={"summary": "patch files", "file_updates": [{"path": "eimemory/foo.py", "content": "x = 1"}]},
        generator_claim="I already checked this and it is definitely correct.",
        replay_gate={"ok": True, "pass_rate": 1.0, "sample_count": 2},
        real_task_replay={"ok": True, "verdict": "pass", "pass_rate": 1.0, "pass_count": 2, "fail_count": 0},
    )

    assert packet.kind == "evaluation_packet"
    assert packet.status == "candidate"
    assert packet.content["model_roles"]["generator_model"] == "gpt"
    assert packet.content["model_roles"]["evaluator_model"] == "minimax"
    assert packet.content["generator_claim"]["isolated"] is True
    assert packet.content["generator_claim"]["text"] == "I already checked this and it is definitely correct."
    evaluator_context = json.dumps(packet.content["evaluator_context"], ensure_ascii=False)
    assert "definitely correct" not in evaluator_context
    assert packet.content["evaluator_context"]["artifact"]["file_update_count"] == 1


def test_evaluator_defaults_to_fail_without_real_execution_evidence(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)

    packet = build_evaluation_packet(
        runtime,
        scope={"agent_id": "main"},
        loop_id="loop_iso_2",
        goal={"title": "Patch without proof"},
        candidate_kind="sop_draft",
        artifact={"summary": "Generator says it is good."},
        generator_claim="The SOP is enough; no need to run anything.",
        replay_gate={"ok": False, "reason": "not_run"},
        real_task_replay={},
    )
    verdict = run_isolated_evaluator(runtime, packet, scope={"agent_id": "main"}, loop_id="loop_iso_2")

    assert verdict.kind == "evaluator_verdict"
    assert verdict.content["verdict"] == "fail"
    assert verdict.content["promotion_allowed"] is False
    assert "missing_real_execution_evidence" in verdict.content["blocked_reasons"]
    assert verdict.content["skeptical_default"] is True


def test_evaluator_requires_model_separation_even_with_passing_replay(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)

    packet = build_evaluation_packet(
        runtime,
        scope={"agent_id": "main"},
        loop_id="loop_iso_3",
        goal={"title": "Same model review"},
        candidate_kind="eval_case",
        artifact={"summary": "replay case"},
        generator_claim="This passes.",
        replay_gate={"ok": True, "pass_rate": 1.0, "sample_count": 1},
        real_task_replay={"ok": True, "verdict": "pass", "pass_rate": 1.0, "pass_count": 1, "fail_count": 0},
        generator_model="gpt",
        evaluator_model="gpt",
    )
    verdict = run_isolated_evaluator(runtime, packet, scope={"agent_id": "main"}, loop_id="loop_iso_3")

    assert verdict.content["verdict"] == "fail"
    assert "model_not_isolated" in verdict.content["blocked_reasons"]


def test_evaluator_rejects_low_quality_replay_even_when_verdict_says_pass(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)

    packet = build_evaluation_packet(
        runtime,
        scope={"agent_id": "main"},
        loop_id="loop_iso_low_quality",
        goal={"title": "Weak replay should not promote"},
        candidate_kind="eval_case",
        artifact={"summary": "replay reported pass with poor metrics"},
        generator_claim="Replay says pass.",
        replay_gate={"ok": True, "pass_rate": 0.5, "sample_count": 4, "threshold": 0.8},
        real_task_replay={"ok": True, "verdict": "pass", "pass_rate": 0.5, "pass_count": 2, "fail_count": 2},
    )
    verdict = run_isolated_evaluator(runtime, packet, scope={"agent_id": "main"}, loop_id="loop_iso_low_quality")
    judgment = judge_stop_condition(runtime, verdict, scope={"agent_id": "main"}, loop_id="loop_iso_low_quality")

    assert verdict.content["verdict"] == "fail"
    assert "insufficient_replay_quality" in verdict.content["blocked_reasons"]
    assert verdict.content["real_execution"]["passed"] is False
    assert verdict.content["real_execution"]["sample_count"] == 4
    assert verdict.content["real_execution"]["pass_rate"] == 0.5
    assert judgment.content["decision"] == "continue"


def test_verification_returncode_zero_counts_as_real_execution(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)

    packet = build_evaluation_packet(
        runtime,
        scope={"agent_id": "main"},
        loop_id="loop_iso_verification",
        goal={"title": "Verified by command"},
        candidate_kind="eval_case",
        artifact={"summary": "command evidence"},
        generator_claim="Command output proves this.",
        replay_gate={"ok": False, "reason": "not_run"},
        real_task_replay={},
        verification_results=[{"command": "pytest tests/foo.py", "returncode": 0}],
    )
    verdict = run_isolated_evaluator(runtime, packet, scope={"agent_id": "main"}, loop_id="loop_iso_verification")

    assert verdict.content["verdict"] == "pass"
    assert verdict.content["real_execution"]["command_passed"] is True


def test_verification_results_fail_closed_when_any_command_fails(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)

    packet = build_evaluation_packet(
        runtime,
        scope={"agent_id": "main"},
        loop_id="loop_iso_mixed_verification",
        goal={"title": "Mixed verification must not promote"},
        candidate_kind="code_patch",
        artifact={"summary": "one smoke command passed but targeted test failed"},
        generator_claim="The smoke command passed.",
        replay_gate={"ok": False, "reason": "not_run"},
        real_task_replay={},
        verification_results=[
            {"command": "python -m compileall eimemory", "returncode": 0},
            {"command": "python -m pytest tests/test_target.py", "returncode": 1},
        ],
    )
    verdict = run_isolated_evaluator(runtime, packet, scope={"agent_id": "main"}, loop_id="loop_iso_mixed_verification")
    judgment = judge_stop_condition(runtime, verdict, scope={"agent_id": "main"}, loop_id="loop_iso_mixed_verification")

    assert verdict.content["verdict"] == "fail"
    assert verdict.content["promotion_allowed"] is False
    assert "verification_command_failed" in verdict.content["blocked_reasons"]
    assert verdict.content["real_execution"]["command_passed"] is False
    assert verdict.content["real_execution"]["command_failed_count"] == 1
    assert judgment.content["decision"] == "require_human"


def test_stop_judge_stops_only_after_passed_isolated_verdict(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    scope = {"agent_id": "main"}

    packet = build_evaluation_packet(
        runtime,
        scope=scope,
        loop_id="loop_iso_4",
        goal={"title": "Verified candidate"},
        candidate_kind="eval_case",
        artifact={"summary": "replay case"},
        generator_claim="Generator rationale should not decide stop.",
        replay_gate={"ok": True, "pass_rate": 1.0, "sample_count": 3},
        real_task_replay={"ok": True, "verdict": "pass", "pass_rate": 1.0, "pass_count": 3, "fail_count": 0},
    )
    verdict = run_isolated_evaluator(runtime, packet, scope=scope, loop_id="loop_iso_4")
    judgment = judge_stop_condition(runtime, verdict, scope=scope, loop_id="loop_iso_4")

    assert verdict.content["verdict"] == "pass"
    assert judgment.kind == "stop_judgment"
    assert judgment.content["decision"] == "stop"
    assert judgment.content["model_roles"]["stop_judge_model"] == "minimax"
    assert judgment.content["promotion_allowed"] is True


def test_stop_judge_must_not_share_generator_model_even_after_passed_verdict(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    scope = {"agent_id": "main"}
    packet = build_evaluation_packet(
        runtime,
        scope=scope,
        loop_id="loop_iso_stop_separation",
        goal={"title": "Verified candidate with unsafe stop judge"},
        candidate_kind="eval_case",
        artifact={"summary": "replay case"},
        generator_claim="Generator rationale should not decide stop.",
        replay_gate={"ok": True, "pass_rate": 1.0, "sample_count": 3},
        real_task_replay={"ok": True, "verdict": "pass", "pass_rate": 1.0, "pass_count": 3, "fail_count": 0},
        generator_model="gpt",
        evaluator_model="minimax",
        stop_judge_model="minimax",
    )
    verdict = run_isolated_evaluator(runtime, packet, scope=scope, loop_id="loop_iso_stop_separation")

    judgment = judge_stop_condition(runtime, verdict, scope=scope, loop_id="loop_iso_stop_separation", stop_judge_model="gpt")

    assert verdict.content["verdict"] == "pass"
    assert judgment.content["decision"] == "quarantine"
    assert judgment.content["promotion_allowed"] is False
    assert "stop_judge_not_isolated" in judgment.content["blocked_reasons"]


def test_stop_judge_model_changes_do_not_reuse_previous_judgment(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    scope = {"agent_id": "main"}
    packet = build_evaluation_packet(
        runtime,
        scope=scope,
        loop_id="loop_iso_stop_reuse",
        goal={"title": "Verified candidate with two stop judges"},
        candidate_kind="eval_case",
        artifact={"summary": "replay case"},
        generator_claim="Generator rationale should not decide stop.",
        replay_gate={"ok": True, "pass_rate": 1.0, "sample_count": 3},
        real_task_replay={"ok": True, "verdict": "pass", "pass_rate": 1.0, "pass_count": 3, "fail_count": 0},
    )
    verdict = run_isolated_evaluator(runtime, packet, scope=scope, loop_id="loop_iso_stop_reuse")
    safe = judge_stop_condition(runtime, verdict, scope=scope, loop_id="loop_iso_stop_reuse", stop_judge_model="minimax")
    unsafe = judge_stop_condition(runtime, verdict, scope=scope, loop_id="loop_iso_stop_reuse", stop_judge_model="gpt")

    assert safe.record_id != unsafe.record_id
    assert safe.content["decision"] == "stop"
    assert unsafe.content["decision"] == "quarantine"


def test_model_role_changes_do_not_reuse_previous_packet(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)

    first = run_isolated_evaluator_harness(runtime, scope={"agent_id": "main"}, loop_id="same_loop")
    second = run_isolated_evaluator_harness(
        runtime,
        scope={"agent_id": "main"},
        loop_id="same_loop",
        generator_model="gpt",
        evaluator_model="gpt",
    )

    assert first["packet_id"] != second["packet_id"]
    assert first["promotion_allowed"] is True
    assert second["promotion_allowed"] is False
    assert second["decision"] == "quarantine"


def test_replay_evidence_changes_do_not_reuse_previous_packet(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)

    first = run_isolated_evaluator_harness(runtime, scope={"agent_id": "main"}, loop_id="same_loop")
    second = run_isolated_evaluator_harness(
        runtime,
        scope={"agent_id": "main"},
        loop_id="same_loop",
        replay_gate={"ok": False, "verdict": "fail", "pass_rate": 0.0, "sample_count": 1, "threshold": 0.6},
        real_task_replay={"ok": False, "verdict": "fail", "pass_rate": 0.0, "pass_count": 0, "fail_count": 1},
    )

    assert first["packet_id"] != second["packet_id"]
    assert first["promotion_allowed"] is True
    assert second["promotion_allowed"] is False
    assert second["verdict"] == "fail"
    assert second["decision"] == "continue"
