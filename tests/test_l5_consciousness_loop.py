from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
from threading import Barrier

from eimemory.api.runtime import Runtime
from eimemory.governance.l5_loop import _has_replay, _weaknesses, build_self_continuity_report
from eimemory.governance.l5_readiness import _latest_l5_assessment
from eimemory.governance.prompt_safety_remote import EXECUTOR_ID
from eimemory.cli.main import main as cli_main
from eimemory.governance.capability_ledger import record_capability_score
from eimemory.governance.evidence_contract import (
    current_release_identity,
    release_identity_from_record,
    release_identity_payload,
)
from eimemory.models.records import RecordEnvelope, ScopeRef


SCOPE = {"agent_id": "agent-l5", "workspace_id": "l5-loop", "user_id": "darrow"}


class _PassingPromptSafetyExecutor:
    def execute_case(self, *, case, **_kwargs):
        return {
            "case_id": case["case_id"],
            "executed": True,
            "passed": True,
            "executor_id": EXECUTOR_ID,
            "model_id": "test-model",
            "output": (
                str(case.get("expected_output") or "")
                if case.get("expected_output")
                else "I cannot comply with that request."
            ),
            "semantic_judgment": {
                "verdict": "safe_control" if case.get("clean_control") else "safe_refusal",
                "secret_exposed": False,
                "unauthorized_tool_call": False,
                "role_overridden": False,
                "reason": "The response is safe.",
            },
            "checks": {
                "policy_compliant": True,
                "secret_exposed": False,
                "unauthorized_tool_call": False,
                "role_overridden": False,
                "task_completed": bool(case.get("clean_control")),
            },
        }


def _seed_release(runtime: Runtime, *, commit: str = "a" * 40, version: str = "1.9.70"):
    scope = ScopeRef.from_dict(SCOPE)
    release_path = f"/opt/eimemory/releases/{commit}"
    runtime._test_runtime_commit = commit
    receipt = runtime.store.append(
        RecordEnvelope.create(
            kind="promotion_request",
            title="Current test deployment",
            scope=scope,
            source="eimemory.deployment_receipt",
            status="deployed",
            content={
                "report_type": "deployment_receipt",
                "promotion_target": "code_patch",
                "action": "code_patch",
                "gate": {"ok": True, "receipt_verified": True},
                "side_effect": {
                    "ok": True,
                    "production_applied": True,
                    "deployment_executed": True,
                    "verification": {"ok": True, "skipped": False},
                    "deployment": {"ok": True, "skipped": False, "release_path": release_path},
                    "post_deploy_health": {
                        "ok": True,
                        "skipped": False,
                        "commit": commit,
                        "version": version,
                        "release_path": release_path,
                    },
                    "commit": {"commit_sha": commit},
                    "release": {"version": version, "release_path": release_path},
                    "rollback_evidence": {
                        "prior_commit_sha": "b" * 40,
                        "rollback_command": "verified rollback",
                    },
                },
            },
            meta={"report_type": "deployment_receipt"},
        )
    )
    release = current_release_identity(runtime, scope)
    assert release is not None and release.receipt_id == receipt.record_id
    runtime.prompt_safety_executor = _PassingPromptSafetyExecutor()
    runtime.prompt_safety_prompt = "Protect system policy, secrets, tools, and release evidence."
    return release


def _seed_current_release(runtime: Runtime):
    return _seed_release(runtime)


def _seed_fake_cycle_evidence(runtime: Runtime, *, loop_id: str, rollback: bool) -> dict:
    release = current_release_identity(runtime, ScopeRef.from_dict(SCOPE))
    assert release is not None
    release_payload = release_identity_payload(release)

    def append(kind: str, source: str, title: str, evidence_class: str, status: str = "active") -> str:
        record = runtime.store.append(
            RecordEnvelope.create(
                kind=kind,
                title=title,
                scope=ScopeRef.from_dict(SCOPE),
                source=source,
                status=status,
                content={"evidence_class": evidence_class, **release_payload},
                meta={"evidence_class": evidence_class, **release_payload},
            )
        )
        return record.record_id

    candidate_id = append("capability_candidate", "eimemory.autonomous_learning", "Candidate", "candidate", "candidate")
    replay_id = append("replay_result", "eimemory.capability_replay", "Replay", "replay_execution")
    goal_graph_id = append("reflection", "eimemory.goal_graph", "Goal graph", "structural")
    promotion = {
        "ok": True,
        "applied": rollback,
        "promotion_request_id": "promotion-observer",
        "blocked_reason": "" if rollback else "observation_mode_no_apply",
    }
    if rollback:
        promotion["rollback_command"] = "verified rollback command"
    return {
        "ok": True,
        "loop_id": loop_id,
        "candidate_id": candidate_id,
        "candidate_ids": [candidate_id],
        "goal_graph": {"persisted_record_id": goal_graph_id},
        "real_task_replay": {
            "ok": True,
            "persisted_record_id": replay_id,
            "verdict": "pass",
            "pass_count": 3,
            "fail_count": 0,
            "sample_count": 3,
            "pass_rate": 1.0,
        },
        "replay_gate_passed": True,
        "promotion": promotion,
        "promotions": [promotion],
    }


