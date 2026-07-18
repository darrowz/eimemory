from __future__ import annotations

from dataclasses import asdict
from hashlib import sha256
from typing import Any

from eimemory.governance.capability_acceptance import (
    CORE_CAPABILITY_ACCEPTANCE_CASE_IDS,
    WEAK_CAPABILITY_ACCEPTANCE_CASE_IDS,
)
from eimemory.governance.capability_replay_packs import CORE_REPLAY_CAPABILITIES
from eimemory.governance.change_policy import decide_change_policy
from eimemory.governance.learning_state import append_learning_record_once, stable_semantic_key
from eimemory.governance.l5_readiness import readiness_gate_status
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
    replay_bootstrap: dict[str, Any] | None = None,
) -> dict[str, Any]:
    scope_ref = scope if isinstance(scope, ScopeRef) else ScopeRef.from_dict(scope)
    report = _initial_closure_report(scope_ref)

    bootstrap = (
        dict(replay_bootstrap)
        if isinstance(replay_bootstrap, dict)
        else run_weak_capability_replay_gate(runtime, scope=scope_ref, persist=persist, loop_id=LOOP_ID)
    )
    acceptance = bootstrap.get("capability_acceptance") if isinstance(bootstrap.get("capability_acceptance"), dict) else {}
    report["capability_acceptance"] = acceptance
    report["sequence"].append("acceptance")
    if not _acceptance_gate(acceptance, expected_count=len(WEAK_CAPABILITY_ACCEPTANCE_CASE_IDS)):
        return _blocked_closure(report, *(bootstrap.get("blocked_reasons") or ["capability_acceptance_failed"]))

    weak_capability_replay = (
        bootstrap.get("weak_capability_replay")
        if isinstance(bootstrap.get("weak_capability_replay"), dict)
        else {}
    )
    replay_gate = bootstrap.get("replay_gate") if isinstance(bootstrap.get("replay_gate"), dict) else {}
    report["weak_capability_replay"] = weak_capability_replay
    report["replay_gate"] = replay_gate
    report["sequence"].append("replay")
    if bootstrap.get("ok") is not True or replay_gate.get("ok") is not True:
        return _blocked_closure(
            report,
            *(bootstrap.get("blocked_reasons") or replay_gate.get("blocked_reasons") or ["weak_capability_replay_invalid"]),
        )

    core_acceptance = runtime.run_capability_acceptance(
        scope=asdict(scope_ref),
        persist=persist,
        case_ids=list(CORE_CAPABILITY_ACCEPTANCE_CASE_IDS),
    )
    report["core_capability_acceptance"] = core_acceptance
    report["sequence"].append("core_acceptance")
    if not _acceptance_gate(core_acceptance, expected_count=len(CORE_CAPABILITY_ACCEPTANCE_CASE_IDS)):
        return _blocked_closure(report, "core_capability_acceptance_failed")

    core_execution_id = str(core_acceptance.get("execution_id") or "").strip()
    core_probe_ids_by_case = {
        str(result.get("case_id") or "").strip(): str(result.get("probe_record_id") or "").strip()
        for result in core_acceptance.get("results") or []
        if isinstance(result, dict)
    }
    if (
        not core_execution_id
        or set(core_probe_ids_by_case) != set(CORE_CAPABILITY_ACCEPTANCE_CASE_IDS)
        or any(not value for value in core_probe_ids_by_case.values())
    ):
        return _blocked_closure(report, "core_acceptance_anchor_missing")

    core_capability_replay = runtime.build_capability_replay_packs(
        scope=asdict(scope_ref),
        capabilities=list(CORE_REPLAY_CAPABILITIES),
        persist=persist,
        loop_id=f"{LOOP_ID}_core",
        acceptance_execution_id=core_execution_id,
        acceptance_probe_ids_by_case=core_probe_ids_by_case,
    )
    core_replay_gate = _capability_replay_gate(
        core_capability_replay,
        expected_capabilities=CORE_REPLAY_CAPABILITIES,
        reason_prefix="core_capability_replay",
    )
    report["core_capability_replay"] = core_capability_replay
    report["core_replay_gate"] = core_replay_gate
    report["sequence"].append("core_replay")
    if core_replay_gate.get("ok") is not True:
        return _blocked_closure(
            report,
            *(core_replay_gate.get("blocked_reasons") or ["core_capability_replay_invalid"]),
        )

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
    report["correction_replay"] = correction_replay
    report["sequence"].append("skill_rollback")
    if correction_replay.get("ok") is not True:
        return _blocked_closure(report, "correction_replay_failed")

    pre_answer_gate = runtime.build_ground_truth_pre_answer_gate(
        query=CORRECTION_QUERY,
        scope=asdict(scope_ref),
        persist=persist,
    )
    report["pre_answer_gate"] = pre_answer_gate
    if int(pre_answer_gate.get("matched_rule_count") or 0) < 1:
        return _blocked_closure(report, "ground_truth_rule_not_matched")

    playbook_ids = _seed_eiskill_playbooks(runtime, scope=scope_ref, persist=persist)
    skill_promotion = runtime.promote_repeated_sops_to_skill_candidates(
        scope=asdict(scope_ref), min_repeats=3, persist=persist, limit=50
    )
    report["playbook_record_ids"] = playbook_ids
    report["skill_promotion"] = skill_promotion
    skill_id = str((skill_promotion.get("skills") or [{}])[0].get("skill_id") or "")
    if not skill_id:
        return _blocked_closure(report, "skill_call_failed")
    skill_call = runtime.call_eiskill(
        skill_id=skill_id,
        scope=asdict(scope_ref),
        context={"query": CORRECTION_QUERY, "rehearsal": True},
        persist=persist,
    )
    report["skill_call"] = skill_call
    if skill_call.get("ok") is not True:
        return _blocked_closure(report, "skill_call_failed")

    rollback = _run_non_destructive_rollback(runtime, scope=scope_ref, persist=persist)
    report["rollback"] = rollback
    if rollback.get("ok") is not True:
        return _blocked_closure(report, "rollback_rehearsal_failed")

    observation_input = _observation_autonomous_report(
        replay_report=weak_capability_replay,
        skill_promotion=skill_promotion,
        rollback=rollback,
    )
    l5_observation = runtime.run_l5_cycle(
        scope=asdict(scope_ref),
        apply=False,
        force=False,
        max_goals=1,
        max_promotions=0,
        allow_network=False,
        loop_id=f"{LOOP_ID}_observation",
        persist=persist,
        autonomous_learning_report=observation_input,
    )
    report["l5_observation"] = l5_observation
    report["sequence"].append("l5_observation_assessment")
    assessment = l5_observation.get("assessment") if isinstance(l5_observation.get("assessment"), dict) else {}
    if l5_observation.get("ok") is not True or assessment.get("complete") is not True or assessment.get("level") != "L5":
        return _blocked_closure(report, "l5_observation_assessment_incomplete")

    capability_dashboard = runtime.build_capability_dashboard_metrics(
        scope=asdict(scope_ref), persist=persist, loop_id=LOOP_ID
    )
    report["capability_dashboard"] = capability_dashboard
    report["sequence"].append("dashboard")
    if capability_dashboard.get("ok") is not True:
        return _blocked_closure(report, "capability_dashboard_failed")

    l5_readiness = runtime.build_l5_readiness_report(
        scope=asdict(scope_ref), persist=persist, loop_id=LOOP_ID
    )
    report["l5_readiness"] = l5_readiness
    report["sequence"].append("readiness")
    readiness_status = readiness_gate_status(l5_readiness)
    if not readiness_status:
        return _blocked_closure(report, "l5_readiness_not_l5")
    report["outcome_trace"] = _record_successful_task_outcome(runtime, scope=scope_ref, persist=persist)
    report["ok"] = True
    report["closure_complete"] = readiness_status == "L5"
    report["data_accumulating"] = readiness_status == "data_accumulating"
    report["change_policy"] = decide_change_policy(
        event="code_change",
        closure_complete=bool(report["closure_complete"]),
    )
    report["blocked_reasons"] = []
    return report


