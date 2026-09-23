"""Production real-query evaluation engine surface (A2).

``real_query_schema`` holds pure constants/digests; ``real_query_baseline``
holds bootstrap/high-water entrypoints; this module is the evaluation engine
import path. Concrete implementations remain in ``real_query_gate`` during the
incremental split.
"""
from __future__ import annotations

from eimemory.evaluation.real_query_gate import (  # noqa: F401
    run_real_query_gate,
    freeze_production_recall_dataset,
    evaluate_labeled_ranking_at_5,
)

__all__ = [
    "run_real_query_gate",
    "freeze_production_recall_dataset",
    "evaluate_labeled_ranking_at_5",
]
