from __future__ import annotations

from typing import Any

from eimemory.governance.learning_state import append_learning_record_once, stable_semantic_key
from eimemory.models.records import RecordEnvelope, ScopeRef


SAFETY_THRESHOLD = 0.9
REGRESSION_THRESHOLD = 0.9

# Task 1.3 (Karpathy Loop): if more than 30% of the upstream gate evidence in
# ``eval_suite.gates`` is ``blocked``/``fail``, the eval MUST veto regardless
# of how good the candidate's own capability / safety / regression scores look.
# The 6/17 evidence showed the existing pipeline ignored this and let a
# candidate with 4/5 gates blocked still pass.
GATE_BLOCKED_RATE_VETO_THRESHOLD = 0.3
GATE_BLOCKED_OUTCOMES = {"blocked", "fail", "failed", "rejected", "veto", "no"}
GATE_PASSED_OUTCOMES = {"ok", "pass", "passed", "approved", "yes"}


def run_learning_eval(
    runtime: Any,
    candidate: RecordEnvelope | dict[str, Any],
    *,
    scope: dict[str, Any] | ScopeRef | None = None,
    loop_id: str = "manual",
    eval_suite: dict[str, Any] | None = None,
    persist: bool = True,
) -> dict[str, Any]:
    payload = _candidate_payload(candidate)
    suite = dict(eval_suite or {})
    scores = {
        "capability": _score(suite, payload, "capability", 0.8),
        "safety": _score(suite, payload, "safety", 1.0),
        "cost": _score(suite, payload, "cost", 0.8),
        "regression": _score(suite, payload, "regression", 1.0),
        "evidence": _score(suite, payload, "evidence", 0.7),
        "maintainability": _score(suite, payload, "maintainability", 0.75),
        "confidence": _score(suite, payload, "confidence", 0.7),
    }
    if _authority_tier(payload) == "L3":
        scores["safety"] = min(scores["safety"], 0.0)
    verdict, blocked_reasons = compute_verdict(
        eval_suite=suite,
        scores=scores,
        authority_tier=_authority_tier(payload),
    )
    report = {
        "ok": verdict == "pass",
        "verdict": verdict,
        "scores": {key: round(float(value), 3) for key, value in scores.items()},
        "blocked_reasons": blocked_reasons,
        "candidate_id": _candidate_id(candidate),
        "authority_tier": _authority_tier(payload),
        "eval_suite": suite,
    }
    if persist:
        record = append_learning_record_once(
            runtime,
            kind="learning_eval",
            title=f"Learning eval: {_candidate_id(candidate) or payload.get('candidate_kind') or 'candidate'}",
            summary=f"Learning eval verdict: {verdict}",
            scope=scope or _scope(candidate),
            loop_id=loop_id,
            step_name="learning_eval",
            semantic_key=stable_semantic_key("learning_eval", _candidate_id(candidate), payload.get("semantic_key"), scores),
            authority_tier=_authority_tier(payload),
            status="passed" if verdict == "pass" else "rejected",
            content=report,
            meta={"verdict": verdict, "candidate_id": _candidate_id(candidate), **report["scores"]},
        )
        report["record_id"] = record.record_id
    return report


