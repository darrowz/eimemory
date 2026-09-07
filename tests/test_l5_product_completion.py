from __future__ import annotations

from types import SimpleNamespace

import pytest

from eimemory.governance.l5_product_completion import build_product_completion
from eimemory.governance.l5_reader import _historical_advertisement_evidence_error


@pytest.mark.parametrize("provenance", [
    dict(origin="user_reported", known_before_detection=1, prior_user_reported=1, manual_bootstrap=0),
    dict(origin="manual_bootstrap", known_before_detection=1, prior_user_reported=1, manual_bootstrap=1),
    dict(origin="system_detector", known_before_detection=1, prior_user_reported=0, manual_bootstrap=0),
])
def test_active_ledger_projection_preserves_real_provenance(monkeypatch, provenance):
    from eimemory.governance.l5_reader import _code_evolution_evidence

    scope = dict(tenant_id="default", agent_id="a", workspace_id="w", user_id="u")
    active = dict(**scope, **provenance, transaction_id="maintenance", terminal=0,
                  repository_root="/repo", repository_ref="master", current_state="OBSERVING",
                  profile_key="l5.default", base_commit="a" * 40,
                  candidate_commit="b" * 40, deployed_commit="b" * 40)
    monkeypatch.setattr("eimemory.storage.code_evolution_store.CodeEvolutionStore",
        lambda store: SimpleNamespace(list_transactions=lambda **kwargs: [active]))
    monkeypatch.setattr("eimemory.adapters.hermes.code_implementation.resolve_code_implementation_provider",
        lambda *args, **kwargs: {})
    runtime = SimpleNamespace(store=object(), code_evolution_current_lineage={"ok": True, "compatible": True})
    _, transaction, _ = _code_evolution_evidence(runtime, runtime_scope=scope,
        capability_scope="global", checked_at="", repo_root="/repo", catalog=None)
    assert transaction["origin"] == provenance["origin"]
    for field in ("known_before_detection", "prior_user_reported", "manual_bootstrap"):
        assert transaction[field] is bool(provenance[field])
    assert transaction["profile_key"] == active["profile_key"]
    report = build_product_completion(_assessment(), provider={"ready": True, "catalog_ready": True,
        "advertisement_fresh": True}, transaction=transaction, current_lineage={"ok": True, "compatible": True})
    assert report["product_l5_complete"] is False
    assert "incident_known_before_system_detection" in report["gaps"]


def test_successful_known_maintenance_remains_nonqualifying_after_observation():
    report = build_product_completion(_assessment(),
        provider={"ready": True, "catalog_ready": True, "advertisement_fresh": True},
        transaction=dict(transaction_id="maintenance", terminal_receipt_digest="a" * 64,
            qualifying_terminal_outcome="succeeded_sedimented", origin="user_reported",
            known_before_detection=True, prior_user_reported=True, manual_bootstrap=False,
            observation_valid=True, evidence_verified=True, nonterminal=False, quarantined=False),
        current_lineage={"ok": True, "compatible": True})
    assert report["product_l5_complete"] is False
    assert report["code_evolution"]["transaction_verified"] is False
    assert set(report["gaps"]) == {
        "incident_known_before_system_detection", "incident_not_system_originated",
        "incident_prior_knowledge_unproven", "incident_not_user_reported_unproven",
    }


def _assessment() -> dict:
    return {
        "ok": True,
        "status": "ready",
        "loop_maturity": "evolving",
        "adapter_readiness": {"hermes": "ready"},
        "deployment_assurance": {"ok": None, "required": False, "blocking": False},
    }


def test_product_completion_keeps_bootstrap_and_manual_incident_incomplete() -> None:
    report = build_product_completion(
        _assessment(),
        provider={"ready": True},
        transaction={
            "qualifying_terminal_outcome": "succeeded_sedimented",
            "manual_bootstrap": True,
            "origin": "user_reported",
            "observation_valid": True,
        },
        current_lineage={"ok": True, "compatible": True},
    )

    assert report["control_plane_ok"] is True
    assert report["product_l5_complete"] is False
    assert report["ok"] is False
    assert report["completion_status"] == "incomplete"
    assert "manual_bootstrap_nonqualifying" in report["gaps"]


