from __future__ import annotations

from dataclasses import asdict
import json
from typing import Any

from eimemory.governance.learning_state import append_learning_record_once, stable_semantic_key
from eimemory.models.records import ScopeRef


SUCCESS_LABELS = {
    "1",
    "true",
    "yes",
    "ok",
    "good",
    "success",
    "succeeded",
    "pass",
    "passed",
    "complete",
    "completed",
    "delivered",
    "done",
    "verified",
    "health_ok",
    "all_ok",
    "ready",
    "readyz_ok",
}
FAILURE_LABELS = {
    "0",
    "false",
    "no",
    "bad",
    "fail",
    "failed",
    "failure",
    "error",
    "errored",
    "timeout",
    "timed_out",
    "rollback",
    "rolled_back",
    "quarantined",
    "verification_missing",
    "missing_verification",
}


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
    task_outcomes = [
        record
        for record in task_evals + _outcome_trace_records(runtime, scope_ref, limit) + _event_outcome_records(runtime, scope_ref, limit)
        if not _truthy(_field(record, "rehearsal"))
    ]
    task_success = sum(1 for record in task_outcomes if _outcome_success(record))

    promotions = _records(runtime, scope_ref, ["promotion_request"], limit)
    patch_promotions = [
        record
        for record in promotions
        if str(_field(record, "promotion_target") or "").lower() == "code_patch"
        or str(_field(record, "action") or "").lower() == "code_patch"
    ]
    patch_success = sum(1 for record in patch_promotions if _verified_code_patch_promotion(record))
    policy_rollbacks = _policy_rollback_records(runtime, scope_ref, limit)
    rollback_count = len(policy_rollbacks)
    skill_invocations = sum(1 for record in evals if str(_field(record, "report_type") or "") == "eiskill_invocation")
    skill_registry_reuse = sum(
        max(0, _int(_field(record, "reuse_count")))
        for record in _records(runtime, scope_ref, ["learning_playbook"], limit)
        if str(_field(record, "report_type") or "") == "eiskill_registry_entry"
    )
    skill_reuse_count = max(skill_invocations, skill_registry_reuse)

    patch_promotion_success_rate = _rate(patch_success, len(patch_promotions))
    patch_metric_quality = _quality(len(patch_promotions))
    metrics = {
        "recall_hit_rate": _rate(recall_hits, recall_total),
        "user_correction_rate": _rate(len(corrections), recall_total),
        "task_success_rate": _rate(task_success, len(task_outcomes)),
        "patch_promotion_success_rate": patch_promotion_success_rate,
        "auto_patch_success_rate": patch_promotion_success_rate,
        "rollback_count": rollback_count,
        "skill_reuse_count": skill_reuse_count,
    }
    metric_quality = {
        "recall_hit_rate": _quality(recall_total),
        "user_correction_rate": _quality(recall_total),
        "task_success_rate": _quality(len(task_outcomes)),
        "patch_promotion_success_rate": patch_metric_quality,
        "auto_patch_success_rate": patch_metric_quality,
        "rollback_count": _quality(len(policy_rollbacks), minimum=1),
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
                f"patch_success_rate={metrics['patch_promotion_success_rate']}"
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
            "task_outcomes": len(task_outcomes),
            "patch_promotions": len(patch_promotions),
            "policy_rollbacks": len(policy_rollbacks),
        },
    }


def _records(runtime: Any, scope: ScopeRef, kinds: list[str], limit: int) -> list[Any]:
    return runtime.store.list_records(kinds=kinds, scope=scope, limit=max(0, int(limit)))


def _outcome_trace_records(runtime: Any, scope: ScopeRef, limit: int) -> list[Any]:
    records = _records(runtime, scope, ["reflection"], limit)
    return [
        record
        for record in records
        if str(_field(record, "report_type") or "") == "outcome_trace" and _has_outcome_signal(record)
    ]


def _event_outcome_records(runtime: Any, scope: ScopeRef, limit: int) -> list[dict[str, Any]]:
    sqlite = getattr(getattr(runtime, "store", None), "sqlite", None)
    conn = getattr(sqlite, "conn", None)
    if conn is None:
        return []
    rows = conn.execute(
        """
        SELECT payload_json
        FROM event_outcomes
        WHERE tenant_id = ?
          AND agent_id = ?
          AND workspace_id = ?
          AND user_id = ?
        ORDER BY recorded_at DESC
        LIMIT ?
        """,
        (scope.tenant_id, scope.agent_id, scope.workspace_id, scope.user_id, max(0, int(limit))),
    ).fetchall()
    outcomes: list[dict[str, Any]] = []
    for row in rows:
        try:
            payload = json.loads(str(row["payload_json"] or "{}"))
        except (TypeError, json.JSONDecodeError):
            continue
        payload.setdefault("report_type", "event_outcome")
        outcomes.append(payload)
    return outcomes


