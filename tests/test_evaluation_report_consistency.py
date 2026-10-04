"""Synthetic report aggregation, ranking, and plain text expectations."""
from types import SimpleNamespace

import pytest

from eimemory.evaluation import actionable_memory, regression_replay
from eimemory.evaluation.task_replay import _meets_threshold
from eimemory.models.records import ScopeRef


@pytest.mark.parametrize("query_type", [None, " Project ", "PROJECT", "project"])
def test_project_aggregation_uses_normalized_sample_type(monkeypatch, query_type):
    monkeypatch.setattr(actionable_memory, "_seed_records", lambda *args, **kwargs: ([], []))
    monkeypatch.setattr(actionable_memory, "_case_record", lambda *args, **kwargs: None)
    monkeypatch.setattr(actionable_memory, "_run_case", lambda **kwargs: {
        "query_type": "project", "passed": False, "contamination_detected": True})
    case = {"query": "A project query"}
    if query_type is not None:
        case["query_type"] = query_type
    report = actionable_memory._run_actionable_memory_eval_on_runtime(None,
        normalized={"scope": {}, "name": "synthetic"}, dataset_scope=ScopeRef(), seed=[], cases=[case])
    assert report["project_query_contamination_rate"] == 1.0


def test_kind_filter_keeps_original_return_positions():
    rows = [SimpleNamespace(kind="knowledge_page"), SimpleNamespace(kind="memory")]
    assert actionable_memory._ranked_records_for_eval(rows, expected_kinds={"memory"}) == [(2, rows[1])]
    assert actionable_memory._ranked_records_for_eval(rows, expected_kinds={"missing"}) == list(enumerate(rows, 1))


@pytest.mark.parametrize("expected", [[], "", None])
def test_missing_regression_expectations_are_not_success(expected):
    report = regression_replay.run_regression_replay([{"id": "case", "expected_text": expected}], {})
    assert report["verdict"] == "fail"
    assert report["samples"][0]["failure_reason"] == "expectations_missing"


def test_nonempty_regression_expectation_can_pass():
    report = regression_replay.run_regression_replay([{"id": "case", "expected_text": ["result"]}], {"case": "A result"})
    assert report["verdict"] == "pass"


@pytest.mark.parametrize("passed,total,threshold,expected", [
    (2499, 2500, 1.0, False), (2500, 2500, 1.0, True),
    (4, 5, 0.8004, False), (4, 5, 0.8, True), (0, 0, 0.0, True),
])
def test_replay_verdict_uses_unrounded_ratio(passed, total, threshold, expected):
    assert _meets_threshold(passed, total, threshold) is expected
