"""EA-075 synthetic control-flow regression; no project modules are imported.

Only inspected function definitions are AST-extracted. Runtime, normalization,
retrieval, ingestion, scope, metrics, clock and logging are synthetic callbacks.
The aggregator's two known imports are removed and supplied as callbacks.
Run directly with stdlib unittest; no providers, stores, or real datasets run.
"""
from __future__ import annotations

import ast
import copy
from pathlib import Path
from statistics import mean
from types import SimpleNamespace
import unittest

ROOT = Path(__file__).resolve().parents[1]
METRICS = ("retrieval_recall_at_1", "retrieval_recall_at_5", "retrieval_recall_at_10",
           "recall_any_at_1", "recall_any_at_5", "recall_any_at_10",
           "recall_all_at_1", "recall_all_at_5", "recall_all_at_10", "ndcg_at_5")


def extract(path, names, namespace, allowed_imports=()):
    tree = ast.parse((ROOT / path).read_text(encoding="utf-8"))
    definitions = [copy.deepcopy(node) for node in tree.body
                   if isinstance(node, ast.FunctionDef) and node.name in names]
    assert {node.name for node in definitions} == set(names)
    for definition in definitions:
        for node in ast.walk(definition):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                assert isinstance(node, ast.ImportFrom)
                assert (node.module, tuple((alias.name, alias.asname) for alias in node.names)) in allowed_imports
        definition.body = [node for node in definition.body if not isinstance(node, ast.ImportFrom)]
        assert not any(isinstance(node, (ast.Import, ast.ImportFrom)) for node in ast.walk(definition))
    module = ast.Module(body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0),
                              *definitions], type_ignores=[])
    exec(compile(ast.fix_missing_locations(module), "<synthetic-lme-control-flow>", "exec"), namespace)


def score(returned, expected, *, k):
    # Empty labels must never reach a metric in these turn-mode fixtures.
    assert expected
    return len(set(returned[:k]) & expected) / len(expected)


def rank(returned, expected):
    return next((i for i, value in enumerate(returned, 1) if value in expected), 0)


def mrr(ranks):
    return round(mean(1 / value if value else 0 for value in ranks), 3) if ranks else 0.0


class Scope:
    @staticmethod
    def from_dict(value):
        return dict(value)


