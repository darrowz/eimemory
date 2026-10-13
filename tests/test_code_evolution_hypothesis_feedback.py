"""Native strict proposal/candidate/ledger feedback; external effects isolated."""
from copy import deepcopy
from dataclasses import asdict, replace
import json

import pytest

from eimemory.contracts.release_identity import ReleaseIdentity
from eimemory.governance.capability.capability_hypotheses import (
    hypothesis_behavior_gate, record_hypothesis_evaluation_artifact, record_hypothesis_experiment_feedback,
)
from eimemory.governance.capability.capability_acceptance import run_capability_acceptance
from eimemory.governance.evolution import autonomous_evolution, dynamic_capability_evolution
from eimemory.governance.evolution.code_evolution_effects import CodeEvolutionEffectOwner
from eimemory.governance.evolution.code_evolution_hypothesis_feedback import (
    SCHEMA, record_transaction_hypothesis_feedback,
)
from eimemory.governance.evolution.code_evolution_transaction import CodeEvolutionTransactionManager
from eimemory.governance.promotion.promotion_watch import resume_code_evolution_transactions
from test_dynamic_code_patch_context import SCOPE, PROFILE, native_gap, proposal


@pytest.fixture
def strict_candidate(native_gap, monkeypatch):
    native = native_gap
    monkeypatch.setattr("eimemory.governance.release.evidence_contract.current_release_identity",
        lambda *_args, **_kwargs: ReleaseIdentity(native.base, "fixture", "receipt", "session"))
    value, _edge = proposal(native, monkeypatch)
    monkeypatch.setattr("eimemory.governance.evolution.code_evolution_effects.execute_code_evolution_effects",
        lambda *_args, **_kwargs: dict(ok=True, applied=True, transaction_id=value["transaction_id"]))
    result = autonomous_evolution._apply_code_evolution_v2(native.runtime,
        dict(opportunity_id="dynamic-capability-" + native.item["work_item_id"],
             summary="Native feedback fixture", code_evolution_proposal=value), scope=asdict(SCOPE), apply=True)
    assert result["candidate_id"] and result["applied"] and "isolated_evaluator" not in result
    return native, value, result, CodeEvolutionTransactionManager(native.runtime)


def feedback(native, result, *, blocked=False):
    return dynamic_capability_evolution._record_dynamic_candidate_feedback(native.runtime, scope=SCOPE,
        item=native.item, evolution={"applied_patches": [] if blocked else [result],
                                   "blocked_patches": [result] if blocked else []})


def failed_terminal(fixture, *, exit_status=1):
    native, value, result, manager = fixture
    tx_id = value["transaction_id"]
    manager.transition(tx_id, "CANDIDATE_MATERIALIZED", updates={"candidate_tree_digest": "c" * 64})
    tx = manager.store.get_transaction(tx_id)
    artifact = manager.store.store_artifact(tx_id, artifact_kind="verification_log_focused",
        artifact_schema="code_evolution_verification_log.v1", data=b"1 failed" if exit_status == 1 else b"sandbox unavailable")
    receipt = manager.store.add_verification_receipt(tx_id,
        dict(verification_kind="focused", base_commit=native.base, patch_digest=tx["patch_digest"],
             candidate_tree_digest=tx["candidate_tree_digest"], test_plan_id=value["test_plan"]["id"],
             test_plan_digest=value["test_plan"]["digest"], command_digest="a" * 64, environment_digest="b" * 64,
             verifier_id="eimemory.protected-test-plan", verifier_revision="v1", exit_status=exit_status,
             test_count=1 if exit_status == 1 else 0, failed_count=1 if exit_status == 1 else 0,
             result="fail", log_artifact_digest=artifact["sha256"]))
    owner = CodeEvolutionEffectOwner(native.runtime, owner_id="fixture", adapter=None,
                                    policy_loader=lambda: {}, policy_consumer=lambda **_kwargs: {})
    owner._abort_candidate(tx_id, reason="focused_verification_failed" if exit_status == 1 else "verification_sandbox_unavailable",
                           evidence_digest=receipt["receipt_digest"])
    return feedback(native, result, blocked=True)


