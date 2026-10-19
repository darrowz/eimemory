import json
import os
from dataclasses import asdict
from types import SimpleNamespace

import pytest

from deploy import record_release_lineage as cli
from eimemory.api.runtime import Runtime
from eimemory.evaluation.capability_catalog import CapabilityEvaluationCatalog
from eimemory.governance.release.evidence_contract import ReleaseIdentity
from eimemory.governance.release import evidence_contract
from eimemory.models.records import ScopeRef
from test_deployment_receipt import (
    _git_release_repo,
    _health_payload,
    _health_server,
    _module_trust_anchors,
    _release_link,
    _trusted_anchors,
    verify_and_record_deployment,
)
from test_release_lineage import _commit


@pytest.mark.parametrize("identity_commit", ["a" * 40, "b" * 40, None])
def test_installer_lineage_forwards_only_the_verified_current_receipt(
    tmp_path, monkeypatch, capsys, identity_commit,
):
    calls = []
    closed = []
    runtime = SimpleNamespace(
        record_release_lineage=lambda **kwargs: calls.append(kwargs) or {"ok": True},
        close=lambda: closed.append(True),
    )
    monkeypatch.setattr(cli.Runtime, "create", lambda: runtime)
    release = (
        ReleaseIdentity(commit=identity_commit, version="1.0.0",
                        receipt_id="current-receipt", session_id="current-session")
        if identity_commit else None
    )
    monkeypatch.setattr(cli, "current_release_identity", lambda *_args: release)

    result = cli.main([
        "--repo-root", str(tmp_path), "--current-commit", "a" * 40,
        "--scope-agent", "agent", "--scope-workspace", "workspace", "--scope-user", "user",
    ])

    assert closed == [True]
    if identity_commit != "a" * 40:
        assert result == 2
        assert calls == []
        assert "current_release_receipt_invalid" in capsys.readouterr().out
        return
    assert result == 0
    assert len(calls) == 1
    assert calls[0]["current_release"] == release
    assert calls[0]["gate_evidence"] == {"deployment.runtime": ["current-receipt"]}


@pytest.fixture
def native_lineage_release(tmp_path, monkeypatch):
    if os.name == "nt":
        pytest.skip("native CLI lineage uses POSIX immutable-release receipt anchors")
    repo, genesis, prior_commit = _git_release_repo(tmp_path, version="1.0.0")
    prior_release, current_link = _release_link(tmp_path, prior_commit, repo=repo)
    monkeypatch.setenv("EIMEMORY_TRUSTED_REPOSITORY_ROOT", str(repo))
    monkeypatch.setenv("EIMEMORY_DEPLOYMENT_RELEASES_ROOT", str(prior_release.parent))
    scope = ScopeRef(agent_id="agent", workspace_id="workspace", user_id="user")
    runtime = Runtime.create(root=tmp_path / "runtime")
    # Dynamic lineage requires an application-owned executable catalog. This
    # case needs no replay cases, but must still use the real sealed authority.
    runtime.capability_catalog = CapabilityEvaluationCatalog().seal()
    close = runtime.close
    try:
        runtime._test_runtime_commit = prior_commit
        with _health_server(_health_payload(commit=prior_commit, version="1.0.0",
                current_link=current_link, release_dir=prior_release)) as prior_health:
            with _trusted_anchors(repo, current_link, prior_health):
                prior = verify_and_record_deployment(runtime, scope=asdict(scope), repo_root=repo,
                    current_link=current_link, health_url=prior_health, prior_commit=genesis)
                assert prior["ok"] is True
                prior_record = runtime.store.get_by_id(prior["promotion_request_id"], scope=scope)
                prior_identity = evidence_contract.verified_deployment_receipt_identity(prior_record)
                assert prior_identity is not None and prior_identity.commit == prior_commit

        _commit(repo, "eimemory/ops/controlled_rpc_probe.py", "changed\n", "runtime and channel")
        current_commit = _commit(repo, "eimemory/evaluation/controlled_production_acceptance.py",
                                 "changed\n", "governance")
        current_link.unlink()
        release, current_link = _release_link(tmp_path, current_commit, repo=repo)
        # The deployment producer checks every tracked package source against
        # the immutable release, including the two domain changes under test.
        for relative in ("eimemory/ops/controlled_rpc_probe.py",
                         "eimemory/evaluation/controlled_production_acceptance.py"):
            target = release / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes((repo / relative).read_bytes())
        runtime._test_runtime_commit = current_commit
        with _health_server(_health_payload(commit=current_commit, version="1.0.0",
                current_link=current_link, release_dir=release)) as health_url:
            with _trusted_anchors(repo, current_link, health_url), _module_trust_anchors(repo, current_link, health_url):
                deployed = verify_and_record_deployment(runtime, scope=asdict(scope), repo_root=repo,
                    current_link=current_link, health_url=health_url, prior_commit=prior_commit)
                assert deployed["ok"] is True, deployed
                record = runtime.store.get_by_id(deployed["promotion_request_id"], scope=scope)
                current = evidence_contract.verified_deployment_receipt_identity(record)
                assert current is not None and current.commit == current_commit
                assert record.content["side_effect"]["deployment"]["current_link"] == str(current_link)
                assert record.content["side_effect"]["post_deploy_health"]["url"] == health_url
                yield runtime, scope, repo, current
    finally:
        close()


