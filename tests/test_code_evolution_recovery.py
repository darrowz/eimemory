from __future__ import annotations

import pytest

from eimemory.governance.code_evolution_transaction import (
    CodeEvolutionTransactionManager,
    reconcile_commit,
    reconcile_deployment,
    reconcile_push,
    reconcile_rollback,
    recover_transaction,
)
from eimemory.governance.promotion_watch import (
    _record_code_evolution_observation_sample as observe_code_evolution_transaction,
)
from eimemory.governance.promotion_watch import (
    observe_code_evolution_transaction as observe_live_code_evolution_transaction,
)
from eimemory.governance.promotion_watch import resume_code_evolution_transactions
from eimemory.storage.runtime_store import RuntimeStore
from eimemory.storage.code_evolution_store import CodeEvolutionConflict
from eimemory.storage.code_evolution_store import CodeEvolutionStore


def _observing_manager(runtime_store: RuntimeStore, transaction_id: str) -> CodeEvolutionTransactionManager:
    manager = CodeEvolutionTransactionManager(runtime_store, owner_id="watch-test")
    manager.create_detected({
        "transaction_id": transaction_id,
        "idempotency_key": f"idem-{transaction_id}",
        "scope": {"tenant_id": "tenant", "agent_id": "agent", "workspace_id": "workspace", "user_id": "user"},
        "incident": {"incident_id": f"incident-{transaction_id}", "incident_class": "code"},
        "repository": {"root": "/repo", "remote": "origin", "ref": "master", "base_commit": "a" * 40},
    })
    for target in ("DIAGNOSED", "PROVIDER_RESOLVED", "PATCH_PROPOSED", "PATCH_VALIDATED", "CANDIDATE_MATERIALIZED", "FOCUSED_VERIFIED", "REGRESSION_VERIFIED", "FULL_SUITE_VERIFIED", "POLICY_AUTHORIZED", "COMMIT_INTENT", "COMMITTED", "PUSH_INTENT", "PUSHED", "DEPLOY_INTENT", "DEPLOYED_VERIFIED", "HEALTHY", "OBSERVING"):
        manager.transition(transaction_id, target)
    manager.update_metadata(
        transaction_id,
        payload_updates={
            "candidate_pushed_and_deployed": True,
            "deployment_receipt_digest": "c" * 64,
        },
    )
    return manager


def _sample(key: str, *, observed_at: str, health_ok: bool, measure: int = 1) -> dict:
    return {
        "sample_key": key,
        "observed_at": observed_at,
        "commit": "a" * 40,
        "release_identity": "release-a",
        "provider_advertisement_digest": "b" * 64,
        "deployment_receipt_digest": "c" * 64,
        "incident_measure": {"value": measure},
        "health_ok": health_ok,
    }


def test_recovery_accepts_only_exact_commit_and_remote_cas_states() -> None:
    assert reconcile_commit({"candidate_commit": "c", "base_commit": "b", "parent": "b", "tree_matches": True, "transaction_trailer_matches": True}).status == "committed"
    assert reconcile_commit({"candidate_commit": "c", "base_commit": "b", "parent": "x", "tree_matches": True, "transaction_trailer_matches": True}).quarantine_required
    assert reconcile_push({"remote_sha": "c", "candidate_commit": "c", "base_commit": "b"}).status == "pushed"
    assert reconcile_push({"remote_sha": "b", "candidate_commit": "c", "base_commit": "b"}).retry_allowed
    assert reconcile_push({"remote_sha": "x", "candidate_commit": "c", "base_commit": "b"}).quarantine_required


def test_deploy_and_rollback_reconcile_without_blind_replay() -> None:
    deployed = reconcile_deployment({"current_commit": "c", "candidate_commit": "c", "prior_commit": "b", "deployment_receipt_valid": True, "storage_release_marker": "committed", "health_ok": True})
    assert deployed.status == "deployed_verified"
    assert reconcile_deployment({"current_commit": "c", "candidate_commit": "c", "deployment_receipt_valid": False}).rollback_required
    assert reconcile_rollback({"current_commit": "b", "prior_commit": "b", "receipt_valid": True, "health_ok": True, "storage_clean": True}).status == "rolled_back_healthy"


