"""Interrupted projection maintenance must resume from committed authority only."""
from contextlib import closing
from concurrent.futures import ThreadPoolExecutor
import os
import sqlite3
from threading import Event

import pytest

from eimemory.models.records import RecordEnvelope, ScopeRef
from eimemory.retrieval.incremental_sync import SnapshotProjectionReader, delta_snapshot
from eimemory.retrieval.postgres_sync import ProjectionCursor, SQLiteProjectionReader
from eimemory.storage.runtime_store import RuntimeStore


def _seed(root, *, memory_only=True):
    store = RuntimeStore(root)
    record = RecordEnvelope.create(kind="memory", title="committed fact", scope=ScopeRef())
    store.append(record)
    reader = SQLiteProjectionReader(store, projection_memory_only=memory_only)
    reader.page(ProjectionCursor(), limit=10)
    return store, record, reader


def test_interrupted_snapshot_never_publishes_partial_database(tmp_path, monkeypatch):
    store, record, reader = _seed(tmp_path / "authority")
    with closing(store):
        path = tmp_path / "derived" / "snapshot.sqlite"
        original_page = reader.page
        calls = 0

        def interrupted_page(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise KeyboardInterrupt("synthetic process interruption")
            return original_page(*args, **kwargs)

        monkeypatch.setattr(reader, "page", interrupted_page)
        with pytest.raises(KeyboardInterrupt):
            SnapshotProjectionReader(reader, path=path, fingerprint="test-contract")
        assert not path.exists(), "a partial snapshot must not become resumable state"
        assert not store.sqlite.in_transaction
        monkeypatch.setattr(reader, "page", original_page)
        with closing(SnapshotProjectionReader(reader, path=path, fingerprint="test-contract")) as snapshot:
            assert [row["record_id"] for row in snapshot.page(ProjectionCursor(), limit=10)] == [record.record_id]


@pytest.mark.parametrize("partial_state", ["empty", "schema_only", "empty_contract"])
def test_legacy_interrupted_snapshot_is_rebuilt_from_authority(tmp_path, partial_state):
    store, record, reader = _seed(tmp_path / "authority")
    with closing(store):
        path = tmp_path / "snapshot.sqlite"
        with closing(sqlite3.connect(path)) as partial:
            if partial_state != "empty":
                partial.execute("CREATE TABLE projections(updated_at TEXT, storage_key TEXT PRIMARY KEY, payload TEXT)")
            if partial_state == "empty_contract":
                partial.execute("CREATE TABLE contract(fingerprint TEXT, revision TEXT)")
            partial.commit()
        with closing(SnapshotProjectionReader(reader, path=path, fingerprint="test-contract")) as snapshot:
            assert snapshot.snapshot_token() == reader.snapshot_token()
            assert [row["record_id"] for row in snapshot.page(ProjectionCursor(), limit=10)] == [record.record_id]


@pytest.mark.parametrize("memory_only", [False, True])
def test_durable_snapshot_refuses_uncommitted_caller_rows(tmp_path, memory_only):
    store, record, reader = _seed(tmp_path / "authority", memory_only=memory_only)
    with closing(store):
        path = tmp_path / "snapshot.sqlite"
        with store.locked() as sqlite:
            sqlite.execute("BEGIN")
            sqlite.execute("SAVEPOINT caller_work")
            sqlite.execute("UPDATE records SET title='uncommitted' WHERE record_id=?", (record.record_id,))
            with pytest.raises(RuntimeError, match="projection_snapshot_requires_own_transaction"):
                SnapshotProjectionReader(reader, path=path, fingerprint="test-contract")
            assert sqlite.in_transaction
            assert not path.exists()
            sqlite.execute("ROLLBACK TO SAVEPOINT caller_work")
            sqlite.execute("RELEASE SAVEPOINT caller_work")
            sqlite.rollback()
        with closing(SnapshotProjectionReader(reader, path=path, fingerprint="test-contract")) as snapshot:
            assert snapshot.page(ProjectionCursor(), limit=10)[0]["title"] == "committed fact"


def test_delta_snapshot_refuses_uncommitted_caller_rows(tmp_path):
    store, record, reader = _seed(tmp_path / "authority")
    with closing(store):
        since = reader.snapshot_token()
        with store.locked() as sqlite:
            sqlite.execute("BEGIN")
            sqlite.execute("SAVEPOINT caller_work")
            sqlite.execute("UPDATE records SET title='uncommitted' WHERE record_id=?", (record.record_id,))
            with pytest.raises(RuntimeError, match="projection_snapshot_requires_own_transaction"):
                delta_snapshot(reader, since=since, limit=4)
            assert sqlite.in_transaction
            sqlite.execute("ROLLBACK TO SAVEPOINT caller_work")
            sqlite.execute("RELEASE SAVEPOINT caller_work")
            sqlite.rollback()
        assert delta_snapshot(reader, since=since, limit=4)["rows"] == []


def test_complete_snapshot_with_wrong_contract_is_preserved_and_rejected(tmp_path):
    store, _record, reader = _seed(tmp_path / "authority")
    with closing(store):
        path = tmp_path / "snapshot.sqlite"
        with closing(SnapshotProjectionReader(reader, path=path, fingerprint="original-contract")):
            pass
        original = path.read_bytes()
        with pytest.raises(ValueError, match="projection_snapshot_contract_mismatch"):
            SnapshotProjectionReader(reader, path=path, fingerprint="different-contract")
        assert path.read_bytes() == original


def test_concurrent_builders_share_one_complete_snapshot(tmp_path, monkeypatch):
    store, record, reader = _seed(tmp_path / "authority")
    with closing(store):
        path = tmp_path / "snapshot.sqlite"
        entered, release, second_started = Event(), Event(), Event()
        original_page = reader.page

        def paused_page(*args, **kwargs):
            if not entered.is_set():
                entered.set()
                assert release.wait(5)
            return original_page(*args, **kwargs)

        def build_snapshot(*, second=False):
            if second:
                second_started.set()
            with closing(SnapshotProjectionReader(reader, path=path, fingerprint="test-contract")) as snapshot:
                return (snapshot.revision, snapshot._file_identity,
                        [row["record_id"] for row in snapshot.page(ProjectionCursor(), limit=10)])

        monkeypatch.setattr(reader, "page", paused_page)
        with ThreadPoolExecutor(max_workers=2) as executor:
            first = executor.submit(build_snapshot)
            assert entered.wait(3)
            second = executor.submit(build_snapshot, second=True)
            try:
                assert second_started.wait(3)
                assert not path.exists()
            finally:
                release.set()
            snapshots = [first.result(timeout=5), second.result(timeout=5)]
        assert snapshots[0] == snapshots[1]
        assert snapshots[0][0] == reader.snapshot_token()
        assert snapshots[0][2] == [record.record_id]


def test_completed_snapshot_cleanup_preserves_a_newer_generation(tmp_path):
    store, _record, reader = _seed(tmp_path / "authority")
    with closing(store):
        path = tmp_path / "snapshot.sqlite"
        completed = SnapshotProjectionReader(reader, path=path, fingerprint="test-contract")
        completed.close()
        store.append(RecordEnvelope.create(kind="memory", title="next fact", scope=ScopeRef()))
        replacement_path = tmp_path / "next.sqlite"
        with closing(SnapshotProjectionReader(reader, path=replacement_path, fingerprint="test-contract")) as replacement:
            next_revision = replacement.revision
        os.replace(replacement_path, path)
        completed.discard()
        with closing(SnapshotProjectionReader(reader, path=path, fingerprint="test-contract")) as resumed:
            assert resumed.revision == next_revision != completed.revision
        resumed.discard()
        assert not path.exists()
        resumed.discard()  # another completed owner may have removed it already
