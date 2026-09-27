from __future__ import annotations

import os
from typing import Any

from eimemory.governance.learning_state import append_learning_record_once, stable_semantic_key
from eimemory.models.records import ScopeRef


def run_regression_watch(
    runtime: Any,
    *,
    candidate_id: str,
    eval_result: dict[str, Any],
    scope: dict[str, Any] | ScopeRef | None = None,
    loop_id: str = "manual",
) -> dict[str, Any]:
    candidate = runtime.store.get_by_id(candidate_id, scope=scope)
    if candidate is None:
        raise ValueError(f"candidate not found: {candidate_id}")
    scores = dict(eval_result.get("scores") or {})
    regressed = str(eval_result.get("verdict") or "") == "fail" or _score_value(scores, "regression", default=1.0) < 0.9
    tier = str(candidate.meta.get("authority_tier") or candidate.content.get("authority_tier") or "L0").upper()
    action = "observed"
    if regressed:
        action = "disabled" if tier in {"L0", "L1"} else "rollback_requested"
    if regressed and tier in {"L0", "L1"}:
        candidate.status = "disabled"
        candidate.meta["disabled_reason"] = "regression_watch"
        runtime.store.rewrite(candidate)
    record = append_learning_record_once(
        runtime,
        kind="regression_watch",
        title=f"Regression watch: {candidate.title}",
        summary=f"Regression detected for {candidate.record_id}" if regressed else f"No regression detected for {candidate.record_id}",
        scope=scope or candidate.scope,
        loop_id=loop_id,
        step_name="regression_watch",
        semantic_key=stable_semantic_key("regression", candidate.record_id, eval_result),
        authority_tier=tier,
        status="active",
        content={
            "candidate_id": candidate.record_id,
            "eval_result": eval_result,
            "regressed": bool(regressed),
            "action": action,
            "rollback": candidate.content.get("rollback") or "disable candidate",
        },
        meta={"candidate_id": candidate.record_id, "action": action, "authority_tier": tier, "regressed": bool(regressed)},
    )
    return {"ok": True, "regressed": bool(regressed), "action": action, "record_id": record.record_id}


def _score_value(scores: dict[str, Any], key: str, *, default: float) -> float:
    if key not in scores or scores.get(key) is None:
        return float(default)
    try:
        return float(scores.get(key))
    except (TypeError, ValueError):
        return 0.0


def _int_value(value: Any, *, default: int = 0) -> int:
    if value is None:
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _float_value(value: Any, *, default: float = 0.0) -> float:
    if value is None:
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def evaluate_harness_gate(
    runtime: Any,
    *,
    candidate_id: str,
    held_in_scores: dict[str, float],
    held_out_scores: dict[str, float] | None,
    baseline_held_in: float,
    baseline_held_out: float | None,
    scope: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Dual-regression evaluation wrapper for ``HarnessGate``.

    Lazy-imports :mod:`eimemory.governance.harness_patch` so this module stays
    importable on the 1.5.x default path (when ``HARNESS_PATCH_V2`` is unset).

    Returns a JSON-friendly ``dict`` with the gate verdict and supporting
    scores. When ``HARNESS_PATCH_V2`` is unset (legacy path), a minimal
    ``ProposalCard`` is synthesized so the gate still produces a verdict.
    """
    # Lazy imports keep the module importable when HARNESS_PATCH_V2 is unset.
    from eimemory.governance.harness_patch import (  # noqa: WPS433
        HarnessGate,
        HarnessSurface,
        ProposalCard,
    )

    # Re-read the env var at call time so monkeypatching in tests takes effect
    # (the module-level constant in harness_patch is captured at import time).
    harness_v2 = os.environ.get("HARNESS_PATCH_V2") == "1"

    candidate = runtime.store.get_by_id(candidate_id, scope=scope)
    if candidate is None:
        raise ValueError(f"candidate not found: {candidate_id}")

    card_data = ((candidate.content or {}).get("proposal_card") if harness_v2 else None)
    if harness_v2 and not card_data:
        return {
            "verdict": "REJECT",
            "reason": "proposal_card missing on candidate",
            "candidate_id": candidate_id,
        }

    if harness_v2:
        # Build the ProposalCard from the candidate's stored content.
        card = ProposalCard(
            target_surface=HarnessSurface(card_data["target_surface"]),
            evidence_record_ids=tuple(card_data.get("evidence_record_ids") or ()),
            expected_delta=_float_value(card_data.get("expected_delta"), default=0.0),
            target_agent=str(card_data.get("target_agent") or ""),
            risk_tier=str(card_data.get("risk_tier") or "L0"),
            rollback_plan=str(card_data.get("rollback_plan") or ""),
            diff_lines=_int_value(card_data.get("diff_lines"), default=0),
            diff_tokens=_int_value(card_data.get("diff_tokens"), default=0),
        )
    else:
        # Legacy mode: synthesize a minimal card so the gate still runs.
        card = ProposalCard(
            target_surface=HarnessSurface.RUNTIME_POLICY,
            evidence_record_ids=(candidate_id,),
            expected_delta=0.0,
            target_agent="legacy",
            risk_tier="L0",
            rollback_plan="legacy",
            diff_lines=0,
            diff_tokens=0,
        )

    gate = HarnessGate(card)
    result = gate.evaluate(
        held_in_scores=held_in_scores,
        held_out_scores=held_out_scores,
        baseline_held_in=baseline_held_in,
        baseline_held_out=baseline_held_out,
    )
    return {
        "verdict": result.verdict.value,
        "reason": result.reason,
        "held_in_score": result.held_in_score,
        "held_out_score": result.held_out_score,
        "delta": result.delta,
        "candidate_id": candidate_id,
    }
