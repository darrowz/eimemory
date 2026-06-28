from __future__ import annotations

import json

from eimemory.api.runtime import Runtime
from eimemory.cli.main import main as cli_main
from eimemory.governance.capability_ledger import record_capability_score


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
            "real_task_replay": {"ok": True, "pass_count": 3, "sample_count": 3},
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
    finally:
        runtime.close()

    assert calls["apply"] is True
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


def test_cli_l5_assess_returns_json(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("EIMEMORY_ROOT", str(tmp_path))

    exit_code = cli_main(["learn", "l5-assess", "--json"])
    payload = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert payload["report_type"] == "l5_assessment"
    assert payload["consciousness_research_layer"]["boundary"] == "consciousness_like_research_not_verified_agi"
