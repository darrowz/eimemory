"""EA-105: isolated synthetic nearest-rank and aggregate regressions.

Run directly with Python's stdlib unittest runner. Only the named, inspected
numeric/report functions are AST-extracted; project imports and module startup
are never executed. Aggregate dependencies are explicitly supplied below.
"""

from __future__ import annotations

import ast
import builtins
from copy import deepcopy
from fractions import Fraction
import math
from pathlib import Path
from statistics import mean
from types import SimpleNamespace
import unittest


ROOT = Path(__file__).resolve().parents[1]
RUNNER = ROOT / "scripts/run_full_eval.py"
METRICS = ROOT / "eimemory/evaluation/metrics.py"
AGGREGATES = ("aggregate_lme_reports", "aggregate_loc_reports")


def _functions(source, names, filename):
    tree = ast.parse(source, filename=str(filename))
    nodes = [node for node in tree.body
             if isinstance(node, ast.FunctionDef) and node.name in names]
    if {node.name for node in nodes} != set(names):
        raise AssertionError("Expected functions not found")
    if any(node.decorator_list for node in nodes):
        raise AssertionError("Unexpected function decorators")
    return ast.Module(body=nodes, type_ignores=[])


def load_helpers(runner_source=None, *, pct_override=None):
    """Load pure functions with blocked imports and inert aggregate logging."""
    safe_builtins = {name: getattr(builtins, name) for name in (
        "dict", "list", "int", "float", "str", "len", "min", "max", "round",
        "sorted", "sum", "TypeError", "ValueError",
    )}

    def blocked_import(*args, **kwargs):
        raise AssertionError("Unexpected import from extracted function")

    safe_builtins["__import__"] = blocked_import
    numeric = {"__builtins__": safe_builtins, "mean": mean}
    exec(compile(_functions(METRICS.read_text(encoding="utf-8"),
                            {"_round", "percentile", "mean_reciprocal_rank"},
                            METRICS), str(METRICS), "exec"), numeric)
    import_calls = []

    def approved_import(name, globals=None, locals=None, fromlist=(), level=0):
        import_calls.append((name, tuple(fromlist), level))
        if (name, tuple(fromlist), level) == (
            "eimemory.evaluation.metrics", ("mean_reciprocal_rank",), 0
        ):
            return SimpleNamespace(mean_reciprocal_rank=numeric["mean_reciprocal_rank"])
        if (name, tuple(fromlist), level) == ("statistics", ("mean",), 0):
            return SimpleNamespace(mean=mean)
        raise AssertionError(f"Unexpected import: {name}, {fromlist}, {level}")

    namespace = {
        "__builtins__": dict(safe_builtins, __import__=approved_import),
        "Any": object,
        "log": lambda message: None,
    }
    if runner_source is None:
        runner_source = RUNNER.read_text(encoding="utf-8")
    exec(compile(_functions(runner_source,
                            {"_safe_int", "_safe_float", "_pct", *AGGREGATES},
                            RUNNER), str(RUNNER), "exec"), namespace)
    if pct_override is not None:
        namespace["_pct"] = pct_override
    namespace["percentile"] = numeric["percentile"]
    namespace["import_calls"] = import_calls
    return namespace


def legacy_pct(values, p):
    """Prior rank convention, used only to check unrelated aggregate fields."""
    if not values:
        return 0.0
    ordered = sorted(values)
    idx = max(0, min(len(ordered) - 1, int(round((p / 100) * (len(ordered) - 1)))))
    return round(ordered[idx], 3)


def chunks(groups):
    return [
        {"ok": True, "chunk_id": i, "n": len(group), "elapsed": 0.0,
         "report": {"samples": group, "latency_ms_p95": -999.0}}
        for i, group in enumerate(groups)
    ]


def samples_for(latencies):
    return [
        {"case_id": f"case-{i}", "rank": i % 3, "latency_ms": latency,
         "retrieval_recall_at_1": 0.25, "retrieval_recall_at_5": 0.5,
         "retrieval_recall_at_10": 0.75, "recall_at_1": 0.25,
         "recall_at_5": 0.5, "recall_at_10": 0.75,
         "recall_any_at_5": 0.5, "recall_all_at_5": 0.25, "ndcg_at_5": 0.125}
        for i, latency in enumerate(latencies)
    ]


class PercentileConsistencyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.helpers = load_helpers()
        cls.old_helpers = load_helpers(pct_override=legacy_pct)

    def test_twelve_sample_reproducer(self):
        values = [1.0] * 11 + [1000.0]
        self.assertEqual(legacy_pct(values, 95), 1.0)
        self.assertEqual(self.helpers["percentile"](values, 95), 1000.0)
        self.assertEqual(self.helpers["_pct"](values, 95), 1000.0)

    def test_exact_rational_oracle_and_main_metric_parity(self):
        for size in range(1, 1001):
            values = list(range(1, size + 1))
            for pct in range(101):
                expected = max(1, math.ceil(Fraction(pct * size, 100)))
                actual = self.helpers["_pct"](values, pct)
                if actual != expected:
                    self.fail(f"size={size}, p={pct}: {actual} != {expected}")
                self.assertEqual(actual, self.helpers["percentile"](values, pct))

    def test_integer_boundary_does_not_reintroduce_float_rank_error(self):
        self.assertEqual(self.helpers["_pct"](list(range(1, 101)), 7), 7)

    def test_empty_returns_float_zero_before_reading_percent(self):
        result = self.helpers["_pct"]([], object())
        self.assertEqual(result, 0.0)
        self.assertIsInstance(result, float)

    def test_clamps_integer_percentiles_and_keeps_endpoints(self):
        for pct, expected in [(-10**100, -5.0), (-1, -5.0), (0, -5.0),
                              (100, 8.0), (101, 8.0), (10**100, 8.0)]:
            with self.subTest(pct=pct):
                self.assertEqual(self.helpers["_pct"]([8.0, 2.0, -5.0], pct), expected)

    def test_singleton_rounding(self):
        for pct in range(-1, 102):
            self.assertEqual(self.helpers["_pct"]([2.12349], pct), 2.123)

    def test_sort_duplicates_rounding_and_no_mutation(self):
        values = [3.33339, -2.0, 1.23456, 1.23456]
        original = values.copy()
        for pct, expected in [(0, -2.0), (50, 1.235), (95, 3.333), (100, 3.333)]:
            self.assertEqual(self.helpers["_pct"](values, pct), expected)
        self.assertEqual(values, original)
        # Preserve _pct's own output type instead of adding float coercion.
        self.assertIsInstance(self.helpers["_pct"]([1, 2], 100), int)

    def test_p95_twenty_sample_boundary(self):
        for count, expected in [(19, 1000.0), (20, 1.0), (21, 1.0)]:
            values = [1.0] * (count - 1) + [1000.0]
            self.assertEqual(self.helpers["_pct"](values, 95), expected)

    def test_both_aggregates_recompute_sample_p95(self):
        rows = samples_for([1.0] * 11 + [1000.0])
        for name in AGGREGATES:
            with self.subTest(name=name):
                result = self.helpers[name](chunks([rows]))
                self.assertEqual(result["latency_ms_p95"], 1000.0)
                self.assertEqual(result["latency_ms_avg"], 84.25)
                self.assertEqual(result["sample_count"], 12)

    def test_all_contiguous_partitions_keep_aggregate_metrics(self):
        rows = samples_for([1.0] * 11 + [1000.0])
        for name in AGGREGATES:
            expected = self.helpers[name](chunks([rows]))
            expected.pop("chunks_processed")
            for mask in range(1 << (len(rows) - 1)):
                groups, start = [], 0
                for index in range(1, len(rows)):
                    if mask & (1 << (index - 1)):
                        groups.append(rows[start:index])
                        start = index
                groups.append(rows[start:])
                actual = self.helpers[name](chunks(groups))
                self.assertEqual(actual.pop("chunks_processed"), len(groups))
                self.assertEqual(actual, expected, (name, mask))

    def test_missing_null_and_invalid_latency_are_excluded(self):
        rows = [{"rank": 0}, {"rank": None, "latency_ms": None},
                {"rank": "bad", "latency_ms": "bad"},
                {"rank": "2", "latency_ms": "12.34567"}]
        for name in AGGREGATES:
            with self.subTest(name=name):
                result = self.helpers[name](chunks([rows]))
                self.assertEqual(result["sample_count"], 4)
                self.assertEqual(result["latency_ms_avg"], 12.346)
                self.assertEqual(result["latency_ms_p95"], 12.346)
                self.assertEqual(result["failure_count"], 3)
                self.assertEqual(result["mrr"], 0.125)

    def test_no_valid_latencies_retains_zero(self):
        for name in AGGREGATES:
            result = self.helpers[name](chunks([[{}, {"latency_ms": None},
                                                {"latency_ms": "invalid"}]]))
            self.assertEqual(result["latency_ms_avg"], 0.0)
            self.assertEqual(result["latency_ms_p95"], 0.0)

    def test_lme_excludes_unscorable_from_latency_ranks_and_metrics(self):
        scored = samples_for([1.0] * 11 + [1000.0])
        skipped = {"scoring_status": "unscorable", "rank": None, "latency_ms": 999999.0,
                   "retrieval_recall_at_1": None, "retrieval_recall_at_5": None,
                   "retrieval_recall_at_10": None, "recall_any_at_5": None,
                   "recall_all_at_5": None, "ndcg_at_5": None}
        base = self.helpers["aggregate_lme_reports"](chunks([scored]))
        result = self.helpers["aggregate_lme_reports"](chunks([[skipped], scored]))
        for field in ("latency_ms_avg", "latency_ms_p95", "mrr", "rank_histogram",
                      "failure_count", "failure_examples", "retrieval_recall_at_1",
                      "retrieval_recall_at_5", "retrieval_recall_at_10",
                      "recall_any_at_5", "recall_all_at_5", "ndcg_at_5"):
            self.assertEqual(result[field], base[field], field)
        self.assertEqual(result["sample_count"], 13)
        self.assertEqual(result["scored_sample_count"], 12)
        self.assertEqual(result["unscorable_sample_count"], 1)
        self.assertEqual(result["unscorable_samples"], [skipped])
        self.assertEqual(result["scoring_status"], "partially_scored")

    def test_lme_all_unscorable_keeps_classification_and_zeros(self):
        rows = [{"scoring_status": "unscorable", "rank": None, "latency_ms": None},
                {"scoring_status": "unscorable", "rank": None, "latency_ms": 1000.0}]
        result = self.helpers["aggregate_lme_reports"](chunks([rows]))
        self.assertEqual(result["scoring_status"], "unscorable")
        self.assertEqual(result["unscorable_reason"], "missing_turn_annotations")
        self.assertEqual(result["scored_sample_count"], 0)
        self.assertEqual(result["unscorable_sample_count"], 2)
        for field in ("latency_ms_avg", "latency_ms_p95", "mrr", "failure_count"):
            self.assertEqual(result[field], 0.0)
        self.assertEqual(result["rank_histogram"], {})
        self.assertEqual(result["failure_examples"], [])

    def test_locomo_preserves_existing_population_without_status_filter(self):
        rows = samples_for([1.0] * 11)
        rows.append({"scoring_status": "unscorable", "rank": None, "latency_ms": 1000.0})
        result = self.helpers["aggregate_loc_reports"](chunks([rows]))
        self.assertEqual(result["sample_count"], 12)
        self.assertEqual(result["latency_ms_avg"], 84.25)
        self.assertEqual(result["latency_ms_p95"], 1000.0)
        self.assertEqual(result["failure_count"], 5)

    def test_empty_and_failed_chunks_keep_error_contract(self):
        failed = {"ok": False, "chunk_id": 1, "error": "synthetic"}
        for name in AGGREGATES:
            for results, failures in [([], 0), (chunks([[]]), 0), ([failed], 1),
                                       (chunks([[]]) + [failed], 1)]:
                self.assertEqual(self.helpers[name](results), {
                    "ok": False, "error": "no successful chunks", "chunks_failed": failures})

    def test_non_percentile_fields_and_input_unchanged(self):
        rows = samples_for([1.0] * 11 + [1000.0]) + [{"rank": 0}, {"rank": 2, "latency_ms": None}]
        results = chunks([rows[:3], [], rows[3:]])
        results.append({"ok": False, "chunk_id": 3, "error": "synthetic",
                        "report": {"samples": [{"rank": 100, "latency_ms": 999999.0}]}})
        original = deepcopy(results)
        options = {"granularity": "session", "worker_count": 7, "limit": 2,
                   "reranker": "deterministic"}
        for name in AGGREGATES:
            actual = self.helpers[name](results, **options)
            expected = self.old_helpers[name](results, **options)
            self.assertEqual(actual.pop("latency_ms_p95"), 1000.0)
            self.assertEqual(expected.pop("latency_ms_p95"), 1.0)
            self.assertEqual(actual, expected)
            self.assertEqual(results, original)

    def test_dependency_imports_are_only_supplied_inert_helpers(self):
        helpers = load_helpers()
        for name in AGGREGATES:
            helpers[name](chunks([[{"rank": 1, "latency_ms": 2.0}]]))
        self.assertEqual(helpers["import_calls"], [
            ("eimemory.evaluation.metrics", ("mean_reciprocal_rank",), 0),
            ("statistics", ("mean",), 0),
        ] * 2)


if __name__ == "__main__":
    unittest.main()
