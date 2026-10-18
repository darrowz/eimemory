"""Lineage uses the original owner of a native, service-verified receipt."""
from dataclasses import asdict, replace
import os

import pytest

from eimemory.api.runtime import Runtime
from eimemory.core.clock import now_iso
from eimemory.evaluation.capability_catalog import CapabilityEvaluationCatalog
from eimemory.governance.l5.l5_reader import _code_evolution_evidence
from eimemory.governance.release import evidence_contract
from eimemory.models.records import RecordEnvelope, ScopeRef
from test_deployment_receipt import (
    _git_release_repo,
    _health_payload,
    _health_server,
    _module_trust_anchors,
    _release_link,
    _trusted_anchors,
    verify_and_record_deployment,
)


@pytest.fixture
def native_receipt(tmp_path, monkeypatch):
    repo, prior, commit = _git_release_repo(tmp_path, version="9.8.7")
    release, current = _release_link(tmp_path, commit, repo=repo)
    monkeypatch.setenv("EIMEMORY_TRUSTED_REPOSITORY_ROOT", str(repo))
    monkeypatch.setenv("EIMEMORY_DEPLOYMENT_RELEASES_ROOT", str(release.parent))
    owner = ScopeRef(tenant_id="default", agent_id="hongrui",
                     workspace_id="embodied::channel::hermes", user_id="business-user")
    report_owner = ScopeRef(tenant_id="default", agent_id="hongrui",
                            workspace_id="embodied", user_id="darrow")
    runtime = Runtime.create(root=tmp_path / "runtime")
    runtime._test_runtime_commit = commit
    monkeypatch.setattr(
        "eimemory.adapters.hermes.code_implementation.resolve_code_implementation_provider",
        lambda *args, **kwargs: {"provider_ready": None},
    )
    try:
        with _health_server(_health_payload(commit=commit, version="9.8.7",
                current_link=current, release_dir=release)) as health_url:
            with _trusted_anchors(repo, current, health_url), _module_trust_anchors(repo, current, health_url):
                deployment = verify_and_record_deployment(runtime, scope=asdict(owner), repo_root=repo,
                    current_link=current, health_url=health_url, prior_commit=prior)
                assert deployment["ok"] is True
                receipt = runtime.store.get_by_id(deployment["promotion_request_id"], scope=owner)
                assert evidence_contract.verified_deployment_receipt_identity(receipt) is not None
                yield runtime, owner, report_owner, repo, receipt
    finally:
        runtime.close()


def _lineage(runtime, scope, repo):
    return _code_evolution_evidence(runtime, runtime_scope=scope, evidence_scope=scope,
        capability_scope="global", checked_at=now_iso(), repo_root=str(repo),
        catalog=CapabilityEvaluationCatalog().seal())[2]


def test_native_exact_owner_receipt_keeps_full_lineage_gate(native_receipt):
    runtime, owner, _, repo, receipt = native_receipt
    identity = evidence_contract.current_release_identity(runtime, owner)
    assert identity is not None and identity.receipt_id == receipt.record_id
    result = _lineage(runtime, owner, repo)
    # A genuine receipt proves release identity; it does not invent an attestation.
    assert result["ok"] is False
    assert result["error"] == "current_release_lineage_not_found"
    assert result["report_scope"] == asdict(owner)
    assert "shared_from_product_scope" not in result


@pytest.mark.skipif(os.name == "nt", reason="shared service receipt uses POSIX immutable-release path anchors")
def test_native_shared_service_receipt_selects_physical_lineage_owner(native_receipt):
    runtime, owner, report_owner, repo, receipt = native_receipt
    identity = evidence_contract.current_release_identity(runtime, report_owner)
    assert identity is not None and identity.receipt_id == receipt.record_id
    assert evidence_contract.release_owner_scope(runtime, report_owner, identity) == report_owner
    runtime.store.append(RecordEnvelope.create(kind="memory", title="Owner private business memory", scope=owner))
    result = _lineage(runtime, report_owner, repo)
    assert result["ok"] is False
    assert result["error"] == "current_release_lineage_not_found"
    assert result["report_scope"] == asdict(report_owner)
    assert result["shared_from_product_scope"] is True
    assert runtime.store.get_by_id(receipt.record_id, scope=report_owner) is None
    assert runtime.store.list_records(kinds=["memory"], scope=report_owner, limit=10) == []


@pytest.mark.skipif(os.name == "nt", reason="shared service receipt uses POSIX immutable-release path anchors")
def test_shared_receipt_with_wrong_service_anchor_is_not_reused(native_receipt, monkeypatch):
    runtime, _, report_owner, repo, _ = native_receipt
    assert evidence_contract.current_release_identity(runtime, report_owner) is not None
    from eimemory.governance.release import deployment_receipt

    monkeypatch.setattr(deployment_receipt, "DEFAULT_DEPLOYMENT_HEALTH_URL", "http://127.0.0.1:1/health")
    assert evidence_contract.current_release_identity(runtime, report_owner) is None
    result = _lineage(runtime, report_owner, repo)
    assert result["ok"] is False
    assert result["reason"] == "current_release_identity_unavailable"


@pytest.mark.parametrize("field,value", [("session_id", "wrong-session"), ("commit", "f" * 40)])
def test_cached_current_identity_cannot_replace_native_receipt_authority(native_receipt, monkeypatch, field, value):
    runtime, owner, _, repo, receipt = native_receipt
    actual = evidence_contract.verified_deployment_receipt_identity(receipt)
    assert actual is not None
    monkeypatch.setattr(evidence_contract, "current_release_identity",
                        lambda *args, **kwargs: replace(actual, **{field: value}))
    result = _lineage(runtime, owner, repo)
    assert result["ok"] is False
    assert result["error"] == "current_release_receipt_invalid"


def test_damaged_native_receipt_cannot_be_repaired_by_scope_resolution(native_receipt):
    runtime, owner, _, repo, receipt = native_receipt
    receipt.content["gate"]["receipt_verified"] = False
    with runtime.store._lock:
        runtime.store.sqlite.upsert(receipt)
    result = _lineage(runtime, owner, repo)
    assert result["ok"] is False
    assert result["reason"] == "current_release_identity_unavailable"
