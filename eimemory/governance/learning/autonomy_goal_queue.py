from __future__ import annotations

from dataclasses import asdict
from typing import Any

from eimemory.capabilities.consumer_views import dynamic_capability_views, dynamic_evaluation_view
from eimemory.core.clock import now_iso
from eimemory.governance.capability_ledger import build_dynamic_capability_ledger
from eimemory.models.records import RecordEnvelope, ScopeRef


SCORING_FACTORS = [
    "user_value",
    "failure_frequency",
    "potential_gain",
    "risk",
    "cost",
    "priority_weight",
    "evidence_gap",
]

def build_autonomy_goal_queue(
    runtime: Any,
    *,
    scope: dict[str, Any] | ScopeRef | None = None,
    max_goals: int = 3,
    persist: bool = False,
    capabilities: list[str] | None = None,
    signal_limit: int = 500,
    capability_scope: str = "global",
    profile_key: str = "",
    catalog: Any | None = None,
    at_time: str = "",
    legacy_compatibility: bool = False,
) -> dict[str, Any]:
    """Plan the 1-3 highest-value capability goals without executing learning."""
    scope_ref = scope if isinstance(scope, ScopeRef) else ScopeRef.from_dict(scope)
    generated_at = now_iso()
    selected_limit = max(1, min(3, int(max_goals or 1)))
    bounded_signal_limit = max(1, min(500, int(signal_limit or 1)))
    evaluation_view: dict[str, Any] = {}
    evaluation_targets: dict[str, list[dict[str, Any]]] = {}
    requested = set(_dedupe_capabilities(capabilities or []))
    if legacy_compatibility:
        # The compatibility caller owns the historical cohort.  This module
        # deliberately has no compiled default list, so an ordinary dynamic
        # invocation can never accidentally recreate one.
        view = _legacy_capability_view(requested, scope=scope_ref, capability_scope=capability_scope)
        evaluation_view = {
            "ok": True,
            "status": "legacy_compatibility",
            "reason": "",
            "cases": [],
        }
    elif catalog is not None:
        evaluation_view = dynamic_evaluation_view(
            runtime,
            scope=scope_ref,
            capability_scope=capability_scope,
            profile_key=profile_key,
            catalog=catalog,
            at_time=at_time,
            max_cases=min(256, bounded_signal_limit),
        )
        if evaluation_view.get("ok") is not True:
            return {
                "ok": False,
                "status": "blocked",
                "reason": str(evaluation_view.get("reason") or "capability_evaluation_selection_blocked"),
                "errors": [str(item) for item in evaluation_view.get("errors") or ()],
                "goals": [],
                "goal_count": 0,
                "selected_count": 0,
                "capability_evaluation_view": evaluation_view,
                "persisted_record_id": "",
                "generated_at": generated_at,
            }
        view = evaluation_view.get("capability_view") if isinstance(evaluation_view.get("capability_view"), dict) else {}
        for entry in evaluation_view.get("cases") or ():
            if not isinstance(entry, dict):
                continue
            artifact = entry.get("artifact") if isinstance(entry.get("artifact"), dict) else {}
            target = entry.get("target") if isinstance(entry.get("target"), dict) else {}
            capability_id = str(target.get("capability_id") or artifact.get("capability") or "").strip()
            if capability_id:
                evaluation_targets.setdefault(capability_id, []).append(
                    {
                        "case_id": str(artifact.get("case_id") or ""),
                        "evaluation_case_digest": str(artifact.get("evaluation_case_digest") or ""),
                        "eval_spec_id": str(artifact.get("eval_spec_id") or ""),
                        "capability_revision_id": str(target.get("capability_revision_id") or ""),
                        "provider_binding_id": str(target.get("provider_binding_id") or ""),
                    }
                )
    else:
        view = dynamic_capability_views(
            runtime,
            scope=scope_ref,
            capability_scope=capability_scope,
            profile_key=profile_key,
            at_time=at_time,
            limit=min(499, bounded_signal_limit),
        )
    target_capabilities = [
        entry
        for entry in view["capabilities"]
        if not requested or str(entry.get("capability_id") or "") in requested
    ]
    excluded_capabilities = sorted(requested.difference(str(item.get("capability_id") or "") for item in target_capabilities))
    ledger = build_dynamic_capability_ledger(
        runtime,
        scope=scope_ref,
        capability_scope=capability_scope,
        limit=bounded_signal_limit,
    )
    signals = _collect_recent_signals(runtime, scope=scope_ref, limit=bounded_signal_limit)

    ranked = [
        _score_capability(
            entry,
            _ledger_item(ledger, str(entry["capability_id"])),
            signals,
            evaluation_targets=evaluation_targets.get(str(entry["capability_id"])) or [],
        )
        for entry in target_capabilities
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
            consumer_view=view,
            evaluation_view=evaluation_view,
        )
        runtime.store.append(record)
        persisted_record_id = record.record_id

    return {
        "goals": selected_goals,
        "goal_count": len(ranked),
        "selected_count": len(selected_goals),
        "capability_scope": capability_scope,
        "profile": view.get("profile") or {},
        "consumer_view_digest": str(view.get("resolution_digest") or ""),
        "excluded_capabilities": excluded_capabilities,
        "persisted_record_id": persisted_record_id,
        "generated_at": generated_at,
        "capability_evaluation_view": evaluation_view,
        "legacy_compatibility": bool(legacy_compatibility),
    }


