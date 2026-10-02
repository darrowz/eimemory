"""Isolated page-cursor probe with fake SQL; no target import or database/schema work."""
from __future__ import annotations

import argparse
import ast
import copy
from contextlib import contextmanager
import hashlib
from pathlib import Path
from types import SimpleNamespace
import unittest


BASELINE_SHA256 = "8bcb9fa74985db58f429e0cf60178a21d0091bcfb1db232ce79574b709a150e6"


def extract_shell(source_path, expected_sha256):
    data = source_path.read_bytes()
    actual = hashlib.sha256(data).hexdigest()
    if actual != expected_sha256:
        raise ValueError(f"Pinned source digest mismatch: {actual}")
    module = ast.parse(data, filename=str(source_path))
    owners = [node for node in module.body if isinstance(node, ast.ClassDef) and node.name == "SQLiteProjectionReader"]
    if len(owners) != 1:
        raise ValueError("Expected one SQLiteProjectionReader class")
    methods = [node for node in owners[0].body if isinstance(node, ast.FunctionDef) and node.name == "page"]
    if len(methods) != 1 or methods[0].decorator_list:
        raise ValueError("Expected one complete undecorated page method")
    method = methods[0]
    if actual == BASELINE_SHA256 and (method.lineno, method.end_lineno) != (94, 180):
        raise ValueError("Baseline page range mismatch")
    if any(isinstance(node, (ast.Import, ast.ImportFrom)) for node in ast.walk(method)):
        raise ValueError("Project imports are forbidden in the extracted method")
    isolated = ast.Module(
        body=[
            ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0),
            ast.ClassDef(name="Shell", bases=[], keywords=[], body=[copy.deepcopy(method)], decorator_list=[]),
        ],
        type_ignores=[],
    )
    ast.fix_missing_locations(isolated)
    namespace = {}
    exec(compile(isolated, str(source_path), "exec"), namespace)
    print(f"SOURCE_SHA256 {actual}")
    print(f"EXTRACTED page L{method.lineno}-{method.end_lineno}")
    return namespace["Shell"]


class FakeSqlite:
    def __init__(self, positions, trace):
        self.positions = sorted(positions)
        self.trace = trace
        self.calls = []
        self.selected = []

    def execute(self, sql, params):
        self.trace.append("select")
        self.calls.append((sql, tuple(params)))
        if "ORDER BY r.updated_at ASC, r.storage_key ASC" not in sql or "LIMIT ?" not in sql:
            raise AssertionError("Unexpected page SQL shape")
        # Only interpret tuple keyset presence and the existing final row bound.
        # The SQL string's projection/schema/alias subqueries are never executed.
        positions = list(self.positions)
        if "WHERE (r.updated_at, r.storage_key) > (?, ?)" in sql:
            if len(params) != 9:
                raise AssertionError("Unexpected keyset parameter shape")
            positions = [position for position in positions if position > tuple(params[6:8])]
        elif len(params) != 7:
            raise AssertionError("Unexpected initial-page parameter shape")
        self.selected = positions[:params[-1]]
        return self

    def fetchall(self):
        fields = (
            "record_id", "kind", "status", "tenant_id", "agent_id", "workspace_id", "user_id", "source_id",
            "title", "summary", "detail", "bounded_content_text", "alias_text",
        )
        return [
            {**dict.fromkeys(fields, ""), "updated_at": updated_at, "storage_key": storage_key}
            for updated_at, storage_key in self.selected
        ]


class FakeStore:
    def __init__(self, positions):
        self.trace = []
        self.sqlite = FakeSqlite(positions, self.trace)

    @contextmanager
    def locked(self):
        self.trace.append("lock")
        try:
            yield self.sqlite
        finally:
            self.trace.append("unlock")


def cursor(updated_at="", storage_key=""):
    return SimpleNamespace(updated_at=updated_at, storage_key=storage_key)


def cursor_tests(shell_type):
    class ProjectionCursorTests(unittest.TestCase):
        def setUp(self):
            self.positions = [("", "a"), ("", "b"), ("", "c"), ("2026-01-01", "d"), ("2026-01-01", "e"), ("2026-01-02", "f")]
            self.shell = shell_type()
            self.shell.store = FakeStore(self.positions)
            self.shell.max_text_chars = 64
            self.shell.projection_memory_only = False
            self.shell._ensure_contract_locked = lambda: self.shell.store.trace.append("fake-contract")

        def page_keys(self, position, *, limit=2):
            rows = self.shell.page(position, limit=limit)
            return [(row["updated_at"], row["storage_key"]) for row in rows]

        def test_initial_empty_cursor_starts_at_first_page(self):
            self.assertEqual(self.page_keys(cursor()), self.positions[:2])
            self.assertEqual(self.shell.store.trace, ["lock", "fake-contract", "select", "unlock"])
            self.assertEqual(len(self.shell.store.sqlite.calls[-1][1]), 7)

        def test_empty_timestamp_nonempty_key_advances(self):
            self.assertEqual(self.page_keys(cursor("", "b")), self.positions[2:4])
            sql, params = self.shell.store.sqlite.calls[-1]
            self.assertIn("WHERE (r.updated_at, r.storage_key) > (?, ?)", sql)
            self.assertEqual(params[6:8], ("", "b"))

        def test_nonempty_timestamp_cursor_keeps_existing_order(self):
            self.assertEqual(self.page_keys(cursor("2026-01-01", "d")), self.positions[4:])

        def test_final_timestamp_page_is_empty(self):
            self.assertEqual(self.page_keys(cursor(*self.positions[-1])), [])

        def test_empty_timestamp_tail_is_empty(self):
            self.shell.store.sqlite.positions = self.positions[:3]
            self.assertEqual(self.page_keys(cursor("", "c")), [])

        def test_bounded_walk_returns_every_row_once(self):
            position = cursor()
            collected = []
            for _ in range(len(self.positions) + 1):
                page = self.page_keys(position)
                if not page:
                    break
                self.assertFalse(set(page) & set(collected), "Cursor repeated an already-returned row")
                collected.extend(page)
                position = cursor(*page[-1])
            self.assertEqual(collected, self.positions)

        def test_existing_page_limit_clamps_are_preserved(self):
            for requested, expected in ((-5, 1), (0, 1), (1, 1), (5000, 1000)):
                with self.subTest(limit=requested):
                    self.page_keys(cursor(), limit=requested)
                    self.assertEqual(self.shell.store.sqlite.calls[-1][1][-1], expected)

    return ProjectionCursorTests


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--expected-sha256", required=True)
    args = parser.parse_args()
    shell_type = extract_shell(args.source, args.expected_sha256)
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(cursor_tests(shell_type))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
