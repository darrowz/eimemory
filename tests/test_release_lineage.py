from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
import subprocess

import pytest

import eimemory.evaluation.real_query_gate as real_query_gate
import eimemory.governance.l5_readiness as l5_readiness
import eimemory.governance.release_lineage as release_lineage
import eimemory.models.records as record_models
from eimemory.api.runtime import Runtime
from eimemory.governance.evidence_contract import (
    ReleaseIdentity,
    verified_deployment_receipt_identity,
)
from eimemory.governance.release_lineage import (
    current_release_lineage as _current_release_lineage,
    evidence_release_for_domain as _evidence_release_for_domain,
    record_release_lineage as _record_release_lineage,
)
from eimemory.governance.live_task_acceptance import LIVE_ACCEPTANCE_CASE_IDS
from eimemory.models.records import RecordEnvelope, ScopeRef, TimeRef
from eimemory.runtime_identity import runtime_package_tree_digest


SCOPE = ScopeRef(
    tenant_id="tenant-1",
    agent_id="hongtu",
    workspace_id="embodied",
    user_id="darrow",
)


# This module exercises the frozen release-closure lineage contract.  That
# cohort is available only through the explicit compatibility reader; keep
# each historic assertion out of the dynamic-default authority path.
def record_release_lineage(*args, **kwargs):
    return _record_release_lineage(*args, legacy_compatibility=True, **kwargs)


def current_release_lineage(*args, **kwargs):
    return _current_release_lineage(*args, legacy_compatibility=True, **kwargs)


def evidence_release_for_domain(*args, **kwargs):
    return _evidence_release_for_domain(*args, legacy_compatibility=True, **kwargs)


def test_release_lineage_current_authority_does_not_bind_version(tmp_path: Path) -> None:
    runtime = Runtime.create(root=tmp_path / "runtime")
    commit = "a" * 40
    current = _receipt(runtime, SCOPE, commit, "1.0.0")
    runtime._test_runtime_commit = commit
    descriptive_version_drift = ReleaseIdentity(
        commit=current.commit,
        version="9.9.999",
        receipt_id=current.receipt_id,
        session_id=current.session_id,
    )
    try:
        error = release_lineage._current_release_error(
            runtime,
            SCOPE,
            descriptive_version_drift,
        )
        resolved = release_lineage._receipt_identity(
            runtime,
            SCOPE,
            descriptive_version_drift,
        )
    finally:
        runtime.close()

    assert error == {}
    assert resolved is not None
    assert resolved.commit == commit
    assert resolved.version == "1.0.0"


