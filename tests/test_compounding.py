"""Tests for the compounding context builder (Karpathy Loop Task 2.6).

Covers two responsibilities from the 2026-06-17 plan:

1. ``load_recent_kept`` filters the experiment log to the most recent
   ``kept=True`` rows so the next loop iteration can reuse prior wins.
2. ``format_as_context`` renders those rows as a markdown block the loop
   can paste into the next hypothesis prompt, with a hard 2 KB byte cap
   to keep the context window small. The cap must be a tunable
   parameter (the plan note: "Configurable but not a comment").
"""
from __future__ import annotations

import json
import tempfile
from pathlib import Path

from eimemory.autonomous.compounding import (
    DEFAULT_CONTEXT_MAX_BYTES,
    DEFAULT_RECENT_KEPT,
    format_as_context,
    load_recent_kept,
)


def test_load_recent_kept_filters_to_kept_rows() -> None:
    """``load_recent_kept`` returns only ``kept=True`` rows, last N first."""
    with tempfile.TemporaryDirectory() as tmp:
        log = Path(tmp) / "exp.jsonl"
        rows = [
            {
                "hypothesis": "h1",
                "kept": True,
                "elapsed": 10.0,
                "primary_metric_before": 0.6,
                "primary_metric_after": 0.65,
                "timestamp": "2026-06-15T10:00:00Z",
            },
            {
                "hypothesis": "h2",
                "kept": False,
                "elapsed": 10.0,
                "primary_metric_before": 0.6,
                "primary_metric_after": 0.59,
                "timestamp": "2026-06-15T11:00:00Z",
            },
            {
                "hypothesis": "h3",
                "kept": True,
                "elapsed": 10.0,
                "primary_metric_before": 0.65,
                "primary_metric_after": 0.7,
                "timestamp": "2026-06-16T10:00:00Z",
            },
        ]
        log.write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")
        kept = load_recent_kept(log, n=5)
        assert len(kept) == 2
        # The last kept row returned is the most recent.
        assert kept[-1]["hypothesis"] == "h3"
        assert all(r["kept"] for r in kept)


def test_load_recent_kept_accepts_outcome_kept_without_legacy_flag() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        log = Path(tmp) / "exp.jsonl"
        rows = [
            {
                "hypothesis": "kept by outcome",
                "outcome": "kept",
                "duration_seconds": 1.0,
                "baseline_value": 0.5,
                "candidate_value": 0.6,
                "timestamp": "2026-06-18T10:00:00Z",
            },
            {
                "hypothesis": "discarded by outcome",
                "outcome": "discarded",
                "duration_seconds": 1.0,
                "baseline_value": 0.5,
                "candidate_value": 0.4,
                "timestamp": "2026-06-18T11:00:00Z",
            },
        ]
        log.write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")

        kept = load_recent_kept(log, n=5)

        assert [row["hypothesis"] for row in kept] == ["kept by outcome"]


def test_load_recent_kept_caps_at_n() -> None:
    """``load_recent_kept`` returns at most ``n`` rows even when more are kept."""
    with tempfile.TemporaryDirectory() as tmp:
        log = Path(tmp) / "exp.jsonl"
        rows = [
            {
                "hypothesis": f"h{i}",
                "kept": True,
                "elapsed": 1.0,
                "primary_metric_before": 0.5,
                "primary_metric_after": 0.5 + i * 0.01,
                "timestamp": f"2026-06-15T{i:02d}:00:00Z",
            }
            for i in range(8)
        ]
        log.write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")
        kept = load_recent_kept(log, n=3)
        assert len(kept) == 3
        # The last three kept rows, in chronological order, are h5, h6, h7.
        assert [r["hypothesis"] for r in kept] == ["h5", "h6", "h7"]


def test_load_recent_kept_missing_file_returns_empty() -> None:
    """A missing log file is a cold start: returns ``[]`` instead of raising."""
    with tempfile.TemporaryDirectory() as tmp:
        kept = load_recent_kept(Path(tmp) / "does_not_exist.jsonl", n=5)
        assert kept == []


def test_format_as_context_includes_metrics() -> None:
    """``format_as_context`` surfaces hypothesis and metric delta."""
    rows = [
        {
            "hypothesis": "x",
            "kept": True,
            "elapsed": 10.0,
            "primary_metric_before": 0.6,
            "primary_metric_after": 0.7,
            "timestamp": "2026-06-15",
        }
    ]
    ctx = format_as_context(rows)
    assert "x" in ctx
    assert "0.6" in ctx and "0.7" in ctx


def test_format_as_context_empty_rows() -> None:
    """An empty input returns the documented empty-state marker."""
    assert format_as_context([]) == "(no prior kept experiments)"


def test_format_as_context_caps_at_max_bytes() -> None:
    """The 2 KB cap from the plan note is enforced and is a real parameter.

    The cap is a function-level knob, not a comment — a caller can
    override it (e.g. to debug), and the default is the plan value
    (``DEFAULT_CONTEXT_MAX_BYTES``).
    """
    long_hypothesis = "x" * 200  # each row will be ~250 bytes
    rows = [
        {
            "hypothesis": long_hypothesis,
            "kept": True,
            "elapsed": 10.0,
            "primary_metric_before": 0.6,
            "primary_metric_after": 0.7,
            "timestamp": "2026-06-15T10:00:00Z",
        }
        for _ in range(20)
    ]

    # Default cap (2 KB) is much smaller than the 20-row context, so the
    # returned string must be truncated and still under the cap.
    ctx_default = format_as_context(rows)
    assert len(ctx_default.encode("utf-8")) <= DEFAULT_CONTEXT_MAX_BYTES
    assert len(ctx_default) < len(format_as_context(rows, max_bytes=10_000))

    # An explicit cap is honoured.
    ctx_small = format_as_context(rows, max_bytes=128)
    assert len(ctx_small.encode("utf-8")) <= 128


def test_module_exposes_plan_defaults() -> None:
    """The plan's default values are exposed as module constants."""
    assert DEFAULT_RECENT_KEPT == 5
    assert DEFAULT_CONTEXT_MAX_BYTES == 2 * 1024