def _policy_rollback_records(runtime: Any, scope: ScopeRef, limit: int) -> list[dict[str, Any]]:
    getter = getattr(runtime, "get_policy_rollout_ledger", None)
    if not callable(getter):
        return []
    try:
        records = getter(scope=scope, limit=max(0, int(limit)))
    except Exception:
        return []
    return [record for record in records if str(record.get("action_type") or "").lower() in {"rollback", "rolled_back", "quarantine", "quarantined"}]


def _field(record: Any, key: str) -> Any:
    if isinstance(record, dict):
        if key in record:
            return record.get(key)
        meta = record.get("meta")
        content = record.get("content")
        for payload in (meta, content):
            if isinstance(payload, dict) and key in payload:
                return payload.get(key)
        return None
    for payload in (getattr(record, "meta", {}) or {}, getattr(record, "content", {}) or {}):
        if isinstance(payload, dict) and key in payload:
            return payload.get(key)
    return None


def _verified_code_patch_promotion(record: Any) -> bool:
    status = str(record.get("status", "") if isinstance(record, dict) else getattr(record, "status", "") or "").lower()
    if status not in {"promoted", "active", "deployed"}:
        return False
    content = record.get("content") if isinstance(record, dict) else getattr(record, "content", {})
    meta = record.get("meta") if isinstance(record, dict) else getattr(record, "meta", {})
    content = content if isinstance(content, dict) else {}
    meta = meta if isinstance(meta, dict) else {}
    gate = content.get("gate") if isinstance(content.get("gate"), dict) else {}
    side_effect = content.get("side_effect") if isinstance(content.get("side_effect"), dict) else {}
    verification = side_effect.get("verification") if isinstance(side_effect.get("verification"), dict) else {}
    health = side_effect.get("post_deploy_health") if isinstance(side_effect.get("post_deploy_health"), dict) else {}
    commit = side_effect.get("commit") if isinstance(side_effect.get("commit"), dict) else {}
    rollback = side_effect.get("rollback_evidence") if isinstance(side_effect.get("rollback_evidence"), dict) else {}
    return bool(
        (gate.get("ok") is True or meta.get("gate_ok") is True)
        and side_effect.get("ok") is True
        and side_effect.get("production_applied") is True
        and verification.get("ok") is True
        and verification.get("skipped") is not True
        and health.get("ok") is True
        and health.get("skipped") is not True
        and str(commit.get("commit_sha") or meta.get("commit_sha") or "").strip()
        and rollback
    )


def _has_key(record: Any, key: str) -> bool:
    return _field(record, key) is not None


def _capability(record: Any) -> str:
    return str(_field(record, "capability") or _field(record, "target_capability") or "")


def _verdict(record: Any) -> str:
    status = record.get("status", "") if isinstance(record, dict) else getattr(record, "status", "")
    return str(_field(record, "verdict") or status or "").lower()


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "pass", "passed", "success"}


def _has_outcome_signal(record: Any) -> bool:
    return any(
        _field(record, key) is not None
        for key in (
            "task_success",
            "outcome",
            "status",
            "result",
            "ok",
            "success",
            "verified",
            "verification",
            "verdict",
        )
    )


def _outcome_success(record: Any) -> bool:
    task_success = _field(record, "task_success")
    if task_success is not None:
        return _truthy(task_success)
    outcome = _field(record, "outcome")
    labels: list[str] = []
    bool_values: list[Any] = []
    if isinstance(outcome, dict):
        labels.extend(str(outcome.get(key) or "").strip().lower() for key in ("status", "outcome", "result"))
        for key in ("success", "verified", "ok"):
            if key in outcome:
                bool_values.append(outcome.get(key))
    elif outcome is not None:
        labels.append(str(outcome or "").strip().lower())
    labels.extend(str(_field(record, key) or "").strip().lower() for key in ("status", "result", "verdict"))
    for label in labels:
        if label in FAILURE_LABELS:
            return False
    for value in bool_values:
        return _truthy(value)
    for key in ("success", "verified", "ok"):
        value = _field(record, key)
        if value is not None:
            return _truthy(value)
    for label in labels:
        if label in SUCCESS_LABELS:
            return True
    return _verdict(record) in SUCCESS_LABELS


def _rate(numerator: int, denominator: int) -> float:
    return round(numerator / denominator, 3) if denominator else 0.0


def _quality(sample_count: int, *, minimum: int = 10) -> dict[str, Any]:
    count = max(0, int(sample_count or 0))
    return {
        "sample_count": count,
        "minimum": minimum,
        "sufficient": count >= minimum,
    }


def _int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0
