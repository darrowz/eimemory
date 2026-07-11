from __future__ import annotations

import json

from eimemory.api.runtime import Runtime
from eimemory.cli.main import main as cli_main
from eimemory.governance.closure_rehearsal import _weak_replay_gate


SCOPE = {"agent_id": "hongtu", "workspace_id": "l5-closure", "user_id": "darrow"}


def test_l5_closure_rehearsal_opens_success_skill_and_rollback_metrics(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    try:
        runtime.run_capability_acceptance(scope=SCOPE, persist=True)
        before = runtime.build_capability_dashboard_metrics(scope=SCOPE, persist=False)
        assert before["metrics"]["task_success_rate"] == 0.0
        assert before["metrics"]["skill_reuse_count"] == 0
        assert before["metrics"]["rollback_count"] == 0

        report = runtime.run_l5_closure_rehearsal(scope=SCOPE, persist=True)

        assert report["ok"] is True
        assert report["closure_complete"] is True
        assert report["blocked_reasons"] == []
        assert report["correction_replay"]["ground_truth_rule_id"]
        assert report["pre_answer_gate"]["matched_rule_count"] == 1
        assert report["weak_capability_replay"]["capabilities"] == [
            "search.discovery",
            "research.synthesis",
            "operations.uumit",
            "device.control",
        ]
        assert report["weak_capability_replay"]["persisted_replay_count"] == 12
        assert report["skill_call"]["ok"] is True
        assert report["skill_call"]["record_id"]
        assert report["rollback"]["status"] in {"rolled_back", "quarantined"}

        metrics = report["capability_dashboard"]["metrics"]
        assert metrics["task_success_rate"] == 0.0
        assert metrics["skill_reuse_count"] >= 1
        assert metrics["rollback_count"] >= 1
        assert report["outcome_trace"]["outcome"]["rehearsal"] is True
        weak_gaps = {
            gap["capability"]
            for gap in report["l5_readiness"]["capability_gaps"]
            if gap["capability"] in {"search.discovery", "research.synthesis", "operations.uumit", "device.control"}
        }
        assert weak_gaps == set()
    finally:
        runtime.close()


def test_l5_closure_rehearsal_fails_closed_without_replay_executor(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    try:
        report = runtime.run_l5_closure_rehearsal(scope=SCOPE, persist=True)

        metrics = runtime.build_capability_dashboard_metrics(scope=SCOPE, persist=False)
    finally:
        runtime.close()

    assert report["ok"] is False
    assert report["closure_complete"] is False
    assert "weak_capability_replay_not_executed" in report["blocked_reasons"]
    assert report["skill_call"]["error"] == "replay_gate_failed"
    assert report["rollback"]["status"] == "not_run"
    assert report["outcome_trace"]["status"] == "not_run"
    assert metrics["metrics"]["task_success_rate"] == 0.0
    assert metrics["metrics"]["skill_reuse_count"] == 0
    assert metrics["metrics"]["rollback_count"] == 0


def test_l5_closure_rehearsal_cli_fails_closed_without_executor(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("EIMEMORY_ROOT", str(tmp_path))

    assert cli_main(["learn", "closure-rehearsal", "--scope-agent", "hongtu", "--scope-workspace", "l5-closure", "--scope-user", "darrow"]) == 1

    output = json.loads(capsys.readouterr().out)
    assert output["ok"] is False
    assert output["pre_answer_gate"]["matched_rule_count"] == 1
    assert output["weak_capability_replay"]["persisted_replay_count"] == 12
    assert output["l5_readiness"]["evidence_counts"]["rollback_or_quarantine"] == 0
    weak_gaps = {
        gap["capability"]
        for gap in output["l5_readiness"]["capability_gaps"]
        if gap["capability"] in {"search.discovery", "research.synthesis", "operations.uumit", "device.control"}
    }
    assert weak_gaps == {"search.discovery", "research.synthesis", "operations.uumit", "device.control"}
    assert output["capability_dashboard"]["metrics"]["skill_reuse_count"] == 0
    assert output["capability_dashboard"]["metrics"]["rollback_count"] == 0


def test_weak_replay_gate_requires_each_named_capability_once() -> None:
    pack = {
        "capability": "search.discovery",
        "cases": [{"case_id": "one", "threshold": 1.0}],
        "case_results": [{"case_id": "one", "verdict": "pass", "hit": True, "observed": "verified"}],
        "pass_rate": 1.0,
    }

    gate = _weak_replay_gate({"ok": True, "packs": [dict(pack) for _ in range(4)]})

    assert gate["ok"] is False
    assert "weak_capability_replay_invalid" in gate["blocked_reasons"]
