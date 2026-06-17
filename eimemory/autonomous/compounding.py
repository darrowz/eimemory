"""Compounding context builder for the Karpathy Loop.

Task 2.6 of the 2026-06-17 plan: feed the next experiment iteration with
a markdown summary of the most recent ``kept`` experiments so each
iteration can build on prior wins instead of starting from scratch.

Two public functions:

* :func:`load_recent_kept` — filter the JSONL experiment log to the
  last ``n`` rows where ``kept=True``, in chronological order. This
  intentionally differs from :meth:`eimemory.autonomous.exp_log.ExpLog.recent_kept`,
  which returns the recent window without filtering (callers that need
  only kept rows must filter themselves — that is what this module
  does).
* :func:`format_as_context` — render those rows as a compact markdown
  block, capped at a configurable byte budget so it never bloats the
  next prompt.

The cap is exposed as a keyword argument (default
:data:`DEFAULT_CONTEXT_MAX_BYTES`) so it is a real knob, not a comment
— the plan note explicitly says "Configurable but not a comment".
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

# Plan defaults (Phase 2 acceptance gate).
# "Compounding cap: Last 5 kept experiments, capped at 2 KB context."
DEFAULT_RECENT_KEPT: int = 5
DEFAULT_CONTEXT_MAX_BYTES: int = 2 * 1024

EMPTY_CONTEXT_MARKER = "(no prior kept experiments)"
TRUNCATION_MARKER_TEMPLATE = (
    "…({n} more kept experiments omitted to stay under {cap} byte cap)"
)


def load_recent_kept(
    exp_log_path: Path,
    n: int = DEFAULT_RECENT_KEPT,
) -> list[dict[str, Any]]:
    """Return the last ``n`` ``kept=True`` rows from the experiment log.

    Args:
        exp_log_path: Path to the JSONL experiment log written by
            :class:`eimemory.autonomous.exp_log.ExpLog`. A missing file
            is treated as an empty log (returns ``[]``) so the loop can
            call this on a cold start without a guard.
        n: Maximum number of kept rows to return. Defaults to
            :data:`DEFAULT_RECENT_KEPT` (5). Values ``<= 0`` return
            ``[]``.

    Returns:
        The last ``n`` kept rows in chronological (file) order. Rows
        that fail to parse are skipped silently to keep the loop
        resilient to a partially-written log line.
    """
    if n <= 0:
        return []
    path = Path(exp_log_path)
    if not path.exists():
        return []
    kept: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if row.get("kept"):
                kept.append(row)
    return kept[-n:]


def format_as_context(
    rows: list[dict[str, Any]],
    max_bytes: int = DEFAULT_CONTEXT_MAX_BYTES,
) -> str:
    """Render kept experiments as a markdown context block.

    Rows are emitted as one bullet line each, oldest first. The output
    is hard-capped at ``max_bytes`` bytes (UTF-8); once adding the next
    row would push the block over the cap, remaining rows are omitted
    and a truncation marker is appended (if the marker itself fits).
    An empty input yields the documented empty-state marker so the
    caller can always paste the result into a prompt without a
    special case.

    Args:
        rows: Kept-experiment rows, typically from
            :func:`load_recent_kept`. Each row should expose
            ``hypothesis``, ``primary_metric_before``,
            ``primary_metric_after``, and ``timestamp`` keys — the
            JSONL schema written by
            :class:`eimemory.autonomous.exp_log.ExpLog`.
        max_bytes: Hard byte cap for the rendered string. Defaults to
            :data:`DEFAULT_CONTEXT_MAX_BYTES` (2 KB). Values ``<= 0``
            disable the cap (no truncation, no marker).

    Returns:
        A markdown string safe to paste into the next hypothesis
        prompt. Length is always ``<= max_bytes`` when ``max_bytes > 0``.
    """
    if not rows:
        return EMPTY_CONTEXT_MARKER

    header = "## Prior kept experiments (compounding context)"
    formatted = [_format_row(r) for r in rows]

    if max_bytes <= 0:
        # Cap disabled — emit everything, no marker.
        return "\n".join([header, *formatted])

    # Reserve a fixed slice of the cap for the header line (+ its newline).
    header_bytes = len(header.encode("utf-8")) + 1
    remaining = max_bytes - header_bytes
    if remaining < 0:
        # Budget is so tight the header itself does not fit; emit just
        # the header (truncated to the cap) and skip rows + marker.
        # (Caller asked for an unreasonably small cap; honour it literally.)
        return header[:max_bytes]

    kept_lines: list[str] = []
    omitted = 0
    for line in formatted:
        line_bytes = len(line.encode("utf-8")) + 1  # +1 for the trailing "\n"
        if line_bytes > remaining:
            omitted += 1
            continue
        kept_lines.append(line)
        remaining -= line_bytes
        if remaining <= 0:
            # No more room; the rest are omitted. We do not break out of
            # the loop here so we can keep an accurate omitted count
            # when the cap is hit mid-batch.
            continue

    parts: list[str] = [header, *kept_lines]
    if omitted > 0:
        marker = TRUNCATION_MARKER_TEMPLATE.format(n=omitted, cap=max_bytes)
        marker_bytes = len(marker.encode("utf-8")) + 1
        current_bytes = sum(len(p.encode("utf-8")) + 1 for p in parts)
        if current_bytes + marker_bytes <= max_bytes:
            parts.append(marker)
        # If the marker would push us over, silently drop it. The
        # caller still gets a context block that fits the cap; the
        # omitted rows are still accounted for in the (suppressed)
        # marker text should the cap be raised.

    return "\n".join(parts)


def _format_row(row: dict[str, Any]) -> str:
    """Format one kept experiment as a single markdown bullet line."""
    ts = row.get("timestamp", "") or ""
    hyp = row.get("hypothesis", "") or ""
    before = float(row.get("primary_metric_before", 0.0) or 0.0)
    after = float(row.get("primary_metric_after", 0.0) or 0.0)
    return (
        f"- {ts} | {hyp} | "
        f"before={before:.3f} -> after={after:.3f}"
    )
