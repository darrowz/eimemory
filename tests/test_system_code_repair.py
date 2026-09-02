from __future__ import annotations

from types import SimpleNamespace
from pathlib import Path
import pytest

from eimemory.api.runtime import Runtime
from eimemory.governance.system_code_repair import process_system_code_incidents
from eimemory.ops.release_closure_failure import record_release_closure_failure


SCOPE = {
    "tenant_id": "default",
    "agent_id": "hongtu",
    "workspace_id": "embodied",
    "user_id": "darrow",
}
COMMIT = "a" * 40


@pytest.mark.parametrize("policy_consumed", [False, True])
@pytest.mark.parametrize("profile_available", [False, True])
def test_trusted_closure_incident_enters_v2_transaction_path(tmp_path, monkeypatch, policy_consumed, profile_available) -> None:
    runtime = Runtime.create(root=tmp_path / "runtime")
    recorded = record_release_closure_failure(
        runtime,
        scope=SCOPE,
        closure_report={
            "ok": False,
            "closure_complete": False,
            "data_accumulating": False,
            "blocked_stage": "release_lineage",
            "blocked_reason": "release_lineage_not_compatible",
            "deployment": {"commit": COMMIT, "version": "1.11.40"},
            "release_lineage": {"compatible": False},
        },
        detected_at="2026-08-28T12:00:00Z",
    )
    monkeypatch.setattr(
        "eimemory.governance.system_code_repair._repository_identity",
        lambda _root: {"ok": True, "base_commit": COMMIT},
    )
    monkeypatch.setattr(
        "eimemory.governance.system_code_repair._automation_policy_identity",
        lambda: (recorded["incident"]["incident_digest"], "f" * 64, "custom.production"),
    )
    def resolve(_self, profile_key, **kwargs):
        from eimemory.capabilities.profiles import CapabilityProfileError
        assert profile_key == "custom.production"
        assert kwargs["runtime_scope"].agent_id == SCOPE["agent_id"]
        if not profile_available:
            raise CapabilityProfileError("profile unavailable")
        return SimpleNamespace()
    monkeypatch.setattr("eimemory.capabilities.profiles.CapabilityProfiles.resolve", resolve)
    if policy_consumed:
        monkeypatch.setattr(
            "eimemory.governance.system_code_repair.CodeEvolutionStore.get_policy_consumption",
            lambda _self, _digest: {"transaction_id": "already-authorized"},
        )
    monkeypatch.setattr(
        "eimemory.governance.evidence_contract.current_release_identity",
        lambda *_args, **_kwargs: SimpleNamespace(commit=COMMIT),
    )
    proposal_calls = []

    def proposal(_runtime, **kwargs):
        proposal_calls.append(kwargs)
        return {
            "ok": True,
            "schema_version": "code_implementation_proposal.v2",
            "transaction_id": kwargs["transaction_id"],
            "incident": kwargs["incident"],
            "repository": {"base_commit": COMMIT},
            "provider": {"capability_id": "code.implementation"},
        }

    monkeypatch.setattr(
        "eimemory.governance.code_evolution_bridge.propose_code_patch_v2",
        proposal,
    )
    evolution_calls = []

    def evolve(_runtime, **kwargs):
        evolution_calls.append(kwargs)
        return {"ok": True, "applied_count": 0, "blocked_patches": []}

    monkeypatch.setattr(Runtime, "run_autonomous_evolution", evolve)
    try:
        report = process_system_code_incidents(
            runtime,
            scope=SCOPE,
            repo_root=Path.cwd(),
            max_items=1,
        )
    finally:
        runtime.close()

    if policy_consumed:
        assert report["reason"] == "automation_policy_already_consumed"
        assert proposal_calls == []
        assert evolution_calls == []
        return
    if not profile_available:
        assert report["reason"] == "automation_policy_profile_unavailable"
        assert proposal_calls == []
        assert evolution_calls == []
        return
    assert report["ok"] is True
    assert report["status"] == "processed"
    assert proposal_calls[0]["origin"] == "system_detector"
    assert proposal_calls[0]["known_before_detection"] is False
    assert proposal_calls[0]["prior_user_reported"] is False
    assert proposal_calls[0]["manual_bootstrap"] is False
    assert proposal_calls[0]["profile_key"] == "custom.production"
    assert proposal_calls[0]["bounds"] == {
        "maximum_files": 1,
        "maximum_bytes_per_file": 48 * 1024,
        "maximum_total_bytes": 96 * 1024,
        "maximum_changed_lines": 400,
    }
    assert evolution_calls[0]["apply"] is True
    assert evolution_calls[0]["mine_events"] is False
    opportunity = evolution_calls[0]["opportunities"][0]
    assert opportunity["source_outcome_payload"]["source_trust"] == "system_verified"
    assert opportunity["code_evolution_proposal"]["schema_version"] == "code_implementation_proposal.v2"