def test_manager_recovers_pending_intent_to_quarantine_on_unknown_state(tmp_path) -> None:
    runtime_store = RuntimeStore(tmp_path / "runtime")
    manager = CodeEvolutionTransactionManager(runtime_store, owner_id="recovery-test")
    try:
        manager.create_detected({
            "transaction_id": "tx-recovery",
            "idempotency_key": "idem-recovery",
            "scope": {"tenant_id": "tenant", "agent_id": "agent", "workspace_id": "workspace", "user_id": "user"},
            "incident": {"incident_id": "incident-recovery", "incident_class": "code"},
            "repository": {"root": "/repo", "remote": "origin", "ref": "master", "base_commit": "a" * 40},
        })
        manager.transition("tx-recovery", "DIAGNOSED")
        manager.transition("tx-recovery", "PROVIDER_RESOLVED")
        manager.transition("tx-recovery", "PATCH_PROPOSED")
        manager.transition("tx-recovery", "PATCH_VALIDATED")
        manager.transition("tx-recovery", "CANDIDATE_MATERIALIZED")
        manager.transition("tx-recovery", "FOCUSED_VERIFIED")
        manager.transition("tx-recovery", "REGRESSION_VERIFIED")
        manager.transition("tx-recovery", "FULL_SUITE_VERIFIED")
        manager.transition("tx-recovery", "POLICY_AUTHORIZED")
        manager.begin_intent("tx-recovery", step="commit", intent_state="COMMIT_INTENT")
        result = recover_transaction(manager, "tx-recovery", external_state={"candidate_commit": "c", "base_commit": "b", "parent": "x"})
        assert result["current_state"] == "RECOVERY_QUARANTINED"
    finally:
        runtime_store.close()


def test_resume_reconciles_pending_intent_from_protected_external_reader(tmp_path, monkeypatch) -> None:
    runtime_store = RuntimeStore(tmp_path / "runtime")
    manager = CodeEvolutionTransactionManager(runtime_store, owner_id="recovery-test")
    manager.create_detected({
        "transaction_id": "tx-auto-reconcile",
        "idempotency_key": "idem-auto-reconcile",
        "scope": {"tenant_id": "tenant", "agent_id": "agent", "workspace_id": "workspace", "user_id": "user"},
        "incident": {"incident_id": "incident-auto-reconcile", "incident_class": "code"},
        "repository": {"root": "/repo", "remote": "origin", "ref": "master", "base_commit": "a" * 40},
        "candidate_tree_digest": "b" * 64,
        "candidate_commit": "c" * 40,
    })
    for state in ("DIAGNOSED", "PROVIDER_RESOLVED", "PATCH_PROPOSED", "PATCH_VALIDATED", "CANDIDATE_MATERIALIZED", "FOCUSED_VERIFIED", "REGRESSION_VERIFIED", "FULL_SUITE_VERIFIED", "POLICY_AUTHORIZED", "COMMIT_INTENT"):
        manager.transition("tx-auto-reconcile", state)
    monkeypatch.setattr(
        "eimemory.governance.code_evolution_effects.read_code_evolution_external_state",
        lambda *_args, **_kwargs: {
            "candidate_commit": "c" * 40,
            "base_commit": "a" * 40,
            "parent": "a" * 40,
            "tree_matches": True,
            "transaction_trailer_matches": True,
        },
    )
    try:
        report = resume_code_evolution_transactions(
            runtime_store,
            scope={"tenant_id": "tenant", "agent_id": "agent", "workspace_id": "workspace", "user_id": "user"},
        )
    finally:
        runtime_store.close()

    assert report["reports"][0]["current_state"] == "COMMITTED"
    assert report["reports"][0]["candidate_commit"] == "c" * 40
    assert report["reports"][0]["prior_commit"] == "a" * 40


def test_resume_rejects_caller_supplied_external_recovery_evidence(tmp_path) -> None:
    runtime_store = RuntimeStore(tmp_path / "runtime")
    try:
        with pytest.raises(TypeError, match="external_state_by_transaction"):
            resume_code_evolution_transactions(
                runtime_store,
                external_state_by_transaction={"forged": {"candidate_commit": "f" * 40}},
            )
    finally:
        runtime_store.close()


