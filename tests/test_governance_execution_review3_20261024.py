"""Durable proposal retries at the protected execution handoff."""
from __future__ import annotations

from copy import deepcopy

import pytest

from eimemory.adapters.hermes import code_implementation as provider
from eimemory.api.runtime import Runtime
from eimemory.governance.evolution import code_evolution_effects as effects
from eimemory.governance.evolution.code_evolution_transaction import (
    CodeEvolutionTransactionManager,
    TRANSITIONS,
    effect_execution_authorized,
)
from eimemory.storage.code_evolution_store import CodeEvolutionConflict


SCOPE = dict(tenant_id="tenant", agent_id="agent", workspace_id="workspace", user_id="user")


def _proposal(transaction_id="retry-proposal"):
    return {
        "schema_version": "code_implementation_proposal.v2",
        "transaction_id": transaction_id, "proposal_only": True,
        "qualifying": True, "test_only_provider": False,
        "origin": "system_detector", "detector": "detector.l5",
        "known_before_detection": False, "prior_user_reported": False, "manual_bootstrap": False,
        "profile_key": "l5.default:v1",
        "incident": {"incident_id": "incident-retry", "incident_digest": "a" * 64,
                     "incident_class": "l5.product_completion_semantic_misreport"},
        "repository": {"repository_root": "/isolated/eimemory", "repository_remote": "origin",
                       "repository_ref": "master", "remote_url_digest": "b" * 64,
                       "base_commit": "c" * 40, "base_tree_digest": "d" * 64},
        "provider": {"capability_id": provider.CAPABILITY_ID, "revision_id": provider.REVISION_ID,
                     "binding_id": provider.BINDING_ID, "provider_kind": provider.PROVIDER_KIND,
                     "provider_instance_id": provider.PROVIDER_INSTANCE_ID, "operation": provider.OPERATION,
                     "implementation_digest": "e" * 64},
        "file_updates": [{"path": "eimemory/governance/l5/l5_reader.py", "prior_sha256": "f" * 64,
                          "content": "bounded candidate\n"}],
        "proposal_digest": "1" * 64, "patch_digest": "1" * 64,
        "advertisement": {"advertisement_id": "ad-retry", "advertisement_digest": "2" * 64},
        "catalog": {"catalog_case_id": "hongtu_code_implementation_v2", "catalog_snapshot_digest": "3" * 64},
        "test_plan": {"id": "l5.product-completion-reporting.v1", "digest": "4" * 64},
    }


def _strict_deployment_receipt(runtime, transaction, policy):
    """Persist the worker's receipt; native transaction validation stays real."""
    from eimemory.governance.release.deployment_receipt import _deployment_receipt_response
    from eimemory.models.records import RecordEnvelope, ScopeRef
    from eimemory.storage.code_evolution_store import CodeEvolutionStore

    commit = transaction["candidate_commit"]
    release_path = f"/opt/eimemory/releases/{commit}"
    lineage = {
        "ok": True, "compatible": True, "transaction_id": transaction["transaction_id"],
        "authorization_digest": transaction["authorization_digest"], "policy_digest": transaction["policy_digest"],
        "patch_digest": transaction["patch_digest"], "candidate_tree_digest": transaction["candidate_tree_digest"],
        "provider_implementation_digest": transaction["implementation_digest"], "deployed_commit": commit,
    }
    strict = {
        "strict": True, **{key: value for key, value in lineage.items() if key not in {"ok", "compatible"}},
        "lineage": lineage, "observation_deadline": transaction["observation_deadline"],
        "verification_receipt_digests": [item["receipt_digest"] for item in
            CodeEvolutionStore(runtime.store).list_verification_receipts(transaction["transaction_id"])],
    }
    record = runtime.store.append(RecordEnvelope.create(
        kind="promotion_request", title="Controlled strict deployment receipt",
        scope=ScopeRef.from_dict(SCOPE), source="eimemory.deployment_receipt", status="deployed",
        content={
            "report_type": "deployment_receipt", "promotion_target": "code_patch", "action": "code_patch",
            "gate": {"ok": True, "receipt_verified": True},
            "side_effect": {
                "ok": True, "production_applied": True, "deployment_executed": True,
                "verification": {"ok": True, "skipped": False, "prior_commit": transaction["base_commit"]},
                "deployment": {"ok": True, "skipped": False, "release_path": release_path,
                               "current_link": policy["deployment"]["current_link"]},
                "post_deploy_health": {"ok": True, "skipped": False, "commit": commit, "version": "1.0.0",
                                       "release_path": release_path, "url": policy["deployment"]["health_url"]},
                "commit": {"commit_sha": commit}, "release": {"version": "1.0.0", "release_path": release_path},
                "rollback_evidence": {"prior_commit_sha": transaction["base_commit"],
                                      "rollback_command": "controlled rollback", "commands": [["controlled-rollback"]]},
                "code_evolution": strict,
            },
        },
        meta={"transaction_id": transaction["transaction_id"], "commit_sha": commit},
    ))
    return record, _deployment_receipt_response(record)


