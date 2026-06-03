from __future__ import annotations

from eimemory.governance.autonomous_learning import _candidate_kind_for_goal, _candidate_patch, choose_candidate_kinds_for_goal


def test_candidate_kinds_include_expected_portfolio_types() -> None:
    assert "tool_route" in choose_candidate_kinds_for_goal({"target_capability": "tool.routing"}, max_candidates=3)
    assert "memory_rule" in choose_candidate_kinds_for_goal({"target_capability": "memory.recall"}, max_candidates=3)
    assert "code_patch" in choose_candidate_kinds_for_goal({"target_capability": "code.implementation"}, max_candidates=3)
    assert "skill_draft" in choose_candidate_kinds_for_goal({"target_capability": "skill.draft"}, max_candidates=3)
    assert "source_policy" in choose_candidate_kinds_for_goal({"target_capability": "knowledge.source", "goal_type": "research"}, max_candidates=3)
    assert "eval_case" in choose_candidate_kinds_for_goal({"target_capability": "tool.routing"}, max_candidates=3)


def test_candidate_kind_compatibility_with_legacy_single_selector() -> None:
    goal = {"target_capability": "tool.routing", "goal_type": "maintenance"}
    portfolio = choose_candidate_kinds_for_goal(goal, max_candidates=2)

    assert _candidate_kind_for_goal(goal) == portfolio[0]


def test_candidate_patch_shapes_differ_by_candidate_kind() -> None:
    goal = {
        "target_capability": "tool.routing",
        "question": "Choose safer execution path and avoid unnecessary actions.",
        "success_criteria": "Replay routing for deterministic behavior.",
        "goal_type": "maintenance",
        "title": "Tool routing safety upgrade",
    }
    replay_dataset = {
        "cases": [
            {
                "query": "Open project notes",
                "expected_text": ["confirm the latest file path first"],
            }
        ]
    }

    eval_case = _candidate_patch(goal, [], candidate_kind="eval_case", replay_dataset=replay_dataset)
    assert eval_case["input"] == "Open project notes"
    assert eval_case["expected"] == "confirm the latest file path first"
    assert eval_case["labels"] == ["tool.routing", "maintenance"]

    sop_draft = _candidate_patch(goal, [], candidate_kind="sop_draft")
    assert "steps" in sop_draft
    assert "success_criteria" in sop_draft
    assert isinstance(sop_draft["steps"], list)

    skill_patch = _candidate_patch(
        {"target_capability": "audio.skill", "goal_type": "maintenance", "title": "Playback skill upgrade", "success_criteria": "Improve playback skill."},
        [],
        candidate_kind="skill_draft",
        replay_dataset=replay_dataset,
    )
    assert "skill_name" in skill_patch
    assert skill_patch["skill_name"] == "audio-skill"
    assert isinstance(skill_patch["triggers"], list)
    assert skill_patch["eval_cases"] == replay_dataset["cases"]

    tool_route = _candidate_patch(goal, [], candidate_kind="tool_route", replay_dataset=replay_dataset)
    assert "pattern" in tool_route
    assert "execution_policy" in tool_route
