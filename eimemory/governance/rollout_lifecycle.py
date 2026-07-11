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

ROLLBACK_ACTION_TYPES = {"rollback", "rolled_back", "quarantine", "quarantined"}


def is_executed_rollback_ledger_record(item: dict[str, Any]) -> bool:
    action = str(item.get("action_type") or "").strip().lower()
    if action not in ROLLBACK_ACTION_TYPES:
        return False
    details = item.get("details") if isinstance(item.get("details"), dict) else {}
    if details.get("blocked") is True:
        return False
    rollback = details.get("rollback") if isinstance(details.get("rollback"), dict) else {}
    side_effect = details.get("side_effect") if isinstance(details.get("side_effect"), dict) else {}
    side_rollback = side_effect.get("rollback") if isinstance(side_effect.get("rollback"), dict) else {}
    execution = rollback or side_rollback
    if execution.get("ok") is not True or execution.get("skipped") is True:
        return False
    top_level_identities = _rollback_ledger_identities(item)
    if not top_level_identities:
        return False
    if action in {"rollback", "quarantine"}:
        if str(item.get("budget_decision") or "").strip().lower() not in {"ok", "manual_ok"}:
            return False
        if not str(item.get("applied_pattern_id") or "").strip():
            return False
    return _has_verifiable_rollback_execution(
        execution,
        top_level_identities=top_level_identities,
    )


def _has_verifiable_rollback_execution(
    execution: dict[str, Any],
    *,
    top_level_identities: set[str],
) -> bool:
    transition = execution.get("status_transition") if isinstance(execution.get("status_transition"), dict) else {}
    previous = str(transition.get("from") or "").strip()
    current = str(transition.get("to") or "").strip().lower()
    transition_artifact = str(
        transition.get("pattern_id")
        or transition.get("candidate_id")
        or transition.get("artifact_id")
        or ""
    ).strip()
    if transition_artifact and transition_artifact not in top_level_identities:
        return False
    if previous and current in {"rolled_back", "quarantined"} and previous.lower() != current:
        if transition_artifact:
            return True

    file_restore = execution.get("file_restore") if isinstance(execution.get("file_restore"), dict) else {}
    if file_restore.get("ok") is True and int(file_restore.get("restored_count") or 0) > 0:
        return True
    if _executed_command_report(execution.get("command_report")):
        return True
    repo_reset = execution.get("repo_reset") if isinstance(execution.get("repo_reset"), dict) else {}
    if (
        repo_reset.get("ok") is True
        and repo_reset.get("skipped") is not True
        and str(repo_reset.get("prior_commit_sha") or "").strip()
        and _reports_include_success(repo_reset.get("reports"))
    ):
        return True
    return False


def _rollback_ledger_identities(item: dict[str, Any]) -> set[str]:
    source = item.get("source_opportunity") if isinstance(item.get("source_opportunity"), dict) else {}
    values = (
        item.get("applied_pattern_id"),
        item.get("source_opportunity_id"),
        item.get("rollback_policy_id"),
        source.get("pattern_id"),
        source.get("candidate_id"),
        source.get("opportunity_id"),
    )
    return {str(value).strip() for value in values if str(value or "").strip()}


def _executed_command_report(value: Any) -> bool:
    report = value if isinstance(value, dict) else {}
    return bool(
        report.get("ok") is True
        and report.get("skipped") is not True
        and _reports_include_success(report.get("reports"))
    )


def _reports_include_success(value: Any) -> bool:
    return any(
        isinstance(report, dict)
        and report.get("ok") is True
        and report.get("returncode") == 0
        and bool(report.get("command"))
        for report in list(value or [])
    )


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