def test_proposal_retry_after_handoff_interruption_resumes_without_backward_events(tmp_path, monkeypatch):
    root = tmp_path / "runtime"
    proposal = _proposal()
    runtime = Runtime.create(root=root)
    manager = CodeEvolutionTransactionManager(runtime, owner_id="first-submitter")

    def interrupted(*_args, **_kwargs):
        raise RuntimeError("controlled interruption before effect owner")

    try:
        with monkeypatch.context() as injected:
            injected.setattr(effects, "execute_code_evolution_effects", interrupted)
            with pytest.raises(RuntimeError, match="controlled interruption"):
                manager.submit_proposal(proposal, scope=SCOPE, effects_enabled=True, apply=True)
        current = manager.store.get_transaction(proposal["transaction_id"])
        assert current["current_state"] == "PATCH_VALIDATED"
        assert effect_execution_authorized(current) is True
        before = manager.store.list_step_events(proposal["transaction_id"])
        assert len(before) == 4
    finally:
        runtime.close()

    # The real effect owner must retain its own policy gate on resumption.
    # This deterministic missing-policy boundary performs no external effect.
    monkeypatch.setattr(effects, "load_code_automation_policy", lambda **_kwargs: {
        "ok": False, "status": "blocked", "reason": "policy_not_configured",
    })
    reopened = Runtime.create(root=root)
    try:
        resumed = CodeEvolutionTransactionManager(reopened, owner_id="retry-submitter")
        report = resumed.submit_proposal(proposal, scope=SCOPE, effects_enabled=True, apply=True)
        assert report["ok"] is False and report["applied"] is False
        assert report["blocked_reason"] == "policy_not_configured"
        assert report["transaction"]["current_state"] == "ABORTED_NO_EXTERNAL_EFFECT"
        events = resumed.store.list_step_events(proposal["transaction_id"])
        assert events[:len(before)] == before
        assert len(events) == len(before) + 1
        assert all(event["to_state"] in TRANSITIONS[event["from_state"]] for event in events)
        assert resumed.store.get_terminal_receipt(proposal["transaction_id"])["outcome"] == "aborted_no_external_effect"
    finally:
        reopened.close()


def test_proposal_retry_cannot_restart_a_transaction_with_unknown_external_effects(tmp_path, monkeypatch):
    runtime = Runtime.create(root=tmp_path / "runtime")
    proposal = _proposal("unknown-external-effect")
    manager = CodeEvolutionTransactionManager(runtime, owner_id="effect-submitter")

    def stop_before_effects(*_args, **_kwargs):
        raise RuntimeError("controlled handoff interruption")

    monkeypatch.setattr(effects, "execute_code_evolution_effects", stop_before_effects)
    try:
        with pytest.raises(RuntimeError, match="controlled handoff"):
            manager.submit_proposal(proposal, scope=SCOPE, effects_enabled=True, apply=True)
        for state in ("CANDIDATE_MATERIALIZED", "FOCUSED_VERIFIED", "REGRESSION_VERIFIED",
                      "FULL_SUITE_VERIFIED", "POLICY_AUTHORIZED"):
            manager.transition(proposal["transaction_id"], state)
        manager.begin_intent(proposal["transaction_id"], step="commit", intent_state="COMMIT_INTENT",
                             input_data={"candidate_tree_digest": "8" * 64})
        before = manager.store.get_transaction(proposal["transaction_id"])
        events = manager.store.list_step_events(proposal["transaction_id"])
        report = manager.submit_proposal(proposal, scope=SCOPE, effects_enabled=True, apply=True)
        assert report["ok"] is False and report["applied"] is False
        assert report["reconciliation_required"] is True
        assert report["blocked_reason"] == "code_evolution_transaction_recovery_required"
        assert report["transaction"]["current_state"] == "COMMIT_INTENT"
        assert manager.store.get_transaction(proposal["transaction_id"]) == before
        assert manager.store.list_step_events(proposal["transaction_id"]) == events
    finally:
        runtime.close()


