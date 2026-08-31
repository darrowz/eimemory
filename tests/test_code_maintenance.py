from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace

import pytest

from eimemory.governance import code_maintenance as maintenance
from eimemory.models.records import ScopeRef


SCOPE = dict(tenant_id="default", agent_id="hongtu", workspace_id="embodied", user_id="darrow")
BASE = "a" * 40
TREE = "b" * 64
REMOTE = "c" * 64


@pytest.fixture
def harness(monkeypatch):
    records = {}
    calls = dict(proposal=[], submit=[], append=[])

    def append(record):
        records[record.record_id] = record
        calls["append"].append(record)
        return record

    store = SimpleNamespace(
        append=append,
        get_by_id=lambda record_id, **kwargs: records.get(record_id),
        list_records_by_meta_value=lambda **kwargs: [
            row for row in records.values()
            if row.meta.get(kwargs["meta_key"]) == kwargs["meta_value"]
            and asdict(row.scope) == asdict(kwargs["scope"])
        ],
    )
    runtime = SimpleNamespace(store=store)
    context = dict(ok=True, base_commit=BASE, base_tree_digest=TREE,
                   remote_url_digest=REMOTE, repository_root=str(Path.cwd()), repository_ref="master")
    monkeypatch.setattr(maintenance, "_repository_context", lambda *_args, **_kwargs: dict(context))
    from eimemory.adapters.hermes import code_implementation as provider
    policy = dict(
        ok=True, status="enabled", policy_digest="d" * 64,
        incident=dict(**{"class": maintenance.INCIDENT_CLASS}, detector_id=maintenance.DETECTOR_ID),
        capability=dict(profile_key="l5.default", capability_id=provider.CAPABILITY_ID,
                        revision_id=provider.REVISION_ID, binding_id=provider.BINDING_ID,
                        implementation_digest=provider.IMPLEMENTATION_DIGEST, operation=provider.OPERATION),
        repository=dict(root=str(Path.cwd()), remote="origin", branch="master", base_commit=BASE,
                        base_tree_digest=TREE, remote_url_digest=REMOTE),
        patch=dict(allowed_files=["eimemory/governance/system_code_repair.py"]),
        verification=dict(test_plan_id=maintenance.INCIDENT_ROUTING_REPAIR_TEST_PLAN_ID,
                          test_plan_digest="e" * 64, full_suite_required=True),
        effects={name: True for name in ("commit", "push", "deployment", "rollback", "sedimentation")},
    )
    monkeypatch.setattr(maintenance, "load_code_automation_policy", lambda **kwargs: deepcopy(policy))
    monkeypatch.setattr(maintenance, "protected_test_plan_digest", lambda _plan_id: "e" * 64)
    monkeypatch.setattr(maintenance, "allowed_files_for_incident", lambda *_args, **_kwargs: tuple(policy["patch"]["allowed_files"]))
    ledger = SimpleNamespace(get_policy_consumption=lambda _digest: None,
                             get_transaction=lambda _transaction_id: None)
    monkeypatch.setattr(maintenance, "CodeEvolutionStore", lambda _store: ledger)
    monkeypatch.setattr(maintenance, "_repository_blocker", lambda *_args: None)
    monkeypatch.setattr("eimemory.capabilities.profiles.CapabilityProfiles.resolve", lambda *_args, **_kwargs: None)

    def propose(_runtime, **kwargs):
        calls["proposal"].append(kwargs)
        return dict(ok=True, transaction_id=kwargs["transaction_id"], qualifying=True,
                    origin=kwargs["origin"], known_before_detection=kwargs["known_before_detection"],
                    prior_user_reported=kwargs["prior_user_reported"], manual_bootstrap=kwargs["manual_bootstrap"])

    def submit(_manager, proposal, **kwargs):
        calls["submit"].append((proposal, kwargs))
        return dict(ok=True, transaction_id=proposal["transaction_id"], transaction=dict(current_state="OBSERVING"))

    monkeypatch.setattr(maintenance, "propose_code_patch_v2", propose)
    monkeypatch.setattr(maintenance, "CodeEvolutionTransactionManager", lambda *_args, **_kwargs: SimpleNamespace(
        submit_proposal=lambda proposal, **kwargs: submit(None, proposal, **kwargs)))
    return SimpleNamespace(runtime=runtime, records=records, calls=calls, context=context, policy=policy, ledger=ledger)