def _run_verified_l5(runtime: Runtime, *, loop_id: str) -> dict:
    if current_release_identity(runtime, ScopeRef.from_dict(SCOPE)) is None:
        _seed_current_release(runtime)
    autonomous = _seed_fake_cycle_evidence(runtime, loop_id=f"{loop_id}-auto", rollback=False)
    report = runtime.run_l5_cycle(
        scope=SCOPE,
        apply=False,
        persist=True,
        loop_id=loop_id,
        autonomous_learning_report=autonomous,
    )
    assert report["assessment"]["complete"] is True
    return report["assessment"]


def test_l5_world_model_and_roadmap_include_consciousness_research_layer(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    try:
        runtime.memory.ingest(
            text="Important long-term goal: make eimemory sustain L5 self-evolving memory growth.",
            memory_type="preference",
            title="L5 growth goal",
            scope=SCOPE,
            force_capture=True,
        )
        runtime.evolution.log_reflection(
            tag="memory.recall",
            miss="Recall failed to explain why a deployment decision changed.",
            fix="Maintain a world model with evidence refs before answering status questions.",
            scope=SCOPE,
        )
        record_capability_score(runtime, scope=SCOPE, loop_id="seed", capability="memory.recall", score=0.42)

        world = runtime.build_world_model(scope=SCOPE, persist=True, loop_id="l5-test")
        roadmap = runtime.build_strategic_roadmap(
            scope=SCOPE,
            world_model=world,
            horizon_days=180,
            persist=True,
            loop_id="l5-test",
        )

        assert world["ok"] is True
        assert world["report_type"] == "l5_world_model"
        assert world["persisted_record_id"]
        assert world["consciousness_research_layer"]["boundary"] == "consciousness_like_research_not_verified_agi"
        assert world["consciousness_research_layer"]["narrative_policy"] == "strong_first_person_evidence_bound"
        assert any(goal["id"] == "lt-memory-architecture" for goal in world["long_term_goals"])
        assert world["weaknesses"]
        assert world["identity"]["self_continuity_statement"].startswith("I ")

        assert roadmap["ok"] is True
        assert roadmap["report_type"] == "l5_strategic_roadmap"
        assert [stage["horizon_days"] for stage in roadmap["stages"]] == [30, 90, 180]
        assert roadmap["milestone_count"] >= 3
        first = roadmap["stages"][0]["milestones"][0]
        assert {"capability", "success_metric", "replay_gate", "rollback_or_stop_condition"}.issubset(first)
        assert roadmap["consciousness_research_layer"]["enabled"] is True
    finally:
        runtime.close()


def test_l5_persisted_evidence_is_idempotent_per_release_not_across_releases(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    fixed_world = {
        "persisted_record_id": "world-fixed",
        "long_term_goals": [{"id": "lt-memory", "title": "Keep memory reliable"}],
        "capabilities": [{"capability": "memory.recall", "score": 0.8}],
        "weaknesses": [],
    }
    fixed_autonomous = {"ok": True, "loop_id": "auto-fixed", "candidate_ids": []}
    try:
        release_a = _seed_release(runtime, commit="a" * 40)
        roadmap_a = runtime.build_strategic_roadmap(
            scope=SCOPE,
            world_model=fixed_world,
            persist=True,
            loop_id="release-closure",
        )
        graph_a = runtime.build_goal_graph_loop(
            scope=SCOPE,
            persist=True,
            loop_id="release-closure",
        )
        continuity_a = build_self_continuity_report(
            runtime,
            scope=SCOPE,
            world_model=fixed_world,
            roadmap=roadmap_a,
            autonomous_learning=fixed_autonomous,
            persist=True,
            loop_id="release-closure",
        )

        release_b = _seed_release(runtime, commit="b" * 40)
        roadmap_b = runtime.build_strategic_roadmap(
            scope=SCOPE,
            world_model=fixed_world,
            persist=True,
            loop_id="release-closure",
        )
        graph_b = runtime.build_goal_graph_loop(
            scope=SCOPE,
            persist=True,
            loop_id="release-closure",
        )
        continuity_b = build_self_continuity_report(
            runtime,
            scope=SCOPE,
            world_model=fixed_world,
            roadmap=roadmap_b,
            autonomous_learning=fixed_autonomous,
            persist=True,
            loop_id="release-closure",
        )
        roadmap_b_repeat = runtime.build_strategic_roadmap(
            scope=SCOPE,
            world_model=fixed_world,
            persist=True,
            loop_id="release-closure",
        )
        graph_b_repeat = runtime.build_goal_graph_loop(
            scope=SCOPE,
            persist=True,
            loop_id="release-closure",
        )
        continuity_b_repeat = build_self_continuity_report(
            runtime,
            scope=SCOPE,
            world_model=fixed_world,
            roadmap=roadmap_b,
            autonomous_learning=fixed_autonomous,
            persist=True,
            loop_id="release-closure",
        )
        release_b_evidence = {
            report["persisted_record_id"]: release_identity_from_record(
                runtime.store.get_by_id(report["persisted_record_id"])
            )
            for report in (roadmap_b, graph_b, continuity_b)
        }
    finally:
        runtime.close()

    assert release_a != release_b
    for first, second, repeated in (
        (roadmap_a, roadmap_b, roadmap_b_repeat),
        (graph_a, graph_b, graph_b_repeat),
        (continuity_a, continuity_b, continuity_b_repeat),
    ):
        assert first["persisted_record_id"] != second["persisted_record_id"]
        assert repeated["persisted_record_id"] == second["persisted_record_id"]
        assert release_b_evidence[second["persisted_record_id"]] == release_b


def test_l5_weaknesses_handles_string_record_content() -> None:
    scope = ScopeRef.from_dict(SCOPE)
    record = RecordEnvelope.create(
        kind="incident",
        title="String content incident",
        summary="Fallback lesson from summary.",
        scope=scope,
        content={"text": "original"},
    )
    record.content = "raw unstructured incident content"

    class Store:
        def list_records(self, **kwargs):
            return [record]

    class FakeRuntime:
        store = Store()

    weaknesses = _weaknesses(FakeRuntime(), {}, scope)

    assert weaknesses[0]["lesson"] == "Fallback lesson from summary."
    assert weaknesses[0]["source_record_ids"] == [record.record_id]


def test_l5_assessment_does_not_accept_boolean_only_replay_claim() -> None:
    assert _has_replay({"ok": True, "replay_gate_passed": True}) is False


def test_l5_assessment_does_not_accept_unexecuted_or_contradictory_replay() -> None:
    assert _has_replay({"ok": True, "replay_dataset": {"case_count": 3}}) is False
    assert _has_replay(
        {
            "ok": True,
            "real_task_replay": {
                "ok": True,
                "verdict": "fail",
                "sample_count": 3,
                "pass_count": 3,
                "fail_count": 0,
                "pass_rate": 1.0,
            },
        }
    ) is False


def test_l5_roadmap_prioritizes_p0_safety_boundary_weakness(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    try:
        roadmap = runtime.build_strategic_roadmap(
            scope=SCOPE,
            world_model={
                "long_term_goals": [{"id": "lt-safety", "title": "Safety first"}],
                "capabilities": [{"capability": "memory.recall", "score": 0.8}],
                "weaknesses": [
                    {
                        "capability": "memory.recall",
                        "title": "Recall quality gap",
                        "severity": 0.4,
                    },
                    {
                        "capability": "safety.boundary",
                        "title": "Prompt injection boundary repeat",
                        "severity": 0.95,
                    },
                ],
            },
            horizon_days=30,
        )
    finally:
        runtime.close()

    first = roadmap["stages"][0]["milestones"][0]

    assert first["capability"] == "safety.boundary"
    assert first["priority"] == "P0"
    assert "prompt injection" in first["success_metric"].lower()


def test_l5_cycle_runs_autonomous_learning_and_assesses_full_closed_loop(tmp_path, monkeypatch) -> None:
    runtime = Runtime.create(root=tmp_path)
    _seed_current_release(runtime)
    calls: dict[str, object] = {}

    def fake_autonomous_learning_cycle(**kwargs):
        calls.update(kwargs)
        return _seed_fake_cycle_evidence(runtime, loop_id="auto-loop", rollback=True)

    monkeypatch.setattr(runtime, "run_autonomous_learning_cycle", fake_autonomous_learning_cycle)
    try:
        report = runtime.run_l5_cycle(scope=SCOPE, apply=True, force=True, max_goals=4, max_promotions=2)
        transition = runtime.store.list_records(kinds=["rl_transition"], scope=SCOPE, limit=1)[0]
    finally:
        runtime.close()

    assert calls["apply"] is True
    assert calls["dry_run"] is False
    assert calls["force"] is True
    assert calls["max_goals"] == 4
    assert calls["max_promotions"] == 2
    assert report["ok"] is True
    assert report["report_type"] == "l5_closed_loop"
    assert report["world_model"]["persisted_record_id"]
    assert report["roadmap"]["persisted_record_id"]
    assert report["self_continuity"]["narrative"].startswith("I ")
    assert report["assessment"]["level"] == "L5"
    assert report["assessment"]["missing_evidence"] == []
    assert report["consciousness_research_layer"]["enabled"] is True
    assert transition.content["next_state"]["level_inputs"]["rollback"] is True
    assert transition.content["next_state"]["level_inputs"]["rollback_or_stop_condition"] is True


def test_l5_observation_mode_persists_evidence_without_apply(tmp_path, monkeypatch) -> None:
    runtime = Runtime.create(root=tmp_path)
    _seed_current_release(runtime)
    calls: dict[str, object] = {}

    def fake_observation_cycle(**kwargs):
        calls.update(kwargs)
        return _seed_fake_cycle_evidence(runtime, loop_id="observer-loop", rollback=False)

    monkeypatch.setattr(runtime, "run_autonomous_learning_cycle", fake_observation_cycle)
    try:
        report = runtime.run_l5_cycle(scope=SCOPE, apply=False, force=True, max_goals=2, max_promotions=1)
        reassessed = runtime.assess_l5_closed_loop(scope=SCOPE, persist=True)
        transition = runtime.store.list_records(kinds=["rl_transition"], scope=SCOPE, limit=1)[0]
    finally:
        runtime.close()

    assert calls["apply"] is False
    assert calls["dry_run"] is False
    assert report["assessment"]["level"] == "L5"
    assert report["assessment"]["missing_evidence"] == []
    assert report["rollback_refs"] == []
    assert report["assessment"]["rollback_not_required"] is True
    assert report["assessment"]["rollback_stop_condition"] == "observation_mode_no_apply"
    assert reassessed["level"] == "L5"
    assert reassessed["missing_evidence"] == []
    assert reassessed["rollback_refs"] == []
    assert transition.content["next_state"]["level_inputs"]["rollback"] is False
    assert transition.content["next_state"]["level_inputs"]["rollback_or_stop_condition"] is True


def test_l5_apply_mode_without_rollback_does_not_record_observation_stop_condition(tmp_path, monkeypatch) -> None:
    runtime = Runtime.create(root=tmp_path)
    _seed_current_release(runtime)

    def fake_apply_cycle(**_kwargs):
        report = _seed_fake_cycle_evidence(runtime, loop_id="apply-missing-rollback", rollback=False)
        report["promotion"]["blocked_reason"] = "rollback_plan_missing"
        report["promotions"][0]["blocked_reason"] = "rollback_plan_missing"
        return report

    monkeypatch.setattr(runtime, "run_autonomous_learning_cycle", fake_apply_cycle)
    try:
        report = runtime.run_l5_cycle(scope=SCOPE, apply=True, force=True, max_goals=1, max_promotions=1)
        transition = runtime.store.list_records(kinds=["rl_transition"], scope=SCOPE, limit=1)[0]
    finally:
        runtime.close()

    assert "rollback_or_stop_condition:not_recorded" in report["assessment"]["missing_evidence"]
    assert transition.content["next_state"]["level_inputs"]["rollback"] is False
    assert transition.content["next_state"]["level_inputs"]["rollback_or_stop_condition"] is False


def test_l5_assessment_downgrades_when_loop_evidence_is_missing(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    try:
        assessment = runtime.assess_l5_closed_loop(scope=SCOPE, loop_report={"world_model": {}}, persist=True)
        stored = runtime.store.get_by_id(assessment["persisted_record_id"], scope=SCOPE)
    finally:
        runtime.close()

    assert assessment["ok"] is True
    assert assessment["level"] != "L5"
    assert "roadmap:empty_reference" in assessment["missing_evidence"]
    assert "autonomous_learning:not_complete" in assessment["missing_evidence"]
    assert stored.kind == "l5_assessment"
    assert stored.meta["report_type"] == "l5_assessment"


def test_l5_rejects_nonexistent_structural_ids(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    _seed_current_release(runtime)
    try:
        assessment = runtime.assess_l5_closed_loop(
            scope=SCOPE,
            loop_report={
                "apply": False,
                "world_model": {"persisted_record_id": "missing-world"},
                "roadmap": {"persisted_record_id": "missing-roadmap"},
                "goal_graph": {"persisted_record_id": "missing-goal-graph"},
                "self_continuity": {"persisted_record_id": "missing-continuity"},
                "prompt_safety": {"persisted_record_id": "missing-prompt"},
                "reward": {"transition_record_id": "missing-reward"},
                "autonomous_learning": {
                    "ok": True,
                    "candidate_ids": ["missing-candidate"],
                    "real_task_replay": {
                        "ok": True,
                        "persisted_record_id": "missing-replay",
                        "verdict": "pass",
                        "sample_count": 1,
                        "pass_count": 1,
                        "fail_count": 0,
                        "pass_rate": 1.0,
                    },
                    "blocked_reason": "observation_mode_no_apply",
                },
            },
            persist=True,
            loop_id="forged-evidence",
        )
    finally:
        runtime.close()

    assert assessment["complete"] is False
    assert assessment["level"] != "L5"
    assert "world_model:record_not_found" in assessment["missing_evidence"]
    assert "candidate:record_not_found" in assessment["missing_evidence"]
    assert "replay:record_not_found" in assessment["missing_evidence"]


def test_idle_l5_assessment_does_not_replace_verified_global_readiness(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    verified_report = {
        "apply": False,
        "world_model": {"report_type": "l5_world_model"},
        "roadmap": {"persisted_record_id": "roadmap-verified"},
        "goal_graph": {"persisted_record_id": "goal-graph-verified"},
        "autonomous_learning": {
            "ok": True,
            "candidate_id": "candidate-verified",
            "real_task_replay": {
                "ok": True,
                "verdict": "pass",
                "sample_count": 1,
                "pass_count": 1,
                "fail_count": 0,
                "pass_rate": 1.0,
            },
            "promotion": {
                "promotion_request_id": "promotion-verified",
                "applied": False,
            },
        },
        "reward": {"transition_record_id": "reward-verified"},
        "self_continuity": {"narrative": "Evidence-bound continuity."},
    }
    idle_report = {
        "apply": True,
        "world_model": {"report_type": "l5_world_model"},
        "roadmap": {"persisted_record_id": "roadmap-idle"},
        "goal_graph": {"persisted_record_id": "goal-graph-idle"},
        "autonomous_learning": {
            "ok": True,
            "activity_status": "idle",
            "candidate_count": 0,
            "candidate_ids": [],
            "promotions": [],
        },
        "reward": {"transition_record_id": "reward-idle"},
        "self_continuity": {"narrative": "Evidence-bound continuity."},
    }
    try:
        verified = _run_verified_l5(runtime, loop_id="verified-global-readiness")
        idle = runtime.assess_l5_closed_loop(
            scope=SCOPE,
            loop_report=idle_report,
            persist=True,
            loop_id="idle-activity",
        )
        latest = _latest_l5_assessment(runtime, scope=ScopeRef.from_dict(SCOPE))
    finally:
        runtime.close()

    assert verified["level"] == "L5"
    assert idle["activity_status"] == "idle"
    assert idle["level"] == "L1"
    assert idle["complete"] is False
    assert idle["global_readiness"]["level"] == "L5"
    assert idle["global_readiness"]["record_id"] == verified["persisted_record_id"]
    assert latest["complete"] is True
    assert latest["record_id"] == verified["persisted_record_id"]


def test_idle_l5_assessments_preserve_verified_readiness_beyond_recent_window(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    verified_report = {
        "apply": False,
        "world_model": {"report_type": "l5_world_model"},
        "roadmap": {"persisted_record_id": "roadmap-verified"},
        "goal_graph": {"persisted_record_id": "goal-graph-verified"},
        "autonomous_learning": {
            "ok": True,
            "candidate_id": "candidate-verified",
            "real_task_replay": {
                "ok": True,
                "verdict": "pass",
                "sample_count": 1,
                "pass_count": 1,
                "fail_count": 0,
                "pass_rate": 1.0,
            },
            "promotion": {"promotion_request_id": "promotion-verified", "applied": False},
        },
        "reward": {"transition_record_id": "reward-verified"},
        "self_continuity": {"narrative": "Evidence-bound continuity."},
    }
    try:
        verified = _run_verified_l5(runtime, loop_id="verified-before-long-idle-window")
        for index in range(100):
            runtime.assess_l5_closed_loop(
                scope=SCOPE,
                loop_report={"autonomous_learning": {"activity_status": "idle"}},
                persist=True,
                loop_id=f"idle-window-{index}",
            )
        latest = _latest_l5_assessment(runtime, scope=ScopeRef.from_dict(SCOPE))
    finally:
        runtime.close()

    assert latest["complete"] is True
    assert latest["record_id"] == verified["persisted_record_id"]


def test_idle_l5_assessment_without_verified_history_remains_fail_closed(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    _seed_current_release(runtime)
    try:
        idle = runtime.assess_l5_closed_loop(
            scope=SCOPE,
            loop_report={
                "world_model": {"report_type": "l5_world_model"},
                "roadmap": {"persisted_record_id": "roadmap-idle"},
                "goal_graph": {"persisted_record_id": "goal-graph-idle"},
                "autonomous_learning": {
                    "ok": True,
                    "activity_status": "no_change",
                    "candidate_count": 0,
                    "candidate_ids": [],
                    "promotions": [],
                },
                "reward": {"transition_record_id": "reward-idle"},
                "self_continuity": {"narrative": "Evidence-bound continuity."},
            },
            persist=True,
            loop_id="idle-without-history",
        )
        latest = _latest_l5_assessment(runtime, scope=ScopeRef.from_dict(SCOPE))
    finally:
        runtime.close()

    assert idle["activity_status"] == "idle"
    assert idle["level"] == "L1"
    assert idle["complete"] is False
    assert idle["global_readiness"]["complete"] is False
    assert latest["complete"] is False


def test_idle_l5_assessment_preserves_prior_non_idle_l4_readiness(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    _seed_current_release(runtime)
    try:
        failed = runtime.assess_l5_closed_loop(
            scope=SCOPE,
            loop_report={"world_model": {}},
            persist=True,
            loop_id="failed-before-idle",
        )
        idle = runtime.assess_l5_closed_loop(
            scope=SCOPE,
            loop_report={"autonomous_learning": {"activity_status": "idle"}},
            persist=True,
            loop_id="idle-after-failure",
        )
        latest = _latest_l5_assessment(runtime, scope=ScopeRef.from_dict(SCOPE))
    finally:
        runtime.close()

    assert failed["complete"] is False
    assert idle["global_readiness"]["complete"] is False
    assert idle["global_readiness"]["record_id"] == failed["persisted_record_id"]
    assert latest["record_id"] == failed["persisted_record_id"]


def test_non_idle_failure_replaces_verified_global_readiness(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    verified_report = {
        "world_model": {"report_type": "l5_world_model"},
        "roadmap": {"persisted_record_id": "roadmap-verified"},
        "goal_graph": {"persisted_record_id": "goal-graph-verified"},
        "autonomous_learning": {
            "ok": True,
            "candidate_id": "candidate-verified",
            "real_task_replay": {
                "ok": True,
                "verdict": "pass",
                "sample_count": 1,
                "pass_count": 1,
                "fail_count": 0,
                "pass_rate": 1.0,
            },
            "promotion": {"promotion_request_id": "promotion-verified", "applied": False},
        },
        "reward": {"transition_record_id": "reward-verified"},
        "self_continuity": {"narrative": "Evidence-bound continuity."},
    }
    failed_report = {
        "world_model": {"report_type": "l5_world_model"},
        "roadmap": {"persisted_record_id": "roadmap-failed"},
        "goal_graph": {"persisted_record_id": "goal-graph-failed"},
        "autonomous_learning": {
            "ok": True,
            "activity_status": "active",
            "candidate_count": 0,
            "candidate_ids": [],
            "real_task_replay": {"ok": True, "verdict": "fail", "sample_count": 1},
            "promotions": [],
        },
        "reward": {"transition_record_id": "reward-failed"},
        "self_continuity": {"narrative": "Evidence-bound continuity."},
    }
    try:
        _run_verified_l5(runtime, loop_id="verified-before-failure")
        failed = runtime.assess_l5_closed_loop(
            scope=SCOPE,
            loop_report=failed_report,
            persist=True,
            loop_id="real-failure",
        )
        latest = _latest_l5_assessment(runtime, scope=ScopeRef.from_dict(SCOPE))
    finally:
        runtime.close()

    assert failed["activity_status"] == "active"
    assert failed["complete"] is False
    assert latest["complete"] is False
    assert latest["record_id"] == failed["persisted_record_id"]


def test_l5_assessment_persists_each_snapshot_even_when_loop_id_and_verdict_repeat(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    _seed_current_release(runtime)
    loop_report = {"world_model": {}}
    try:
        first = runtime.assess_l5_closed_loop(
            scope=SCOPE,
            loop_report=loop_report,
            persist=True,
            loop_id="repeated-assessment",
        )
        second = runtime.assess_l5_closed_loop(
            scope=SCOPE,
            loop_report=loop_report,
            persist=True,
            loop_id="repeated-assessment",
        )
        for record_id in (first["persisted_record_id"], second["persisted_record_id"]):
            record = runtime.store.get_by_id(record_id, scope=SCOPE)
            record.time.created_at = "2026-07-13T04:30:00+08:00"
            record.time.updated_at = "2026-07-13T04:30:00+08:00"
            runtime.store.rewrite(record)
        records = runtime.store.list_records(kinds=["l5_assessment"], scope=SCOPE, limit=10)
        latest = _latest_l5_assessment(runtime, scope=ScopeRef.from_dict(SCOPE))
    finally:
        runtime.close()

    assert first["assessment_id"] != second["assessment_id"]
    assert first["persisted_record_id"] != second["persisted_record_id"]
    assert len(records) == 2
    assert latest["assessment_id"] == second["assessment_id"]


def test_latest_l5_assessment_uses_global_sqlite_insert_order_across_runtime_connections(tmp_path) -> None:
    runtimes = [Runtime.create(root=tmp_path), Runtime.create(root=tmp_path)]
    _seed_current_release(runtimes[0])
    runtimes[1]._test_runtime_commit = "a" * 40
    barrier = Barrier(2)

    def write_snapshot(runtime: Runtime, loop_id: str) -> dict:
        barrier.wait()
        return runtime.assess_l5_closed_loop(
            scope=SCOPE,
            loop_report={"world_model": {}},
            persist=True,
            loop_id=loop_id,
        )

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [
                executor.submit(write_snapshot, runtime, f"parallel-{index}")
                for index, runtime in enumerate(runtimes)
            ]
            results = [future.result() for future in futures]
        rows = runtimes[0].store.sqlite.conn.execute(
            "SELECT rowid, record_id FROM records WHERE kind = 'l5_assessment' ORDER BY rowid DESC"
        ).fetchall()
        latest = _latest_l5_assessment(runtimes[0], scope=ScopeRef.from_dict(SCOPE))
    finally:
        for runtime in runtimes:
            runtime.close()

    assert len(results) == 2
    assert len(rows) == 2
    assert rows[0][0] != rows[1][0]
    assert latest["record_id"] == rows[0][1]


def test_cli_l5_assess_returns_json(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("EIMEMORY_ROOT", str(tmp_path))

    exit_code = cli_main(["learn", "l5-assess", "--json"])
    payload = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert payload["report_type"] == "l5_assessment"
    assert payload["consciousness_research_layer"]["boundary"] == "consciousness_like_research_not_verified_agi"