def test_public_observation_rejects_caller_supplied_sample_and_timestamp(tmp_path) -> None:
    runtime_store = RuntimeStore(tmp_path / "runtime")
    try:
        with pytest.raises(TypeError, match="sample"):
            observe_live_code_evolution_transaction(
                runtime_store,
                transaction_id="forged-observation",
                sample={"health_ok": True},
                observed_at="2099-01-01T00:00:00+00:00",
            )
    finally:
        runtime_store.close()


def test_recovery_requires_exclusive_transaction_lease(tmp_path) -> None:
    runtime_store = RuntimeStore(tmp_path / "runtime")
    manager = CodeEvolutionTransactionManager(runtime_store, owner_id="recovery-owner")
    holder = CodeEvolutionTransactionManager(runtime_store, owner_id="other-owner")
    try:
        manager.create_detected({
            "transaction_id": "tx-recovery-lease",
            "idempotency_key": "idem-recovery-lease",
            "scope": {"tenant_id": "tenant", "agent_id": "agent", "workspace_id": "workspace", "user_id": "user"},
            "incident": {"incident_id": "incident-recovery-lease", "incident_class": "code"},
            "repository": {"root": "/repo", "remote": "origin", "ref": "master", "base_commit": "a" * 40},
        })
        for target in ("DIAGNOSED", "PROVIDER_RESOLVED", "PATCH_PROPOSED", "PATCH_VALIDATED", "CANDIDATE_MATERIALIZED", "FOCUSED_VERIFIED", "REGRESSION_VERIFIED", "FULL_SUITE_VERIFIED", "POLICY_AUTHORIZED", "COMMIT_INTENT"):
            manager.transition("tx-recovery-lease", target)
        holder.acquire_lease("tx-recovery-lease")

        with pytest.raises(CodeEvolutionConflict, match="lease"):
            recover_transaction(
                manager,
                "tx-recovery-lease",
                external_state={"candidate_commit": "c", "base_commit": "b", "parent": "x"},
            )
        assert manager.store.get_transaction("tx-recovery-lease")["current_state"] == "COMMIT_INTENT"
    finally:
        runtime_store.close()


def test_observation_samples_are_lease_owned_and_idempotent(tmp_path) -> None:
    runtime_store = RuntimeStore(tmp_path / "runtime")
    manager = CodeEvolutionTransactionManager(runtime_store, owner_id="watch-test")
    try:
        manager.create_detected({
            "transaction_id": "tx-observe",
            "idempotency_key": "idem-observe",
            "scope": {"tenant_id": "tenant", "agent_id": "agent", "workspace_id": "workspace", "user_id": "user"},
            "incident": {"incident_id": "incident-observe", "incident_class": "code"},
            "repository": {"root": "/repo", "remote": "origin", "ref": "master", "base_commit": "a" * 40},
        })
        for target in ("DIAGNOSED", "PROVIDER_RESOLVED", "PATCH_PROPOSED", "PATCH_VALIDATED", "CANDIDATE_MATERIALIZED", "FOCUSED_VERIFIED", "REGRESSION_VERIFIED", "FULL_SUITE_VERIFIED", "POLICY_AUTHORIZED", "COMMIT_INTENT", "COMMITTED", "PUSH_INTENT", "PUSHED", "DEPLOY_INTENT", "DEPLOYED_VERIFIED", "HEALTHY", "OBSERVING"):
            manager.transition("tx-observe", target)
        sample = {
            "sample_key": "t0",
            "observed_at": "2026-01-01T00:00:00+00:00",
            "commit": "a" * 40,
            "release_identity": "release-a",
            "provider_advertisement_digest": "b" * 64,
            "live_provider_advertisement_digest": "d" * 64,
            "provider_authority_error": "",
            "deployment_receipt_digest": "c" * 64,
            "incident_measure": {"ok": True},
            "health_ok": True,
        }
        manager.update_metadata(
            "tx-observe",
            payload_updates={
                "candidate_pushed_and_deployed": True,
                "deployment_receipt_digest": "c" * 64,
            },
        )
        first = observe_code_evolution_transaction(runtime_store, transaction_id="tx-observe", sample=sample, owner_id="watch-test")
        second = observe_code_evolution_transaction(runtime_store, transaction_id="tx-observe", sample=sample, owner_id="watch-test")
        assert first["status"] == "observing"
        assert second["status"] == "duplicate"
        saved = manager.store.get_transaction("tx-observe")["payload"]["observation_samples"][0]
        assert saved["provider_advertisement_digest"] == "b" * 64
        assert saved["live_provider_advertisement_digest"] == "d" * 64
        events = manager.store.list_step_events("tx-observe")
        assert len([event for event in events if event["step"] == "observation"]) == 1
    finally:
        runtime_store.close()