def independently_evaluate(native, *, passed=True):
    catalog = native.catalog
    if not passed:
        from eimemory.evaluation.capability_catalog import CapabilityEvaluationCatalog
        catalog = CapabilityEvaluationCatalog()
        catalog.register_executor(executor_id="eimemory.eval.native-gap", revision="v1",
                                  handler=lambda *_args: {"valid": False})
        catalog.register_cases(native.catalog.list_cases())
    report = run_capability_acceptance(native.runtime, scope=SCOPE, runtime_scope=SCOPE,
                                      profile_key=PROFILE, catalog=catalog)
    for result in report["results"]:
        probe = native.runtime.store.get_by_id(result["probe_record_id"], scope=SCOPE)
        trace = native.runtime.store.get_by_id(result["trace_record_id"], scope=SCOPE)
        verifier = trace.content["payload"]["verifier"]
        artifact = record_hypothesis_evaluation_artifact(native.runtime, runtime_scope=SCOPE,
            hypothesis_id=native.hypothesis.record_id, provider_binding_id=result["provider_binding_id"],
            execution=probe.content, probe_id=probe.record_id, trace_record_id=trace.record_id, verifier=verifier,
            evaluation_run_id=result["evaluation_run_id"])
        record_hypothesis_experiment_feedback(native.runtime, runtime_scope=SCOPE,
            hypothesis_id=native.hypothesis.record_id, artifact_type="evaluation", artifact_id=artifact.record_id,
            verdict="pass" if passed else "fail", verifier=verifier)


def test_nonterminal_strict_result_is_not_legacy_or_business_success(strict_candidate):
    native, value, result, manager = strict_candidate
    for blocked in (False, True):
        recorded = feedback(native, result, blocked=blocked)
        assert recorded["reason"] == "transaction_not_terminal"
        assert recorded["candidate_id"] == result["candidate_id"]
        assert recorded["qualifies_behavior"] is False
    assert manager.store.get_terminal_receipt(value["transaction_id"]) is None


def test_protected_failed_test_restricts_then_new_independent_eval_reopens(strict_candidate):
    native, value, result, manager = strict_candidate
    report = failed_terminal(strict_candidate)
    assert report["ok"] is True, report
    assert report["applicability_effect"] == "restrictive"
    record = native.runtime.store.get_by_id(report["feedback_id"], scope=SCOPE)
    assert record.content["schema"] == SCHEMA
    assert "isolated_evaluator" not in record.content and "verifier" not in record.content
    assert record.content["certifies_improvement"] is False
    gate = hypothesis_behavior_gate(native.runtime, runtime_scope=SCOPE, hypothesis_id=native.hypothesis.record_id)
    assert gate["allowed"] is False and gate["reason"] == "latest_transaction_feedback_not_pass", gate
    independently_evaluate(native)
    gate = hypothesis_behavior_gate(native.runtime, runtime_scope=SCOPE, hypothesis_id=native.hypothesis.record_id)
    assert gate["allowed"] is True, gate
    # Replaying/backfilling an old terminal cannot replace the newer proof.
    again = record_transaction_hypothesis_feedback(native.runtime, transaction_id=value["transaction_id"], scope=SCOPE)
    assert again["feedback_id"] == report["feedback_id"]
    assert native.runtime.store.get_by_id(again["feedback_id"], scope=SCOPE).time == record.time
    assert hypothesis_behavior_gate(native.runtime, runtime_scope=SCOPE, hypothesis_id=native.hypothesis.record_id)["allowed"] is True


def test_unavailable_verifier_is_trace_only(strict_candidate):
    native, value, result, manager = strict_candidate
    report = failed_terminal(strict_candidate, exit_status=126)
    assert report["ok"] is True and report["applicability_effect"] == "trace_only", report
    assert report["qualifies_behavior"] is False
    assert hypothesis_behavior_gate(native.runtime, runtime_scope=SCOPE, hypothesis_id=native.hypothesis.record_id)["allowed"] is True


