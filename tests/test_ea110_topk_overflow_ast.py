"""Isolated EA-110 regression; never imports or runs the evaluator module."""

import ast
from pathlib import Path
import unittest


SOURCE = Path(__file__).resolve().parents[1] / "eimemory/evaluation/production_recall.py"
DEFAULTS = (-7, 0, 5, 1000, 1001)
SUCCESSFUL_CONVERSIONS = (
    (0, 1), (-10, 1), (1, 1), (999, 999), (1000, 1000), (1001, 1000),
    (10**400, 1000), (-(10**400), 1),
    (4.9, 4), (-4.9, 1), (0.9, 1), (1e300, 1000), (-1e300, 1),
    ("42", 42), (" 42 ", 42), ("+12", 12), ("-12", 1),
    ("1_000", 1000), ("1001", 1000), (True, 1), (False, 1),
)
EXISTING_FALLBACKS = ("4.5", "bad", "", "inf", "-inf", "nan", float("nan"), 1 + 2j)


def load_preinspected_helper():
    """Compile only the exact reviewed helper, with restricted builtins."""
    tree = ast.parse(SOURCE.read_text(encoding="utf-8"), filename=str(SOURCE))
    helpers = [
        node for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "_positive_int"
    ]
    if len(helpers) != 1:
        raise AssertionError("Expected exactly one top-level _positive_int helper")
    helper = helpers[0]
    expected_template = (
        "def _positive_int(value: object, *, default: int) -> int:\n"
        "    try:\n"
        "        parsed = int(value)\n"
        "    except ({exceptions}):\n"
        "        return default\n"
        "    return max(1, min(1000, parsed))\n"
    )
    allowed_shapes = {
        ast.dump(ast.parse(expected_template.format(exceptions=exceptions)).body[0],
                 include_attributes=False)
        for exceptions in (
            "TypeError, ValueError",
            "TypeError, ValueError, OverflowError",
        )
    }
    if ast.dump(helper, include_attributes=False) not in allowed_shapes:
        raise AssertionError("Helper changed beyond the pre-inspected EA-110 shapes")
    namespace = {"__builtins__": {
        "object": object, "int": int, "max": max, "min": min,
        "TypeError": TypeError, "ValueError": ValueError, "OverflowError": OverflowError,
    }}
    module = ast.fix_missing_locations(ast.Module(body=[helper], type_ignores=[]))
    exec(compile(module, str(SOURCE) + ":selected-helper-only", "exec"), namespace)
    return namespace["_positive_int"]


class TestEA110TopKOverflowFallback(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.convert = staticmethod(load_preinspected_helper())

    def test_float_infinities_return_default_unchanged(self):
        for value in (float("inf"), float("-inf")):
            for default in DEFAULTS:
                with self.subTest(value=value, default=default):
                    self.assertEqual(self.convert(value, default=default), default)

    def test_successful_conversions_keep_existing_clamp(self):
        for value, expected in SUCCESSFUL_CONVERSIONS:
            for default in DEFAULTS:
                with self.subTest(value=value, default=default):
                    result = self.convert(value, default=default)
                    self.assertEqual(result, expected)
                    self.assertIs(type(result), int)

    def test_existing_conversion_failures_keep_default_unchanged(self):
        for value in EXISTING_FALLBACKS:
            for default in DEFAULTS:
                with self.subTest(value=value, default=default):
                    self.assertEqual(self.convert(value, default=default), default)


if __name__ == "__main__":
    unittest.main(verbosity=2)
