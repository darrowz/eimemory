from contextlib import closing
import sqlite3
import threading
from time import monotonic

import pytest

from eimemory.models.records import ScopeRef
from eimemory.retrieval.contracts import CandidateRequest
from eimemory.retrieval.sqlite_source import SQLiteCandidateSource
from eimemory.storage.runtime_store import RuntimeStore


LONG_READ = ('WITH RECURSIVE n(x) AS (VALUES(1) UNION ALL SELECT x+1 FROM n WHERE x<1000000) '
             'SELECT sum(x) FROM n')


def test_sqlite_read_deadline_interrupts_vm_and_restores_connection(tmp_path):
    from eimemory.storage.recall_deadline import RecallReadDeadlineExceeded, recall_read_scope
    with closing(RuntimeStore(tmp_path)) as store:
        connection = store.sqlite.conn
        previous_busy = connection.execute('PRAGMA busy_timeout').fetchone()[0]
        started = monotonic()
        with pytest.raises(RecallReadDeadlineExceeded):
            with recall_read_scope(store, {'_recall_collection_deadline_monotonic': started + .03}):
                store.sqlite.conn.execute(LONG_READ).fetchone()
        assert monotonic() - started < .5
        assert store.sqlite.conn is connection
        assert connection.execute('PRAGMA busy_timeout').fetchone()[0] == previous_busy
        connection.execute('CREATE TABLE after_deadline(value INTEGER)')
        connection.execute('INSERT INTO after_deadline VALUES(1)')
        assert connection.execute('SELECT value FROM after_deadline').fetchone()[0] == 1


def test_nested_read_deadline_restores_outer_scope_and_propagates_real_sql_errors(tmp_path):
    from eimemory.storage.recall_deadline import RecallReadDeadlineExceeded, recall_read_scope
    with closing(RuntimeStore(tmp_path)) as store:
        original = store.sqlite.conn
        with recall_read_scope(store, {'_recall_collection_deadline_monotonic': monotonic() + 2}):
            outer = store.sqlite.conn
            with pytest.raises(RecallReadDeadlineExceeded):
                with recall_read_scope(store, {'_recall_collection_deadline_monotonic': monotonic() + .03}):
                    store.sqlite.conn.execute(LONG_READ).fetchone()
            assert store.sqlite.conn is outer
            assert store.sqlite.conn.execute('SELECT 1').fetchone()[0] == 1
            with pytest.raises(sqlite3.OperationalError, match='no such table'):
                store.sqlite.conn.execute('SELECT * FROM nonexistent_deadline_test_table')
        assert store.sqlite.conn is original


def test_waiting_for_store_lock_obeys_recall_deadline(tmp_path):
    from eimemory.storage.recall_deadline import RecallReadDeadlineExceeded, recall_read_scope
    with closing(RuntimeStore(tmp_path)) as store:
        acquired, release = threading.Event(), threading.Event()
        def hold_lock():
            with store._lock:
                acquired.set()
                release.wait(2)
        thread = threading.Thread(target=hold_lock)
        thread.start()
        assert acquired.wait(1)
        try:
            started = monotonic()
            with pytest.raises(RecallReadDeadlineExceeded):
                with recall_read_scope(store, {'_recall_collection_deadline_monotonic': started + .03}):
                    pytest.fail('a lock unavailable before deadline must not be entered')
            assert monotonic() - started < .5
        finally:
            release.set()
            thread.join(2)
        assert not thread.is_alive()


def test_source_reports_interrupted_sql_as_incomplete_not_empty_success(tmp_path, monkeypatch):
    with closing(RuntimeStore(tmp_path)) as store:
        source = SQLiteCandidateSource(store)
        def slow_candidates(**kwargs):
            store.sqlite.conn.execute(LONG_READ).fetchone()
            return [], {'candidate_count': 0}
        monkeypatch.setattr(store.sqlite, '_candidate_rows', slow_candidates)
        request = CandidateRequest.create(query='ordinary lookup', scope=ScopeRef(user_id='owner'), limit=6,
            recall_filters={'_recall_collection_deadline_monotonic': monotonic() + .03})
        batch = source.search(request)
        assert batch.hits == ()
        assert batch.diagnostic_dict()['drops'].get('recall_budget_exhausted') == 1
        assert store.sqlite.conn.execute('SELECT 1').fetchone()[0] == 1


def test_busy_timeout_is_refreshed_for_each_statement_including_cursor(tmp_path, monkeypatch):
    from eimemory.storage.recall_deadline import recall_read_scope
    with closing(RuntimeStore(tmp_path)) as store:
        clock = [100.0]
        monkeypatch.setattr('eimemory.storage.recall_deadline.monotonic', lambda: clock[0])
        with recall_read_scope(store, {'_recall_collection_deadline_monotonic': 101.0}):
            assert store.sqlite.conn.execute('PRAGMA busy_timeout').fetchone()[0] == 1000
            clock[0] = 100.8
            with closing(store.sqlite.conn.cursor()) as cursor:
                remaining_ms = cursor.execute('PRAGMA busy_timeout').fetchone()[0]
                assert 0 < remaining_ms <= 200
