from __future__ import annotations

import sys

from eimemory.api.runtime import Runtime
from eimemory.governance.autonomous_learning import run_autonomous_learning_cycle


def test_autonomous_learning_cycle_produces_goal_candidate_and_ledger(tmp_path, monkeypatch) -> None:
    runtime = Runtime.create(root=tmp_path)
    scope = {"agent_id": "hongtu", "workspace_id": "personal"}
    runtime.evolution.log_reflection(
        tag="tool.routing",
        miss="Repeated unnecessary web searches",
        fix="Use memory-first routing for stable personal facts",
        scope=scope,
    )
    _force_real_task_replay_pass(runtime, monkeypatch)

    report = run_autonomous_learning_cycle(runtime, scope=scope, apply=False, force=True)

    assert report["ok"] is True
    assert report["goal_count"] >= 1
    assert report["selected_goal_id"]
    assert report["research_note_id"]
    assert report["experiment_id"]
    assert report["candidate_id"]
    assert report["promotion"]["applied"] is False
    assert report["capability_score_id"]


def test_autonomous_learning_cycle_distills_diverse_capability_candidates(tmp_path, monkeypatch) -> None:
    runtime = Runtime.create(root=tmp_path)
    scope = {"agent_id": "hongtu", "workspace_id": "personal"}
    _force_real_task_replay_pass(runtime, monkeypatch)
    goals = [
        {
            "goal_type": "capability_gap",
            "title": "Fix empty patch generation",
            "question": "Avoid promoting code patches without file updates.",
            "success_criteria": "Empty code patch output becomes a SOP or eval asset.",
            "authority_tier": "L1",
            "priority": 0.95,
            "target_capability": "code.implementation",
            "semantic_key": "test-code",
        },
        {
            "goal_type": "capability_gap",
            "title": "Improve memory recall",
            "question": "Recall exact deployed version evidence before answering.",
            "success_criteria": "Version answers cite current evidence.",
            "authority_tier": "L1",
            "priority": 0.9,
            "target_capability": "memory.recall",
            "semantic_key": "test-memory",
        },
        {
            "goal_type": "capability_gap",
            "title": "Improve tool routing",
            "question": "Route status questions to health and ledger tools first.",
            "success_criteria": "Routing decisions use query-first evidence.",
            "authority_tier": "L1",
            "priority": 0.85,
            "target_capability": "tool.routing",
            "semantic_key": "test-tool",
        },
        {
            "goal_type": "capability_gap",
            "title": "Improve knowledge intake",
            "question": "Score external sources and keep weak web learning as summary.",
            "success_criteria": "Knowledge intake lands as source score or summary.",
            "authority_tier": "L1",
            "priority": 0.8,
            "target_capability": "knowledge.intake",
            "semantic_key": "test-knowledge",
        },
        {
            "goal_type": "capability_gap",
            "title": "Improve proactive judgment",
            "question": "Notice low-yield learn-watch runs and suggest cheaper cadence.",
            "success_criteria": "Proactive judgments become a concrete SOP/eval asset.",
            "authority_tier": "L1",
            "priority": 0.75,
            "target_capability": "proactive.judgment",
            "semantic_key": "test-proactive",
        },
    ]
    monkeypatch.setattr("eimemory.governance.autonomous_learning.generate_learning_goals", lambda *_args, **_kwargs: goals)

    report = run_autonomous_learning_cycle(runtime, scope=scope, apply=False, force=True, max_goals=5, allow_network=False)
    candidates = [runtime.store.get_by_id(candidate_id, scope=scope) for candidate_id in report["candidate_ids"]]
    candidate_capabilities = {candidate.meta["target_capability"] for candidate in candidates if candidate is not None}
    promotion_targets = {candidate.meta["promotion_target"] for candidate in candidates if candidate is not None}

    assert {"memory.recall", "tool.routing", "knowledge.intake", "proactive.judgment"}.issubset(candidate_capabilities)
    assert "code_patch" not in promotion_targets
    assert {"memory_rule", "tool_route", "source_policy", "sop_draft"}.issubset(promotion_targets)


