"""AST-only phase aggregation with explicit finite nonnegative ranks and arithmetic stubs."""
from __future__ import annotations

import argparse
import ast
import builtins
from collections import defaultdict
import copy
import hashlib
from pathlib import Path
import unittest

BASELINE_SHA256 = "fdebb8160479d7f3c00ffcae41346eae1eb2f2a9b6a774742a4e62254ee6b417"
PHASES = {"usage", "synthetic_alpha", "synthetic_beta"}
ZERO = {"sample_count": 0, "pass_rate": 0.0, "hallucination_rate": 0.0, "mrr": 0.0, "recall_at_k": 0.0, "precision_at_k": 0.0}


def extract_shell(path, expected):
    data = path.read_bytes(); actual = hashlib.sha256(data).hexdigest()
    if actual != expected:
        raise ValueError(f"Pinned source digest mismatch: {actual}")
    module = ast.parse(data, filename=str(path))
    found = [n for n in module.body if isinstance(n, ast.FunctionDef) and n.name == "_phase_scores"]
    if len(found) != 1 or found[0].decorator_list:
        raise ValueError("Expected one complete undecorated function")
    method = found[0]
    if actual == BASELINE_SHA256 and (method.lineno, method.end_lineno) != (539, 575):
        raise ValueError("Unexpected baseline boundary")
    forbidden = []
    def fenced_import(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "__future__" and level == 0:
            return builtins.__import__(name, globals, locals, fromlist, level)
        forbidden.append(name)
        raise AssertionError("Target/metric/evaluation/helper imports are forbidden")
    namespace = {"__builtins__": {**vars(builtins), "__import__": fenced_import}, "defaultdict": defaultdict, "SUPPORTED_PHASES": set(PHASES)}
    isolated = ast.fix_missing_locations(ast.Module(body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0), copy.deepcopy(method)], type_ignores=[]))
    exec(compile(isolated, str(path), "exec"), namespace)
    print(f"SOURCE_SHA256 {actual}")
    print(f"EXTRACTED _phase_scores L{method.lineno}-{method.end_lineno}")
    return namespace, forbidden


