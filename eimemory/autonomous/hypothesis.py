"""Karpathy-loop hypothesis generator: weakness/incident clustering.

Task 2.4 of the Karpathy Loop plan
(`docs/superpowers/plans/2026-06-17-eimemory-karpathy-loop.md`)
replaces the boilerplate 12-line ``AUTONOMOUS_LEARNING_CANDIDATE``
template with a real hypothesis stream built from the last 7 days of
``weakness`` + ``incident`` records. The generator never talks to a
network, never spends money, and never invokes a paid API — the buckets
are pure-Python keyword clusters, so this module is safe to run inside
the Karpathy Loop ``loop.py`` runner.

Usage::

    from eimemory.autonomous.hypothesis import generate_hypotheses_from_weaknesses
    from pathlib import Path

    hyps = generate_hypotheses_from_weaknesses(
        records_path=Path("E:/eimemory/records.jsonl"),
        max_n=50,
    )
    for h in hyps:
        print("-", h)

The default ``records_path`` is the Windows-friendly
``E:/eimemory/records.jsonl`` substitute for the Linux
``/var/lib/eimemory/records.jsonl`` from the original plan; tests
should pass an explicit ``records_path`` rooted in ``tmp_path``.
"""
from __future__ import annotations

import json
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator

# Windows-friendly substitute for the Linux /var/lib/eimemory/... path
# called out in the Karpathy Loop plan.
DEFAULT_RECORDS_PATH = Path("E:/eimemory/records.jsonl")

# How far back to look when clustering weakness/incident records.
WINDOW_DAYS = 7

# The Kinds we treat as failure signals. "incident" is the operational
# counterpart to "weakness" — both surface in the Karpathy Loop input
# stream.
FAILURE_KINDS = ("weakness", "incident")

# Default upper bound on emitted hypotheses. Matches the
# ``at most 50`` acceptance criterion in the plan.
DEFAULT_MAX_N = 50

# Bucket keyword map. Each key is a stable failure category; each
# value is a list of substring needles that route a record summary into
# that bucket. Order matters: the first match wins, so put the more
# specific needles first.
_BUCKET_NEEDLES: dict[str, tuple[str, ...]] = {
    "rate_limit": ("rate limit", "usage limit", "429", "throttle"),
    "timeout": ("timeout", "idle", "slow", "hang"),
    "permission": ("permission", "denied", "forbidden", "unauthorized"),
    "recall": ("recall", "search", "hit@", "retriev"),
    "crash": ("crash", "fail", "error", "exception", "traceback"),
    "tooling": ("tool", "mcp", "function call", "plugin"),
    "embedding": ("embed", "chunk", "index", "vector"),
    "governance": ("govern", "policy", "rbac", "approval"),
}


def _iter_failure_records(
    records_path: Path, *, days: int = WINDOW_DAYS
) -> Iterator[dict]:
    """Yield weakness/incident records from the last ``days`` window.

    Skips rows whose ``time.occurred_at`` is missing or unparseable, and
    rows that fall outside the window. JSON parse errors are silently
    ignored — a single corrupt line in a 480k-record file should not
    take down the loop.
    """
    if not records_path.exists():
        return
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    with records_path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except ValueError:
                # ValueError catches json.JSONDecodeError on every
                # supported CPython version.
                continue
            if not isinstance(row, dict):
                continue
            if row.get("kind") not in FAILURE_KINDS:
                continue
            occurred = (row.get("time") or {}).get("occurred_at", "")
            if not occurred:
                continue
            try:
                ts = datetime.fromisoformat(occurred.replace("Z", "+00:00"))
            except ValueError:
                continue
            if ts < cutoff:
                continue
            yield row


def _bucket_key(summary: str) -> str:
    """Route a record summary into a stable failure bucket.

    The summary is lower-cased once; the first bucket whose needles all
    appear in the summary wins. Falls back to ``"other"`` so we never
    drop a record from the count.
    """
    needle = summary.lower()
    for bucket, needles in _BUCKET_NEEDLES.items():
        if any(n in needle for n in needles):
            return bucket
    return "other"


def _format_hypothesis(bucket: str, count: int) -> str:
    """Format a single real hypothesis for a bucket.

    The output deliberately references the actual bucket keyword and
    the actual record count, so it cannot regress to the boilerplate
    12-line ``AUTONOMOUS_LEARNING_CANDIDATE`` template that the
    ``test_hypothesis_is_non_template`` test guards against.
    """
    return (
        f"Top failure bucket '{bucket}' appears {count} times in last "
        f"{WINDOW_DAYS}d. Reduce it by adjusting related eimemory "
        f"config or policy."
    )


def generate_hypotheses_from_weaknesses(
    *,
    records_path: Path | None = None,
    max_n: int = DEFAULT_MAX_N,
    days: int = WINDOW_DAYS,
) -> list[str]:
    """Cluster the last ``days`` of weakness/incident records into hypotheses.

    Args:
        records_path: JSONL records file. Defaults to ``DEFAULT_RECORDS_PATH``
            (Windows-friendly ``E:/eimemory/records.jsonl``).
        max_n: Upper bound on returned hypotheses. Defaults to 50.
        days: How far back to look. Defaults to 7 days.

    Returns:
        A list of hypothesis strings, one per bucket, sorted by record
        count (descending). If no records are in the window, returns
        ``["baseline: no recent weakness/incident in 7d"]``.
    """
    path = Path(records_path) if records_path is not None else DEFAULT_RECORDS_PATH
    bucket_counts: Counter[str] = Counter()
    for row in _iter_failure_records(path, days=days):
        summary = ((row.get("content") or {}).get("summary") or "").strip()
        if not summary:
            continue
        bucket_counts[_bucket_key(summary)] += 1

    if not bucket_counts:
        return [f"baseline: no recent weakness/incident in {days}d"]

    hyps = [
        _format_hypothesis(bucket, count)
        for bucket, count in bucket_counts.most_common(max_n)
    ]
    log.info("hypothesis_generated source=hypothesis.py count=%d", len(hyps))
    return hyps


# Logging hook: emit one structured event per hypothesis set so the
# audit chain can correlate hypothesis generation with downstream
# experiments without forcing callers to instrument the call site.
import logging
log = logging.getLogger(__name__)


def log_hypotheses(hyps: list[str], *, source: str = "hypothesis.py") -> None:
    """Emit one log record summarising the generated hypothesis set."""
    log.info("hypothesis_generated source=%s count=%d", source, len(hyps))


if __name__ == "__main__":
    hyps = generate_hypotheses_from_weaknesses()
    print(f"{len(hyps)} hypotheses")
    for h in hyps[:5]:
        print(f" - {h}")
