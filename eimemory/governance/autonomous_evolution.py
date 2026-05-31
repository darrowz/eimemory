from __future__ import annotations

import json
import re
from dataclasses import asdict
from hashlib import sha256
from typing import Any

from eimemory.core.clock import now_iso
from eimemory.models.records import RecordEnvelope, ScopeRef


AUTONOMOUS_EVOLUTION_SCHEMA_VERSION = "autonomous_evolution.v1"
MAX_EVENT_OPPORTUNITIES = 200


def run_autonomous_evolution(
    runtime: Any,
    *,
    scope: dict[str, Any] | ScopeRef | None = None,
    apply: bool = False,
    web_hypotheses: list[dict[str, Any]] | None = None,
    max_apply: int = 3,
    persist_report: bool = False,
) -> dict[str, Any]:
    scope_ref = scope if isinstance(scope, ScopeRef) else ScopeRef.from_dict(scope)
    scope_payload = asdict(scope_ref)
    opportunities = _mine_event_opportunities(runtime, scope=scope_ref)
    opportunities.extend(_web_opportunities(web_hypotheses or [], scope=scope_ref))
    replay_cases = [_replay_case_from_opportunity(item) for item in opportunities]
    safe_patches = [_safe_patch_from_opportunity(item, scope=scope_ref) for item in opportunities]
    patch_evaluations = [_evaluate_patch(patch) for patch in safe_patches]
    experiments = [
        _experiment_from_patch(patch, replay_case, evaluation)
        for patch, replay_case, evaluation in zip(safe_patches, replay_cases, patch_evaluations)
    ]

    max_apply_count = max(0, int(max_apply))
    applied_count = 0
    applied_patches: list[dict[str, Any]] = []
    blocked_patches: list[dict[str, Any]] = []
    for patch, evaluation in zip(safe_patches, patch_evaluations):
        if not evaluation["ok"]:
            blocked_patches.append({
                "opportunity_id": patch["opportunity_id"],
                "patch_type": patch["patch_type"],
                "risk_level": patch["risk_level"],
                "blocked_reason": evaluation["blocked_reason"],
            })
            continue
        if applied_count >= max_apply_count:
            if apply:
                blocked_patches.append({
                    "opportunity_id": patch["opportunity_id"],
                    "patch_type": patch["patch_type"],
                    "risk_level": patch["risk_level"],
                    "blocked_reason": "max_apply_reached",
                })
            continue
        if apply:
            applied = _apply_safe_patch(runtime, patch, scope=scope_payload)
            applied_count += 1
            applied_patches.append(applied)

    report: dict[str, Any] = {
        "ok": True,
        "apply": bool(apply),
        "persist_report": bool(persist_report),
        "report_type": "autonomous_evolution",
        "schema_version": AUTONOMOUS_EVOLUTION_SCHEMA_VERSION,
        "generated_at": now_iso(),
        "scope": scope_payload,
        "opportunity_count": len(opportunities),
        "opportunities": opportunities,
        "replay_cases": replay_cases,
        "safe_patches": safe_patches,
        "experiments": experiments,
        "passed_experiment_count": sum(1 for item in experiments if item.get("passed")),
        "failed_experiment_count": sum(1 for item in experiments if not item.get("passed")),
        "applied_count": applied_count,
        "applied_patches": applied_patches,
        "blocked_patches": blocked_patches,
        "max_apply": max_apply_count,
    }
    persisted_record_id = ""
    if persist_report:
        record = _autonomous_evolution_report_record(report, scope=scope_ref)
        runtime.store.append(record)
        persisted_record_id = record.record_id
    report["persisted"] = bool(persist_report)
    report["persisted_record_id"] = persisted_record_id
    return report


def _mine_event_opportunities(runtime: Any, *, scope: ScopeRef) -> list[dict[str, Any]]:
    pairs = _load_recent_event_outcome_pairs(runtime, scope=scope, limit=MAX_EVENT_OPPORTUNITIES)
    opportunities: list[dict[str, Any]] = []
    for pair in pairs:
        event = dict(pair.get("event") or {})
        outcome = dict(pair.get("outcome") or {})
        if _normalized_outcome(outcome) != "bad":
            continue
        policy_text = _first_nonempty(
            outcome.get("policy_update"),
            outcome.get("correction_from_user"),
        )
        if not policy_text:
            continue
        if not event.get("id"):
            continue
        opportunities.append(
            {
                "opportunity_id": _stable_id("event-opportunity", event.get("id"), policy_text),
                "opportunity_type": "intent_policy",
                "source": "event",
                "source_event_id": str(event.get("id") or ""),
                "event_type": str(event.get("event_type") or "communication"),
                "trigger": str(event.get("user_phrase") or event.get("goal") or ""),
                "risk_level": "low",
                "policy_hint": policy_text,
                "policy_update": str(outcome.get("policy_update") or ""),
                "correction_from_user": str(outcome.get("correction_from_user") or ""),
                "outcome_reason": str(outcome.get("reason") or ""),
                "recorded_at": str((outcome.get("recorded_at")) or ""),
                "confidence": round(float(event.get("confidence") or 0.0), 3),
                "source_event_payload": event,
                "source_outcome_payload": outcome,
            }
        )
    return opportunities


