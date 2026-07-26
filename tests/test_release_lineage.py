from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
import subprocess

import pytest

import eimemory.evaluation.real_query_gate as real_query_gate
import eimemory.governance.l5_readiness as l5_readiness
import eimemory.governance.release_lineage as release_lineage
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
from eimemory.governance.live_task_acceptance import LIVE_ACCEPTANCE_CASE_IDS
from eimemory.models.records import RecordEnvelope, ScopeRef
from eimemory.runtime_identity import runtime_package_tree_digest


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
        resolved["domains"]["memory.recall"]["mode"] = "current"
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

        assert report["domains"]["channel.openclaw"]["mode"] == "changed_unverified"
        assert report["domains"]["channel.openclaw"]["changed_paths"] == [
            "deploy/systemd/openclaw-loop.service"
        ]
    finally:
        runtime.close()


def test_version_only_project_metadata_remains_inheritable(tmp_path: Path) -> None:
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

        assert {state["mode"] for state in report["domains"].values()} == {"inherited"}
        assert report["unknown_production_paths"] == []
    finally:
        runtime.close()


@pytest.mark.parametrize(
    ("prior", "current", "expected_mode"),
    [
        ('__version__ = "1.0.0"\n', '__version__ = "1.0.1"\n', "inherited"),
        (
            '__version__ = "1.0.0"\nCHANNEL = "stable"\n',
            '__version__ = "1.0.1"\nCHANNEL = "preview"\n',
            "changed_unverified",
        ),
    ],
)
def test_version_module_only_ignores_the_release_literal(
    tmp_path: Path,
    prior: str,
    current: str,
    expected_mode: str,
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

        assert {state["mode"] for state in report["domains"].values()} == {expected_mode}
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
            "channel.openclaw",
            "storage.integrity",
            "deployment.runtime",
        }
    finally:
        runtime.close()


def test_openclaw_requires_complete_canonical_live_acceptance_set(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    prior_commit = _commit(repo, "integrations/openclaw/index.ts", "prior\n", "prior")
    current_commit = _commit(repo, "integrations/openclaw/index.ts", "changed\n", "current")
    runtime = Runtime.create(root=tmp_path / "runtime")
    try:
        _receipt(runtime, SCOPE, prior_commit, "1.0.0")
        current = _receipt(runtime, SCOPE, current_commit, "1.0.1")
        runtime._test_runtime_commit = current.commit
        case_records = [
            _live_case(runtime, SCOPE, current, case_id=case_id, index=index)
            for index, case_id in enumerate(LIVE_ACCEPTANCE_CASE_IDS)
        ]

        partial = record_release_lineage(
            runtime,
            scope=SCOPE,
            repo_root=repo,
            current_release=current,
            gate_evidence={"channel.openclaw": [case_records[0].record_id]},
        )
        complete = record_release_lineage(
            runtime,
            scope=SCOPE,
            repo_root=repo,
            current_release=current,
            gate_evidence={
                "channel.openclaw": [record.record_id for record in case_records]
            },
        )
        resolved = current_release_lineage(
            runtime,
            scope=SCOPE,
            repo_root=repo,
            current_release=current,
        )

        assert partial["domains"]["channel.openclaw"]["mode"] == "changed_unverified"
        assert complete["domains"]["channel.openclaw"]["mode"] == "current"
        assert (
            evidence_release_for_domain(
                runtime,
                scope=SCOPE,
                repo_root=repo,
                domain="channel.openclaw",
                current_release=current,
                expected_record_id=resolved["record_id"],
            )
            == current
        )
    finally:
        runtime.close()


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

        weak_manifest = _manifest(runtime, SCOPE, title="weak")
        core_manifest = _manifest(runtime, SCOPE, title="core")

        def verified_summary(*args, **kwargs):
            missing_field = kwargs["missing_field"]
            weak = missing_field.startswith("weak_")
            capabilities = set(kwargs["capabilities"])
            record_id = weak_manifest.record_id if weak else core_manifest.record_id
            return {
                "executed_count": len(capabilities) * 3,
                "pass_count": len(capabilities) * 3,
                "fail_count": 0,
                "not_run_count": 0,
                "minimum_executed": len(capabilities) * 3,
                missing_field: [],
                "rejection_reasons": {},
                "manifest_record_ids": {
                    capability: record_id for capability in capabilities
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


def _manifest(
    runtime: Runtime,
    scope: ScopeRef,
    *,
    title: str,
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
