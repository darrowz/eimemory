"""Offline cursor-only regressions; run directly, without project pytest/conftest.

Usage: python -I tests/test_store_access_cursor_isolated.py [source_file]
Only the _FetchedCursor class is AST-extracted and executed. No project imports,
SQL, database connections, or service calls occur.
"""
from __future__ import annotations

import ast
import itertools
from pathlib import Path
import sys
from typing import Any
import unittest


SOURCE = (Path(sys.argv.pop(1)) if __name__ == "__main__" and len(sys.argv) > 1 else
          Path(__file__).resolve().parents[1] / "eimemory/storage/store_access.py")
tree = ast.parse(SOURCE.read_text(encoding="utf-8"), filename=str(SOURCE))
classes = [node for node in tree.body
           if isinstance(node, ast.ClassDef) and node.name == "_FetchedCursor"]
if len(classes) != 1:
    raise RuntimeError("Expected exactly one _FetchedCursor class")
namespace = {"Any": Any}
exec(compile(ast.Module(body=classes, type_ignores=[]), str(SOURCE), "exec"), namespace)
FetchedCursor = namespace["_FetchedCursor"]


class CursorConsumptionTests(unittest.TestCase):
    def test_empty_and_repeated_exhaustion(self):
        cursor = FetchedCursor([])
        for _ in range(3):
            self.assertIsNone(cursor.fetchone())
            self.assertEqual(cursor.fetchall(), [])
            self.assertEqual(list(cursor), [])
            with self.assertRaises(StopIteration):
                next(iter(cursor))

    def test_single_row_consumption(self):
        cursor = FetchedCursor([("a",)])
        self.assertEqual(cursor.fetchone(), ("a",))
        self.assertIsNone(cursor.fetchone())
        self.assertEqual(cursor.fetchall(), [])

    def test_multiple_fetchone(self):
        cursor = FetchedCursor([1, 2, 3])
        self.assertEqual([cursor.fetchone() for _ in range(4)], [1, 2, 3, None])

    def test_fetchall_consumes_remaining(self):
        cursor = FetchedCursor([1, 2, 3])
        self.assertEqual(cursor.fetchone(), 1)
        self.assertEqual(cursor.fetchall(), [2, 3])
        self.assertEqual(cursor.fetchall(), [])
        self.assertIsNone(cursor.fetchone())

    def test_iterator_then_fetch_interfaces(self):
        cursor = FetchedCursor([1, 2, 3, 4])
        iterator = iter(cursor)
        self.assertEqual(next(iterator), 1)
        self.assertEqual(cursor.fetchone(), 2)
        self.assertEqual(cursor.fetchall(), [3, 4])
        with self.assertRaises(StopIteration):
            next(iterator)

    def test_fetchone_then_iteration(self):
        cursor = FetchedCursor([1, 2, 3])
        self.assertEqual(cursor.fetchone(), 1)
        self.assertEqual(list(cursor), [2, 3])
        self.assertIsNone(cursor.fetchone())
        self.assertEqual(list(cursor), [])

    def test_interleaved_iterator_references(self):
        cursor = FetchedCursor([1, 2, 3])
        first, second = iter(cursor), iter(cursor)
        self.assertEqual(next(first), 1)
        self.assertEqual(next(second), 2)
        self.assertEqual(next(first), 3)
        for iterator in (first, second):
            with self.assertRaises(StopIteration):
                next(iterator)

    def test_none_rows_are_not_iteration_sentinels(self):
        cursor = FetchedCursor([None, 1, None, 2])
        iterator = iter(cursor)
        self.assertIsNone(next(iterator))
        self.assertEqual(cursor.fetchone(), 1)
        self.assertIsNone(cursor.fetchone())
        self.assertEqual(list(iterator), [2])
        self.assertEqual(cursor.fetchall(), [])

    def test_none_only_row_advances(self):
        cursor = FetchedCursor([None])
        self.assertIsNone(cursor.fetchone())
        self.assertEqual(cursor.fetchall(), [])
        with self.assertRaises(StopIteration):
            next(iter(cursor))

    def test_fetchall_returns_independent_outer_list(self):
        original = [[1], [2]]
        cursor = FetchedCursor(original)
        rows = cursor.fetchall()
        self.assertIsNot(rows, original)
        self.assertIs(rows[0], original[0])
        rows.clear()
        self.assertEqual(original, [[1], [2]])
        self.assertEqual(cursor.fetchall(), [])

    def test_exhaustive_short_mixed_sequences(self):
        # Compare 5^4 operation sequences across five row shapes. Iterator
        # exhaustion is tagged separately from a legitimate None row.
        shapes = ([], [None], [1], [1, 2, 3], [None, 1, None])
        operations = ("one", "all", "next_a", "next_b", "iterate")
        for shape in shapes:
            for sequence in itertools.product(operations, repeat=4):
                with self.subTest(rows=shape, sequence=sequence):
                    cursor = FetchedCursor(list(shape))
                    a, b = iter(cursor), iter(cursor)
                    position = 0
                    for operation in sequence:
                        if operation == "one":
                            expected = shape[position] if position < len(shape) else None
                            self.assertEqual(cursor.fetchone(), expected)
                            position = min(position + 1, len(shape))
                        elif operation in ("all", "iterate"):
                            actual = cursor.fetchall() if operation == "all" else list(cursor)
                            self.assertEqual(actual, shape[position:])
                            position = len(shape)
                        else:
                            iterator = a if operation == "next_a" else b
                            if position == len(shape):
                                with self.assertRaises(StopIteration):
                                    next(iterator)
                            else:
                                self.assertEqual(next(iterator), shape[position])
                                position += 1


if __name__ == "__main__":
    unittest.main(verbosity=2)
