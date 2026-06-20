from __future__ import annotations

from dataclasses import asdict
from typing import Any

from eimemory.models.records import ScopeRef


LIFECYCLE_DETAIL_FIELDS = {
    "candidate_id": "",
    "patch_id": "",
    "commit_sha": "",
    "release_path": "",
    "test_result": {},
    "health_result": {},
    "rollback_command": "",
    "observed_count": 0,
    "failure_rate": 0.0,
}


def record_lifecycle_event(
    runtime: Any,
    *,
    scope: dict[str, Any] | ScopeRef | None,
    action_type: str,
    candidate_id: str,
    promotion_id: str = "",
    patch_id: str = "",
    commit_sha: str = "",
    release_path: str = "",
    test_result: dict[str, Any] | None = None,
    health_result: dict[str, Any] | None = None,
    rollback_command: str = "",
    observed_count: int = 0,
    failure_rate: float = 0.0,
    source_opportunity: dict[str, Any] | None = None,
    trust_report: dict[str, Any] | None = None,
    replay_report: dict[str, Any] | None = None,
    reason: str = "",
    details: dict[str, Any] | None = None,
    applied_artifact_id: str = "",
    budget_decision: str = "ok",
) -> dict[str, Any]:
    sqlite = getattr(getattr(runtime, "store", None), "sqlite", None)
    record_ledger = getattr(sqlite, "_record_policy_rollout_ledger", None)
    if not callable(record_ledger):
        return {"ok": False, "error": "rollout_ledger_unavailable"}
    scope_ref = scope if isinstance(scope, ScopeRef) else ScopeRef.from_dict(scope)
    normalized_details = standardized_lifecycle_details(
        candidate_id=candidate_id,
        patch_id=patch_id,
        commit_sha=commit_sha,
        release_path=release_path,
        test_result=test_result or {},
        health_result=health_result or {},
        rollback_command=rollback_command,
        observed_count=observed_count,
        failure_rate=failure_rate,
        extra=details or {},
    )
    source = {
        "candidate_id": str(candidate_id or ""),
        "patch_id": str(patch_id or ""),
        "action_type": str(action_type or ""),
        **dict(source_opportunity or {}),
    }
    ledger = record_ledger(
        action_type=str(action_type),
        scope=scope_ref,
        promotion_id=str(promotion_id or candidate_id or action_type),
        source_opportunity_id=str(candidate_id or ""),
        source_opportunity=_jsonable(source),
        trust_report=_jsonable(trust_report or {}),
        replay_report=_jsonable(replay_report or {}),
        is_auto=True,
        applied_pattern_id=str(applied_artifact_id or ""),
        budget_decision=str(budget_decision or "ok"),
        reason=str(reason or ""),
        details=_jsonable(normalized_details),
    )
    sqlite.conn.commit()
    return {"ok": True, **ledger}


def standardized_lifecycle_details(
    *,
    candidate_id: str,
    patch_id: str = "",
    commit_sha: str = "",
    release_path: str = "",
    test_result: dict[str, Any] | None = None,
    health_result: dict[str, Any] | None = None,
    rollback_command: str = "",
    observed_count: int = 0,
    failure_rate: float = 0.0,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    details = {
        **LIFECYCLE_DETAIL_FIELDS,
        **dict(extra or {}),
        "candidate_id": str(candidate_id or ""),
        "patch_id": str(patch_id or ""),
        "commit_sha": str(commit_sha or ""),
        "release_path": str(release_path or ""),
        "test_result": dict(test_result or {}),
        "health_result": dict(health_result or {}),
        "rollback_command": str(rollback_command or ""),
        "observed_count": int(observed_count or 0),
        "failure_rate": round(float(failure_rate or 0.0), 6),
    }
    return details


def _jsonable(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, ScopeRef):
        return asdict(value)
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(item) for item in value]
    return str(value)
