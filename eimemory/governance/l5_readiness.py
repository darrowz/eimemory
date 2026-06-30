from __future__ import annotations

from dataclasses import asdict
from typing import Any

from eimemory.core.clock import now_iso
from eimemory.governance.capability_ledger import build_capability_ledger
from eimemory.governance.learning_state import append_learning_record_once, stable_semantic_key
from eimemory.models.records import ScopeRef


READINESS_CAPABILITIES = [
    "memory.recall",
    "tool.routing",
    "knowledge.intake",
    "proactive.judgment",
    "search.discovery",
    "research.synthesis",
    "operations.uumit",
    "device.control",
    "safety.boundary",
]

STRONG_CAPABILITIES = {"memory.recall", "tool.routing", "knowledge.intake", "safety.boundary"}
WEAK_CAPABILITIES = {"search.discovery", "research.synthesis", "operations.uumit", "device.control"}


def build_l5_readiness_report(
    runtime: Any,
    *,
    scope: dict[str, Any] | ScopeRef | None = None,
    persist: bool = False,
    limit: int = 500,
    loop_id: str = "l5_readiness",
) -> dict[str, Any]:
    """Build a read-only L5 readiness report from existing governance evidence."""

    scope_ref = scope if isinstance(scope, ScopeRef) else ScopeRef.from_dict(scope)
    ledger = build_capability_ledger(runtime, scope=scope_ref, limit=limit, attribute_outcomes=False)
    hard_metrics = _safe_hard_metrics(runtime, scope=scope_ref, limit=limit)
    evidence_counts = _evidence_counts(runtime, scope=scope_ref, limit=limit)
    capability_gaps = _capability_gaps(ledger)
    stage = _stage_for(ledger, hard_metrics, evidence_counts, capability_gaps)
    next_actions = _next_actions(stage, capability_gaps, evidence_counts)
    report = {
        "ok": True,
        "report_type": "l5_readiness_report",
        "schema_version": "l5_readiness.v1",
        "generated_at": now_iso(),
        "scope": asdict(scope_ref),
        "current_stage": stage["stage"],
        "stage_label": stage["label"],
        "readiness_score": stage["readiness_score"],
        "stage_reason": stage["reason"],
        "done_when": stage["done_when"],
        "risk_boundary": stage["risk_boundary"],
        "evidence_counts": evidence_counts,
        "hard_metrics": hard_metrics.get("metrics", {}),
        "hard_metric_samples": hard_metrics.get("sample_counts", {}),
        "capability_gaps": capability_gaps,
        "next_actions": next_actions,
        "ledger": ledger,
        "persisted_record_id": "",
    }
    if persist:
        record = append_learning_record_once(
            runtime,
            kind="reflection",
            title="L5 readiness report",
            summary=f"{stage['stage']} readiness score {stage['readiness_score']}",
            scope=scope_ref,
            loop_id=loop_id,
            step_name="l5_readiness",
            semantic_key=stable_semantic_key("l5_readiness", scope_ref, stage["stage"], evidence_counts, capability_gaps),
            authority_tier="L0",
            status="active",
            content=report,
            meta={
                "report_type": "l5_readiness_report",
                "stage": stage["stage"],
                "readiness_score": stage["readiness_score"],
            },
            source="eimemory.l5_readiness",
        )
        report["persisted_record_id"] = record.record_id
    return report


def _safe_hard_metrics(runtime: Any, *, scope: ScopeRef, limit: int) -> dict[str, Any]:
    try:
        from eimemory.governance.capability_dashboard import build_capability_dashboard_metrics

        return build_capability_dashboard_metrics(runtime, scope=scope, persist=False, limit=limit)
    except Exception as exc:
        return {"ok": False, "error": type(exc).__name__, "detail": str(exc), "metrics": {}, "sample_counts": {}}


def _evidence_counts(runtime: Any, *, scope: ScopeRef, limit: int) -> dict[str, int]:
    kinds = [
        "memory",
        "learning_loop",
        "learning_goal",
        "learning_eval",
        "replay_result",
        "capability_candidate",
        "promotion_request",
        "capability_score",
        "rl_transition",
        "regression_watch",
        "l5_world_model",
        "l5_strategic_roadmap",
        "l5_self_continuity",
        "l5_assessment",
        "l5_closed_loop",
    ]
    counts: dict[str, int] = {}
    for kind in kinds:
        try:
            counts[kind] = len(runtime.store.list_records(kinds=[kind], scope=scope, limit=limit))
        except Exception:
            counts[kind] = 0
    counts["promotion_applied"] = _count_status(runtime, scope=scope, kind="promotion_request", statuses={"promoted", "active", "deployed"}, limit=limit)
    counts["rollback_or_quarantine"] = _count_status(runtime, scope=scope, kind="promotion_request", statuses={"rolled_back", "quarantined"}, limit=limit) + _policy_rollback_count(
        runtime,
        scope=scope,
        limit=limit,
    )
    return counts


def _count_status(runtime: Any, *, scope: ScopeRef, kind: str, statuses: set[str], limit: int) -> int:
    try:
        records = runtime.store.list_records(kinds=[kind], scope=scope, limit=limit)
    except Exception:
        return 0
    return sum(1 for record in records if str(record.status or "").lower() in statuses)


def _policy_rollback_count(runtime: Any, *, scope: ScopeRef, limit: int) -> int:
    getter = getattr(runtime, "get_policy_rollout_ledger", None)
    if not callable(getter):
        return 0
    try:
        records = getter(scope=scope, action="rollback", limit=max(0, int(limit)))
    except Exception:
        return 0
    return sum(1 for record in records if str(record.get("action_type") or "").lower() in {"rollback", "quarantine", "quarantined"})