def test_autonomous_learning_cycle_applies_supported_policy_adapter(tmp_path, monkeypatch) -> None:
    runtime = Runtime.create(root=tmp_path)
    scope = {"agent_id": "hongtu", "workspace_id": "personal"}
    runtime.evolution.log_reflection(
        tag="tool.routing",
        miss="Repeated unnecessary web searches",
        fix="Use memory-first routing for stable personal facts",
        scope=scope,
    )
    _force_real_task_replay_pass(runtime, monkeypatch)

    report = run_autonomous_learning_cycle(runtime, scope=scope, apply=True, force=True)

    assert report["ok"] is True
    assert report["promotion"]["applied"] is True
    assert report["promotion"]["post_promotion_status"] in {"promoted", "shadow_observe"}
    assert runtime.store.get_by_id(report["candidate_id"]).status in {"promoted", "shadow_observe"}
    assert report["promotion"]["applied_artifact_ids"]


def test_autonomous_learning_cycle_applies_code_patch_directly_to_repo(tmp_path, monkeypatch) -> None:
    runtime = Runtime.create(root=tmp_path)
    scope = {"agent_id": "hongtu", "workspace_id": "personal"}
    repo = tmp_path / "repo"
    repo.mkdir()
    target = repo / "module.py"
    target.write_text("VALUE = 'broken'\n", encoding="utf-8")
    monkeypatch.setenv("EIMEMORY_AUTONOMOUS_CODE_REPO", str(repo))
    monkeypatch.setenv("EIMEMORY_AUTONOMOUS_CODE_DEPLOY", "0")
    _force_real_task_replay_pass(
        runtime,
        monkeypatch,
        code_patch={
            "summary": "Fix broken module value",
            "repo_root": str(repo),
            "allowed_files": ["module.py"],
            "file_updates": [{"path": "module.py", "content": "VALUE = 'fixed'\n"}],
            "verification_commands": [
                [
                    sys.executable,
                    "-c",
                    "from pathlib import Path; assert Path('module.py').read_text(encoding='utf-8') == \"VALUE = 'fixed'\\n\"",
                ]
            ],
            "commit_to_repo": False,
        },
    )
    runtime.evolution.log_reflection(
        tag="code.implementation",
        miss="A code path failed tests",
        fix="Generate a code patch candidate with replay evidence",
        scope=scope,
    )

    report = run_autonomous_learning_cycle(runtime, scope=scope, apply=True, force=True)

    assert report["ok"] is True
    assert report["promotion"]["applied"] is True
    assert report["promotion"]["side_effect"]["adapter"] == "direct_repo_patch"
    assert report["promotion"]["side_effect"]["repo_mutated"] is True
    assert report["promotion"]["side_effect"]["production_applied"] is False
    assert target.read_text(encoding="utf-8") == "VALUE = 'fixed'\n"
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


def _force_real_task_replay_pass(runtime: Runtime, monkeypatch, code_patch: dict | None = None) -> None:
    def fake_build_replay_dataset(
        _runtime,
        *,
        scope,
        limit=50,
        persist=True,
        loop_id="",
        include_built_in_regressions=False,
    ):
        return {
            "ok": True,
            "schema_version": "real_task_replay.v1",
            "report_type": "proactive_replay_dataset",
            "case_count": 2,
            "correction_count": 1,
            "persisted_record_id": "replay_dataset_record",
            "cases": [
                {
                    "case_id": "case_1",
                    "query": "prefer memory-first routing",
                    "task_type": "tool.routing",
                    "target_capability": "tool.routing",
                    "expected_text": ["memory-first"],
                },
                {
                    "case_id": "case_2",
                    "query": "generate safe code patch",
                    "task_type": "code.implementation",
                    "target_capability": "code.implementation",
                    "expected_text": ["replay evidence"],
                    **({"code_patch": code_patch} if code_patch else {}),
                },
            ],
        }

    def fake_run_real_task_replay(dataset, *, seed=False, persist_report=False):
        return {
            "ok": True,
            "report_type": "real_task_replay",
            "schema_version": "real_task_replay.v1",
            "verdict": "pass",
            "pass_rate": 1.0,
            "threshold": dataset.get("threshold", 0.6),
            "sample_count": len(dataset.get("cases") or []),
            "pass_count": len(dataset.get("cases") or []),
            "fail_count": 0,
        }

    monkeypatch.setattr("eimemory.governance.autonomous_learning.build_replay_dataset", fake_build_replay_dataset)
    monkeypatch.setattr(runtime, "run_real_task_replay", fake_run_real_task_replay)
