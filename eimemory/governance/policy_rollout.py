from __future__ import annotations

import json
from datetime import datetime, timezone
from hashlib import sha256
from typing import Any

from eimemory.models.records import ScopeRef

PATTERN_STATUS_CANDIDATE = "candidate"
PATTERN_STATUS_SHADOW = "shadow"
PATTERN_STATUS_ACTIVE = "active"
PATTERN_STATUS_ROLLED_BACK = "rolled_back"
PATTERN_STATUS_QUARANTINED = "quarantined"
PATTERN_STATUSES = {
    PATTERN_STATUS_CANDIDATE,
    PATTERN_STATUS_SHADOW,
    PATTERN_STATUS_ACTIVE,
    PATTERN_STATUS_ROLLED_BACK,
    PATTERN_STATUS_QUARANTINED,
}

AUTO_PROMOTION_BUDGET_PER_DAY = 3
AUTO_ROLLBACK_BUDGET_PER_DAY = 5

IMMEDIATE_ROLLBACK_MARKERS = (
    "不是这个意思",
    "别这样",
    "不要这样",
    "别再",
    "不要再",
    "别这样做",
)


REPEATED_BAD_OUTCOME_THRESHOLD = 2


def normalize_pattern_status(value: Any, *, default: str = PATTERN_STATUS_ACTIVE) -> str:
    status = str(value or default).strip().lower()
    if status in PATTERN_STATUSES:
        return status
    return default


def should_include_pattern_status(status: str, *, include_shadow: bool) -> bool:
    normalized = normalize_pattern_status(status)
    if normalized == PATTERN_STATUS_ACTIVE:
        return True
    if include_shadow and normalized == PATTERN_STATUS_SHADOW:
        return True
    return False


def extract_pattern_ids_from_outcome(payload: dict[str, Any]) -> list[str]:
    raw = payload.get("policy_attribution")
    if isinstance(raw, dict):
        candidates: list[str] = []
        raw_candidates = raw.get("policy_suggestion_ids")
        if isinstance(raw_candidates, (list, tuple, set)):
            candidates.extend(str(item).strip() for item in raw_candidates if str(item).strip())
        if isinstance(raw_candidates, str):
            candidates.append(str(raw_candidates).strip())
        for key in ("policy_id", "pattern_id"):
            candidate = raw.get(key)
            if candidate:
                candidates.append(str(candidate))
        policy_updates = raw.get("policy_updates")
        if isinstance(policy_updates, (list, tuple, set)):
            for item in policy_updates:
                if isinstance(item, dict):
                    for key in ("policy_id", "id"):
                        if item.get(key):
                            candidates.append(str(item.get(key)).strip())
        return _dedupe([item for item in candidates if item])

    ids: list[str] = []
    raw_list = payload.get("policy_suggestion_ids")
    if isinstance(raw_list, (list, tuple, set)):
        ids.extend(str(item).strip() for item in raw_list if str(item).strip())
    elif isinstance(raw_list, str):
        ids.append(str(raw_list).strip())

    for key in ("pattern_id", "pattern_ids", "policy_id"):
        raw_value = payload.get(key)
        if isinstance(raw_value, (list, tuple, set)):
            ids.extend(str(item).strip() for item in raw_value if str(item).strip())
        elif isinstance(raw_value, str):
            ids.append(str(raw_value).strip())

    return _dedupe([item for item in ids if item])


def outcome_triggers_immediate_rollback(payload: dict[str, Any]) -> bool:
    correction = str(payload.get("correction_from_user") or "").strip()
    if not correction:
        return False
    lowered = correction.lower()
    for marker in IMMEDIATE_ROLLBACK_MARKERS:
        if marker in lowered:
            return True
    return False


def should_auto_rollback_from_repeated_bad_outcomes(*, bad_outcome_count: int, threshold: int = REPEATED_BAD_OUTCOME_THRESHOLD) -> bool:
    try:
        count = int(bad_outcome_count)
    except (TypeError, ValueError):
        count = 0
    return count >= int(threshold)


def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def today_utc() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def daily_rollout_count(*, conn, scope: ScopeRef, action: str, auto_only: bool = True, date: str | None = None) -> int:
    lookup_date = date or today_utc()
    where = [
        "tenant_id = ?",
        "agent_id = ?",
        "workspace_id = ?",
        "user_id = ?",
        "record_date = ?",
        "action_type = ?",
    ]
    params: list[Any] = [
        scope.tenant_id,
        scope.agent_id,
        scope.workspace_id,
        scope.user_id,
        lookup_date,
        action,
    ]
    if auto_only:
        where.append("is_auto = 1")
    row = conn.execute(
        f"SELECT COUNT(*) AS total FROM policy_rollout_ledger WHERE {' AND '.join(where)}",
        params,
    ).fetchone()
    return int(row["total"]) if row is not None else 0


