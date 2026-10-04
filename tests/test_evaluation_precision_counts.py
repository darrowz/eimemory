"""Synthetic precision counting and aggregation cases; no runtime data."""
from types import SimpleNamespace

from eimemory.evaluation.benchmarks import _matching_ranks, _rank_metrics
from eimemory.evaluation.framework import _mean_precision


def test_case_precision_counts_every_matching_record():
    records = [SimpleNamespace(record_id=value) for value in ("a", "b")]
    ranks = _matching_ranks(returned=records, expected_record_ids=["a", "b"],
        expected_titles=[], expected_kinds=[], expected_text=[], expected_current_text=[])
    assert ranks == [1, 2]
    result = _rank_metrics(expected_rank=ranks[0], returned_count=2, limit=5,
        expected_present=True, matched_count=len(ranks))
    assert result["precision_at_k"] == 1.0
    assert result["mrr"] == 1.0


def test_partial_case_precision_keeps_rank_and_hit_count_distinct():
    result = _rank_metrics(expected_rank=2, returned_count=5, limit=5,
        expected_present=True, matched_count=2)
    assert result["precision_at_k"] == 0.4
    assert result["mrr"] == 0.5


def test_framework_precision_includes_reported_invalid_case_zero():
    assert _mean_precision([{"precision_at_k": 1.0}, {"precision_at_k": 0.0}]) == 0.5


def test_framework_precision_excludes_only_inapplicable_cases():
    assert _mean_precision([{"precision_at_k": 0.5}, {"precision_at_k": None}]) == 0.5
    assert _mean_precision([]) == 0.0
