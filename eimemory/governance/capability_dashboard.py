from __future__ import annotations

from dataclasses import asdict
from typing import Any

from eimemory.governance.learning_state import append_learning_record_once, stable_semantic_key
from eimemory.models.records import ScopeRef


def build_capability_dashboard_metrics(
    runtime: Any,
    *,
    scope: dict[str, Any] | ScopeRef | None = None,
    persist: bool = False,
    limit: int = 500,
    loop_id: str = "capability_dashboard_1_6_9",
) -> dict[str, Any]:
    scope_ref = scope if isinstance(scope, ScopeRef) else ScopeRef.from_dict(scope)
    recall_replays = _records(runtime, scope_ref, ["replay_result"], limit)
    recall_items = [record for record in recall_replays if _capability(record) == "memory.recall" or _has_key(record, "hit")]
    recall_hits = sum(1 for record in recall_items if _truthy(_field(record, "hit")) or _verdict(record) == "pass")
    recall_total = len(recall_items)

    corrections = [
        record
        for record in _records(runtime, scope_ref, ["feedback", "incident", "reflection"], limit)
        if str(_field(record, "report_type") or "").lower() in {"user_correction", "operator_correction"}
        or "correction" in (record.title + " " + record.summary).lower()
    ]

    evals = _records(runtime, scope_ref, ["learning_eval"], limit)
    task_evals = [record for record in evals if _field(record, "task_success") is not None]
    task_success = sum(1 for record in task_evals if _truthy(_field(record, "task_success")) or _verdict(record) == "pass")

    promotions = _records(runtime, scope_ref, ["promotion_request"], limit)
    patch_promotions = [
        record
        for record in promotions
        if str(_field(record, "promotion_target") or "").lower() == "code_patch"
        or str(_field(record, "action") or "").lower() in {"promote", "rollback", "code_patch"}
    ]
    patch_success = sum(1 for record in patch_promotions if str(record.status or "").lower() in {"promoted", "active", "deployed"})
    rollback_count = sum(1 for record in promotions if str(record.status or "").lower() in {"rolled_back", "quarantined"} or str(_field(record, "action") or "").lower() == "rollback")
    skill_reuse_count = sum(1 for record in evals if str(_field(record, "report_type") or "") == "eiskill_invocation")

    metrics = {
        "recall_hit_rate": _rate(recall_hits, recall_total),
        "user_correction_rate": _rate(len(corrections), recall_total),
        "task_success_rate": _rate(task_success, len(task_evals)),
        "auto_patch_success_rate": _rate(patch_success, len(patch_promotions)),
        "rollback_count": rollback_count,
        "skill_reuse_count": skill_reuse_count,
    }
    metric_quality = {
        "recall_hit_rate": _quality(recall_total),
        "user_correction_rate": _quality(recall_total),
        "task_success_rate": _quality(len(task_evals)),
        "auto_patch_success_rate": _quality(len(patch_promotions)),
        "rollback_count": _quality(len(promotions), minimum=1),
        "skill_reuse_count": _quality(skill_reuse_count, minimum=1),
    }
    record_id = ""
    if persist:
        record = append_learning_record_once(
            runtime,
            kind="reflection",
            title="Capability dashboard hard metrics",
            summary=(
                f"recall_hit_rate={metrics['recall_hit_rate']}; "
                f"task_success_rate={metrics['task_success_rate']}; "
                f"patch_success_rate={metrics['auto_patch_success_rate']}"
            ),
            scope=scope_ref,
            loop_id=loop_id,
            step_name="capability_dashboard_metrics",
            semantic_key=stable_semantic_key("capability_dashboard_metrics", scope_ref, metrics),
            authority_tier="L0",
            status="active",
            content={"report_type": "capability_dashboard_metrics", "metrics": metrics, "metric_quality": metric_quality},
            meta={"report_type": "capability_dashboard_metrics", **metrics, "metric_quality": metric_quality},
            source="eimemory.capability_dashboard",
        )
        record_id = record.record_id
    return {
        "ok": True,
        "report_type": "capability_dashboard_metrics",
        "scope": asdict(scope_ref),
        "metrics": metrics,
        "metric_quality": metric_quality,
        "persisted_record_id": record_id,
        "sample_counts": {
            "recall": recall_total,
            "corrections": len(corrections),
            "task_evals": len(task_evals),
            "patch_promotions": len(patch_promotions),
        },
    }


def _records(runtime: Any, scope: ScopeRef, kinds: list[str], limit: int) -> list[Any]:
    return runtime.store.list_records(kinds=kinds, scope=scope, limit=max(0, int(limit)))


def _field(record: Any, key: str) -> Any:
    for payload in (getattr(record, "meta", {}) or {}, getattr(record, "content", {}) or {}):
        if isinstance(payload, dict) and key in payload:
            return payload.get(key)
    return None


def _has_key(record: Any, key: str) -> bool:
    return _field(record, key) is not None


def _capability(record: Any) -> str:
    return str(_field(record, "capability") or _field(record, "target_capability") or "")


def _verdict(record: Any) -> str:
    return str(_field(record, "verdict") or getattr(record, "status", "") or "").lower()


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "pass", "passed", "success"}


def _rate(numerator: int, denominator: int) -> float:
    return round(numerator / denominator, 3) if denominator else 0.0


def _quality(sample_count: int, *, minimum: int = 10) -> dict[str, Any]:
    count = max(0, int(sample_count or 0))
    return {
        "sample_count": count,
        "minimum": minimum,
        "sufficient": count >= minimum,
    }
