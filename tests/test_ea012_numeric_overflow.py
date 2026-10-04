"""Isolated ordinary score-number regressions; no project imports."""

import ast
import math
from pathlib import Path
import unittest


def _load_validate_scores():
    path = Path(__file__).resolve().parents[1] / "eimemory/retrieval/relevance.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    nodes = [
        node for node in tree.body
        if (isinstance(node, ast.FunctionDef) and node.name == "validate_scores")
        or (isinstance(node, ast.ClassDef) and node.name == "RelevanceUnavailable")
    ]
    namespace = {"math": math}
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(path), "exec"), namespace)
    return namespace["validate_scores"], namespace["RelevanceUnavailable"]


class NumericOverflowTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        validate, error = _load_validate_scores()
        cls.validate = staticmethod(validate)
        cls.error = error

    def test_unrepresentable_integers_use_existing_error(self):
        for value in (10**400, -(10**400)):
            with self.subTest(sign=value > 0):
                with self.assertRaisesRegex(self.error, "^reranker_response_invalid$"):
                    self.validate([{"index": 0, "score": value}], 1)

    def test_large_finite_integers_remain_valid(self):
        for value in (10**300, -(10**300)):
            with self.subTest(sign=value > 0):
                self.assertEqual(self.validate([{"index": 0, "score": value}], 1), [float(value)])

    def test_score_order_and_empty_input_are_preserved(self):
        self.assertEqual(self.validate([], 0), [])
        self.assertEqual(self.validate([
            {"index": 1, "score": -2}, {"index": 0, "score": 3.5},
        ], 2), [3.5, -2.0])


if __name__ == "__main__":
    unittest.main()