def mrr_tests(namespace, forbidden):
    class PhaseMrrDenominatorTests(unittest.TestCase):
        def setUp(self):
            self.rank_calls = []; self.pass_calls = []
            def reciprocal_mean(ranks):
                values = list(ranks)
                self.assertTrue(values)
                self.assertTrue(all(type(value) is int and value >= 0 for value in values))
                self.rank_calls.append(values)
                return sum(0.0 if value == 0 else 1.0 / value for value in values) / len(values)
            def pass_rate(passed):
                values = list(passed)
                self.assertTrue(values)
                self.assertTrue(all(type(value) is bool for value in values))
                self.pass_calls.append(values)
                return sum(values) / len(values)
            namespace.update(mean_reciprocal_rank=reciprocal_mean, binary_pass_rate=pass_rate)
        def tearDown(self):
            self.assertEqual(forbidden, [])
            self.assertEqual(namespace["SUPPORTED_PHASES"], PHASES)
        def evaluate(self, samples):
            for sample in samples:
                self.assertTrue(set(sample) <= {"phase", "expected_rank", "passed", "hallucinated"})
                self.assertIs(type(sample["expected_rank"]), int)
                self.assertGreaterEqual(sample["expected_rank"], 0)
                if "phase" in sample:
                    self.assertIs(type(sample["phase"]), str)
                for flag in ("passed", "hallucinated"):
                    if flag in sample:
                        self.assertIs(type(sample[flag]), bool)
            before = copy.deepcopy(samples)
            identities = [id(sample) for sample in samples]
            result = namespace["_phase_scores"](samples)
            self.assertEqual(samples, before)
            self.assertEqual([id(sample) for sample in samples], identities)
            self.assertEqual(list(result), sorted(PHASES))
            return result
        def test_rank_one_and_miss_keep_half_mrr(self):
            result = self.evaluate([{"phase": "usage", "expected_rank": 1, "passed": True}, {"phase": "usage", "expected_rank": 0, "hallucinated": True}])
            self.assertEqual(result["usage"], {"sample_count": 2, "pass_rate": 0.5, "hallucination_rate": 0.5, "mrr": 0.5, "recall_at_k": 0.5, "precision_at_k": 0.5})
            self.assertEqual(self.rank_calls, [[1, 0]])
            self.assertEqual(self.pass_calls, [[True, False]])
        def test_rank_two_and_miss_keep_quarter_mrr(self):
            result = self.evaluate([{"phase": "usage", "expected_rank": 2}, {"phase": "usage", "expected_rank": 0}])
            self.assertEqual(result["usage"]["mrr"], 0.25)
            self.assertEqual(result["usage"]["recall_at_k"], 0.5)
            self.assertEqual(result["usage"]["precision_at_k"], 0.5)
            self.assertEqual(self.rank_calls, [[2, 0]])
        def test_positive_ranks_keep_reciprocal_mean_and_order(self):
            result = self.evaluate([{"phase": "usage", "expected_rank": 2, "passed": True}, {"phase": "usage", "expected_rank": 1, "passed": True}])
            self.assertEqual(result["usage"], {"sample_count": 2, "pass_rate": 1.0, "hallucination_rate": 0.0, "mrr": 0.75, "recall_at_k": 1.0, "precision_at_k": 1.0})
            self.assertEqual(self.rank_calls, [[2, 1]])
        def test_all_misses_keep_zero_contribution_rows(self):
            result = self.evaluate([{"phase": "usage", "expected_rank": 0}, {"phase": "usage", "expected_rank": 0}])
            self.assertEqual(result["usage"], {**ZERO, "sample_count": 2})
            self.assertEqual(self.rank_calls, [[0, 0]])
            self.assertEqual(self.pass_calls, [[False, False]])
        def test_empty_input_keeps_zero_groups_without_metric_calls(self):
            result = self.evaluate([])
            self.assertEqual(result, {phase: dict(ZERO) for phase in sorted(PHASES)})
            self.assertEqual(self.rank_calls, []); self.assertEqual(self.pass_calls, [])
        def test_empty_phase_in_nonempty_input_keeps_zero_metrics(self):
            result = self.evaluate([{"phase": "usage", "expected_rank": 1}])
            self.assertEqual(result["synthetic_alpha"], ZERO)
            self.assertEqual(result["synthetic_beta"], ZERO)
            self.assertEqual(self.rank_calls, [[1]])
            self.assertEqual(self.pass_calls, [[False]])
        def test_synthetic_phases_keep_independent_denominators(self):
            result = self.evaluate([{"phase": "synthetic_beta", "expected_rank": 2}, {"phase": "synthetic_alpha", "expected_rank": 1}, {"phase": "synthetic_beta", "expected_rank": 0}, {"phase": "synthetic_alpha", "expected_rank": 0}, {"phase": "usage", "expected_rank": 1}])
            self.assertEqual(result["synthetic_alpha"]["mrr"], 0.5)
            self.assertEqual(result["synthetic_beta"]["mrr"], 0.25)
            self.assertEqual(result["usage"]["mrr"], 1.0)
            self.assertEqual([result[p]["sample_count"] for p in sorted(PHASES)], [2, 2, 1])
            self.assertEqual(self.rank_calls, [[1, 0], [2, 0], [1]])
        def test_default_usage_and_unknown_phase_handling_are_unchanged(self):
            result = self.evaluate([{"expected_rank": 1}, {"phase": "", "expected_rank": 0}, {"phase": "synthetic_unknown", "expected_rank": 2}])
            self.assertEqual(result["usage"]["sample_count"], 2)
            self.assertEqual(result["usage"]["mrr"], 0.5)
            self.assertNotIn("synthetic_unknown", result)
            self.assertEqual(self.rank_calls, [[1, 0]])
        def test_other_numeric_fields_keep_existing_results_on_mixed_rows(self):
            result = self.evaluate([{"phase": "usage", "expected_rank": 0, "passed": True}, {"phase": "usage", "expected_rank": 2, "hallucinated": True}, {"phase": "usage", "expected_rank": 0}, {"phase": "usage", "expected_rank": 1, "passed": True}])
            self.assertEqual(result["usage"], {"sample_count": 4, "pass_rate": 0.5, "hallucination_rate": 0.25, "mrr": 0.375, "recall_at_k": 0.5, "precision_at_k": 0.5})
            self.assertEqual(self.rank_calls, [[0, 2, 0, 1]])
            self.assertEqual(self.pass_calls, [[True, False, False, True]])
    return PhaseMrrDenominatorTests


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--expected-sha256", required=True)
    args = parser.parse_args()
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(mrr_tests(*extract_shell(args.source, args.expected_sha256)))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
