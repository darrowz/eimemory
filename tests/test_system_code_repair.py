from __future__ import annotations

from types import SimpleNamespace
from pathlib import Path
import pytest

from eimemory.api.runtime import Runtime
from eimemory.governance.system_code_repair import process_system_code_incidents
from eimemory.models.records import ScopeRef
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
    assert report["ok"] is False
    assert report["status"] == "blocked"
    assert report["processed"][0]["reason"] == "autonomous_evolution_transaction_not_persisted"
    assert proposal_calls[0]["origin"] == "system_detector"
    assert proposal_calls[0]["known_before_detection"] is None
    assert proposal_calls[0]["prior_user_reported"] is None
    assert proposal_calls[0]["manual_bootstrap"] is None
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
        record_id="incident-record-current",
        kind="incident",
        scope=ScopeRef.from_dict(SCOPE),
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
    record.to_dict = lambda: dict(record_id=record.record_id,scope=SCOPE,source=record.source,
        content=record.content,provenance=record.provenance,meta=record.meta)

    def consumption(digest):
        calls["consumption_reads"].append(digest)
        return None

    ledger = SimpleNamespace(
        get_policy_consumption=consumption,
        rows={},
    )
    ledger.get_transaction = lambda transaction_id: ledger.rows.get(transaction_id)

    def transaction_row(transaction_id):
        return {
            **SCOPE, "transaction_id": transaction_id,
            "repository_root": str(Path.cwd().resolve()), "base_commit": COMMIT,
            "origin": "system_detector", "detector": record.provenance["detector"],
            **{field: record.content[field] for field in ("incident_id", "incident_digest", "incident_class")},
            "current_state": "PATCH_VALIDATED", "terminal": 0,
            "proposal_digest": "b" * 64, "patch_digest": "c" * 64,
        }

    def proposal(_runtime, **kwargs):
        calls["proposal"].append(kwargs)
        return {"ok": True, "transaction_id": kwargs["transaction_id"],
                "proposal_digest": "b" * 64, "patch_digest": "c" * 64}

    def evolve(**kwargs):
        calls["evolution"].append(kwargs)
        transaction_id = kwargs["opportunities"][0]["code_evolution_proposal"]["transaction_id"]
        ledger.rows[transaction_id] = transaction_row(transaction_id)
        return {"ok": True}

    def append(attempt_record, existing_match=None):
        calls.setdefault("appends", []).append(attempt_record)
        return attempt_record

    runtime = SimpleNamespace(
        store=SimpleNamespace(
            list_records_by_sources_exact_scope=lambda **kwargs: [record]
                if record.source in kwargs['sources'] and record.kind in kwargs['kinds']
                and (kwargs.get('record_ids') is None or record.record_id in kwargs['record_ids']) else [],
            append=append,
        ),
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
        lambda: (record.content['incident_digest'], "f" * 64, "l5.default"),
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
    monkeypatch.setattr('eimemory.ops.runtime_identity_drift.current_runtime_identity_incident',
                        lambda *_args, **_kwargs: record.content['incident_digest'])
    return SimpleNamespace(
        record=record,
        calls=calls,
        ledger=ledger,
        transaction_row=transaction_row,
        run=lambda: process_system_code_incidents(runtime, scope=SCOPE, repo_root=Path.cwd()),
    )


def _select_runtime_drift_incident(record):
    from eimemory.ops.runtime_identity_drift import detect_runtime_identity_drift
    report=detect_runtime_identity_drift(expected_commit=COMMIT,scope=record.scope,
        unit_environments={'fixture.service':'EIMEMORY_RUNTIME_COMMIT='+('b'*40)},
        detected_at='2026-10-01T00:00:00+00:00')
    record.source = "eimemory.runtime_identity_drift"
    record.provenance = dict(report['source_facts']['facts'])
    record.record_id=report['incident']['incident_id']
    record.meta['incident_digest']=report['incident']['incident_digest']
    record.content={**report['incident'],'detector_report':report}


def _select_system_repair_policy_incident(record):
    record.source = "eimemory.system_code_repair_failure"
    record.provenance["detector"] = "eimemory.system_code_repair_failure.v1"
    record.content["incident_class"] = "code.system_repair_policy_stale"
    record.content["detector_report"]["detector"] = "eimemory.system_code_repair_failure.v1"


@pytest.mark.parametrize("status", ["resolved", "archived", "quarantined", "", None])
def test_non_active_detector_incident_never_reaches_provider(routing_harness, status):
    routing_harness.record.status = status

    report = routing_harness.run()

    if status in ("", None):
        assert report["ok"] is False and report["reason"] == "current_incident_lifecycle_unverified"
    else:
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
        routing_harness.ledger.get_transaction = routing_harness.transaction_row

    report = routing_harness.run()

    assert report["status"] == "processed"
    assert report["repair_complete"] is False
    assert len(report["processed"]) == 1
    assert report["processed"][0]["repair_complete"] is False
    assert report["processed"][0]["attempt_record_id"]
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


@pytest.mark.parametrize("damage", ["absent", "scope", "incident", "base", "proposal", "failed", "unknown_state"])
def test_invocation_success_cannot_acknowledge_missing_or_unbound_transaction(routing_harness, damage):
    def read(transaction_id):
        if not routing_harness.calls["evolution"] or damage == "absent":
            return None
        row = routing_harness.transaction_row(transaction_id)
        field, value = {
            "scope": ("agent_id", "different-agent"),
            "incident": ("incident_digest", "e" * 64),
            "base": ("base_commit", "e" * 40),
            "proposal": ("proposal_digest", "e" * 64),
            "failed": ("current_state", "ABORTED_CANDIDATE_RESTORED"),
            "unknown_state": ("current_state", "UNKNOWN"),
        }[damage]
        row[field] = value
        return row

    routing_harness.ledger.get_transaction = read
    report = routing_harness.run()
    assert report["ok"] is False and report["status"] == "blocked"
    assert report["processed"][0]["status"] == "submission_failed"
    assert report["processed"][0]["repair_complete"] is False
    assert report["processed"][0]["attempt_record_id"]
    assert len(routing_harness.calls["evolution"]) == 1


def test_legacy_router_source_label_cannot_replace_native_current_fence(routing_harness):
    _select_system_repair_policy_incident(routing_harness.record)

    report = routing_harness.run()

    assert report["status"] == "blocked" and report['reason'] == 'current_incident_source_unverified'
    assert routing_harness.calls['proposal'] == [] and routing_harness.calls['evolution'] == []


def test_consumed_policy_for_no_matching_current_incident_is_idle(routing_harness, monkeypatch):
    monkeypatch.setattr(
        "eimemory.governance.system_code_repair._automation_policy_identity",
        lambda: ("e" * 64, "f" * 64, "l5.default"),
    )
    routing_harness.ledger.get_policy_consumption = lambda _digest: {
        "transaction_id": "older-transaction"
    }

    report = routing_harness.run()

    # Pack fix: auth/digest mismatch must not fake idle (AUDIT §7).
    assert report["ok"] is False
    assert report["status"] == "blocked"
    assert report["reason"] == "incident_policy_digest_mismatch"
    assert report["repair_complete"] is False
    assert report["pending_incident_record_ids"] == ["incident-record-current"]
    assert report["processed"] == []
    assert routing_harness.calls["proposal"] == []
    assert routing_harness.calls["evolution"] == []