def _web_opportunities(web_hypotheses: list[dict[str, Any]], *, scope: ScopeRef) -> list[dict[str, Any]]:
    opportunities: list[dict[str, Any]] = []
    for index, raw in enumerate(web_hypotheses or []):
        if not isinstance(raw, dict):
            continue
        candidate = raw.get("candidate_policy") if isinstance(raw.get("candidate_policy"), dict) else {}
        replay_hints = [item for item in raw.get("replay_hints") or [] if isinstance(item, dict)]
        first_replay_hint = replay_hints[0] if replay_hints else {}
        trigger = _first_nonempty(
            raw.get("trigger"),
            raw.get("query"),
            raw.get("pattern"),
            raw.get("title"),
            candidate.get("title"),
            first_replay_hint.get("query"),
            raw.get("url"),
            raw.get("source_url"),
        )
        if not trigger:
            continue
        policy_text = _first_nonempty(
            raw.get("policy_update"),
            raw.get("policy"),
            raw.get("hint"),
            raw.get("text"),
            candidate.get("policy_update"),
            candidate.get("summary"),
            candidate.get("title"),
        )
        if not policy_text:
            policy_text = f"web hypothesis: {trigger}"
        event_type = str(raw.get("event_type") or raw.get("default_event_type") or "communication").strip() or "communication"
        evidence = [str(item) for item in _coerce_string_list(raw.get("evidence"))]
        evidence.extend(str(item.get("source_url") or "") for item in replay_hints if item.get("source_url"))
        evidence.extend([str(raw.get("url") or ""), str(raw.get("source_url") or "")])
        opportunities.append(
            {
                "opportunity_id": _stable_id("web-hypothesis", trigger, str(index), event_type, policy_text),
                "opportunity_type": "intent_pattern",
                "source": "web_hypothesis",
                "source_event_id": f"web_{index}",
                "event_type": event_type,
                "trigger": str(trigger),
                "risk_level": _normalize_risk_level(str(raw.get("risk_level") or "medium")),
                "policy_hint": policy_text,
                "policy_update": policy_text,
                "correction_from_user": "",
                "outcome_reason": "",
                "recorded_at": now_iso(),
                "confidence": _coerce_float(raw.get("confidence") or candidate.get("confidence_hint"), default=0.7),
                "source_event_payload": dict(raw),
                "source_outcome_payload": {"replay_hints": replay_hints},
                "scope": asdict(scope),
                "web_evidence": [item for item in evidence if item],
            }
        )
    return opportunities


def _replay_case_from_opportunity(opportunity: dict[str, Any]) -> dict[str, Any]:
    replay_hints = [
        item for item in (opportunity.get("source_outcome_payload") or {}).get("replay_hints") or []
        if isinstance(item, dict)
    ]
    if replay_hints:
        first_hint = replay_hints[0]
        return {
            "opportunity_id": str(opportunity.get("opportunity_id") or ""),
            "query": str(_first_nonempty(first_hint.get("query"), opportunity.get("trigger"))),
            "expected_text": _coerce_string_list(first_hint.get("expected_text")),
            "event_type": str(opportunity.get("event_type") or ""),
            "risk_level": str(opportunity.get("risk_level") or "medium"),
            "source": str(opportunity.get("source") or ""),
            "source_url": str(first_hint.get("source_url") or opportunity.get("source_event_payload", {}).get("source_url") or ""),
        }
    expected_text = _coerce_string_list(_first_nonempty(opportunity.get("correction_from_user"), opportunity.get("policy_hint")))
    if not expected_text:
        expected_text = [_first_nonempty(opportunity.get("policy_update"), opportunity.get("correction_from_user"), "")]
    return {
        "opportunity_id": str(opportunity.get("opportunity_id") or ""),
        "query": str(opportunity.get("trigger") or ""),
        "expected_text": expected_text,
        "event_type": str(opportunity.get("event_type") or ""),
        "risk_level": str(opportunity.get("risk_level") or "medium"),
        "source": str(opportunity.get("source") or ""),
    }


