"""Offline numeric-helper regressions; no project imports or store execution."""

import ast
import math
from pathlib import Path
import unittest


def _load_reward_float():
    path = Path(__file__).resolve().parents[1] / "eimemory/storage/replay_buffer.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    nodes = [
        node for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "_float"
    ]
    if len(nodes) != 1:
        raise AssertionError("Expected exactly one reward conversion helper")
    node = nodes[0]
    if node.decorator_list or node.args.defaults or node.args.kw_defaults:
        raise AssertionError("Helper must not execute decorators or defaults")
    if any(isinstance(child, (ast.Import, ast.ImportFrom)) for child in ast.walk(node)):
        raise AssertionError("Helper must not import modules")
    namespace = {
        "__builtins__": {
            "float": float,
            "round": round,
            "TypeError": TypeError,
            "ValueError": ValueError,
            "OverflowError": OverflowError,
        },
        "Any": object,
        "float": float,
    }
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(path), "exec"), namespace)
    return namespace["_float"]


class ReplayRewardOverflowTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.reward_float = staticmethod(_load_reward_float())

    def test_unrepresentable_integer_rewards_use_zero_fallback(self):
        for value in (10**400, -(10**400), 10**1000, -(10**1000)):
            with self.subTest(positive=value > 0, digits=len(str(abs(value)))):
                result = self.reward_float(value)
                self.assertIs(type(result), float)
                self.assertEqual(result, 0.0)

    def test_existing_invalid_inputs_keep_zero_fallback(self):
        for value in (None, "", "not-a-number", {}, [], complex(1, 2)):
            with self.subTest(value=repr(value)):
                self.assertEqual(self.reward_float(value), 0.0)

    def test_normal_rewards_keep_three_decimal_rounding(self):
        for value, expected in ((1.23456, 1.235), (-1.23456, -1.235),
                                ("2.34567", 2.346), (0, 0.0), (3, 3.0)):
            with self.subTest(value=value):
                self.assertEqual(self.reward_float(value), expected)

    def test_large_representable_integer_rewards_remain_valid(self):
        for value in (10**300, -(10**300)):
            with self.subTest(positive=value > 0):
                self.assertEqual(self.reward_float(value), float(value))

    def test_boolean_conversion_is_unchanged(self):
        self.assertEqual(self.reward_float(True), 1.0)
        self.assertEqual(self.reward_float(False), 0.0)

    def test_nonfinite_behavior_remains_outside_overflow_fix(self):
        # EA-080-B2 has a separate, unresolved finite-value contract.
        for value in ("NaN", float("nan")):
            self.assertTrue(math.isnan(self.reward_float(value)))
        for value in ("Infinity", float("inf")):
            self.assertEqual(self.reward_float(value), float("inf"))
        for value in ("-Infinity", float("-inf")):
            self.assertEqual(self.reward_float(value), float("-inf"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
