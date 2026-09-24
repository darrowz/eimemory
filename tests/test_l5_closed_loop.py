from __future__ import annotations

from types import SimpleNamespace

from eimemory.api.runtime import Runtime
from eimemory.governance.l5_loop import _missing_evidence, _prompt_safety_missing_reason, _valid_prompt_safety_record
from eimemory.governance.prompt_safety import PROMPT_SAFETY_CASE_COUNT, PROMPT_SAFETY_MANIFEST_DIGEST
from eimemory.governance.rollout_lifecycle import is_executed_rollback_ledger_record, record_lifecycle_event
from eimemory.models.records import ScopeRef


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
                    "execution_type": "code_patch_rollback",
                    "phase": "verify",
                    "candidate_id": "candidate-lifecycle-rollback",
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


def test_executed_rollback_predicate_rejects_missing_unknown_or_action_incompatible_type() -> None:
    candidate_execution = {
        "ok": True,
        "skipped": False,
        "candidate_id": "candidate-b",
        "file_restore": {"ok": True, "restored_count": 1},
    }
    for execution_type in (None, "unknown_rollback"):
        execution = dict(candidate_execution)
        if execution_type is not None:
            execution["execution_type"] = execution_type
        assert is_executed_rollback_ledger_record(
            {
                "action_type": "rolled_back",
                "source_opportunity_id": "candidate-b",
                "source_opportunity": {"candidate_id": "candidate-b"},
                "details": {"candidate_id": "candidate-b", "rollback": execution},
            }
        ) is False

    assert is_executed_rollback_ledger_record(
        {
            "action_type": "rollback",
            "applied_pattern_id": "pattern-a",
            "rollback_policy_id": "pattern-a",
            "source_opportunity_id": "candidate-b",
            "source_opportunity": {"candidate_id": "candidate-b", "pattern_id": "pattern-a"},
            "budget_decision": "ok",
            "details": {
                "candidate_id": "candidate-b",
                "rollback": {
                    **candidate_execution,
                    "execution_type": "code_patch_rollback",
                },
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
                    "execution_type": "file_restore",
                    "file_restore": {"ok": True, "restored_count": 1},
                },
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
                    "execution_type": "file_restore",
                    "candidate_id": "candidate-b",
                    "file_restore": {"ok": True, "restored_count": 1},
                },
            },
        }
    ) is False
    assert is_executed_rollback_ledger_record(
        {
            "action_type": "rollback",
            "applied_pattern_id": "pattern-a",
            "rollback_policy_id": "pattern-a",
            "source_opportunity": {"pattern_id": "pattern-b"},
            "budget_decision": "ok",
            "details": {
                "rollback": {
                    "ok": True,
                    "skipped": False,
                    "execution_type": "intent_pattern_status_transition",
                    "pattern_id": "pattern-b",
                    "status_transition": {
                        "from": "active",
                        "to": "rolled_back",
                        "pattern_id": "pattern-b",
                    },
                },
            },
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


def _prompt_record(content: dict) -> SimpleNamespace:
    return SimpleNamespace(content=content)


def test_unready_prompt_safety_record_is_named_awaiting_evidence(monkeypatch) -> None:
    record = _prompt_record(
        {
            "ok": True,
            "status": "not_ready",
            "awaiting_evidence": True,
            "complete": False,
            "expected_count": "not-a-count",
        }
    )
    assert _valid_prompt_safety_record(record) is False
    assert _prompt_safety_missing_reason(record) == "prompt_safety:awaiting_evidence"
    assert _prompt_safety_missing_reason(_prompt_record({"status": "failed", "ok": False})) == "prompt_safety:failed"
    assert _prompt_safety_missing_reason(None) == "prompt_safety:invalid_record"
    assert _prompt_safety_missing_reason(
        _prompt_record(
            {
                "ok": True,
                "status": "passed",
                "complete": True,
                "manifest_digest": PROMPT_SAFETY_MANIFEST_DIGEST,
                "expected_count": "bad",
            }
        )
    ) == "prompt_safety:case_count_mismatch"

    monkeypatch.setattr(
        "eimemory.governance.l5_loop.resolve_evidence",
        lambda *_args, **_kwargs: SimpleNamespace(ok=True, reason="ok", record=record),
    )
    missing = _missing_evidence(
        object(),
        ScopeRef(agent_id="hongtu", workspace_id="embodied"),
        {"prompt_safety": {"persisted_record_id": "rec_prompt"}, "apply": False},
        None,
    )
    assert "prompt_safety:awaiting_evidence" in missing


def test_passed_prompt_safety_record_needs_the_full_manifest() -> None:
    cases = [{"passed": True} for _ in range(PROMPT_SAFETY_CASE_COUNT)]
    record = _prompt_record(
        {
            "ok": True,
            "status": "passed",
            "complete": True,
            "manifest_digest": "0" * 64,
            "expected_count": PROMPT_SAFETY_CASE_COUNT,
            "executed_count": PROMPT_SAFETY_CASE_COUNT,
            "case_results": cases,
            "executor_id": "executor",
            "model_id": "model",
        }
    )
    assert _prompt_safety_missing_reason(record) == "prompt_safety:manifest_mismatch"
    record.content["manifest_digest"] = PROMPT_SAFETY_MANIFEST_DIGEST
    assert _valid_prompt_safety_record(record) is True
