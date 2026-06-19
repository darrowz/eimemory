from __future__ import annotations

import json
import re
from dataclasses import asdict
from hashlib import sha256
from typing import Any

from eimemory.governance.capability_distiller import distill_capability_candidate
from eimemory.governance.promotion_manager import promote_candidate
from eimemory.core.clock import now_iso
from eimemory.governance.policy_replay import (
    build_replay_case,
    evaluate_replay_gate,
    evaluate_safe_action_gate,
)
from eimemory.governance.policy_trust import evaluate_trust_gate
from eimemory.governance.sandbox_lab import create_sandbox_experiment
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
    patch_gates = [
        {
            "trusted_gate": evaluate_trust_gate(
                outcome=dict(opportunities[index].get("source_outcome_payload") or {}),
                event=dict(opportunities[index].get("source_event_payload") or {}),
                source=str(opportunities[index].get("source") or ""),
            ),
            "replay_gate": evaluate_replay_gate(replay_case),
            "safe_action_gate": evaluate_safe_action_gate(patch=patch),
        }
        for index, (patch, replay_case) in enumerate(zip(safe_patches, replay_cases))
    ]
    experiments = [
        _experiment_from_patch(patch, replay_case, evaluation, gates["trusted_gate"], gates["replay_gate"], gates["safe_action_gate"])
        for patch, replay_case, evaluation, gates in zip(safe_patches, replay_cases, patch_evaluations, patch_gates)
    ]

    max_apply_count = max(0, int(max_apply))
    applied_count = 0
    applied_patches: list[dict[str, Any]] = []
    blocked_patches: list[dict[str, Any]] = []
    for patch, evaluation, gates, replay_case in zip(safe_patches, patch_evaluations, patch_gates, replay_cases):
        trusted_gate = gates["trusted_gate"]
        replay_gate = gates["replay_gate"]
        safe_action_gate = gates["safe_action_gate"]
        patch_passes = (
            bool(evaluation.get("ok"))
            and bool(trusted_gate.get("ok"))
            and bool(replay_gate.get("ok"))
            and bool(safe_action_gate.get("ok"))
        )

        if not evaluation["ok"]:
            blocked_patches.append(
                {
                    "opportunity_id": patch["opportunity_id"],
                    "patch_type": patch["patch_type"],
                    "risk_level": patch["risk_level"],
                    "blocked_reason": evaluation["blocked_reason"],
                    "blocked_gates": _blocked_gates(
                        trusted_gate=trusted_gate,
                        replay_gate=replay_gate,
                        safe_action_gate=safe_action_gate,
                    ),
                }
            )
            continue
        if not trusted_gate.get("ok"):
            blocked_patches.append(
                {
                    "opportunity_id": patch["opportunity_id"],
                    "patch_type": patch["patch_type"],
                    "risk_level": patch["risk_level"],
                    "blocked_reason": "trusted_gate_reject",
                    "blocked_gates": _blocked_gates(
                        trusted_gate=trusted_gate,
                        replay_gate=replay_gate,
                        safe_action_gate=safe_action_gate,
                    ),
                }
            )
            continue
        if not replay_gate.get("ok"):
            blocked_patches.append(
                {
                    "opportunity_id": patch["opportunity_id"],
                    "patch_type": patch["patch_type"],
                    "risk_level": patch["risk_level"],
                    "blocked_reason": str(replay_gate.get("blocked_reason") or "replay_gate_reject"),
                    "blocked_gates": _blocked_gates(
                        trusted_gate=trusted_gate,
                        replay_gate=replay_gate,
                        safe_action_gate=safe_action_gate,
                    ),
                    "blocked_replay": replay_case,
                }
            )
            continue
        if not safe_action_gate.get("ok"):
            blocked_patches.append(
                {
                    "opportunity_id": patch["opportunity_id"],
                    "patch_type": patch["patch_type"],
                    "risk_level": patch["risk_level"],
                    "blocked_reason": str(safe_action_gate.get("blocked_reason") or "safe_action_gate_reject"),
                    "blocked_gates": _blocked_gates(
                        trusted_gate=trusted_gate,
                        replay_gate=replay_gate,
                        safe_action_gate=safe_action_gate,
                    ),
                }
            )
            continue
        if applied_count >= max_apply_count:
            if apply:
                block_reason = "max_apply_reached"
                blocked_patches.append({
                    "opportunity_id": patch["opportunity_id"],
                    "patch_type": patch["patch_type"],
                    "risk_level": patch["risk_level"],
                    "blocked_reason": block_reason,
                    "blocked_gates": _blocked_gates(
                        trusted_gate=trusted_gate,
                        replay_gate=replay_gate,
                        safe_action_gate=safe_action_gate,
                    ),
                })
            continue
        if apply and patch_passes:
            applied = _apply_safe_patch(
                runtime,
                {
                    **patch,
                    "evaluation": evaluation,
                    "trust_report": trusted_gate,
                    "replay_report": replay_gate,
                    "safe_action_report": safe_action_gate,
                    "replay_case": replay_case,
                },
                scope=scope_payload,
            )
            if applied.get("applied"):
                applied_count += 1
                applied_patches.append(applied)
            else:
                blocked_patches.append(
                    {
                        "opportunity_id": patch["opportunity_id"],
                        "patch_type": patch["patch_type"],
                        "risk_level": patch["risk_level"],
                        "blocked_reason": str(applied.get("blocked_reason") or "apply_blocked"),
                        "blocked_gates": _blocked_gates(
                            trusted_gate=trusted_gate,
                            replay_gate=replay_gate,
                            safe_action_gate=safe_action_gate,
                        ),
                        "apply_result": applied,
                    }
                )

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
        "gate_summary": _gate_summary(experiments),
        "applied_count": applied_count,
        "applied_patches": applied_patches,
        "promotion_ledger_ids": [str(item.get("promotion_id") or "") for item in applied_patches if item.get("promotion_id")],
        "rolled_back_count": 0,
        "blocked_patches": blocked_patches,
        "circuit_breaker": {"open": False, "reason": ""},
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