def test_untrusted_incident_is_not_routed(tmp_path, monkeypatch) -> None:
    runtime = Runtime.create(root=tmp_path / "runtime")
    monkeypatch.setattr(
        "eimemory.governance.system_code_repair._repository_identity",
        lambda _root: {"ok": True, "base_commit": COMMIT},
    )
    monkeypatch.setattr(
        "eimemory.governance.evidence_contract.current_release_identity",
        lambda *_args, **_kwargs: SimpleNamespace(commit=COMMIT),
    )
    try:
        report = process_system_code_incidents(runtime, scope=SCOPE, repo_root=tmp_path)
    finally:
        runtime.close()

    assert report == {"ok": True, "status": "idle", "processed": []}


@pytest.fixture
def routing_harness(monkeypatch):
    """Exercise routing without a provider, writable store, or real policy."""

    record = SimpleNamespace(
        status="active",
        source="eimemory.release_closure_failure",
        provenance={
            "origin": "system_detector",
            "detector": "eimemory.release_closure_failure.v1",
            "known_before_detection": False,
            "prior_user_reported": False,
        },
        meta={"observation_valid": True, "incident_digest": "d" * 64},
        content={
            "incident_id": "incident-current-release",
            "incident_digest": "d" * 64,
            "incident_class": "release.closure_internal_failure",
            "title": "Current release detector incident",
            "summary": "Bounded routing fixture",
            "diagnostic_codes": ["release_lineage:implementation_failure"],
            "acceptance_requirements": ["focused_failure_reproduction"],
            "detector_report": {
                "origin": "system_detector",
                "manual_bootstrap": False,
                "observation_valid": True,
                "release_commit": COMMIT,
            },
        },
    )
    calls = {"proposal": [], "evolution": [], "consumption_reads": []}

    def consumption(digest):
        calls["consumption_reads"].append(digest)
        return None

    ledger = SimpleNamespace(
        get_policy_consumption=consumption,
        get_transaction=lambda _transaction_id: None,
    )

    def proposal(_runtime, **kwargs):
        calls["proposal"].append(kwargs)
        return {"ok": True, "transaction_id": kwargs["transaction_id"]}

    def evolve(**kwargs):
        calls["evolution"].append(kwargs)
        return {"ok": True}

    runtime = SimpleNamespace(
        store=SimpleNamespace(list_records=lambda **_kwargs: [record]),
        run_autonomous_evolution=evolve,
    )
    monkeypatch.setattr(
        "eimemory.governance.system_code_repair._repository_identity",
        lambda _root: {"ok": True, "base_commit": COMMIT},
    )
    monkeypatch.setattr(
        "eimemory.governance.evidence_contract.current_release_identity",
        lambda *_args, **_kwargs: SimpleNamespace(commit=COMMIT),
    )
    monkeypatch.setattr(
        "eimemory.governance.system_code_repair._automation_policy_identity",
        lambda: ("d" * 64, "f" * 64, "l5.default"),
    )
    monkeypatch.setattr(
        "eimemory.governance.system_code_repair.CodeEvolutionStore",
        lambda _store: ledger,
    )
    monkeypatch.setattr(
        "eimemory.capabilities.profiles.CapabilityProfiles",
        lambda _store: SimpleNamespace(resolve=lambda *_args, **_kwargs: None),
    )
    monkeypatch.setattr(
        "eimemory.governance.code_evolution_bridge.propose_code_patch_v2",
        proposal,
    )
    return SimpleNamespace(
        record=record,
        calls=calls,
        ledger=ledger,
        run=lambda: process_system_code_incidents(runtime, scope=SCOPE, repo_root=Path.cwd()),
    )


