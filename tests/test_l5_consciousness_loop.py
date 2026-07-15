from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
from threading import Barrier

from eimemory.api.runtime import Runtime
from eimemory.governance.l5_loop import _has_replay, _weaknesses
from eimemory.governance.l5_readiness import _latest_l5_assessment
from eimemory.cli.main import main as cli_main
from eimemory.governance.capability_ledger import record_capability_score
from eimemory.models.records import RecordEnvelope, ScopeRef


SCOPE = {"agent_id": "agent-l5", "workspace_id": "l5-loop", "user_id": "darrow"}


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
    calls: dict[str, object] = {}

    def fake_autonomous_learning_cycle(**kwargs):
        calls.update(kwargs)
        return {
            "ok": True,
            "loop_id": "auto-loop",
            "candidate_id": "cand-memory",
            "candidate_ids": ["cand-memory"],
            "goal_count": 2,
            "goal_graph": {"persisted_record_id": "goal-graph-record", "root_goal_count": 2},
            "real_task_replay": {"ok": True, "verdict": "pass", "pass_count": 3, "fail_count": 0, "sample_count": 3, "pass_rate": 1.0},
            "replay_gate_passed": True,
            "promotion": {
                "applied": True,
                "promotion_request_id": "promotion-memory",
                "rollout_ledger_id": "rollout-memory",
                "rollback_command": "eimemory learn promote cand-memory --rollback",
            },
            "promotions": [
                {
                    "applied": True,
                    "promotion_request_id": "promotion-memory",
                    "rollout_ledger_id": "rollout-memory",
                    "rollback_command": "eimemory learn promote cand-memory --rollback",
                }
            ],
            "capability_score_id": "cap-score-memory",
            "replay_dataset": {"case_count": 3},
        }

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
    calls: dict[str, object] = {}

    def fake_observation_cycle(**kwargs):
        calls.update(kwargs)
        return {
            "ok": True,
            "loop_id": "observer-loop",
            "candidate_id": "cand-observer",
            "candidate_ids": ["cand-observer"],
            "goal_graph": {"persisted_record_id": "goal-graph-observer"},
            "real_task_replay": {"ok": True, "verdict": "pass", "pass_count": 2, "sample_count": 2, "pass_rate": 1.0},
            "replay_gate_passed": True,
            "promotion": {
                "ok": True,
                "applied": False,
                "promotion_request_id": "promotion-observer",
                "blocked_reason": "observation_mode_no_apply",
            },
            "promotions": [
                {
                    "ok": True,
                    "applied": False,
                    "promotion_request_id": "promotion-observer",
                    "blocked_reason": "observation_mode_no_apply",
                }
            ],
            "capability_score_id": "cap-score-observer",
            "replay_dataset": {"case_count": 2},
        }

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

    def fake_apply_cycle(**_kwargs):
        return {
            "ok": True,
            "loop_id": "apply-missing-rollback",
            "candidate_id": "cand-apply",
            "candidate_ids": ["cand-apply"],
            "goal_graph": {"persisted_record_id": "goal-graph-apply"},
            "real_task_replay": {"ok": True, "pass_count": 1, "sample_count": 1},
            "replay_gate_passed": True,
            "promotion": {
                "ok": False,
                "applied": False,
                "promotion_request_id": "promotion-apply",
                "blocked_reason": "rollback_plan_missing",
            },
            "promotions": [
                {
                    "ok": False,
                    "applied": False,
                    "promotion_request_id": "promotion-apply",
                    "blocked_reason": "rollback_plan_missing",
                }
            ],
            "replay_dataset": {"case_count": 1},
        }

    monkeypatch.setattr(runtime, "run_autonomous_learning_cycle", fake_apply_cycle)
    try:
        report = runtime.run_l5_cycle(scope=SCOPE, apply=True, force=True, max_goals=1, max_promotions=1)
        transition = runtime.store.list_records(kinds=["rl_transition"], scope=SCOPE, limit=1)[0]
    finally:
        runtime.close()

    assert "rollback_or_stop_condition" in report["assessment"]["missing_evidence"]
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
    assert "roadmap" in assessment["missing_evidence"]
    assert "autonomous_learning" in assessment["missing_evidence"]
    assert stored.kind == "l5_assessment"
    assert stored.meta["report_type"] == "l5_assessment"


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
        verified = runtime.assess_l5_closed_loop(
            scope=SCOPE,
            loop_report=verified_report,
            persist=True,
            loop_id="verified-global-readiness",
        )
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
    assert idle["level"] == "L4"
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
        verified = runtime.assess_l5_closed_loop(
            scope=SCOPE,
            loop_report=verified_report,
            persist=True,
            loop_id="verified-before-long-idle-window",
        )
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
    assert idle["level"] == "L4"
    assert idle["complete"] is False
    assert idle["global_readiness"]["complete"] is False
    assert latest["complete"] is False


def test_idle_l5_assessment_preserves_prior_non_idle_l4_readiness(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
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
        runtime.assess_l5_closed_loop(
            scope=SCOPE,
            loop_report=verified_report,
            persist=True,
            loop_id="verified-before-failure",
        )
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
