"""Tests for the 3-sigma anomaly detector on per-action-class hourly counts.

Mirrors `eimemory/governance/safety/anomaly.py`. RED-GREEN TDD per
`docs/superpowers/plans/2026-06-17-eimemory-karpathy-loop.md` Task 0.6.
"""
from __future__ import annotations

import tempfile
from pathlib import Path

from eimemory.governance.safety.anomaly import AnomalyDetector


def test_anomaly_baseline_clean():
    """A normal-count observation against a stable baseline must NOT trigger."""
    with tempfile.TemporaryDirectory() as tmp:
        det = AnomalyDetector(root=Path(tmp), window_days=7)
        # 7 days * 24 hours of 10 actions per hour (synthetic baseline)
        for day in range(7):
            for hour in range(24):
                det.record("intent_pattern_upsert", 10, day=day, hour=hour)
        # Today's count at hour 12 is normal (10) — should not trigger
        det.record("intent_pattern_upsert", 10, day=7, hour=12)
        assert det.check("intent_pattern_upsert", 10, day=7, hour=12) is False


def test_anomaly_baseline_3sigma_triggers():
    """A 10x spike against a stable baseline MUST trigger (3-sigma breach)."""
    with tempfile.TemporaryDirectory() as tmp:
        det = AnomalyDetector(root=Path(tmp), window_days=7)
        for day in range(7):
            for hour in range(24):
                det.record("intent_pattern_upsert", 10, day=day, hour=hour)
        # Today's count at hour 12 is 100 (10x) — should trigger
        triggered = det.check("intent_pattern_upsert", 100, day=7, hour=12)
        assert triggered is True