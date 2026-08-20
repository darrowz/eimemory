from __future__ import annotations

from eimemory.api.runtime import Runtime
from eimemory.governance.autonomous_learning import (
    _candidate_kind_for_goal,
    _candidate_patch,
    _candidate_specs_for_goals,
    _resolved_candidate_kind_and_patch,
    choose_candidate_kinds_for_goal,
)


def _enable_local_code_apply(monkeypatch) -> None:
    monkeypatch.setenv(
        "EIMEMORY_CODE_AUTOMATION_POLICY_JSON",
        '{"schema_version":"code_automation_policy.v1","policy_id":"test-local-apply","actions":{"local_apply":true,"commit":false,"deployment":false}}',
    )


def test_candidate_kinds_include_expected_portfolio_types() -> None:
    assert "tool_route" in choose_candidate_kinds_for_goal({"target_capability": "tool.routing"}, max_candidates=3, legacy_compatibility=True)
    assert "memory_rule" in choose_candidate_kinds_for_goal({"target_capability": "memory.recall"}, max_candidates=3, legacy_compatibility=True)
    assert "code_patch" in choose_candidate_kinds_for_goal({"target_capability": "code.implementation"}, max_candidates=3, legacy_compatibility=True)
    assert "skill_draft" in choose_candidate_kinds_for_goal({"target_capability": "skill.draft"}, max_candidates=3, legacy_compatibility=True)
    assert "source_policy" in choose_candidate_kinds_for_goal({"target_capability": "knowledge.source", "goal_type": "research"}, max_candidates=3, legacy_compatibility=True)
    assert "eval_case" in choose_candidate_kinds_for_goal({"target_capability": "tool.routing"}, max_candidates=3, legacy_compatibility=True)


def test_candidate_kind_compatibility_with_legacy_single_selector() -> None:
    goal = {"target_capability": "tool.routing", "goal_type": "maintenance"}
    portfolio = choose_candidate_kinds_for_goal(goal, max_candidates=2, legacy_compatibility=True)

    assert _candidate_kind_for_goal(goal, legacy_compatibility=True) == portfolio[0]


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


def test_all_actionable_candidate_patches_have_trigger_action_verification_and_rollback() -> None:
    goal = {
        "target_capability": "operations.uumit",
        "goal_type": "capability_gap",
        "title": "Close UUMit operations loop",
        "success_criteria": "Inspect current evidence, act, verify, and rollback safely.",
    }
    replay_dataset = {"cases": [{"case_id": "uumit-ops", "query": "uumit delivery issue"}]}

    for kind in ["sop_draft", "tool_route", "source_policy", "skill_draft", "memory_rule"]:
        patch = _candidate_patch(goal, [], candidate_kind=kind, replay_dataset=replay_dataset)
        assert patch["trigger_condition"]
        assert patch["action"]
        assert patch["verification"]
        assert patch["rollback"]


def test_missing_code_proposer_is_explicitly_blocked_without_sop_fallback(monkeypatch) -> None:
    _enable_local_code_apply(monkeypatch)
    kind, patch = _resolved_candidate_kind_and_patch(
        {
            "target_capability": "code.implementation",
            "goal_type": "bugfix",
            "title": "Empty generated code patch",
            "success_criteria": "Must produce structured file updates.",
        },
        [],
        candidate_kind="code_patch",
        replay_dataset={"cases": []},
        legacy_compatibility=True,
    )

    assert kind == "code_patch"
    assert patch["proposal_status"] == "proposal_unavailable"
    assert patch["proposal_blocked_reason"] == "code_proposer_unavailable"
    assert patch["promotion_ready"] is False
    assert "code_proposer_unavailable" in patch["blocked_reasons"]


def test_empty_code_patch_is_retained_as_blocked_code_candidate(monkeypatch) -> None:
    _enable_local_code_apply(monkeypatch)
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
        legacy_compatibility=True,
    )

    assert kind == "code_patch"
    assert patch["proposal_status"] == "proposal_unavailable"
    assert patch["proposal_blocked_reason"] == "code_proposer_unavailable"
    assert patch["file_updates"] == []


def test_code_goal_uses_injected_proposer_to_emit_reviewable_diff(tmp_path, monkeypatch) -> None:
    _enable_local_code_apply(monkeypatch)
    runtime = Runtime.create(root=tmp_path / "runtime")
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "module.py").write_text("VALUE = 'old'\n", encoding="utf-8")
    try:
        runtime.code_patch_proposer = lambda **_kwargs: {
            "allowed_files": ["module.py"],
            "file_updates": [{"path": "module.py", "content": "VALUE = 'new'\n"}],
            "verification_commands": [["python", "-m", "compileall", "module.py"]],
        }
        kind, patch = _resolved_candidate_kind_and_patch(
            {
                "target_capability": "code.implementation",
                "goal_type": "bugfix",
                "title": "Repair module value",
                "question": "Repair the detected value regression.",
                "success_criteria": "The module reports the corrected value.",
                "semantic_key": "code-goal-1",
                "repo_root": str(repo),
                "files": ["module.py"],
            },
            [],
            candidate_kind="code_patch",
            replay_dataset={"cases": [{"case_id": "code-case-1", "query": "repair module"}]},
            runtime=runtime,
            scope={"agent_id": "tests"},
            legacy_compatibility=True,
        )

        assert kind == "code_patch"
        assert patch["proposal_status"] == "proposal_ready"
        assert patch["authorization_mode"] == "machine_gated"
        assert patch["machine_policy_status"] == "authorized"
        assert "requires_human_approval" not in patch
        assert "approval_status" not in patch
        assert patch["file_updates"] == [{"path": "module.py", "content": "VALUE = 'new'\n"}]
        assert "-VALUE = 'old'" in patch["unified_diff"]
        assert "+VALUE = 'new'" in patch["unified_diff"]
        assert patch["patch_digest"]
        assert patch["subject_state_digest"]
        assert patch["deploy_to_production"] is False
        assert patch["commit_to_repo"] is False
    finally:
        runtime.close()


def test_candidate_specs_cover_diverse_capability_goals() -> None:
    goals = [
        {"target_capability": "code.implementation", "title": "Fix code", "goal_type": "capability_gap"},
        {"target_capability": "memory.recall", "title": "Improve recall", "goal_type": "capability_gap"},
        {"target_capability": "tool.routing", "title": "Improve routing", "goal_type": "capability_gap"},
        {"target_capability": "knowledge.intake", "title": "Improve intake", "goal_type": "capability_gap"},
        {"target_capability": "proactive.judgment", "title": "Improve proactive judgment", "goal_type": "capability_gap"},
    ]

    specs = _candidate_specs_for_goals(
        goals,
        max_goals=5,
        max_candidates_per_goal=1,
        replay_dataset={},
        legacy_compatibility=True,
    )
    capabilities = [spec["target_capability"] for spec in specs]
    targets = {spec["promotion_target"] for spec in specs}

    assert capabilities[:5] == [
        "code.implementation",
        "memory.recall",
        "tool.routing",
        "knowledge.intake",
        "proactive.judgment",
    ]
    assert "code_patch" in targets
    assert {"memory_rule", "tool_route", "source_policy", "sop_draft"}.issubset(targets)
