from __future__ import annotations

import sqlite3

import pytest

from eimemory.governance.code_evolution_transaction import (
    CodeEvolutionTransactionManager,
    InvalidCodeEvolutionTransition,
    effect_execution_authorized,
)
from eimemory.governance.autonomous_evolution import _apply_safe_patch, _safe_patch_from_opportunity
from eimemory.api.runtime import Runtime
from eimemory.models.records import ScopeRef
from eimemory.storage.code_evolution_store import (
    CodeEvolutionConflict,
    CodeEvolutionStore,
    CodeEvolutionStoreError,
    digest_json,
)
from eimemory.storage.runtime_store import RuntimeStore


def _qualifying_v2_proposal(*, transaction_id: str = "tx-enabled-v2") -> dict:
    return {
        "schema_version": "code_implementation_proposal.v2",
        "transaction_id": transaction_id,
        "proposal_only": True,
        "qualifying": True,
        "test_only_provider": False,
        "origin": "system_detector",
        "detector": "detector.l5",
        "known_before_detection": False,
        "prior_user_reported": False,
        "manual_bootstrap": False,
        "profile_key": "l5.default:v1",
        "incident": {
            "incident_id": f"incident-{transaction_id}",
            "incident_class": "l5.product_completion_semantic_misreport",
            "incident_digest": "a" * 64,
        },
        "repository": {
            "repository_root": "/dev-project/eimemory",
            "repository_remote": "origin",
            "repository_ref": "master",
            "remote_url_digest": "b" * 64,
            "base_commit": "c" * 40,
            "base_tree_digest": "d" * 64,
        },
        "provider": {
            "capability_id": "code.implementation",
            "revision_id": "code.implementation:v8",
            "binding_id": "binding.hermes.code-implementation:v8",
            "provider_kind": "hermes",
            "provider_instance_id": "hermes.eimemory.code-implementation.production",
            "operation": "propose_patch_v2",
            "implementation_digest": "e" * 64,
        },
        "file_updates": [
            {
                "path": "eimemory/governance/l5_reader.py",
                "prior_sha256": "f" * 64,
                "content": "bounded candidate\n",
            }
        ],
        "proposal_digest": "1" * 64,
        "patch_digest": "1" * 64,
        "advertisement": {"advertisement_id": "advertisement-v2", "advertisement_digest": "2" * 64},
        "catalog": {"catalog_case_id": "hongtu_code_implementation_v2", "catalog_snapshot_digest": "3" * 64},
        "test_plan": {"id": "l5.product-completion-reporting.v1", "digest": "4" * 64},
    }


def test_enabled_qualifying_proposal_routes_to_protected_effect_owner(tmp_path, monkeypatch) -> None:
    runtime = Runtime.create(root=tmp_path / "runtime")
    captured: list[str] = []

    def execute(runtime_arg, *, transaction_id: str, owner_id: str):
        assert runtime_arg is runtime
        captured.append(transaction_id)
        transaction = CodeEvolutionStore(runtime.store).get_transaction(transaction_id)
        assert effect_execution_authorized(transaction or {}) is True
        return {
            "ok": True,
            "applied": True,
            "blocked_reason": "",
            "transaction_id": transaction_id,
            "transaction": transaction,
        }

    monkeypatch.setattr(
        "eimemory.governance.code_evolution_effects.execute_code_evolution_effects",
        execute,
    )
    try:
        result = CodeEvolutionTransactionManager(runtime, owner_id="test-owner").submit_proposal(
            _qualifying_v2_proposal(),
            scope={"tenant_id": "tenant", "agent_id": "agent", "workspace_id": "workspace", "user_id": "user"},
            effects_enabled=True,
            apply=True,
        )
    finally:
        runtime.close()

    assert result["ok"] is True
    assert captured == ["tx-enabled-v2"]


def _payload(transaction_id: str = "tx-1", *, ref: str = "master") -> dict:
    return {
        "transaction_id": transaction_id,
        "idempotency_key": f"idem-{transaction_id}",
        "scope": {"tenant_id": "tenant", "agent_id": "agent", "workspace_id": "workspace", "user_id": "user"},
        "incident": {"incident_id": f"incident-{transaction_id}", "incident_class": "l5.product_completion_semantic_misreport"},
        "origin": "system_detector",
        "detector": "detector.l5",
        "known_before_detection": False,
        "prior_user_reported": False,
        "manual_bootstrap": False,
        "repository": {"root": "/repo", "remote": "origin", "ref": ref, "base_commit": "a" * 40},
    }