def test_current_authoritative_gate_wins_when_domain_is_unchanged(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = _repo(tmp_path)
    bootstrap_commit = _commit(repo, "docs/bootstrap.md", "bootstrap\n", "bootstrap")
    prior_commit = _commit(repo, "eimemory/retrieval/engine.py", "prior\n", "prior")
    current_commit = _commit(repo, "docs/current.md", "current\n", "current")
    runtime = Runtime.create(root=tmp_path / "runtime")
    try:
        _receipt(runtime, SCOPE, bootstrap_commit, "0.9.9")
        prior = _receipt(runtime, SCOPE, prior_commit, "1.0.0")
        runtime._test_runtime_commit = prior.commit
        prior_gate, prior_strict = _recall_gate_pair(runtime, SCOPE, prior)
        current_refs: dict[str, dict[str, str]] = {
            prior.commit: {
                "gate": prior_gate.record_id,
                "strict": prior_strict.record_id,
            }
        }
        _mock_recall_verifiers(monkeypatch, current_refs)
        prior_lineage = record_release_lineage(
            runtime,
            scope=SCOPE,
            repo_root=repo,
            current_release=prior,
            gate_evidence={"memory.recall": [prior_gate.record_id, prior_strict.record_id]},
        )
        assert prior_lineage["domains"]["memory.recall"]["mode"] == "current"

        current = _receipt(runtime, SCOPE, current_commit, "1.0.1")
        runtime._test_runtime_commit = current.commit
        current_gate, current_strict = _recall_gate_pair(runtime, SCOPE, current)
        current_refs[current.commit] = {
            "gate": current_gate.record_id,
            "strict": current_strict.record_id,
        }

        recorded = record_release_lineage(
            runtime,
            scope=SCOPE,
            repo_root=repo,
            current_release=current,
            gate_evidence={"memory.recall": [current_gate.record_id, current_strict.record_id]},
        )

        assert recorded["domains"]["memory.recall"]["changed"] is False
        assert recorded["domains"]["memory.recall"]["mode"] == "current"
        assert (
            evidence_release_for_domain(
                runtime,
                scope=SCOPE,
                repo_root=repo,
                domain="memory.recall",
                current_release=current,
                expected_record_id=recorded["record_id"],
            )
            == current
        )
    finally:
        runtime.close()


def test_unattested_intermediate_receipt_does_not_shadow_older_domain_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = _repo(tmp_path)
    bootstrap_commit = _commit(repo, "docs/bootstrap.md", "bootstrap\n", "bootstrap")
    evidence_commit = _commit(repo, "eimemory/retrieval/engine.py", "evidence\n", "evidence")
    intermediate_commit = _commit(repo, "docs/intermediate.md", "middle\n", "intermediate")
    current_commit = _commit(repo, "CHANGELOG.md", "current\n", "current")
    runtime = Runtime.create(root=tmp_path / "runtime")
    try:
        _receipt(runtime, SCOPE, bootstrap_commit, "0.9.9")
        evidence_release = _receipt(runtime, SCOPE, evidence_commit, "1.0.0")
        runtime._test_runtime_commit = evidence_release.commit
        gate, strict = _recall_gate_pair(runtime, SCOPE, evidence_release)
        _mock_recall_verifiers(
            monkeypatch,
            {
                evidence_release.commit: {
                    "gate": gate.record_id,
                    "strict": strict.record_id,
                }
            },
        )
        evidence_lineage = record_release_lineage(
            runtime,
            scope=SCOPE,
            repo_root=repo,
            current_release=evidence_release,
            gate_evidence={"memory.recall": [gate.record_id, strict.record_id]},
        )
        assert evidence_lineage["domains"]["memory.recall"]["mode"] == "current"

        intermediate = _receipt(runtime, SCOPE, intermediate_commit, "1.0.1")
        runtime._test_runtime_commit = intermediate.commit
        intermediate_lineage = record_release_lineage(
            runtime,
            scope=SCOPE,
            repo_root=repo,
            current_release=intermediate,
        )
        assert intermediate_lineage["domains"]["memory.recall"]["mode"] == "inherited"
        assert (
            intermediate_lineage["domains"]["memory.recall"]["evidence_release"]["commit"]
            == evidence_release.commit
        )

        current = _receipt(runtime, SCOPE, current_commit, "1.0.2")
        runtime._test_runtime_commit = current.commit
        recorded = record_release_lineage(
            runtime,
            scope=SCOPE,
            repo_root=repo,
            current_release=current,
        )

        assert recorded["ancestor_release"]["commit"] == intermediate.commit
        assert recorded["domains"]["memory.recall"]["mode"] == "inherited"
        assert (
            evidence_release_for_domain(
                runtime,
                scope=SCOPE,
                repo_root=repo,
                domain="memory.recall",
                current_release=current,
                expected_record_id=recorded["record_id"],
            )
            == evidence_release
        )
    finally:
        runtime.close()


def test_unchanged_domain_inherits_verified_ancestor_domain_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = _repo(tmp_path)
    bootstrap_commit = _commit(repo, "docs/bootstrap.md", "bootstrap\n", "bootstrap")
    prior_commit = _commit(repo, "eimemory/retrieval/engine.py", "prior\n", "prior")
    intermediate = _commit(repo, "docs/note.md", "unreceipted\n", "intermediate")
    current_commit = _commit(repo, "CHANGELOG.md", "current\n", "current")
    runtime = Runtime.create(root=tmp_path / "runtime")
    try:
        _receipt(runtime, SCOPE, bootstrap_commit, "0.9.9")
        prior = _receipt(runtime, SCOPE, prior_commit, "1.0.0")
        runtime._test_runtime_commit = prior.commit
        gate, strict = _recall_gate_pair(runtime, SCOPE, prior)
        _mock_recall_verifiers(
            monkeypatch,
            {
                prior.commit: {
                    "gate": gate.record_id,
                    "strict": strict.record_id,
                }
            },
        )
        prior_lineage = record_release_lineage(
            runtime,
            scope=SCOPE,
            repo_root=repo,
            current_release=prior,
            gate_evidence={"memory.recall": [gate.record_id, strict.record_id]},
        )
        assert prior_lineage["domains"]["memory.recall"]["mode"] == "current"

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
        assert (
            evidence_release_for_domain(
                runtime,
                scope=SCOPE,
                repo_root=repo,
                domain="memory.recall",
                current_release=current,
                expected_record_id=recorded["record_id"],
            )
            == prior
        )
        with pytest.raises(ValueError, match="lineage record mismatch"):
            evidence_release_for_domain(
                runtime,
                scope=SCOPE,
                repo_root=repo,
                domain="memory.recall",
                current_release=current,
                expected_record_id="forged-lineage-record",
            )
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
        resolved_missing = current_release_lineage(
            runtime,
            scope=SCOPE,
            repo_root=repo,
            current_release=current,
        )
        with pytest.raises(ValueError, match="not inheritable"):
            evidence_release_for_domain(
                runtime,
                scope=SCOPE,
                repo_root=repo,
                domain="memory.recall",
                current_release=current,
                expected_record_id=resolved_missing["record_id"],
            )

        forged_gate = _gate(
            runtime,
            SCOPE,
            current,
            source="eimemory.evaluation.production_recall",
        )
        bypass = record_release_lineage(
            runtime,
            scope=SCOPE,
            repo_root=repo,
            current_release=current,
            gate_evidence={"memory.recall": [forged_gate.record_id]},
        )
        assert bypass["domains"]["memory.recall"]["mode"] == "changed_unverified"
        assert bypass["domains"]["memory.recall"]["gate_errors"]
    finally:
        runtime.close()


def test_backfilled_ancestor_receipt_cannot_rewrite_release_history(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    prior_commit = _commit(repo, "eimemory/retrieval/engine.py", "prior\n", "prior")
    current_commit = _commit(repo, "docs/current.md", "current\n", "current")
    runtime = Runtime.create(root=tmp_path / "runtime")
    try:
        current = _receipt(runtime, SCOPE, current_commit, "1.0.1")
        _receipt(runtime, SCOPE, prior_commit, "1.0.0")
        runtime._test_runtime_commit = current.commit

        report = record_release_lineage(
            runtime,
            scope=SCOPE,
            repo_root=repo,
            current_release=current,
        )

        assert report == {"ok": False, "error": "verified_ancestor_receipt_not_found"}
    finally:
        runtime.close()


def test_pyproject_dependency_change_marks_every_domain_changed(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    prior_commit = _commit(
        repo,
        "pyproject.toml",
        '[project]\nname = "eimemory"\nversion = "1.0.0"\ndependencies = ["a"]\n',
        "prior",
    )
    current_commit = _commit(
        repo,
        "pyproject.toml",
        '[project]\nname = "eimemory"\nversion = "1.0.1"\ndependencies = ["a", "b"]\n',
        "current",
    )
    runtime = Runtime.create(root=tmp_path / "runtime")
    try:
        _receipt(runtime, SCOPE, prior_commit, "1.0.0")
        current = _receipt(runtime, SCOPE, current_commit, "1.0.1")
        runtime._test_runtime_commit = current.commit

        report = record_release_lineage(
            runtime,
            scope=SCOPE,
            repo_root=repo,
            current_release=current,
        )

        assert {state["mode"] for state in report["domains"].values()} == {
            "changed_unverified"
        }
        assert report["unknown_production_paths"] == []
    finally:
        runtime.close()


def test_openclaw_deploy_surface_marks_channel_domain_changed(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    prior_commit = _commit(repo, "deploy/systemd/openclaw-loop.service", "prior\n", "prior")
    current_commit = _commit(
        repo,
        "deploy/systemd/openclaw-loop.service",
        "changed\n",
        "current",
    )
    runtime = Runtime.create(root=tmp_path / "runtime")
    try:
        _receipt(runtime, SCOPE, prior_commit, "1.0.0")
        current = _receipt(runtime, SCOPE, current_commit, "1.0.1")
        runtime._test_runtime_commit = current.commit

        report = record_release_lineage(
            runtime,
            scope=SCOPE,
            repo_root=repo,
            current_release=current,
        )

        assert report["domains"]["channel.delivery"]["mode"] == "changed_unverified"
        assert report["domains"]["channel.delivery"]["changed_paths"] == [
            "deploy/systemd/openclaw-loop.service"
        ]
    finally:
        runtime.close()


@pytest.mark.parametrize(
    ("path", "expected_domains"),
    [
        (
            "deploy/systemd/hermes-gateway-eimemory.sh",
            {"deployment.runtime"},
        ),
        (
            "deploy/bootstrap_production_recall.py",
            {"deployment.runtime"},
        ),
        (
            "integrations/hermes/eimemory/__init__.py",
            {"memory.recall", "deployment.runtime"},
        ),
        (
            "integrations/hermes/eimemory_hook/__init__.py",
            {
                "memory.governance",
                "channel.delivery",
                "deployment.runtime",
                "code.evolution",
            },
        ),
        (
            "eimemory/capabilities/code_implementation_bootstrap.py",
            {"code.evolution"},
        ),
        (
            "eimemory/evaluation/hongtu_code_implementation.py",
            {"memory.governance", "code.evolution"},
        ),
        (
            "eimemory/governance/code_evolution_bridge.py",
            {"memory.governance", "code.evolution"},
        ),
        (
            "eimemory/governance/code_evolution_effects.py",
            {"memory.governance", "code.evolution"},
        ),
        (
            "eimemory/governance/code_evolution_observation.py",
            {"memory.governance", "code.evolution"},
        ),
        (
            "eimemory/governance/code_evolution_repository.py",
            {"memory.governance", "code.evolution"},
        ),
        (
            "eimemory/governance/system_code_repair.py",
            {"memory.governance", "code.evolution"},
        ),
        (
            "eimemory/governance/code_maintenance.py",
            {"memory.governance", "code.evolution"},
        ),
        (
            "eimemory/ops/release_closure_failure.py",
            {"code.evolution"},
        ),
        (
            "eimemory/cli/main.py",
            {"code.evolution"},
        ),
        (
            "deploy/record_release_closure_incident.py",
            {"deployment.runtime", "code.evolution"},
        ),
    ],
)


def test_hermes_release_surface_is_fully_classified(
    tmp_path: Path,
    path: str,
    expected_domains: set[str],
) -> None:
    repo = _repo(tmp_path)
    prior_commit = _commit(repo, path, "prior\n", "prior")
    current_commit = _commit(repo, path, "changed\n", "current")
    runtime = Runtime.create(root=tmp_path / "runtime")
    try:
        _receipt(runtime, SCOPE, prior_commit, "1.0.0")
        current = _receipt(runtime, SCOPE, current_commit, "1.0.1")
        runtime._test_runtime_commit = current.commit

        report = record_release_lineage(
            runtime,
            scope=SCOPE,
            repo_root=repo,
            current_release=current,
        )

        changed_domains = {
            domain
            for domain, state in report["domains"].items()
            if state["changed"] is True
        }
        assert changed_domains == expected_domains
        assert report["unknown_production_paths"] == []
        for domain in expected_domains:
            assert report["domains"][domain]["changed_paths"] == [path]
    finally:
        runtime.close()


def test_version_only_project_metadata_leaves_capability_domains_unchanged(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path)
    prior_commit = _commit(
        repo,
        "pyproject.toml",
        '[project]\nname = "eimemory"\nversion = "1.0.0"\ndependencies = ["a"]\n',
        "prior",
    )
    current_commit = _commit(
        repo,
        "pyproject.toml",
        '[project]\nname = "eimemory"\nversion = "1.0.1"\ndependencies = ["a"]\n',
        "current",
    )
    runtime = Runtime.create(root=tmp_path / "runtime")
    try:
        _receipt(runtime, SCOPE, prior_commit, "1.0.0")
        current = _receipt(runtime, SCOPE, current_commit, "1.0.1")
        runtime._test_runtime_commit = current.commit

        report = record_release_lineage(
            runtime,
            scope=SCOPE,
            repo_root=repo,
            current_release=current,
        )

        assert all(state["changed"] is False for state in report["domains"].values())
        assert {state["mode"] for state in report["domains"].values()} == {
            "changed_unverified"
        }
        assert report["unknown_production_paths"] == []
    finally:
        runtime.close()


@pytest.mark.parametrize(
    "path",
    [
        "integrations/hermes/eimemory/plugin.yaml",
        "integrations/hermes/eimemory_hook/plugin.yaml",
        "integrations/codex/eimemory/.codex-plugin/plugin.json",
    ],
)
def test_version_only_integration_manifest_leaves_capability_domains_unchanged(
    tmp_path: Path,
    path: str,
) -> None:
    repo = _repo(tmp_path)
    if path.endswith(".json"):
        prior = '{"name":"eimemory","version":"1.0.0","description":"memory"}\n'
        current = '{"name":"eimemory","version":"1.0.1","description":"memory"}\n'
    else:
        prior = "name: eimemory\nversion: 1.0.0\ndescription: memory\n"
        current = "name: eimemory\nversion: 1.0.1\ndescription: memory\n"
    prior_commit = _commit(repo, path, prior, "prior")
    current_commit = _commit(repo, path, current, "current")

    for domain in release_lineage.DOMAINS:
        report = release_lineage._domain_change_summary(
            repo,
            domain=domain,
            ancestor=prior_commit,
            current=current_commit,
        )
        assert report is not None
        assert report["changed"] is False
        assert report["domain_changed_paths"] == []
        assert report["unknown_production_paths"] == []


@pytest.mark.parametrize(
    ("prior", "current", "expected_changed"),
    [
        ('__version__ = "1.0.0"\n', '__version__ = "1.0.1"\n', False),
        (
            '__version__ = "1.0.0"\nCHANNEL = "stable"\n',
            '__version__ = "1.0.1"\nCHANNEL = "preview"\n',
            True,
        ),
    ],
)
def test_version_module_only_ignores_the_release_literal(
    tmp_path: Path,
    prior: str,
    current: str,
    expected_changed: bool,
) -> None:
    repo = _repo(tmp_path)
    prior_commit = _commit(repo, "eimemory/version.py", prior, "prior")
    current_commit = _commit(repo, "eimemory/version.py", current, "current")
    runtime = Runtime.create(root=tmp_path / "runtime")
    try:
        _receipt(runtime, SCOPE, prior_commit, "1.0.0")
        release = _receipt(runtime, SCOPE, current_commit, "1.0.1")
        runtime._test_runtime_commit = release.commit

        report = record_release_lineage(
            runtime,
            scope=SCOPE,
            repo_root=repo,
            current_release=release,
        )

        assert {
            state["changed"] for state in report["domains"].values()
        } == {expected_changed}
        assert {state["mode"] for state in report["domains"].values()} == {
            "changed_unverified"
        }
    finally:
        runtime.close()


def test_storage_deploy_surface_marks_storage_domain_changed(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    prior_commit = _commit(repo, "deploy/migrate_storage_release.py", "prior\n", "prior")
    current_commit = _commit(repo, "deploy/migrate_storage_release.py", "changed\n", "current")
    runtime = Runtime.create(root=tmp_path / "runtime")
    try:
        _receipt(runtime, SCOPE, prior_commit, "1.0.0")
        current = _receipt(runtime, SCOPE, current_commit, "1.0.1")
        runtime._test_runtime_commit = current.commit

        report = record_release_lineage(
            runtime,
            scope=SCOPE,
            repo_root=repo,
            current_release=current,
        )

        assert report["domains"]["storage.integrity"]["mode"] == "changed_unverified"
        assert report["domains"]["storage.integrity"]["changed_paths"] == [
            "deploy/migrate_storage_release.py"
        ]
    finally:
        runtime.close()


def test_recall_requires_authoritative_gate_and_strict_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = _repo(tmp_path)
    prior_commit = _commit(repo, "eimemory/retrieval/engine.py", "prior\n", "prior")
    current_commit = _commit(repo, "eimemory/retrieval/engine.py", "changed\n", "current")
    runtime = Runtime.create(root=tmp_path / "runtime")
    try:
        _receipt(runtime, SCOPE, prior_commit, "1.0.0")
        current = _receipt(runtime, SCOPE, current_commit, "1.0.1")
        runtime._test_runtime_commit = current.commit
        gate = _gate(
            runtime,
            SCOPE,
            current,
            source="eimemory.evaluation.production_recall",
        )
        strict = _gate(
            runtime,
            SCOPE,
            current,
            source="eimemory.evaluation.production_recall.bootstrap",
        )
        monkeypatch.setattr(
            real_query_gate,
            "verify_current_production_recall_gate",
            lambda *args, **kwargs: {
                "ok": True,
                "status": "accepted",
                "reason": "",
                "record_id": gate.record_id,
            },
        )
        monkeypatch.setattr(
            real_query_gate,
            "verify_current_production_recall_strict_state",
            lambda *args, **kwargs: {
                "ok": True,
                "status": "strict_activated",
                "reason": "",
                "record_id": strict.record_id,
                "candidate_commit": current.commit,
                "gate_record_id": gate.record_id,
            },
        )

        report = record_release_lineage(
            runtime,
            scope=SCOPE,
            repo_root=repo,
            current_release=current,
            gate_evidence={"memory.recall": [gate.record_id, strict.record_id]},
        )
        resolved = current_release_lineage(
            runtime,
            scope=SCOPE,
            repo_root=repo,
            current_release=current,
        )

        assert report["domains"]["memory.recall"]["mode"] == "current"
        assert (
            evidence_release_for_domain(
                runtime,
                scope=SCOPE,
                repo_root=repo,
                domain="memory.recall",
                current_release=current,
                expected_record_id=resolved["record_id"],
            )
            == current
        )
    finally:
        runtime.close()


def test_unchanged_recall_accepts_current_bootstrap_pending_and_core_replay(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = _repo(tmp_path)
    prior_commit = _commit(repo, "eimemory/retrieval/engine.py", "prior\n", "prior")
    current_commit = _commit(repo, "docs/current.md", "current\n", "current")
    runtime = Runtime.create(root=tmp_path / "runtime")
    try:
        _receipt(runtime, SCOPE, prior_commit, "1.0.0")
        current_record = _deployment_receipt_record(
            SCOPE,
            current_commit,
            "1.0.1",
        )
        anticipated = ReleaseIdentity(
            commit=current_commit,
            version="1.0.1",
            receipt_id=current_record.record_id,
            session_id=current_record.record_id,
        )
        pending = _gate(
            runtime,
            SCOPE,
            anticipated,
            source="eimemory.evaluation.production_recall.bootstrap",
        )
        runtime.store.append(current_record)
        current = verified_deployment_receipt_identity(current_record)
        assert current == anticipated
        runtime._test_runtime_commit = current.commit
        core_manifest = _manifest(
            runtime,
            SCOPE,
            title="core",
            capabilities=["memory.recall"],
        )
        _mock_pending_recall_verifiers(
            monkeypatch,
            release=current,
            pending_record_id=pending.record_id,
            core_manifest_record_id=core_manifest.record_id,
        )

        report = record_release_lineage(
            runtime,
            scope=SCOPE,
            repo_root=repo,
            current_release=current,
            gate_evidence={
                "memory.recall": [pending.record_id, core_manifest.record_id]
            },
        )

        domain = report["domains"]["memory.recall"]
        assert domain["changed"] is False
        assert domain["mode"] == "current"
        assert domain["gate_errors"] == {}
        assert domain["gate_evidence"] == [
            pending.record_id,
            core_manifest.record_id,
        ]
    finally:
        runtime.close()


def test_bootstrap_pending_cannot_authorize_changed_recall_domain(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = _repo(tmp_path)
    prior_commit = _commit(repo, "eimemory/retrieval/engine.py", "prior\n", "prior")
    current_commit = _commit(
        repo,
        "eimemory/retrieval/engine.py",
        "changed\n",
        "current",
    )
    runtime = Runtime.create(root=tmp_path / "runtime")
    try:
        _receipt(runtime, SCOPE, prior_commit, "1.0.0")
        current = _receipt(runtime, SCOPE, current_commit, "1.0.1")
        runtime._test_runtime_commit = current.commit
        pending = _gate(
            runtime,
            SCOPE,
            current,
            source="eimemory.evaluation.production_recall.bootstrap",
        )
        core_manifest = _manifest(
            runtime,
            SCOPE,
            title="core",
            capabilities=["memory.recall"],
        )
        _mock_pending_recall_verifiers(
            monkeypatch,
            release=current,
            pending_record_id=pending.record_id,
            core_manifest_record_id=core_manifest.record_id,
        )

        report = record_release_lineage(
            runtime,
            scope=SCOPE,
            repo_root=repo,
            current_release=current,
            gate_evidence={
                "memory.recall": [pending.record_id, core_manifest.record_id]
            },
        )

        domain = report["domains"]["memory.recall"]
        assert domain["changed"] is True
        assert domain["mode"] == "changed_unverified"
        assert domain["gate_errors"] == {
            "__contract__": "bootstrap_pending_requires_unchanged_recall_domain"
        }
    finally:
        runtime.close()


def test_bootstrap_pending_requires_exact_current_recall_replay_manifest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = _repo(tmp_path)
    prior_commit = _commit(repo, "eimemory/retrieval/engine.py", "prior\n", "prior")
    current_commit = _commit(repo, "docs/current.md", "current\n", "current")
    runtime = Runtime.create(root=tmp_path / "runtime")
    try:
        _receipt(runtime, SCOPE, prior_commit, "1.0.0")
        current = _receipt(runtime, SCOPE, current_commit, "1.0.1")
        runtime._test_runtime_commit = current.commit
        pending = _gate(
            runtime,
            SCOPE,
            current,
            source="eimemory.evaluation.production_recall.bootstrap",
        )
        supplied_manifest = _manifest(
            runtime,
            SCOPE,
            title="supplied",
            capabilities=["memory.recall"],
        )
        verified_manifest = _manifest(
            runtime,
            SCOPE,
            title="verified",
            capabilities=["memory.recall"],
        )
        _mock_pending_recall_verifiers(
            monkeypatch,
            release=current,
            pending_record_id=pending.record_id,
            core_manifest_record_id=verified_manifest.record_id,
        )

        report = record_release_lineage(
            runtime,
            scope=SCOPE,
            repo_root=repo,
            current_release=current,
            gate_evidence={
                "memory.recall": [pending.record_id, supplied_manifest.record_id]
            },
        )

        domain = report["domains"]["memory.recall"]
        assert domain["mode"] == "changed_unverified"
        assert domain["gate_errors"] == {
            "__contract__": "exact_bootstrap_pending_and_recall_replay_required"
        }
    finally:
        runtime.close()


def test_recall_gate_records_must_follow_current_receipt_insertion(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = _repo(tmp_path)
    prior_commit = _commit(repo, "eimemory/retrieval/engine.py", "prior\n", "prior")
    current_commit = _commit(repo, "eimemory/retrieval/engine.py", "changed\n", "current")
    runtime = Runtime.create(root=tmp_path / "runtime")
    try:
        _receipt(runtime, SCOPE, prior_commit, "1.0.0")
        current_record = _deployment_receipt_record(
            SCOPE,
            current_commit,
            "1.0.1",
        )
        anticipated = ReleaseIdentity(
            commit=current_commit,
            version="1.0.1",
            receipt_id=current_record.record_id,
            session_id=current_record.record_id,
        )
        gate = _gate(
            runtime,
            SCOPE,
            anticipated,
            source="eimemory.evaluation.production_recall",
        )
        strict = _gate(
            runtime,
            SCOPE,
            anticipated,
            source="eimemory.evaluation.production_recall.bootstrap",
        )
        runtime.store.append(current_record)
        current = verified_deployment_receipt_identity(current_record)
        assert current == anticipated
        runtime._test_runtime_commit = current.commit
        monkeypatch.setattr(
            real_query_gate,
            "verify_current_production_recall_gate",
            lambda *args, **kwargs: {
                "ok": True,
                "status": "accepted",
                "reason": "",
                "record_id": gate.record_id,
            },
        )
        monkeypatch.setattr(
            real_query_gate,
            "verify_current_production_recall_strict_state",
            lambda *args, **kwargs: {
                "ok": True,
                "status": "strict_activated",
                "reason": "",
                "record_id": strict.record_id,
                "candidate_commit": current.commit,
                "gate_record_id": gate.record_id,
            },
        )

        report = record_release_lineage(
            runtime,
            scope=SCOPE,
            repo_root=repo,
            current_release=current,
            gate_evidence={"memory.recall": [gate.record_id, strict.record_id]},
        )

        assert report["domains"]["memory.recall"]["mode"] == "changed_unverified"
        assert set(report["domains"]["memory.recall"]["gate_errors"].values()) == {
            "gate_not_after_current_receipt"
        }
    finally:
        runtime.close()


def test_deployment_domain_accepts_only_exact_current_verified_receipt(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    prior_commit = _commit(repo, "deploy/record_deployment_receipt.py", "prior\n", "prior")
    current_commit = _commit(repo, "deploy/record_deployment_receipt.py", "changed\n", "current")
    runtime = Runtime.create(root=tmp_path / "runtime")
    try:
        prior = _receipt(runtime, SCOPE, prior_commit, "1.0.0")
        current = _receipt(runtime, SCOPE, current_commit, "1.0.1")
        runtime._test_runtime_commit = current.commit

        stale = record_release_lineage(
            runtime,
            scope=SCOPE,
            repo_root=repo,
            current_release=current,
            gate_evidence={"deployment.runtime": [prior.receipt_id]},
        )
        exact = record_release_lineage(
            runtime,
            scope=SCOPE,
            repo_root=repo,
            current_release=current,
            gate_evidence={"deployment.runtime": [current.receipt_id]},
        )

        assert stale["domains"]["deployment.runtime"]["mode"] == "changed_unverified"
        assert exact["domains"]["deployment.runtime"]["mode"] == "current"
    finally:
        runtime.close()


def test_code_evolution_domain_rejects_an_ordinary_current_deployment_receipt(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path)
    prior_commit = _commit(
        repo,
        "eimemory/governance/code_evolution_transaction.py",
        "prior\n",
        "prior",
    )
    current_commit = _commit(
        repo,
        "eimemory/governance/code_evolution_transaction.py",
        "changed\n",
        "current",
    )
    runtime = Runtime.create(root=tmp_path / "runtime")
    try:
        _receipt(runtime, SCOPE, prior_commit, "1.0.0")
        current = _receipt(runtime, SCOPE, current_commit, "1.0.1")
        runtime._test_runtime_commit = current.commit

        report = record_release_lineage(
            runtime,
            scope=SCOPE,
            repo_root=repo,
            current_release=current,
            gate_evidence={"code.evolution": [current.receipt_id]},
        )

        domain = report["domains"]["code.evolution"]
        assert domain["mode"] == "changed_unverified"
        assert domain["gate_errors"] == {
            "__contract__": "strict_code_evolution_receipt_required"
        }
    finally:
        runtime.close()


def test_immutable_installer_affects_runtime_openclaw_and_storage_domains(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path)
    prior_commit = _commit(repo, "deploy/install_immutable_release.sh", "prior\n", "prior")
    current_commit = _commit(
        repo,
        "deploy/install_immutable_release.sh",
        "changed\n",
        "current",
    )
    runtime = Runtime.create(root=tmp_path / "runtime")
    try:
        _receipt(runtime, SCOPE, prior_commit, "1.0.0")
        current = _receipt(runtime, SCOPE, current_commit, "1.0.1")
        runtime._test_runtime_commit = current.commit

        report = record_release_lineage(
            runtime,
            scope=SCOPE,
            repo_root=repo,
            current_release=current,
        )

        assert {
            domain
            for domain, state in report["domains"].items()
            if state["changed"] is True
        } == {
            "channel.delivery",
            "storage.integrity",
            "deployment.runtime",
            "code.evolution",
        }
    finally:
        runtime.close()


def test_openclaw_requires_real_platform_channel_acceptance_not_local_probes(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path)
    prior_commit = _commit(repo, "integrations/openclaw/index.ts", "prior\n", "prior")
    current_commit = _commit(repo, "integrations/openclaw/index.ts", "changed\n", "current")
    runtime = Runtime.create(root=tmp_path / "runtime")
    try:
        _receipt(runtime, SCOPE, prior_commit, "1.0.0")
        current = _receipt(runtime, SCOPE, current_commit, "1.0.1")
        runtime._test_runtime_commit = current.commit
        local_probe = _live_case(
            runtime,
            SCOPE,
            current,
            case_id=LIVE_ACCEPTANCE_CASE_IDS[0],
            index=0,
        )
        channel_receipt = _channel_case(runtime, SCOPE, current)

        partial = record_release_lineage(
            runtime,
            scope=SCOPE,
            repo_root=repo,
            current_release=current,
            gate_evidence={"channel.delivery": [local_probe.record_id]},
        )
        complete = record_release_lineage(
            runtime,
            scope=SCOPE,
            repo_root=repo,
            current_release=current,
            gate_evidence={"channel.delivery": [channel_receipt.record_id]},
        )
        resolved = current_release_lineage(
            runtime,
            scope=SCOPE,
            repo_root=repo,
            current_release=current,
        )

        assert partial["domains"]["channel.delivery"]["mode"] == "changed_unverified"
        assert complete["domains"]["channel.delivery"]["mode"] == "current"
        assert (
            evidence_release_for_domain(
                runtime,
                scope=SCOPE,
                repo_root=repo,
                domain="channel.delivery",
                current_release=current,
                expected_record_id=resolved["record_id"],
            )
            == current
        )
    finally:
        runtime.close()


def test_current_lineage_uses_sqlite_insertion_order_for_equal_timestamps(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = _repo(tmp_path)
    prior_commit = _commit(repo, "integrations/openclaw/index.ts", "prior\n", "prior")
    current_commit = _commit(repo, "integrations/openclaw/index.ts", "changed\n", "current")
    runtime = Runtime.create(root=tmp_path / "runtime")
    try:
        _receipt(runtime, SCOPE, prior_commit, "1.0.0")
        current = _receipt(runtime, SCOPE, current_commit, "1.0.1")
        runtime._test_runtime_commit = current.commit
        local_probe = _live_case(
            runtime,
            SCOPE,
            current,
            case_id=LIVE_ACCEPTANCE_CASE_IDS[0],
            index=0,
        )
        channel_receipt = _channel_case(runtime, SCOPE, current)
        lineage_ids = iter(("rec_ffffffffffff", "rec_000000000000"))
        original_generate_record_id = record_models.generate_record_id

        def deterministic_lineage_id(kind: str) -> str:
            if kind == "l5_self_continuity":
                return next(lineage_ids)
            return original_generate_record_id(kind)

        monkeypatch.setattr(
            record_models,
            "generate_record_id",
            deterministic_lineage_id,
        )
        monkeypatch.setattr(
            TimeRef,
            "now",
            classmethod(
                lambda cls: cls(
                    created_at="2026-07-29T18:20:56+08:00",
                    updated_at="2026-07-29T18:20:56+08:00",
                    occurred_at="2026-07-29T18:20:56+08:00",
                )
            ),
        )

        partial = record_release_lineage(
            runtime,
            scope=SCOPE,
            repo_root=repo,
            current_release=current,
            gate_evidence={"channel.delivery": [local_probe.record_id]},
        )
        complete = record_release_lineage(
            runtime,
            scope=SCOPE,
            repo_root=repo,
            current_release=current,
            gate_evidence={"channel.delivery": [channel_receipt.record_id]},
        )
        resolved = current_release_lineage(
            runtime,
            scope=SCOPE,
            repo_root=repo,
            current_release=current,
        )

        assert partial["record_id"] == "rec_ffffffffffff"
        assert complete["record_id"] == "rec_000000000000"
        assert partial["domains"]["channel.delivery"]["mode"] == "changed_unverified"
        assert complete["domains"]["channel.delivery"]["mode"] == "current"
        assert resolved["record_id"] == complete["record_id"]
        assert (
            evidence_release_for_domain(
                runtime,
                scope=SCOPE,
                repo_root=repo,
                domain="channel.delivery",
                current_release=current,
                expected_record_id=complete["record_id"],
            )
            == current
        )
    finally:
        runtime.close()


def test_current_lineage_fails_closed_without_sqlite_insertion_order(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = Runtime.create(root=tmp_path / "runtime")
    current = _receipt(runtime, SCOPE, "a" * 40, "1.0.1")
    runtime._test_runtime_commit = current.commit
    receipt = runtime.store.get_by_id(current.receipt_id, scope=SCOPE)
    assert receipt is not None
    lineage_scan_calls = 0

    def controlled_list_records(*, kinds, **_kwargs):
        nonlocal lineage_scan_calls
        if kinds == ["promotion_request"]:
            return [receipt]
        if kinds == ["l5_self_continuity"]:
            lineage_scan_calls += 1
            raise AssertionError("generic lineage ordering must not be used")
        return []

    monkeypatch.setattr(runtime.store, "list_records", controlled_list_records)
    monkeypatch.setattr(
        runtime.store,
        "get_by_id",
        lambda record_id, **_kwargs: receipt
        if record_id == receipt.record_id
        else None,
    )
    monkeypatch.setattr(runtime.store, "sqlite", None)
    try:
        report = current_release_lineage(
            runtime,
            scope=SCOPE,
            current_release=current,
        )
    finally:
        monkeypatch.undo()
        runtime.close()

    assert report == {"ok": False, "error": "current_release_lineage_not_found"}
    assert lineage_scan_calls == 0


def test_governance_requires_complete_weak_and_core_manifest_contracts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = _repo(tmp_path)
    prior_commit = _commit(repo, "eimemory/governance/policy.py", "prior\n", "prior")
    current_commit = _commit(repo, "eimemory/governance/policy.py", "changed\n", "current")
    runtime = Runtime.create(root=tmp_path / "runtime")
    try:
        _receipt(runtime, SCOPE, prior_commit, "1.0.0")
        current = _receipt(runtime, SCOPE, current_commit, "1.0.1")
        runtime._test_runtime_commit = current.commit
        forged = _gate(
            runtime,
            SCOPE,
            current,
            source="eimemory.capability_replay",
        )
        bypass = record_release_lineage(
            runtime,
            scope=SCOPE,
            repo_root=repo,
            current_release=current,
            gate_evidence={"memory.governance": [forged.record_id]},
        )
        assert bypass["domains"]["memory.governance"]["mode"] == "changed_unverified"

        weak_manifest = _manifest(
            runtime,
            SCOPE,
            title="weak",
            capabilities=sorted(l5_readiness.LEGACY_WEAK_CAPABILITIES),
        )
        core_manifest = _manifest(
            runtime,
            SCOPE,
            title="core",
            capabilities=sorted(l5_readiness.LEGACY_READINESS_CAPABILITIES),
        )

        def verified_summary(*args, **kwargs):
            missing_field = kwargs["missing_field"]
            capabilities = set(kwargs["capabilities"])
            return {
                "executed_count": len(capabilities) * 3,
                "pass_count": len(capabilities) * 3,
                "fail_count": 0,
                "not_run_count": 0,
                "minimum_executed": len(capabilities) * 3,
                missing_field: [],
                "rejection_reasons": {},
                "manifest_record_ids": {
                    capability: (
                        weak_manifest.record_id
                        if capability in l5_readiness.LEGACY_WEAK_CAPABILITIES
                        else core_manifest.record_id
                    )
                    for capability in capabilities
                },
                "manifest_rejection_reasons": {},
            }

        monkeypatch.setattr(l5_readiness, "_verified_replay_summary", verified_summary)
        report = record_release_lineage(
            runtime,
            scope=SCOPE,
            repo_root=repo,
            current_release=current,
            gate_evidence={
                "memory.governance": [
                    weak_manifest.record_id,
                    core_manifest.record_id,
                ]
            },
        )

        assert report["domains"]["memory.governance"]["mode"] == "current"
    finally:
        runtime.close()


def test_ancestor_lookup_uses_bounded_source_filtered_pages(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = _repo(tmp_path)
    prior_commit = _commit(repo, "eimemory/retrieval/engine.py", "prior\n", "prior")
    current_commit = _commit(repo, "docs/current.md", "current\n", "current")
    runtime = Runtime.create(root=tmp_path / "runtime")
    try:
        _receipt(runtime, SCOPE, prior_commit, "1.0.0")
        current = _receipt(runtime, SCOPE, current_commit, "1.0.1")
        runtime._test_runtime_commit = current.commit
        original = runtime.store.list_records

        def reject_broad_scan(*args, **kwargs):
            if kwargs.get("kinds") == ["promotion_request"] and kwargs.get("limit") == 1000:
                raise AssertionError("broad unbounded receipt scan")
            return original(*args, **kwargs)

        monkeypatch.setattr(runtime.store, "list_records", reject_broad_scan)

        report = record_release_lineage(
            runtime,
            scope=SCOPE,
            repo_root=repo,
            current_release=current,
        )

        assert report["ok"] is True
        assert report["ancestor_release"]["commit"] == prior_commit
    finally:
        runtime.close()


def test_ancestor_lookup_keysets_past_old_cap_with_one_git_graph_walk(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ancestor_commit = "a" * 40
    current_commit = "b" * 40
    ancestor_record = _deployment_receipt_record(SCOPE, ancestor_commit, "1.0.0")
    ancestor = verified_deployment_receipt_identity(ancestor_record)
    current_record = _deployment_receipt_record(SCOPE, current_commit, "1.0.1")
    current = verified_deployment_receipt_identity(current_record)
    assert ancestor is not None and current is not None

    row_records: list[tuple[int, RecordEnvelope]] = []
    for rowid in range(22, 1, -1):
        forged = _deployment_receipt_record(
            SCOPE,
            f"{rowid:040x}",
            f"0.0.{rowid}",
            forged=True,
        )
        row_records.append((rowid, forged))
    row_records.append((1, ancestor_record))
    connection = _KeysetReceiptConnection(
        current_rowid=23,
        current_record=current_record,
        row_records=row_records,
    )
    runtime = _KeysetReceiptRuntime(connection, current_record, row_records)
    git_calls: list[tuple[str, ...]] = []

    def git_graph(_repo: Path, *args: str) -> bytes | None:
        git_calls.append(args)
        if args == ("rev-list", "--parents", current_commit):
            return (
                f"{current_commit} {ancestor_commit}\n"
                f"{ancestor_commit}\n"
            ).encode()
        raise AssertionError(f"unexpected git invocation: {args}")

    monkeypatch.setattr(release_lineage, "_git_bytes", git_graph)

    selected = release_lineage._newest_verified_ancestor(
        runtime,
        scope=SCOPE,
        repo=Path("unused"),
        current_release=current,
    )

    assert selected == ancestor
    keyset_queries = [
        sql
        for sql in connection.queries
        if "SELECT rowid, record_id, source_id" in sql
    ]
    assert len(keyset_queries) > 20
    assert all("rowid < ?" in sql and "OFFSET" not in sql for sql in keyset_queries)
    assert git_calls == [("rev-list", "--parents", current_commit)]


@pytest.mark.parametrize("sqlite_mode", ["missing", "current_row_missing"])
def test_ancestor_lookup_fails_closed_without_sqlite_insertion_order(
    sqlite_mode: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ancestor_commit = "a" * 40
    current_commit = "b" * 40
    ancestor_record = _deployment_receipt_record(SCOPE, ancestor_commit, "1.0.0")
    current_record = _deployment_receipt_record(SCOPE, current_commit, "1.0.1")
    ancestor_record.time.created_at = "2026-01-01T00:00:00+00:00"
    current_record.time.created_at = "2026-01-01T00:00:01+00:00"
    current = verified_deployment_receipt_identity(current_record)
    assert current is not None
    runtime = _UnavailableOrderingRuntime(
        sqlite_mode=sqlite_mode,
        current_record=current_record,
        fallback_records=[ancestor_record],
    )
    git_calls: list[tuple[str, ...]] = []

    def git_graph(_repo: Path, *args: str) -> bytes | None:
        git_calls.append(args)
        return (
            f"{current_commit} {ancestor_commit}\n"
            f"{ancestor_commit}\n"
        ).encode()

    monkeypatch.setattr(release_lineage, "_git_bytes", git_graph)

    selected = release_lineage._newest_verified_ancestor(
        runtime,
        scope=SCOPE,
        repo=Path("unused"),
        current_release=current,
    )

    assert selected is None
    assert runtime.store.list_calls == 0
    assert git_calls == []
    assert all("OFFSET" not in query for query in runtime.store.queries)


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


def test_known_release_support_surfaces_do_not_taint_memory_recall(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path)
    files = {
        "deploy/systemd/README.md": "prior docs\n",
        "deploy/verify_release_health.py": "PRIOR = True\n",
        "eimemory/adapters/eibrain/rpc_server.py": "HEALTH = 'prior'\n",
        "eimemory/api/runtime.py": "GOVERNANCE = 'prior'\n",
        "integrations/codex/eimemory/.codex-plugin/plugin.json": (
            '{"name":"eimemory","version":"1.0.0"}\n'
        ),
        "integrations/hermes/eimemory/plugin.yaml": (
            "name: eimemory\nversion: 1.0.0\ndescription: memory\n"
        ),
        "scripts/test_openclaw_loop.py": "PRIOR = True\n",
    }
    for relative_path, content in files.items():
        path = repo / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "prior")
    prior_commit = _git(repo, "rev-parse", "HEAD").stdout.strip().lower()

    updates = {
        "deploy/systemd/README.md": "current docs\n",
        "deploy/verify_release_health.py": "CURRENT = True\n",
        "eimemory/adapters/eibrain/rpc_server.py": "HEALTH = 'current'\n",
        "eimemory/api/runtime.py": "GOVERNANCE = 'current'\n",
        "integrations/codex/eimemory/.codex-plugin/plugin.json": (
            '{"name":"eimemory","version":"1.0.1"}\n'
        ),
        "integrations/hermes/eimemory/plugin.yaml": (
            "name: eimemory\nversion: 1.0.1\ndescription: memory\n"
        ),
        "scripts/test_openclaw_loop.py": "CURRENT = True\n",
    }
    for relative_path, content in updates.items():
        (repo / relative_path).write_text(content, encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "current")
    current_commit = _git(repo, "rev-parse", "HEAD").stdout.strip().lower()

    report = release_lineage._domain_change_summary(
        repo,
        domain="memory.recall",
        ancestor=prior_commit,
        current=current_commit,
    )

    assert report is not None
    assert report["changed"] is False
    assert report["domain_changed_paths"] == []
    assert report["unknown_production_paths"] == []


@pytest.mark.parametrize(
    "helper_path",
    [
        "deploy/ensure_openclaw_bundled_bridge.py",
        "deploy/ensure_openclaw_bridge_config.py",
        "deploy/verify_openclaw_plugin_runtime.py",
        "deploy/wait_openclaw_gateway_ready.py",
    ],
)
def test_explicit_openclaw_deployment_helpers_are_channel_only(
    tmp_path: Path,
    helper_path: str,
) -> None:
    repo = _repo(tmp_path)
    prior_commit = _commit(repo, helper_path, "PRIOR = True\n", "prior")
    current_commit = _commit(repo, helper_path, "CURRENT = True\n", "current")

    summary = release_lineage._release_change_summary(
        repo, ancestor=prior_commit, current=current_commit
    )
    assert summary is not None
    changed_paths, classified, unknown = summary
    assert changed_paths == [helper_path]
    assert unknown == []
    assert classified == {helper_path: {"channel.delivery"}}

    recall = release_lineage._domain_change_summary(
        repo, domain="memory.recall", ancestor=prior_commit, current=current_commit
    )
    channel = release_lineage._domain_change_summary(
        repo, domain="channel.delivery", ancestor=prior_commit, current=current_commit
    )
    assert recall is not None and recall["changed"] is False
    assert channel is not None and channel["changed"] is True
    assert channel["domain_changed_paths"] == [helper_path]
    assert channel["ancestor_digest"] != channel["current_digest"]


@pytest.mark.parametrize(
    "helper_path",
    [
        "deploy/ensure_openclaw_future_runtime.py",
        "deploy/verify_openclaw_future_runtime.py",
        "deploy/openclaw_future_runtime.py",
    ],
)
def test_unregistered_openclaw_named_deploy_helper_remains_unknown(
    tmp_path: Path,
    helper_path: str,
) -> None:
    repo = _repo(tmp_path)
    prior_commit = _commit(repo, "docs/prior.md", "prior\n", "prior")
    current_commit = _commit(repo, helper_path, "RUNTIME = True\n", "current")

    summary = release_lineage._domain_change_summary(
        repo, domain="memory.recall", ancestor=prior_commit, current=current_commit
    )

    assert summary is not None
    assert summary["unknown_production_paths"] == [helper_path]
    assert summary["changed"] is True


@pytest.mark.parametrize("candidate_changes_channel", [False, True])
def test_openclaw_support_channel_receipt_is_inherited_only_without_channel_changes(
    tmp_path: Path,
    candidate_changes_channel: bool,
) -> None:
    helper_path = "deploy/ensure_openclaw_bundled_bridge.py"
    repo = _repo(tmp_path)
    prior_commit = _commit(repo, helper_path, "PRIOR = True\n", "prior")
    support_commit = _commit(repo, helper_path, "SUPPORT = True\n", "support")
    candidate_path = (
        helper_path if candidate_changes_channel else "eimemory/governance/system_code_repair.py"
    )
    candidate_commit = _commit(repo, candidate_path, "CANDIDATE = True\n", "candidate")
    runtime = Runtime.create(root=tmp_path / "runtime")
    try:
        _receipt(runtime, SCOPE, prior_commit, "1.0.0")
        support = _receipt(runtime, SCOPE, support_commit, "1.0.1")
        runtime._test_runtime_commit = support.commit
        acceptance = _channel_case(
            runtime, SCOPE, support, transport_owner="openclaw", platform="feishu"
        )
        support_lineage = record_release_lineage(
            runtime, scope=SCOPE, repo_root=repo, current_release=support,
            gate_evidence={"channel.delivery": [acceptance.record_id]},
        )
        assert support_lineage["domains"]["channel.delivery"]["mode"] == "current"
        assert support_lineage["compatible"] is False

        candidate = _receipt(runtime, SCOPE, candidate_commit, "1.0.2")
        runtime._test_runtime_commit = candidate.commit
        report = record_release_lineage(
            runtime, scope=SCOPE, repo_root=repo, current_release=candidate
        )
        channel = report["domains"]["channel.delivery"]
        assert channel["changed"] is candidate_changes_channel
        if candidate_changes_channel:
            assert channel["mode"] == "changed_unverified"
            assert channel["gate_evidence"] == []
            assert channel["evidence_release"] == {}
        else:
            assert channel["mode"] == "inherited"
            assert channel["gate_evidence"] == [acceptance.record_id]
            assert channel["evidence_release"]["commit"] == support.commit
            assert channel["evidence_release"]["receipt_id"] == support.receipt_id
    finally:
        runtime.close()


def test_test_named_future_runtime_script_remains_unknown(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    prior_commit = _commit(repo, "docs/prior.md", "prior\n", "prior")
    current_commit = _commit(
        repo,
        "scripts/test_future_runtime.py",
        "RUNTIME_BEHAVIOR = True\n",
        "current",
    )

    report = release_lineage._domain_change_summary(
        repo,
        domain="memory.recall",
        ancestor=prior_commit,
        current=current_commit,
    )

    assert report is not None
    assert report["changed"] is True
    assert report["unknown_production_paths"] == [
        "scripts/test_future_runtime.py"
    ]


def test_hermes_nested_runtime_version_change_remains_unknown(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    prior_commit = _commit(
        repo,
        "integrations/hermes/eimemory/plugin.yaml",
        "name: eimemory\nversion: 1.0.0\nruntime:\n  version: 1\n",
        "prior",
    )
    current_commit = _commit(
        repo,
        "integrations/hermes/eimemory/plugin.yaml",
        "name: eimemory\nversion: 1.0.1\nruntime:\n  version: 2\n",
        "current",
    )

    report = release_lineage._domain_change_summary(
        repo,
        domain="memory.recall",
        ancestor=prior_commit,
        current=current_commit,
    )

    assert report is not None
    assert report["changed"] is True
    assert report["unknown_production_paths"] == [
        "integrations/hermes/eimemory/plugin.yaml"
    ]


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
            gate_evidence={"channel.delivery": [gate.record_id]},
        )

        domain = report["domains"]["channel.delivery"]
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
    record = _deployment_receipt_record(scope, commit, version, forged=forged)
    record = runtime.store.append(record)
    identity = verified_deployment_receipt_identity(record)
    if forged:
        assert identity is None
        return ReleaseIdentity(commit=commit, version=version, receipt_id=record.record_id, session_id=record.record_id)
    assert identity is not None
    return identity


def _deployment_receipt_record(
    scope: ScopeRef,
    commit: str,
    version: str,
    *,
    forged: bool = False,
) -> RecordEnvelope:
    release_path = f"/opt/eimemory/releases/{commit}"
    return RecordEnvelope.create(
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
                    "import_root": f"{release_path}/eimemory",
                    "package_tree_digest": runtime_package_tree_digest(),
                    "checks": {"ready": True},
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


def _recall_gate_pair(
    runtime: Runtime,
    scope: ScopeRef,
    release: ReleaseIdentity,
) -> tuple[RecordEnvelope, RecordEnvelope]:
    return (
        _gate(
            runtime,
            scope,
            release,
            source="eimemory.evaluation.production_recall",
        ),
        _gate(
            runtime,
            scope,
            release,
            source="eimemory.evaluation.production_recall.bootstrap",
        ),
    )


def _mock_recall_verifiers(
    monkeypatch: pytest.MonkeyPatch,
    evidence: dict[str, dict[str, str]],
) -> None:
    def verify_gate(*args, **kwargs):
        release = kwargs["release"]
        refs = evidence.get(release.commit)
        return {
            "ok": refs is not None,
            "status": "accepted" if refs is not None else "missing",
            "reason": "" if refs is not None else "production_recall_gate_missing",
            "record_id": "" if refs is None else refs["gate"],
        }

    def verify_strict(*args, **kwargs):
        release = kwargs["release"]
        refs = evidence.get(release.commit)
        return {
            "ok": refs is not None,
            "status": "strict_activated" if refs is not None else "missing",
            "reason": "" if refs is not None else "production_recall_strict_state_missing",
            "record_id": "" if refs is None else refs["strict"],
            "candidate_commit": release.commit if refs is not None else "",
            "gate_record_id": "" if refs is None else refs["gate"],
        }

    monkeypatch.setattr(real_query_gate, "verify_current_production_recall_gate", verify_gate)
    monkeypatch.setattr(
        real_query_gate,
        "verify_current_production_recall_strict_state",
        verify_strict,
    )


def _mock_pending_recall_verifiers(
    monkeypatch: pytest.MonkeyPatch,
    *,
    release: ReleaseIdentity,
    pending_record_id: str,
    core_manifest_record_id: str,
) -> None:
    monkeypatch.setattr(
        real_query_gate,
        "verify_current_bootstrap_data_pending",
        lambda *args, **kwargs: {
            "ok": kwargs.get("release") == release,
            "status": "bootstrap_data_pending",
            "reason": "production_dataset_not_ready",
            "record_id": pending_record_id,
            "release_identity": {
                "release_commit": release.commit,
                "release_version": release.version,
                "deployment_receipt_id": release.receipt_id,
                "release_session_id": release.session_id,
            },
        },
    )

    def verified_replay_summary(*_args, **kwargs):
        assert kwargs["scope"] == SCOPE
        assert kwargs["capabilities"] == {"memory.recall"}
        assert kwargs["release"] == release
        missing_field = kwargs["missing_field"]
        return {
            "executed_count": 3,
            "pass_count": 3,
            "fail_count": 0,
            "not_run_count": 0,
            "minimum_executed": 3,
            "manifest_record_ids": {
                "memory.recall": core_manifest_record_id,
            },
            "manifest_rejection_reasons": {},
            "rejection_reasons": {},
            missing_field: [],
        }

    monkeypatch.setattr(
        l5_readiness,
        "_verified_replay_summary",
        verified_replay_summary,
    )


def _live_case(
    runtime: Runtime,
    scope: ScopeRef,
    release: ReleaseIdentity,
    *,
    case_id: str,
    index: int,
) -> RecordEnvelope:
    observation_digest = f"{index + 1:064x}"
    task_type = f"live.acceptance.{case_id}"
    trace_id = (
        f"live-acceptance:{release.commit}:{case_id}:{observation_digest[:12]}"
    )
    return runtime.store.append(
        RecordEnvelope.create(
            kind="learning_eval",
            title=f"Live acceptance {case_id}",
            scope=scope,
            source="eimemory.live_task_acceptance",
            status="active",
            content={
                "report_type": "live_task_acceptance_case",
                "schema_version": "live_task_acceptance.v1",
                "evidence_class": "operational_probe",
                "case_id": case_id,
                "task_type": task_type,
                "trace_id": trace_id,
                "passed": True,
                "deployment_commit": release.commit,
                "deployment_version": release.version,
                "release_path": f"/opt/eimemory/releases/{release.commit}",
                "promotion_request_id": release.receipt_id,
                "release_session_id": release.session_id,
                "observation_digest": observation_digest,
            },
            meta={
                "report_type": "live_task_acceptance_case",
                "case_id": case_id,
                "task_type": task_type,
                "trace_id": trace_id,
                "passed": True,
            },
        )
    )


def _channel_case(
    runtime: Runtime,
    scope: ScopeRef,
    release: ReleaseIdentity,
    *,
    transport_owner: str = "hermes",
    platform: str = "telegram",
) -> RecordEnvelope:
    digest = "c" * 64
    return runtime.store.append(
        RecordEnvelope.create(
            kind="learning_eval",
            title="External channel acceptance",
            scope=scope,
            source="eimemory.external_channel.acceptance",
            status="active",
            content={
                "report_type": "external_channel_acceptance",
                "schema_version": "external_channel_acceptance.v1",
                "evidence_class": "external_channel_receipt",
                "passed": True,
                "deployment_commit": release.commit,
                "deployment_version": release.version,
                "promotion_request_id": release.receipt_id,
                "release_session_id": release.session_id,
                "transport_owner": transport_owner,
                "platform": platform,
                "conversation_kind": "direct",
                "platform_accepted_at_ms": 2_000,
                "inbound_message_digest": digest,
                "delivery_receipt_digest": digest,
                "channel_session_digest": digest,
            },
            meta={
                "report_type": "external_channel_acceptance",
                "schema_version": "external_channel_acceptance.v1",
                "evidence_class": "external_channel_receipt",
                "passed": True,
            },
        )
    )


def _manifest(
    runtime: Runtime,
    scope: ScopeRef,
    *,
    title: str,
    capabilities: list[str] | None = None,
) -> RecordEnvelope:
    return runtime.store.append(
        RecordEnvelope.create(
            kind="replay_result",
            title=f"{title} replay manifest",
            scope=scope,
            source="eimemory.capability_replay",
            status="active",
            content={
                "report_type": "capability_replay_manifest",
                "schema_version": "capability_replay_manifest.v1",
                **({"capabilities": list(capabilities)} if capabilities is not None else {}),
            },
            meta={"report_type": "capability_replay_manifest"},
        )
    )


class _QueryRows:
    def __init__(self, rows: list[dict[str, object]]) -> None:
        self._rows = rows

    def fetchall(self) -> list[dict[str, object]]:
        return list(self._rows)

    def fetchone(self) -> dict[str, object] | None:
        return self._rows[0] if self._rows else None


class _KeysetReceiptConnection:
    def __init__(
        self,
        *,
        current_rowid: int,
        current_record: RecordEnvelope,
        row_records: list[tuple[int, RecordEnvelope]],
    ) -> None:
        self.current_rowid = current_rowid
        self.current_record = current_record
        self.row_records = row_records
        self.queries: list[str] = []
        self.rowid_by_record = {
            current_record.record_id: current_rowid,
            **{record.record_id: rowid for rowid, record in row_records},
        }

    def execute(
        self,
        sql: str,
        params: tuple[object, ...],
    ) -> _QueryRows:
        self.queries.append(sql)
        if "SELECT rowid" in sql and "SELECT rowid," not in sql:
            rowid = self.rowid_by_record.get(str(params[0]))
            return _QueryRows([] if rowid is None else [{"rowid": rowid}])
        if "rowid < ?" in sql:
            cursor = int(params[-2])
            rows = [
                {
                    "rowid": rowid,
                    "record_id": record.record_id,
                    "source_id": record.source_id,
                }
                for rowid, record in self.row_records
                if rowid < cursor
            ][:1]
            return _QueryRows(rows)
        if "OFFSET" in sql:
            rowid, record = self.row_records[0]
            return _QueryRows(
                [
                    {
                        "rowid": rowid,
                        "record_id": record.record_id,
                        "source_id": record.source_id,
                    }
                ]
            )
        raise AssertionError(f"unexpected SQL: {sql}")


class _KeysetReceiptStore:
    def __init__(
        self,
        connection: _KeysetReceiptConnection,
        current_record: RecordEnvelope,
        row_records: list[tuple[int, RecordEnvelope]],
    ) -> None:
        self.sqlite = type("_SQLite", (), {"conn": connection})()
        self.records = {
            current_record.record_id: current_record,
            **{record.record_id: record for _, record in row_records},
        }

    def get_by_id(
        self,
        record_id: str,
        *,
        scope: ScopeRef,
    ) -> RecordEnvelope | None:
        return self.records.get(record_id)

    def get_by_exact_ref(
        self,
        record_id: str,
        *,
        scope: ScopeRef,
        source_id: str,
    ) -> RecordEnvelope | None:
        record = self.records.get(record_id)
        return (
            record
            if record is not None
            and record.scope == scope
            and record.source_id == source_id
            else None
        )


class _KeysetReceiptRuntime:
    def __init__(
        self,
        connection: _KeysetReceiptConnection,
        current_record: RecordEnvelope,
        row_records: list[tuple[int, RecordEnvelope]],
    ) -> None:
        self.store = _KeysetReceiptStore(
            connection,
            current_record,
            row_records,
        )


class _MissingCurrentRowConnection:
    def __init__(self) -> None:
        self.queries: list[str] = []

    def execute(
        self,
        sql: str,
        params: tuple[object, ...],
    ) -> _QueryRows:
        self.queries.append(sql)
        return _QueryRows([])


class _UnavailableOrderingStore:
    def __init__(
        self,
        *,
        sqlite_mode: str,
        current_record: RecordEnvelope,
        fallback_records: list[RecordEnvelope],
    ) -> None:
        connection = (
            None
            if sqlite_mode == "missing"
            else _MissingCurrentRowConnection()
        )
        self.sqlite = None if connection is None else type(
            "_SQLite",
            (),
            {"conn": connection},
        )()
        self.current_record = current_record
        self.fallback_records = fallback_records
        self.list_calls = 0
        self.queries = [] if connection is None else connection.queries

    def get_by_id(
        self,
        record_id: str,
        *,
        scope: ScopeRef,
    ) -> RecordEnvelope | None:
        return self.current_record if record_id == self.current_record.record_id else None

    def list_records(self, **kwargs) -> list[RecordEnvelope]:
        self.list_calls += 1
        return list(self.fallback_records) if self.list_calls == 1 else []


class _UnavailableOrderingRuntime:
    def __init__(
        self,
        *,
        sqlite_mode: str,
        current_record: RecordEnvelope,
        fallback_records: list[RecordEnvelope],
    ) -> None:
        self.store = _UnavailableOrderingStore(
            sqlite_mode=sqlite_mode,
            current_record=current_record,
            fallback_records=fallback_records,
        )
