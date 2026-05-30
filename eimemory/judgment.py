from __future__ import annotations

import json
from collections import Counter, defaultdict
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any

from eimemory.core.clock import now_iso
from eimemory.models.records import RecordEnvelope, ScopeRef


TEMPORARY_FIX_MARKERS = (
    "临时",
    "暂时",
    "绕过",
    "hotfix",
    "quick fix",
    "workaround",
    "restart only",
)


def run_judgment_evaluation(
    runtime: Any,
    *,
    scope: dict[str, Any] | ScopeRef | None = None,
    since: str | None = None,
    limit: int | None = 200,
    persist_playbook: bool = False,
) -> dict[str, Any]:
    scope_ref = scope if isinstance(scope, ScopeRef) else ScopeRef.from_dict(scope)
    max_events = max(0, min(1000, int(limit if limit is not None else 200)))
    pairs = _load_recent_event_outcome_pairs(runtime, scope=scope_ref, since=since, limit=max_events)
    report = _build_report(scope_ref, pairs, since=since, limit=max_events)
    persisted_record_id = ""
    persisted_policy_ids: list[str] = []
    if persist_playbook:
        record = _judgment_report_record(report, scope=scope_ref)
        runtime.store.append(record)
        persisted_record_id = record.record_id
        persisted_policy_ids = _persist_playbook_policies(runtime, report["playbook_entries"], scope=scope_ref)
    return {
        **report,
        "persisted": bool(persist_playbook),
        "persisted_record_id": persisted_record_id,
        "persisted_policy_ids": persisted_policy_ids,
    }


def _load_recent_event_outcome_pairs(
    runtime: Any,
    *,
    scope: ScopeRef,
    since: str | None,
    limit: int,
) -> list[dict[str, Any]]:
    if limit <= 0:
        return []
    conn = runtime.store.sqlite.conn
    params: list[Any] = [scope.tenant_id, scope.agent_id, scope.workspace_id, scope.user_id]
    since_clause = ""
    if since:
        since_clause = " AND timestamp >= ?"
        params.append(str(since))
    params.append(limit)
    event_rows = conn.execute(
        f"""
        SELECT id, payload_json, timestamp
        FROM events
        WHERE tenant_id = ?
          AND agent_id = ?
          AND workspace_id = ?
          AND user_id = ?
          {since_clause}
        ORDER BY timestamp DESC, id DESC
        LIMIT ?
        """,
        tuple(params),
    ).fetchall()
    events = [_json_loads(row["payload_json"]) for row in event_rows]
    event_ids = [str(event.get("id") or "") for event in events if str(event.get("id") or "")]
    outcomes_by_event: dict[str, dict[str, Any]] = {}
    if event_ids:
        placeholders = ",".join("?" for _ in event_ids)
        outcome_rows = conn.execute(
            f"""
            SELECT event_id, payload_json, recorded_at
            FROM event_outcomes
            WHERE event_id IN ({placeholders})
              AND tenant_id = ?
              AND agent_id = ?
              AND workspace_id = ?
              AND user_id = ?
            ORDER BY recorded_at DESC, id DESC
            """,
            (
                *event_ids,
                scope.tenant_id,
                scope.agent_id,
                scope.workspace_id,
                scope.user_id,
            ),
        ).fetchall()
        for row in outcome_rows:
            event_id = str(row["event_id"] or "")
            if event_id not in outcomes_by_event:
                outcomes_by_event[event_id] = _json_loads(row["payload_json"])
    return [{"event": event, "outcome": outcomes_by_event.get(str(event.get("id") or ""), {})} for event in events]