def test_cancelled_proposal_replay_cannot_acquire_new_effect_authority(tmp_path, monkeypatch):
    runtime = Runtime.create(root=tmp_path / "runtime")
    proposal = _proposal("cancelled-proposal")
    manager = CodeEvolutionTransactionManager(runtime, owner_id="cancelled-submitter")
    calls = []

    def forbidden(*_args, **_kwargs):
        calls.append(True)
        return {"ok": False, "applied": False, "blocked_reason": "unexpected_effect_handoff"}

    monkeypatch.setattr(effects, "execute_code_evolution_effects", forbidden)
    try:
        cancelled = manager.submit_proposal(proposal, scope=SCOPE, effects_enabled=False, apply=False)
        transaction_id = proposal["transaction_id"]
        before = manager.store.get_transaction(transaction_id)
        receipt = manager.store.get_terminal_receipt(transaction_id)
        events = manager.store.list_step_events(transaction_id)
        assert cancelled["ok"] is False
        assert before["current_state"] == "ABORTED_NO_EXTERNAL_EFFECT"
        assert effect_execution_authorized(before) is False

        replay = manager.submit_proposal(proposal, scope=SCOPE, effects_enabled=True, apply=True)
        assert calls == []
        assert replay["ok"] is False and replay["applied"] is False
        assert replay["transaction"]["current_state"] == "ABORTED_NO_EXTERNAL_EFFECT"
        assert manager.store.get_transaction(transaction_id) == before
        assert manager.store.get_terminal_receipt(transaction_id) == receipt
        assert manager.store.list_step_events(transaction_id) == events

        changed = deepcopy(proposal)
        changed["patch_digest"] = "9" * 64
        with pytest.raises(CodeEvolutionConflict, match="identity conflict"):
            manager.submit_proposal(changed, scope=SCOPE, effects_enabled=True, apply=True)
        with pytest.raises(CodeEvolutionConflict, match="identity conflict"):
            manager.submit_proposal(proposal, scope={**SCOPE, "user_id": "other"}, effects_enabled=True, apply=True)
        assert calls == [] and manager.store.get_transaction(transaction_id) == before
    finally:
        runtime.close()


