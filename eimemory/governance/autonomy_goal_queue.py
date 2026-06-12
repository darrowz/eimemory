from __future__ import annotations

from dataclasses import asdict
from typing import Any

from eimemory.core.clock import now_iso
from eimemory.governance.capability_ledger import build_capability_ledger
from eimemory.models.records import RecordEnvelope, ScopeRef


DEFAULT_AUTONOMY_CAPABILITIES = [
    "operations.uumit",
    "search.discovery",
    "research.synthesis",
    "office.daily_task",
    "device.control",
]

SCORING_FACTORS = ["user_value", "failure_frequency", "potential_gain", "risk", "evidence_gap"]

USER_VALUE_BY_CAPABILITY = {
    "operations.uumit": 0.95,
    "search.discovery": 0.9,
    "research.synthesis": 0.86,
    "office.daily_task": 0.82,
    "device.control": 0.78,
}

RISK_BY_CAPABILITY = {
    "operations.uumit": 0.3,
    "search.discovery": 0.18,
    "research.synthesis": 0.2,
    "office.daily_task": 0.24,
    "device.control": 0.55,
}


def build_autonomy_goal_queue(
    runtime: Any,
    *,
    scope: dict[str, Any] | ScopeRef | None = None,
    max_goals: int = 3,
    persist: bool = False,
    capabilities: list[str] | None = None,
    signal_limit: int = 500,
) -> dict[str, Any]:
    """Plan the 1-3 highest-value capability goals without executing learning."""
    scope_ref = scope if isinstance(scope, ScopeRef) else ScopeRef.from_dict(scope)
    generated_at = now_iso()
    selected_limit = max(1, min(3, int(max_goals or 1)))
    target_capabilities = _dedupe_capabilities(capabilities or DEFAULT_AUTONOMY_CAPABILITIES)
    ledger = build_capability_ledger(runtime, scope=scope_ref, limit=signal_limit, attribute_outcomes=False)
    signals = _collect_recent_signals(runtime, scope=scope_ref, limit=signal_limit)

    ranked = [
        _score_capability(capability, ledger.get("capabilities", {}).get(capability, {}), signals)
        for capability in target_capabilities
    ]
    ranked.sort(key=lambda item: (-float(item["priority_score"]), str(item["capability"])))
    selected_goals = ranked[:selected_limit]

    persisted_record_id = ""
    if persist:
        record = _queue_record(
            selected_goals,
            ranked=ranked,
            scope=scope_ref,
            generated_at=generated_at,
            selected_limit=selected_limit,
        )
        runtime.store.append(record)
        persisted_record_id = record.record_id

    return {
        "goals": selected_goals,
        "goal_count": len(ranked),
        "selected_count": len(selected_goals),
        "persisted_record_id": persisted_record_id,
        "generated_at": generated_at,
    }


def _score_capability(capability: str, ledger_item: dict[str, Any], signals: list[dict[str, Any]]) -> dict[str, Any]:
    score = _clamp(float(ledger_item.get("score") or 0.0))
    evidence_count = max(0, int(ledger_item.get("evidence_count") or 0))
    regression_count = max(0, int(ledger_item.get("regression_count") or 0))
    trend = float(ledger_item.get("trend") or 0.0)
    capability_signals = [signal for signal in signals if signal.get("capability") == capability]
    failure_count = regression_count + sum(1 for signal in capability_signals if signal.get("is_failure"))
    failure_frequency = _clamp(failure_count / 5.0)
    evidence_gap = _clamp((3 - min(evidence_count, 3)) / 3.0)
    potential_gain = _clamp((1.0 - score) * 0.65 + failure_frequency * 0.25 + max(0.0, -trend) * 0.1)
    user_value = USER_VALUE_BY_CAPABILITY.get(capability, 0.7)
    risk = RISK_BY_CAPABILITY.get(capability, 0.3)
    priority_score = _clamp(
        user_value * 0.3
        + failure_frequency * 0.24
        + potential_gain * 0.24
        + evidence_gap * 0.18
        - risk * 0.1
    )
    factors = {
        "user_value": round(user_value, 3),
        "failure_frequency": round(failure_frequency, 3),
        "potential_gain": round(potential_gain, 3),
        "risk": round(risk, 3),
        "evidence_gap": round(evidence_gap, 3),
    }
    return {
        "capability": capability,
        "title": f"Improve {capability}",
        "priority_score": round(priority_score, 3),
        "scoring_factors": factors,
        "explanation": _explain_goal(capability, score=score, evidence_count=evidence_count, failure_count=failure_count, factors=factors),
        "source_signal_counts": {
            "failures": failure_count,
            "recent_signals": len(capability_signals),
            "ledger_evidence": evidence_count,
            "regressions": regression_count,
        },
    }


