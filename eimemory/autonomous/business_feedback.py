"""Compute real business impact from recall_view + capability_score records.

The previous version of this module returned ``delta_vs_baseline =
-0.6`` when there were no recall samples in the window. That
**fabricated a regression** out of thin air: a silent, sparse window
should be treated as **no data**, not as a measurement. Downstream
callers (auto-rollback, nightly report, seven-day review) that use
this metric must treat the no-data state as neutral/inconclusive
rather than as evidence of a regression.

The new contract:

* **No samples in the window** → ``avg_hit_at_1`` and
  ``delta_vs_baseline`` are ``None``; ``no_data`` is ``True``.
* **At least one sample** → numeric values (rounded to 4 dp) and
  ``no_data`` is ``False``. ``n_recall_view`` is the sample count.

Callers that previously interpreted a negative delta as a regression
must now short-circuit on ``no_data``.
"""
from __future__ import annotations

from pathlib import Path
from datetime import datetime, timedelta, timezone
from statistics import mean

from eimemory.storage.jsonl import iter_jsonl_payloads

BASELINE_HIT_AT_1 = 0.60  # from 2026-06-17 evidence


def _iter(records_path: Path, kinds: list[str], days: int):
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    for r in iter_jsonl_payloads(records_path):
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
    """Average hit@1 and capability_score.evidence over last N days.

    Returns a dict with the following shape:

    * ``avg_hit_at_1`` — mean of observed hit@1 in the window, or
      ``None`` when no samples were observed (``no_data`` is then
      ``True``).
    * ``delta_vs_baseline`` — ``avg_hit_at_1 - BASELINE_HIT_AT_1``,
      or ``None`` when no samples were observed.
    * ``n_recall_view`` — number of ``recall_view`` records with a
      non-null ``hit_at_1`` in the window.
    * ``days`` — the window size used for the query.
    * ``no_data`` — ``True`` iff ``n_recall_view == 0``. Downstream
      callers (auto-rollback, nightly report, seven-day review) must
      treat ``no_data=True`` as **inconclusive**, not as a
      regression.
    * ``baseline`` — the constant baseline (``BASELINE_HIT_AT_1``)
      used for the delta computation, echoed so callers do not have
      to re-import the constant.
    """
    hits = []
    for r in _iter(records_path, ["recall_view"], days):
        h = (r.get("content", {}) or {}).get("hit_at_1")
        if h is not None:
            hits.append(float(h))
    n_recall_view = len(hits)
    if n_recall_view == 0:
        # No data in the window. Do NOT synthesize a regression by
        # returning a numeric delta; the previous implementation
        # returned -0.6, which caused the auto-rollback gate and the
        # nightly report to fire on a silent week.
        return {
            "avg_hit_at_1": None,
            "delta_vs_baseline": None,
            "n_recall_view": 0,
            "days": days,
            "no_data": True,
            "baseline": BASELINE_HIT_AT_1,
        }
    avg_hit = mean(hits)
    return {
        "avg_hit_at_1": round(avg_hit, 4),
        "delta_vs_baseline": round(avg_hit - BASELINE_HIT_AT_1, 4),
        "n_recall_view": n_recall_view,
        "days": days,
        "no_data": False,
        "baseline": BASELINE_HIT_AT_1,
    }
