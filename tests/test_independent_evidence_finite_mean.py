"""Isolated summarize regression; never import or execute the project module."""
import ast
from fractions import Fraction
import json
import math
from pathlib import Path
from statistics import mean
import sys
import unittest


SOURCE_PATH = (
    Path(__file__).resolve().parents[1]
    / "eimemory" / "evaluation" / "independent_evidence.py"
)


def load_summary(path):
    """Read only the target source; execute REQUIRED and summarize, not main."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    required = [
        node for node in tree.body
        if isinstance(node, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id == "REQUIRED" for t in node.targets)
    ]
    functions = [
        node for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "summarize"
    ]
    assert len(required) == len(functions) == 1
    assignment, function = required[0], functions[0]
    added_import = [
        node for node in tree.body
        if isinstance(node, ast.ImportFrom) and node.module == "statistics"
    ]
    assert len(added_import) <= 1
    if added_import:
        imported = added_import[0]
        assert imported.level == 0
        assert [(n.name, n.asname) for n in imported.names] == [("mean", None)]
    offset = int(bool(added_import))
    assert (assignment.lineno, assignment.end_lineno) == (12 + offset, 12 + offset)
    assert (function.lineno, function.end_lineno) == (15 + offset, 44 + offset)
    assert len(assignment.targets) == 1
    assert isinstance(assignment.targets[0], ast.Name)
    assert assignment.targets[0].id == "REQUIRED"
    assert ast.literal_eval(assignment.value) == {
        "elapsed_ms", "correct", "delivered", "retrieval_status", "route"
    }
    assert not function.decorator_list and function.returns is None
    args = function.args
    assert not args.posonlyargs and not args.kwonlyargs
    assert not args.defaults and not args.kw_defaults
    assert args.vararg is None and args.kwarg is None
    assert len(args.args) == 1 and args.args[0].arg == "samples"
    assert args.args[0].annotation is None
    assert not getattr(function, "type_params", [])
    selected = ast.Module(body=[assignment, function], type_ignores=[])
    namespace = {"math": math, "mean": mean}
    exec(compile(selected, str(path) + ":isolated", "exec"), namespace)
    return namespace["summarize"]


def row(latency, **changes):
    result = {
        "elapsed_ms": latency, "correct": True, "delivered": True,
        "retrieval_status": "no_evidence", "route": "local",
    }
    result.update(changes)
    return result


class FiniteMeanTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.summarize = staticmethod(load_summary(SOURCE_PATH))

    def test_overflow_regression_and_other_fields(self):
        result = self.summarize([row(1e308), row(1e308)])
        self.assertEqual(result, {
            "count": 2, "within_3s_effective": 0,
            "within_3s_effective_rate": 0.0, "effective": 2,
            "unavailable": 0, "max_ms": 1e308, "mean_ms": 1e308,
            "sample_nearest_rank_p95_ms": 1e308, "verdict": "fail",
            "boundary": "sample_only_not_production_slo_or_natural_gold",
            "routes": {"local": 2, "model": 0, "negative": 0, "unknown": 0},
        })
        json.dumps(result, allow_nan=False)

    def test_finite_means_match_exact_rational_oracle(self):
        largest = sys.float_info.max
        smallest = float.fromhex("0x0.0000000000001p-1022")
        cases = [
            [0], [0.0], [7], [1, 2], [1, 2.5], [100, 200, 300],
            [1e308] * 2, [1e308] * 3,
            [largest] * 2, [largest] * 3, [largest, 0],
            [10**308] * 2, [10**308, 1e308],
            [smallest] * 2, [smallest] * 3, [0, smallest],
            [smallest, smallest * 2], [largest, smallest, 0],
        ]
        for values in cases:
            with self.subTest(values=values):
                result = self.summarize([row(value) for value in values])
                actual = result["mean_ms"]
                expected = float(sum(map(Fraction, values), Fraction()) / len(values))
                self.assertIs(type(actual), float)
                self.assertTrue(math.isfinite(actual))
                self.assertEqual(actual, expected)
                # Compare rounded endpoints: an exact integer endpoint may not
                # itself be representable as a float. The rational oracle above
                # still checks the correctly rounded mean without tolerance.
                self.assertLessEqual(float(min(values)), actual)
                self.assertLessEqual(actual, float(max(values)))
                json.dumps(result, allow_nan=False)

    def test_empty(self):
        self.assertEqual(self.summarize([]), {
            "count": 0, "verdict": "unknown", "reason": "no_samples",
        })

    def test_labels_routes_and_inclusive_budget(self):
        result = self.summarize([
            row(0),
            row(3000, retrieval_status="evidence_found", route="model"),
            row(6000, retrieval_status="unavailable", route="negative"),
            row(9000, correct=False, delivered=False, route="unknown"),
        ])
        self.assertEqual(result, {
            "count": 4, "within_3s_effective": 2,
            "within_3s_effective_rate": 0.5, "effective": 2,
            "unavailable": 1, "max_ms": 9000, "mean_ms": 4500.0,
            "sample_nearest_rank_p95_ms": 9000, "verdict": "fail",
            "boundary": "sample_only_not_production_slo_or_natural_gold",
            "routes": {"local": 1, "model": 1, "negative": 1, "unknown": 1},
        })
        json.dumps(result, allow_nan=False)

    def test_invalid_latencies_retain_rejection(self):
        for value in [-1, -0.5, float("nan"), float("inf"), -float("inf"), True, False, "1", None]:
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, "^acceptance_time_invalid$"):
                    self.summarize([row(value)])

    def test_other_validation_unchanged(self):
        cases = [
            (None, "acceptance_fields_missing"),
            ({}, "acceptance_fields_missing"),
            (row(1, correct=1), "acceptance_label_invalid"),
            (row(1, delivered=0), "acceptance_label_invalid"),
            (row(1, retrieval_status="invalid"), "acceptance_status_invalid"),
            (row(1, route="invalid"), "acceptance_route_invalid"),
        ]
        for value, message in cases:
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, "^" + message + "$"):
                    self.summarize([value])


if __name__ == "__main__":
    unittest.main()
