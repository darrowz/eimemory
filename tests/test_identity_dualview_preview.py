"""Synthetic exact-view checks; proposed decisions never grant authority."""
from copy import deepcopy
from dataclasses import asdict
import json

import pytest

from eimemory.api.runtime import Runtime
from eimemory.api.memory import MemoryAPI
from eimemory.cli import main as cli
from eimemory.identity_ops import _exact_ref, _repair_snapshot
from eimemory.models.records import RecordEnvelope, ScopeRef


@pytest.fixture
def pair(tmp_path):
    runtime = Runtime.create(root=tmp_path / "runtime")
    owner = ScopeRef(agent_id="hongtu", workspace_id="embodied", user_id="synthetic-owner")
    old = RecordEnvelope.create(kind="memory",
        title="Synthetic historical database", summary="Historical SQLite configuration",
        scope=ScopeRef(agent_id="main", user_id=owner.user_id), source_id="alpha",
        content={"text": "Historical SQLite configuration", "memory_type": "fact"})
    old.record_id = "mem_dualview"
    current = RecordEnvelope.from_dict(old.to_dict())
    current.scope = owner
    current.summary = "Current PostgreSQL configuration"
    current.content["text"] = current.summary
    with runtime.store.locked() as db:
        db.upsert(old)
        db.upsert(current)
    def capture(db):
        return {side + "_version": list(_repair_snapshot(db, _exact_ref(row))[1])
                for side, row in (("source", old), ("target", current))}
    versions = runtime.store.read_consistent(capture)
    manifest = {"schema": "identity_dualview_preview.v1", "owner_scope": asdict(owner),
        "source_id": "alpha", "items": [{"source_ref": list(_exact_ref(old)),
        "target_ref": list(_exact_ref(current)), **versions,
        "decision": "retain_both", "current": "target"}]}
    yield runtime, manifest, old, current, tmp_path
    runtime.close()


def preview(pair, manifest=None):
    from eimemory.identity_ops import preview_identity_manifest
    runtime, original, *_ = pair
    return preview_identity_manifest(runtime.store.sqlite.path, manifest or original)


def test_current_exact_and_historical_refs_are_unchanged(pair):
    runtime, manifest, old, current, _ = pair
    before = runtime.store.read_consistent(lambda db: [dict(r) for r in db.execute(
        "SELECT * FROM records ORDER BY storage_key").fetchall()])
    result = preview(pair)
    assert result["validation_ok"] is True
    assert result["verified_pair_count"] == result["current_exact_verified_count"] == 1
    assert result["ok"] is False and result["resolved_count"] == result["repaired_count"] == 0
    assert result["unresolved_count"] == result["manifest_pair_count"] == 1
    assert result["outcome_counts"] == {"owner_authorization_binding_unavailable": 1}
    with runtime.store.locked() as db:
        for row in (old, current):
            assert db.get_by_exact_ref(row.record_id, scope=row.scope, source_id=row.source_id).to_dict() == row.to_dict()
        assert [dict(r) for r in db.execute("SELECT * FROM records ORDER BY storage_key").fetchall()] == before
    assert result["database_changes"] == 0
    bundle = MemoryAPI(runtime.store).recall(query="PostgreSQL", scope=asdict(current.scope),
        task_context={"scope_strategy": "exact", "source_ids": ["alpha"]}, limit=3)
    assert [item.record_id for item in bundle.items] == [current.record_id]
    assert bundle.items[0].summary == current.summary


@pytest.mark.parametrize("field,value,reason", [
    ("decision", "revoked", "decision_revoked"),
    ("decision", "unresolved", "decision_unresolved"),
    ("decision", "superseded", "invalid_decision"),
    ("current", "source", "canonical_current_required"),
])
def test_decision_never_unblocks(pair, field, value, reason):
    manifest = deepcopy(pair[1]); manifest["items"][0][field] = value
    result = preview(pair, manifest)
    assert result["outcome_counts"] == {reason: 1}
    assert result["unresolved_count"] == 1 and result["resolved_count"] == 0


@pytest.mark.parametrize("side,index,value", [
    ("source", 0, "other-tenant"), ("target", 3, "other-owner"),
    ("source", 4, "foreign-source"), ("target", 4, "foreign-source"),
    ("source", 1, "other-agent"), ("target", 2, "other-workspace"),
])
def test_foreign_scope_source_is_rejected(pair, side, index, value):
    manifest = deepcopy(pair[1]); manifest["items"][0][side + "_ref"][index] = value
    result = preview(pair, manifest)
    assert result["validation_ok"] is False and result["verified_pair_count"] == 0
    assert result["unresolved_count"] == 1


