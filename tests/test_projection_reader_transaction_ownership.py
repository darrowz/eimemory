"""Full-module projection reader transaction ownership against real SQLite.

Every store is synthetic under tmp_path. No PostgreSQL/provider/shell extraction.
"""
from contextlib import contextmanager

import pytest

from eimemory.models.records import RecordEnvelope, ScopeRef
from eimemory.retrieval.incremental_sync import SnapshotProjectionReader, delta_snapshot
from eimemory.retrieval.postgres_sync import ProjectionCursor, SQLiteProjectionReader
from eimemory.storage.runtime_store import RuntimeStore

ERROR = 'projection_reader_bootstrap_requires_own_transaction'


@contextmanager
def seeded_store(path):
    store = RuntimeStore(path)
    try:
        record = RecordEnvelope.create(kind='memory', title='committed', scope=ScopeRef())
        store.append(record)
        yield store, record
    finally:
        store.close()


def title(sqlite, record):
    return sqlite.execute('SELECT title FROM records WHERE record_id=?', (record.record_id,)).fetchone()[0]


@pytest.mark.parametrize(('memory_only', 'entry'), [(False,'page'), (False,'snapshot_token'), (True,'page')])
@pytest.mark.parametrize('caller_outcome', ['rollback', 'commit'])
def test_first_bootstrap_rejects_caller_transaction_without_touching_it(tmp_path, memory_only, entry, caller_outcome):
    with seeded_store(tmp_path / 'authority') as (store, record):
        reader = SQLiteProjectionReader(store, projection_memory_only=memory_only)
        with store.locked() as sqlite:
            sqlite.execute('BEGIN')
            sqlite.execute('UPDATE records SET title=? WHERE record_id=?', ('pending', record.record_id))
            sqlite.execute('SAVEPOINT caller_work')
            statements = []
            sqlite.conn.set_trace_callback(statements.append)
            try:
                with pytest.raises(RuntimeError, match=ERROR):
                    if entry == 'page':
                        reader.page(ProjectionCursor(), limit=10)
                    else:
                        reader.snapshot_token()
            finally:
                sqlite.conn.set_trace_callback(None)
            assert sqlite.in_transaction
            assert reader._index_ready is False
            assert title(sqlite, record) == 'pending'
            assert not statements  # guard precedes SQL, including schema writes.
            sqlite.execute('RELEASE SAVEPOINT caller_work')
            getattr(sqlite, caller_outcome)()
            assert title(sqlite, record) == ('committed' if caller_outcome == 'rollback' else 'pending')
        rows = reader.page(ProjectionCursor(), limit=10)
        assert reader._index_ready is True
        assert rows[0]['title'] == ('committed' if caller_outcome == 'rollback' else 'pending')
        assert not store.sqlite.in_transaction
        before = int(reader.snapshot_token())
        store.append(RecordEnvelope.create(kind='memory', title='next', scope=ScopeRef()))
        assert int(reader.snapshot_token()) > before


def test_memory_reader_construction_rejects_before_bootstrap_in_caller_transaction(tmp_path):
    with seeded_store(tmp_path / 'authority') as (store, record):
        with store.locked() as sqlite:
            before = {row[0] for row in sqlite.execute("SELECT name FROM sqlite_master WHERE name LIKE 'memory_vector%'")}
            sqlite.execute('BEGIN')
            sqlite.execute('UPDATE records SET title=? WHERE record_id=?', ('pending', record.record_id))
            with pytest.raises(RuntimeError, match=ERROR):
                SQLiteProjectionReader(store, projection_memory_only=True)
            assert sqlite.in_transaction and title(sqlite, record) == 'pending'
            after = {row[0] for row in sqlite.execute("SELECT name FROM sqlite_master WHERE name LIKE 'memory_vector%'")}
            assert after == before
            sqlite.rollback()
            assert title(sqlite, record) == 'committed'
        reader = SQLiteProjectionReader(store, projection_memory_only=True)
        assert reader.snapshot_token().isdigit()
        assert reader.page(ProjectionCursor(), limit=10)[0]['title'] == 'committed'


