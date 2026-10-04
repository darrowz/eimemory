"""Status repair must export the exact scoped rows it repaired."""
from __future__ import annotations

import pytest

from eimemory.models.records import RecordEnvelope, ScopeRef
from eimemory.storage.record_export import _scope_partition, exported_records_dir
from eimemory.storage.runtime_store import RuntimeStore


@pytest.fixture
def store(tmp_path):
    value = RuntimeStore(tmp_path)
    try:
        yield value
    finally:
        value.close()


def record(user, timestamp, source_id="default"):
    item = RecordEnvelope.create(
        kind="memory", title=f"Audit {user}", summary="Scoped repair evidence",
        scope=ScopeRef(tenant_id="audit", agent_id="agent", workspace_id="workspace", user_id=user),
        source="audit", source_id=source_id,
    )
    item.record_id = "audit_shared_id"
    item.time.created_at = item.time.updated_at = timestamp
    return item


def make_stale(store, records):
    with store.locked() as sql:
        for item in records:
            sql.execute("UPDATE records SET status='superseded' WHERE storage_key=?", (sql._storage_key(item),))
        sql.commit()


@pytest.mark.parametrize("scope_mode", ["one_scope", "all_scopes"])
def test_status_repair_keeps_exact_scope_through_exports_and_rebuild(store, scope_mode):
    older = record("one", "2026-10-01T00:00:00+00:00", "source-one")
    newer = record("two", "2099-01-01T00:00:00+00:00", "source-two")
    store.append(older)
    store.append(newer)
    changed = [older] if scope_mode == "one_scope" else [older, newer]
    make_stale(store, changed)
    result = store.repair_status_projection_mismatches(
        scope=older.scope if scope_mode == "one_scope" else None,
        record_ids=[older.record_id],
    )
    assert result["repaired_count"] == len(changed)
    assert result["repaired_record_ids"] == [item.record_id for item in changed]
    assert "repaired_record_refs" not in result
    for item in changed:
        path = exported_records_dir(store.root) / _scope_partition(item.scope) / f"{item.record_id}.md"
        assert "Status: `superseded`" in path.read_text()
    report = store.rebuild_sqlite_from_jsonl(replace=True)
    assert report["ok"], report
    for item in changed:
        assert store.get_by_exact_ref(item.record_id, scope=item.scope, source_id=item.source_id).status == "superseded"
    if scope_mode == "one_scope":
        assert store.get_by_exact_ref(newer.record_id, scope=newer.scope, source_id=newer.source_id).status == "active"


def test_missing_exact_repair_identity_rolls_back_instead_of_falling_back(store, monkeypatch):
    item = record("one", "2026-10-01T00:00:00+00:00")
    store.append(item)
    make_stale(store, [item])
    original = store.sqlite.repair_status_projection_mismatches

    def discard_identity(**kwargs):
        result = original(**kwargs)
        result.pop("repaired_record_refs")
        return result

    monkeypatch.setattr(store.sqlite, "repair_status_projection_mismatches", discard_identity)
    with pytest.raises(RuntimeError, match="status projection repair identity missing"):
        store.repair_status_projection_mismatches(scope=item.scope, record_ids=[item.record_id])
    with store.locked() as sql:
        row = sql.execute("SELECT status, json_extract(payload_json, '$.status') FROM records WHERE storage_key=?",
                          (sql._storage_key(item),)).fetchone()
        assert tuple(row) == ("superseded", "active")
        assert not sql.in_transaction


def test_empty_targeted_repair_returns_compatible_empty_refs(store):
    result = store.repair_status_projection_mismatches(scope=None, record_ids=[])
    assert result["repaired_count"] == 0
    assert result["repaired_record_ids"] == []
    assert "repaired_record_refs" not in result


@pytest.mark.parametrize("fault", ["missing", "wrong_source", "wrong_scope"])
def test_exact_hydration_failure_rolls_back_without_export(store, monkeypatch, fault):
    from copy import deepcopy

    item = record("one", "2026-10-01T00:00:00+00:00")
    store.append(item)
    make_stale(store, [item])
    projection = exported_records_dir(store.root) / _scope_partition(item.scope) / f"{item.record_id}.md"
    before_projection = projection.read_bytes()
    with store.locked() as sql:
        before_outbox = sql.execute("SELECT COUNT(*) FROM export_outbox").fetchone()[0]
    wrong = deepcopy(item)
    if fault == "wrong_source":
        wrong.source_id = "another-source"
    else:
        wrong.scope = ScopeRef(tenant_id="different-tenant")
    monkeypatch.setattr(store.sqlite, "get_by_exact_ref", lambda *_args, **_kwargs: None if fault == "missing" else wrong)
    with pytest.raises(RuntimeError, match="status projection repair hydration failed"):
        store.repair_status_projection_mismatches(scope=item.scope, record_ids=[item.record_id])
    with store.locked() as sql:
        row = sql.execute("SELECT status, json_extract(payload_json, '$.status') FROM records WHERE storage_key=?",
                          (sql._storage_key(item),)).fetchone()
        assert tuple(row) == ("superseded", "active")
        assert not sql.in_transaction
        assert sql.execute("SELECT COUNT(*) FROM export_outbox").fetchone()[0] == before_outbox
    assert projection.read_bytes() == before_projection


def test_sqlite_repair_returns_complete_exact_refs_for_internal_handoff(store):
    item = record("one", "2026-10-01T00:00:00+00:00", "specific-source")
    store.append(item)
    make_stale(store, [item])
    with store.locked() as sql:
        sql.execute("BEGIN IMMEDIATE")
        result = sql.repair_status_projection_mismatches(scope=item.scope, record_ids=[item.record_id], commit=False)
        assert result["repaired_record_refs"] == [{
            "record_id": item.record_id,
            "scope": {"tenant_id": "audit", "agent_id": "agent", "workspace_id": "workspace", "user_id": "one"},
            "source_id": "specific-source",
        }]
        assert result["repaired_record_ids"] == [item.record_id]
        sql.rollback()