def _collect_recent_signals(runtime: Any, *, scope: ScopeRef, limit: int) -> list[dict[str, Any]]:
    records = runtime.store.list_records(kinds=["incident", "replay_result", "learning_eval"], scope=scope, limit=limit)
    signals: list[dict[str, Any]] = []
    for record in records:
        capability = _record_capability(record)
        if not capability:
            continue
        signals.append(
            {
                "record_id": record.record_id,
                "kind": record.kind,
                "capability": capability,
                "is_failure": _record_is_failure(record),
            }
        )
    return signals


def _record_capability(record: RecordEnvelope) -> str:
    for source in (record.meta, record.content, record.provenance):
        for key in ("capability", "target_capability", "capability_domain"):
            value = str(source.get(key) or "").strip()
            if value:
                return value
    text = " ".join([record.title, record.summary, record.detail, " ".join(record.tags)]).lower()
    for capability in DEFAULT_AUTONOMY_CAPABILITIES:
        if capability.lower() in text:
            return capability
    return ""


def _record_is_failure(record: RecordEnvelope) -> bool:
    if record.kind == "incident":
        return True
    verdict = str(record.meta.get("verdict") or record.content.get("verdict") or record.meta.get("status") or record.status or "").lower()
    if verdict in {"fail", "failed", "failure", "blocked", "regressed", "unsafe"}:
        return True
    if record.kind == "learning_eval" and record.meta.get("ok") is False:
        return True
    if record.kind == "replay_result" and float(record.meta.get("pass_rate") or 1.0) < 0.8:
        return True
    return False


def _queue_record(
    goals: list[dict[str, Any]],
    *,
    ranked: list[dict[str, Any]],
    scope: ScopeRef,
    generated_at: str,
    selected_limit: int,
) -> RecordEnvelope:
    summary = f"Autonomy goal queue selected {len(goals)} of {len(ranked)} capability goals."
    return RecordEnvelope.create(
        kind="autonomy_goal_queue",
        title="Autonomy goal queue",
        summary=summary,
        detail=summary,
        scope=scope,
        source="eimemory.autonomy_goal_queue",
        status="active",
        content={
            "generated_at": generated_at,
            "goals": goals,
            "ranked_capabilities": ranked,
            "scoring_factors": SCORING_FACTORS,
        },
        tags=["autonomy", "goal-queue", "planning-only"],
        provenance={"report_type": "autonomy_goal_queue", "generated_at": generated_at},
        meta={
            "report_type": "autonomy_goal_queue",
            "generated_at": generated_at,
            "goal_count": len(ranked),
            "selected_count": len(goals),
            "max_goals": selected_limit,
            "scoring_factors": SCORING_FACTORS,
            "scope": asdict(scope),
        },
    )


def _explain_goal(
    capability: str,
    *,
    score: float,
    evidence_count: int,
    failure_count: int,
    factors: dict[str, float],
) -> str:
    reasons = []
    if evidence_count < 3:
        reasons.append(f"evidence is thin ({evidence_count}/3 baseline)")
    if failure_count:
        reasons.append(f"{failure_count} recent failure signal(s)")
    if score < 0.5:
        reasons.append(f"ledger score is low ({round(score, 3)})")
    if not reasons:
        reasons.append("default daily value keeps this capability worth monitoring")
    return (
        f"{capability} ranks here because {', '.join(reasons)}; "
        f"user_value={factors['user_value']}, potential_gain={factors['potential_gain']}, risk={factors['risk']}."
    )


def _dedupe_capabilities(capabilities: list[str]) -> list[str]:
    deduped: list[str] = []
    for capability in capabilities:
        value = str(capability or "").strip()
        if value and value not in deduped:
            deduped.append(value)
    return deduped or list(DEFAULT_AUTONOMY_CAPABILITIES)


def _clamp(value: float) -> float:
    return max(0.0, min(1.0, value))
