from __future__ import annotations

import json
from dataclasses import asdict
from typing import Any

from eimemory.events import normalize_scope
from eimemory.governance.policy_rollout import extract_pattern_ids_from_outcome, next_rollout_id, now_utc
from eimemory.governance.rollout_lifecycle import record_lifecycle_event
from eimemory.models.records import RecordEnvelope, ScopeRef


REQUIRED_OBSERVATIONS = 3
WATCH_STATUS = "shadow_observe"


def initialize_promotion_watch(
    runtime: Any,
    *,
    candidate: RecordEnvelope,
    scope: dict[str, Any] | ScopeRef | None,
    promotion_request_id: str,
    applied_pattern_ids: list[str],
) -> dict[str, Any]:
    initialized: list[dict[str, Any]] = []
    for pattern_id in applied_pattern_ids:
        pattern = _load_pattern(runtime, pattern_id=str(pattern_id), scope=scope or candidate.scope)
        if not pattern:
            continue
        watch = _initial_watch(
            candidate_id=candidate.record_id,
            promotion_request_id=promotion_request_id,
            pattern_id=str(pattern_id),
        )
        pattern["status"] = "shadow"
        pattern["post_promotion_watch"] = watch
        _write_pattern(runtime, pattern, scope=scope or candidate.scope)
        initialized.append({"pattern_id": str(pattern_id), "status": WATCH_STATUS})
    return {"status": WATCH_STATUS, "patterns": initialized, "required_observations": REQUIRED_OBSERVATIONS}


def record_outcome_observations(
    runtime: Any,
    *,
    event_id: str,
    outcome_payload: dict[str, Any],
    scope: dict[str, Any] | ScopeRef | None = None,
) -> list[dict[str, Any]]:
    reports: list[dict[str, Any]] = []
    attribution = _outcome_policy_attribution(runtime, event_id=event_id, outcome_payload=outcome_payload, scope=scope)
    for pattern_id in attribution["pattern_ids"]:
        pattern = _load_pattern(runtime, pattern_id=pattern_id, scope=scope)
        watch = dict((pattern or {}).get("post_promotion_watch") or {})
        if watch.get("status") != WATCH_STATUS:
            continue
        details = {
            "outcome_id": str(outcome_payload.get("id") or ""),
            "outcome_event_id": str(event_id or outcome_payload.get("event_id") or ""),
            "outcome_trace_id": str(outcome_payload.get("trace_id") or ""),
            "audit_record_id": str(attribution.get("audit_record_id") or ""),
            "selected_records": list(attribution.get("selected_records") or []),
        }
        reports.append(
            record_promotion_observation(
                runtime,
                pattern_id=pattern_id,
                scope=scope,
                event_id=event_id,
                hit=True,
                improved=_improved_from_outcome(outcome_payload),
                outcome=str(outcome_payload.get("outcome") or ""),
                reason=str(outcome_payload.get("reason") or outcome_payload.get("correction_from_user") or ""),
                details=details,
            )
        )
    return reports