def test_ledger_is_installed_on_existing_sqlite_authority_and_cas_is_strict(tmp_path) -> None:
    runtime_store = RuntimeStore(tmp_path / "runtime")
    ledger = CodeEvolutionStore(runtime_store)
    try:
        tables = {
            row[0]
            for row in runtime_store.sqlite.conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'code_evolution_%'"
            )
        }
        assert {
            "code_evolution_transactions",
            "code_evolution_artifacts",
            "code_evolution_step_events",
            "code_evolution_verification_receipts",
            "code_evolution_policy_consumptions",
            "code_evolution_terminal_receipts",
        } <= tables
        first = ledger.create_transaction(_payload())
        replay = ledger.create_transaction(_payload())
        assert first["idempotent"] is False
        assert replay["idempotent"] is True
        with pytest.raises(CodeEvolutionConflict):
            ledger.create_transaction({**_payload("tx-2"), "idempotency_key": "idem-tx-1"})

        manager = CodeEvolutionTransactionManager(runtime_store, owner_id="test-owner")
        diagnosed = manager.transition("tx-1", "DIAGNOSED")
        assert diagnosed["current_state"] == "DIAGNOSED"
        with pytest.raises(InvalidCodeEvolutionTransition):
            manager.transition("tx-1", "PUSHED")
        with pytest.raises(CodeEvolutionConflict):
            ledger.cas_transition(
                "tx-1",
                expected_state="DETECTED",
                expected_state_version=0,
                target_state="DIAGNOSED",
            )
    finally:
        runtime_store.close()


def test_intent_events_are_append_only_and_policy_is_one_shot(tmp_path) -> None:
    runtime_store = RuntimeStore(tmp_path / "runtime")
    ledger = CodeEvolutionStore(runtime_store)
    try:
        ledger.create_transaction(_payload())
        event = ledger.append_step_event(
            "tx-1",
            {"step": "commit", "phase": "intent", "attempt": 1, "from_state": "POLICY_AUTHORIZED", "to_state": "COMMIT_INTENT", "summary": "intent"},
        )
        assert event["sequence"] == 1
        assert ledger.append_step_event(
            "tx-1",
            {"step": "commit", "phase": "intent", "attempt": 1, "from_state": "POLICY_AUTHORIZED", "to_state": "COMMIT_INTENT", "summary": "intent"},
        )["idempotent"] is True
        result_event = ledger.append_step_event(
            "tx-1",
            {"step": "commit", "phase": "result", "attempt": 1, "from_state": "COMMIT_INTENT", "to_state": "COMMITTED", "summary": "result"},
        )
        assert result_event["prior_event_digest"] == event["event_digest"]
        with pytest.raises(sqlite3.DatabaseError):
            try:
                runtime_store.sqlite.conn.execute("DELETE FROM code_evolution_step_events WHERE transaction_id='tx-1'")
            finally:
                runtime_store.sqlite.conn.rollback()

        authorization_material = {
            "transaction_id": "tx-1",
            "policy_digest": "b" * 64,
        }
        authorization_digest = digest_json(authorization_material)
        authorized_policy = {"ok": True, "policy_digest": "b" * 64}
        policy_payload = {
            "authorization_material": authorization_material,
            "authorized_policy": authorized_policy,
        }
        first = ledger.consume_policy(
            transaction_id="tx-1",
            policy_digest="b" * 64,
            authorization_receipt_digest=authorization_digest,
            payload=policy_payload,
        )
        assert first["idempotent"] is False
        assert ledger.consume_policy(
            transaction_id="tx-1",
            policy_digest="b" * 64,
            authorization_receipt_digest=authorization_digest,
            payload=policy_payload,
        )["idempotent"] is True
        conflicting_material = {**authorization_material, "nonce": "different"}
        with pytest.raises(CodeEvolutionConflict):
            ledger.consume_policy(
                transaction_id="tx-1",
                policy_digest="b" * 64,
                authorization_receipt_digest=digest_json(conflicting_material),
                payload={"authorization_material": conflicting_material, "authorized_policy": authorized_policy},
            )
        with pytest.raises(CodeEvolutionConflict, match="payload"):
            ledger.consume_policy(
                transaction_id="tx-1",
                policy_digest="b" * 64,
                authorization_receipt_digest=authorization_digest,
                payload={**policy_payload, "changed": True},
            )
    finally:
        runtime_store.close()


