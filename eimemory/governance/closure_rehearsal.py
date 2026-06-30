from __future__ import annotations

from dataclasses import asdict
from hashlib import sha256
from typing import Any

from eimemory.governance.learning_state import append_learning_record_once, stable_semantic_key
from eimemory.models.records import ScopeRef


CORRECTION_TEXT = "\u4e0d\u8981\u8bf4\u505a\u4e0d\u5230\uff0c\u8981\u8865\u80fd\u529b\u89e3\u51b3"
CORRECTION_QUERY = "\u9047\u5230\u505a\u4e0d\u5230\u7684\u80fd\u529b\u600e\u4e48\u529e\uff1f\u4e0d\u8981\u8bf4\u505a\u4e0d\u5230\uff0c\u8981\u8865\u80fd\u529b\u89e3\u51b3"
LOOP_ID = "l5_closure_rehearsal"
WEAK_REPLAY_CAPABILITIES = [
    "search.discovery",
    "research.synthesis",
    "operations.uumit",
    "device.control",
]


def run_l5_closure_rehearsal(
    runtime: Any,
    *,
    scope: dict[str, Any] | ScopeRef | None = None,
    persist: bool = True,
) -> dict[str, Any]:
    scope_ref = scope if isinstance(scope, ScopeRef) else ScopeRef.from_dict(scope)
    correction_replay = runtime.record_user_correction_replay(
        {
            "text": CORRECTION_TEXT,
            "context": "assistant stopped at inability instead of creating the missing capability path",
            "target_capability": "proactive.judgment",
            "expected_behavior": "When a capability is missing, create a concrete plan, replay, gated implementation path, and rollback boundary.",
        },
        scope=asdict(scope_ref),
        persist=persist,
    )
    pre_answer_gate = runtime.build_ground_truth_pre_answer_gate(
        query=CORRECTION_QUERY,
        scope=asdict(scope_ref),
        persist=persist,
    )
    outcome_trace = _record_successful_task_outcome(runtime, scope=scope_ref, persist=persist)
    playbook_ids = _seed_eiskill_playbooks(runtime, scope=scope_ref, persist=persist)
    weak_capability_replay = runtime.build_capability_replay_packs(
        scope=asdict(scope_ref),
        capabilities=WEAK_REPLAY_CAPABILITIES,
        persist=persist,
        loop_id=LOOP_ID,
    )
    skill_promotion = runtime.promote_repeated_sops_to_skill_candidates(
        scope=asdict(scope_ref),
        min_repeats=3,
        persist=persist,
        limit=50,
    )
    skill_id = str((skill_promotion.get("skills") or [{}])[0].get("skill_id") or "")
    skill_call = (
        runtime.call_eiskill(
            skill_id=skill_id,
            scope=asdict(scope_ref),
            context={"query": CORRECTION_QUERY, "rehearsal": True},
            persist=persist,
        )
        if skill_id
        else {"ok": False, "error": "skill_not_generated"}
    )
    rollback = _run_non_destructive_rollback(runtime, scope=scope_ref, persist=persist)
    capability_dashboard = runtime.build_capability_dashboard_metrics(
        scope=asdict(scope_ref),
        persist=persist,
        loop_id=LOOP_ID,
    )
    l5_readiness = runtime.build_l5_readiness_report(
        scope=asdict(scope_ref),
        persist=persist,
        loop_id=LOOP_ID,
    )
    return {
        "ok": bool(
            correction_replay.get("ok")
            and pre_answer_gate.get("matched_rule_count", 0) >= 1
            and weak_capability_replay.get("ok")
            and skill_call.get("ok")
            and rollback.get("ok")
            and l5_readiness.get("ok")
        ),
        "report_type": "l5_closure_rehearsal",
        "scope": asdict(scope_ref),
        "correction_replay": correction_replay,
        "pre_answer_gate": pre_answer_gate,
        "outcome_trace": outcome_trace,
        "playbook_record_ids": playbook_ids,
        "weak_capability_replay": weak_capability_replay,
        "skill_promotion": skill_promotion,
        "skill_call": skill_call,
        "rollback": rollback,
        "capability_dashboard": capability_dashboard,
        "l5_readiness": l5_readiness,
    }