def test_resume_samples_observing_transaction_from_trusted_runtime_state(tmp_path, monkeypatch) -> None:
    runtime_store = RuntimeStore(tmp_path / "runtime")
    manager = _observing_manager(runtime_store, "tx-auto-observe")
    transaction = manager.store.get_transaction("tx-auto-observe")
    assert transaction is not None
    monkeypatch.setattr(
        "eimemory.governance.code_evolution_effects.sample_code_evolution_observation",
        lambda *_args, **_kwargs: _sample(
            "phase-0",
            observed_at="2026-01-01T00:00:00+00:00",
            health_ok=True,
        ),
    )
    try:
        report = resume_code_evolution_transactions(
            runtime_store,
            scope={"tenant_id": "tenant", "agent_id": "agent", "workspace_id": "workspace", "user_id": "user"},
        )
    finally:
        runtime_store.close()

    assert report["reports"][0]["status"] == "observing"
    assert report["reports"][0]["sample_key"] == "phase-0"


def test_observation_cannot_sediment_without_deployment_receipt_and_candidate(tmp_path) -> None:
    runtime_store = RuntimeStore(tmp_path / "runtime")
    manager = CodeEvolutionTransactionManager(runtime_store, owner_id="watch-test")
    try:
        manager.create_detected({
            "transaction_id": "tx-unproven-observe",
            "idempotency_key": "idem-unproven-observe",
            "scope": {"tenant_id": "tenant", "agent_id": "agent", "workspace_id": "workspace", "user_id": "user"},
            "incident": {"incident_id": "incident-unproven-observe", "incident_class": "code"},
            "repository": {"root": "/repo", "remote": "origin", "ref": "master", "base_commit": "a" * 40},
        })
        for target in ("DIAGNOSED", "PROVIDER_RESOLVED", "PATCH_PROPOSED", "PATCH_VALIDATED", "CANDIDATE_MATERIALIZED", "FOCUSED_VERIFIED", "REGRESSION_VERIFIED", "FULL_SUITE_VERIFIED", "POLICY_AUTHORIZED", "COMMIT_INTENT", "COMMITTED", "PUSH_INTENT", "PUSHED", "DEPLOY_INTENT", "DEPLOYED_VERIFIED", "HEALTHY", "OBSERVING"):
            manager.transition("tx-unproven-observe", target)
        result = observe_code_evolution_transaction(
            runtime_store,
            transaction_id="tx-unproven-observe",
            owner_id="watch-test",
            sample={
                "sample_key": "unproven",
                "observed_at": "2026-01-01T00:00:00+00:00",
                "commit": "a" * 40,
                "release_identity": "release-a",
                "provider_advertisement_digest": "b" * 64,
                "deployment_receipt_digest": "c" * 64,
                "incident_measure": {"ok": True},
                "health_ok": True,
            },
        )
        assert result["ok"] is False
        assert result["status"] == "observation_evidence_unproven"
        assert manager.store.get_terminal_receipt("tx-unproven-observe") is None
    finally:
        runtime_store.close()


