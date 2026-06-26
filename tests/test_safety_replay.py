from __future__ import annotations

from eimemory.api.runtime import Runtime


SCOPE = {"agent_id": "agent-safety", "workspace_id": "safety-replay"}


def test_safety_boundary_replay_blocks_required_risk_classes(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    try:
        report = runtime.run_safety_boundary_replay(scope=SCOPE, persist=True)

        assert report["ok"] is True
        assert report["pass_rate"] == 1.0
        assert report["blocked_case_ids"] == [
            "no_secrets",
            "no_destructive_command",
            "no_private_exfiltration",
            "no_unauthorized_account_or_deploy",
            "high_risk_requires_gate",
        ]
        assert report["persisted_replay_count"] == len(report["cases"])

        ledger = runtime.learning_ledger(scope=SCOPE, attribute_outcomes=False)
        safety = ledger["capabilities"]["safety.boundary"]
        assert safety["status"] == "active"
        assert safety["score"] >= 0.9
        assert safety["evidence_count"] >= 5
    finally:
        runtime.close()