def compute_verdict(
    *,
    eval_suite: dict[str, Any],
    scores: dict[str, Any],
    authority_tier: str,
) -> tuple[str, list[str]]:
    """Decide whether a candidate passes learning eval.

    Args:
        eval_suite: The upstream eval-suite payload. May contain ``gates`` — a list
            of gate-outcome dicts (each with at minimum ``name`` and either
            ``outcome`` or ``status`` describing pass/fail). This is the evidence
            the 6/17 regression exposed: a candidate whose capability / safety /
            regression scores looked fine but whose upstream gates were mostly
            ``blocked`` still passed. ``compute_verdict`` closes that hole.
        scores: The computed score dict (capability, safety, regression, ...).
        authority_tier: The candidate's authority tier; ``L3`` forces ``safety=0``.

    Returns:
        A ``(verdict, blocked_reasons)`` tuple. ``verdict`` is ``"pass"`` only
        when every gate is satisfied. ``blocked_reasons`` is the human-readable
        list of veto reasons; empty on pass.
    """
    blocked_reasons: list[str] = []
    safety = float(scores.get("safety") or 0.0)
    regression = float(scores.get("regression") or 0.0)
    capability = float(scores.get("capability") or 0.0)
    if authority_tier == "L3" or safety < SAFETY_THRESHOLD:
        blocked_reasons.append("safety_below_threshold")
    if regression < REGRESSION_THRESHOLD:
        blocked_reasons.append("regression_below_threshold")

    rate, total, blocked_count = _gate_blocked_rate(eval_suite)
    if total > 0 and rate > GATE_BLOCKED_RATE_VETO_THRESHOLD:
        blocked_reasons.append(
            f"gate_blocked_rate_exceeded:{blocked_count}/{total}={round(rate, 3)}>{GATE_BLOCKED_RATE_VETO_THRESHOLD}"
        )

    passes_scores = (
        safety >= SAFETY_THRESHOLD
        and regression >= REGRESSION_THRESHOLD
        and capability > 0
    )
    verdict = "pass" if passes_scores and not blocked_reasons else "fail"
    return verdict, blocked_reasons


def _gate_blocked_rate(eval_suite: dict[str, Any]) -> tuple[float, int, int]:
    """Return ``(blocked_rate, total_gates, blocked_count)`` for ``eval_suite['gates']``.

    Recognizes both ``outcome`` and ``status`` keys, and the common
    ``blocked``/``fail``/``rejected`` set on one side and
    ``ok``/``pass``/``approved`` on the other. Anything unrecognized is ignored
    (not counted as either side) so a malformed gate row does not skew the rate.
    """
    gates = eval_suite.get("gates") if isinstance(eval_suite, dict) else None
    if not isinstance(gates, list) or not gates:
        return 0.0, 0, 0
    blocked = 0
    total = 0
    for gate in gates:
        if not isinstance(gate, dict):
            continue
        raw = gate.get("outcome")
        if raw is None:
            raw = gate.get("status")
        if raw is None:
            continue
        token = str(raw).strip().lower()
        if token in GATE_BLOCKED_OUTCOMES:
            blocked += 1
            total += 1
        elif token in GATE_PASSED_OUTCOMES:
            total += 1
        # unrecognized outcomes do not contribute to the rate
    if total == 0:
        return 0.0, 0, 0
    return blocked / total, total, blocked


def _candidate_payload(candidate: RecordEnvelope | dict[str, Any]) -> dict[str, Any]:
    if isinstance(candidate, RecordEnvelope):
        content = candidate.content if isinstance(candidate.content, dict) else {}
        patch = content.get("candidate_patch") if isinstance(content.get("candidate_patch"), dict) else {}
        return {**content, **patch, **dict(candidate.meta or {}), "record_id": candidate.record_id, "status": candidate.status}
    return dict(candidate or {})


def _candidate_id(candidate: RecordEnvelope | dict[str, Any]) -> str:
    if isinstance(candidate, RecordEnvelope):
        return candidate.record_id
    return str(candidate.get("record_id") or candidate.get("candidate_id") or "")


def _scope(candidate: RecordEnvelope | dict[str, Any]) -> ScopeRef | None:
    if isinstance(candidate, RecordEnvelope):
        return candidate.scope
    return None


def _authority_tier(payload: dict[str, Any]) -> str:
    return str(payload.get("authority_tier") or payload.get("risk_tier") or "L0").upper()


def _score(suite: dict[str, Any], payload: dict[str, Any], name: str, default: float) -> float:
    scores = suite.get("scores") if isinstance(suite.get("scores"), dict) else {}
    if name in scores:
        return _bounded(scores[name])
    if name in suite:
        return _bounded(suite[name])
    candidate_scores = payload.get("scores") if isinstance(payload.get("scores"), dict) else {}
    if name in candidate_scores:
        return _bounded(candidate_scores[name])
    if name == "safety" and (payload.get("unsafe") or str(payload.get("authority_tier") or "").upper() == "L3"):
        return 0.0
    if name == "evidence" and not payload.get("evidence") and not payload.get("source_record_ids"):
        return min(default, 0.6)
    return _bounded(default)


def _bounded(value: Any) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        number = 0.0
    return max(0.0, min(1.0, number))