def test_terminal_success_never_certifies_improvement_or_clears_independent_failure(strict_candidate):
    native, value, result, manager = strict_candidate
    tx_id = value["transaction_id"]
    independently_evaluate(native, passed=False)
    for state in ("CANDIDATE_MATERIALIZED", "FOCUSED_VERIFIED", "REGRESSION_VERIFIED", "FULL_SUITE_VERIFIED",
                  "POLICY_AUTHORIZED", "COMMIT_INTENT", "COMMITTED", "PUSH_INTENT", "PUSHED", "DEPLOY_INTENT",
                  "DEPLOYED_VERIFIED", "HEALTHY", "OBSERVING"):
        manager.transition(tx_id, state)
    tx = manager.store.get_transaction(tx_id)
    manager.terminalize(tx_id, dict(outcome="succeeded_sedimented", incident_digest=tx["incident_digest"],
        provider_digest=tx["implementation_digest"], base_commit=tx["base_commit"], evidence_digest="d" * 64),
        terminal_state="SUCCEEDED_SEDIMENTED")
    report = feedback(native, result)
    assert report["ok"] is True and report["applicability_effect"] == "trace_only", report
    assert report["qualifies_behavior"] is report["certifies_improvement"] is report["qualifies_for_product_completion"] is False
    gate = hypothesis_behavior_gate(native.runtime, runtime_scope=SCOPE, hypothesis_id=native.hypothesis.record_id)
    assert gate["allowed"] is False and gate["reason"] == "latest_independent_feedback_not_pass", gate


@pytest.mark.parametrize("mutation", ["scope", "hypothesis", "revision", "candidate", "terminal_missing", "terminal_tampered", "context_tampered"])
def test_cross_binding_and_broken_terminal_cannot_attest(strict_candidate, mutation):
    native, value, result, manager = strict_candidate
    original = failed_terminal(strict_candidate)
    assert original["ok"] is True, original
    tx_id = value["transaction_id"]
    kwargs = dict(transaction_id=tx_id, scope=SCOPE, candidate_id=result["candidate_id"], hypothesis_id=native.hypothesis.record_id)
    if mutation == "scope":
        kwargs["scope"] = replace(SCOPE, user_id="other")
    elif mutation == "hypothesis":
        kwargs["hypothesis_id"] = "different-hypothesis"
    elif mutation == "revision":
        hypothesis = deepcopy(native.hypothesis)
        hypothesis.content["capability_revision_id"] = "same-capability:other-revision"
        native.runtime.store.append(hypothesis)
    elif mutation == "candidate":
        kwargs["candidate_id"] = native.hypothesis.record_id
    elif mutation == "terminal_missing":
        # Simulate physical corruption after proving the normal owner path;
        # ordinary clients cannot delete these append-only ledger rows.
        manager.store.conn.execute("DROP TRIGGER trg_code_evolution_terminal_receipts_no_delete")
        manager.store.conn.execute("DELETE FROM code_evolution_terminal_receipts WHERE transaction_id=?", (tx_id,))
        manager.store.conn.commit()
    elif mutation == "terminal_tampered":
        manager.store.conn.execute("DROP TRIGGER trg_code_evolution_terminal_receipts_no_update")
        manager.store.conn.execute("UPDATE code_evolution_terminal_receipts SET evidence_digest=? WHERE transaction_id=?", ("x" * 64, tx_id))
        manager.store.conn.commit()
    else:
        tx = manager.store.get_transaction(tx_id)
        tx["payload"]["payload"]["dynamic_capability_context"]["item"]["hypothesis_id"] = "different-hypothesis"
        manager.store.conn.execute("DROP TRIGGER trg_code_evolution_transactions_terminal_no_update")
        manager.store.conn.execute("UPDATE code_evolution_transactions SET payload_json=? WHERE transaction_id=?",
                                   (json.dumps(tx["payload"]), tx_id))
        manager.store.conn.commit()
    report = record_transaction_hypothesis_feedback(native.runtime, **kwargs)
    assert report["ok"] is False and report["status"] == "blocked", report
    assert report["qualifies_behavior"] is False
    if mutation in {"terminal_missing", "terminal_tampered", "context_tampered"}:
        gate = hypothesis_behavior_gate(native.runtime, runtime_scope=SCOPE, hypothesis_id=native.hypothesis.record_id)
        assert gate["allowed"] is False and gate["reason"] == "transaction_feedback_evidence_invalid", gate


def test_maintenance_backfills_same_terminal_without_retime_or_external_effects(strict_candidate):
    native, value, result, manager = strict_candidate
    report = failed_terminal(strict_candidate)
    record = native.runtime.store.get_by_id(report["feedback_id"], scope=SCOPE)
    resumed = resume_code_evolution_transactions(native.runtime, scope=SCOPE)
    assert report["feedback_id"] in str(resumed), resumed
    assert native.runtime.store.get_by_id(report["feedback_id"], scope=SCOPE).time == record.time
