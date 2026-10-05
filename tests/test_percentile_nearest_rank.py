"""Offline percentile regressions; run directly with the stdlib unittest runner.

Only the two relevant function definitions are loaded, avoiding package startup
and pytest conftest imports. No third-party dependencies are required.
"""

from __future__ import annotations

import ast
from fractions import Fraction
import math
from pathlib import Path
import unittest


SOURCE = Path(__file__).resolve().parents[1] / "eimemory/evaluation/metrics.py"


def load_percentile(source: str):
    tree = ast.parse(source, filename=str(SOURCE))
    nodes = [
        node for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name in {"_round", "percentile"}
    ]
    assert {node.name for node in nodes} == {"_round", "percentile"}
    namespace = {"math": math}
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(SOURCE), "exec"), namespace)
    return namespace["percentile"]


percentile = load_percentile(SOURCE.read_text(encoding="utf-8"))


class PercentileNearestRankTests(unittest.TestCase):
    def test_reported_integer_boundary(self):
        self.assertEqual(percentile(list(range(1, 101)), 7), 7.0)

    def test_exhaustive_integer_reference(self):
        # Fraction supplies an independent exact-rational nearest-rank oracle.
        for size in range(1, 1001):
            values = list(range(1, size + 1))
            for pct in range(101):
                expected = float(max(1, math.ceil(Fraction(pct * size, 100))))
                actual = percentile(values, pct)
                if actual != expected:
                    self.fail(f"size={size}, pct={pct}: {actual} != {expected}")

    def test_empty_returns_float_zero_before_converting_percent(self):
        result = percentile([], object())
        self.assertEqual(result, 0.0)
        self.assertIsInstance(result, float)

    def test_clamps_percent_and_preserves_endpoints(self):
        for pct, expected in [(-1000, 1.0), (0, 1.0), (100, 100.0), (1000, 100.0)]:
            with self.subTest(pct=pct):
                self.assertEqual(percentile(list(range(1, 101)), pct), expected)

    def test_percent_is_truncated_using_int(self):
        for pct, expected in [(7.9, 7.0), ("7", 7.0), (-0.9, 1.0), (True, 1.0)]:
            with self.subTest(pct=pct):
                self.assertEqual(percentile(list(range(1, 101)), pct), expected)

    def test_singleton(self):
        for pct in range(-1, 102):
            self.assertEqual(percentile([2.12349], pct), 2.123)

    def test_sort_float_conversion_duplicates_and_no_mutation(self):
        values = ["3.33339", -2, "1.23456", "1.23456"]
        original = values.copy()
        self.assertEqual(percentile(values, 0), -2.0)
        self.assertEqual(percentile(values, 50), 1.235)
        self.assertEqual(percentile(values, 100), 3.333)
        self.assertEqual(values, original)

    def test_invalid_percent_conversion_is_unchanged(self):
        with self.assertRaises(ValueError):
            percentile([1], "invalid")
        with self.assertRaises(TypeError):
            percentile([1], None)


if __name__ == "__main__":
    unittest.main()
