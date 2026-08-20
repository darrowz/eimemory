from __future__ import annotations

from eimemory.api.runtime import Runtime
from eimemory.governance.curiosity import generate_learning_goals
from eimemory.governance.goal_registry import load_goal_registry
from eimemory.governance.thoughts import generate_thoughts, promote_thoughts_to_goals


def test_goal_registry_drives_learning_goals() -> None:
    # This assertion covers the retired bundled registry, so selecting it is
    # explicit and its targets are admitted through the caller's capability
    # view rather than revived as a default vocabulary.
    registry = load_goal_registry(legacy_compatibility=True)
    capability_ids = sorted(
        {
            str(capability)
            for goal in registry["long_term"]
            for capability in goal.get("sub_capabilities") or []
        }
    )

    goals = generate_learning_goals(
        {
            "weaknesses": [],
            "metrics": {"replay_pass_rate": 1.0},
            "capabilities": [{"capability": capability} for capability in capability_ids],
        },
        [],
        goal_registry=registry,
        max_goals=2,
    )

    assert goals
    assert goals[0]["source_type"] == "goal_registry"
    assert goals[0]["goal_type"] == "long_term"


def test_thought_queue_persists_merges_and_promotes_to_goal(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    scope = {"agent_id": "hongtu"}
    signal = {
        "record_id": "sig-1",
        "signal_type": "outcome_weakness",
        "summary": "User correction says playback requests need physical output verification.",
        "target_capability": "device.control",
        "impact": 0.8,
        "urgency": 0.7,
        "confidence": 0.9,
        "evidence_tier": "T0",
    }

    first = generate_thoughts(
        runtime,
        signals=[signal],
        self_model={},
        goals=[],
        scope=scope,
        loop_id="loop-1",
        persist=True,
        legacy_compatibility=True,
    )
    second = generate_thoughts(
        runtime,
        signals=[signal],
        self_model={},
        goals=[],
        scope=scope,
        loop_id="loop-2",
        persist=True,
        legacy_compatibility=True,
    )
    thoughts = second["thoughts"]
    promoted = promote_thoughts_to_goals(thoughts, limit=1)

    assert first["thought_count"] == 1
    assert second["thought_count"] == 1
    assert thoughts[0]["repeat_count"] >= 2
    assert promoted[0]["goal_type"] == "proactive_thought"
    assert promoted[0]["target_capability"] == "device.control"
