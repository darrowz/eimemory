from __future__ import annotations

from dataclasses import replace

from eimemory.models.records import RecordEnvelope, ScopeRef
from threading import RLock

from eimemory.storage.sqlite_store import SqliteRecordStore as _Store


def SqliteRecordStore(path):
    store = _Store(path)
    lock = RLock()
    store.bind_runtime_lock(lock)
    lock.acquire()
    return store

OLD = ScopeRef(tenant_id="t", agent_id="old-agent", workspace_id="w", user_id="u")
NEW = replace(OLD, agent_id="new-agent")


def _rec(i: int, scope: ScopeRef = OLD) -> RecordEnvelope:
    return RecordEnvelope.create(kind="memory", title=f"title {i}", summary=f"alpha bravo {i}",
                                 detail=f"detail {i}", scope=scope)


def _fts_keys(store):
    return sorted(r[0] for r in store.conn.execute("SELECT storage_key FROM recall_index_fts"))


def _map_keys(store):
    return sorted(r[0] for r in store.conn.execute(
        "SELECT m.storage_key FROM recall_index_fts_keys m JOIN recall_index_fts f ON f.rowid = m.fts_rowid"))


def test_fts_key_map_tracks_upserts_rewrites_and_deletes(tmp_path):
    store = SqliteRecordStore(tmp_path / "s.sqlite")
    if not store._has_fts_table():
        return
    records = [_rec(i) for i in range(5)]
    for record in records:
        store.upsert(record)
        store.upsert(record)  # re-upsert must not duplicate FTS rows
    assert store._fts_key_map_ready()
    assert _fts_keys(store) == _map_keys(store) and len(_fts_keys(store)) == 5
    moved = replace(records[0], scope=NEW)
    store.rewrite(moved, previous_scope=OLD)
    assert _fts_keys(store) == _map_keys(store) and len(_fts_keys(store)) == 5


def test_existing_fts_rows_are_backfilled_into_map(tmp_path):
    path = tmp_path / "s.sqlite"
    store = SqliteRecordStore(path)
    if not store._has_fts_table():
        return
    for i in range(3):
        store.upsert(_rec(i))
    store.conn.execute("DROP TABLE recall_index_fts_keys")
    store.conn.commit()
    reopened = SqliteRecordStore(path)
    reopened.upsert(_rec(0))  # first write lazily builds the map
    assert reopened._fts_key_map_ready()
    assert _fts_keys(reopened) == _map_keys(reopened) and len(_map_keys(reopened)) == 4


def test_fts_delete_does_not_scan_virtual_table(tmp_path):
    store = SqliteRecordStore(tmp_path / "s.sqlite")
    if not store._has_fts_table():
        return
    store.upsert(_rec(1))
    plans = []
    store.conn.set_trace_callback(plans.append)
    store.upsert(_rec(2))
    store.conn.set_trace_callback(None)
    assert not any("DELETE FROM recall_index_fts WHERE storage_key" in p for p in plans)
