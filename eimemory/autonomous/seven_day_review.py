"""7-day review: auto-promote or rollback active candidates based on real metrics.

Reads each candidate currently in `active/`, looks up before/after `hit@1` from the
caller-supplied metrics, and decides one of three outcomes:

- ``promote_l2`` — delta >= promote_threshold (default +5%).
- ``rollback`` — delta <= rollback_threshold (default -3%); the file is moved to
  the sibling ``rolled_back/`` directory.
- ``keep`` — anywhere in between; nothing is moved.

Decisions are returned as a list of :class:`ReviewDecision` records. The function
is pure with respect to side effects (the only filesystem change is the rollback
``shutil.move``); it does not touch audit logs or external state.
"""
from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path


@dataclass(slots=True, frozen=True)
class ReviewDecision:
    """One candidate's review verdict.

    Attributes:
        record_id: Stem of the candidate file (no extension).
        decision: One of ``"promote_l2"``, ``"rollback"``, or ``"keep"``.
        delta: ``hit@1_after - hit@1_before`` for this candidate.
    """

    record_id: str
    decision: str
    delta: float


def review_active_candidates(
    active_dir: Path,
    metrics: dict[str, dict[str, float]],
    promote_threshold: float = 0.05,
    rollback_threshold: float = -0.03,
) -> list[ReviewDecision]:
    """Review every candidate in ``active/`` against real hit@1 metrics.

    Args:
        active_dir: Directory holding the ``*.md`` candidate files currently in
            the active state. The sibling ``rolled_back/`` directory is
            created if missing.
        metrics: Mapping of ``record_id`` -> ``{"hit@1_before": float,
            "hit@1_after": float}``. Candidates not present in this mapping
            are treated as ``keep`` with a delta of 0.0.
        promote_threshold: Minimum positive delta that triggers a
            ``promote_l2`` decision. Default ``0.05``.
        rollback_threshold: Maximum (most negative) delta that triggers a
            ``rollback`` decision. Default ``-0.03``.

    Returns:
        A list of :class:`ReviewDecision`, one per candidate found in
        ``active_dir``. Order matches ``Path.glob("*.md")`` (unsorted).
    """
    active_dir = Path(active_dir)
    rolled_back = active_dir.parent / "rolled_back"
    rolled_back.mkdir(exist_ok=True)

    decisions: list[ReviewDecision] = []
    for candidate in active_dir.glob("*.md"):
        rid = candidate.stem
        m = metrics.get(rid, {})
        before = float(m.get("hit@1_before", 0.0))
        after = float(m.get("hit@1_after", 0.0))
        delta = after - before

        if delta >= promote_threshold:
            decisions.append(ReviewDecision(rid, "promote_l2", delta))
        elif delta <= rollback_threshold:
            shutil.move(str(candidate), str(rolled_back / candidate.name))
            decisions.append(ReviewDecision(rid, "rollback", delta))
        else:
            decisions.append(ReviewDecision(rid, "keep", delta))
    return decisions
