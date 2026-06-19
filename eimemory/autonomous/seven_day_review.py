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
from typing import Any

from eimemory.governance.capability_ledger import record_capability_score
from eimemory.models.records import RecordEnvelope, ScopeRef


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
    side_effect_record_ids: tuple[str, ...] = ()


def review_active_candidates(
    active_dir: Path,
    metrics: dict[str, dict[str, float]],
    promote_threshold: float = 0.05,
    rollback_threshold: float = -0.03,
    runtime: Any | None = None,
    scope: dict[str, Any] | ScopeRef | None = None,
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
            decision = "promote_l2"
            side_effect_ids = _record_review_side_effects(
                runtime,
                scope=scope,
                record_id=rid,
                decision=decision,
                metrics=m,
                delta=delta,
            )
            decisions.append(ReviewDecision(rid, decision, delta, tuple(side_effect_ids)))
        elif delta <= rollback_threshold:
            shutil.move(str(candidate), str(rolled_back / candidate.name))
            decision = "rollback"
            side_effect_ids = _record_review_side_effects(
                runtime,
                scope=scope,
                record_id=rid,
                decision=decision,
                metrics=m,
                delta=delta,
            )
            decisions.append(ReviewDecision(rid, decision, delta, tuple(side_effect_ids)))
        else:
            decisions.append(ReviewDecision(rid, "keep", delta))
    return decisions


def _record_review_side_effects(
    runtime: Any | None,
    *,
    scope: dict[str, Any] | ScopeRef | None,
    record_id: str,
    decision: str,
    metrics: dict[str, float],
    delta: float,
) -> list[str]:
    if runtime is None:
        return []
    scope_ref = scope if isinstance(scope, ScopeRef) else ScopeRef.from_dict(scope)
    capability = str(metrics.get("capability") or metrics.get("target_capability") or record_id)
    before = float(metrics.get("hit@1_before", 0.0))
    after = float(metrics.get("hit@1_after", 0.0))
    side_effect_ids: list[str] = []
    score_id = record_capability_score(
        runtime,
        scope=scope_ref,
        loop_id="seven_day_review",
        capability=capability,
        score=max(0.0, min(1.0, after)),
        evidence_items=[
            {
                "candidate_id": record_id,
                "decision": decision,
                "hit@1_before": before,
                "hit@1_after": after,
                "delta": delta,
            }
        ],
        evidence_sources=["seven_day_review"],
        regression_count=1 if decision == "rollback" else 0,
    )
    side_effect_ids.append(score_id)
    audit = RecordEnvelope.create(
        kind="reflection",
        title=f"Seven-day review {decision}: {record_id}",
        summary=f"{record_id} {decision} delta={delta:.3f}",
        detail="",
        content={
            "candidate_id": record_id,
            "decision": decision,
            "hit@1_before": before,
            "hit@1_after": after,
            "delta": delta,
            "capability": capability,
            "capability_score_id": score_id,
        },
        source="eimemory.autonomous.seven_day_review",
        scope=scope_ref,
        meta={
            "report_type": "seven_day_review_decision",
            "candidate_id": record_id,
            "decision": decision,
            "capability": capability,
        },
    )
    runtime.store.append(audit)
    side_effect_ids.append(audit.record_id)
    return side_effect_ids