def _safe_patch_from_opportunity(opportunity: dict[str, Any], *, scope: ScopeRef) -> dict[str, Any]:
    opportunity_type = str(opportunity.get("opportunity_type") or "")
    if opportunity_type != "intent_policy":
        return {
            "opportunity_id": str(opportunity.get("opportunity_id") or ""),
            "patch_type": "unsupported",
            "risk_level": _normalize_risk_level(str(opportunity.get("risk_level") or "medium")),
            "applied": False,
        }

    event_type = str(opportunity.get("event_type") or "communication").strip() or "communication"
    trigger = _first_nonempty(opportunity.get("trigger"), "")
    interpreted_intent = _first_nonempty(
        opportunity.get("source_event_payload", {}).get("interpreted_intent"),
        trigger,
    )
    policy_update = str(opportunity.get("policy_update") or opportunity.get("policy_hint") or "")
    execution_policy = _policy_steps(policy_update)
    if not execution_policy:
        execution_policy = _policy_steps(
            str(opportunity.get("correction_from_user") or opportunity.get("outcome_reason") or "")
        )
    success_criteria = _first_nonempty(
        opportunity.get("source_event_payload", {}).get("verification"),
        opportunity.get("source_event_payload", {}).get("goal"),
        "用户反馈通过验证。",
    )
    confidence = _coerce_float(opportunity.get("confidence"), default=0.8)
    confidence = min(1.0, max(0.35, confidence))

    return {
        "opportunity_id": str(opportunity.get("opportunity_id") or ""),
        "patch_type": "intent_pattern",
        "risk_level": _normalize_risk_level(str(opportunity.get("risk_level") or "medium")),
        "source": str(opportunity.get("source") or ""),
        "scope": asdict(scope),
        "pattern": trigger,
        "default_event_type": event_type,
        "interpreted_intent": interpreted_intent,
        "execution_policy": execution_policy,
        "success_criteria": str(success_criteria),
        "first_questions": [q for q in _coerce_string_list(
            opportunity.get("source_event_payload", {}).get("first_questions")
        )[:3] if q],
        "ask_first_boundaries": [],
        "confidence": confidence,
        "source_opportunity": opportunity,
    }


def _evaluate_patch(patch: dict[str, Any]) -> dict[str, Any]:
    if str(patch.get("patch_type") or "") != "intent_pattern":
        return {"ok": False, "blocked_reason": "unsupported_patch_type"}
    if not str(patch.get("pattern") or "").strip():
        return {"ok": False, "blocked_reason": "missing_trigger"}
    if _normalize_risk_level(str(patch.get("risk_level") or "medium")) != "low":
        return {"ok": False, "blocked_reason": "risk_level_not_low"}
    if not str(patch.get("default_event_type") or "").strip():
        return {"ok": False, "blocked_reason": "missing_event_type"}
    execution_policy = _coerce_string_list(patch.get("execution_policy"))
    if not execution_policy:
        return {"ok": False, "blocked_reason": "empty_execution_policy"}
    text = " ".join([str(patch.get("interpreted_intent") or ""), *execution_policy])
    if _contains_high_risk_signals(text):
        return {"ok": False, "blocked_reason": "contains_high_risk_action"}
    return {"ok": True, "blocked_reason": ""}


def _experiment_from_patch(
    patch: dict[str, Any],
    replay_case: dict[str, Any],
    evaluation: dict[str, Any],
) -> dict[str, Any]:
    opportunity_id = str(patch.get("opportunity_id") or "")
    patch_type = str(patch.get("patch_type") or "")
    return {
        "experiment_id": _stable_id("patch-experiment", opportunity_id, patch_type),
        "experiment_type": "safe_patch_gate",
        "opportunity_id": opportunity_id,
        "patch_type": patch_type,
        "risk_level": _normalize_risk_level(str(patch.get("risk_level") or "medium")),
        "replay_case": replay_case,
        "evaluation": dict(evaluation),
        "passed": bool(evaluation.get("ok")),
    }


def _apply_safe_patch(runtime: Any, patch: dict[str, Any], *, scope: dict[str, Any]) -> dict[str, Any]:
    payload = {
        "pattern": str(patch.get("pattern") or ""),
        "default_event_type": str(patch.get("default_event_type") or "communication"),
        "interpreted_intent": str(patch.get("interpreted_intent") or ""),
        "execution_policy": list(_coerce_string_list(patch.get("execution_policy"))),
        "success_criteria": str(patch.get("success_criteria") or ""),
        "first_questions": list(_coerce_string_list(patch.get("first_questions"))),
        "ask_first_boundaries": list(_coerce_string_list(patch.get("ask_first_boundaries"))),
        "confidence": float(patch.get("confidence") or 0.0),
        "source": "autonomous_evolution",
        "source_opportunity_id": str(patch.get("opportunity_id") or ""),
    }
    result = runtime.upsert_intent_pattern(payload, scope=scope)
    return {
        "opportunity_id": str(patch.get("opportunity_id") or ""),
        "patch_type": "intent_pattern",
        "pattern_id": str(result.get("id") or ""),
        "pattern": str(result.get("pattern") or payload["pattern"]),
        "event_type": str(result.get("default_event_type") or payload["default_event_type"]),
        "confidence": float(result.get("confidence") or payload["confidence"]),
        "risk_level": str(patch.get("risk_level") or "low"),
        "applied": True,
    }


