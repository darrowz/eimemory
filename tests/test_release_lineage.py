from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
import subprocess

import pytest

from eimemory.api.runtime import Runtime
from eimemory.governance.evidence_contract import (
    ReleaseIdentity,
    verified_deployment_receipt_identity,
)
from eimemory.governance.release_lineage import (
    current_release_lineage,
    evidence_release_for_domain,
    record_release_lineage,
)
from eimemory.models.records import RecordEnvelope, ScopeRef


SCOPE = ScopeRef(
    tenant_id="tenant-1",
    agent_id="hongtu",
    workspace_id="embodied",
    user_id="darrow",
)


def test_unchanged_domain_inherits_newest_verified_ancestor_release(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    prior_commit = _commit(repo, "eimemory/retrieval/engine.py", "prior\n", "prior")
    intermediate = _commit(repo, "docs/note.md", "unreceipted\n", "intermediate")
    current_commit = _commit(repo, "CHANGELOG.md", "current\n", "current")
    runtime = Runtime.create(root=tmp_path / "runtime")
    try:
        prior = _receipt(runtime, SCOPE, prior_commit, "1.0.0")
        _receipt(runtime, SCOPE, intermediate, "1.0.1", forged=True)
        current = _receipt(runtime, SCOPE, current_commit, "1.0.2")
        runtime._test_runtime_commit = current.commit

        recorded = record_release_lineage(
            runtime,
            scope=SCOPE,
            repo_root=repo,
            current_release=current,
        )
        resolved = current_release_lineage(
            runtime,
            scope=SCOPE,
            repo_root=repo,
            current_release=current,
        )

        assert recorded["ok"] is True
        assert recorded["ancestor_release"]["commit"] == prior.commit
        assert resolved["ok"] is True
        assert resolved["domains"]["memory.recall"]["mode"] == "inherited"
        assert evidence_release_for_domain(resolved, "memory.recall", current) == prior
    finally:
        runtime.close()


def test_changed_domain_requires_exact_current_gate_evidence(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    prior_commit = _commit(repo, "eimemory/retrieval/engine.py", "prior\n", "prior")
    current_commit = _commit(repo, "eimemory/retrieval/engine.py", "changed\n", "current")
    runtime = Runtime.create(root=tmp_path / "runtime")
    try:
        _receipt(runtime, SCOPE, prior_commit, "1.0.0")
        current = _receipt(runtime, SCOPE, current_commit, "1.0.1")
        runtime._test_runtime_commit = current.commit

        missing = record_release_lineage(
            runtime,
            scope=SCOPE,
            repo_root=repo,
            current_release=current,
        )
        assert missing["ok"] is True
        assert missing["compatible"] is False
        assert missing["domains"]["memory.recall"]["mode"] == "changed_unverified"
        with pytest.raises(ValueError, match="not inheritable"):
            evidence_release_for_domain(missing, "memory.recall", current)

        gate = _gate(runtime, SCOPE, current, source="eimemory.evaluation.production_recall")
        refreshed = record_release_lineage(
            runtime,
            scope=SCOPE,
            repo_root=repo,
            current_release=current,
            gate_evidence={"memory.recall": [gate.record_id]},
        )
        assert refreshed["domains"]["memory.recall"]["mode"] == "current"
        assert evidence_release_for_domain(refreshed, "memory.recall", current) == current
    finally:
        runtime.close()


def test_unknown_production_path_marks_every_domain_changed(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    prior_commit = _commit(repo, "eimemory/retrieval/engine.py", "prior\n", "prior")
    current_commit = _commit(repo, "eimemory/new_runtime_surface.py", "changed\n", "current")
    runtime = Runtime.create(root=tmp_path / "runtime")
    try:
        _receipt(runtime, SCOPE, prior_commit, "1.0.0")
        current = _receipt(runtime, SCOPE, current_commit, "1.0.1")
        runtime._test_runtime_commit = current.commit

        report = record_release_lineage(runtime, scope=SCOPE, repo_root=repo, current_release=current)

        assert report["unknown_production_paths"] == ["eimemory/new_runtime_surface.py"]
        assert {value["mode"] for value in report["domains"].values()} == {"changed_unverified"}
    finally:
        runtime.close()


def test_non_ancestor_receipt_is_rejected(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    current_commit = _commit(repo, "docs/current.md", "current\n", "current")
    descendant_commit = _commit(repo, "docs/later.md", "later\n", "later")
    runtime = Runtime.create(root=tmp_path / "runtime")
    try:
        _receipt(runtime, SCOPE, descendant_commit, "1.0.1")
        current = _receipt(runtime, SCOPE, current_commit, "1.0.0")
        runtime._test_runtime_commit = current.commit

        report = record_release_lineage(runtime, scope=SCOPE, repo_root=repo, current_release=current)

        assert report == {"ok": False, "error": "verified_ancestor_receipt_not_found"}
    finally:
        runtime.close()


@pytest.mark.parametrize("failure", ["forged_receipt", "scope_mismatch"])
def test_unverified_or_cross_scope_receipt_cannot_inherit(tmp_path: Path, failure: str) -> None:
    repo = _repo(tmp_path)
    prior_commit = _commit(repo, "eimemory/retrieval/engine.py", "prior\n", "prior")
    current_commit = _commit(repo, "docs/current.md", "current\n", "current")
    runtime = Runtime.create(root=tmp_path / "runtime")
    try:
        prior_scope = (
            ScopeRef(**{**asdict(SCOPE), "user_id": "other-user"})
            if failure == "scope_mismatch"
            else SCOPE
        )
        _receipt(
            runtime,
            prior_scope,
            prior_commit,
            "1.0.0",
            forged=failure == "forged_receipt",
        )
        current = _receipt(runtime, SCOPE, current_commit, "1.0.1")
        runtime._test_runtime_commit = current.commit

        report = record_release_lineage(runtime, scope=SCOPE, repo_root=repo, current_release=current)

        assert report == {"ok": False, "error": "verified_ancestor_receipt_not_found"}
    finally:
        runtime.close()


def test_current_lineage_recomputes_stored_classification(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    prior_commit = _commit(repo, "eimemory/retrieval/engine.py", "prior\n", "prior")
    current_commit = _commit(repo, "docs/current.md", "current\n", "current")
    runtime = Runtime.create(root=tmp_path / "runtime")
    try:
        _receipt(runtime, SCOPE, prior_commit, "1.0.0")
        current = _receipt(runtime, SCOPE, current_commit, "1.0.1")
        runtime._test_runtime_commit = current.commit
        recorded = record_release_lineage(runtime, scope=SCOPE, repo_root=repo, current_release=current)
        stored = runtime.store.get_by_id(recorded["record_id"], scope=SCOPE)
        assert stored is not None
        stored.content["lineage"]["domains"]["memory.recall"]["mode"] = "current"
        runtime.store.rewrite(stored)

        resolved = current_release_lineage(
            runtime,
            scope=SCOPE,
            repo_root=repo,
            current_release=current,
        )

        assert resolved == {"ok": False, "error": "lineage_attestation_mismatch"}
    finally:
        runtime.close()


def test_codex_or_hermes_gate_record_cannot_authorize_openclaw_lineage(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    prior_commit = _commit(repo, "integrations/openclaw/index.ts", "prior\n", "prior")
    current_commit = _commit(repo, "integrations/openclaw/index.ts", "changed\n", "current")
    runtime = Runtime.create(root=tmp_path / "runtime")
    try:
        _receipt(runtime, SCOPE, prior_commit, "1.0.0")
        current = _receipt(runtime, SCOPE, current_commit, "1.0.1")
        runtime._test_runtime_commit = current.commit
        gate = _gate(runtime, SCOPE, current, source="codex.stop")

        report = record_release_lineage(
            runtime,
            scope=SCOPE,
            repo_root=repo,
            current_release=current,
            gate_evidence={"channel.openclaw": [gate.record_id]},
        )

        domain = report["domains"]["channel.openclaw"]
        assert domain["mode"] == "changed_unverified"
        assert domain["gate_errors"] == {gate.record_id: "source_not_authorized_for_domain"}
    finally:
        runtime.close()


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "tests@example.invalid")
    _git(repo, "config", "user.name", "Lineage Tests")
    return repo


def _commit(repo: Path, relative_path: str, content: str, message: str) -> str:
    path = repo / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    _git(repo, "add", relative_path)
    _git(repo, "commit", "-m", message)
    return _git(repo, "rev-parse", "HEAD").stdout.strip().lower()


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    )


def _receipt(
    runtime: Runtime,
    scope: ScopeRef,
    commit: str,
    version: str,
    *,
    forged: bool = False,
) -> ReleaseIdentity:
    release_path = f"/opt/eimemory/releases/{commit}"
    record = RecordEnvelope.create(
        kind="promotion_request",
        title="Deployment receipt",
        scope=scope,
        source="eimemory.deployment_receipt",
        status="deployed",
        content={
            "report_type": "deployment_receipt",
            "promotion_target": "code_patch",
            "action": "code_patch",
            "gate": {"ok": True, "receipt_verified": not forged},
            "side_effect": {
                "ok": True,
                "production_applied": True,
                "deployment_executed": True,
                "verification": {"ok": True, "skipped": False},
                "deployment": {"ok": True, "skipped": False, "release_path": release_path},
                "post_deploy_health": {
                    "ok": True,
                    "skipped": False,
                    "commit": commit,
                    "version": version,
                    "release_path": release_path,
                },
                "commit": {"commit_sha": commit},
                "release": {"version": version, "release_path": release_path},
                "rollback_evidence": {
                    "prior_commit_sha": "f" * 40,
                    "rollback_command": "verified rollback",
                },
            },
        },
        meta={"report_type": "deployment_receipt"},
    )
    record = runtime.store.append(record)
    identity = verified_deployment_receipt_identity(record)
    if forged:
        assert identity is None
        return ReleaseIdentity(commit=commit, version=version, receipt_id=record.record_id, session_id=record.record_id)
    assert identity is not None
    return identity


def _gate(
    runtime: Runtime,
    scope: ScopeRef,
    release: ReleaseIdentity,
    *,
    source: str,
) -> RecordEnvelope:
    return runtime.store.append(
        RecordEnvelope.create(
            kind="learning_eval",
            title="Current release gate",
            scope=scope,
            source=source,
            status="active",
            content={
                "report_type": "test_gate",
                "passed": True,
                "release_commit": release.commit,
                "release_version": release.version,
                "deployment_receipt_id": release.receipt_id,
                "release_session_id": release.session_id,
            },
            meta={"report_type": "test_gate"},
        )
    )