@pytest.mark.parametrize(("pending", "fail_bookkeeping"), [
    (True, False), (True, True), (False, False),
], ids=["worker_completion", "postcommit_bookkeeping_retry", "immediate_completion"])
def test_code_promotion_persists_pending_handoff_without_applied_credit(
    tmp_path, monkeypatch, pending, fail_bookkeeping,
):
    from eimemory.governance.promotion import promotion_manager
    from eimemory.governance.evolution.code_evolution_test_plans import protected_test_plan_digest
    from eimemory.models.records import RecordEnvelope, ScopeRef
    from eimemory.storage.code_evolution_store import CodeEvolutionStore, digest_json
    from tests.test_code_evolution_effects import _RecordingAdapter, _policy

    class ControlledAdapter(_RecordingAdapter):
        def deploy(self, runtime, **kwargs):
            if not pending:
                return super().deploy(runtime, **kwargs)
            kwargs["heartbeat"]()
            self.calls.append("deploy")
            return effects.DeploymentResult(False, "", "", "", {
                "pending": True, "job_key": "7" * 64, "operation": "deploy",
                "reason": "managed_deployment_job_awaiting_reconciliation",
            })

    runtime = Runtime.create(root=tmp_path / "runtime")
    proposal = _proposal("pending-promotion")
    proposal["test_plan"]["digest"] = protected_test_plan_digest(proposal["test_plan"]["id"])
    policy = _policy()
    adapter = ControlledAdapter(tmp_path / "candidate")
    monkeypatch.setattr(promotion_manager, "load_code_automation_policy", lambda **_kwargs: policy)

    def execute(runtime_arg, *, transaction_id, owner_id):
        def consume(*, path, transaction_id, expected_digest, store):
            material = {"transaction_id": transaction_id, "policy_digest": expected_digest}
            return {"ok": True, **CodeEvolutionStore(runtime_arg.store).consume_policy(
                transaction_id=transaction_id, policy_digest=expected_digest,
                authorization_receipt_digest=digest_json(material),
                payload={"authorization_material": material, "authorized_policy": policy},
            )}

        return effects.CodeEvolutionEffectOwner(
            runtime_arg, owner_id=owner_id, adapter=adapter,
            policy_loader=lambda: policy, policy_consumer=consume,
        ).execute(transaction_id)

    monkeypatch.setattr(effects, "execute_code_evolution_effects", execute)
    try:
        candidate = runtime.store.append(RecordEnvelope.create(
            kind="capability_candidate", title="Controlled deployment handoff",
            scope=ScopeRef.from_dict(SCOPE), status="candidate",
            content={"code_evolution_v2": True, "promotion_target": "code_patch",
                     "candidate_patch": proposal},
            meta={"authority_tier": "L0", "promotion_target": "code_patch"},
        ))
        report = promotion_manager.promote_candidate(runtime, candidate_id=candidate.record_id,
                                                     scope=SCOPE, apply=True)
        assert report["ok"] is True, report
        assert report["applied"] is (not pending)
        request = runtime.store.get_by_id(report["promotion_request_id"], scope=SCOPE)
        transaction = CodeEvolutionStore(runtime.store).get_transaction(proposal["transaction_id"])
        assert transaction["current_state"] == ("DEPLOY_INTENT" if pending else "OBSERVING")
        assert adapter.calls.count("commit") == adapter.calls.count("push") == adapter.calls.count("deploy") == 1
        assert len(CodeEvolutionStore(runtime.store).list_verification_receipts(proposal["transaction_id"])) == 3
        assert request.status == ("pending" if pending else "promoted")
        with runtime.store.locked() as sqlite:
            events = sqlite.execute("SELECT action_type FROM policy_rollout_ledger WHERE source_opportunity_id=?",
                                    (candidate.record_id,)).fetchall()
        actions = [row["action_type"] for row in events]
        assert ("applied" in actions) is (not pending)
        if pending:
            assert report["pending"] is True
            assert report["blocked_reason"] == "managed_deployment_job_awaiting_reconciliation"
            assert "deployment_pending" in actions
            assert request.content["side_effect"]["pending"] is True
            pending_original = deepcopy(request.content)
            manager = CodeEvolutionTransactionManager(runtime, owner_id="controlled-worker")
            receipt_record, receipt = _strict_deployment_receipt(runtime, transaction, policy)
            assert receipt["ok"] is True and receipt["strict_transaction"] is True
            receipt_digest = digest_json(receipt)
            manager.record_result(proposal["transaction_id"], step="deployment", result_state="DEPLOYED_VERIFIED",
                output_data={"deployment_receipt_digest": receipt_digest},
                updates={"deployed_commit": transaction["candidate_commit"], "payload_json": {
                    **transaction["payload"], "deployment_receipt_digest": receipt_digest,
                    "candidate_pushed_and_deployed": True, "deployment_version": "1.0.0",
                }})
            healthy = manager.record_result(proposal["transaction_id"], step="health", result_state="HEALTHY",
                                            output_data={"deployment_receipt_digest": receipt_digest})
            worker = effects.CodeEvolutionEffectOwner(runtime, owner_id=manager.owner_id, adapter=adapter,
                policy_loader=lambda: policy, policy_consumer=lambda **_kwargs: {})
            worker._start_observing(proposal["transaction_id"], healthy, policy)

            # Only the physical host/health boundary is simulated. The strict
            # receipt reader still binds scope, policy consumption and all
            # three original verification receipts from the real database.
            from eimemory.governance.release import deployment_receipt as receipts
            monkeypatch.setattr(receipts, "DEFAULT_DEPLOYMENT_REPO_ROOT", proposal["repository"]["repository_root"])
            live = {"ok": True, "deployment_receipt_digest": receipt_digest}
            monkeypatch.setattr(receipts, "inspect_immutable_deployment", lambda *_args, **_kwargs: dict(live))
            before_calls = list(adapter.calls)
            if fail_bookkeeping:
                import json

                native_enqueue = runtime.store._enqueue_record_exports
                rolled_back_operation_ids = []

                def interrupt_pending_export(record):
                    exports = native_enqueue(record)
                    if record.record_id == request.record_id and record.status == "completed":
                        rolled_back_operation_ids.extend(item["operation_id"] for item in exports)
                        raise RuntimeError("private bookkeeping storage detail")
                    return exports

                before_transaction = manager.store.get_transaction(proposal["transaction_id"])
                with monkeypatch.context() as interrupted_bookkeeping:
                    interrupted_bookkeeping.setattr(runtime.store, "_enqueue_record_exports", interrupt_pending_export)
                    partial = promotion_manager.promote_candidate(
                        runtime, candidate_id=candidate.record_id, scope=SCOPE, apply=True,
                    )
                assert partial["ok"] is True and partial["applied"] is True
                assert partial["idempotent"] is True and partial["pending"] is False
                assert partial["pending_bookkeeping"] == {
                    "ok": False, "status": "retry_required", "retryable": True,
                    "blocked_reason": "code_promotion_pending_bookkeeping_failed", "error_type": "RuntimeError",
                }
                assert "private bookkeeping storage detail" not in json.dumps(partial)
                assert runtime.store.get_by_id(partial["promotion_request_id"], scope=SCOPE).status == "promoted"
                pending_after_failure = runtime.store.get_by_id(request.record_id, scope=SCOPE)
                assert pending_after_failure.status == "pending"
                assert pending_after_failure.content == pending_original
                assert rolled_back_operation_ids
                with runtime.store.locked() as sqlite:
                    for operation_id in rolled_back_operation_ids:
                        assert sqlite.execute("SELECT count(*) FROM export_outbox WHERE operation_id=?",
                                              (operation_id,)).fetchone()[0] == 0
                assert manager.store.get_transaction(proposal["transaction_id"]) == before_transaction
                assert adapter.calls == before_calls
            first = promotion_manager.promote_candidate(runtime, candidate_id=candidate.record_id, scope=SCOPE, apply=True)
            second = promotion_manager.promote_candidate(runtime, candidate_id=candidate.record_id, scope=SCOPE, apply=True)
            for readback in (first, second):
                assert readback["ok"] is True and readback["applied"] is True, readback
                assert readback["idempotent"] is True and readback["pending"] is False
                assert readback["side_effect"]["execution_attempted"] is False
                assert readback["pending_bookkeeping"] == {"ok": True, "status": "complete", "retryable": False}
            if fail_bookkeeping:
                assert first["promotion_request_id"] == partial["promotion_request_id"]
            assert first["promotion_request_id"] == second["promotion_request_id"]
            completed = runtime.store.get_by_id(first["promotion_request_id"], scope=SCOPE)
            assert completed.status == "promoted"
            handed_off = runtime.store.get_by_id(request.record_id, scope=SCOPE)
            assert handed_off.status == "completed"
            assert handed_off.content == {**pending_original, "completed_by_promotion_request_id": completed.record_id}
            with runtime.store.locked() as sqlite:
                assert sqlite.execute("SELECT count(*) FROM policy_rollout_ledger WHERE source_opportunity_id=? AND action_type='applied'",
                                      (candidate.record_id,)).fetchone()[0] == 0
            assert adapter.calls == before_calls

            # New current-release failure, revoked receipt and revoked action
            # permission each remove reuse; no old success is used as a bypass.
            live["ok"] = False
            changed_release = promotion_manager.promote_candidate(runtime, candidate_id=candidate.record_id, scope=SCOPE, apply=True)
            assert changed_release["ok"] is False and changed_release["applied"] is False
            live["ok"] = True
            receipt_record.status = "revoked"
            runtime.store.rewrite(receipt_record)
            revoked = promotion_manager.promote_candidate(runtime, candidate_id=candidate.record_id, scope=SCOPE, apply=True)
            assert revoked["ok"] is False and revoked["applied"] is False
            receipt_record.status = "deployed"
            runtime.store.rewrite(receipt_record)
            with monkeypatch.context() as revoked_policy:
                revoked_policy.setattr(promotion_manager, "load_code_automation_policy", lambda **_kwargs: {"ok": False})
                denied = promotion_manager.promote_candidate(runtime, candidate_id=candidate.record_id, scope=SCOPE, apply=True)
                assert denied["ok"] is False and denied["applied"] is False
            recovered = promotion_manager.promote_candidate(runtime, candidate_id=candidate.record_id, scope=SCOPE, apply=True)
            assert recovered["ok"] is True and recovered["promotion_request_id"] == completed.record_id
            assert adapter.calls == before_calls
            current = manager.store.get_transaction(proposal["transaction_id"])
            manager.terminalize(proposal["transaction_id"], {
                "outcome": "succeeded_sedimented", "incident_digest": current["incident_digest"],
                "provider_digest": current["implementation_digest"], "policy_digest": current["policy_digest"],
                "authorization_digest": current["authorization_digest"], "base_commit": current["base_commit"],
                "candidate_commit": current["candidate_commit"], "deployed_commit": current["deployed_commit"],
                "deployment_receipt_digest": receipt_digest, "observation_digest": "a" * 64,
                "evidence_digest": "b" * 64,
            }, terminal_state="SUCCEEDED_SEDIMENTED")
            terminal = promotion_manager.promote_candidate(runtime, candidate_id=candidate.record_id, scope=SCOPE, apply=True)
            assert terminal["ok"] is True and terminal["applied"] is True, terminal
            assert terminal["idempotent"] is True and terminal["promotion_request_id"] == completed.record_id
            assert terminal["side_effect"]["terminal_receipt"]["outcome"] == "succeeded_sedimented"
            assert adapter.calls == before_calls
    finally:
        runtime.close()