def _build_report(
    scope: ScopeRef,
    pairs: list[dict[str, Any]],
    *,
    since: str | None,
    limit: int,
) -> dict[str, Any]:
    outcome_counts = Counter({"good": 0, "bad": 0, "uncertain": 0, "verification_missing": 0})
    bad_by_type: dict[str, list[dict[str, Any]]] = defaultdict(list)
    good_by_type: dict[str, list[dict[str, Any]]] = defaultdict(list)
    event_summaries: list[dict[str, Any]] = []
    user_corrections: list[dict[str, Any]] = []
    reliable_paths: list[dict[str, Any]] = []
    verification_missing: list[dict[str, Any]] = []
    noise_signals: list[dict[str, Any]] = []
    temporary_fixes: list[dict[str, Any]] = []

    for pair in pairs:
        event = dict(pair.get("event") or {})
        outcome = dict(pair.get("outcome") or {})
        event_type = str(event.get("event_type") or "unknown")
        outcome_name = _normalized_outcome(outcome)
        outcome_counts[outcome_name] += 1
        if not _has_verification(event, outcome) and outcome_name != "verification_missing":
            outcome_counts["verification_missing"] += 1
            verification_missing.append(_verification_missing_entry(event, outcome))
        summary = _event_summary(event, outcome, outcome_name)
        event_summaries.append(summary)
        if outcome_name == "bad":
            bad_by_type[event_type].append({"event": event, "outcome": outcome})
        if outcome_name == "good" and _has_verification(event, outcome) and event.get("action_path"):
            good_by_type[event_type].append({"event": event, "outcome": outcome})
            reliable_paths.append(_reliable_path_entry(event, outcome))
        if outcome.get("correction_from_user"):
            user_corrections.append(_user_correction_entry(event, outcome))
        if _is_noise_signal(event, outcome, outcome_name):
            noise_signals.append(_noise_signal_entry(event, outcome, outcome_name))
        if _looks_temporary(event, outcome):
            temporary_fixes.append(_temporary_fix_entry(event, outcome))

    repeated_failures = _repeated_failures(bad_by_type)
    playbook_entries = _playbook_entries(
        bad_by_type=bad_by_type,
        good_by_type=good_by_type,
        repeated_failures=repeated_failures,
    )
    return {
        "ok": True,
        "report_type": "judgment_evaluation",
        "judgment_schema_version": "judgment_report.v1",
        "generated_at": now_iso(),
        "scope": asdict(scope),
        "since": str(since or ""),
        "limit": limit,
        "scanned_event_count": len(pairs),
        "source_event_ids": [str((pair.get("event") or {}).get("id") or "") for pair in pairs],
        "outcome_counts": dict(outcome_counts),
        "event_summaries": event_summaries,
        "repeated_failures": repeated_failures,
        "user_corrections": user_corrections,
        "reliable_paths": reliable_paths,
        "verification_missing": verification_missing,
        "noise_signals": noise_signals,
        "temporary_fixes": temporary_fixes,
        "playbook_entries": playbook_entries,
    }


def _normalized_outcome(outcome: dict[str, Any]) -> str:
    value = str(outcome.get("outcome") or "uncertain").strip().lower()
    if value in {"good", "bad", "uncertain", "verification_missing"}:
        return value
    return "uncertain"


def _has_verification(event: dict[str, Any], outcome: dict[str, Any]) -> bool:
    return bool(str(event.get("verification") or outcome.get("verification") or "").strip())


def _event_summary(event: dict[str, Any], outcome: dict[str, Any], outcome_name: str) -> dict[str, Any]:
    return {
        "event_id": str(event.get("id") or ""),
        "timestamp": str(event.get("timestamp") or ""),
        "source": str(event.get("source") or ""),
        "event_type": str(event.get("event_type") or ""),
        "user_phrase": str(event.get("user_phrase") or ""),
        "outcome": outcome_name,
        "reason": str(outcome.get("reason") or ""),
        "verification_present": _has_verification(event, outcome),
        "confidence": _clamp(float(event.get("confidence") or 0.0)),
    }


def _verification_missing_entry(event: dict[str, Any], outcome: dict[str, Any]) -> dict[str, Any]:
    return {
        "event_id": str(event.get("id") or ""),
        "event_type": str(event.get("event_type") or ""),
        "user_phrase": str(event.get("user_phrase") or ""),
        "outcome": _normalized_outcome(outcome),
        "reason": "verification_missing",
    }


def _user_correction_entry(event: dict[str, Any], outcome: dict[str, Any]) -> dict[str, Any]:
    return {
        "event_id": str(event.get("id") or ""),
        "event_type": str(event.get("event_type") or ""),
        "user_phrase": str(event.get("user_phrase") or ""),
        "correction_from_user": str(outcome.get("correction_from_user") or ""),
        "policy_update": str(outcome.get("policy_update") or ""),
    }