def test_product_completion_requires_qualifying_terminal_and_current_lineage() -> None:
    report = build_product_completion(
        _assessment(),
        provider={"ready": True, "catalog_ready": True, "advertisement_fresh": True},
        transaction={
            "transaction_id": "tx-system-implementation",
            "terminal_receipt_digest": "a" * 64,
            "qualifying_terminal_outcome": "succeeded_sedimented",
            "manual_bootstrap": False,
            "origin": "system_detector",
            "known_before_detection": False,
            "prior_user_reported": False,
            "observation_valid": True,
            "quarantined": False,
            "evidence_verified": True,
        },
        current_lineage={"ok": True, "compatible": True},
    )

    assert report["product_l5_complete"] is True
    assert report["completion_status"] == "complete"
    assert report["ok"] is True


def test_product_completion_rejects_unverified_terminal_row_shape() -> None:
    report = build_product_completion(
        _assessment(),
        provider={"ready": True, "catalog_ready": True, "advertisement_fresh": True},
        transaction={
            "transaction_id": "tx-shaped-only",
            "terminal_receipt_digest": "a" * 64,
            "qualifying_terminal_outcome": "succeeded_sedimented",
            "manual_bootstrap": False,
            "origin": "system_detector",
            "known_before_detection": False,
            "prior_user_reported": False,
            "observation_valid": True,
            "quarantined": False,
        },
        current_lineage={"ok": True, "compatible": True},
    )

    assert report["product_l5_complete"] is False
    assert "transaction_evidence_unverified" in report["gaps"]


def test_product_completion_labels_healthy_rollback_without_calling_it_success() -> None:
    report = build_product_completion(
        _assessment(),
        provider={"ready": True, "catalog_ready": True, "advertisement_fresh": True},
        transaction={
            "transaction_id": "tx-system-rollback",
            "terminal_receipt_digest": "b" * 64,
            "qualifying_terminal_outcome": "rolled_back_healthy",
            "manual_bootstrap": False,
            "origin": "system_detector",
            "known_before_detection": False,
            "prior_user_reported": False,
            "candidate_pushed_and_deployed": True,
            "rollback_executed": True,
            "observation_valid": True,
            "quarantined": False,
            "evidence_verified": True,
        },
        current_lineage={"ok": True, "compatible": True},
    )

    assert report["product_l5_complete"] is True
    assert report["code_evolution"]["qualifying_terminal_outcome"] == "rolled_back_healthy"
    assert report["code_evolution"]["label"] == "rolled_back_healthy"


def test_terminal_transaction_keeps_exact_historical_ad_after_live_refresh() -> None:
    original_digest = "a" * 64
    implementation_digest = "b" * 64
    transaction = {
        "advertisement_id": "advertisement.hermes.code-implementation:original",
        "advertisement_digest": original_digest,
        "implementation_digest": implementation_digest,
    }

    class Capabilities:
        def advertisement_context(self, advertisement_id, **_kwargs):
            return {
                "entity_id": advertisement_id,
                "entity_digest": original_digest,
                "status": "active",
                "descriptor": {
                    "binding_id": "binding.hermes.code-implementation:v9",
                    "capability_revision_id": "code.implementation:v9",
                    "provider_kind": "hermes",
                    "provider_instance_id": "hermes.eimemory.code-implementation.production",
                    "side_effect_class": "network",
                    "operations": ["propose_patch_v2"],
                    "environment_fingerprint": {
                        "implementation_digest": implementation_digest,
                    },
                },
            }

    error = _historical_advertisement_evidence_error(
        Capabilities(),
        transaction_row=transaction,
        runtime_scope={
            "tenant_id": "default",
            "agent_id": "hongtu",
            "workspace_id": "embodied",
            "user_id": "darrow",
        },
        capability_scope="global",
    )

    assert error == ""


