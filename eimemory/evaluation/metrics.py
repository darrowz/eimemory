from __future__ import annotations

import math
from statistics import mean


def _round(value: float) -> float:
    return round(float(value), 3)


def recall_at_k(returned_ids: list[str], expected_ids: set[str], *, k: int) -> float:
    if not expected_ids:
        return 1.0 if returned_ids else 0.0
    top = returned_ids[: max(0, int(k))]
    hits = len({item for item in top if item in expected_ids})
    return _round(hits / len(expected_ids))


def precision_at_k(returned_ids: list[str], expected_ids: set[str], *, k: int) -> float:
    top = returned_ids[: max(0, int(k))]
    if not top:
        return 0.0
    hits = len([item for item in top if item in expected_ids])
    return _round(hits / len(top))


def mean_reciprocal_rank(ranks: list[int]) -> float:
    if not ranks:
        return 0.0
    values = [(1.0 / rank) if rank > 0 else 0.0 for rank in ranks]
    return _round(mean(values))


def binary_pass_rate(values: list[bool]) -> float:
    if not values:
        return 0.0
    return _round(sum(1 for item in values if item) / len(values))


def ndcg_at_k(returned_ids: list[str], expected_ids: set[str], *, k: int) -> float:
    top = returned_ids[: max(0, int(k))]
    if not top or not expected_ids:
        return 0.0
    dcg = 0.0
    for index, item in enumerate(top, start=1):
        if item in expected_ids:
            dcg += 1.0 / math.log2(index + 1)
    ideal_hits = min(len(expected_ids), len(top))
    idcg = sum(1.0 / math.log2(index + 1) for index in range(1, ideal_hits + 1))
    return _round(dcg / idcg if idcg else 0.0)


def percentile(values: list[float], pct: int) -> float:
    if not values:
        return 0.0
    ordered = sorted(float(item) for item in values)
    index = math.ceil((max(0, min(100, int(pct))) / 100.0) * len(ordered)) - 1
    return _round(ordered[max(0, min(index, len(ordered) - 1))])
