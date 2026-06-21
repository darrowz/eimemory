from __future__ import annotations

from eimemory.governance.autonomous_learning import (
    _candidate_kind_for_goal,
    _candidate_patch,
    _candidate_specs_for_goals,
    _resolved_candidate_kind_and_patch,
    choose_candidate_kinds_for_goal,
)


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


def test_empty_code_patch_downgrades_to_sop_candidate() -> None:
    goal = {
        "target_capability": "code.implementation",
        "question": "Fix the broken implementation path without guessing.",
        "success_criteria": "The next artifact must be replayable.",
        "patch": {"summary": "empty generator output", "file_updates": []},
    }

    kind, patch = _resolved_candidate_kind_and_patch(
        goal,
        [],
        candidate_kind="code_patch",
        replay_dataset={"cases": [{"case_id": "case-empty-patch", "query": "fix code"}]},
    )

    assert kind == "sop_draft"
    assert patch["fallback_from"] == "code_patch"
    assert patch["fallback_reason"] == "code_patch_missing_file_updates"
    assert "file_updates" not in patch


def test_candidate_specs_cover_diverse_capability_goals() -> None:
    goals = [
        {"target_capability": "code.implementation", "title": "Fix code", "goal_type": "capability_gap"},
        {"target_capability": "memory.recall", "title": "Improve recall", "goal_type": "capability_gap"},
        {"target_capability": "tool.routing", "title": "Improve routing", "goal_type": "capability_gap"},
        {"target_capability": "knowledge.intake", "title": "Improve intake", "goal_type": "capability_gap"},
        {"target_capability": "proactive.judgment", "title": "Improve proactive judgment", "goal_type": "capability_gap"},
    ]

    specs = _candidate_specs_for_goals(goals, max_goals=5, max_candidates_per_goal=1, replay_dataset={})
    capabilities = [spec["target_capability"] for spec in specs]
    targets = {spec["promotion_target"] for spec in specs}

    assert capabilities[:5] == [
        "code.implementation",
        "memory.recall",
        "tool.routing",
        "knowledge.intake",
        "proactive.judgment",
    ]
    assert {"memory_rule", "tool_route", "source_policy", "sop_draft"}.issubset(targets)