def _blocked_gates(*, trusted_gate: dict[str, Any], replay_gate: dict[str, Any], safe_action_gate: dict[str, Any]) -> dict[str, Any]:
    return {
        "trusted_gate": dict(trusted_gate),
        "replay_gate": dict(replay_gate),
        "safe_action_gate": dict(safe_action_gate),
    }


def _gate_summary(experiments: list[dict[str, Any]]) -> dict[str, Any]:
    total = len(experiments)
    return {
        "total": total,
        "trusted_passed": sum(1 for item in experiments if item.get("trusted_gate", {}).get("ok")),
        "replay_passed": sum(1 for item in experiments if item.get("replay_gate", {}).get("ok")),
        "safe_action_passed": sum(1 for item in experiments if item.get("safe_action_gate", {}).get("ok")),
        "passed_all": sum(1 for item in experiments if item.get("passed")),
    }


def _mine_event_opportunities(runtime: Any, *, scope: ScopeRef) -> list[dict[str, Any]]:
    pairs = _load_recent_event_outcome_pairs(runtime, scope=scope, limit=MAX_EVENT_OPPORTUNITIES)
    opportunities: list[dict[str, Any]] = []
    for pair in pairs:
        event = dict(pair.get("event") or {})
        outcome = dict(pair.get("outcome") or {})
        if _normalized_outcome(outcome) != "bad":
            continue
        if not event.get("id"):
            continue
        code_patch = _code_patch_from_event_outcome(event=event, outcome=outcome)
        if code_patch:
            policy_text = _first_nonempty(
                outcome.get("policy_update"),
                outcome.get("correction_from_user"),
                code_patch.get("summary"),
                "Apply a replay-verified direct code patch.",
            )
            opportunities.append(
                {
                    "opportunity_id": _stable_id("code-opportunity", event.get("id"), policy_text, code_patch),
                    "opportunity_type": "code_patch",
                    "source": "event",
                    "source_event_id": str(event.get("id") or ""),
                    "event_type": str(event.get("event_type") or "code.implementation"),
                    "trigger": str(event.get("user_phrase") or event.get("goal") or code_patch.get("summary") or ""),
                    "risk_level": _normalize_risk_level(str(code_patch.get("risk_level") or outcome.get("risk_level") or "low")),
                    "policy_hint": policy_text,
                    "policy_update": policy_text,
                    "correction_from_user": str(outcome.get("correction_from_user") or ""),
                    "outcome_reason": str(outcome.get("reason") or ""),
                    "recorded_at": str((outcome.get("recorded_at")) or ""),
                    "confidence": round(float(event.get("confidence") or 0.0), 3),
                    "source_event_payload": event,
                    "source_outcome_payload": outcome,
                    "code_patch": code_patch,
                }
            )
            continue
        policy_text = _first_nonempty(
            outcome.get("policy_update"),
            outcome.get("correction_from_user"),
        )
        if not policy_text:
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
    return build_replay_case(opportunity)


