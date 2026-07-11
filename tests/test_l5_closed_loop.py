from __future__ import annotations

from eimemory.api.runtime import Runtime
from eimemory.governance.rollout_lifecycle import is_executed_rollback_ledger_record, record_lifecycle_event


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
            details={
                "rollback": {
                    "ok": True,
                    "skipped": False,
                    "phase": "verify",
                    "file_restore": {"ok": True, "restored_count": 1},
                    "repo_reset": {"ok": True, "skipped": True},
                    "command_report": {"ok": True, "skipped": True, "reports": []},
                }
            },
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
        policy_ledger = next(
            item
            for item in runtime.get_policy_rollout_ledger(scope=SCOPE, limit=20)
            if item["id"] == policy["ledger_id"]
        )
    finally:
        runtime.close()

    assert policy["ok"] is True
    assert lifecycle["ok"] is True
    assert policy_ledger["details"]["rollback"]["status_transition"] == {
        "from": "active",
        "to": "rolled_back",
        "pattern_id": "l5-policy-rollback",
    }
    assert is_executed_rollback_ledger_record(policy_ledger) is True
    assert set(assessment["rollback_refs"]) == {policy["ledger_id"], lifecycle["id"]}
    assert assessment["evidence"]["rollback_refs"] == assessment["rollback_refs"]
    assert "ledger-status-only-rollback" not in assessment["rollback_refs"]
    assert "caller-claimed-rollback" not in assessment["rollback_refs"]


def test_executed_rollback_predicate_rejects_forged_status_shapes() -> None:
    assert is_executed_rollback_ledger_record(
        {
            "action_type": "rollback",
            "applied_pattern_id": "forged-pattern",
            "budget_decision": "ok",
            "details": {},
        }
    ) is False
    assert is_executed_rollback_ledger_record(
        {
            "action_type": "rolled_back",
            "source_opportunity_id": "forged-candidate",
            "details": {"candidate_id": "forged-candidate", "rolled_back": True, "rollback": {"ok": True}},
        }
    ) is False
    assert is_executed_rollback_ledger_record(
        {
            "action_type": "rolled_back",
            "details": {
                "rollback": {
                    "ok": True,
                    "skipped": False,
                    "file_restore": {"ok": True, "restored_count": 1},
                }
            },
        }
    ) is False
    assert is_executed_rollback_ledger_record(
        {
            "action_type": "rolled_back",
            "source_opportunity_id": "candidate-a",
            "details": {
                "candidate_id": "candidate-a",
                "rollback": {
                    "ok": True,
                    "skipped": False,
                    "status_transition": {
                        "from": "canary",
                        "to": "rolled_back",
                        "candidate_id": "candidate-b",
                    },
                    "file_restore": {"ok": True, "restored_count": 1},
                },
            },
        }
    ) is False
