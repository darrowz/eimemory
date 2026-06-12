from __future__ import annotations

from eimemory.api.runtime import Runtime
from eimemory.governance.capability_ledger import record_capability_score
from eimemory.models.records import RecordEnvelope, ScopeRef


SCOPE = {"agent_id": "hongtu", "workspace_id": "autonomy-goals"}


def test_seeded_default_capabilities_produce_goals_when_ledger_is_empty(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)

    queue = runtime.build_autonomy_goal_queue(scope=SCOPE)

    assert queue["goal_count"] == 5
    assert 1 <= queue["selected_count"] <= 3
    assert queue["persisted_record_id"] == ""
    assert queue["generated_at"]
    assert {goal["capability"] for goal in queue["goals"]}.issubset(
        {
            "operations.uumit",
            "search.discovery",
            "research.synthesis",
            "office.daily_task",
            "device.control",
        }
    )
    assert all(goal["explanation"] for goal in queue["goals"])


def test_low_evidence_high_failure_capability_ranks_above_healthy_capability(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    record_capability_score(
        runtime,
        scope=SCOPE,
        loop_id="healthy",
        capability="office.daily_task",
        score=0.94,
        evidence_record_ids=["office-1", "office-2", "office-3", "office-4", "office-5"],
    )
    record_capability_score(
        runtime,
        scope=SCOPE,
        loop_id="weak",
        capability="search.discovery",
        score=0.22,
        evidence_record_ids=[],
        regression_count=2,
    )
    _append_signal(runtime, "incident", capability="search.discovery", verdict="fail")
    _append_signal(runtime, "replay_result", capability="search.discovery", verdict="fail")
    _append_signal(runtime, "learning_eval", capability="search.discovery", verdict="fail")

    queue = runtime.build_autonomy_goal_queue(
        scope=SCOPE,
        max_goals=2,
        capabilities=["search.discovery", "office.daily_task"],
    )

    assert queue["goals"][0]["capability"] == "search.discovery"
    assert queue["goals"][0]["scoring_factors"]["failure_frequency"] > queue["goals"][-1]["scoring_factors"]["failure_frequency"]
    assert queue["goals"][0]["scoring_factors"]["evidence_gap"] > queue["goals"][-1]["scoring_factors"]["evidence_gap"]


def test_max_goals_bounds_output_to_one_to_three(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)

    low = runtime.build_autonomy_goal_queue(scope=SCOPE, max_goals=0)
    high = runtime.build_autonomy_goal_queue(scope=SCOPE, max_goals=10)

    assert low["selected_count"] == 1
    assert len(low["goals"]) == 1
    assert high["selected_count"] == 3
    assert len(high["goals"]) == 3


def test_persist_writes_autonomy_goal_queue_record(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)

    queue = runtime.build_autonomy_goal_queue(scope=SCOPE, persist=True, max_goals=2)

    assert queue["persisted_record_id"]
    records = runtime.store.list_records(kinds=["autonomy_goal_queue"], scope=SCOPE, limit=10)
    assert len(records) == 1
    record = records[0]
    assert record.record_id == queue["persisted_record_id"]
    assert record.kind == "autonomy_goal_queue"
    assert record.status == "active"
    assert record.source == "eimemory.autonomy_goal_queue"
    assert record.content["goals"] == queue["goals"]
    assert record.meta["selected_count"] == 2
    assert record.meta["scoring_factors"] == ["user_value", "failure_frequency", "potential_gain", "risk", "evidence_gap"]


def _append_signal(runtime: Runtime, kind: str, *, capability: str, verdict: str) -> RecordEnvelope:
    return runtime.store.append(
        RecordEnvelope.create(
            kind=kind,
            title=f"{capability} {kind}",
            summary=f"{capability} {verdict}",
            scope=ScopeRef.from_dict(SCOPE),
            source="test.autonomy_goal_queue",
            content={"capability": capability, "verdict": verdict},
            meta={"capability": capability, "target_capability": capability, "verdict": verdict},
        )
    )