def _record(harness):
    result = maintenance.record_code_maintenance(
        harness.runtime, scope=SCOPE, base_commit=BASE, repo_root=Path.cwd(),
        title="Reject stale incident routing", summary="Resolved and old-release incidents can reach the provider.",
        evidence=["Read-only reproduction: resolved record passed routing; no provider effects executed."],
    )
    harness.policy["incident"]["incident_digest"] = result["incident"]["incident_digest"]
    return result


def _process(harness, record_id):
    return maintenance.process_code_maintenance(
        harness.runtime, scope=SCOPE, record_id=record_id, repo_root=Path.cwd())


def test_maintenance_incident_and_source_only_plan_pass_real_v9_request_contract():
    from hashlib import sha256

    from eimemory.adapters.hermes.code_implementation import build_request, validate_request
    from eimemory.governance.code_evolution_repository import protected_paths_digest
    from eimemory.governance.code_evolution_test_plans import (
        allowed_files_for_incident,
        protected_test_plan_digest,
    )

    root = Path(__file__).resolve().parents[1]
    report = dict(schema=maintenance.SCHEMA, scope=SCOPE, base_commit=BASE,
                  repository_root="/dev-project/eimemory", title="Reject stale incident routing",
                  summary="Reject inactive incidents and mismatched detector release identity before proposing a repair.",
                  evidence=["Known user-authorized repair with protected failure reproductions."])
    incident = maintenance._incident(report)
    plan_id = maintenance.INCIDENT_ROUTING_REPAIR_TEST_PLAN_ID
    paths = allowed_files_for_incident(incident["incident_class"], test_plan_id=plan_id)
    sources = []
    for relative in paths:
        source = (root / relative).read_bytes().replace(b"\r\n", b"\n")
        sources.append(dict(path=relative, sha256=sha256(source).hexdigest(), content=source.decode("utf-8")))

    request = build_request(
        transaction_id="maintenance-contract-test", request_id="maintenance-request-test",
        nonce="maintenance-nonce-test", incident=incident,
        base=dict(commit=BASE, tree_digest=protected_paths_digest(root, paths)),
        allowed_files=sources,
        bounds=dict(maximum_files=1, maximum_bytes_per_file=49152,
                    maximum_total_bytes=98304, maximum_changed_lines=400),
        test_plan_id=plan_id, test_plan_digest=protected_test_plan_digest(plan_id),
    )

    validated = validate_request(request)
    assert validated["incident"] == incident
    assert validated["test_plan_id"] == plan_id
    assert [item["path"] for item in validated["allowed_files"]] == [
        "eimemory/governance/system_code_repair.py"
    ]


def test_record_retains_honest_provenance_and_is_idempotent(harness):
    first = _record(harness)
    second = _record(harness)
    assert first["ok"] is True
    assert second["record_id"] == first["record_id"]
    assert len(harness.calls["append"]) == 1
    record = harness.records[first["record_id"]]
    assert record.source == maintenance.SOURCE
    assert record.content["maintenance_report"]["base_commit"] == BASE
    assert record.provenance == dict(origin="user_reported", detector=maintenance.DETECTOR_ID,
        known_before_detection=True, prior_user_reported=True, manual_bootstrap=False)
    assert first["qualifies_for_product_completion"] is False
    assert "detector_report" not in record.content


@pytest.mark.parametrize("base", ["", "a" * 7, "b" * 40])
def test_record_requires_current_full_base(harness, base):
    result = maintenance.record_code_maintenance(harness.runtime, scope=SCOPE, base_commit=base,
        title="Routing defect", summary="Known failure", evidence=["read-only proof"], repo_root=Path.cwd())
    assert result["ok"] is False
    assert harness.calls["append"] == []


