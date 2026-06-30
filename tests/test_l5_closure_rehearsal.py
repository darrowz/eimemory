from __future__ import annotations

import json

from eimemory.api.runtime import Runtime
from eimemory.cli.main import main as cli_main


SCOPE = {"agent_id": "hongtu", "workspace_id": "l5-closure", "user_id": "darrow"}


def test_l5_closure_rehearsal_opens_success_skill_and_rollback_metrics(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    try:
        before = runtime.build_capability_dashboard_metrics(scope=SCOPE, persist=False)
        assert before["metrics"]["task_success_rate"] == 0.0
        assert before["metrics"]["skill_reuse_count"] == 0
        assert before["metrics"]["rollback_count"] == 0

        report = runtime.run_l5_closure_rehearsal(scope=SCOPE, persist=True)

        assert report["ok"] is True
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
        assert metrics["task_success_rate"] > 0.0
        assert metrics["skill_reuse_count"] >= 1
        assert metrics["rollback_count"] >= 1
        weak_gaps = {
            gap["capability"]
            for gap in report["l5_readiness"]["capability_gaps"]
            if gap["capability"] in {"search.discovery", "research.synthesis", "operations.uumit", "device.control"}
        }
        assert weak_gaps == {"search.discovery", "research.synthesis", "operations.uumit", "device.control"}
    finally:
        runtime.close()


def test_l5_closure_rehearsal_cli_persists_dashboard_counts(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("EIMEMORY_ROOT", str(tmp_path))

    assert cli_main(["learn", "closure-rehearsal", "--scope-agent", "hongtu", "--scope-workspace", "l5-closure", "--scope-user", "darrow"]) == 0

    output = json.loads(capsys.readouterr().out)
    assert output["ok"] is True
    assert output["pre_answer_gate"]["matched_rule_count"] == 1
    assert output["weak_capability_replay"]["persisted_replay_count"] == 12
    assert output["l5_readiness"]["evidence_counts"]["rollback_or_quarantine"] >= 1
    weak_gaps = {
        gap["capability"]
        for gap in output["l5_readiness"]["capability_gaps"]
        if gap["capability"] in {"search.discovery", "research.synthesis", "operations.uumit", "device.control"}
    }
    assert weak_gaps == {"search.discovery", "research.synthesis", "operations.uumit", "device.control"}
    assert output["capability_dashboard"]["metrics"]["skill_reuse_count"] >= 1
    assert output["capability_dashboard"]["metrics"]["rollback_count"] >= 1