@pytest.mark.parametrize("side", ["source", "target"])
@pytest.mark.parametrize("component", [0, 1, 2])
def test_entire_both_side_version_tokens_are_compared(pair, side, component):
    manifest = deepcopy(pair[1]); manifest["items"][0][side + "_version"][component] = "0" * 64
    result = preview(pair, manifest)
    assert result["outcome_counts"] == {side + "_version_changed": 1}
    assert result["validation_ok"] is False


@pytest.mark.parametrize("side", ["source", "target"])
def test_real_projection_drift_stays_blocked(pair, side):
    runtime, _, old, current, _ = pair
    row = old if side == "source" else current
    with runtime.store.locked() as db:
        db.execute("UPDATE records SET summary = ? WHERE storage_key = ?",
                   ("changed projection", db._storage_key(row)))
        db.commit()
    result = preview(pair)
    assert result["validation_ok"] is False and result["unresolved_count"] == 1


@pytest.mark.parametrize("side", ["source", "target"])
def test_real_full_payload_version_change_stays_blocked(pair, side):
    runtime, _, old, current, _ = pair
    row = deepcopy(old if side == "source" else current)
    row.content["text"] = "new complete version"
    with runtime.store.locked() as db:
        db.upsert(row)
    result = preview(pair)
    assert result["outcome_counts"] == {side + "_version_changed": 1}
    assert result["unresolved_count"] == 1


def test_source_partition_does_not_authorize_other_producer(pair):
    runtime, manifest, _, current, _ = pair
    current.source = "foreign.producer"
    with runtime.store.locked() as db:
        db.upsert(current)
    result = preview(pair)
    assert result["outcome_counts"] == {"canonical_target_mismatch": 1}


def test_duplicate_conflicting_decisions_block_all_entries(pair):
    manifest = deepcopy(pair[1]); manifest["items"].append(deepcopy(manifest["items"][0]))
    manifest["items"][1]["decision"] = "revoked"
    result = preview(pair, manifest)
    assert result["outcome_counts"] == {"conflicting_manifest_entries": 2}
    assert result["unresolved_count"] == 2 and result["resolved_count"] == 0


def test_partially_declared_resolution_cannot_hide_unresolved_ledger(pair):
    manifest = deepcopy(pair[1]); second = deepcopy(manifest["items"][0])
    second["source_ref"][5] = second["target_ref"][5] = "mem_missing"
    second["decision"] = "unresolved"
    manifest["items"].append(second)
    manifest["items"][0]["authorization"] = {
        "reviewer": "owner", "signature": "invented", "source_trust": "user_explicit"}
    result = preview(pair, manifest)
    assert result["manifest_pair_count"] == result["unresolved_count"] == 2
    assert result["resolved_count"] == result["repaired_count"] == 0


def test_cli_is_readonly_and_exit_reports_unresolved(pair, monkeypatch, capsys):
    runtime, manifest, _, _, tmp = pair
    path = tmp / "manifest.json"; path.write_text(json.dumps(manifest))
    monkeypatch.setattr(cli.Runtime, "create", lambda **kw: pytest.fail("no runtime bootstrap"))
    monkeypatch.setattr(cli, "load_settings", lambda: pytest.fail("no production configuration"))
    assert cli.main(["identity", "manifest-preview", "--database", str(runtime.store.sqlite.path),
                     "--manifest", str(path)]) == 1
    result = json.loads(capsys.readouterr().out)
    assert result["validation_ok"] is True and result["apply"] is False
    path.write_text('{"items": [], "items": []}')
    assert cli.main(["identity", "manifest-preview", "--database", str(runtime.store.sqlite.path),
                     "--manifest", str(path)]) == 2
    assert json.loads(capsys.readouterr().out)["error"] == "identity_manifest_invalid"


def test_missing_database_is_not_created(pair):
    from eimemory.identity_ops import preview_identity_manifest
    missing = pair[-1] / "missing.sqlite"
    import sqlite3
    with pytest.raises(sqlite3.OperationalError):
        preview_identity_manifest(missing, pair[1])
    assert not missing.exists()


@pytest.mark.parametrize("mutation", [
    lambda m: m.update(owner_scope={}),
    lambda m: m.update(items=[]),
    lambda m: m.update(items=[{}] * 1001),
    lambda m: m.update(source_id=""),
])
def test_invalid_manifest_is_rejected_before_database_open(pair, mutation):
    manifest = deepcopy(pair[1]); mutation(manifest)
    from eimemory.identity_ops import preview_identity_manifest
    with pytest.raises(ValueError, match="identity_manifest_invalid"):
        preview_identity_manifest(pair[-1] / "absent.sqlite", manifest)
