"""Readiness borrows returning WAL readers without extending its deadline."""
from contextlib import contextmanager
from queue import Queue
import threading
import time

import pytest

from eimemory.models.records import RecordEnvelope, ScopeRef
from eimemory.retrieval.sqlite_source import SQLiteCandidateSource
from eimemory.storage.recall_deadline import RecallReadDeadlineExceeded
from eimemory.storage.runtime_store import RuntimeStore


@pytest.fixture
def native_store(tmp_path, monkeypatch):
    monkeypatch.setenv("EIMEMORY_SQLITE_READERS", "2")
    store = RuntimeStore(tmp_path)
    source = SQLiteCandidateSource(store)
    store.append(RecordEnvelope.create(kind="memory", title="Original native source", scope=ScopeRef()))
    expected = source.authority_snapshot()
    try:
        yield store, source, expected
    finally:
        store.close()


@contextmanager
def held_reader(store):
    acquired, release, finished = threading.Event(), threading.Event(), threading.Event()
    errors = []
    def hold():
        try:
            with store.borrow_reader():
                acquired.set()
                if not release.wait(3):
                    raise TimeoutError("reader test cleanup")
        except BaseException as exc:
            errors.append(exc)
        finally:
            finished.set()
    worker = threading.Thread(target=hold)
    worker.start()
    try:
        assert acquired.wait(2), errors
        yield release, finished
    finally:
        release.set()
        worker.join(3)
        assert not worker.is_alive() and not errors


@contextmanager
def held_writer(store):
    acquired, release = threading.Event(), threading.Event()
    def hold():
        with store._lock:
            acquired.set()
            release.wait(3)
    worker = threading.Thread(target=hold)
    worker.start()
    try:
        assert acquired.wait(2)
        yield
    finally:
        release.set()
        worker.join(3)
        assert not worker.is_alive()


class ObservedNativeLock:
    """Keep a real lock; observe when the probe releases pool bookkeeping."""
    def __init__(self, lock):
        self.lock = lock
        self.probe_ident = None
        self.probe_released = threading.Event()

    def acquire(self, *args, **kwargs):
        return self.lock.acquire(*args, **kwargs)

    def release(self):
        self.lock.release()
        if threading.get_ident() == self.probe_ident:
            self.probe_released.set()

    def locked(self):
        return self.lock.locked()

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, *_args):
        self.release()


def test_returning_reader_beats_a_busy_writer_with_the_original_deadline(native_store):
    store, source, expected = native_store
    observed = ObservedNativeLock(store._reader_pool_lock)
    store._reader_pool_lock = observed
    store._reader_pool_condition = threading.Condition(observed)
    outcome = Queue()
    with held_reader(store) as (release_first, first_finished), held_reader(store), held_writer(store):
        def probe():
            observed.probe_ident = threading.get_ident()
            started = time.monotonic()
            deadline = started + .20
            try:
                outcome.put((source.authority_snapshot(deadline_at=deadline), time.monotonic(), deadline))
            except BaseException as exc:
                outcome.put((exc, time.monotonic(), deadline))
        worker = threading.Thread(target=probe)
        worker.start()
        try:
            # Old code releases this lock then waits only on the busy writer.
            # New code releases it while waiting for a reader-slot notification.
            assert observed.probe_released.wait(1)
            timer = threading.Timer(.03, release_first.set)
            timer.start()
            value, completed, original_deadline = outcome.get(timeout=2)
            timer.join(1)
            assert first_finished.wait(1)
            assert value == expected
            assert completed < original_deadline
        finally:
            worker.join(2)
            assert not worker.is_alive()


def test_full_reader_pool_expires_instead_of_borrowing_an_available_writer(native_store):
    store, source, _ = native_store
    with held_reader(store), held_reader(store):
        started = time.monotonic()
        with pytest.raises(RecallReadDeadlineExceeded, match="recall_budget_exhausted"):
            source.authority_snapshot(deadline_at=started + .08)
        assert .05 <= time.monotonic() - started < .35
    assert all(not slot.in_use for slot in store._readers)


def test_default_borrow_preserves_writer_fallback_when_the_pool_is_full(native_store):
    store, _, _ = native_store
    with held_reader(store), held_reader(store):
        with store.borrow_reader(deadline=time.monotonic() + .20) as slot:
            assert slot.store is store.sqlite


def test_spurious_pool_notification_does_not_extend_the_original_deadline(native_store, monkeypatch):
    store, source, _ = native_store
    condition = store._reader_pool_condition
    original = condition.wait
    entered = threading.Event()
    waits = []
    def observed_wait(timeout=None):
        waits.append(timeout)
        entered.set()
        return original(timeout)
    monkeypatch.setattr(condition, "wait", observed_wait)
    def notify_without_free_reader():
        assert entered.wait(1)
        with condition:
            condition.notify_all()
    with held_reader(store), held_reader(store):
        notifier = threading.Thread(target=notify_without_free_reader)
        notifier.start()
        started = time.monotonic()
        try:
            with pytest.raises(RecallReadDeadlineExceeded):
                source.authority_snapshot(deadline_at=started + .08)
            assert time.monotonic() - started < .35
            assert len(waits) >= 2 and waits[-1] < waits[0]
        finally:
            notifier.join(2)
            assert not notifier.is_alive()


@pytest.mark.parametrize("writer_busy", [False, True])
def test_zero_reader_pool_retains_deadline_bounded_writer_semantics(tmp_path, monkeypatch, writer_busy):
    monkeypatch.setenv("EIMEMORY_SQLITE_READERS", "0")
    store = RuntimeStore(tmp_path)
    source = SQLiteCandidateSource(store)
    try:
        expected = source.authority_snapshot()
        if writer_busy:
            with held_writer(store), pytest.raises(RecallReadDeadlineExceeded):
                source.authority_snapshot(deadline_at=time.monotonic() + .08)
        else:
            assert source.authority_snapshot(deadline_at=time.monotonic() + .20) == expected
        assert store._readers == []
    finally:
        store.close()


def test_callers_write_transaction_stays_visible_and_owned(native_store):
    store, source, previous = native_store
    later = RecordEnvelope.create(kind="memory", title="Uncommitted native source", scope=ScopeRef())
    later.time.updated_at = "2099-01-01T00:00:00Z"
    with held_reader(store), held_reader(store), store._lock:
        store.sqlite.execute("BEGIN IMMEDIATE")
        try:
            store.sqlite.upsert(later, commit=False)
            expected_head = store.sqlite.execute(
                "SELECT updated_at,storage_key FROM records ORDER BY updated_at DESC,storage_key DESC LIMIT 1").fetchone()
            expected_revision = store.sqlite.execute("SELECT revision FROM vector_sync_revision WHERE singleton=1").fetchone()[0]
            actual = source.authority_snapshot(deadline_at=time.monotonic() + .20)
            assert actual == ((expected_head[0], expected_head[1]), str(expected_revision))
            assert actual != previous and store.sqlite.in_transaction
        finally:
            store.sqlite.rollback()
    assert source.authority_snapshot() == previous


def test_explicit_patched_writer_remains_the_observed_connection(native_store, monkeypatch):
    store, source, expected = native_store
    execute = store.sqlite.execute
    observed = []
    def tracked(sql, parameters=()):
        if sql.startswith("SELECT updated_at,storage_key"):
            observed.append("head")
        return execute(sql, parameters)
    monkeypatch.setattr(store.sqlite, "execute", tracked)
    assert source.authority_snapshot(deadline_at=time.monotonic() + .20) == expected
    assert observed == ["head"]
