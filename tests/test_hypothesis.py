"""Tests for hypothesis generation from weakness/incident clustering.

Task 2.4 of the Karpathy Loop plan
(`docs/superpowers/plans/2026-06-17-eimemory-karpathy-loop.md`) replaces
the boilerplate 12-line `AUTONOMOUS_LEARNING_CANDIDATE` template with a
real hypothesis generator that reads recent `weakness` + `incident`
records, buckets them by failure keyword, and emits a concrete
hypothesis per bucket. The two tests below cover the two contracts:

  1. The number of emitted hypotheses is bounded by `max_n` (default 50).
  2. The emitted hypotheses must NOT look like the old template — they
     must reference the actual bucket keyword and the actual record
     count, not boilerplate strings.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path


def _write_records(records_path: Path, rows: list[dict]) -> None:
    """Write a JSONL fixture of weakness/incident rows."""
    records_path.parent.mkdir(parents=True, exist_ok=True)
    with records_path.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")


def _recent_occurred_at() -> str:
    """Return a timestamp inside the 7-day window the generator uses."""
    return (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()


def test_generates_at_most_50_hypotheses(tmp_path: Path):
    """200 weakness rows clustered into buckets should yield 1..50 hypotheses."""
    from eimemory.autonomous.hypothesis import generate_hypotheses_from_weaknesses

    records = tmp_path / "records.jsonl"
    rows: list[dict] = []
    for i in range(200):
        summary = f"timeout {i}" if i % 2 == 0 else f"crash {i}"
        rows.append(
            {
                "kind": "weakness",
                "record_id": f"w{i}",
                "content": {"summary": summary},
                "time": {"occurred_at": _recent_occurred_at()},
            }
        )
    _write_records(records, rows)

    hyps = generate_hypotheses_from_weaknesses(records_path=records, max_n=50)

    assert 1 <= len(hyps) <= 50
    # With 200 records split between 'timeout' and 'crash' buckets, the
    # returned list must contain both — that's the whole point of the
    # clustering step.
    joined = " | ".join(hyps)
    assert "timeout" in joined
    assert "crash" in joined


def test_hypothesis_is_non_template(tmp_path: Path):
    """A real hypothesis should not be the 12-line boilerplate template."""
    from eimemory.autonomous.hypothesis import generate_hypotheses_from_weaknesses

    records = tmp_path / "records.jsonl"
    rows = [
        {
            "kind": "weakness",
            "record_id": "w1",
            "content": {"summary": "openclaw LLM idle timeout 120s"},
            "time": {"occurred_at": _recent_occurred_at()},
        },
    ]
    _write_records(records, rows)

    hyps = generate_hypotheses_from_weaknesses(records_path=records)

    assert len(hyps) >= 1
    # Old template boilerplate markers must not appear.
    for h in hyps:
        assert "cybersecurity risk" not in h, f"template leak: {h!r}"
        assert "AUTONOMOUS_LEARNING_CANDIDATE" not in h, f"template leak: {h!r}"
    # The hypothesis must reference the actual bucket keyword and a count.
    assert any("timeout" in h for h in hyps)
    assert any("1" in h for h in hyps)


def test_empty_records_returns_baseline_hypothesis(tmp_path: Path):
    """If there is nothing in the last 7 days, return a baseline marker."""
    from eimemory.autonomous.hypothesis import generate_hypotheses_from_weaknesses

    records = tmp_path / "records.jsonl"
    records.write_text("", encoding="utf-8")  # empty

    hyps = generate_hypotheses_from_weaknesses(records_path=records)

    assert hyps == ["baseline: no recent weakness/incident in 7d"]


def test_buckets_are_keyword_based(tmp_path: Path):
    """Records with the same bucket keyword should be counted together."""
    from eimemory.autonomous.hypothesis import generate_hypotheses_from_weaknesses

    records = tmp_path / "records.jsonl"
    rows: list[dict] = []
    # 5 "permission denied" rows, 2 "rate limit" rows, 3 unrelated
    for i in range(5):
        rows.append(
            {
                "kind": "incident",
                "record_id": f"p{i}",
                "content": {"summary": f"permission denied while serving tenant {i}"},
                "time": {"occurred_at": _recent_occurred_at()},
            }
        )
    for i in range(2):
        rows.append(
            {
                "kind": "weakness",
                "record_id": f"r{i}",
                "content": {"summary": f"rate limit hit during batch {i}"},
                "time": {"occurred_at": _recent_occurred_at()},
            }
        )
    for i in range(3):
        rows.append(
            {
                "kind": "weakness",
                "record_id": f"x{i}",
                "content": {"summary": f"misc {i}"},
                "time": {"occurred_at": _recent_occurred_at()},
            }
        )
    _write_records(records, rows)

    hyps = generate_hypotheses_from_weaknesses(records_path=records)

    joined = " | ".join(hyps)
    # permission bucket must surface with the right count
    assert "permission" in joined
    # rate_limit bucket must surface
    assert "rate_limit" in joined
    # Counts must reflect the rows: 5 permission, 2 rate_limit, 3 other
    assert "appears 5 times" in joined
    assert "appears 2 times" in joined


def test_ignores_records_outside_seven_day_window(tmp_path: Path):
    """Records older than 7 days must be excluded from the buckets."""
    from eimemory.autonomous.hypothesis import generate_hypotheses_from_weaknesses

    records = tmp_path / "records.jsonl"
    rows = [
        {
            "kind": "weakness",
            "record_id": "old",
            "content": {"summary": "timeout long ago"},
            "time": {"occurred_at": "2020-01-01T10:00:00+08:00"},
        },
        {
            "kind": "weakness",
            "record_id": "fresh",
            "content": {"summary": "timeout recent"},
            "time": {"occurred_at": _recent_occurred_at()},
        },
    ]
    _write_records(records, rows)

    hyps = generate_hypotheses_from_weaknesses(records_path=records)

    # Only the fresh row should be in scope; 1 timeout, no old record.
    assert any("appears 1 times" in h for h in hyps)
    # Baseline should NOT appear because at least one record is in window.
    assert hyps != ["baseline: no recent weakness/incident in 7d"]


def test_windows_friendly_default_records_path():
    """Default records path uses the Windows-friendly E:/eimemory/... prefix."""
    from eimemory.autonomous.hypothesis import DEFAULT_RECORDS_PATH

    normalized = str(DEFAULT_RECORDS_PATH).replace("\\", "/")
    assert normalized.startswith("E:/eimemory/"), (
        f"default records path should live under E:/eimemory/, got {normalized!r}"
    )