def run_weak_capability_replay_gate(
    runtime: Any,
    *,
    scope: dict[str, Any] | ScopeRef | None = None,
    persist: bool = True,
    loop_id: str = LOOP_ID,
) -> dict[str, Any]:
    scope_ref = scope if isinstance(scope, ScopeRef) else ScopeRef.from_dict(scope)
    acceptance = runtime.run_capability_acceptance(
        scope=asdict(scope_ref),
        persist=persist,
        case_ids=list(WEAK_CAPABILITY_ACCEPTANCE_CASE_IDS),
    )
    report = {
        "ok": False,
        "report_type": "weak_capability_replay_gate",
        "scope": asdict(scope_ref),
        "capability_acceptance": acceptance,
        "weak_capability_replay": {},
        "replay_gate": {"ok": False, "blocked_reasons": []},
        "blocked_reasons": [],
    }
    if not _acceptance_gate(acceptance, expected_count=len(WEAK_CAPABILITY_ACCEPTANCE_CASE_IDS)):
        report["blocked_reasons"] = ["capability_acceptance_failed"]
        return report

    weak_capability_replay = runtime.build_capability_replay_packs(
        scope=asdict(scope_ref),
        capabilities=WEAK_REPLAY_CAPABILITIES,
        persist=persist,
        loop_id=loop_id,
        acceptance_execution_id=str(acceptance.get("execution_id") or ""),
        acceptance_probe_ids_by_case={
            str(result.get("case_id") or ""): str(result.get("probe_record_id") or "")
            for result in acceptance.get("results") or []
            if isinstance(result, dict)
        },
    )
    replay_gate = _weak_replay_gate(weak_capability_replay)
    report["weak_capability_replay"] = weak_capability_replay
    report["replay_gate"] = replay_gate
    report["blocked_reasons"] = list(replay_gate.get("blocked_reasons") or [])
    report["ok"] = replay_gate.get("ok") is True
    return report


