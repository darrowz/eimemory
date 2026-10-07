"""Isolated reporting-function regressions; never import the project or run migrations."""
from __future__ import annotations

import ast
from pathlib import Path
import sqlite3
from typing import Any
import unittest


SOURCE = Path(__file__).resolve().parents[1] / "eimemory/storage/migrations/capability_v3.py"
DEFAULT_MIGRATION_ID = "capability.v3.backfill.v1"
QUERY = "SELECT * FROM capability_v3_migration_state WHERE migration_id = ?"


def load_report_function(normalizer):
    """Compile only the reporting function, with an explicit fixture normalizer."""
    module = ast.parse(SOURCE.read_text(encoding="utf-8"), filename=str(SOURCE))
    matches = [node for node in module.body
               if isinstance(node, ast.FunctionDef)
               and node.name == "capability_v3_backfill_state"]
    if len(matches) != 1:
        raise AssertionError("exactly one reporting function required")
    isolated = ast.Module(body=matches, type_ignores=[])
    namespace = {"sqlite3": sqlite3, "Any": Any,
                 "CAPABILITY_V3_BACKFILL_MIGRATION": DEFAULT_MIGRATION_ID,
                 "_sqlite_connection": normalizer}
    exec(compile(isolated, str(SOURCE), "exec"), namespace)
    return namespace["capability_v3_backfill_state"]


class RecordingConnection(sqlite3.Connection):
    def execute(self, sql, parameters=()):
        self.last_sql = sql
        self.last_parameters = parameters
        return super().execute(sql, parameters)


class NamedTupleRow(tuple):
    """A named tuple-like row whose key order is deliberately non-positional."""
    def keys(self):
        return ["phase", "migration_id", "status"]

    def __getitem__(self, key):
        if isinstance(key, str):
            key = {"migration_id": 0, "status": 1, "phase": 2}[key]
        return super().__getitem__(key)


class BackfillStateRowsTest(unittest.TestCase):
    def connection(self, factory=None):
        conn = sqlite3.connect(":memory:", factory=RecordingConnection)
        self.addCleanup(conn.close)
        conn.row_factory = factory
        return conn

    def report(self, conn, **kwargs):
        def normalizer(value):
            self.assertIs(value, conn)
            return conn
        return load_report_function(normalizer)(conn, **kwargs)

    def seed(self, conn):
        conn.execute("CREATE TABLE capability_v3_migration_state "
                     "(migration_id TEXT, status TEXT, phase TEXT)")
        conn.execute("INSERT INTO capability_v3_migration_state VALUES (?, ?, ?)",
                     (DEFAULT_MIGRATION_ID, "running", "fixture_phase"))

    def assert_existing(self, factory):
        conn = self.connection(factory)
        self.seed(conn)
        actual = self.report(conn)
        self.assertEqual(actual, {"migration_id": DEFAULT_MIGRATION_ID,
                                  "status": "running", "phase": "fixture_phase"})
        self.assertIs(conn.row_factory, factory)
        self.assertEqual(conn.last_sql, QUERY)
        self.assertEqual(conn.last_parameters, (DEFAULT_MIGRATION_ID,))

    def test_default_tuple_existing_row(self):
        self.assert_existing(None)

    def test_sqlite_row_existing_row(self):
        self.assert_existing(sqlite3.Row)

    def test_named_tuple_row_keeps_named_key_path(self):
        def factory(cursor, values):
            return NamedTupleRow(values)
        conn = self.connection(factory)
        self.seed(conn)
        actual = self.report(conn)
        self.assertEqual(list(actual), ["phase", "migration_id", "status"])
        self.assertEqual(actual["phase"], "fixture_phase")
        self.assertEqual(actual["migration_id"], DEFAULT_MIGRATION_ID)
        self.assertIs(conn.row_factory, factory)

    def assert_missing(self, factory):
        conn = self.connection(factory)
        self.seed(conn)
        self.assertEqual(self.report(conn, migration_id="absent"),
                         {"migration_id": "absent", "status": "not_installed",
                          "phase": "not_installed"})
        self.assertIs(conn.row_factory, factory)
        self.assertEqual(conn.last_parameters, ("absent",))

    def test_default_tuple_missing_row(self):
        self.assert_missing(None)

    def test_sqlite_row_missing_row(self):
        self.assert_missing(sqlite3.Row)

    def assert_dynamic(self, factory):
        conn = self.connection(factory)
        conn.execute('CREATE TABLE capability_v3_migration_state '
                     '("odd name" BLOB, counter INTEGER, migration_id TEXT, '
                     'nullable TEXT, ratio REAL, status TEXT)')
        conn.execute("INSERT INTO capability_v3_migration_state VALUES (?, ?, ?, ?, ?, ?)",
                     (b"fixture\x00bytes", 7, "321", None, 1.25, "paused"))
        actual = self.report(conn, migration_id=321)
        self.assertEqual(actual, {"odd name": b"fixture\x00bytes", "counter": 7,
                                  "migration_id": "321", "nullable": None,
                                  "ratio": 1.25, "status": "paused"})
        self.assertEqual(list(actual), ["odd name", "counter", "migration_id",
                                       "nullable", "ratio", "status"])
        self.assertEqual(conn.last_sql, QUERY)
        self.assertEqual(conn.last_parameters, ("321",))
        self.assertIs(conn.row_factory, factory)

    def test_tuple_dynamic_columns_and_values(self):
        self.assert_dynamic(None)

    def test_sqlite_row_dynamic_columns_and_values(self):
        self.assert_dynamic(sqlite3.Row)

    def test_missing_table_error_still_propagates(self):
        conn = self.connection()
        with self.assertRaises(sqlite3.OperationalError) as caught:
            self.report(conn)
        self.assertIn("no such table", str(caught.exception))
        self.assertIsNone(conn.row_factory)

    def test_normalizer_exception_identity_is_preserved(self):
        failure = TypeError("fixture_normalizer_failure")
        def normalizer(value):
            raise failure
        with self.assertRaises(TypeError) as caught:
            load_report_function(normalizer)(object())
        self.assertIs(caught.exception, failure)

    def test_migration_id_conversion_error_still_propagates(self):
        failure = ValueError("fixture_string_conversion_failure")
        class Unstringable:
            def __str__(self):
                raise failure
        conn = self.connection()
        self.seed(conn)
        with self.assertRaises(ValueError) as caught:
            self.report(conn, migration_id=Unstringable())
        self.assertIs(caught.exception, failure)
        self.assertIsNone(conn.row_factory)

    def test_non_tuple_unsupported_row_error_still_propagates(self):
        def factory(cursor, values):
            return 42
        conn = self.connection(factory)
        self.seed(conn)
        with self.assertRaises(AttributeError):
            self.report(conn)
        self.assertIs(conn.row_factory, factory)


if __name__ == "__main__":
    unittest.main()