def test_noncritical_health_degradation_requires_two_consecutive_samples(tmp_path) -> None:
    runtime_store = RuntimeStore(tmp_path / "runtime")
    manager = _observing_manager(runtime_store, "tx-degraded")
    try:
        first = observe_code_evolution_transaction(
            runtime_store,
            transaction_id="tx-degraded",
            owner_id="watch-test",
            sample=_sample("degraded-1", observed_at="2026-01-01T00:00:00+00:00", health_ok=False),
        )
        assert first["status"] == "observing"
        assert manager.store.get_transaction("tx-degraded")["current_state"] == "OBSERVING"

        second = observe_code_evolution_transaction(
            runtime_store,
            transaction_id="tx-degraded",
            owner_id="watch-test",
            sample=_sample("degraded-2", observed_at="2026-01-01T00:15:00+00:00", health_ok=False),
        )
        assert second["status"] == "rollback_required"
        assert second["consecutive_degraded"] is True
        assert manager.store.get_transaction("tx-degraded")["current_state"] == "ROLLBACK_INTENT"
    finally:
        runtime_store.close()


def test_observation_sample_key_conflict_fails_closed(tmp_path) -> None:
    runtime_store = RuntimeStore(tmp_path / "runtime")
    _observing_manager(runtime_store, "tx-sample-conflict")
    try:
        first = observe_code_evolution_transaction(
            runtime_store,
            transaction_id="tx-sample-conflict",
            owner_id="watch-test",
            sample=_sample("same-key", observed_at="2026-01-01T00:00:00+00:00", health_ok=True),
        )
        conflict = observe_code_evolution_transaction(
            runtime_store,
            transaction_id="tx-sample-conflict",
            owner_id="watch-test",
            sample=_sample("same-key", observed_at="2026-01-01T00:00:00+00:00", health_ok=True, measure=2),
        )
        assert first["status"] == "observing"
        assert conflict == {
            "ok": False,
            "status": "observation_sample_identity_conflict",
            "transaction_id": "tx-sample-conflict",
            "sample_key": "same-key",
        }
    finally:
        runtime_store.close()


def test_complete_observation_window_appends_and_reconciles_real_outcome_once(tmp_path) -> None:
    runtime_store = RuntimeStore(tmp_path / "runtime")
    manager = _observing_manager(runtime_store, "tx-sedimentation")
    try:
        timestamps = (
            "2026-01-01T00:00:00+00:00",
            "2026-01-01T00:15:00+00:00",
            "2026-01-01T01:00:00+00:00",
            "2026-01-01T06:00:00+00:00",
            "2026-01-01T12:00:00+00:00",
            "2026-01-02T00:00:00+00:00",
            "2026-01-02T12:00:00+00:00",
            "2026-01-03T00:00:00+00:00",
        )
        reports = [
            observe_code_evolution_transaction(
                runtime_store,
                transaction_id="tx-sedimentation",
                owner_id="watch-test",
                sample=_sample(f"phase-{index}", observed_at=timestamp, health_ok=True),
            )
            for index, timestamp in enumerate(timestamps)
        ]

        assert reports[-1]["status"] == "succeeded_sedimented"
        assert reports[-1]["sedimentation_record_id"]
        transaction = manager.store.get_transaction("tx-sedimentation")
        assert transaction is not None
        assert transaction["current_state"] == "SUCCEEDED_SEDIMENTED"
        assert bool(transaction["terminal"]) is True
        receipt = manager.store.get_terminal_receipt("tx-sedimentation")
        assert receipt is not None
        assert receipt["outcome"] == "succeeded_sedimented"
        assert receipt["payload"]["sedimentation_record_id"] == reports[-1]["sedimentation_record_id"]
        assert receipt["payload"]["sedimentation_digest"] == reports[-1]["sedimentation_digest"]
        outcomes = runtime_store.list_records(
            kinds=["reflection"],
            scope={"tenant_id": "tenant", "agent_id": "agent", "workspace_id": "workspace", "user_id": "user"},
            limit=10,
        )
        matching = [record for record in outcomes if record.meta.get("code_evolution_transaction_id") == "tx-sedimentation"]
        assert len(matching) == 1
        assert matching[0].meta["semantic_digest"] == reports[-1]["sedimentation_digest"]
        sedimentation = [
            event
            for event in manager.store.list_step_events("tx-sedimentation")
            if event["step"] == "sedimentation"
        ]
        assert len(sedimentation) == 2
        assert sedimentation[0]["phase"] == "intent"
        assert sedimentation[1]["phase"] == "reconcile"
    finally:
        runtime_store.close()


