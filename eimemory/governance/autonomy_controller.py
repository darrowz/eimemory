from __future__ import annotations

from dataclasses import asdict
from typing import Any

from eimemory.governance.autonomous_learning import run_autonomous_learning_cycle as _legacy_run_autonomous_learning_cycle
from eimemory.governance.autonomy_policy import AutonomyPolicy, normalize_autonomy_policy
from eimemory.governance.evolution_pruner import PRODUCTIVE_MODULES, classify_evolution_modules
from eimemory.models.records import ScopeRef


def run_autonomy_cycle(
    runtime: Any,
    *,
    scope: dict[str, Any] | ScopeRef | None = None,
    apply: bool = False,
    dry_run: bool = False,
    full: bool = True,
    force: bool = False,
    max_goals: int = 3,
    policy: dict[str, Any] | AutonomyPolicy | None = None,
) -> dict[str, Any]:
    autonomy_policy = normalize_autonomy_policy(policy)
    scope_ref = scope if isinstance(scope, ScopeRef) else ScopeRef.from_dict(scope)
    bounded_goals = max(1, min(_int_value(max_goals, default=autonomy_policy.max_daily_goals), autonomy_policy.max_daily_goals))

    learning_report = _legacy_run_autonomous_learning_cycle(
        runtime,
        scope=scope_ref,
        apply=bool(apply),
        dry_run=bool(dry_run),
        full=bool(full),
        force=bool(force),
        max_goals=bounded_goals,
        max_promotions=autonomy_policy.max_auto_promotions,
    )
    roi_report = _safe_roi(runtime, scope=scope_ref)
    dashboard = _safe_dashboard(runtime, scope=scope_ref, persist=not bool(dry_run))
    replay_dataset = dict(learning_report.get("replay_dataset") or {})
    real_task_replay = dict(learning_report.get("real_task_replay") or {})
    promotions = list(learning_report.get("promotions") or [])
    post_promotion_watch = _post_promotion_watch_summary(runtime, scope=scope_ref)
    loop_policy = {"productive_modules": list(PRODUCTIVE_MODULES)}
    demoted_modules: list[str] = []
    if "online_evidence" in learning_report:
        pruner_report = classify_evolution_modules(online_evidence=learning_report.get("online_evidence"))
        demoted_modules = list(pruner_report.get("demote") or [])
        loop_policy.update(
            {
                "ok": bool(pruner_report.get("ok", False)),
                "keep": list(pruner_report.get("keep") or []),
                "observe": list(pruner_report.get("observe") or []),
                "demoted_modules": demoted_modules,
                "evidence_count": int(pruner_report.get("evidence_count") or 0),
            }
        )
    return {
        **learning_report,
        "ok": bool(learning_report.get("ok", False)),
        "report_type": "autonomy_cycle",
        "autonomy_policy": autonomy_policy.to_dict(),
        "loop_policy": loop_policy,
        "demoted_modules": demoted_modules,
        "rollout_radius": autonomy_policy.rollout_radius,
        "bounded_max_goals": bounded_goals,
        "replay_quality": {
            "case_count": int(replay_dataset.get("case_count") or 0),
            "filtered_count": int(replay_dataset.get("filtered_count") or 0),
            "quality_score": float(replay_dataset.get("quality_score") or 0.0),
            "target_pass_rate": float(replay_dataset.get("target_pass_rate") or autonomy_policy.min_replay_pass_rate_for_auto),
            "real_task_pass_rate": _float(real_task_replay.get("pass_rate")),
            "real_task_verdict": str(real_task_replay.get("verdict") or ""),
        },
        "promotion_control": {
            "applied_count": sum(1 for item in promotions if isinstance(item, dict) and item.get("applied")),
            "max_auto_promotions": autonomy_policy.max_auto_promotions,
            "max_auto_rollbacks": autonomy_policy.max_auto_rollbacks,
            "post_promotion_hit_window": autonomy_policy.post_promotion_hit_window,
            "post_promotion_watch": post_promotion_watch,
        },
        "roi": roi_report,
        "roi_components": dict(roi_report.get("roi_components") or {}),
        "dashboard": {
            "ok": bool(dashboard.get("ok", False)),
            "report_type": str(dashboard.get("report_type") or ""),
            "period_type": str(dashboard.get("period_type") or ""),
            "persisted_record_id": str(dashboard.get("persisted_record_id") or ""),
            "output_path": str(dashboard.get("output_path") or ""),
        },
        "scope": asdict(scope_ref),
    }


def _safe_roi(runtime: Any, *, scope: ScopeRef) -> dict[str, Any]:
    try:
        return dict(runtime.evolution.build_roi_report(scope=asdict(scope)))
    except Exception as exc:
        return {"ok": False, "error": type(exc).__name__, "detail": str(exc), "roi_components": {}}


def _safe_dashboard(runtime: Any, *, scope: ScopeRef, persist: bool) -> dict[str, Any]:
    builder = getattr(runtime, "build_learning_dashboard", None)
    if not callable(builder):
        return {"ok": False, "report_type": "autonomy_dashboard", "dashboard_skipped_reason": "unavailable"}
    try:
        return dict(builder(scope=asdict(scope), persist=persist))
    except Exception as exc:
        return {"ok": False, "report_type": "autonomy_dashboard", "error": type(exc).__name__, "detail": str(exc)}


def _post_promotion_watch_summary(runtime: Any, *, scope: ScopeRef) -> dict[str, Any]:
    try:
        records = runtime.store.list_records(kinds=["promotion_request"], scope=scope, limit=100)
    except Exception:
        records = []
    observing = 0
    active = 0
    quarantined = 0
    rolled_back = 0
    for record in records:
        content = record.content if isinstance(record.content, dict) else {}
        meta = record.meta if isinstance(record.meta, dict) else {}
        status = str(content.get("post_promotion_status") or meta.get("post_promotion_status") or record.status or "").lower()
        if status in {"shadow_observe", "observing"}:
            observing += 1
        elif status == "active":
            active += 1
        elif status == "quarantined":
            quarantined += 1
        elif status in {"rolled_back", "rollback"}:
            rolled_back += 1
    return {
        "observing_count": observing,
        "active_count": active,
        "quarantined_count": quarantined,
        "rolled_back_count": rolled_back,
    }


def _float(value: Any) -> float:
    try:
        return round(float(value), 3)
    except (TypeError, ValueError):
        return 0.0


def _int_value(value: Any, *, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return int(default)
