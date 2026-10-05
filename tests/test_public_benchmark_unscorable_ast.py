"""EA-102 public-report regression using one source-extracted pure helper.

No project module, benchmark, runtime, store, provider or model is imported or
executed. Inputs are inert dictionaries matching inspected producer reports.
Run directly with stdlib unittest; the public benchmark entrypoint is not run.
"""
from __future__ import annotations

import __future__
import ast
from copy import deepcopy
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "eimemory/evaluation/public_benchmarks.py"
CLASSIFICATION_FIELDS = (
    "scoring_status", "unscorable_reason", "scored_sample_count",
    "unscorable_sample_count", "failure_count",
)
LEGACY_KEYS = {
    "r_at_1", "r_at_5", "mrr", "ndcg_at_5", "latency_ms_avg",
    "latency_ms_p95", "failure_samples",
}


def extract_metrics(source: str):
    tree = ast.parse(source)
    selected = [node for node in tree.body
                if isinstance(node, ast.FunctionDef) and node.name == "_metrics_for_suite"]
    assert len(selected) == 1
    assert not selected[0].decorator_list
    assert not any(isinstance(node, (ast.Import, ast.ImportFrom))
                   for node in ast.walk(selected[0]))
    module = ast.Module(body=selected, type_ignores=[])
    namespace = {"__builtins__": {"list": list}}
    exec(compile(module, "<public-report-helper-only>", "exec",
                 flags=__future__.annotations.compiler_flag, dont_inherit=True), namespace)
    return namespace["_metrics_for_suite"]


def skipped(case_id="missing-label"):
    return {"case_id": case_id, "scoring_status": "unscorable",
            "unscorable_reason": "missing_turn_annotations", "rank": None,
            "reciprocal_rank": None, "latency_ms": None}


def scored(case_id, rank):
    return {"case_id": case_id, "scoring_status": "scored",
            "unscorable_reason": "", "rank": rank,
            "reciprocal_rank": 1.0 / rank if rank else 0.0, "latency_ms": 1.0}


def producer_report(samples, *, scored_count, failure_count):
    return {
        "report_type": "longmemeval_eval", "sample_count": len(samples),
        "scoring_status": (
            "empty" if not samples else "unscorable" if not scored_count
            else "partially_scored" if scored_count < len(samples) else "scored"
        ),
        "unscorable_reason": "missing_turn_annotations" if samples and not scored_count else "",
        "scored_sample_count": scored_count,
        "unscorable_sample_count": len(samples) - scored_count,
        "failure_count": failure_count,
        "retrieval_recall_at_1": 0.0, "retrieval_recall_at_5": 0.0,
        "mrr": 0.0, "ndcg_at_5": 0.0,
        "latency_ms_avg": 0.0, "latency_ms_p95": 0.0, "samples": samples,
    }


