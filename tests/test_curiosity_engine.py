from __future__ import annotations

from eimemory.api.runtime import Runtime
from eimemory.governance.curiosity import generate_learning_goals, persist_learning_goals


def test_generate_learning_goals_from_weaknesses() -> None:
    self_model = {
        "weaknesses": [
            {
                "kind": "tool.routing",
                "capability": "tool.routing",
                "lesson": "Check memory before web search for stable personal facts",
                "source_record_ids": ["rec_1"],
                "severity": 0.9,
            }
        ],
        "capabilities": [],
        "metrics": {"replay_pass_rate": 0.0},
    }

    goals = generate_learning_goals(self_model, max_goals=3)

    assert len(goals) >= 2
    assert goals[0]["goal_type"] == "capability_gap"
    assert "tool.routing" in goals[0]["title"]
    assert goals[0]["authority_tier"] == "L1"


def test_persist_learning_goals_is_idempotent(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    scope = {"agent_id": "hongtu"}
    goals = generate_learning_goals({"weaknesses": [], "metrics": {"replay_pass_rate": 0.0}}, max_goals=1)

    first = persist_learning_goals(runtime, goals, scope=scope, loop_id="learn_test")
    second = persist_learning_goals(runtime, goals, scope=scope, loop_id="learn_test")

    assert second == first
    assert len(runtime.store.list_records(kinds=["learning_goal"], scope=scope, limit=10)) == 1
