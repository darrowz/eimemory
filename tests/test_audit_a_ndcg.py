"""NDCG must penalize missing evidence and never double-count a document."""
import pytest

from eimemory.evaluation.metrics import ndcg_at_k


@pytest.mark.parametrize("returned, expected, k, score", [
    (["a"], {"a", "b"}, 5, 0.613),
    (["a", "a", "a"], {"a"}, 3, 1.0),
    (["a", "a", "b"], {"a", "b"}, 3, 0.92),
    (["a", "b"], {"a", "b"}, 5, 1.0),
    (["a", "b"], {"a", "b"}, 1, 1.0),
    (["wrong", "a"], {"a", "b"}, 5, 0.387),
    (["a"], {"a"}, 0, 0.0),
    (["a"], {"a"}, -1, 0.0),
    ([], {"a"}, 5, 0.0),
    (["a"], set(), 5, 0.0),
])
def test_ndcg_uses_requested_cutoff_and_unique_gains(returned, expected, k, score):
    assert ndcg_at_k(returned, expected, k=k) == score