class UnscorableTurnRegression(unittest.TestCase):
    def setUp(self):
        self.ingested = []
        self.retrieved = []
        self.metric_calls = []
        self.clock = iter(range(100))

        def retrieve(runtime, **kwargs):
            self.retrieved.append(kwargs["benchmark_case_id"])
            return runtime[kwargs["benchmark_case_id"]]

        def metric(returned, expected, *, k):
            self.metric_calls.append((set(expected), k))
            return score(returned, expected, k=k)

        self.ns = {
            "normalize_longmemeval_dataset": lambda dataset: dataset,
            "assert_isolated_benchmark_runtime": lambda *args, **kwargs: None,
            "ScopeRef": Scope, "asdict": dict,
            "_ingest_case_chunks": lambda runtime, *, case, scope: self.ingested.append(case["case_id"]),
            "_retrieve": retrieve,
            "_returned_ids": lambda records, **kwargs: records,
            "_hit_ids": lambda records, **kwargs: [],
            "first_relevant_rank": rank, "mean_reciprocal_rank": mrr,
            "recall_at_k": metric, "recall_any_at_k": metric,
            "recall_all_at_k": metric, "ndcg_at_k": metric,
            "mean": mean, "_mean": mean,
            "percentile": lambda values, percent: max(values, default=0.0),
            "_pct": lambda values, percent: max(values, default=0.0),
            "perf_counter": lambda: next(self.clock), "now_iso": lambda: "synthetic",
            "log": lambda text: None,
            "_safe_int": lambda value, default=0: int(value) if value is not None else default,
            "_safe_float": lambda value: float(value) if value is not None else None,
        }
        extract("eimemory/evaluation/longmemeval.py",
                ("run_longmemeval", "_expected_ids", "_avg", "_summarize_samples", "_normalize_choice"), self.ns)
        self.ns["_as_int"] = self.ns["_safe_int"]
        self.ns["_as_float"] = lambda value, default=0.0: float(value) if value is not None else default
        extract("eimemory/governance/snapshot.py", ("_longmemeval_summary",), self.ns)
        extract("scripts/run_full_eval.py", ("aggregate_lme_reports",), self.ns,
                allowed_imports=(("eimemory.evaluation.metrics", (("mean_reciprocal_rank", None),)),
                                 ("statistics", (("mean", "_mean"),))))

    def case(self, case_id, turns=(), question_type="mixed"):
        return {"case_id": case_id, "question": "synthetic question", "question_type": question_type,
                "scope": {}, "chunks": [{"chunk_id": "c", "session_id": "s"}],
                "evidence_session_ids": ["s"], "evidence_turn_ids": list(turns), "evidence_chunk_ids": []}

    def run_cases(self, cases, returned=None, granularity="turn", mode="raw"):
        return self.ns["run_longmemeval"](returned or {}, {"name": "synthetic", "scope": {}, "cases": cases},
                                         granularity=granularity, mode=mode)

    def aggregate(self, reports):
        return self.ns["aggregate_lme_reports"]([
            {"ok": True, "chunk_id": i, "n": report["sample_count"], "elapsed": 0, "report": report}
            for i, report in enumerate(reports)])

    def assert_counts(self, report, total, scored, unscorable, failures):
        status = "empty" if not total else "unscorable" if not scored else "partially_scored" if unscorable else "scored"
        self.assertEqual(report["scoring_status"], status)
        self.assertEqual(report["unscorable_reason"], "missing_turn_annotations" if total and not scored else "")
        self.assertEqual([report[key] for key in ("sample_count", "scored_sample_count", "unscorable_sample_count", "failure_count")],
                         [total, scored, unscorable, failures])

    def test_mixed_turn_cases_exclude_missing_labels_from_every_metric(self):
        cases = [self.case("skip"), self.case("hit", ["t"]), self.case("miss", ["t"])]
        for mode in ("raw", "hybrid"):
            with self.subTest(mode=mode):
                self.ingested.clear(); self.retrieved.clear()
                report = self.run_cases(cases, {"hit": ["t"], "miss": []}, mode=mode)
                self.assert_counts(report, 3, 2, 1, 1)
                self.assert_counts(report["by_question_type"]["mixed"], 3, 2, 1, 1)
                for metric in METRICS:
                    self.assertEqual(report[metric], 0.5)
                self.assertEqual(report["mrr"], 0.5)
                self.assertEqual(report["by_question_type"]["mixed"]["mrr"], 0.5)
                self.assertEqual(self.ingested, ["hit", "miss"])
                self.assertEqual(self.retrieved, ["hit", "miss"])
                skipped = report["samples"][0]
                self.assertEqual(skipped["scoring_status"], "unscorable")
                self.assertEqual(skipped["unscorable_reason"], "missing_turn_annotations")
                self.assertEqual(skipped["expected_ids"], [])
                self.assertEqual(skipped["returned_ids"], [])
                for metric in (*METRICS, "rank", "reciprocal_rank", "latency_ms"):
                    self.assertIsNone(skipped[metric])

    def test_all_unscorable_cases_stay_visible_without_inflation_or_side_effects(self):
        report = self.run_cases([self.case("skip", question_type="unknown-turns")])
        self.assert_counts(report, 1, 0, 1, 0)
        self.assert_counts(report["by_question_type"]["unknown-turns"], 1, 0, 1, 0)
        for metric in (*METRICS, "mrr", "latency_ms_avg", "latency_ms_p95"):
            self.assertEqual(report[metric], 0.0)
        self.assertFalse(self.metric_calls)
        self.assertFalse(self.ingested)
        self.assertFalse(self.retrieved)

    def test_valid_turns_and_session_chunk_modes_keep_normal_scoring(self):
        for granularity, turns, expected in (("turn", ["t"], "t"), ("session", [], "s"), ("chunk", [], "c")):
            with self.subTest(granularity=granularity):
                report = self.run_cases([self.case("hit", turns)], {"hit": [expected]}, granularity)
                self.assert_counts(report, 1, 1, 0, 0)
                self.assertEqual(report["retrieval_recall_at_5"], 1.0)
                self.assertEqual(report["samples"][0]["expected_ids"], [expected])
                self.assertEqual(report["samples"][0]["scoring_status"], "scored")

    def test_empty_dataset_has_explicit_empty_status(self):
        report = self.run_cases([])
        self.assert_counts(report, 0, 0, 0, 0)
        self.assertEqual(report["mrr"], 0.0)

    def test_turn_expected_ids_never_fall_back_to_sessions(self):
        self.assertEqual(self.ns["_expected_ids"](self.case("skip"), granularity="turn"), set())

    def test_aggregate_mixed_preserves_skips_and_excludes_them_from_failures(self):
        report = self.run_cases([self.case("skip"), self.case("hit", ["t"]), self.case("miss", ["t"])],
                                {"hit": ["t"], "miss": []})
        result = self.aggregate([report])
        self.assert_counts(result, 3, 2, 1, 1)
        self.assertEqual(result["retrieval_recall_at_5"], 0.5)
        self.assertEqual(result["mrr"], 0.5)
        self.assertEqual(result["rank_histogram"], {0: 1, 1: 1})
        self.assertEqual([x["case_id"] for x in result["failure_examples"]], ["miss"])
        self.assertEqual(result["unscorable_samples"], [report["samples"][0]])

    def test_aggregate_all_unscorable_is_successful_zero_scored_report(self):
        report = self.run_cases([self.case("skip")])
        result = self.aggregate([report])
        self.assertTrue(result["ok"])
        self.assert_counts(result, 1, 0, 1, 0)
        for key in ("retrieval_recall_at_1", "retrieval_recall_at_5", "retrieval_recall_at_10",
                    "recall_any_at_5", "recall_all_at_5", "ndcg_at_5", "mrr", "latency_ms_avg", "latency_ms_p95"):
            self.assertEqual(result[key], 0.0)
        self.assertEqual(result["rank_histogram"], {})
        self.assertEqual(result["failure_examples"], [])

    def test_aggregate_multiple_workers_weights_only_scored_cases(self):
        skipped = self.run_cases([self.case("skip", question_type="missing-only")])
        scored = self.run_cases([self.case("hit", ["t"]), self.case("miss", ["t"])],
                                {"hit": ["t"], "miss": []})
        result = self.aggregate([skipped, scored])
        self.assert_counts(result, 3, 2, 1, 1)
        self.assertEqual(result["retrieval_recall_at_5"], 0.5)
        self.assertEqual(result["mrr"], 0.5)
        self.assertEqual(result["chunks_processed"], 2)
        self.assertEqual(result["chunks_failed"], 0)

    def test_snapshot_summary_preserves_all_unscorable_classification(self):
        report = self.run_cases([self.case("skip")])
        record = SimpleNamespace(content={"report": report}, record_id="synthetic", title="test", meta={}, time={})
        summary = self.ns["_longmemeval_summary"](record)
        self.assert_counts(summary, 1, 0, 1, 0)
        self.assertEqual(summary["retrieval_recall_at_5"], 0.0)
        self.assertNotIn("samples", summary)

    def test_snapshot_summary_legacy_shape_stays_unchanged(self):
        report = {"name": "legacy", "sample_count": 2, "retrieval_recall_at_5": 0.5}
        record = SimpleNamespace(content={"report": report}, record_id="synthetic", title="test", meta={}, time={})
        summary = self.ns["_longmemeval_summary"](record)
        self.assertEqual(summary, {
            "record_id": "synthetic", "name": "legacy", "mode": "", "granularity": "",
            "sample_count": 2, "retrieval_recall_at_1": 0.0, "retrieval_recall_at_5": 0.5,
            "retrieval_recall_at_10": 0.0, "mrr": 0.0, "latency_ms_p95": 0.0, "time": {},
        })

    def test_aggregate_legacy_scored_samples_keep_existing_behavior(self):
        report = self.run_cases([self.case("hit", ["t"])], {"hit": ["t"]})
        del report["samples"][0]["scoring_status"]
        result = self.aggregate([report])
        self.assert_counts(result, 1, 1, 0, 0)
        self.assertEqual(result["retrieval_recall_at_5"], 1.0)
        self.assertEqual(result["mrr"], 1.0)


if __name__ == "__main__":
    unittest.main()
