"""Karpathy Loop package (Phase 2 of the 2026-06-17 karpathy-loop plan).

The ``eimemory.autonomous`` package is the autoresearch loop that
extends the existing ``eimemory.governance.autonomous_learning`` with
a hard-time-boxed single-experiment runner, JSONL experiment log,
hypothesis clustering, compounding context, and a nightly cron.

See ``docs/superpowers/plans/2026-06-17-eimemory-karpathy-loop.md``
Phase 2 (Tasks 2.1-2.8) for the full design.
"""
from __future__ import annotations
