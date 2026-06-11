from __future__ import annotations

from eimemory.api.runtime import Runtime
from eimemory.governance.autonomous_learning import run_autonomous_learning_cycle


def test_autonomous_learning_cycle_produces_goal_candidate_and_ledger(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    scope = {"agent_id": "hongtu", "workspace_id": "personal"}
    runtime.evolution.log_reflection(
        tag="tool.routing",
        miss="Repeated unnecessary web searches",
        fix="Use memory-first routing for stable personal facts",
        scope=scope,
    )

    report = run_autonomous_learning_cycle(runtime, scope=scope, apply=False, force=True)

    assert report["ok"] is True
    assert report["goal_count"] >= 1
    assert report["selected_goal_id"]
    assert report["research_note_id"]
    assert report["experiment_id"]
    assert report["candidate_id"]
    assert report["promotion"]["applied"] is False
    assert report["capability_score_id"]


def test_autonomous_learning_cycle_applies_supported_policy_adapter(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    scope = {"agent_id": "hongtu", "workspace_id": "personal"}
    runtime.evolution.log_reflection(
        tag="tool.routing",
        miss="Repeated unnecessary web searches",
        fix="Use memory-first routing for stable personal facts",
        scope=scope,
    )

    report = run_autonomous_learning_cycle(runtime, scope=scope, apply=True, force=True)

    assert report["ok"] is True
    assert report["promotion"]["applied"] is True
    assert report["promotion"]["post_promotion_status"] == "shadow_observe"
    assert runtime.store.get_by_id(report["candidate_id"]).status == "shadow_observe"
    assert report["promotion"]["applied_artifact_ids"]


def test_autonomous_learning_cycle_promotes_code_patch_as_reviewable_asset(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    scope = {"agent_id": "hongtu", "workspace_id": "personal"}
    runtime.evolution.log_reflection(
        tag="code.implementation",
        miss="A code path failed tests",
        fix="Generate a code patch candidate with replay evidence",
        scope=scope,
    )

    report = run_autonomous_learning_cycle(runtime, scope=scope, apply=True, force=True)

    assert report["ok"] is True
    assert report["promotion"]["applied"] is True
    assert report["promotion"]["side_effect"]["adapter"] == "reviewable_code_patch"
    assert report["promotion"]["side_effect"]["production_applied"] is False
    assert runtime.store.get_by_id(report["candidate_id"]).status == "promoted"


def test_autonomous_learning_cycle_dry_run_does_not_persist_learning_records(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    scope = {"agent_id": "hongtu", "workspace_id": "personal"}
    runtime.evolution.log_reflection(tag="tool.routing", miss="Bad routing", fix="Memory first", scope=scope)

    report = run_autonomous_learning_cycle(runtime, scope=scope, dry_run=True)

    assert report["ok"] is True
    assert report["dry_run"] is True
    assert report["candidate_preview"]
    assert runtime.store.list_records(kinds=["learning_loop"], scope=scope, limit=10) == []
    assert runtime.store.list_records(kinds=["capability_candidate"], scope=scope, limit=10) == []
