"""Production real-query baseline / high-water / bootstrap surface (A2).

Implementation currently lives in ``real_query_gate``; this module is the
stable import path for baseline-oriented callers so schema/engine can evolve
without recreating import cycles.
"""
from __future__ import annotations

from eimemory.evaluation.real_query_gate import (  # noqa: F401
    bootstrap_production_recall_baseline,
    record_production_recall_bootstrap_pending,
    verify_current_bootstrap_data_pending,
    activate_production_recall_strict_state,
    verify_current_production_recall_strict_state,
)

__all__ = [
    "bootstrap_production_recall_baseline",
    "record_production_recall_bootstrap_pending",
    "verify_current_bootstrap_data_pending",
    "activate_production_recall_strict_state",
    "verify_current_production_recall_strict_state",
]