def _reliable_path_entry(event: dict[str, Any], outcome: dict[str, Any]) -> dict[str, Any]:
    return {
        "event_id": str(event.get("id") or ""),
        "event_type": str(event.get("event_type") or ""),
        "trigger": _trigger_for_events(str(event.get("event_type") or ""), [event]),
        "action_path": [str(item) for item in (event.get("action_path") or [])],
        "verification": str(event.get("verification") or outcome.get("verification") or ""),
        "confidence": _clamp(float(event.get("confidence") or 0.0)),
    }


def _is_noise_signal(event: dict[str, Any], outcome: dict[str, Any], outcome_name: str) -> bool:
    return (
        outcome_name == "uncertain"
        or float(event.get("confidence") or 0.0) < 0.35
        or not str(event.get("user_phrase") or "").strip()
        or (not str(event.get("goal") or "").strip() and outcome_name != "good")
    )


def _noise_signal_entry(event: dict[str, Any], outcome: dict[str, Any], outcome_name: str) -> dict[str, Any]:
    reasons: list[str] = []
    if outcome_name == "uncertain":
        reasons.append("uncertain_outcome")
    if float(event.get("confidence") or 0.0) < 0.35:
        reasons.append("low_confidence")
    if not str(event.get("user_phrase") or "").strip():
        reasons.append("missing_user_phrase")
    if not str(event.get("goal") or "").strip():
        reasons.append("missing_goal")
    return {
        "event_id": str(event.get("id") or ""),
        "event_type": str(event.get("event_type") or ""),
        "outcome": outcome_name,
        "reasons": reasons,
        "evidence": str(outcome.get("reason") or event.get("interpreted_intent") or ""),
    }


def _looks_temporary(event: dict[str, Any], outcome: dict[str, Any]) -> bool:
    text = " ".join(
        [
            str(outcome.get("reason") or ""),
            str(outcome.get("policy_update") or ""),
            str(outcome.get("correction_from_user") or ""),
            str(event.get("lesson") or ""),
            " ".join(str(item) for item in (event.get("action_path") or [])),
        ]
    ).lower()
    return any(marker.lower() in text for marker in TEMPORARY_FIX_MARKERS)


def _temporary_fix_entry(event: dict[str, Any], outcome: dict[str, Any]) -> dict[str, Any]:
    return {
        "event_id": str(event.get("id") or ""),
        "event_type": str(event.get("event_type") or ""),
        "user_phrase": str(event.get("user_phrase") or ""),
        "reason": str(outcome.get("reason") or ""),
        "policy_update": str(outcome.get("policy_update") or event.get("next_policy") or ""),
    }


def _repeated_failures(bad_by_type: dict[str, list[dict[str, Any]]]) -> list[dict[str, Any]]:
    failures: list[dict[str, Any]] = []
    for event_type, items in bad_by_type.items():
        if len(items) < 2:
            continue
        events = [dict(item.get("event") or {}) for item in items]
        outcomes = [dict(item.get("outcome") or {}) for item in items]
        failures.append(
            {
                "event_type": event_type,
                "count": len(items),
                "trigger": _trigger_for_events(event_type, events),
                "source_event_ids": [str(event.get("id") or "") for event in events],
                "reasons": _unique_nonempty(str(outcome.get("reason") or "") for outcome in outcomes),
                "policy_updates": _unique_nonempty(str(outcome.get("policy_update") or "") for outcome in outcomes),
                "user_corrections": _unique_nonempty(str(outcome.get("correction_from_user") or "") for outcome in outcomes),
            }
        )
    failures.sort(key=lambda item: (-int(item["count"]), str(item["event_type"])))
    return failures