def _load_recent_event_outcome_pairs(runtime: Any, *, scope: ScopeRef, limit: int) -> list[dict[str, Any]]:
    conn = runtime.store.sqlite.conn
    max_items = max(0, min(MAX_EVENT_OPPORTUNITIES, int(limit)))
    if max_items <= 0:
        return []
    event_rows = conn.execute(
        """
        SELECT id, payload_json, timestamp
        FROM events
        WHERE tenant_id = ?
          AND agent_id = ?
          AND workspace_id = ?
          AND user_id = ?
        ORDER BY timestamp DESC, id DESC
        LIMIT ?
        """,
        (scope.tenant_id, scope.agent_id, scope.workspace_id, scope.user_id, max_items),
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
    return [
        {
            "event": event,
            "outcome": outcomes_by_event.get(str(event.get("id") or ""), {}),
        }
        for event in events
    ]


def _autonomous_evolution_report_record(report: dict[str, Any], *, scope: ScopeRef) -> RecordEnvelope:
    generated_at = now_iso()
    applied_count = int(report.get("applied_count") or 0)
    summary = (
        f"Autonomous evolution run: opportunities={report.get('opportunity_count', 0)}, "
        f"applied={applied_count}, blocked={len(report.get('blocked_patches') or [])}"
    )
    return RecordEnvelope.create(
        kind="reflection",
        title="Autonomous evolution report",
        status="active",
        summary=summary,
        detail=summary,
        content={"report": _json_safe(report)},
        tags=["autonomous-evolution"],
        source="eimemory.autonomous_evolution",
        scope=scope,
        provenance={
            "report_type": "autonomous_evolution",
            "generated_at": generated_at,
            "schema_version": AUTONOMOUS_EVOLUTION_SCHEMA_VERSION,
        },
        meta={
            "report_type": "autonomous_evolution",
            "schema_version": AUTONOMOUS_EVOLUTION_SCHEMA_VERSION,
            "generated_at": generated_at,
            "opportunity_count": int(report.get("opportunity_count") or 0),
            "applied_count": applied_count,
            "blocked_count": int(len(report.get("blocked_patches") or [])),
            "persisted": bool(report.get("persist_report")),
        },
    )


def _normalize_risk_level(value: str) -> str:
    normalized = str(value or "").strip().lower()
    if normalized in {"low", "medium", "high"}:
        return normalized
    return "medium"


def _contains_high_risk_signals(text: str) -> bool:
    lowered = str(text or "").lower()
    high_risk = (
        "delete",
        "del ",
        "删除",
        "重置",
        "格式化",
        "格式",
        "降权",
        "停机",
        "重启",
        "授权",
        "payment",
        "charge",
        "扣费",
        "付费",
        "外发",
        "发送给外部",
        "wipe",
        "reset",
        "rollback",
        "回滚",
    )
    return any(marker in lowered for marker in high_risk)


def _policy_steps(text: str) -> list[str]:
    if not text:
        return []
    normalized = re.sub(r"[;；\n\r]+", "|", str(text))
    return [part.strip() for part in normalized.split("|") if part.strip()]


def _coerce_string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [part.strip() for part in value.split("|") if part.strip()] if "|" in value else [value.strip()] if value.strip() else []
    if isinstance(value, (list, tuple, set)):
        return [str(item).strip() for item in value if str(item).strip()]
    return []


def _coerce_float(value: Any, *, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _stable_id(*parts: object) -> str:
    payload = "|".join(str(part) for part in parts)
    return "ae_" + sha256(payload.encode("utf-8")).hexdigest()[:16]


def _normalized_outcome(outcome: dict[str, Any]) -> str:
    value = str(outcome.get("outcome") or "").strip().lower()
    if value in {"good", "bad", "uncertain"}:
        return value
    return "uncertain"


def _first_nonempty(*values: object) -> str:
    for value in values:
        text = str(value or "").strip()
        if text:
            return text
    return ""


def _json_loads(value: Any) -> dict[str, Any]:
    try:
        payload = json.loads(str(value or "{}"))
    except json.JSONDecodeError:
        return {}
    return dict(payload) if isinstance(payload, dict) else {}


def _json_safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json_safe(item) for item in value]
    if isinstance(value, tuple):
        return [_json_safe(item) for item in value]
    if isinstance(value, set):
        return sorted((_json_safe(item) for item in value), key=lambda item: repr(item))
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)
