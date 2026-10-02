"""Empty candidate timeout tests with fake SQL and embedding, never a database."""
from __future__ import annotations

from threading import Lock, RLock
import unittest
from unittest.mock import patch

from eimemory.models.records import ScopeRef
from eimemory.storage.recall_deadline import RecallReadDeadlineExceeded
from eimemory.storage.runtime_store import RuntimeStore, _ReadSlot
from eimemory.storage.sqlite_store import SqliteRecordStore


class _Clock:
    now = 1000.0

    def __call__(self):
        return self.now


class _FakeConnection:
    def __init__(self, clock):
        self.clock = clock
        self.has_index = True
        self.expire_after_index_probe = False
        self.calls = []
        self.busy_timeout = 30000
        self.rows = []

    def execute(self, sql, _parameters=()):
        self.calls.append(sql)
        if sql == "PRAGMA busy_timeout":
            self.rows = [(self.busy_timeout,)]
        elif sql.startswith("PRAGMA busy_timeout = "):
            self.busy_timeout = int(sql.split("=")[1])
            self.rows = []
        elif sql == "SELECT 1 FROM recall_index LIMIT 1":
            self.rows = [(1,)] if self.has_index else []
            if self.expire_after_index_probe:
                self.clock.now += 0.5
        elif sql.startswith("SELECT r.storage_key,"):
            self.rows = [{"storage_key": "fake-key"}]
        else:
            raise AssertionError(f"Unexpected SQL in fake: {sql}")
        return self

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def fetchall(self):
        return list(self.rows)

    def set_progress_handler(self, _callback, _steps):
        pass


class _FakeCandidateStore(SqliteRecordStore):
    def __init__(self, clock):
        self.clock = clock
        self.conn = _FakeConnection(clock)
        self.collectors = []
        self.seed_partial = False

    def assert_connection_lock_held(self):
        pass

    def _fts_query(self, _query):
        return ""

    def _exact_scope_recall_projection_incomplete(self, **_kwargs):
        return False

    def _exact_scope_has_recall_candidates(self, **_kwargs):
        return False

    def _collect_anchor_candidates(self, candidates, **_kwargs):
        self.collectors.append("anchor")
        if self.seed_partial:
            self._add_candidate(
                candidates, storage_key="fake-key", source="anchor", rank=30.0,
                quality_score=0.5, updated_at="2026-01-01",
            )
            self.clock.now += 0.5

    def _collect_lane_seed_candidates(self, _candidates, **_kwargs):
        self.collectors.append("lane")

    def _collect_recent_candidates(self, _candidates, **_kwargs):
        self.collectors.append("recent")


class CandidateCollectionDeadlineTests(unittest.TestCase):
    def setUp(self):
        self.clock = _Clock()
        self.sqlite = _FakeCandidateStore(self.clock)
        self.runtime = object.__new__(RuntimeStore)
        self.runtime.sqlite = self.sqlite
        self.runtime._lock = RLock()
        self.runtime._reader_pool_lock = Lock()
        self.runtime._readers = [_ReadSlot(self.sqlite, RLock())]
        for target in (
            "eimemory.storage.runtime_store.perf_counter",
            "eimemory.storage.sqlite_store.perf_counter",
            "eimemory.storage.recall_deadline.monotonic",
        ):
            patcher = patch(target, self.clock)
            patcher.start()
            self.addCleanup(patcher.stop)
        embedding = patch("eimemory.storage.sqlite_store._embed_text", return_value=[])
        self.embedding = embedding.start()
        self.addCleanup(embedding.stop)

    def _filters(self):
        return {"_recall_collection_deadline_monotonic": self.clock.now + 0.25}

    def _runtime_search(self, api, **extra_filters):
        return getattr(self.runtime, api)(
            query="fake query", recall_filters={**self._filters(), **extra_filters},
        )

    def test_search_marks_empty_collection_timeout_incomplete(self):
        self.sqlite.conn.expire_after_index_probe = True
        for has_index in (True, False):
            with self.subTest(has_index=has_index):
                self.sqlite.conn.has_index = has_index
                result = self._runtime_search("search")
                self.assertEqual(result, [])
                self.assertTrue(result.degraded)
                self.assertEqual(result.degraded_reason, "recall_budget_exhausted")
        self.assertEqual(self.sqlite.collectors, [])
        self.embedding.assert_not_called()

    def test_diagnostics_marks_empty_collection_timeout_incomplete(self):
        self.sqlite.conn.expire_after_index_probe = True
        for has_index in (True, False):
            with self.subTest(has_index=has_index):
                self.sqlite.conn.has_index = has_index
                records, report = self._runtime_search("search_with_diagnostics")
                self.assertEqual(records, [])
                self.assertEqual(report["retrieval_mode"], "deadline_exhausted")
                self.assertEqual(report["blocked_counts"], {"recall_budget_exhausted": 1})
        self.assertEqual(self.sqlite.collectors, [])
        self.embedding.assert_not_called()

    def test_empty_candidate_collection_raises_existing_deadline_signal(self):
        self.sqlite.conn.expire_after_index_probe = True
        with self.assertRaisesRegex(RecallReadDeadlineExceeded, "recall_budget_exhausted"):
            self.sqlite._candidate_rows(
                query="fake query", kinds=None, scope=ScopeRef(), limit=10,
                recall_filters=self._filters(),
            )
        self.assertEqual(self.sqlite.collectors, [])

    def test_genuine_empty_collection_stays_healthy(self):
        result = self._runtime_search("search")
        self.assertEqual(result, [])
        self.assertFalse(result.degraded)
        records, report = self._runtime_search("search_with_diagnostics")
        self.assertEqual(records, [])
        self.assertEqual(report["retrieval_mode"], "recall_index_hybrid")
        self.assertEqual(report["blocked_counts"], {})
        self.assertEqual(self.sqlite.collectors, ["anchor", "lane", "recent"] * 2)

    def test_exact_scope_empty_short_circuit_stays_healthy(self):
        result = self._runtime_search("search", _exact_scope=True)
        self.assertEqual(result, [])
        self.assertFalse(result.degraded)
        records, report = self._runtime_search("search_with_diagnostics", _exact_scope=True)
        self.assertEqual(records, [])
        self.assertEqual(report["retrieval_mode"], "empty_exact_scope")
        self.assertEqual(report["candidate_short_circuit"], "empty_exact_scope")
        self.assertEqual(report["blocked_counts"], {})
        self.assertEqual(self.sqlite.collectors, [])

    def test_partial_candidates_are_preserved_at_collection_deadline(self):
        self.sqlite.seed_partial = True
        rows, report = self.sqlite._candidate_rows(
            query="fake query", kinds=None, scope=ScopeRef(), limit=10,
            recall_filters=self._filters(),
        )
        self.assertEqual(rows, [{"storage_key": "fake-key"}])
        self.assertEqual(report["candidate_count"], 1)
        self.assertEqual(report["candidate_sources"], {"anchor": 1})
        self.assertEqual(self.sqlite.collectors, ["anchor"])

    def test_partial_candidate_scoring_keeps_existing_timeout_report(self):
        self.sqlite.seed_partial = True
        records, report = self.sqlite.search_with_diagnostics(
            query="fake query", kinds=None, scope=ScopeRef(), limit=10,
            recall_filters=self._filters(),
        )
        self.assertEqual(records, [])
        self.assertEqual(report["candidate_count"], 1)
        self.assertEqual(report["candidate_sources"], {"anchor": 1})
        self.assertEqual(report["blocked_counts"], {"candidate_scoring_timeout": 1})


if __name__ == "__main__":
    unittest.main()