def _playbook_entries(
    *,
    bad_by_type: dict[str, list[dict[str, Any]]],
    good_by_type: dict[str, list[dict[str, Any]]],
    repeated_failures: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    covered_types: set[str] = set()
    for failure in repeated_failures:
        event_type = str(failure["event_type"])
        covered_types.add(event_type)
        items = bad_by_type[event_type]
        reliable = good_by_type.get(event_type, [])
        entries.append(_entry_from_bad_items(event_type, items, reliable=reliable, repeated=True))

    for event_type, items in bad_by_type.items():
        if event_type in covered_types:
            continue
        actionable = [
            item
            for item in items
            if _best_policy_for_items([item]) or str((item.get("event") or {}).get("next_policy") or "")
        ]
        if actionable:
            entries.append(_entry_from_bad_items(event_type, actionable, reliable=good_by_type.get(event_type, []), repeated=False))
            covered_types.add(event_type)

    for event_type, items in good_by_type.items():
        if event_type in covered_types:
            continue
        entries.append(_entry_from_good_items(event_type, items))
    entries.sort(key=lambda item: (-float(item.get("confidence") or 0.0), str(item.get("trigger") or "")))
    return entries


def _entry_from_bad_items(
    event_type: str,
    items: list[dict[str, Any]],
    *,
    reliable: list[dict[str, Any]],
    repeated: bool,
) -> dict[str, Any]:
    events = [dict(item.get("event") or {}) for item in items]
    outcomes = [dict(item.get("outcome") or {}) for item in items]
    policy = _best_policy_for_items(items) or f"{event_type} 请求需要先诊断、记录证据并验证结果"
    success = _best_success_criteria(events, outcomes, reliable)
    evidence = []
    if repeated:
        evidence.append(f"bad_count={len(items)}")
    evidence.extend(_unique_nonempty(str(outcome.get("reason") or "") for outcome in outcomes)[:3])
    evidence.extend(f"user_correction={value}" for value in _unique_nonempty(str(outcome.get("correction_from_user") or "") for outcome in outcomes)[:2])
    source_event_ids = [str(event.get("id") or "") for event in events]
    confidence = 0.58 + min(0.2, len(items) * 0.05)
    if any(outcome.get("correction_from_user") for outcome in outcomes):
        confidence += 0.12
    if reliable:
        confidence += 0.08
    return {
        "trigger": _trigger_for_events(event_type, events),
        "policy": policy,
        "evidence": evidence,
        "success_criteria": success,
        "source_event_ids": source_event_ids,
        "confidence": _clamp(confidence),
    }


def _entry_from_good_items(event_type: str, items: list[dict[str, Any]]) -> dict[str, Any]:
    events = [dict(item.get("event") or {}) for item in items]
    outcomes = [dict(item.get("outcome") or {}) for item in items]
    best = max(events, key=lambda event: float(event.get("confidence") or 0.0))
    policy_steps = [str(item) for item in (best.get("action_path") or [])]
    policy = " -> ".join(policy_steps) if policy_steps else str(best.get("next_policy") or "")
    return {
        "trigger": _trigger_for_events(event_type, events),
        "policy": policy or f"复用已验证的 {event_type} 执行路径",
        "evidence": _unique_nonempty(str(outcome.get("reason") or "") for outcome in outcomes)[:3],
        "success_criteria": str(best.get("verification") or outcomes[0].get("verification") or "后续同类请求完成验证"),
        "source_event_ids": [str(event.get("id") or "") for event in events],
        "confidence": _clamp(0.62 + min(0.25, float(best.get("confidence") or 0.0) * 0.25)),
    }


def _best_policy_for_items(items: list[dict[str, Any]]) -> str:
    candidates: list[tuple[int, str]] = []
    for item in items:
        event = dict(item.get("event") or {})
        outcome = dict(item.get("outcome") or {})
        for key in ("policy_update", "correction_from_user"):
            value = str(outcome.get(key) or "").strip()
            if value:
                score = len(value) + (200 if key == "policy_update" else 100)
                if outcome.get("correction_from_user"):
                    score += 100
                candidates.append((score, value))
        value = str(event.get("next_policy") or event.get("lesson") or "").strip()
        if value:
            candidates.append((len(value), value))
    if not candidates:
        return ""
    candidates.sort(key=lambda item: (-item[0], item[1]))
    return candidates[0][1]


def _best_success_criteria(
    events: list[dict[str, Any]],
    outcomes: list[dict[str, Any]],
    reliable: list[dict[str, Any]],
) -> str:
    for item in reliable:
        event = dict(item.get("event") or {})
        outcome = dict(item.get("outcome") or {})
        verification = str(event.get("verification") or outcome.get("verification") or "").strip()
        if verification:
            return verification
    for event, outcome in zip(events, outcomes):
        verification = str(event.get("verification") or outcome.get("verification") or "").strip()
        if verification:
            return verification
    return "后续同类请求有明确验证结果，且用户未再次纠正"


def _trigger_for_events(event_type: str, events: list[dict[str, Any]]) -> str:
    phrases = _unique_nonempty(str(event.get("user_phrase") or "") for event in events)[:3]
    if phrases:
        return f"{event_type}: {' | '.join(phrases)}"
    return event_type


def _persist_playbook_policies(runtime: Any, entries: list[dict[str, Any]], *, scope: ScopeRef) -> list[str]:
    policy_ids: list[str] = []
    for entry in entries:
        trigger = str(entry.get("trigger") or "").strip()
        if not trigger:
            continue
        event_type = trigger.split(":", 1)[0].strip() or "communication"
        policy = str(entry.get("policy") or "")
        payload = runtime.store.upsert_intent_pattern(
            {
                "pattern": trigger,
                "default_event_type": event_type,
                "interpreted_intent": trigger,
                "execution_policy": _policy_steps(policy),
                "success_criteria": str(entry.get("success_criteria") or ""),
                "confidence": float(entry.get("confidence") or 0.0),
                "source": "judgment_playbook",
                "source_event_ids": list(entry.get("source_event_ids") or []),
                "evidence": list(entry.get("evidence") or []),
            },
            scope=scope,
        )
        policy_ids.append(str(payload.get("id") or ""))
    return policy_ids


def _policy_steps(policy: str) -> list[str]:
    if "->" in policy:
        return [part.strip() for part in policy.split("->") if part.strip()]
    if "；" in policy:
        return [part.strip() for part in policy.split("；") if part.strip()]
    if ";" in policy:
        return [part.strip() for part in policy.split(";") if part.strip()]
    return [policy] if policy.strip() else []


def _judgment_report_record(report: dict[str, Any], *, scope: ScopeRef) -> RecordEnvelope:
    summary = (
        f"Judgment evaluation scanned {int(report.get('scanned_event_count') or 0)} events, "
        f"found {len(report.get('repeated_failures') or [])} repeated failures and "
        f"{len(report.get('playbook_entries') or [])} playbook entries."
    )
    return RecordEnvelope.create(
        kind="reflection",
        title="Judgment evaluation playbook",
        summary=summary,
        detail=summary,
        content={"report": _json_safe(report)},
        tags=["judgment-evaluation", "playbook", "nightly"],
        evidence=list(report.get("source_event_ids") or [])[:25],
        source="eimemory.judgment_evaluation",
        scope=scope,
        provenance={
            "report_type": "judgment_evaluation",
            "generated_at": str(report.get("generated_at") or ""),
        },
        meta={
            "report_type": "judgment_evaluation",
            "schema_version": str(report.get("judgment_schema_version") or "judgment_report.v1"),
            "scanned_event_count": int(report.get("scanned_event_count") or 0),
            "playbook_entry_count": len(report.get("playbook_entries") or []),
            "repeated_failure_count": len(report.get("repeated_failures") or []),
            "verification_missing_count": int((report.get("outcome_counts") or {}).get("verification_missing") or 0),
        },
    )


def _unique_nonempty(values: Any) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        result.append(text)
    return result


def _clamp(value: float) -> float:
    return round(max(0.0, min(1.0, value)), 3)


def _json_loads(value: Any) -> dict[str, Any]:
    try:
        payload = json.loads(str(value or "{}"))
    except json.JSONDecodeError:
        return {}
    return dict(payload) if isinstance(payload, dict) else {}


def _json_safe(value: Any) -> Any:
    if is_dataclass(value) and not isinstance(value, type):
        return _json_safe(asdict(value))
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json_safe(item) for item in value]
    if isinstance(value, tuple):
        return [_json_safe(item) for item in value]
    if isinstance(value, set):
        return sorted((_json_safe(item) for item in value), key=lambda item: repr(item))
    if isinstance(value, Path):
        return str(value)
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)