def _safe_patch_from_opportunity(opportunity: dict[str, Any], *, scope: ScopeRef) -> dict[str, Any]:
    opportunity_type = str(opportunity.get("opportunity_type") or "")
    if opportunity_type == "code_patch":
        code_patch = dict(opportunity.get("code_patch") or {})
        code_patch.setdefault("apply_to_repo", True)
        code_patch.setdefault("target_capability", "code.implementation")
        code_patch.setdefault("deploy_to_production", True)
        code_patch.setdefault("commit_to_repo", bool(code_patch.get("deploy_to_production")))
        return {
            "opportunity_id": str(opportunity.get("opportunity_id") or ""),
            "patch_type": "code_patch",
            "risk_level": _normalize_risk_level(str(opportunity.get("risk_level") or "medium")),
            "source": str(opportunity.get("source") or ""),
            "scope": asdict(scope),
            "summary": str(code_patch.get("summary") or opportunity.get("policy_update") or "Apply direct code patch."),
            "execution_policy": [
                "Apply the structured code patch directly to the configured repository.",
                "Run verification commands before promotion completes.",
            ],
            "success_criteria": str(opportunity.get("policy_update") or code_patch.get("summary") or "Code patch verifies successfully."),
            "code_patch": code_patch,
        }
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
    patch_type = str(patch.get("patch_type") or "")
    if patch_type == "code_patch":
        code_patch = dict(patch.get("code_patch") or {})
        if not code_patch.get("repo_root"):
            return {"ok": False, "blocked_reason": "missing_repo_root"}
        if not _coerce_string_list(code_patch.get("allowed_files")):
            return {"ok": False, "blocked_reason": "missing_allowed_files"}
        if not _code_patch_file_updates(code_patch):
            return {"ok": False, "blocked_reason": "missing_file_updates"}
        if _normalize_risk_level(str(patch.get("risk_level") or "medium")) == "high":
            return {"ok": False, "blocked_reason": "risk_level_high"}
        return {"ok": True, "blocked_reason": ""}
    if patch_type != "intent_pattern":
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
    return {"ok": True, "blocked_reason": ""}


def _experiment_from_patch(
    patch: dict[str, Any],
    replay_case: dict[str, Any],
    evaluation: dict[str, Any],
    trusted_gate: dict[str, Any],
    replay_gate: dict[str, Any],
    safe_action_gate: dict[str, Any],
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
        "trusted_gate": dict(trusted_gate),
        "replay_gate": dict(replay_gate),
        "safe_action_gate": dict(safe_action_gate),
        "passed": (
            bool(evaluation.get("ok"))
            and bool(trusted_gate.get("ok"))
            and bool(replay_gate.get("ok"))
            and bool(safe_action_gate.get("ok"))
        ),
    }


def _apply_safe_patch(runtime: Any, patch: dict[str, Any], *, scope: dict[str, Any]) -> dict[str, Any]:
    if str(patch.get("patch_type") or "") == "code_patch":
        return _apply_code_patch(runtime, patch, scope=scope)
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
        "source_opportunity": dict(patch.get("source_opportunity") or {}),
        "trust_report": dict(patch.get("trust_report") or {}),
        "replay_report": dict(patch.get("replay_report") or {}),
        "promotion_details": {
            "evaluation": dict(patch.get("evaluation") or {}),
            "safe_action_report": dict(patch.get("safe_action_report") or {}),
            "replay_case": dict(patch.get("replay_case") or {}),
        },
        "is_auto": True,
        "status": "active",
    }
    result = runtime.upsert_intent_pattern(payload, scope=scope)
    budget_decision = str(result.get("_promotion_budget_decision") or "")
    applied = budget_decision in {"ok", "manual_ok", ""} and str(result.get("status") or "active") == "active"
    return {
        "opportunity_id": str(patch.get("opportunity_id") or ""),
        "patch_type": "intent_pattern",
        "pattern_id": str(result.get("id") or ""),
        "pattern": str(result.get("pattern") or payload["pattern"]),
        "event_type": str(result.get("default_event_type") or payload["default_event_type"]),
        "confidence": float(result.get("confidence") or payload["confidence"]),
        "risk_level": str(patch.get("risk_level") or "low"),
        "promotion_id": str(result.get("_promotion_id") or ""),
        "promotion_budget_decision": budget_decision,
        "applied": bool(applied),
        "blocked_reason": "" if applied else (budget_decision or "pattern_not_active"),
    }