def test_strict_submission_preserves_provider_eligibility_but_not_l5_qualification(harness):
    recorded = _record(harness)
    report = _process(harness, recorded["record_id"])
    assert report["ok"] is True
    assert report["status"] == "submitted"
    assert report["qualifies_for_product_completion"] is False
    assert len(harness.calls["proposal"]) == len(harness.calls["submit"]) == 1
    proposal = harness.calls["proposal"][0]
    assert proposal["origin"] == "user_reported"
    assert proposal["known_before_detection"] is proposal["prior_user_reported"] is True
    assert proposal["manual_bootstrap"] is False
    assert proposal["allowed_files"] == ("eimemory/governance/system_code_repair.py",)
    assert proposal["test_plan_id"] == maintenance.INCIDENT_ROUTING_REPAIR_TEST_PLAN_ID
    submitted, options = harness.calls["submit"][0]
    assert submitted["qualifying"] is True
    assert options == dict(scope=SCOPE, effects_enabled=True, apply=True)


@pytest.mark.parametrize("mutation", [
    "resolved", "wrong_source", "wrong_scope", "system_origin", "known_false", "prior_false",
    "manual_true", "wrong_detector", "tampered_evidence", "tampered_base", "tampered_digest", "numeric_boolean",
])
def test_maintenance_record_must_remain_exactly_bound(harness, mutation):
    recorded = _record(harness)
    record = harness.records[recorded["record_id"]]
    if mutation == "resolved":
        record.status = "resolved"
    elif mutation == "wrong_source":
        record.source = "eimemory.release_closure_failure"
    elif mutation == "wrong_scope":
        record.scope = ScopeRef.from_dict({**SCOPE, "user_id": "other"})
    elif mutation == "system_origin":
        record.provenance["origin"] = "system_detector"
    elif mutation == "known_false":
        record.provenance["known_before_detection"] = False
    elif mutation == "prior_false":
        record.provenance["prior_user_reported"] = False
    elif mutation == "manual_true":
        record.provenance["manual_bootstrap"] = True
    elif mutation == "wrong_detector":
        record.provenance["detector"] = "system_detector"
    elif mutation == "numeric_boolean":
        record.provenance["known_before_detection"] = 1
    elif mutation == "tampered_evidence":
        record.content["maintenance_report"]["evidence"] = ["unbound replacement"]
    elif mutation == "tampered_base":
        record.content["maintenance_report"]["base_commit"] = "b" * 40
    else:
        record.meta["incident_digest"] = "f" * 64
    report = _process(harness, recorded["record_id"])
    assert report["ok"] is False
    assert harness.calls["proposal"] == harness.calls["submit"] == []


@pytest.mark.parametrize("section,field,value", [
    ("incident", "class", "release.closure_internal_failure"),
    ("incident", "detector_id", "system_detector"),
    ("incident", "incident_digest", "f" * 64),
    ("repository", "base_commit", "b" * 40),
    ("repository", "base_tree_digest", "f" * 64),
    ("repository", "remote_url_digest", "f" * 64),
    ("capability", "revision_id", "code.implementation:v8"),
    ("capability", "binding_id", "wrong"),
    ("capability", "implementation_digest", "f" * 64),
    ("verification", "test_plan_digest", "f" * 64),
    ("verification", "full_suite_required", False),
    ("effects", "rollback", False),
])
def test_policy_mismatch_never_calls_provider(harness, section, field, value):
    recorded = _record(harness)
    harness.policy[section][field] = value
    report = _process(harness, recorded["record_id"])
    assert report["ok"] is False
    assert harness.calls["proposal"] == harness.calls["submit"] == []


@pytest.mark.parametrize("blocker", ["consumed", "active", "quarantined", "profile", "release"])
def test_other_prerequisites_fail_before_provider(harness, monkeypatch, blocker):
    recorded = _record(harness)
    if blocker == "consumed":
        harness.ledger.get_policy_consumption = lambda _digest: {"transaction_id": "old"}
    elif blocker in {"active", "quarantined"}:
        monkeypatch.setattr(maintenance, "_repository_blocker", lambda *_args: {"transaction_id": "other"})
    elif blocker == "profile":
        from eimemory.capabilities.profiles import CapabilityProfileError
        def missing(*_args, **_kwargs):
            raise CapabilityProfileError("missing")
        monkeypatch.setattr("eimemory.capabilities.profiles.CapabilityProfiles.resolve", missing)
    else:
        harness.context["ok"] = False
        harness.context["reason"] = "repository_release_identity_mismatch"
    result = _process(harness, recorded["record_id"])
    assert result["ok"] is False
    assert harness.calls["proposal"] == harness.calls["submit"] == []