class PublicBenchmarkUnscorableRegression(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.map_metrics = staticmethod(extract_metrics(SOURCE.read_text(encoding="utf-8")))

    def assert_classification(self, metrics, report):
        for key in CLASSIFICATION_FIELDS:
            self.assertEqual(metrics[key], report[key], key)

    def test_all_unscorable_has_no_failures_and_preserves_zero_counts(self):
        report = producer_report([skipped()], scored_count=0, failure_count=0)
        metrics = self.map_metrics(report)
        self.assertEqual(metrics["failure_samples"], [])
        self.assert_classification(metrics, report)

    def test_mixed_report_keeps_only_real_failure(self):
        miss = scored("miss", 0)
        report = producer_report([scored("hit", 1), skipped(), miss],
                                 scored_count=2, failure_count=1)
        metrics = self.map_metrics(report)
        self.assertEqual(metrics["failure_samples"], [miss])
        self.assert_classification(metrics, report)

    def test_skipped_rows_do_not_consume_twenty_failure_slots(self):
        misses = [scored(f"miss-{index}", 0) for index in range(25)]
        samples = [skipped(f"skip-{index}") for index in range(20)] + misses
        report = producer_report(samples, scored_count=25, failure_count=25)
        metrics = self.map_metrics(report)
        self.assertEqual(metrics["failure_samples"], misses[:20])
        self.assertEqual(metrics["failure_count"], 25)

    def test_empty_report_preserves_empty_classification(self):
        report = producer_report([], scored_count=0, failure_count=0)
        metrics = self.map_metrics(report)
        self.assertEqual(metrics["failure_samples"], [])
        self.assert_classification(metrics, report)

    def test_fully_scored_report_preserves_classification(self):
        report = producer_report([scored("hit", 3)], scored_count=1, failure_count=0)
        metrics = self.map_metrics(report)
        self.assertEqual(metrics["failure_samples"], [])
        self.assert_classification(metrics, report)

    def test_legacy_report_keeps_legacy_keys_and_rank_fallback(self):
        hit, miss = {"case_id": "hit", "rank": 1}, {"case_id": "miss", "rank": 0}
        metrics = self.map_metrics({"samples": [hit, miss]})
        self.assertEqual(set(metrics), LEGACY_KEYS)
        self.assertEqual(metrics["failure_samples"], [miss])

    def test_legacy_falsy_rank_semantics_remain_unchanged_without_status(self):
        rows = [{"case_id": "zero", "rank": 0}, {"case_id": "null", "rank": None},
                {"case_id": "absent"}]
        self.assertEqual(self.map_metrics({"samples": rows})["failure_samples"], rows)

    def test_explicit_nonempty_failure_samples_remain_authoritative(self):
        failures = [{"case_id": f"reported-{index}", "rank": 0} for index in range(21)]
        report = {"failure_samples": failures, "samples": [skipped(), scored("other", 0)]}
        self.assertIs(self.map_metrics(report)["failure_samples"], failures)

    def test_empty_explicit_failure_samples_keep_existing_fallback(self):
        miss = scored("miss", 0)
        report = {"failure_samples": [], "samples": [skipped(), miss]}
        self.assertEqual(self.map_metrics(report)["failure_samples"], [miss])

    def test_only_present_allowlisted_classification_fields_are_copied(self):
        values = {"scoring_status": "empty", "unscorable_reason": "",
                  "scored_sample_count": 0, "unscorable_sample_count": 0, "failure_count": 0}
        for key, value in values.items():
            with self.subTest(key=key):
                metrics = self.map_metrics({key: value, "unrelated_detail": "not a metric"})
                self.assertEqual(set(metrics), LEGACY_KEYS | {key})
                self.assertEqual(metrics[key], value)

    def test_locomo_count_and_explicit_failures_are_preserved(self):
        miss = {"case_id": "miss", "rank": 0}
        report = {"report_type": "locomo_eval", "failure_samples": [miss],
                  "failure_count": 1, "samples": [{"case_id": "hit", "rank": 2}, miss]}
        metrics = self.map_metrics(report)
        self.assertEqual(set(metrics), LEGACY_KEYS | {"failure_count"})
        self.assertEqual(metrics["failure_samples"], [miss])
        self.assertEqual(metrics["failure_count"], 1)

    def test_numeric_aliases_and_explicit_zero_values_are_unchanged(self):
        report = {"recall_at_1": 0.0, "retrieval_recall_at_1": 0.75,
                  "retrieval_recall_at_5": 0.5, "mrr": 0.25, "ndcg_at_5": 0.375,
                  "latency_ms_avg": 3.5, "latency_ms_p95": 9.0}
        self.assertEqual(self.map_metrics(report), {
            "r_at_1": 0.0, "r_at_5": 0.5, "mrr": 0.25, "ndcg_at_5": 0.375,
            "latency_ms_avg": 3.5, "latency_ms_p95": 9.0, "failure_samples": [],
        })

    def test_empty_legacy_report_keeps_original_defaults(self):
        self.assertEqual(self.map_metrics({}), {
            "r_at_1": 0.0, "r_at_5": 0.0, "mrr": 0.0, "ndcg_at_5": 0.0,
            "latency_ms_avg": 0.0, "latency_ms_p95": 0.0, "failure_samples": [],
        })

    def test_input_report_and_numeric_fields_are_unchanged(self):
        report = producer_report([skipped(), scored("miss", 0)], scored_count=1, failure_count=1)
        original = deepcopy(report)
        metrics = self.map_metrics(report)
        self.assertEqual(report, original)
        for output_key, source_key in (("r_at_1", "retrieval_recall_at_1"),
                                       ("r_at_5", "retrieval_recall_at_5"),
                                       ("mrr", "mrr"), ("ndcg_at_5", "ndcg_at_5"),
                                       ("latency_ms_avg", "latency_ms_avg"),
                                       ("latency_ms_p95", "latency_ms_p95")):
            self.assertEqual(metrics[output_key], original[source_key])


if __name__ == "__main__":
    unittest.main(verbosity=2)