def test_installer_closes_deployment_without_inventing_other_domain_evidence(
    native_lineage_release, monkeypatch, capsys,
):
    runtime, scope, repo, current = native_lineage_release
    monkeypatch.setattr(Runtime, "create", lambda: runtime)
    monkeypatch.setattr(runtime, "close", lambda: None)

    assert cli.main([
        "--repo-root", str(repo), "--current-commit", current.commit,
        "--scope-agent", "agent", "--scope-workspace", "workspace", "--scope-user", "user",
    ]) == 0
    recorded = json.loads(capsys.readouterr().out)
    verified = runtime.current_release_lineage(
        scope=scope, repo_root=str(repo), current_release=current,
    )
    assert verified["validated"] is True
    assert verified["record_id"] == recorded["record_id"]
    physical = runtime.store.get_by_id(recorded["record_id"], scope=scope)
    assert physical.source == "eimemory.release_lineage"
    assert physical.content["lineage"] == {
        key: recorded[key] for key in physical.content["lineage"]
    }
    assert verified["domains"]["deployment.runtime"]["mode"] == "current"
    assert verified["domains"]["deployment.runtime"]["gate_evidence"] == [current.receipt_id]
    for domain in ("memory.governance", "channel.delivery"):
        assert verified["domains"][domain]["mode"] == "changed_unverified"
        assert verified["domains"][domain]["gate_evidence"] == []
    assert verified["compatible"] is False


def test_installer_lineage_rejects_missing_application_catalog(
    native_lineage_release, monkeypatch, capsys,
):
    runtime, scope, repo, current = native_lineage_release
    monkeypatch.setattr(Runtime, "create", lambda: runtime)
    monkeypatch.setattr(runtime, "close", lambda: None)
    # Remove both possible authorities, keeping the production resolver intact.
    runtime.capability_catalog = None
    monkeypatch.setattr("eimemory.evaluation.capability_catalog._APPLICATION_CATALOG", None)
    assert cli.main([
        "--repo-root", str(repo), "--current-commit", current.commit,
        "--scope-agent", "agent", "--scope-workspace", "workspace", "--scope-user", "user",
    ]) == 2
    assert json.loads(capsys.readouterr().out) == {
        "ok": False, "error": "evaluation_catalog_untrusted",
    }
    assert runtime.store.list_records(kinds=["l5_self_continuity"], scope=scope, limit=10) == []
