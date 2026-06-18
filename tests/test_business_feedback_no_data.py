"""No-data contract tests for ``compute_business_impact`` (R9, Bug E).

The previous version of ``compute_business_impact`` returned
``delta_vs_baseline = -0.6`` when the rolling window contained no
``recall_view`` records. That **fabricated a regression** out of a
silent week and tripped the auto-rollback gate on healthy systems.

The fix returns ``avg_hit_at_1=None`` and ``delta_vs_baseline=None``
with ``no_data=True`` when there are no samples in the window, so
downstream callers can short-circuit instead of treating the empty
case as a regression. These tests pin the new contract:

1. Empty records -> ``no_data=True`` and numeric fields are ``None``.
2. Real records -> numeric ``avg_hit_at_1`` and ``delta_vs_baseline``,
   ``no_data=False``.
3. Records with ``hit_at_1=0.0`` are **not** no_data; they are real
   measurements of zero performance. The fix must distinguish
   "no data" from "data says 0".
4. The auto-rollback caller treats ``no_data=True`` as inconclusive
   and skips the rollback.
"""
from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

from eimemory.autonomous.business_feedback import (
    BASELINE_HIT_AT_1,
    compute_business_impact,
)


# ---------- helpers ----------


def _write_records(records_path: Path, rows: list[dict]) -> None:
    """Write ``rows`` (list of dicts) as JSONL to ``records_path``."""
    records_path.write_text(
        "\n".join(json.dumps(row) for row in rows),
        encoding="utf-8",
    )


def _make_recall_view(hit_at_1: float | None, *, occurred_at: str) -> dict:
    """Build a single ``recall_view`` record with the given hit@1."""
    return {
        "kind": "recall_view",
        "record_id": f"r-{occurred_at}-{hit_at_1}",
        "content": {"hit_at_1": hit_at_1},
        "time": {"occurred_at": occurred_at},
    }


def _auto_rollback_gate(impact: dict) -> str:
    """Re-implementation of the auto-rollback gate that consumes the
    impact dict. Mirrors the logic that would live in
    ``eimemory.autonomous.business_feedback`` (no production caller
    exists yet, so this is the canonical contract for the next
    caller to follow).

    Returns one of:

    * ``"skip"``     — inconclusive (``no_data``); do not touch state.
    * ``"rollback"`` — measured regression; roll back the candidate.
    * ``"keep"``     — measured improvement or neutral; keep state.
    """
    if impact.get("no_data"):
        return "skip"
    delta = impact.get("delta_vs_baseline")
    if delta is None:
        return "skip"
    # Mirror the seven_day_review style threshold: a -0.05 absolute
    # delta is the rollback trigger; otherwise keep.
    if delta <= -0.05:
        return "rollback"
    return "keep"


# ---------- the four required tests ----------


def test_empty_records_returns_no_data_flag() -> None:
    """No samples in the window -> avg_hit_at_1=None, no_data=True."""
    with tempfile.TemporaryDirectory() as tmp:
        records = Path(tmp) / "records.jsonl"
        records.write_text("", encoding="utf-8")  # no rows at all

        impact = compute_business_impact(records_path=records, days=7)

        assert impact["no_data"] is True
        assert impact["avg_hit_at_1"] is None
        assert impact["delta_vs_baseline"] is None
        assert impact["n_recall_view"] == 0
        assert impact["days"] == 7
        # baseline is echoed so callers do not have to re-import the
        # module-level constant.
        assert impact["baseline"] == BASELINE_HIT_AT_1


def test_real_records_returns_numeric_metrics() -> None:
    """A window with samples -> numeric avg_hit_at_1 and delta."""
    with tempfile.TemporaryDirectory() as tmp:
        records = Path(tmp) / "records.jsonl"
        rows = [
            _make_recall_view(0.70, occurred_at="2026-06-15T10:00:00+08:00"),
            _make_recall_view(0.65, occurred_at="2026-06-16T10:00:00+08:00"),
            _make_recall_view(0.75, occurred_at="2026-06-17T10:00:00+08:00"),
        ]
        _write_records(records, rows)

        impact = compute_business_impact(records_path=records, days=7)

        assert impact["no_data"] is False
        assert impact["n_recall_view"] == 3
        assert impact["avg_hit_at_1"] == pytest.approx(0.70, abs=1e-4)
        # 0.70 - 0.60 = +0.10
        assert impact["delta_vs_baseline"] == pytest.approx(0.10, abs=1e-4)
        assert impact["baseline"] == BASELINE_HIT_AT_1