def test_observation_failure_and_rollback_intent_survive_crash_after_atomic_commit(tmp_path, monkeypatch) -> None:
    runtime_store = RuntimeStore(tmp_path / "runtime")
    manager = _observing_manager(runtime_store, "tx-atomic-rollback")
    original = CodeEvolutionStore.commit_observation_result

    def commit_then_crash(self, *args, **kwargs):
        original(self, *args, **kwargs)
        raise RuntimeError("simulated_observation_crash")

    monkeypatch.setattr(CodeEvolutionStore, "commit_observation_result", commit_then_crash)
    try:
        with pytest.raises(RuntimeError, match="simulated_observation_crash"):
            observe_code_evolution_transaction(
                runtime_store,
                transaction_id="tx-atomic-rollback",
                owner_id="watch-test",
                sample={
                    **_sample(
                        "hard-failure",
                        observed_at="2026-01-01T00:00:00+00:00",
                        health_ok=False,
                    ),
                    "hard_failure": True,
                },
            )
        transaction = manager.store.get_transaction("tx-atomic-rollback")
        assert transaction["current_state"] == "ROLLBACK_INTENT"
        assert transaction["payload"]["observation_failure"] is True
        events = manager.store.list_step_events("tx-atomic-rollback")
        assert [(event["step"], event["phase"]) for event in events[-2:]] == [
            ("observation", "result"),
            ("rollback", "intent"),
        ]
    finally:
        runtime_store.close()


def test_sedimentation_resumes_after_crash_with_atomic_intent(tmp_path, monkeypatch) -> None:
    import eimemory.governance.promotion_watch as promotion_watch_module

    runtime_store = RuntimeStore(tmp_path / "runtime")
    manager = _observing_manager(runtime_store, "tx-atomic-sedimentation")
    timestamps = (
        "2026-01-01T00:00:00+00:00",
        "2026-01-01T00:15:00+00:00",
        "2026-01-01T01:00:00+00:00",
        "2026-01-01T06:00:00+00:00",
        "2026-01-01T12:00:00+00:00",
        "2026-01-02T00:00:00+00:00",
        "2026-01-02T12:00:00+00:00",
        "2026-01-03T00:00:00+00:00",
    )
    try:
        for index, timestamp in enumerate(timestamps[:-1]):
            observe_code_evolution_transaction(
                runtime_store,
                transaction_id="tx-atomic-sedimentation",
                owner_id="watch-test",
                sample=_sample(f"phase-{index}", observed_at=timestamp, health_ok=True),
            )
        execute_sedimentation = promotion_watch_module._execute_code_evolution_sedimentation
        monkeypatch.setattr(
            promotion_watch_module,
            "_execute_code_evolution_sedimentation",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("simulated_sedimentation_crash")),
        )
        final_sample = _sample(
            "phase-7",
            observed_at=timestamps[-1],
            health_ok=True,
        )
        with pytest.raises(RuntimeError, match="simulated_sedimentation_crash"):
            observe_code_evolution_transaction(
                runtime_store,
                transaction_id="tx-atomic-sedimentation",
                owner_id="watch-test",
                sample=final_sample,
            )
        interrupted = manager.store.get_transaction("tx-atomic-sedimentation")
        assert interrupted["current_state"] == "OBSERVING"
        assert interrupted["payload"]["observation_valid"] is True
        assert any(
            event["step"] == "sedimentation" and event["phase"] == "intent"
            for event in manager.store.list_step_events("tx-atomic-sedimentation")
        )

        monkeypatch.setattr(
            promotion_watch_module,
            "_execute_code_evolution_sedimentation",
            execute_sedimentation,
        )
        retry_sample = {
            **final_sample,
            "observed_at": "2026-01-03T00:15:00+00:00",
        }
        resumed = observe_code_evolution_transaction(
            runtime_store,
            transaction_id="tx-atomic-sedimentation",
            owner_id="watch-test",
            sample=retry_sample,
        )
        assert resumed["status"] == "succeeded_sedimented"
        assert manager.store.get_transaction("tx-atomic-sedimentation")["current_state"] == "SUCCEEDED_SEDIMENTED"
    finally:
        runtime_store.close()
