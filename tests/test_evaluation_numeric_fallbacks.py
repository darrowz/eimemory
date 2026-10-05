"""Stdlib-only regressions for isolated evaluation numeric helpers."""

import ast
from pathlib import Path
import unittest


def _load_helpers():
    source_path = Path(__file__).resolve().parents[1] / "eimemory/evaluation/contracts.py"
    tree = ast.parse(source_path.read_text(encoding="utf-8"), filename=str(source_path))
    names = {"_int_value", "_clamp_float"}
    definitions = [
        node for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name in names
    ]
    if {node.name for node in definitions} != names:
        raise AssertionError("Numeric helper definitions not found")
    isolated = ast.Module(
        body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0), *definitions],
        type_ignores=[],
    )
    namespace = {}
    exec(compile(ast.fix_missing_locations(isolated), str(source_path), "exec"), namespace)
    return namespace["_int_value"], namespace["_clamp_float"]


class NumericFallbackTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        int_value, clamp_float = _load_helpers()
        cls.int_value = staticmethod(int_value)
        cls.clamp_float = staticmethod(clamp_float)

    def test_integer_conversion_errors_use_supplied_default(self):
        for value in (float("inf"), float("-inf"), float("nan"), None, "bad"):
            with self.subTest(value=value):
                self.assertEqual(self.int_value(value, default=7), 7)

    def test_float_overflow_and_invalid_values_use_supplied_default(self):
        for value in (10**400, -(10**400), None, "bad"):
            with self.subTest(value=value):
                self.assertEqual(self.clamp_float(value, default=0.8), 0.8)

    def test_nan_uses_supplied_default(self):
        for value in (float("nan"), "NaN", "-nan"):
            for default in (0.0, 0.2, 0.8, 1.0):
                with self.subTest(value=value, default=default):
                    self.assertEqual(self.clamp_float(value, default=default), default)

    def test_float_clamp_and_infinity_behavior_preserved(self):
        for value, expected in ((-0.5, 0.0), (0.0, 0.0), (0.3, 0.3), (1.0, 1.0),
                                (1.5, 1.0), ("0.4", 0.4), (float("inf"), 1.0),
                                (float("-inf"), 0.0), ("inf", 1.0), ("-inf", 0.0)):
            with self.subTest(value=value):
                self.assertEqual(self.clamp_float(value, default=0.8), expected)

    def test_fallback_still_uses_existing_clamp(self):
        for value in (None, "NaN", 10**400):
            with self.subTest(value=value):
                self.assertEqual(self.clamp_float(value, default=-0.5), 0.0)
                self.assertEqual(self.clamp_float(value, default=1.5), 1.0)

    def test_valid_integer_conversion_preserved(self):
        for value, expected in (("12", 12), (3.9, 3), (-3.9, -3), (0, 0), (10**400, 10**400)):
            with self.subTest(value=value):
                self.assertEqual(self.int_value(value, default=7), expected)


if __name__ == "__main__":
    unittest.main()