def test_zero_hit_records_distinguished_from_empty() -> None:
    """Five records of hit@1=0.0 are NOT no_data — they are real measurements of 0.

    This is the regression that matters: a system that legitimately
    scored 0 on five recall views should report ``avg_hit_at_1=0.0``
    and ``delta_vs_baseline=-0.6`` (a real regression), not
    ``no_data=True``. The empty case (zero records) is the only
    path that returns ``no_data=True``.
    """
    with tempfile.TemporaryDirectory() as tmp:
        records = Path(tmp) / "records.jsonl"
        rows = [
            _make_recall_view(0.0, occurred_at=f"2026-06-{15 + i}T10:00:00+08:00")
            for i in range(5)
        ]
        _write_records(records, rows)

        impact = compute_business_impact(records_path=records, days=7)

        assert impact["no_data"] is False
        assert impact["n_recall_view"] == 5
        assert impact["avg_hit_at_1"] == 0.0
        # 0.0 - 0.6 = -0.6 — a *real* regression, not a fabricated one.
        assert impact["delta_vs_baseline"] == pytest.approx(-0.6, abs=1e-4)

        # And the auto-rollback gate must trigger on the real regression.
        assert _auto_rollback_gate(impact) == "rollback"


def test_auto_rollback_skips_on_no_data() -> None:
    """An empty window -> auto-rollback gate must skip, not rollback.

    The previous implementation would have returned
    ``delta_vs_baseline = -0.6`` and the gate would have rolled back
    a healthy system on a quiet week. The fix must let the gate see
    ``no_data`` and short-circuit.
    """
    with tempfile.TemporaryDirectory() as tmp:
        records = Path(tmp) / "records.jsonl"
        records.write_text("", encoding="utf-8")

        impact = compute_business_impact(records_path=records, days=7)
        decision = _auto_rollback_gate(impact)

        assert decision == "skip", (
            "auto-rollback gate should skip when no_data is True; "
            "a silent week is not evidence of a regression. "
            f"Got decision={decision!r} impact={impact!r}"
        )


# ---------- additional sanity tests for the no_data path ----------


def test_no_data_path_also_handles_old_records_outside_window() -> None:
    """A window where every record is older than ``days`` -> no_data=True.

    This is the case the old code accidentally turned into a -0.6
    regression: the records exist, but they are outside the rolling
    window. The result should be no_data, not a regression.
    """
    with tempfile.TemporaryDirectory() as tmp:
        records = Path(tmp) / "records.jsonl"
        rows = [
            _make_recall_view(0.99, occurred_at="2020-01-01T00:00:00+00:00"),
            _make_recall_view(0.99, occurred_at="2020-06-01T00:00:00+00:00"),
        ]
        _write_records(records, rows)

        impact = compute_business_impact(records_path=records, days=7)

        assert impact["no_data"] is True
        assert impact["avg_hit_at_1"] is None
        assert impact["delta_vs_baseline"] is None
        assert impact["n_recall_view"] == 0


def test_no_data_path_handles_malformed_timestamps() -> None:
    """Records with invalid timestamps must not poison the sample set.

    Malformed rows are silently skipped (existing behaviour). If
    every record is malformed, the result is no_data, not a
    regression.
    """
    with tempfile.TemporaryDirectory() as tmp:
        records = Path(tmp) / "records.jsonl"
        rows = [
            {"kind": "recall_view", "record_id": "bad-1",
             "content": {"hit_at_1": 0.5},
             "time": {"occurred_at": "not-a-date"}},
            {"kind": "recall_view", "record_id": "bad-2",
             "content": {"hit_at_1": 0.7},
             "time": {}},
        ]
        _write_records(records, rows)

        impact = compute_business_impact(records_path=records, days=7)

        assert impact["no_data"] is True
        assert impact["n_recall_view"] == 0