def _initial_closure_report(scope: ScopeRef) -> dict[str, Any]:
    not_run = {"ok": False, "status": "not_run", "reason": "upstream_gate_not_run"}
    return {
        "ok": False,
        "closure_complete": False,
        "data_accumulating": False,
        "blocked_reasons": [],
        "report_type": "l5_closure_rehearsal",
        "scope": asdict(scope),
        "sequence": [],
        "capability_acceptance": dict(not_run),
        "correction_replay": dict(not_run),
        "pre_answer_gate": dict(not_run),
        "outcome_trace": dict(not_run),
        "playbook_record_ids": [],
        "weak_capability_replay": dict(not_run),
        "replay_gate": {**not_run, "blocked_reasons": []},
        "core_capability_acceptance": dict(not_run),
        "core_capability_replay": dict(not_run),
        "core_replay_gate": {**not_run, "blocked_reasons": []},
        "change_policy": decide_change_policy(event="code_change", closure_complete=False),
        "skill_promotion": {**not_run, "skills": []},
        "skill_call": dict(not_run),
        "rollback": dict(not_run),
        "l5_observation": dict(not_run),
        "capability_dashboard": dict(not_run),
        "l5_readiness": dict(not_run),
    }


def _blocked_closure(report: dict[str, Any], *reasons: str) -> dict[str, Any]:
    report["ok"] = False
    report["closure_complete"] = False
    report["blocked_reasons"] = list(dict.fromkeys(str(reason) for reason in reasons if str(reason)))
    return report


def _acceptance_gate(report: dict[str, Any], *, expected_count: int) -> bool:
    return bool(
        report.get("ok") is True
        and report.get("all_passed") is True
        and int(report.get("case_count") or 0) == expected_count
        and int(report.get("pass_count") or 0) == expected_count
        and report.get("distinct_probe_sources") is True
        and report.get("distinct_trace_ids") is True
    )


