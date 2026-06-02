from __future__ import annotations

from typing import Any

from eimemory.governance.learning_state import append_learning_record_once, stable_semantic_key
from eimemory.models.records import RecordEnvelope, ScopeRef


SAFETY_THRESHOLD = 0.9
REGRESSION_THRESHOLD = 0.9


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
    verdict = "pass" if scores["safety"] >= SAFETY_THRESHOLD and scores["regression"] >= REGRESSION_THRESHOLD and scores["capability"] > 0 else "fail"
    blocked_reasons = []
    if scores["safety"] < SAFETY_THRESHOLD:
        blocked_reasons.append("safety_below_threshold")
    if scores["regression"] < REGRESSION_THRESHOLD:
        blocked_reasons.append("regression_below_threshold")
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