def _score_capability(
    entry: dict[str, Any],
    ledger_item: dict[str, Any],
    signals: list[dict[str, Any]],
    *,
    evaluation_targets: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    capability = str(entry["capability_id"])
    score = _clamp(float(ledger_item.get("pass_rate") or 0.0))
    evidence_count = max(0, int(ledger_item.get("evidence_count") or 0))
    regression_count = max(0, int(ledger_item.get("regression_count") or 0))
    trend = float(ledger_item.get("trend") or 0.0)
    capability_signals = [signal for signal in signals if signal.get("capability") == capability]
    failure_count = regression_count + sum(1 for signal in capability_signals if signal.get("is_failure"))
    failure_frequency = _clamp(failure_count / 5.0)
    evidence_gap = _clamp((3 - min(evidence_count, 3)) / 3.0)
    potential_gain = _clamp((1.0 - score) * 0.65 + failure_frequency * 0.25 + max(0.0, -trend) * 0.1)
    planning_policy = entry.get("planning_policy") if isinstance(entry.get("planning_policy"), dict) else {}
    # Absence means neutral policy, not a hard-coded assumption for an
    # arbitrary semantic capability.  A profile can tune these per rule.
    user_value = _clamp(float(planning_policy.get("user_value", 0.5)))
    risk = _clamp(float(planning_policy.get("risk", 0.5)))
    cost = _clamp(float(planning_policy.get("cost", 0.5)))
    priority_weight = _clamp(float(planning_policy.get("priority_weight", 0.5)))
    priority_score = _clamp(
        user_value * 0.28
        + failure_frequency * 0.24
        + potential_gain * 0.24
        + evidence_gap * 0.16
        + priority_weight * 0.12
        - risk * 0.08
        - cost * 0.04
    )
    factors = {
        "user_value": round(user_value, 3),
        "failure_frequency": round(failure_frequency, 3),
        "potential_gain": round(potential_gain, 3),
        "risk": round(risk, 3),
        "cost": round(cost, 3),
        "priority_weight": round(priority_weight, 3),
        "evidence_gap": round(evidence_gap, 3),
    }
    return {
        "capability": capability,
        "title": f"Improve {str(entry.get('display_name') or capability)}",
        "priority_score": round(priority_score, 3),
        "scoring_factors": factors,
        "explanation": _explain_goal(capability, score=score, evidence_count=evidence_count, failure_count=failure_count, factors=factors),
        "source_signal_counts": {
            "failures": failure_count,
            "recent_signals": len(capability_signals),
            "ledger_evidence": evidence_count,
            "regressions": regression_count,
        },
        "policy": planning_policy,
        "profile_requirement": entry.get("requirement") or {},
        "evaluation_targets": list(evaluation_targets or []),
    }


def _legacy_capability_view(
    capability_ids: set[str],
    *,
    scope: ScopeRef,
    capability_scope: str,
) -> dict[str, Any]:
    """Render only an explicitly supplied historical compatibility cohort."""

    return {
        "schema": "capability.consumer_view.v1",
        "source": "legacy_compatibility",
        "scope": asdict(scope),
        "capability_scope": capability_scope,
        "profile": {},
        "resolution_digest": "",
        "registry_watermark": "",
        "lifecycle_watermark": "",
        "capabilities": [
            {
                "capability_id": capability_id,
                "display_name": capability_id,
                "planning_policy": {},
                "requirement": {},
            }
            for capability_id in sorted(capability_ids)
        ],
        "truncated": False,
    }


def _ledger_item(ledger: dict[str, Any], capability_id: str) -> dict[str, Any]:
    """Flatten the v3 evidence ledger without hiding revision/binding facts."""

    capability = (ledger.get("capabilities") or {}).get(capability_id)
    if not isinstance(capability, dict):
        return {"pass_rate": 0.0, "evidence_count": 0, "regression_count": 0, "trend": 0.0}
    decisive = 0
    passes = 0
    evidence_count = 0
    failures = 0
    for revision in (capability.get("revisions") or {}).values():
        if not isinstance(revision, dict):
            continue
        for binding in (revision.get("bindings") or {}).values():
            if not isinstance(binding, dict):
                continue
            evidence_count += int(binding.get("observation_count") or 0)
            decisive += int(binding.get("decisive_count") or 0)
            passes += int(binding.get("pass_count") or 0)
            failures += int(binding.get("failure_count") or 0)
    return {
        "pass_rate": (passes / decisive) if decisive else 0.0,
        "evidence_count": evidence_count,
        "regression_count": failures,
        "trend": 0.0,
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
    # Unknown historical prose is deliberately unclassified.  Reintroducing a
    # keyword map here would bind future capabilities to a compiled universe.
    return ""


def _record_is_failure(record: RecordEnvelope) -> bool:
    if record.kind == "incident":
        return True
    verdict = str(record.meta.get("verdict") or record.content.get("verdict") or record.meta.get("status") or record.status or "").lower()
    if verdict in {"fail", "failed", "failure", "blocked", "regressed", "unsafe"}:
        return True
    if record.kind == "learning_eval" and record.meta.get("ok") is False:
        return True
    if record.kind == "replay_result":
        pass_rate = _optional_float(record.meta.get("pass_rate"))
        if pass_rate is None:
            return record.meta.get("pass_rate") is not None
        return pass_rate < 0.8
    return False


def _optional_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _queue_record(
    goals: list[dict[str, Any]],
    *,
    ranked: list[dict[str, Any]],
    scope: ScopeRef,
    generated_at: str,
    selected_limit: int,
    consumer_view: dict[str, Any],
    evaluation_view: dict[str, Any],
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
            "capability_view": consumer_view,
            "capability_evaluation_view": evaluation_view,
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
            "capability_view_source": str(consumer_view.get("source") or ""),
            "capability_resolution_digest": str(consumer_view.get("resolution_digest") or ""),
            "capability_evaluation_status": str(evaluation_view.get("status") or "legacy_shadow"),
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
        reasons.append("profile policy and observed evidence keep this capability worth monitoring")
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
    return deduped


def _clamp(value: float) -> float:
    return max(0.0, min(1.0, value))