def _observation_autonomous_report(
    *,
    replay_report: dict[str, Any],
    skill_promotion: dict[str, Any],
    rollback: dict[str, Any],
) -> dict[str, Any]:
    results = [
        result
        for pack in replay_report.get("packs") or []
        if isinstance(pack, dict)
        for result in pack.get("case_results") or []
        if isinstance(result, dict)
    ]
    pass_count = sum(1 for result in results if str(result.get("verdict") or "").lower() == "pass")
    fail_count = sum(1 for result in results if str(result.get("verdict") or "").lower() == "fail")
    sample_count = pass_count + fail_count
    candidate_ids = [str(value) for value in skill_promotion.get("candidate_ids") or [] if str(value)]
    return {
        "ok": sample_count > 0 and pass_count == sample_count and bool(candidate_ids) and bool(rollback.get("ledger_id")),
        "loop_id": f"{LOOP_ID}_evidence",
        "candidate_id": candidate_ids[0] if candidate_ids else "",
        "candidate_ids": candidate_ids,
        "real_task_replay": {
            "ok": sample_count > 0 and pass_count == sample_count,
            "persisted_record_id": str(replay_report.get("manifest_record_id") or ""),
            "verdict": "pass" if sample_count > 0 and pass_count == sample_count else "fail",
            "sample_count": sample_count,
            "pass_count": pass_count,
            "fail_count": fail_count,
            "pass_rate": round(pass_count / sample_count, 3) if sample_count else 0.0,
        },
        "replay_gate_passed": sample_count > 0 and pass_count == sample_count,
        "blocked_reason": "observation_mode_no_apply",
        "promotion": {
            "ok": True,
            "applied": False,
            "blocked_reason": "observation_mode_no_apply",
        },
        "promotions": [],
    }


def _weak_replay_gate(report: dict[str, Any]) -> dict[str, Any]:
    return _capability_replay_gate(
        report,
        expected_capabilities=WEAK_REPLAY_CAPABILITIES,
        reason_prefix="weak_capability_replay",
    )


def _capability_replay_gate(
    report: dict[str, Any],
    *,
    expected_capabilities: list[str],
    reason_prefix: str,
) -> dict[str, Any]:
    packs = [pack for pack in report.get("packs") or [] if isinstance(pack, dict)]
    blocked_reasons: list[str] = []
    capabilities = [str(pack.get("capability") or "") for pack in packs]
    if (
        report.get("ok") is not True
        or len(packs) != len(expected_capabilities)
        or sorted(capabilities) != sorted(expected_capabilities)
    ):
        blocked_reasons.append(f"{reason_prefix}_invalid")
    not_executed: list[str] = []
    failed: list[str] = []
    duplicate_evidence: list[str] = []
    for pack in packs:
        capability = str(pack.get("capability") or "")
        results = [item for item in pack.get("case_results") or [] if isinstance(item, dict)]
        case_count = len(pack.get("cases") or [])
        executed = [item for item in results if str(item.get("verdict") or "").lower() in {"pass", "fail"}]
        if case_count <= 0 or len(executed) != case_count:
            not_executed.append(capability)
            continue
        threshold = max((float(case.get("threshold") or 0.8) for case in pack.get("cases") or [] if isinstance(case, dict)), default=0.8)
        if float(pack.get("pass_rate") or 0.0) < threshold:
            failed.append(capability)
        source_ids = {str(item.get("evidence_source_id") or "") for item in executed if str(item.get("evidence_source_id") or "")}
        if len(source_ids) != case_count:
            duplicate_evidence.append(capability)
    if not_executed:
        blocked_reasons.append(f"{reason_prefix}_not_executed")
    if failed:
        blocked_reasons.append(f"{reason_prefix}_failed")
    if duplicate_evidence:
        blocked_reasons.append(f"{reason_prefix}_evidence_not_distinct")
    return {
        "ok": not blocked_reasons,
        "blocked_reasons": blocked_reasons,
        "not_executed_capabilities": sorted(not_executed),
        "failed_capabilities": sorted(failed),
        "duplicate_evidence_capabilities": sorted(duplicate_evidence),
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
            "rehearsal": True,
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
            "rehearsal": True,
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
