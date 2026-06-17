"""Compute real business impact from recall_view + capability_score records."""
from __future__ import annotations

import json
from pathlib import Path
from datetime import datetime, timedelta, timezone
from statistics import mean

BASELINE_HIT_AT_1 = 0.60  # from 2026-06-17 evidence


def _iter(records_path: Path, kinds: list[str], days: int):
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    with open(records_path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            if r.get("kind") not in kinds:
                continue
            occurred = r.get("time", {}).get("occurred_at", "")
            try:
                t = datetime.fromisoformat(occurred.replace("Z", "+00:00"))
            except ValueError:
                continue
            if t < cutoff:
                continue
            yield r


def compute_business_impact(
    records_path: Path = Path("/var/lib/eimemory/records.jsonl"),
    days: int = 7,
) -> dict:
    """Average hit@1 and capability_score.evidence over last N days."""
    hits = []
    for r in _iter(records_path, ["recall_view"], days):
        h = (r.get("content", {}) or {}).get("hit_at_1")
        if h is not None:
            hits.append(float(h))
    avg_hit = mean(hits) if hits else 0.0
    return {
        "avg_hit_at_1": avg_hit,
        "delta_vs_baseline": avg_hit - BASELINE_HIT_AT_1,
        "n_recall_view": len(hits),
        "days": days,
    }
