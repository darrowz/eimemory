"""Reader acquisition regressions with fake locks, clocks, and connections only."""
from __future__ import annotations

from pathlib import Path
import unittest
from unittest.mock import patch

from eimemory.storage.runtime_store import RuntimeStore, _ReadSlot


class _Clock:
    now = 1000.0

    def __call__(self):
        return self.now


class _FakeLock:
    def __init__(self, clock, *, blocked=False, owned=False):
        self.clock = clock
        self.blocked = blocked
        self.owned = owned
        self.acquisitions = []
        self.releases = 0

    def _is_owned(self):
        return self.owned

    def acquire(self, *, timeout=None):
        self.acquisitions.append(timeout)
        if self.blocked:
            if timeout is None:
                raise AssertionError("Unbounded acquisition attempted on blocked fake lock")
            if timeout < 0:
                raise AssertionError("Negative timeout attempted on blocked fake lock")
            self.clock.now += timeout
            return False
        return True

    def release(self):
        self.releases += 1

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, *_args):
        self.release()


class _FakeConnection:
    def __init__(self):
        self.busy_timeout = 30000
        self.progress_handler = None

    def execute(self, sql):
        if sql == "PRAGMA busy_timeout":
            return self
        if sql.startswith("PRAGMA busy_timeout = "):
            self.busy_timeout = int(sql.split("=")[1])
        elif sql != "PRAGMA query_only=ON":
            raise AssertionError(f"Unexpected SQL in fake: {sql}")
        return self

    def fetchone(self):
        return (self.busy_timeout,)

    def set_progress_handler(self, callback, _steps):
        self.progress_handler = callback


class _FakeSqliteStore:
    def __init__(self):
        self.conn = _FakeConnection()
        self.path = Path("unused-fake-store.sqlite")
        self.searches = []

    def bind_runtime_lock(self, lock):
        self.bound_lock = lock

    def search_with_diagnostics(self, **kwargs):
        self.searches.append(kwargs)
        return ["fake result"], {"candidate_count": 1}


