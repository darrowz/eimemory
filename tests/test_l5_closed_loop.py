from __future__ import annotations

from eimemory.api.runtime import Runtime
from eimemory.governance.rollout_lifecycle import record_lifecycle_event


SCOPE = {"agent_id": "agent-l5-rollback", "workspace_id": "l5-rollback", "user_id": "darrow"}


def test_l5_assessment_imports_only_executed_policy_and_lifecycle_rollback_refs(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    try:
        runtime.upsert_intent_pattern(
            {
                "id": "l5-policy-rollback",
                "pattern": "l5 policy rollback",
                "default_event_type": "repair",
                "interpreted_intent": "prove executed policy rollback evidence",
                "confidence": 0.9,
                "status": "active",
            },
            scope=SCOPE,
        )
        policy = runtime.rollback_intent_pattern(
            "l5-policy-rollback",
            scope=SCOPE,
            reason="executed policy rollback for L5 assessment",
            auto=False,
        )
        lifecycle = record_lifecycle_event(
            runtime,
            scope=SCOPE,
            action_type="rolled_back",
            candidate_id="candidate-lifecycle-rollback",
            commit_sha="a" * 40,
            rollback_command="git reset --hard " + "b" * 40,
            details={"rolled_back": True, "rollback": {"ok": True}},
            reason="executed lifecycle rollback for L5 assessment",
            budget_decision="blocked",
        )
        runtime.store.sqlite.upsert_policy_rollout_ledger_payload(
            {
                "id": "ledger-status-only-rollback",
                "scope": SCOPE,
                "action_type": "rollback",
                "promotion_id": "status-only",
                "budget_decision": "ok",
                "applied_pattern_id": "",
                "details": {"status": "rolled_back"},
            }
        )

        assessment = runtime.assess_l5_closed_loop(
            scope=SCOPE,
            loop_report={
                "apply": True,
                "world_model": {"report_type": "l5_world_model"},
                "rollback_refs": ["caller-claimed-rollback"],
            },
            persist=False,
        )
    finally:
        runtime.close()

    assert policy["ok"] is True
    assert lifecycle["ok"] is True
    assert set(assessment["rollback_refs"]) == {policy["ledger_id"], lifecycle["id"]}
    assert assessment["evidence"]["rollback_refs"] == assessment["rollback_refs"]
    assert "ledger-status-only-rollback" not in assessment["rollback_refs"]
    assert "caller-claimed-rollback" not in assessment["rollback_refs"]
