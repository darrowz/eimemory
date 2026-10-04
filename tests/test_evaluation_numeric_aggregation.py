"""Pure synthetic metric aggregation cases; no runtime or external I/O."""
import pytest

from eimemory.evaluation.benchmarks import _phase_scores, _passes_threshold
from eimemory.evaluation.reward import RewardEngine
from eimemory.evaluation.contracts import _clamp_float


def test_phase_precision_keeps_per_case_returned_count():
    sample = {"phase": "usage", "passed": True, "expected_rank": 1,
              "mrr": 1.0, "recall_at_k": 1.0, "precision_at_k": 0.2}
    assert _phase_scores([sample])["usage"]["precision_at_k"] == 0.2


def test_phase_averages_include_misses_and_successful_empty_cases():
    samples = [
        {"phase": "usage", "passed": True, "expected_rank": 0,
         "mrr": 1.0, "recall_at_k": 1.0, "precision_at_k": 1.0},
        {"phase": "usage", "passed": False, "expected_rank": 0,
         "mrr": 0.0, "recall_at_k": 0.0, "precision_at_k": 0.0},
    ]
    result = _phase_scores(samples)["usage"]
    assert result["mrr"] == result["recall_at_k"] == result["precision_at_k"] == 0.5
    assert result["sample_count"] == 2
    assert result["pass_rate"] == 0.5


def test_empty_phase_has_zero_metric_averages():
    result = _phase_scores([])["usage"]
    assert result["mrr"] == result["recall_at_k"] == result["precision_at_k"] == 0.0


@pytest.mark.parametrize("outcome, expected_cost", [
    ({"cost": 0}, 0.0), ({"cost": 0.0}, 0.0), ({"cost": 0.25}, -0.25),
    ({"cost": None}, -0.75), ({}, -0.75),
])
def test_explicit_zero_outcome_cost_does_not_use_experience_cost(outcome, expected_cost):
    result = RewardEngine().compute({"cost": 0.75}, {"ok": True}, outcome)
    assert result["components"]["cost"] == expected_cost


@pytest.mark.parametrize("passed,total,threshold,expected", [
    (2499, 2500, 1.0, False), (2500, 2500, 1.0, True),
    (1999, 2500, 0.8, False), (2000, 2500, 0.8, True),
    (0, 0, 0.0, True), (0, 0, 0.8, False),
    (1, 2, 0.0, False), (2, 2, 0.0, True),
])
def test_threshold_uses_unrounded_ratio(passed, total, threshold, expected):
    assert _passes_threshold(passed, total, threshold) is expected


def test_threshold_normalization_keeps_requested_precision():
    threshold = _clamp_float(0.8004, default=0.8)
    assert threshold == 0.8004
    assert _passes_threshold(4, 5, threshold) is False