class ReaderAcquisitionDeadlineTests(unittest.TestCase):
    def setUp(self):
        self.clock = _Clock()
        self.store = object.__new__(RuntimeStore)
        self.store._lock = _FakeLock(self.clock)
        self.store._reader_pool_lock = _FakeLock(self.clock)
        self.store._readers = []
        self.store.sqlite = _FakeSqliteStore()
        self.store.auxiliary_log_dir = Path("unused-fake-logs")
        self.store._reader_count = lambda: 2
        self.created_readers = []
        for target in (
            "eimemory.storage.runtime_store.perf_counter",
            "eimemory.storage.recall_deadline.monotonic",
        ):
            patcher = patch(target, self.clock)
            patcher.start()
            self.addCleanup(patcher.stop)
        factory = patch("eimemory.storage.runtime_store.SqliteRecordStore", self._make_reader)
        factory.start()
        self.addCleanup(factory.stop)

    def _make_reader(self, *_args, **_kwargs):
        reader = _FakeSqliteStore()
        self.created_readers.append(reader)
        return reader

    def _search(self, api):
        return getattr(self.store, api)(
            query="fake query",
            recall_filters={"_recall_collection_deadline_monotonic": self.clock.now + 0.25},
        )

    def _assert_exhausted(self, api):
        result = self._search(api)
        if api == "search":
            self.assertEqual(result, [])
            self.assertTrue(result.degraded)
            self.assertEqual(result.degraded_reason, "recall_budget_exhausted")
        else:
            records, report = result
            self.assertEqual(records, [])
            self.assertEqual(report["retrieval_mode"], "deadline_exhausted")
            self.assertEqual(report["blocked_counts"], {"recall_budget_exhausted": 1})
        self.assertEqual(self.store.sqlite.searches, [])

    def test_cold_pool_writer_wait_obeys_both_search_deadlines(self):
        self.store._lock.blocked = True
        for api in ("search", "search_with_diagnostics"):
            with self.subTest(api=api):
                self._assert_exhausted(api)
        self.assertEqual(self.store._lock.acquisitions, [0.25, 0.25])
        self.assertEqual(self.created_readers, [])

    def test_disabled_pool_writer_wait_obeys_both_search_deadlines(self):
        self.store._reader_count = lambda: 0
        self.store._lock.blocked = True
        for api in ("search", "search_with_diagnostics"):
            with self.subTest(api=api):
                self._assert_exhausted(api)
        self.assertEqual(self.store._lock.acquisitions, [0.25, 0.25])

    def test_exhausted_pool_writer_wait_obeys_both_search_deadlines(self):
        slot = _ReadSlot(_FakeSqliteStore(), _FakeLock(self.clock))
        slot.in_use = True
        self.store._readers = [slot]
        self.store._lock.blocked = True
        for api in ("search", "search_with_diagnostics"):
            with self.subTest(api=api):
                self._assert_exhausted(api)
        self.assertTrue(slot.in_use)
        self.assertEqual(self.store._lock.acquisitions, [0.25, 0.25])

    def test_patched_writer_wait_obeys_both_search_deadlines(self):
        self.store.sqlite.patched_method = lambda: None
        self.store._lock.blocked = True
        for api in ("search", "search_with_diagnostics"):
            with self.subTest(api=api):
                self._assert_exhausted(api)
        self.assertEqual(self.store._lock.acquisitions, [0.25, 0.25])
        self.assertEqual(self.created_readers, [])

    def test_pool_bookkeeping_wait_obeys_both_search_deadlines(self):
        slot = _ReadSlot(_FakeSqliteStore(), _FakeLock(self.clock))
        self.store._readers = [slot]
        self.store._reader_pool_lock.blocked = True
        for api in ("search", "search_with_diagnostics"):
            with self.subTest(api=api):
                self._assert_exhausted(api)
        self.assertFalse(slot.in_use)
        self.assertEqual(self.store._reader_pool_lock.acquisitions, [0.25, 0.25])

    def test_selected_reader_timeout_releases_reservation(self):
        slot = _ReadSlot(_FakeSqliteStore(), _FakeLock(self.clock, blocked=True))
        self.store._readers = [slot]
        for api in ("search", "search_with_diagnostics"):
            with self.subTest(api=api):
                self._assert_exhausted(api)
                self.assertFalse(slot.in_use)
        self.assertEqual(slot.lock.acquisitions, [0.25, 0.25])
        self.assertEqual(slot.lock.releases, 0)

    def test_warm_free_reader_does_not_wait_for_writer(self):
        slot = _ReadSlot(_FakeSqliteStore(), _FakeLock(self.clock))
        self.store._readers = [slot]
        self.store._lock.blocked = True
        for api in ("search", "search_with_diagnostics"):
            with self.subTest(api=api):
                result = self._search(api)
                self.assertEqual(result if api == "search" else result[0], ["fake result"])
                self.assertFalse(slot.in_use)
        self.assertEqual(self.store._lock.acquisitions, [])
        self.assertEqual(len(slot.store.searches), 2)
        self.assertEqual(slot.lock.releases, 2)

    def test_caller_owned_writer_is_reused(self):
        self.store._lock.owned = True
        for api in ("search", "search_with_diagnostics"):
            with self.subTest(api=api):
                result = self._search(api)
                self.assertEqual(result if api == "search" else result[0], ["fake result"])
        self.assertEqual(self.store._lock.acquisitions, [])
        self.assertEqual(len(self.store.sqlite.searches), 2)
        self.assertEqual(self.created_readers, [])

    def test_expired_deadline_does_not_search_reentrant_writer(self):
        self.store._lock.owned = True
        for api in ("search", "search_with_diagnostics"):
            with self.subTest(api=api):
                result = getattr(self.store, api)(
                    query="fake query",
                    recall_filters={"_recall_collection_deadline_monotonic": self.clock.now - 1},
                )
                if api == "search":
                    self.assertEqual(result, [])
                    self.assertTrue(result.degraded)
                else:
                    self.assertEqual(result[0], [])
                    self.assertEqual(result[1]["retrieval_mode"], "deadline_exhausted")
        self.assertEqual(self.store.sqlite.searches, [])
        self.assertEqual(self.store._lock.acquisitions, [])

    def test_nested_recall_retains_earlier_connection_deadline(self):
        from eimemory.storage.recall_deadline import _ReadConnection

        self.store._lock.owned = True
        raw = self.store.sqlite.conn
        earlier = self.clock.now + 0.125
        outer = _ReadConnection(raw, earlier)
        self.store.sqlite.conn = outer
        observed = []

        def search_with_diagnostics(**_kwargs):
            observed.append(self.store.sqlite.conn.deadline)
            return ["fake result"], {"candidate_count": 1}

        self.store.sqlite.search_with_diagnostics = search_with_diagnostics
        for api in ("search", "search_with_diagnostics"):
            with self.subTest(api=api):
                self._search(api)
                self.assertIs(self.store.sqlite.conn, outer)
        self.assertEqual(observed, [earlier, earlier])

    def test_cold_pool_stops_between_constructions_after_expiry(self):
        self.store._reader_count = lambda: 3
        make_reader = self._make_reader

        def slow_fake_constructor(*args, **kwargs):
            reader = make_reader(*args, **kwargs)
            self.clock.now += 0.5
            return reader

        with patch("eimemory.storage.runtime_store.SqliteRecordStore", slow_fake_constructor):
            self._assert_exhausted("search")
        self.assertEqual(len(self.created_readers), 1)
        self.assertEqual(len(self.store._readers), 1)
        self.assertFalse(self.store._readers[0].in_use)

    def test_deadline_free_borrow_accepts_original_no_argument_lock(self):
        calls = []

        class OriginalLock:
            def _is_owned(self):
                return False

            def acquire(self):
                calls.append("acquire")
                return True

            def release(self):
                calls.append("release")

            def __enter__(self):
                self.acquire()

            def __exit__(self, *_args):
                self.release()

        self.store._lock = OriginalLock()
        self.store._reader_count = lambda: 0
        with self.store.borrow_reader() as slot:
            self.assertIs(slot.store, self.store.sqlite)
        self.assertEqual(calls, ["acquire", "release"])

    def test_non_search_borrow_retains_no_deadline_behavior(self):
        self.store._reader_count = lambda: 0
        with self.store.borrow_reader() as slot:
            self.assertIs(slot.store, self.store.sqlite)
        self.assertEqual(self.store._lock.acquisitions, [None])
        self.assertEqual(self.store._lock.releases, 1)


if __name__ == "__main__":
    unittest.main()
