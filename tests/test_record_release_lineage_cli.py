import json
from types import SimpleNamespace

import pytest

from deploy import record_release_lineage as cli
from eimemory.api.runtime import Runtime
from eimemory.governance.release.evidence_contract import ReleaseIdentity
from eimemory.models.records import ScopeRef
from test_release_lineage import _commit, _receipt, _repo


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


def test_installer_closes_deployment_without_inventing_other_domain_evidence(
    tmp_path, monkeypatch, capsys,
):
    repo = _repo(tmp_path)
    prior_commit = _commit(repo, "docs/prior.md", "prior\n", "prior")
    _commit(repo, "eimemory/ops/controlled_rpc_probe.py", "changed\n", "runtime and channel")
    current_commit = _commit(repo, "eimemory/evaluation/controlled_production_acceptance.py",
                             "changed\n", "governance")
    scope = ScopeRef(agent_id="agent", workspace_id="workspace", user_id="user")
    runtime = Runtime.create(root=tmp_path / "runtime")
    close = runtime.close
    monkeypatch.setattr(Runtime, "create", lambda: runtime)
    monkeypatch.setattr(runtime, "close", lambda: None)
    try:
        _receipt(runtime, scope, prior_commit, "1.0.0")
        current = _receipt(runtime, scope, current_commit, "1.0.1")
        runtime._test_runtime_commit = current_commit

        assert cli.main([
            "--repo-root", str(repo), "--current-commit", current_commit,
            "--scope-agent", "agent", "--scope-workspace", "workspace", "--scope-user", "user",
        ]) == 0
        recorded = json.loads(capsys.readouterr().out)
        verified = runtime.current_release_lineage(
            scope=scope, repo_root=str(repo), current_release=current,
        )
        assert verified["validated"] is True
        assert verified["record_id"] == recorded["record_id"]
        assert verified["domains"]["deployment.runtime"]["mode"] == "current"
        assert verified["domains"]["deployment.runtime"]["gate_evidence"] == [current.receipt_id]
        for domain in ("memory.governance", "channel.delivery"):
            assert verified["domains"][domain]["mode"] == "changed_unverified"
            assert verified["domains"][domain]["gate_evidence"] == []
        assert verified["compatible"] is False
    finally:
        close()