def test_only_explicit_record_is_processed(harness):
    _record(harness)
    report = _process(harness, "missing")
    assert report["ok"] is False
    assert harness.calls["proposal"] == harness.calls["submit"] == []


def test_provider_failure_does_not_create_transaction(harness, monkeypatch):
    record = _record(harness)
    monkeypatch.setattr(maintenance, "propose_code_patch_v2", lambda *_args, **_kwargs: dict(ok=False, reason="provider_invalid"))
    report = _process(harness, record["record_id"])
    assert report["status"] == "proposal_blocked"
    assert report["reason"] == "provider_invalid"
    assert harness.calls["submit"] == []


def test_changed_authority_after_provider_never_submits(harness, monkeypatch):
    record = _record(harness)
    original = maintenance.propose_code_patch_v2

    def propose(*args, **kwargs):
        result = original(*args, **kwargs)
        harness.policy["policy_digest"] = "f" * 64
        return result

    monkeypatch.setattr(maintenance, "propose_code_patch_v2", propose)
    result = _process(harness, record["record_id"])
    assert result["reason"] == "maintenance_authority_changed_during_proposal"
    assert harness.calls["submit"] == []


def test_disallowed_file_policy_never_reaches_provider(harness, monkeypatch):
    record = _record(harness)
    monkeypatch.setattr(maintenance, "allowed_files_for_incident", lambda *_args, **_kwargs:
        ("eimemory/governance/system_code_repair.py",))
    harness.policy["patch"]["allowed_files"] = ["eimemory/governance/l5_reader.py"]
    result = _process(harness, record["record_id"])
    assert result["reason"] == "maintenance_policy_allowed_files_mismatch"
    assert harness.calls["proposal"] == harness.calls["submit"] == []


def test_real_store_persistence_and_uncapped_repository_lock(tmp_path, monkeypatch):
    from eimemory.api.runtime import Runtime
    from eimemory.storage.code_evolution_store import CodeEvolutionStore
    runtime = Runtime.create(root=tmp_path / "runtime")
    context = dict(ok=True, base_commit=BASE, base_tree_digest=TREE, remote_url_digest=REMOTE,
                   repository_root=str(Path.cwd()), repository_ref="master")
    monkeypatch.setattr(maintenance, "_repository_context", lambda *_args, **_kwargs: dict(context))
    kwargs = dict(scope=SCOPE, base_commit=BASE, title="Known routing error", summary="Current known routing defect",
                  evidence=["Read-only diagnostic evidence"], repo_root=Path.cwd())
    try:
        recorded = maintenance.record_code_maintenance(runtime, **kwargs)
        reread = runtime.store.get_by_id(recorded["record_id"], scope=ScopeRef.from_dict(SCOPE))
        assert maintenance._trusted_record(reread, ScopeRef.from_dict(SCOPE), context) == recorded["incident"]
        assert maintenance.record_code_maintenance(runtime, **kwargs)["idempotent"] is True
        ledger = CodeEvolutionStore(runtime.store)
        assert maintenance._repository_blocker(ledger, str(Path.cwd())) is None
        ledger.create_transaction(dict(transaction_id="maintenance-lock", idempotency_key="maintenance-lock",
            scope=SCOPE, incident=recorded["incident"], origin="user_reported", detector=maintenance.DETECTOR_ID,
            known_before_detection=True, prior_user_reported=True, manual_bootstrap=False,
            repository=dict(root=str(Path.cwd()), remote="origin", ref="master", base_commit=BASE)))
        assert maintenance._repository_blocker(ledger, str(Path.cwd()))["transaction_id"] == "maintenance-lock"
        assert maintenance._repository_blocker(ledger, "/different-repository") is None
    finally:
        runtime.close()