def record_promotion_observation(
    runtime: Any,
    *,
    pattern_id: str,
    scope: dict[str, Any] | ScopeRef | None = None,
    event_id: str = "",
    hit: bool,
    improved: bool | None = None,
    outcome: str = "uncertain",
    reason: str = "",
    regressed: bool = False,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    pattern = _load_pattern(runtime, pattern_id=pattern_id, scope=scope)
    if not pattern:
        return {"ok": False, "status": "not_found", "pattern_id": str(pattern_id)}

    watch = _watch_state(pattern, pattern_id=str(pattern_id))
    if watch.get("status") in {"active", "quarantined", "rolled_back"}:
        return {"ok": True, "status": str(watch["status"]), "pattern_id": str(pattern_id), "watch": watch}

    outcome_status = str(outcome or "uncertain").strip().lower()
    improved_value = _coerce_improved(improved, outcome=outcome_status)
    regressed_value = bool(regressed or outcome_status == "bad")
    observation = {
        "event_id": str(event_id or next_rollout_id(kind="promotion-observation", scope=_scope(scope), payload={"pattern_id": str(pattern_id), "count": int(watch.get("observed_count") or 0)})),
        "hit": bool(hit),
        "improved": bool(improved_value),
        "regressed": bool(regressed_value),
        "outcome": outcome_status,
        "reason": str(reason or ""),
        "details": dict(details or {}),
        "observed_at": now_utc(),
    }
    existing_ids = {str(item.get("event_id") or "") for item in watch.get("observations") or [] if isinstance(item, dict)}
    if observation["event_id"] not in existing_ids:
        observations = [item for item in watch.get("observations") or [] if isinstance(item, dict)]
        observations.append(observation)
        watch["observations"] = observations[-REQUIRED_OBSERVATIONS:]
        watch["observed_count"] = int(watch.get("observed_count") or 0) + 1
        if observation["hit"]:
            watch["hit_count"] = int(watch.get("hit_count") or 0) + 1
        if observation["improved"]:
            watch["improvement_count"] = int(watch.get("improvement_count") or 0) + 1
        if observation["regressed"]:
            watch["regression_count"] = int(watch.get("regression_count") or 0) + 1
    if outcome_status == "bad":
            watch["bad_outcome_count"] = int(watch.get("bad_outcome_count") or 0) + 1
    watch["failure_rate"] = _failure_rate(watch)
    watch["updated_at"] = now_utc()
    pattern["post_promotion_watch"] = watch

    if int(watch.get("observed_count") or 0) >= int(watch.get("required_observations") or REQUIRED_OBSERVATIONS):
        failure_rate = _failure_rate(watch)
        watch["failure_rate"] = failure_rate
        if failure_rate >= 0.2:
            return _rollback_shadow_pattern(runtime, pattern=pattern, scope=scope, watch=watch, reason=reason or "canary failure rate exceeded threshold")
        if failure_rate <= 0.05 and _watch_can_activate(watch):
            return _activate_shadow_pattern(runtime, pattern=pattern, scope=scope, watch=watch)
        return _quarantine_shadow_pattern(runtime, pattern=pattern, scope=scope, watch=watch)

    pattern["status"] = "shadow"
    watch["status"] = WATCH_STATUS
    _write_pattern(runtime, pattern, scope=scope)
    _record_watch_ledger(runtime, pattern=pattern, scope=scope, watch=watch, decision=WATCH_STATUS)
    return {"ok": True, "status": WATCH_STATUS, "pattern_id": str(pattern_id), "watch": watch}


def _initial_watch(*, candidate_id: str, promotion_request_id: str, pattern_id: str) -> dict[str, Any]:
    now = now_utc()
    return {
        "status": WATCH_STATUS,
        "candidate_id": str(candidate_id),
        "promotion_request_id": str(promotion_request_id),
        "pattern_id": str(pattern_id),
        "required_observations": REQUIRED_OBSERVATIONS,
        "observed_count": 0,
        "hit_count": 0,
        "improvement_count": 0,
        "regression_count": 0,
        "bad_outcome_count": 0,
        "failure_rate": 0.0,
        "observations": [],
        "started_at": now,
        "updated_at": now,
    }


def _watch_state(pattern: dict[str, Any], *, pattern_id: str) -> dict[str, Any]:
    watch = dict(pattern.get("post_promotion_watch") or {})
    if not watch:
        watch = _initial_watch(candidate_id=str((pattern.get("source_opportunity") or {}).get("opportunity_id") or ""), promotion_request_id="", pattern_id=pattern_id)
    watch.setdefault("status", WATCH_STATUS)
    watch.setdefault("pattern_id", pattern_id)
    watch.setdefault("required_observations", REQUIRED_OBSERVATIONS)
    watch.setdefault("observed_count", 0)
    watch.setdefault("hit_count", 0)
    watch.setdefault("improvement_count", 0)
    watch.setdefault("regression_count", 0)
    watch.setdefault("bad_outcome_count", 0)
    watch.setdefault("failure_rate", _failure_rate(watch))
    watch.setdefault("observations", [])
    return watch


def _watch_can_activate(watch: dict[str, Any]) -> bool:
    return (
        int(watch.get("hit_count") or 0) > 0
        and int(watch.get("improvement_count") or 0) > 0
        and _failure_rate(watch) <= 0.05
    )


def _failure_rate(watch: dict[str, Any]) -> float:
    observed = int(watch.get("observed_count") or 0)
    if observed <= 0:
        return 0.0
    failures = int(watch.get("regression_count") or 0) + int(watch.get("bad_outcome_count") or 0)
    return round(min(1.0, max(0.0, failures / observed)), 6)


def _activate_shadow_pattern(runtime: Any, *, pattern: dict[str, Any], scope: dict[str, Any] | ScopeRef | None, watch: dict[str, Any]) -> dict[str, Any]:
    watch["status"] = "active"
    watch["decision"] = "active"
    watch["decided_at"] = now_utc()
    pattern["status"] = "active"
    pattern["post_promotion_watch"] = watch
    _write_pattern(runtime, pattern, scope=scope)
    _update_candidate_status(runtime, watch, scope=scope, status="promoted")
    _record_watch_ledger(runtime, pattern=pattern, scope=scope, watch=watch, decision="active")
    return {"ok": True, "status": "active", "activated": True, "pattern_id": str(pattern.get("id") or ""), "watch": watch}


def _quarantine_shadow_pattern(runtime: Any, *, pattern: dict[str, Any], scope: dict[str, Any] | ScopeRef | None, watch: dict[str, Any]) -> dict[str, Any]:
    watch["status"] = "quarantined"
    watch["decision"] = "quarantined"
    watch["decided_at"] = now_utc()
    pattern["status"] = "quarantined"
    pattern["post_promotion_watch"] = watch
    _write_pattern(runtime, pattern, scope=scope)
    _update_candidate_status(runtime, watch, scope=scope, status="quarantined")
    _record_watch_ledger(runtime, pattern=pattern, scope=scope, watch=watch, decision="quarantined")
    return {"ok": True, "status": "quarantined", "quarantined": True, "pattern_id": str(pattern.get("id") or ""), "watch": watch}


def _rollback_shadow_pattern(
    runtime: Any,
    *,
    pattern: dict[str, Any],
    scope: dict[str, Any] | ScopeRef | None,
    watch: dict[str, Any],
    reason: str,
) -> dict[str, Any]:
    watch["status"] = "rolled_back"
    watch["decision"] = "rolled_back"
    watch["decided_at"] = now_utc()
    pattern["post_promotion_watch"] = watch
    _write_pattern(runtime, pattern, scope=scope)
    rollback = runtime.rollback_intent_pattern(str(pattern.get("id") or ""), scope=_scope_dict(scope), reason=str(reason or "bad outcome during shadow observe"), auto=True)
    _update_candidate_status(runtime, watch, scope=scope, status="rolled_back")
    _record_watch_ledger(runtime, pattern=pattern, scope=scope, watch=watch, decision="rolled_back")
    return {"ok": bool(rollback.get("ok")), "status": "rolled_back", "rolled_back": bool(rollback.get("ok")), "pattern_id": str(pattern.get("id") or ""), "rollback": rollback, "watch": watch}


def _load_pattern(runtime: Any, *, pattern_id: str, scope: dict[str, Any] | ScopeRef | None) -> dict[str, Any]:
    row = runtime.store.sqlite._pattern_row_for_scope(str(pattern_id), _scope(scope))
    if row is None:
        return {}
    try:
        payload = json.loads(str(row["payload_json"]))
    except json.JSONDecodeError:
        payload = {}
    return payload if isinstance(payload, dict) else {}


def _write_pattern(runtime: Any, pattern: dict[str, Any], *, scope: dict[str, Any] | ScopeRef | None) -> None:
    runtime.store.sqlite.conn.execute(
        """
        UPDATE intent_patterns
        SET status = ?, payload_json = ?, last_rollback_reason = ?, updated_at = ?
        WHERE id = ?
        """,
        (
            str(pattern.get("status") or "shadow"),
            json.dumps(pattern, ensure_ascii=False, sort_keys=True),
            str(pattern.get("last_rollback_reason") or ""),
            now_utc(),
            str(pattern.get("id") or ""),
        ),
    )
    runtime.store.sqlite.conn.commit()


def _record_watch_ledger(
    runtime: Any,
    *,
    pattern: dict[str, Any],
    scope: dict[str, Any] | ScopeRef | None,
    watch: dict[str, Any],
    decision: str,
) -> None:
    scope_ref = _scope(scope)
    pattern_id = str(pattern.get("id") or watch.get("pattern_id") or "")
    last_observation = {}
    observations = [item for item in list(watch.get("observations") or []) if isinstance(item, dict)]
    if observations:
        last_observation = dict(observations[-1])
    evidence = dict(last_observation.get("details") or {})
    action_type = _watch_action_for_decision(decision)
    details = {
        "decision": str(decision),
        "pattern_id": pattern_id,
        "candidate_id": str(watch.get("candidate_id") or ""),
        "audit_record_id": str(evidence.get("audit_record_id") or ""),
        "outcome_trace_id": str(evidence.get("outcome_trace_id") or ""),
        "outcome_event_id": str(evidence.get("outcome_event_id") or last_observation.get("event_id") or ""),
        "selected_records": list(evidence.get("selected_records") or []),
        "observed_count": int(watch.get("observed_count") or 0),
        "hit_count": int(watch.get("hit_count") or 0),
        "improvement_count": int(watch.get("improvement_count") or 0),
        "regression_count": int(watch.get("regression_count") or 0),
        "bad_outcome_count": int(watch.get("bad_outcome_count") or 0),
        "failure_rate": _failure_rate(watch),
    }
    record_lifecycle_event(
        runtime,
        scope=scope_ref,
        action_type=action_type,
        candidate_id=str(watch.get("candidate_id") or ""),
        promotion_id=str(watch.get("promotion_request_id") or ""),
        patch_id=pattern_id,
        observed_count=int(watch.get("observed_count") or 0),
        failure_rate=_failure_rate(watch),
        source_opportunity={"candidate_id": str(watch.get("candidate_id") or ""), "pattern_id": pattern_id},
        replay_report={"post_promotion_watch": watch},
        reason=str(watch.get("decision_reason") or ""),
        details=details,
        applied_artifact_id=pattern_id if decision == "active" else "",
        budget_decision="ok" if decision in {"active", WATCH_STATUS} else "blocked",
    )
    runtime.store.sqlite._record_policy_rollout_ledger(
        action_type="shadow_observe",
        scope=scope_ref,
        promotion_id=str(watch.get("promotion_request_id") or next_rollout_id(kind="promotion-watch", scope=scope_ref, payload={"pattern_id": pattern_id})),
        source_opportunity_id=str(watch.get("candidate_id") or ""),
        source_opportunity={"candidate_id": str(watch.get("candidate_id") or ""), "pattern_id": pattern_id},
        trust_report={},
        replay_report={"post_promotion_watch": watch},
        is_auto=True,
        applied_pattern_id=pattern_id if decision == "active" else "",
        budget_decision="ok",
        reason=str(watch.get("decision_reason") or ""),
        details=details,
    )
    runtime.store.sqlite.conn.commit()


def _watch_action_for_decision(decision: str) -> str:
    if decision == "active":
        return "promoted_active"
    if decision == "quarantined":
        return "quarantined"
    if decision == "rolled_back":
        return "rolled_back"
    return "shadow_observed"


def _update_candidate_status(runtime: Any, watch: dict[str, Any], *, scope: dict[str, Any] | ScopeRef | None, status: str) -> None:
    candidate_id = str(watch.get("candidate_id") or "")
    if not candidate_id:
        return
    candidate = runtime.store.get_by_id(candidate_id, scope=scope)
    if candidate is None:
        return
    candidate.status = str(status)
    candidate.meta["post_promotion_watch"] = {
        "status": str(watch.get("status") or status),
        "pattern_id": str(watch.get("pattern_id") or ""),
        "observed_count": int(watch.get("observed_count") or 0),
        "hit_count": int(watch.get("hit_count") or 0),
        "improvement_count": int(watch.get("improvement_count") or 0),
        "regression_count": int(watch.get("regression_count") or 0),
        "bad_outcome_count": int(watch.get("bad_outcome_count") or 0),
    }
    runtime.store.rewrite(candidate)


def _coerce_improved(value: bool | None, *, outcome: str) -> bool:
    if value is not None:
        return bool(value)
    return _improved_from_outcome({"outcome": outcome})


def _improved_from_outcome(payload: dict[str, Any]) -> bool:
    if "improved" in payload:
        return bool(payload.get("improved"))
    if "improvement" in payload:
        return bool(payload.get("improvement"))
    return str(payload.get("outcome") or "").strip().lower() in {"good", "success", "improved", "better"}


def _outcome_policy_attribution(
    runtime: Any,
    *,
    event_id: str,
    outcome_payload: dict[str, Any],
    scope: dict[str, Any] | ScopeRef | None,
) -> dict[str, Any]:
    direct_ids = extract_pattern_ids_from_outcome(outcome_payload)
    if direct_ids:
        return {"pattern_ids": direct_ids, "audit_record_id": "", "selected_records": []}
    session_id = _session_id_from_outcome(runtime, event_id=event_id, outcome_payload=outcome_payload, scope=scope)
    if not session_id:
        return {"pattern_ids": [], "audit_record_id": "", "selected_records": []}
    audit = _latest_recall_audit_for_session(runtime, session_id=session_id, scope=scope)
    if not audit:
        return {"pattern_ids": [], "audit_record_id": "", "selected_records": []}
    content = audit.content if isinstance(audit.content, dict) else {}
    meta = audit.meta if isinstance(audit.meta, dict) else {}
    policy_ids = _coerce_string_list(content.get("policy_suggestion_ids") or meta.get("policy_suggestion_ids"))
    selected_records = [
        dict(item)
        for item in list(content.get("selected_records") or [])
        if isinstance(item, dict)
    ]
    return {
        "pattern_ids": policy_ids,
        "audit_record_id": audit.record_id,
        "selected_records": selected_records,
    }


def _session_id_from_outcome(
    runtime: Any,
    *,
    event_id: str,
    outcome_payload: dict[str, Any],
    scope: dict[str, Any] | ScopeRef | None,
) -> str:
    for value in (
        outcome_payload.get("session_id"),
        (outcome_payload.get("policy_attribution") or {}).get("session_id")
        if isinstance(outcome_payload.get("policy_attribution"), dict)
        else "",
    ):
        text = str(value or "").strip()
        if text:
            return text
    scope_ref = _scope(scope)
    try:
        row = runtime.store.sqlite.conn.execute(
            """
            SELECT payload_json FROM events
            WHERE id = ?
              AND tenant_id = ?
              AND agent_id = ?
              AND workspace_id = ?
              AND user_id = ?
            LIMIT 1
            """,
            (str(event_id), scope_ref.tenant_id, scope_ref.agent_id, scope_ref.workspace_id, scope_ref.user_id),
        ).fetchone()
    except Exception:
        row = None
    if row is None:
        return ""
    try:
        event_payload = json.loads(str(row["payload_json"]))
    except json.JSONDecodeError:
        return ""
    return str(event_payload.get("session_id") or "").strip()


def _latest_recall_audit_for_session(
    runtime: Any,
    *,
    session_id: str,
    scope: dict[str, Any] | ScopeRef | None,
) -> RecordEnvelope | None:
    try:
        records = runtime.store.list_records(kinds=["recall_view", "reflection"], scope=_scope(scope), limit=100)
    except Exception:
        return None
    for record in records:
        if str(record.source or "") != "openclaw.before_prompt_build":
            continue
        content = record.content if isinstance(record.content, dict) else {}
        meta = record.meta if isinstance(record.meta, dict) else {}
        if str(content.get("session_id") or meta.get("session_id") or "").strip() != session_id:
            continue
        if _coerce_string_list(content.get("policy_suggestion_ids") or meta.get("policy_suggestion_ids")):
            return record
    return None


def _coerce_string_list(value: Any) -> list[str]:
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def _scope(scope: dict[str, Any] | ScopeRef | None) -> ScopeRef:
    return normalize_scope(scope)


def _scope_dict(scope: dict[str, Any] | ScopeRef | None) -> dict[str, Any]:
    scope_ref = _scope(scope)
    return asdict(scope_ref)