def _select_runtime_drift_incident(record):
    record.source = "eimemory.runtime_identity_drift"
    record.provenance["detector"] = "eimemory.runtime_identity_drift.v1"
    record.content["incident_class"] = "deployment.runtime_commit_drift"
    report = record.content["detector_report"]
    report["expected_commit"] = report.pop("release_commit")


def _select_system_repair_policy_incident(record):
    record.source = "eimemory.system_code_repair_failure"
    record.provenance["detector"] = "eimemory.system_code_repair_failure.v1"
    record.content["incident_class"] = "code.system_repair_policy_stale"
    record.content["detector_report"]["detector"] = "eimemory.system_code_repair_failure.v1"


@pytest.mark.parametrize("status", ["resolved", "archived", "quarantined", "", None])
def test_non_active_detector_incident_never_reaches_provider(routing_harness, status):
    routing_harness.record.status = status

    report = routing_harness.run()

    assert report == {"ok": True, "status": "idle", "processed": []}
    assert routing_harness.calls["proposal"] == []
    assert routing_harness.calls["evolution"] == []


@pytest.mark.parametrize("runtime_drift", [False, True])
@pytest.mark.parametrize("detected_commit", [None, "", "a" * 7, "b" * 40])
def test_detector_release_must_exactly_match_current_base_before_provider(
    routing_harness, runtime_drift, detected_commit
):
    record = routing_harness.record
    if runtime_drift:
        _select_runtime_drift_incident(record)
    field = "expected_commit" if runtime_drift else "release_commit"
    if detected_commit is None:
        record.content["detector_report"].pop(field)
    else:
        record.content["detector_report"][field] = detected_commit

    report = routing_harness.run()

    assert report == {"ok": True, "status": "idle", "processed": []}
    assert routing_harness.calls["proposal"] == []
    assert routing_harness.calls["evolution"] == []


@pytest.mark.parametrize("runtime_drift", [False, True])
@pytest.mark.parametrize("existing_transaction", [False, True])
def test_current_active_incident_preserves_routing_and_idempotency(
    routing_harness, runtime_drift, existing_transaction
):
    if runtime_drift:
        _select_runtime_drift_incident(routing_harness.record)
    if existing_transaction:
        routing_harness.ledger.get_transaction = lambda _transaction_id: {
            "current_state": "DETECTED"
        }

    report = routing_harness.run()

    assert report["status"] == "processed"
    assert len(report["processed"]) == 1
    if existing_transaction:
        assert report["processed"][0]["idempotent"] is True
        assert routing_harness.calls["proposal"] == []
        assert routing_harness.calls["evolution"] == []
    else:
        assert report["processed"][0]["status"] == "submitted"
        assert len(routing_harness.calls["proposal"]) == 1
        assert len(routing_harness.calls["evolution"]) == 1
        assert routing_harness.calls["proposal"][0]["base_commit"] == COMMIT


def test_missing_current_release_blocks_before_any_policy_or_provider(routing_harness, monkeypatch):
    monkeypatch.setattr(
        "eimemory.governance.evidence_contract.current_release_identity",
        lambda *_args, **_kwargs: None,
    )

    report = routing_harness.run()

    assert report["reason"] == "repository_release_identity_mismatch"
    assert routing_harness.calls == {
        "proposal": [], "evolution": [], "consumption_reads": []
    }


def test_current_system_repair_policy_incident_uses_protected_routing_plan(routing_harness):
    _select_system_repair_policy_incident(routing_harness.record)

    report = routing_harness.run()

    assert report["status"] == "processed"
    assert routing_harness.calls["proposal"][0]["allowed_files"] == (
        "eimemory/governance/system_code_repair.py",
    )
    assert routing_harness.calls["proposal"][0]["test_plan_id"] == (
        "code.incident-routing-repair.v1"
    )


def test_consumed_policy_for_no_matching_current_incident_is_idle(routing_harness, monkeypatch):
    monkeypatch.setattr(
        "eimemory.governance.system_code_repair._automation_policy_identity",
        lambda: ("e" * 64, "f" * 64, "l5.default"),
    )
    routing_harness.ledger.get_policy_consumption = lambda _digest: {
        "transaction_id": "older-transaction"
    }

    report = routing_harness.run()

    assert report == {"ok": True, "status": "idle", "processed": []}
    assert routing_harness.calls["proposal"] == []
    assert routing_harness.calls["evolution"] == []