@pytest.mark.parametrize('memory_only', [False, True])
def test_initialized_reader_preserves_caller_savepoint_and_snapshot(tmp_path, memory_only):
    with seeded_store(tmp_path / 'authority') as (store, record):
        reader = SQLiteProjectionReader(store, projection_memory_only=memory_only)
        reader.page(ProjectionCursor(), limit=10)
        initial = reader.snapshot_token()
        with store.locked() as sqlite:
            sqlite.execute('BEGIN')
            sqlite.execute('SAVEPOINT caller_work')
            sqlite.execute('UPDATE records SET title=? WHERE record_id=?', ('pending', record.record_id))
            assert reader.page(ProjectionCursor(), limit=10)[0]['title'] == 'pending'
            assert reader.snapshot_token() != initial
            assert sqlite.in_transaction and reader._index_ready
            sqlite.execute('ROLLBACK TO SAVEPOINT caller_work')
            assert reader.page(ProjectionCursor(), limit=10)[0]['title'] == 'committed'
            assert reader.snapshot_token() == initial
            sqlite.execute('RELEASE SAVEPOINT caller_work')
            sqlite.rollback()
        assert reader.page(ProjectionCursor(), limit=10)[0]['title'] == 'committed'
        assert reader.snapshot_token() == initial
        assert not store.sqlite.in_transaction


def test_failed_owned_bootstrap_rolls_back_schema_and_can_retry(tmp_path):
    import sqlite3
    with seeded_store(tmp_path / 'authority') as (store, record):
        with store.locked() as sqlite:
            sqlite.execute('CREATE INDEX idx_records_vector_sync_cursor ON records(storage_key)')
            sqlite.commit()
            reader = SQLiteProjectionReader(store)
            def authorizer(action, arg1, arg2, database, trigger):
                if action == sqlite3.SQLITE_CREATE_TRIGGER and arg1 == 'trg_recall_alias_vector_sync_insert':
                    return sqlite3.SQLITE_DENY
                return sqlite3.SQLITE_OK
            sqlite.conn.set_authorizer(authorizer)
            try:
                with pytest.raises(sqlite3.DatabaseError, match='not authorized'):
                    reader.page(ProjectionCursor(), limit=10)
            finally:
                sqlite.conn.set_authorizer(None)
            assert not sqlite.in_transaction
            assert reader._index_ready is False
            assert [row['name'] for row in sqlite.execute('PRAGMA index_info(idx_records_vector_sync_cursor)')] == ['storage_key']
            assert title(sqlite, record) == 'committed'
        assert reader.page(ProjectionCursor(), limit=10)[0]['title'] == 'committed'
        assert reader._index_ready
        with store.locked() as sqlite:
            assert [row['name'] for row in sqlite.execute('PRAGMA index_info(idx_records_vector_sync_cursor)')] == ['updated_at','storage_key']
        assert not store.sqlite.in_transaction


def test_fresh_reader_keeps_snapshot_and_delta_entry_points_working(tmp_path):
    with seeded_store(tmp_path / 'authority') as (store, record):
        reader = SQLiteProjectionReader(store, projection_memory_only=True)
        snapshot = SnapshotProjectionReader(reader, path=tmp_path / 'snapshot.sqlite', fingerprint='synthetic')
        try:
            initial = snapshot.snapshot_token()
            assert snapshot.page(ProjectionCursor(), limit=10)[0]['record_id'] == record.record_id
        finally:
            snapshot.close()
        assert not store.sqlite.in_transaction
        added = store.append(RecordEnvelope.create(kind='memory',title='delta',scope=ScopeRef()))
        fresh_reader = SQLiteProjectionReader(store, projection_memory_only=True)
        result = delta_snapshot(fresh_reader, since=initial, limit=4)
        assert [row['record_id'] for row in result['rows']] == [added.record_id]
        assert result['revision'] == result['current_revision'] == fresh_reader.snapshot_token()
        assert not store.sqlite.in_transaction