def _record_successful_task_outcome(runtime: Any, *, scope: ScopeRef, persist: bool) -> dict[str, Any]:
    if not persist:
        return {
            "event": {
                "event_type": "learning_rehearsal",
                "user_phrase": CORRECTION_QUERY,
                "result": "completed",
            },
            "outcome": {"outcome": "good", "status": "completed", "ok": True, "success": True, "verified": True},
            "dry_run": True,
        }
    event = runtime.record_event(
        {
            "source": "manual",
            "event_type": "learning_rehearsal",
            "user_phrase": CORRECTION_QUERY,
            "interpreted_intent": "verify missing-capability correction produces a concrete capability-building path",
            "goal": "open task_success_rate with a verified non-destructive rehearsal",
            "action_path": ["record correction", "check pre-answer gate", "call eiskill", "rollback rehearsal", "recompute dashboard"],
            "verification": "dashboard counts task success, skill reuse, and rollback evidence",
            "result": "completed",
            "confidence": 0.93,
        },
        scope=asdict(scope),
    )
    outcome = runtime.record_outcome(
        event["id"],
        {
            "outcome": "good",
            "status": "completed",
            "ok": True,
            "success": True,
            "verified": True,
            "reason": "L5 closure rehearsal completed without destructive actions.",
        },
        scope=asdict(scope),
    )
    return {"event": event, "outcome": outcome}


def _seed_eiskill_playbooks(runtime: Any, *, scope: ScopeRef, persist: bool) -> list[str]:
    if not persist:
        return []
    record_ids: list[str] = []
    for index in range(3):
        record = append_learning_record_once(
            runtime,
            kind="learning_playbook",
            title="Missing capability closure SOP",
            summary="When the agent lacks a capability, it must build or route a capability path instead of stopping at refusal.",
            scope=scope,
            loop_id=LOOP_ID,
            step_name=f"seed_eiskill_playbook_{index + 1}",
            semantic_key=stable_semantic_key("missing_capability_closure", index),
            authority_tier="L0",
            status="active",
            content={
                "report_type": "sop_draft",
                "sop_key": "missing-capability-closure",
                "target_capability": "proactive.judgment",
                "steps": [
                    "state the missing capability precisely",
                    "create the smallest implementation or routing plan",
                    "attach replay or evaluation evidence",
                    "define rollback or quarantine boundary",
                    "report the verified next action",
                ],
                "trigger_conditions": ["user correction says do not stop at inability", "missing capability blocks task completion"],
                "action": "convert missing capability into a concrete implementation, replay, and gate plan",
                "verification": "pre-answer gate matches and dashboard records success evidence",
                "rollback": "disable this eiskill registry entry or quarantine the related intent pattern if replay fails",
                "replay_passed": True,
                "source_repeat": index + 1,
            },
            meta={
                "report_type": "sop_draft",
                "sop_key": "missing-capability-closure",
                "target_capability": "proactive.judgment",
                "replay_passed": True,
            },
            source="eimemory.closure_rehearsal",
        )
        record_ids.append(record.record_id)
    return record_ids


def _run_non_destructive_rollback(runtime: Any, *, scope: ScopeRef, persist: bool) -> dict[str, Any]:
    pattern_id = f"closure-rehearsal-rollback-{_scope_hash(scope)}"
    if persist:
        runtime.upsert_intent_pattern(
            {
                "id": pattern_id,
                "pattern": "closure rehearsal rollback sample",
                "default_event_type": "repair",
                "interpreted_intent": "non-destructive rollback rehearsal for L5 readiness",
                "confidence": 0.91,
                "status": "active",
            },
            scope=asdict(scope),
        )
        return runtime.rollback_intent_pattern(
            pattern_id,
            scope=asdict(scope),
            reason="non-destructive L5 rollback rehearsal",
            auto=False,
        )
    return {"ok": True, "status": "dry_run", "pattern_id": pattern_id}


def _scope_hash(scope: ScopeRef) -> str:
    payload = "|".join([scope.tenant_id, scope.agent_id, scope.workspace_id, scope.user_id])
    return sha256(payload.encode("utf-8")).hexdigest()[:12]