def test_terminal_transaction_rejects_missing_historical_advertisement() -> None:
    class Capabilities:
        def advertisement_context(self, *_args, **_kwargs):
            return None

    error = _historical_advertisement_evidence_error(
        Capabilities(),
        transaction_row={
            "advertisement_id": "advertisement.hermes.code-implementation:missing",
            "advertisement_digest": "a" * 64,
            "implementation_digest": "b" * 64,
        },
        runtime_scope={
            "tenant_id": "default",
            "agent_id": "hongtu",
            "workspace_id": "embodied",
            "user_id": "darrow",
        },
        capability_scope="global",
    )

    assert error == "terminal_advertisement_unavailable"


@pytest.mark.parametrize("separate_runtime_scope", [False, True])
def test_report_release_scope_is_separate_from_provider_and_transaction_scope(
    monkeypatch, separate_runtime_scope: bool,
) -> None:
    from eimemory.governance.evidence_contract import ReleaseIdentity
    from eimemory.governance.l5_reader import build_l5_effective_report
    from eimemory.models.records import ScopeRef

    evidence_scope = ScopeRef(tenant_id="default", agent_id="hongtu",
                              workspace_id="embodied::channel::hermes", user_id="darrow")
    runtime_scope = ScopeRef(tenant_id="default", agent_id="hongtu",
                             workspace_id="embodied", user_id="darrow") if separate_runtime_scope else evidence_scope
    release = ReleaseIdentity(commit="a" * 40, version="1.0", receipt_id="receipt", session_id="receipt")
    seen = {}

    def assess(_runtime, **kwargs):
        seen["assessment_scope"] = kwargs["scope"]
        return _assessment()

    def provider(_runtime, **kwargs):
        seen["provider_scope"] = ScopeRef.from_dict(kwargs["runtime_scope"])
        return {"ready": True, "advertisement_fresh": True}

    def current_identity(_runtime, scope):
        seen["identity_scope"] = scope
        return release if scope == evidence_scope else None

    def lineage(_runtime, **kwargs):
        seen["lineage_scope"] = kwargs["scope"]
        return {"ok": True, "compatible": False, "current_release": {"commit": release.commit},
                "reason": "current_gate_evidence_missing"}

    def transaction(scope, transaction_id):
        return {"tenant_id": scope.tenant_id, "agent_id": scope.agent_id,
                "workspace_id": scope.workspace_id, "user_id": scope.user_id,
                "transaction_id": transaction_id, "terminal": 0,
                "repository_root": "/repo", "repository_ref": "master", "current_state": "OBSERVING"}

    rows = [transaction(runtime_scope, "runtime-transaction")]
    if separate_runtime_scope:
        rows.insert(0, transaction(evidence_scope, "report-scope-transaction"))
    monkeypatch.setattr("eimemory.governance.l5_assessment_v3.build_l5_assessment_v3", assess)
    monkeypatch.setattr("eimemory.adapters.hermes.code_implementation.resolve_code_implementation_provider", provider)
    monkeypatch.setattr("eimemory.storage.code_evolution_store.CodeEvolutionStore",
                        lambda store: SimpleNamespace(list_transactions=lambda **kwargs: rows))
    monkeypatch.setattr("eimemory.governance.evidence_contract.current_release_identity", current_identity)
    monkeypatch.setattr("eimemory.governance.release_lineage.current_release_lineage", lineage)

    report = build_l5_effective_report(SimpleNamespace(store=object()), scope=evidence_scope,
        runtime_scope=runtime_scope if separate_runtime_scope else None,
        repo_root="/repo", reader_mode="v3", profile_key="xiaomage")

    assert seen["identity_scope"] == evidence_scope
    assert seen["lineage_scope"] == evidence_scope
    assert seen["assessment_scope"] == runtime_scope
    assert seen["provider_scope"] == runtime_scope
    assert report["transaction_evidence"]["transaction_id"] == "runtime-transaction"
    assert report["current_lineage"]["reason"] == "current_gate_evidence_missing"
    assert report["current_lineage"]["compatible"] is False
    assert report["product_l5_complete"] is False
    assert report["status"] == "incomplete"
    assert "current_lineage_incompatible" in report["gaps"]
