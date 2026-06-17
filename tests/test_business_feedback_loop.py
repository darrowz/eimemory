"""Tests for the business feedback loop.

Phase 3 acceptance gate per the karpathy-loop plan
(`docs/superpowers/plans/2026-06-17-eimemory-karpathy-loop.md` Task 3.3):

  - `compute_business_impact` reads `recall_view` records from the
    records.jsonl log and returns `avg_hit_at_1` plus `delta_vs_baseline`
    over a sliding window of N days.
  - The baseline used is the 2026-06-17 evidence value (0.60).
"""
from __future__ import annotations

import json
import tempfile
from pathlib import Path

from eimemory.autonomous.business_feedback import compute_business_impact


def test_compute_business_impact_from_real_metrics():
    with tempfile.TemporaryDirectory() as tmp:
        records = Path(tmp) / "records.jsonl"
        rows = [
            {"kind": "recall_view", "record_id": f"r{i}",
             "content": {"hit_at_1": 0.6 + i * 0.01},
             "time": {"occurred_at": "2026-06-15T10:00:00+08:00"}}
            for i in range(7)
        ]
        records.write_text("\n".join(json.dumps(r) for r in rows))
        impact = compute_business_impact(records_path=records, days=7)
        assert 0.6 < impact["avg_hit_at_1"] < 0.7
        assert impact["delta_vs_baseline"] != 0