def test_policy_consumption_requires_digest_bound_authorization_material(tmp_path) -> None:
    runtime_store = RuntimeStore(tmp_path / "runtime")
    ledger = CodeEvolutionStore(runtime_store)
    try:
        ledger.create_transaction(_payload())
        with pytest.raises(CodeEvolutionStoreError, match="authorization material"):
            ledger.consume_policy(
                transaction_id="tx-1",
                policy_digest="b" * 64,
                authorization_receipt_digest="c" * 64,
                payload={},
            )
    finally:
        runtime_store.close()


def test_step_event_hash_chain_and_artifact_digest_are_revalidated_on_read(tmp_path) -> None:
    runtime_store = RuntimeStore(tmp_path / "runtime")
    ledger = CodeEvolutionStore(runtime_store)
    try:
        ledger.create_transaction(_payload())
        ledger.append_step_event(
            "tx-1",
            {
                "step": "commit",
                "phase": "intent",
                "attempt": 1,
                "from_state": "POLICY_AUTHORIZED",
                "to_state": "COMMIT_INTENT",
                "summary": "intent",
            },
        )
        ledger.store_artifact(
            "tx-1",
            artifact_kind="proposal",
            artifact_schema="proposal.v1",
            data=b"bounded proposal",
        )
        assert len(ledger.list_step_events("tx-1")) == 1
        assert ledger.get_artifact("tx-1", "proposal")["sha256"]

        connection = runtime_store.sqlite.conn
        connection.execute("DROP TRIGGER trg_code_evolution_step_events_no_update")
        connection.execute(
            "UPDATE code_evolution_step_events SET summary='tampered' "
            "WHERE transaction_id='tx-1' AND sequence=1"
        )
        connection.commit()
        with pytest.raises(CodeEvolutionStoreError, match="event digest"):
            ledger.list_step_events("tx-1")

        connection.execute("DROP TRIGGER trg_code_evolution_artifacts_no_update")
        connection.execute(
            "UPDATE code_evolution_artifacts SET compressed_bytes=? "
            "WHERE transaction_id='tx-1' AND artifact_kind='proposal'",
            (b"not-zlib",),
        )
        connection.commit()
        with pytest.raises(CodeEvolutionStoreError, match="artifact"):
            ledger.get_artifact("tx-1", "proposal")
    finally:
        runtime_store.close()


def test_transaction_create_retry_ignores_generated_timestamps_only(tmp_path) -> None:
    runtime_store = RuntimeStore(tmp_path / "runtime")
    ledger = CodeEvolutionStore(runtime_store)
    try:
        first = ledger.create_transaction(
            {
                **_payload(),
                "created_at": "2026-08-23T00:00:00+00:00",
                "updated_at": "2026-08-23T00:00:00+00:00",
            }
        )
        replay = ledger.create_transaction(_payload())
        assert first["idempotent"] is False
        assert replay["idempotent"] is True

        changed = _payload()
        changed["incident"] = {
            **changed["incident"],
            "incident_class": "different.incident",
        }
        with pytest.raises(CodeEvolutionConflict, match="identity conflict"):
            ledger.create_transaction(changed)
    finally:
        runtime_store.close()