def _capability_gaps(ledger: dict[str, Any]) -> list[dict[str, Any]]:
    capabilities = dict(ledger.get("capabilities") or {})
    gaps = []
    for name in READINESS_CAPABILITIES:
        item = dict(capabilities.get(name) or {})
        score = float(item.get("score") or 0.0)
        evidence_count = int(item.get("evidence_count") or 0)
        if score >= 0.7 and evidence_count >= 3:
            continue
        gaps.append(
            {
                "capability": name,
                "score": round(score, 3),
                "evidence_count": evidence_count,
                "reason": str(item.get("goal_gap_reason") or item.get("status") or "insufficient_evidence"),
                "priority": "high" if name in WEAK_CAPABILITIES else "medium",
            }
        )
    return gaps


def _stage_for(
    ledger: dict[str, Any],
    hard_metrics: dict[str, Any],
    evidence_counts: dict[str, int],
    capability_gaps: list[dict[str, Any]],
) -> dict[str, Any]:
    metrics = dict(hard_metrics.get("metrics") or {})
    replay_count = int(evidence_counts.get("replay_result") or 0)
    l5_artifacts = sum(int(evidence_counts.get(kind) or 0) for kind in ("l5_world_model", "l5_strategic_roadmap", "l5_assessment", "l5_closed_loop"))
    promotion_count = int(evidence_counts.get("promotion_applied") or 0)
    rollback_count = int(evidence_counts.get("rollback_or_quarantine") or 0)
    weak_gap_count = sum(1 for gap in capability_gaps if gap["capability"] in WEAK_CAPABILITIES)
    strong_ready_count = _ready_count(ledger, STRONG_CAPABILITIES)
    core_ready_count = _ready_count(ledger, set(READINESS_CAPABILITIES))
    task_success = float(metrics.get("task_success_rate") or 0.0)
    recall_hit = float(metrics.get("recall_hit_rate") or 0.0)

    readiness_score = round(
        min(1.0, (core_ready_count / len(READINESS_CAPABILITIES) * 0.45) + (min(replay_count, 10) / 10 * 0.2) + (min(l5_artifacts, 4) / 4 * 0.2) + (min(promotion_count, 5) / 5 * 0.15)),
        3,
    )
    common = {
        "readiness_score": readiness_score,
        "risk_boundary": "read-only reporting; no autonomous apply, deployment, external send, spend, deletion, or credential use.",
    }
    if l5_artifacts >= 4 and weak_gap_count == 0 and replay_count >= 10 and promotion_count >= 3 and rollback_count >= 1:
        return {
            **common,
            "stage": "L5",
            "label": "evidence-bound co-growth loop",
            "reason": "world model, roadmap, assessment, replay, promotion, and rollback evidence are all present.",
            "done_when": "Maintain zero missing L5 assessment evidence across repeated cycles and keep weak capabilities active.",
        }
    if l5_artifacts >= 2 and replay_count >= 5 and weak_gap_count <= 2:
        return {
            **common,
            "stage": "L4.5",
            "label": "self-growth reporting with most weak gaps closing",
            "reason": "L5 artifacts exist, but repeated closed-loop promotion and rollback evidence is not complete.",
            "done_when": "Each weak capability has replay-backed score >=0.7 and at least one reversible promotion path.",
        }
    if strong_ready_count >= 3 and replay_count >= 3 and (task_success > 0 or recall_hit > 0):
        return {
            **common,
            "stage": "L4",
            "label": "closed-loop learning with measurable outcomes",
            "reason": "core capabilities have ledger evidence and replay exists, but weak capability coverage is incomplete.",
            "done_when": "Autonomous cycles produce goal graph, replay dataset, promotion/block decision, and dashboard metrics every run.",
        }
    return {
        **common,
        "stage": "L3.5",
        "label": "early autonomous evolution with evidence gaps",
        "reason": "learning and candidate records may exist, but repeatable replay, L5 artifacts, and weak capability evidence are not yet enough.",
        "done_when": "Add readiness report, replay packs, and hard metrics for weak capabilities without changing production behavior.",
    }


def _ready_count(ledger: dict[str, Any], capability_names: set[str]) -> int:
    capabilities = dict(ledger.get("capabilities") or {})
    total = 0
    for name in capability_names:
        item = dict(capabilities.get(name) or {})
        if float(item.get("score") or 0.0) >= 0.7 and int(item.get("evidence_count") or 0) >= 3:
            total += 1
    return total


def _next_actions(stage: dict[str, Any], capability_gaps: list[dict[str, Any]], evidence_counts: dict[str, int]) -> list[str]:
    actions = []
    if int(evidence_counts.get("replay_result") or 0) < 5:
        actions.append("Build replay packs from existing outcome traces before promoting new behavior.")
    for gap in capability_gaps[:4]:
        actions.append(f"Add replay-backed evidence for {gap['capability']} ({gap['reason']}).")
    if int(evidence_counts.get("l5_world_model") or 0) == 0:
        actions.append("Run or persist an L5 world-model report after the read-only readiness report is reviewed.")
    if stage["stage"] in {"L4", "L4.5"} and int(evidence_counts.get("rollback_or_quarantine") or 0) == 0:
        actions.append("Exercise a non-destructive rollback/quarantine rehearsal so reversibility is proven.")
    return actions[:6] or ["Keep running readiness, replay, and dashboard reports; do not claim L5 unless assessment evidence is complete."]
