"""Exact-reference SQL construction tests using fake projection rows only."""
from __future__ import annotations

from threading import RLock
import unittest

from eimemory.models.records import ScopeRef
from eimemory.storage.runtime_store import RuntimeStore
from eimemory.storage.sqlite_store import MAX_SQL_IN_PARAMS, SqliteRecordStore


class _FakeProjectionConnection:
    def __init__(self):
        self.calls = []
        self.rows = []

    def execute(self, sql, params):
        self.calls.append((sql, list(params)))
        # Model SQL OR row uniqueness within a query, without using SQLite or
        # calling any payload/archive hydration code.
        identities = dict.fromkeys(tuple(params[index:index + 6]) for index in range(0, len(params), 6))
        self.rows = [{"identity": identity} for identity in identities]
        return self

    def fetchall(self):
        return list(self.rows)


class _FakeProjectionStore(SqliteRecordStore):
    def __init__(self):
        self.conn = _FakeProjectionConnection()

    def assert_connection_lock_held(self):
        pass

    def _record_from_storage_row(self, row, *, hydrate):
        if hydrate is not True:
            raise AssertionError("Expected the existing hydration call contract")
        return row["identity"]

    def _record_matches_projection_row(self, record, row):
        return record == row["identity"]


def _refs(count):
    return [
        {"record_id": f"fake-{index}", "scope": ScopeRef(agent_id="fake-agent"), "source_id": "test"}
        for index in range(count)
    ]


class ExactReferenceChunkBoundTests(unittest.TestCase):
    def setUp(self):
        self.sqlite = _FakeProjectionStore()
        self.runtime = object.__new__(RuntimeStore)
        self.runtime.sqlite = self.sqlite
        self.runtime._lock = RLock()

    def test_oversized_public_chunk_is_bounded_and_returns_all_unique_refs(self):
        refs = _refs(6000)
        records = self.runtime.get_by_exact_refs(refs, chunk_size=6000)
        self.assertEqual(len(records), len(refs))
        self.assertEqual({record[0] for record in records}, {ref["record_id"] for ref in refs})
        self.assertTrue(self.sqlite.conn.calls)
        for sql, params in self.sqlite.conn.calls:
            self.assertLessEqual(len(params), MAX_SQL_IN_PARAMS)
            self.assertEqual(len(params) % 6, 0)
            self.assertLessEqual(sql.count(" OR ") + 1, MAX_SQL_IN_PARAMS // 6)
            self.assertEqual(sql.count("?"), len(params))

    def test_default_chunking_remains_one_hundred_refs(self):
        records = self.runtime.get_by_exact_refs(_refs(251))
        self.assertEqual(len(records), 251)
        self.assertEqual([len(params) // 6 for _, params in self.sqlite.conn.calls], [100, 100, 51])

    def test_small_explicit_chunk_is_preserved(self):
        records = self.runtime.get_by_exact_refs(_refs(5), chunk_size=2)
        self.assertEqual(len(records), 5)
        self.assertEqual([len(params) // 6 for _, params in self.sqlite.conn.calls], [2, 2, 1])

    def test_zero_and_none_keep_default_normalization(self):
        for value in (0, None):
            with self.subTest(chunk_size=value):
                self.sqlite.conn.calls.clear()
                records = self.runtime.get_by_exact_refs(_refs(101), chunk_size=value)
                self.assertEqual(len(records), 101)
                self.assertEqual([len(params) // 6 for _, params in self.sqlite.conn.calls], [100, 1])

    def test_negative_chunk_keeps_one_ref_normalization(self):
        records = self.runtime.get_by_exact_refs(_refs(3), chunk_size=-5)
        self.assertEqual(len(records), 3)
        self.assertEqual([len(params) // 6 for _, params in self.sqlite.conn.calls], [1, 1, 1])

    def test_existing_duplicate_behavior_is_not_globally_deduplicated(self):
        ref = _refs(1)[0]
        records = self.runtime.get_by_exact_refs([ref, ref], chunk_size=1)
        self.assertEqual(len(records), 2)
        self.assertEqual(records[0], records[1])
        self.sqlite.conn.calls.clear()
        records = self.runtime.get_by_exact_refs([ref, ref], chunk_size=2)
        self.assertEqual(len(records), 1)

    def test_missing_scopes_and_empty_refs_do_not_add_queries(self):
        self.assertEqual(self.runtime.get_by_exact_refs([]), [])
        self.assertEqual(self.runtime.get_by_exact_refs([{"record_id": "fake"}], chunk_size=1), [])
        self.assertEqual(self.sqlite.conn.calls, [])


if __name__ == "__main__":
    unittest.main()