def budget_decision_for_promotion(*, conn, scope: ScopeRef, auto: bool, budget_limit: int = AUTO_PROMOTION_BUDGET_PER_DAY, date: str | None = None) -> str:
    if not auto:
        return "manual_ok"
    used = daily_rollout_count(conn=conn, scope=scope, action="promotion", auto_only=True, date=date)
    if used >= int(budget_limit):
        return "budget_exhausted"
    return "ok"


def budget_decision_for_rollback(*, conn, scope: ScopeRef, auto: bool, budget_limit: int = AUTO_ROLLBACK_BUDGET_PER_DAY, date: str | None = None) -> str:
    if not auto:
        return "manual_ok"
    used = daily_rollout_count(conn=conn, scope=scope, action="rollback", auto_only=True, date=date)
    if used >= int(budget_limit):
        return "budget_exhausted"
    return "ok"


def next_rollout_id(*, kind: str, scope: ScopeRef, payload: dict[str, Any]) -> str:
    stable = {
        "kind": str(kind or "") ,
        "tenant_id": scope.tenant_id,
        "agent_id": scope.agent_id,
        "workspace_id": scope.workspace_id,
        "user_id": scope.user_id,
        "payload": payload,
    }
    digest = sha256(json.dumps(stable, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()[:16]
    return f"{kind}_{digest}"


def build_rollout_ledger_record(
    *,
    promotion_id: str,
    source_opportunity: dict[str, Any],
    trust_gate_report: dict[str, Any],
    replay_gate_report: dict[str, Any],
    applied_pattern_id: str,
    budget_decision: str,
    rollback_policy_id: str = "",
    action: str = "promotion",
    scope: ScopeRef,
    is_auto: bool = True,
    reason: str | None = None,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "promotion_id": str(promotion_id),
        "source_opportunity": dict(source_opportunity or {}),
        "trust_report": dict(trust_gate_report or {}),
        "replay_report": dict(replay_gate_report or {}),
        "applied_pattern_id": str(applied_pattern_id),
        "rollback_policy_id": str(rollback_policy_id or ""),
        "budget_decision": str(budget_decision or "manual_ok"),
        "action_type": str(action),
        "scope": {
            "tenant_id": scope.tenant_id,
            "agent_id": scope.agent_id,
            "workspace_id": scope.workspace_id,
            "user_id": scope.user_id,
        },
        "is_auto": bool(is_auto),
        "details": dict(details or {}),
        "reason": str(reason or ""),
    }


def follow_up_opportunities_from_rollback(
    *,
    pattern_id: str,
    event_id: str,
    reason: str,
    source: str,
    scope: ScopeRef,
) -> list[dict[str, Any]]:
    opportunity_id = next_rollout_id(
        kind="policy-rollback-followup",
        scope=scope,
        payload={"pattern_id": pattern_id, "event_id": event_id},
    )
    return [
        {
            "opportunity_id": opportunity_id,
            "opportunity_type": "policy_follow_up",
            "source": "policy_rollout",
            "source_event_id": str(event_id),
            "event_type": "policy_rollout",
            "trigger": f"Rollback pattern {pattern_id}",
            "risk_level": "low",
            "policy_update": f"回滚触发：{reason}",
            "correction_from_user": str(reason),
            "outcome_reason": str(reason),
            "recorded_at": now_utc(),
            "scope": {
                "tenant_id": scope.tenant_id,
                "agent_id": scope.agent_id,
                "workspace_id": scope.workspace_id,
                "user_id": scope.user_id,
            },
            "policy_source": str(source),
            "source_opportunity": "rollback",
            "pattern_id": str(pattern_id),
            "source_pattern_id": str(pattern_id),
        }
    ]


def build_follow_up_opportunities_from_rollback(
    *,
    pattern_id: str,
    event_id: str,
    reason: str,
    source: str,
    scope: ScopeRef,
) -> list[dict[str, Any]]:
    return follow_up_opportunities_from_rollback(
        pattern_id=pattern_id,
        event_id=event_id,
        reason=reason,
        source=source,
        scope=scope,
    )


def _dedupe(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for item in values:
        normalized = str(item or "").strip()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        result.append(normalized)
    return result


def _json_dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)