def test_terminal_receipt_is_required_and_quarantine_blocks_repository_ref(tmp_path) -> None:
    runtime_store = RuntimeStore(tmp_path / "runtime")
    manager = CodeEvolutionTransactionManager(runtime_store, owner_id="terminal-test")
    ledger = manager.store
    try:
        ledger.create_transaction(_payload("tx-quarantine"))
        terminal = manager.terminalize(
            "tx-quarantine",
            {
                "outcome": "recovery_quarantined",
                "incident_digest": ledger.get_transaction("tx-quarantine")["incident_digest"],
                "provider_digest": "",
                "policy_digest": "",
                "authorization_digest": "",
                "base_commit": "a" * 40,
                "evidence_digest": digest_json({"reason": "unknown external state"}),
            },
            terminal_state="RECOVERY_QUARANTINED",
        )
        assert terminal["receipt_digest"]
        assert ledger.get_transaction("tx-quarantine")["terminal_receipt_digest"] == terminal["receipt_digest"]

        with pytest.raises(CodeEvolutionConflict):
            manager.update_metadata("tx-quarantine", payload_updates={"tampered": True})
        with pytest.raises(CodeEvolutionConflict, match="quarantined"):
            ledger.create_transaction(_payload("tx-after-quarantine"))
        with pytest.raises(sqlite3.DatabaseError):
            runtime_store.sqlite.conn.execute(
                "UPDATE code_evolution_transactions SET payload_json='{}' WHERE transaction_id='tx-quarantine'"
            )
        runtime_store.sqlite.conn.rollback()
    finally:
        runtime_store.close()


def test_receipt_digest_cannot_be_supplied_without_matching_body(tmp_path) -> None:
    runtime_store = RuntimeStore(tmp_path / "runtime")
    ledger = CodeEvolutionStore(runtime_store)
    try:
        ledger.create_transaction(_payload())
        with pytest.raises(CodeEvolutionConflict, match="digest"):
            ledger.add_verification_receipt(
                "tx-1",
                {
                    "verification_kind": "focused",
                    "result": "pass",
                    "receipt_digest": "f" * 64,
                },
            )
        with pytest.raises(CodeEvolutionConflict, match="digest"):
            ledger.add_terminal_receipt(
                "tx-1",
                {
                    "outcome": "recovery_quarantined",
                    "evidence_digest": "e" * 64,
                    "receipt_digest": "f" * 64,
                },
                terminal_state="RECOVERY_QUARANTINED",
            )
    finally:
        runtime_store.close()


def test_autonomous_code_opportunity_routes_strict_v2_proposal_to_same_transaction_owner(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    scope = {"tenant_id": "tenant", "agent_id": "agent", "workspace_id": "workspace", "user_id": "user"}
    proposal = {
        "schema_version": "code_implementation_proposal.v2",
        "transaction_id": "tx-autonomous-v2",
        "proposal_only": True,
        "origin": "system_detector",
        "detector": "detector.l5",
        "known_before_detection": False,
        "prior_user_reported": False,
        "manual_bootstrap": False,
        "incident": {
            "incident_id": "incident-autonomous-v2",
            "incident_class": "l5.product_completion_semantic_misreport",
            "incident_digest": "a" * 64,
        },
        "repository": {
            "repository_root": str(tmp_path / "repo"),
            "repository_ref": "master",
            "base_commit": "b" * 40,
            "base_tree_digest": "d" * 64,
        },
        "provider": {
            "capability_id": "code.implementation",
            "revision_id": "code.implementation:v8",
            "binding_id": "binding.hermes.code-implementation:v8",
            "provider_kind": "hermes",
            "provider_instance_id": "hermes.eimemory.code-implementation.production",
            "operation": "propose_patch_v2",
            "implementation_digest": "c" * 64,
        },
        "file_updates": [],
    }
    opportunity = {
        "opportunity_id": "opportunity-autonomous-v2",
        "opportunity_type": "code_evolution_v2",
        "source": "test",
        "code_evolution_proposal": proposal,
    }
    try:
        patch = _safe_patch_from_opportunity(opportunity, scope=ScopeRef.from_dict(scope))
        assert patch["patch_type"] == "code_evolution_v2"
        result = _apply_safe_patch(runtime, patch, scope=scope, legacy_compatibility=False)
        assert result["applied"] is False
        assert result["blocked_reason"] == "code_evolution_effects_disabled"
        transaction_report = runtime.code_evolution_status(
            scope=scope,
            repo_root=str(tmp_path / "repo"),
        )["transactions"][0]
        assert transaction_report["transaction"]["transaction_id"] == "tx-autonomous-v2"
        assert transaction_report["transaction"]["current_state"] == "ABORTED_NO_EXTERNAL_EFFECT"
        assert transaction_report["terminal_receipt"]["outcome"] == "aborted_no_external_effect"
    finally:
        runtime.close()