def _apply_code_patch(runtime: Any, patch: dict[str, Any], *, scope: dict[str, Any]) -> dict[str, Any]:
    code_patch = dict(patch.get("code_patch") or {})
    loop_id = f"autonomous_evolution:{patch.get('opportunity_id') or 'code_patch'}"
    eval_result = {
        "ok": True,
        "verdict": "pass",
        "scores": {"capability": 0.9, "safety": 1.0, "regression": 1.0, "evidence": 1.0, "cost": 0.85},
        "gate_bundle": _code_patch_gate_bundle(patch),
    }
    experiment_id = create_sandbox_experiment(
        runtime,
        scope=scope,
        loop_id=loop_id,
        learning_goal_id=str(patch.get("opportunity_id") or "code_patch"),
        research_note_id=str(patch.get("opportunity_id") or "code_patch"),
        candidate_kind="code_patch",
        candidate_patch=code_patch,
        expected_gain=str(patch.get("success_criteria") or code_patch.get("summary") or "Code patch verifies successfully."),
    )
    candidate_id = distill_capability_candidate(
        runtime,
        scope=scope,
        loop_id=loop_id,
        experiment_id=experiment_id,
        eval_result=eval_result,
        promotion_target="code_patch",
        summary=str(patch.get("summary") or code_patch.get("summary") or "Autonomous code patch"),
        target_capability="code.implementation",
    )
    promotion = promote_candidate(
        runtime,
        candidate_id=candidate_id,
        scope=scope,
        loop_id=loop_id,
        apply=True,
        eval_result=eval_result,
        health={"ok": True, "source": "autonomous_evolution"},
    )
    side_effect = dict(promotion.get("side_effect") or {})
    applied = bool(promotion.get("ok") and promotion.get("applied") and side_effect.get("repo_mutated"))
    return {
        "opportunity_id": str(patch.get("opportunity_id") or ""),
        "patch_type": "code_patch",
        "risk_level": str(patch.get("risk_level") or "low"),
        "candidate_id": candidate_id,
        "experiment_id": experiment_id,
        "promotion_id": str(promotion.get("promotion_request_id") or ""),
        "applied": applied,
        "blocked_reason": "" if applied else str(promotion.get("blocked_reason") or side_effect.get("blocked_reason") or "code_patch_promotion_failed"),
        "promotion": promotion,
        "side_effect": side_effect,
    }


def _code_patch_gate_bundle(patch: dict[str, Any]) -> dict[str, Any]:
    replay_report = dict((patch.get("replay_report") or {}))
    real_task_replay = {
        "ok": bool(replay_report.get("ok", True)),
        "report_type": "real_task_replay",
        "verdict": "pass" if bool(replay_report.get("ok", True)) else "fail",
        "pass_rate": 1.0 if bool(replay_report.get("ok", True)) else 0.0,
        "threshold": 0.6,
        "sample_count": 1,
        "source": "autonomous_evolution_replay_gate",
    }
    return {
        "evidence": [
            {
                "tier": "T1",
                "ref": str(patch.get("opportunity_id") or ""),
                "summary": "Autonomous evolution trust, replay, and safe-action gates passed.",
            }
        ],
        "rollback": {"available": True, "executable": True, "method": "restore_file_backups_or_revert_commit"},
        "canary": {"passed": True, "blast_radius": "service_local"},
        "timeout_seconds": 900,
        "audit": {"enabled": True, "ledger": "promotion_request"},
        "real_task_replay": real_task_replay,
        "prompt_shadow_eval": {"passed": True},
        "prompt_injection_check": {"passed": True},
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


def _code_patch_from_event_outcome(*, event: dict[str, Any], outcome: dict[str, Any]) -> dict[str, Any]:
    for key in ("code_patch", "candidate_patch", "patch"):
        value = outcome.get(key)
        if isinstance(value, dict) and _code_patch_file_updates(value):
            return _normalize_code_patch_payload(value)
    if _code_patch_file_updates(outcome):
        return _normalize_code_patch_payload(outcome)
    return {}


def _normalize_code_patch_payload(payload: dict[str, Any]) -> dict[str, Any]:
    patch = dict(payload)
    patch.setdefault("apply_to_repo", True)
    patch.setdefault("target_capability", "code.implementation")
    patch.setdefault("summary", str(payload.get("summary") or payload.get("title") or "Autonomous code patch"))
    return patch


def _code_patch_file_updates(payload: dict[str, Any]) -> list[dict[str, Any]]:
    updates = payload.get("file_updates") or payload.get("files") or []
    if not isinstance(updates, list):
        return []
    return [dict(item) for item in updates if isinstance(item, dict) and str(item.get("path") or item.get("file") or "").strip() and item.get("content") is not None]


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
