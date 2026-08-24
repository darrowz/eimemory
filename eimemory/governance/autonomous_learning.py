from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict
import json
import os
from typing import Any

from eimemory.evaluation.capability_catalog import CapabilityEvaluationCatalog
from eimemory.governance.capability_attribution import attribute_capability_outcomes
from eimemory.governance.capability_distiller import distill_capability_candidate
from eimemory.governance.capability_hypotheses import (
    CapabilityHypothesisError,
    explicit_hypothesis_reference,
    hypothesis_behavior_gate,
    record_hypothesis_experiment_feedback,
)
from eimemory.governance.capability_ledger import build_capability_ledger, record_capability_score
from eimemory.governance.capability_dashboard import build_capability_dashboard_metrics
from eimemory.governance.capability_replay_packs import (
    build_capability_replay_packs,
    capability_replay_case_ids,
)
from eimemory.governance.capability_seeding import ensure_all_seeded
from eimemory.governance.code_patch_command_policy import AUTOMATION_POLICY_ACTIONS
from eimemory.governance.code_evolution_bridge import propose_code_patch
from eimemory.governance.curiosity import generate_learning_goals, persist_learning_goals
from eimemory.governance.evidence_collector import collect
from eimemory.governance.evidence_contract import ReleaseIdentity, current_release_identity
from eimemory.governance.goal_graph import build_goal_graph_loop
from eimemory.governance.goal_registry import load_goal_registry
from eimemory.governance.isolated_evaluator import (
    build_evaluation_packet,
    judge_stop_condition,
    run_isolated_evaluator,
)
from eimemory.governance.learning_eval import run_learning_eval
from eimemory.governance.learning_retention import compact_learning_records
from eimemory.governance.learning_state import (
    active_learning_loops,
    complete_learning_loop,
    mark_step,
    stable_semantic_key,
    start_learning_loop,
)
from eimemory.governance.promotion_manager import (
    promote_candidate,
    recover_incomplete_code_apply,
)
from eimemory.governance.prompt_safety import (
    prompt_injection_check,
    prompt_shadow_eval,
    run_prompt_safety_battery,
)
from eimemory.governance.replay_dataset import build_replay_dataset
from eimemory.governance.regression_watch import run_regression_watch
from eimemory.governance.research_planner import create_research_note, create_research_task, plan_research_tasks
from eimemory.governance.sandbox_lab import create_sandbox_experiment
from eimemory.governance.self_model import build_self_model
from eimemory.governance.safety_replay import run_safety_boundary_replay
from eimemory.governance.signal_intake import rank_learning_signals
from eimemory.governance.skill_sedimentation import promote_repeated_sops_to_skill_candidates
from eimemory.governance.thoughts import generate_thoughts
from eimemory.governance.world_watchers import collect_world_signals, default_watches
from eimemory.llm import llm_client_from_env
from eimemory.models.records import RecordEnvelope, ScopeRef


# These identifiers describe the pre-Registry autonomous-learning behavior.
# They are never a default capability selection: callers must intentionally
# opt into ``legacy_compatibility`` to replay the historic maintenance path.
LEGACY_AUTONOMOUS_LEARNING_CAPABILITIES = (
    "memory.recall",
    "tool.routing",
    "safety.boundary",
)
LEGACY_AUTONOMOUS_LEARNING_FALLBACK_CAPABILITY = "proactive.judgment"

# Candidate kinds are artifact contracts, not capability categories.  A
# dynamic goal must declare one of them (directly or through its explicit
# expected-artifact alias); capability names and prose never choose a kind.
_CANDIDATE_KINDS = frozenset(
    {
        "tool_route",
        "memory_rule",
        "eval_case",
        "skill_draft",
        "sop_draft",
        "source_policy",
        "code_patch",
    }
)
_EXPLICIT_ARTIFACT_KIND_ALIASES: dict[str, tuple[str, ...]] = {
    "tool_route": ("tool_route",),
    "memory_rule": ("memory_rule",),
    "eval_case": ("eval_case",),
    "skill_draft": ("skill_draft",),
    "sop_draft": ("sop_draft",),
    "source_policy": ("source_policy",),
    "code_patch": ("code_patch",),
    "playbook_or_policy_candidate": ("sop_draft", "eval_case"),
    "policy_candidate": ("sop_draft", "eval_case"),
    # Thought-queue and long-term registry goals declare this composite
    # alias.  Without it, the dynamic path sees a declared artifact but
    # cannot map it to a candidate kind, so the loop reports
    # no_accepted_capability_goal_with_declared_artifact and converts nothing.
    "rule_sop_eval_or_skill": ("sop_draft", "eval_case", "skill_draft"),
}


def classify_autonomous_learning_activity(
    report: dict[str, Any],
    *,
    timeout_exceeded: bool = False,
    error_reason: str = "",
) -> dict[str, Any]:
    candidate_specs = [item for item in report.get("candidate_specs") or [] if isinstance(item, dict)]
    eval_record_ids = [str(item) for item in report.get("eval_record_ids") or [] if str(item or "").strip()]
    candidate_ids = [str(item) for item in report.get("candidate_ids") or [] if str(item or "").strip()]
    promotions = [item for item in report.get("promotions") or [] if isinstance(item, dict)]
    attempted_count = max(len(candidate_specs), len(eval_record_ids))
    if timeout_exceeded:
        status, reason = "failed", "timeout_exceeded"
    elif report.get("ok") is not True:
        status, reason = "failed", str(error_reason or report.get("error") or "cycle_failed")
    elif attempted_count or candidate_ids or promotions:
        status, reason = "active", "candidate_evaluation_attempted"
    elif str(report.get("eval_verdict") or "").strip().lower() in {
        "fail",
        "failed",
        "blocked",
        "reject",
        "rejected",
    }:
        status, reason = "active", "evaluation_gate_failed"
    elif any(
        key in report and report.get(key) is False
        for key in ("replay_gate_passed", "safety_gate_passed", "isolation_gate_passed")
    ):
        status, reason = "active", "evidence_gate_failed"
    else:
        status, reason = "idle", "no_candidate_change"
    result = {
        "activity_status": status,
        "activity_reason": reason,
        "attempted_candidate_count": attempted_count,
    }
    return result


def run_autonomous_learning_cycle(
    runtime: Any,
    *,
    scope: dict[str, Any] | ScopeRef | None = None,
    apply: bool = False,
    dry_run: bool = False,
    full: bool = True,
    force: bool = False,
    max_goals: int = 3,
    max_promotions: int | None = None,
    allow_network: bool | None = None,
    profile_key: str = "",
    capability_scope: str = "global",
    runtime_scope: ScopeRef | Mapping[str, Any] | None = None,
    at_time: str = "",
    catalog: CapabilityEvaluationCatalog | None = None,
    legacy_compatibility: bool = False,
) -> dict[str, Any]:
    scope_ref = scope if isinstance(scope, ScopeRef) else ScopeRef.from_dict(scope)
    # A caller-selected catalog is a stronger binding than the Runtime startup
    # catalog.  Neither path falls back to a process-global catalog.
    effective_catalog = catalog if catalog is not None else getattr(runtime, "capability_catalog", None)
    network_enabled = _network_enabled(allow_network)
    if dry_run:
        return _run_autonomous_learning_dry_run(
            runtime,
            scope=scope_ref,
            apply=apply,
            full=full,
            max_goals=max_goals,
            allow_network=network_enabled,
            profile_key=profile_key,
            capability_scope=capability_scope,
            runtime_scope=runtime_scope,
            at_time=at_time,
            legacy_compatibility=legacy_compatibility,
        )
    code_apply_recovery = _recover_code_apply_before_cycle(
        runtime,
        scope=scope_ref,
        apply=apply,
    )
    loop = start_learning_loop(runtime, scope=scope_ref, trigger="learn_cycle", dry_run=dry_run, force=force)
    loop_id = str(loop.meta.get("loop_id") or loop.content.get("loop_id") or loop.record_id)
    try:
        try:
            preexisting_outcome_attribution = attribute_capability_outcomes(
                runtime,
                scope=scope_ref,
                loop_id="outcome_attribution",
                catalog=effective_catalog,
                legacy_compatibility=legacy_compatibility,
            )
        except Exception as exc:
            preexisting_outcome_attribution = {
                "ok": False,
                "status": "failed",
                "reason": "preexisting_outcome_attribution_failed",
                "error_type": type(exc).__name__,
                "record_count": 0,
                "record_ids": [],
                "capabilities": {},
            }
        watch_report = collect_world_signals(
            runtime,
            scope=scope_ref,
            watches=default_watches(),
            dry_run=dry_run,
            loop_id=loop_id,
            profile_key=profile_key,
            capability_scope=capability_scope,
            at_time=at_time,
            legacy_compatibility=legacy_compatibility,
        )
        mark_step(runtime, loop, step_name="observe", status="completed", record_ids=watch_report.get("persisted_record_ids") or [], metrics={"signal_count": watch_report.get("signal_count", 0)})

        self_model = build_self_model(
            runtime,
            scope=scope_ref,
            persist=True,
            loop_id=loop_id,
            profile_key=profile_key,
            capability_scope=capability_scope,
            at_time=at_time,
        )
        mark_step(runtime, loop, step_name="self_model", status="completed", metrics=self_model.get("metrics") or {})

        # Dynamic learning must resolve an actual active registry/profile.  The
        # historic seeding cohort is only available to an explicit legacy
        # replay, never as a way to manufacture current capabilities.
        if legacy_compatibility:
            ensure_all_seeded(
                runtime,
                scope=scope_ref,
                loop_id=loop_id,
                legacy_compatibility=True,
            )
            self_model = {
                **self_model,
                "capabilities": _legacy_self_model_capabilities(runtime, scope=scope_ref),
                "legacy_compatibility": True,
            }
        capability_selection = _resolve_autonomous_capability_selection(
            runtime,
            scope=scope_ref,
            runtime_scope=runtime_scope,
            profile_key=profile_key,
            capability_scope=capability_scope,
            at_time=at_time,
            legacy_compatibility=legacy_compatibility,
        )
        selected_capability_ids = list(capability_selection.get("capability_ids") or [])
        goal_registry = load_goal_registry(legacy_compatibility=legacy_compatibility)
        ranked_signals = rank_learning_signals(watch_report.get("signals") or [], self_model, [], max_items=20)
        thought_report = generate_thoughts(
            runtime,
            signals=ranked_signals,
            self_model=self_model,
            goals=list(goal_registry.get("long_term") or []),
            scope=scope_ref,
            loop_id=loop_id,
            persist=True,
            max_items=20,
            profile_key=profile_key,
            capability_scope=capability_scope,
            at_time=at_time,
            legacy_compatibility=legacy_compatibility,
        )
        mark_step(runtime, loop, step_name="think", status="completed", record_ids=thought_report.get("persisted_record_ids") or [], metrics={"thought_count": thought_report.get("thought_count", 0)})

        goals = generate_learning_goals(
            self_model,
            ranked_signals,
            goal_registry=goal_registry,
            thoughts=[] if legacy_compatibility else thought_report.get("thoughts") or [],
            max_goals=max_goals,
        )
        goal_ids = persist_learning_goals(runtime, goals, scope=scope_ref, loop_id=loop_id)
        selected_goal = goals[0] if goals else _fallback_goal(legacy_compatibility=legacy_compatibility)
        selected_goal_id = goal_ids[0] if goal_ids else ""
        mark_step(runtime, loop, step_name="goals", status="completed", record_ids=goal_ids, metrics={"goal_count": len(goals)})

        research_tasks = plan_research_tasks(selected_goal, source_policy={"network_enabled": network_enabled})
        research_task_ids = [
            create_research_task(runtime, scope=scope_ref, loop_id=loop_id, goal_id=selected_goal_id or selected_goal.get("semantic_key", ""), task=task)
            for task in research_tasks
        ]
        evidence: list[dict[str, Any]] = []
        for task in research_tasks:
            evidence.extend(collect(task, runtime=runtime, scope=scope_ref))
        network_research = _network_research_summary(enabled=network_enabled, tasks=research_tasks, evidence=evidence)
        if not evidence or _all_t6_evidence(evidence):
            evidence = [
                {
                    "tier": "T2",
                    "kind": "self_model",
                    "ref": loop.record_id,
                    "summary": "Self-model indicated an autonomous maintenance opportunity.",
                    "confidence": 0.5,
                }
            ]
        research_note_id = create_research_note(
            runtime,
            scope=scope_ref,
            loop_id=loop_id,
            learning_goal_id=selected_goal_id or str(selected_goal.get("semantic_key") or ""),
            title=f"Research: {selected_goal.get('title') or 'autonomous learning'}",
            summary=str(selected_goal.get("question") or selected_goal.get("success_criteria") or "Autonomous learning research note"),
            evidence=evidence[:20],
            applicability_score=_evidence_score(evidence),
            risk_tier=str(selected_goal.get("authority_tier") or "L0"),
            capability_hypothesis=explicit_hypothesis_reference(selected_goal),
        )
        mark_step(runtime, loop, step_name="research", status="completed", record_ids=research_task_ids + [research_note_id], metrics={"evidence_count": len(evidence)})

        replay_dataset = build_replay_dataset(
            runtime,
            scope=scope_ref,
            limit=50,
            persist=True,
            loop_id=loop_id,
            capability_scope=capability_scope,
            profile_key=profile_key,
            catalog=effective_catalog,
            at_time=at_time,
            legacy_compatibility=legacy_compatibility,
        )
        mark_step(runtime, loop, step_name="replay_dataset", status="completed", record_ids=[replay_dataset.get("persisted_record_id", "")], metrics={"case_count": replay_dataset.get("case_count", 0), "correction_count": replay_dataset.get("correction_count", 0)})
        real_task_replay = _run_real_task_replay_if_available(
            runtime=runtime,
            loop=loop,
            scope=scope_ref,
            replay_dataset=replay_dataset,
            seed_records=_replay_seed_records_from_cases(replay_dataset.get("cases") or [], scope=scope_ref),
            legacy_compatibility=legacy_compatibility,
        )
        replay_gate = _replay_gate_report(real_task_replay)
        replay_gate_passed = bool(replay_gate.get("ok"))
        loop_capabilities = _loop_capabilities(
            goals or [selected_goal],
            allowed_capability_ids=selected_capability_ids,
            legacy_compatibility=legacy_compatibility,
        )
        goal_graph = build_goal_graph_loop(
            runtime,
            scope=scope_ref,
            max_goals=max_goals,
            persist=True,
            capabilities=loop_capabilities,
            loop_id=loop_id,
            capability_scope=capability_scope,
            profile_key=profile_key,
            catalog=effective_catalog,
            legacy_compatibility=legacy_compatibility,
        )
        mark_step(
            runtime,
            loop,
            step_name="goal_graph",
            status="completed",
            record_ids=[item for item in [goal_graph.get("persisted_record_id", ""), *(goal_graph.get("episode_event_ids") or [])] if item],
            metrics={
                "root_goal_count": goal_graph.get("root_goal_count", 0),
                "task_count": goal_graph.get("task_count", 0),
                "episode_event_count": goal_graph.get("episode_event_count", 0),
            },
        )
        capability_acceptance = runtime.run_capability_acceptance(
            scope=asdict(scope_ref),
            persist=True,
            profile_key=profile_key,
            capability_scope=capability_scope,
            runtime_scope=runtime_scope if runtime_scope is not None else scope_ref,
            at_time=at_time,
            catalog=effective_catalog,
            legacy_compatibility=legacy_compatibility,
        )
        acceptance_probe_ids_by_case = {
            str(result.get("case_id") or ""): str(
                result.get("probe_record_id") or result.get("probe_id") or ""
            )
            for result in capability_acceptance.get("results") or []
            if isinstance(result, dict)
            and str(result.get("case_id") or "").strip()
            and str(result.get("probe_record_id") or result.get("probe_id") or "").strip()
        }
        mark_step(
            runtime,
            loop,
            step_name="capability_acceptance",
            status="completed" if capability_acceptance.get("ok") else "blocked",
            record_ids=list(capability_acceptance.get("probe_record_ids") or [])
            + list(capability_acceptance.get("trace_record_ids") or []),
            metrics={
                "case_count": capability_acceptance.get("case_count", 0),
                "pass_count": capability_acceptance.get("pass_count", 0),
                "failed_count": capability_acceptance.get("failed_count", 0),
            },
        )
        accepted_capability_ids = _accepted_capability_ids(
            capability_acceptance.get("results") or [],
        )
        replay_selection_capability_ids = (
            loop_capabilities if legacy_compatibility else accepted_capability_ids
        )
        replay_capabilities = _evidence_bound_capabilities(
            replay_selection_capability_ids,
            capability_acceptance.get("results") or [],
            legacy_compatibility=legacy_compatibility,
        )
        capability_replay = build_capability_replay_packs(
            runtime,
            scope=scope_ref,
            capabilities=replay_capabilities,
            persist=True,
            loop_id=loop_id,
            acceptance_execution_id=str(capability_acceptance.get("execution_id") or ""),
            acceptance_probe_ids_by_case=acceptance_probe_ids_by_case,
            profile_key=profile_key,
            capability_scope=capability_scope,
            runtime_scope=runtime_scope if runtime_scope is not None else scope_ref,
            at_time=at_time,
            catalog=effective_catalog,
            legacy_compatibility=legacy_compatibility,
        )
        mark_step(
            runtime,
            loop,
            step_name="capability_replay",
            status=(
                "skipped"
                if not replay_capabilities
                else "blocked"
                if not capability_acceptance.get("ok")
                or any(pack.get("not_run_case_count") for pack in capability_replay.get("packs") or [])
                else "completed"
            ),
            record_ids=list(capability_replay.get("persisted_replay_ids") or []) + list(capability_replay.get("score_record_ids") or []),
            metrics={
                "pack_count": capability_replay.get("pack_count", 0),
                "case_count": capability_replay.get("case_count", 0),
                "eligible_capability_count": len(replay_capabilities),
                "executed_case_count": sum(
                    int(pack.get("executed_case_count") or 0)
                    for pack in capability_replay.get("packs") or []
                ),
                "not_run_case_count": sum(
                    int(pack.get("not_run_case_count") or 0)
                    for pack in capability_replay.get("packs") or []
                ),
            },
        )
        safety_replay = run_safety_boundary_replay(runtime, scope=scope_ref, persist=True, loop_id=loop_id)
        safety_gate_passed = bool(safety_replay.get("ok"))
        mark_step(
            runtime,
            loop,
            step_name="safety_replay",
            status="completed" if safety_replay.get("ok") else "blocked",
            record_ids=list(safety_replay.get("replay_record_ids") or []) + [str(safety_replay.get("score_record_id") or "")],
            metrics={
                "case_count": safety_replay.get("case_count", 0),
                "pass_rate": safety_replay.get("pass_rate", 0.0),
                "safety_gate_passed": safety_gate_passed,
            },
        )

        goal_portfolio = _candidate_goal_portfolio(
            goals or [selected_goal],
            max_goals=max_goals,
            allowed_capability_ids=accepted_capability_ids,
            legacy_compatibility=legacy_compatibility,
        )
        max_candidates_per_goal = max(1, min(3, max_goals)) if len(goal_portfolio) <= 1 else 1
        candidate_specs = _candidate_specs_for_goals(
            goal_portfolio,
            max_goals=max_goals,
            max_candidates_per_goal=max_candidates_per_goal,
            replay_dataset=replay_dataset,
            evidence=evidence,
            runtime=runtime,
            scope=scope_ref,
            allowed_capability_ids=accepted_capability_ids,
            legacy_compatibility=legacy_compatibility,
        )
        candidate_kinds = [str(spec.get("promotion_target") or "") for spec in candidate_specs]
        candidate_selection_reason = ""
        if not candidate_specs:
            candidate_selection_reason = (
                "implicit_capability_fallback_unclassified"
                if any(_implicit_capability_fallback(goal) for goal in goals or [selected_goal])
                else (
                    str(capability_selection.get("reason") or "")
                    or "no_accepted_capability_goal_with_declared_artifact"
                )
            )
        network_research["output_gate"] = _network_output_gate(
            runtime,
            enabled=network_enabled,
            scope=scope_ref,
            loop_id=loop_id,
            goal=selected_goal,
            evidence=evidence,
            candidate_kinds=candidate_kinds,
            research_note_id=research_note_id,
            replay_dataset=replay_dataset,
            persist=True,
        )
        experiment_ids: list[str] = []
        eval_results: list[dict[str, Any]] = []
        candidate_ids: list[str] = []
        promotion_reports: list[dict[str, Any]] = []
        regression_reports: list[dict[str, Any]] = []
        evaluator_packet_ids: list[str] = []
        evaluator_verdict_ids: list[str] = []
        stop_judgment_ids: list[str] = []
        evaluator_verdicts: list[dict[str, Any]] = []
        stop_judgments: list[dict[str, Any]] = []
        isolation_blocked_reasons: list[str] = []
        hypothesis_feedback_ids: list[str] = []
        hypothesis_blocked_reasons: list[str] = []
        isolation_gate_passed = True
        hypothesis_gate_passed = True
        promotion_budget = max(0, _as_int(max_promotions, default=0)) if max_promotions is not None else len(candidate_kinds)
        for spec in candidate_specs:
            candidate_kind = str(spec.get("promotion_target") or "")
            goal_for_candidate = dict(spec.get("goal") or selected_goal)
            goal_id_for_candidate = _goal_id_for_goal(goal_for_candidate, goals, goal_ids)
            target_capability = _explicit_goal_capability(spec) or _explicit_goal_capability(goal_for_candidate)
            if not target_capability:
                # Candidate construction is a capability-changing path.  A
                # goal without a durable target may be observed/researched,
                # but it cannot become a candidate by falling back to a
                # familiar capability name.
                isolation_gate_passed = False
                isolation_blocked_reasons.append("target_capability_unclassified")
                continue
            candidate_patch = dict(
                spec.get("patch")
                or _candidate_patch(
                    goal_for_candidate,
                    evidence,
                    candidate_kind=candidate_kind,
                    replay_dataset=replay_dataset,
                    legacy_compatibility=legacy_compatibility,
                )
            )
            raw_hypothesis_context = (
                dict(spec.get("capability_hypothesis") or {})
                if isinstance(spec.get("capability_hypothesis"), dict)
                else (
                    dict(goal_for_candidate.get("capability_hypothesis") or {})
                    if isinstance(goal_for_candidate.get("capability_hypothesis"), dict)
                    else dict(goal_for_candidate)
                )
            )
            hypothesis_reference = explicit_hypothesis_reference(raw_hypothesis_context)
            nonlegacy_code_patch = candidate_kind == "code_patch" and not legacy_compatibility
            hypothesis_gate = (
                _nonlegacy_code_patch_hypothesis_gate(
                    runtime,
                    scope=scope_ref,
                    hypothesis_reference=hypothesis_reference,
                    raw_hypothesis_context=raw_hypothesis_context,
                    candidate_patch=candidate_patch,
                    target_capability=target_capability,
                )
                if nonlegacy_code_patch
                else _hypothesis_gate_for_candidate(
                    runtime,
                    scope=scope_ref,
                    hypothesis_reference=hypothesis_reference,
                )
            )
            requires_hypothesis_gate = bool(hypothesis_reference) or nonlegacy_code_patch
            if hypothesis_reference:
                hypothesis_context = _hypothesis_trace_context(hypothesis_reference, hypothesis_gate)
                candidate_patch["capability_hypothesis"] = hypothesis_context
                if hypothesis_context.get("expected_metric"):
                    candidate_patch["expected_metric"] = dict(hypothesis_context["expected_metric"])
                if hypothesis_context.get("candidate_bounds"):
                    candidate_patch["candidate_bounds"] = dict(hypothesis_context["candidate_bounds"])
            if requires_hypothesis_gate:
                candidate_patch["capability_hypothesis_gate"] = hypothesis_gate
                hypothesis_gate_passed = bool(hypothesis_gate_passed and hypothesis_gate.get("allowed"))
                if not hypothesis_gate.get("allowed"):
                    hypothesis_blocked_reasons.append(str(hypothesis_gate.get("reason") or "hypothesis_behavior_gate_blocked"))
            experiment_id = create_sandbox_experiment(
                runtime,
                scope=scope_ref,
                loop_id=loop_id,
                learning_goal_id=goal_id_for_candidate or str(goal_for_candidate.get("semantic_key") or ""),
                research_note_id=research_note_id,
                candidate_kind=candidate_kind,
                candidate_patch=candidate_patch,
                expected_gain=str(goal_for_candidate.get("success_criteria") or ""),
            )
            experiment_ids.append(experiment_id)
            experiment = runtime.store.get_by_id(experiment_id, scope=scope_ref)
            measured_eval_suite = _measured_learning_eval_suite(
                evidence=evidence,
                replay_gate=replay_gate,
                safety_replay=safety_replay,
                isolation_gate_passed=None,
            )
            if requires_hypothesis_gate:
                measured_eval_suite["gates"] = [
                    *list(measured_eval_suite.get("gates") or []),
                    {
                        "name": "capability_hypothesis",
                        "outcome": "pass" if hypothesis_gate.get("allowed") else "blocked",
                        "reason": "" if hypothesis_gate.get("allowed") else str(hypothesis_gate.get("reason") or "hypothesis_behavior_gate_blocked"),
                    },
                ]
                if not hypothesis_gate.get("allowed"):
                    measured_eval_suite["blocked_reasons"] = [
                        *[str(item) for item in measured_eval_suite.get("blocked_reasons") or [] if str(item)],
                        str(hypothesis_gate.get("reason") or "hypothesis_behavior_gate_blocked"),
                    ]
            if candidate_kind == "code_patch" and str(candidate_patch.get("proposal_status") or "") != "proposal_ready":
                blocked_reason = str(candidate_patch.get("proposal_blocked_reason") or "invalid_code_proposal")
                measured_eval_suite["blocked_reasons"] = [
                    *[str(item) for item in measured_eval_suite.get("blocked_reasons") or [] if str(item)],
                    blocked_reason,
                ]
            eval_result = run_learning_eval(
                runtime,
                experiment,
                scope=scope_ref,
                loop_id=loop_id,
                eval_suite=measured_eval_suite,
            )
            if hypothesis_reference:
                eval_result["capability_hypothesis"] = _hypothesis_trace_context(hypothesis_reference, hypothesis_gate)
                eval_result["capability_hypothesis_gate"] = hypothesis_gate
                feedback_id, feedback_error = _record_non_authorizing_hypothesis_eval_feedback(
                    runtime,
                    scope=scope_ref,
                    hypothesis_reference=hypothesis_reference,
                    eval_result=eval_result,
                    loop_id=loop_id,
                )
                if feedback_id:
                    hypothesis_feedback_ids.append(feedback_id)
                if feedback_error:
                    hypothesis_gate["allowed"] = False
                    hypothesis_gate["reason"] = feedback_error
                    hypothesis_gate_passed = False
                    hypothesis_blocked_reasons.append(feedback_error)
            eval_result["gate_bundle"] = _gate_bundle_for_candidate(
                candidate_kind,
                evidence=evidence,
                scope=scope_ref,
                prompt_text=_candidate_prompt_text(candidate_patch),
                prompt_safety_executor=getattr(runtime, "prompt_safety_executor", None),
                release=current_release_identity(runtime, scope_ref),
                real_task_replay=real_task_replay,
                replay_gate=replay_gate,
            )
            evaluator_packet = build_evaluation_packet(
                runtime,
                scope=scope_ref,
                loop_id=loop_id,
                goal=goal_for_candidate,
                candidate_kind=candidate_kind,
                artifact=candidate_patch,
                generator_claim=_candidate_summary(goal_for_candidate, candidate_kind=candidate_kind, patch=candidate_patch),
                replay_gate=replay_gate,
                real_task_replay=real_task_replay,
            )
            evaluator_verdict = run_isolated_evaluator(runtime, evaluator_packet, scope=scope_ref, loop_id=loop_id)
            stop_judgment = judge_stop_condition(runtime, evaluator_verdict, scope=scope_ref, loop_id=loop_id)
            evaluator_packet_ids.append(evaluator_packet.record_id)
            evaluator_verdict_ids.append(evaluator_verdict.record_id)
            stop_judgment_ids.append(stop_judgment.record_id)
            evaluator_verdict_content = dict(evaluator_verdict.content or {})
            stop_judgment_content = dict(stop_judgment.content or {})
            evaluator_verdicts.append(evaluator_verdict_content)
            stop_judgments.append(stop_judgment_content)
            candidate_isolation_passed = bool(evaluator_verdict_content.get("promotion_allowed")) and bool(stop_judgment_content.get("promotion_allowed"))
            if not candidate_isolation_passed:
                isolation_gate_passed = False
                isolation_blocked_reasons.extend(str(item) for item in evaluator_verdict_content.get("blocked_reasons") or [])
            eval_result["isolated_evaluator"] = {
                "packet_id": evaluator_packet.record_id,
                "verdict_id": evaluator_verdict.record_id,
                "stop_judgment_id": stop_judgment.record_id,
                "verdict": evaluator_verdict_content,
                "stop_judgment": stop_judgment_content,
            }
            eval_results.append(eval_result)
            if (
                eval_result.get("ok")
                and replay_gate_passed
                and candidate_isolation_passed
                and safety_gate_passed
                and (not requires_hypothesis_gate or bool(hypothesis_gate.get("allowed")))
            ):
                candidate_id = distill_capability_candidate(
                    runtime,
                    scope=scope_ref,
                    loop_id=loop_id,
                    experiment_id=experiment_id,
                    eval_result=eval_result,
                    promotion_target=candidate_kind,
                    summary=_candidate_summary(goal_for_candidate, candidate_kind=candidate_kind, patch=candidate_patch),
                    target_capability=target_capability,
                    candidate_patch=candidate_patch,
                )
                candidate_ids.append(candidate_id)
                promotion_report = promote_candidate(
                    runtime,
                    candidate_id=candidate_id,
                    scope=scope_ref,
                    loop_id=loop_id,
                    apply=bool(apply and not dry_run and sum(1 for report in promotion_reports if report.get("applied")) < promotion_budget),
                    eval_result=eval_result,
                    health={"ok": True, "source": "offline_learning_cycle"},
                    # Compatibility can select retired read/evaluation facts;
                    # it cannot mint v1 repository-mutation authority.
                    legacy_authority=None,
                )
                promotion_reports.append(promotion_report)
                regression_reports.append(
                    run_regression_watch(
                        runtime,
                        candidate_id=candidate_id,
                        scope=scope_ref,
                        loop_id=loop_id,
                        eval_result=eval_result,
                    )
                )
        experiment_id = experiment_ids[0] if experiment_ids else ""
        eval_result = eval_results[0] if eval_results else {"ok": False, "verdict": ""}
        candidate_id = candidate_ids[0] if candidate_ids else ""
        promotion_report = promotion_reports[0] if promotion_reports else {"ok": True, "applied": False}
        regression_report = regression_reports[0] if regression_reports else {"ok": True, "regressed": False, "record_id": ""}
        mark_step(runtime, loop, step_name="experiment", status="completed", record_ids=experiment_ids, metrics={"experiment_count": len(experiment_ids), "candidate_kind_count": len(candidate_kinds)})
        mark_step(
            runtime,
            loop,
            step_name="eval",
            status="completed",
            record_ids=[str(item.get("record_id") or "") for item in eval_results] + hypothesis_feedback_ids,
            metrics={
                "pass_count": sum(1 for item in eval_results if item.get("ok")),
                "eval_count": len(eval_results),
                "hypothesis_feedback_count": len(hypothesis_feedback_ids),
            },
        )
        mark_step(
            runtime,
            loop,
            step_name="isolated_evaluator",
            status="completed" if isolation_gate_passed else "blocked",
            record_ids=evaluator_packet_ids + evaluator_verdict_ids + stop_judgment_ids,
            metrics={
                "packet_count": len(evaluator_packet_ids),
                "verdict_count": len(evaluator_verdict_ids),
                "stop_judgment_count": len(stop_judgment_ids),
                "isolation_gate_passed": bool(isolation_gate_passed),
                "blocked_reason_count": len(set(isolation_blocked_reasons)),
            },
        )
        mark_step(
            runtime,
            loop,
            step_name="promotion",
            status=(
                "skipped"
                if not candidate_specs
                else "completed"
                if replay_gate_passed and isolation_gate_passed and safety_gate_passed and hypothesis_gate_passed
                else "blocked"
            ),
            record_ids=[item for report in promotion_reports for item in [report.get("promotion_request_id", "")] if item] + candidate_ids,
            metrics={
                "applied_count": sum(1 for report in promotion_reports if report.get("applied")),
                "replay_gate_passed": replay_gate_passed,
                "replay_gate_reason": str(replay_gate.get("reason") or ""),
                "safety_gate_passed": safety_gate_passed,
                "safety_gate_reason": str(safety_replay.get("blocked_reason") or safety_replay.get("reason") or ""),
                "isolation_gate_passed": bool(isolation_gate_passed),
                "isolation_blocked_reasons": sorted(set(isolation_blocked_reasons)),
                "hypothesis_gate_passed": bool(hypothesis_gate_passed),
                "hypothesis_blocked_reasons": sorted(set(hypothesis_blocked_reasons)),
                "candidate_selection_reason": candidate_selection_reason,
            },
        )

        measured_score = _measured_capability_score(
            eval_result=eval_result,
            replay_gate=replay_gate,
            safety_replay=safety_replay,
            isolation_gate_passed=isolation_gate_passed,
            hypothesis_gate_passed=hypothesis_gate_passed,
        )
        score_id = ""
        scored_goal = (
            dict(candidate_specs[0].get("goal") or {})
            if candidate_specs and isinstance(candidate_specs[0].get("goal"), dict)
            else selected_goal
        )
        score_capability = _explicit_goal_capability(scored_goal)
        score_capability_authorized = bool(
            legacy_compatibility or score_capability in set(accepted_capability_ids)
        )
        if measured_score is not None and score_capability and score_capability_authorized:
            score_id = record_capability_score(
                runtime,
                scope=scope_ref,
                loop_id=loop_id,
                capability=score_capability,
                score=measured_score,
                evidence_record_ids=[item for item in [research_note_id, eval_result.get("record_id", ""), candidate_id] if item],
                meta={
                    "kind": "autonomous_learning_measured",
                    "measurement_source": "autonomous_learning_gates",
                    "eval_verdict": str(eval_result.get("verdict") or ""),
                    "replay_gate_passed": bool(replay_gate_passed),
                    "safety_gate_passed": bool(safety_gate_passed),
                    "isolation_gate_passed": bool(isolation_gate_passed),
                    "hypothesis_gate_passed": bool(hypothesis_gate_passed),
                },
            )
        skill_sedimentation = promote_repeated_sops_to_skill_candidates(
            runtime,
            scope=scope_ref,
            min_repeats=3,
            persist=True,
        )
        mark_step(
            runtime,
            loop,
            step_name="skill_sedimentation",
            status="completed",
            record_ids=list(skill_sedimentation.get("candidate_ids") or []) + list(skill_sedimentation.get("registry_record_ids") or []),
            metrics={
                "skill_candidate_count": skill_sedimentation.get("skill_candidate_count", 0),
                "sop_group_count": skill_sedimentation.get("sop_group_count", 0),
            },
        )
        retention_report = compact_learning_records(runtime, scope=scope_ref, loop_id=loop_id, dry_run=not bool(apply), max_records=1000)
        # This cycle already records a measured score only after all gates pass.
        # Keep the report read-only here so synthetic replay outcomes generated
        # by a failed cycle cannot supersede the last verified capability score.
        ledger = build_capability_ledger(
            runtime,
            scope=scope_ref,
            attribute_outcomes=False,
            legacy_compatibility=legacy_compatibility,
        )
        capability_dashboard = build_capability_dashboard_metrics(runtime, scope=scope_ref, persist=True, loop_id=loop_id)
        mark_step(
            runtime,
            loop,
            step_name="capability_dashboard",
            status="completed",
            record_ids=[str(capability_dashboard.get("persisted_record_id") or "")],
            metrics=dict(capability_dashboard.get("metrics") or {}),
        )
        mark_step(
            runtime,
            loop,
            step_name="ledger",
            status="completed",
            record_ids=list(preexisting_outcome_attribution.get("record_ids") or [])
            + [item for item in [score_id, regression_report.get("record_id", "")] if item],
            metrics={
                "preexisting_outcome_attribution_count": preexisting_outcome_attribution.get("record_count", 0),
                "preexisting_outcome_attribution_status": str(
                    preexisting_outcome_attribution.get("status")
                    or ("completed" if preexisting_outcome_attribution.get("ok") is True else "failed")
                ),
                "preexisting_outcome_attribution_reason": str(
                    preexisting_outcome_attribution.get("reason") or ""
                ),
                "retention_disabled_count": retention_report.get("disabled_count", 0),
                "regressed": regression_report.get("regressed", False),
            },
        )
        complete_learning_loop(runtime, loop, status="completed", summary=f"Autonomous learning cycle completed; candidate={candidate_id or 'none'}")

        result = {
            "ok": True,
            "loop_id": loop_id,
            "loop_record_id": loop.record_id,
            "scope": asdict(scope_ref),
            "dry_run": bool(dry_run),
            "apply": bool(apply),
            "legacy_compatibility": bool(legacy_compatibility),
            "capability_selection": capability_selection,
            "active_capability_ids": selected_capability_ids,
            "accepted_capability_ids": accepted_capability_ids,
            "replay_capability_ids": replay_capabilities,
            "code_apply_recovery": code_apply_recovery,
            "watch_signal_count": _as_int(watch_report.get("signal_count"), default=0),
            "thought_count": _as_int(thought_report.get("thought_count"), default=0),
            "goal_count": len(goals),
            "selected_goal_id": selected_goal_id,
            "selected_goal": selected_goal,
            "research_task_ids": research_task_ids,
            "research_note_id": research_note_id,
            "network_research": network_research,
            "experiment_id": experiment_id,
            "experiment_ids": experiment_ids,
            "eval_record_id": str(eval_result.get("record_id") or ""),
            "eval_record_ids": [str(item.get("record_id") or "") for item in eval_results],
            "eval_verdict": str(eval_result.get("verdict") or ""),
            "candidate_id": candidate_id,
            "candidate_ids": candidate_ids,
            "candidate_kinds": candidate_kinds,
            "candidate_selection_reason": candidate_selection_reason,
            "candidate_specs": [
                {
                    "target_capability": str(spec.get("target_capability") or ""),
                    "requested_target": str(spec.get("requested_target") or ""),
                    "promotion_target": str(spec.get("promotion_target") or ""),
                    "capability_hypothesis": explicit_hypothesis_reference(
                        spec.get("capability_hypothesis") if isinstance(spec.get("capability_hypothesis"), dict) else {}
                    ),
                    "fallback_reason": str((spec.get("patch") or {}).get("fallback_reason") or ""),
                    "proposal_status": str((spec.get("patch") or {}).get("proposal_status") or ""),
                    "proposal_blocked_reason": str((spec.get("patch") or {}).get("proposal_blocked_reason") or ""),
                }
                for spec in candidate_specs
            ],
            "real_task_replay": real_task_replay,
            "replay_gate": replay_gate,
            "replay_gate_passed": replay_gate_passed,
            "safety_gate_passed": safety_gate_passed,
            "isolation_gate_passed": bool(isolation_gate_passed),
            "hypothesis_gate_passed": bool(hypothesis_gate_passed),
            "hypothesis_feedback_ids": hypothesis_feedback_ids,
            "hypothesis_blocked_reasons": sorted(set(hypothesis_blocked_reasons)),
            "evaluator_packet_ids": evaluator_packet_ids,
            "evaluator_verdict_ids": evaluator_verdict_ids,
            "stop_judgment_ids": stop_judgment_ids,
            "isolated_evaluator": {
                "packet_ids": evaluator_packet_ids,
                "verdict_ids": evaluator_verdict_ids,
                "stop_judgment_ids": stop_judgment_ids,
                "verdicts": evaluator_verdicts,
                "stop_judgments": stop_judgments,
                "blocked_reasons": sorted(set(isolation_blocked_reasons)),
                "debt_metrics": _aggregate_isolation_debt(evaluator_verdicts),
            },
            "goal_graph": goal_graph,
            "capability_replay": capability_replay,
            "safety_replay": safety_replay,
            "promotion": promotion_report,
            "promotions": promotion_reports,
            "regression_watch": regression_report,
            "regression_watches": regression_reports,
            "replay_dataset": replay_dataset,
            "capability_score_id": score_id,
            "capability_score_blocked_reason": (
                ""
                if not (measured_score is not None and not score_id)
                else "target_capability_unclassified_or_not_accepted"
            ),
            "preexisting_outcome_attribution": preexisting_outcome_attribution,
            "skill_sedimentation": skill_sedimentation,
            "capability_dashboard": capability_dashboard,
            "ledger": ledger,
            "retention": retention_report,
        }
        result.update(classify_autonomous_learning_activity(result))
        return result
    except Exception as exc:
        mark_step(runtime, loop, step_name="failed", status="failed", error=str(exc))
        complete_learning_loop(runtime, loop, status="failed", summary=str(exc))
        raise


def _recover_code_apply_before_cycle(
    runtime: Any,
    *,
    scope: ScopeRef,
    apply: bool,
) -> dict[str, Any]:
    """Resolve durable interrupted writes before beginning another auto-apply."""
    if not apply:
        return {
            "ok": True,
            "skipped": True,
            "reason": "apply_disabled",
            "transaction_count": 0,
            "recovered_count": 0,
            "recovery_quarantined_count": 0,
            "retried_apply_count": 0,
            "transactions": [],
        }
    try:
        return recover_incomplete_code_apply(runtime, scope=scope)
    except Exception as exc:
        return {
            "ok": False,
            "skipped": False,
            "reason": "code_apply_recovery_failed",
            "error_type": type(exc).__name__,
            "transaction_count": 0,
            "recovered_count": 0,
            "recovery_quarantined_count": 0,
            "retried_apply_count": 0,
            "transactions": [],
        }


def _run_real_task_replay_if_available(
    runtime: Any,
    loop: Any,
    scope: ScopeRef,
    replay_dataset: dict[str, Any],
    seed_records: list[dict[str, Any]] | None = None,
    legacy_compatibility: bool = False,
) -> dict[str, Any]:
    cases = list((replay_dataset or {}).get("cases") or [])
    if not cases:
        skipped_report = {
            "ok": False,
            "replay_skipped_reason": "no_replay_cases",
            "case_count": 0,
        }
        mark_step(runtime, loop, step_name="real_task_replay", status="completed", metrics=skipped_report)
        return skipped_report

    replay_runner = getattr(runtime, "run_real_task_replay", None)
    if not callable(replay_runner):
        skipped_report = {
            "ok": False,
            "replay_skipped_reason": "run_real_task_replay_unavailable",
            "case_count": len(cases),
        }
        mark_step(runtime, loop, step_name="real_task_replay", status="skipped", metrics=skipped_report)
        return skipped_report

    payload = {
        "name": "autonomous_learning_real_task_replay",
        "scope": asdict(scope),
        "threshold": 0.6,
        "seed": list(seed_records or []),
        "cases": [dict(case) for case in cases],
    }
    try:
        replay_kwargs: dict[str, Any] = {"seed": True, "persist_report": True}
        if legacy_compatibility:
            replay_kwargs["legacy_compatibility"] = True
        report = replay_runner(payload, **replay_kwargs)
        if not isinstance(report, dict):
            report = {
                "ok": False,
                "replay_skipped_reason": "invalid_real_task_replay_report",
                "case_count": len(cases),
                "report_type": "real_task_replay",
            }
        mark_step(
            runtime,
            loop,
            step_name="real_task_replay",
            status="completed",
            metrics={
                "case_count": len(cases),
                "seed_count": len(seed_records or []),
                "ok": bool(report.get("ok", False)),
                "report_type": str(report.get("report_type") or "real_task_replay"),
            },
        )
        return report
    except Exception as exc:
        failed_report = {
            "ok": False,
            "replay_skipped_reason": "real_task_replay_failed",
            "error_type": type(exc).__name__,
            "error": str(exc),
            "case_count": len(cases),
        }
        mark_step(
            runtime,
            loop,
            step_name="real_task_replay",
            status="skipped",
            metrics={"case_count": len(cases), "error_type": type(exc).__name__},
        )
        return failed_report


def _replay_gate_passed(report: dict[str, Any]) -> bool:
    return bool(_replay_gate_report(report).get("ok"))


def _replay_gate_report(report: dict[str, Any]) -> dict[str, Any]:
    status_passed = _status_passed(report)
    if not status_passed:
        return {
            "ok": False,
            "reason": str(report.get("replay_skipped_reason") or "real_task_replay_not_ok"),
            "real_task_replay": report,
        }
    verdict = str(report.get("verdict") or "pass").strip().lower()
    sample_count = _as_int(_first_present(report, "sample_count", "case_count", "pass_count"), default=0)
    fail_count = _as_int(_first_present(report, "fail_count", "failed_count", "failures"), default=0)
    pass_rate = _as_float(report.get("pass_rate"), default=0.0)
    threshold = _as_float(report.get("threshold"), default=0.6)
    ok = status_passed and sample_count > 0 and fail_count == 0 and pass_rate >= threshold
    reason = "passed" if ok else "real_task_replay_threshold_failed"
    if not status_passed:
        reason = "real_task_replay_verdict_not_pass"
    elif sample_count <= 0:
        reason = "real_task_replay_no_samples"
    elif fail_count > 0:
        reason = "real_task_replay_failures_present"
    elif pass_rate < threshold:
        reason = "real_task_replay_pass_rate_below_threshold"
    return {
        "ok": ok,
        "reason": reason,
        "verdict": verdict,
        "sample_count": sample_count,
        "fail_count": fail_count,
        "pass_rate": pass_rate,
        "threshold": threshold,
        "real_task_replay": report,
    }


def _measured_learning_eval_suite(
    *,
    evidence: list[dict[str, Any]],
    replay_gate: dict[str, Any],
    safety_replay: dict[str, Any],
    isolation_gate_passed: bool | None,
) -> dict[str, Any]:
    replay_ok = bool(replay_gate.get("ok"))
    safety_ok = bool(safety_replay.get("ok"))
    replay_reason = str(replay_gate.get("reason") or "")
    safety_reason = str(safety_replay.get("blocked_reason") or safety_replay.get("reason") or "")
    replay_pass_rate = _as_float(replay_gate.get("pass_rate"), default=0.0) if replay_ok else 0.0
    safety_pass_rate = _as_float(safety_replay.get("pass_rate"), default=0.0) if safety_ok else 0.0
    blocked_reasons: list[str] = []
    if not replay_ok:
        blocked_reasons.append(replay_reason or "real_task_replay_not_passed")
    if not safety_ok:
        blocked_reasons.append(safety_reason or "safety_replay_not_passed")
    if isolation_gate_passed is False:
        blocked_reasons.append("isolated_evaluator_not_passed")
    gates = [
        {"name": "real_task_replay", "outcome": "pass" if replay_ok else "blocked", "reason": replay_reason},
        {"name": "safety_replay", "outcome": "pass" if safety_ok else "blocked", "reason": safety_reason},
    ]
    if isolation_gate_passed is not None:
        gates.append(
            {
                "name": "isolated_evaluator",
                "outcome": "pass" if isolation_gate_passed else "blocked",
                "reason": "" if isolation_gate_passed else "isolated_evaluator_not_passed",
            }
        )
    return {
        "measurement_source": "autonomous_learning_gates",
        "requires_measured_gates": True,
        "scores": {
            "capability": replay_pass_rate,
            "safety": safety_pass_rate,
            "regression": replay_pass_rate,
            "evidence": _evidence_score(evidence),
            "cost": 0.0 if blocked_reasons else 1.0,
            "maintainability": min(replay_pass_rate, safety_pass_rate) if blocked_reasons else 0.75,
            "confidence": min(replay_pass_rate, safety_pass_rate) if blocked_reasons else 0.7,
        },
        "gates": gates,
        "blocked_reasons": blocked_reasons,
    }


def _measured_capability_score(
    *,
    eval_result: dict[str, Any],
    replay_gate: dict[str, Any],
    safety_replay: dict[str, Any],
    isolation_gate_passed: bool,
    hypothesis_gate_passed: bool = True,
) -> float | None:
    if not (
        eval_result.get("ok")
        and replay_gate.get("ok")
        and safety_replay.get("ok")
        and isolation_gate_passed
        and hypothesis_gate_passed
    ):
        return None
    scores = eval_result.get("scores") if isinstance(eval_result.get("scores"), dict) else {}
    measured = [
        _as_float(scores.get("capability"), default=0.0),
        _as_float(scores.get("safety"), default=0.0),
        _as_float(scores.get("regression"), default=0.0),
        _as_float(scores.get("evidence"), default=0.0),
    ]
    return round(sum(measured) / len(measured), 3) if measured else 0.0


def _hypothesis_gate_for_candidate(
    runtime: Any,
    *,
    scope: ScopeRef,
    hypothesis_reference: dict[str, str],
) -> dict[str, Any]:
    """Apply the WP8 gate only when a goal supplied an explicit hypothesis."""

    if not hypothesis_reference:
        return {"required": False, "allowed": True, "reason": "not_applicable"}
    hypothesis_id = str(hypothesis_reference.get("capability_hypothesis_id") or "").strip()
    if not hypothesis_id:
        return {
            "required": True,
            "allowed": False,
            "reason": "capability_hypothesis_id_missing",
        }
    try:
        result = dict(
            hypothesis_behavior_gate(
                runtime,
                runtime_scope=scope,
                hypothesis_id=hypothesis_id,
            )
        )
    except CapabilityHypothesisError as exc:
        result = {"allowed": False, "reason": str(exc), "hypothesis_id": hypothesis_id}
    # A planner may carry optional link/revision/scope context, but it cannot
    # substitute that context for the durable hypothesis identity.  Any
    # disagreement closes the candidate path and leaves the authoritative
    # hypothesis trace in the result for audit.
    for reference_key, gate_key in (
        ("link_id", "link_id"),
        ("link_digest", "link_digest"),
        ("capability_id", "capability_id"),
        ("capability_revision_id", "capability_revision_id"),
        ("capability_scope", "capability_scope"),
    ):
        supplied = str(hypothesis_reference.get(reference_key) or "").strip()
        resolved = str(result.get(gate_key) or "").strip()
        if supplied and resolved and supplied != resolved:
            result["allowed"] = False
            result["reason"] = f"capability_hypothesis_{reference_key}_mismatch"
            break
    result["required"] = True
    result["hypothesis_id"] = hypothesis_id
    return result


def _nonlegacy_code_patch_hypothesis_gate(
    runtime: Any,
    *,
    scope: ScopeRef,
    hypothesis_reference: dict[str, str],
    raw_hypothesis_context: dict[str, Any],
    candidate_patch: dict[str, Any],
    target_capability: str,
) -> dict[str, Any]:
    """Require an exact durable v3 hypothesis before generic code mutation.

    Code proposals can still be evaluated with incomplete context, but the
    default dynamic path must never distill or promote one.  A caller that
    needs historic generic behavior has to opt in through the explicit
    ``legacy_compatibility`` path at the cycle boundary.
    """

    required_reference_fields = (
        "capability_hypothesis_id",
        "link_id",
        "link_digest",
        "capability_id",
        "capability_revision_id",
        "capability_scope",
    )
    missing_reference = [field for field in required_reference_fields if not str(hypothesis_reference.get(field) or "").strip()]
    if missing_reference:
        return {
            "required": True,
            "allowed": False,
            "reason": "nonlegacy_code_patch_hypothesis_context_incomplete",
            "missing_fields": missing_reference,
        }
    if str(hypothesis_reference.get("capability_id") or "") != str(target_capability or ""):
        return {
            "required": True,
            "allowed": False,
            "reason": "nonlegacy_code_patch_hypothesis_target_mismatch",
        }
    required_patch_fields = (
        "provider_binding_id",
        "capability_revision_id",
        "capability_scope",
    )
    missing_patch = [field for field in required_patch_fields if not str(candidate_patch.get(field) or "").strip()]
    if missing_patch:
        return {
            "required": True,
            "allowed": False,
            "reason": "nonlegacy_code_patch_binding_context_incomplete",
            "missing_fields": missing_patch,
        }
    context_binding_id = str(raw_hypothesis_context.get("provider_binding_id") or "").strip()
    if not context_binding_id:
        return {
            "required": True,
            "allowed": False,
            "reason": "nonlegacy_code_patch_hypothesis_binding_missing",
        }
    if context_binding_id != str(candidate_patch.get("provider_binding_id") or ""):
        return {
            "required": True,
            "allowed": False,
            "reason": "nonlegacy_code_patch_provider_binding_id_mismatch",
        }
    for patch_field, reference_field in (
        ("target_capability", "capability_id"),
        ("capability_revision_id", "capability_revision_id"),
        ("capability_scope", "capability_scope"),
    ):
        if str(candidate_patch.get(patch_field) or "") != str(hypothesis_reference.get(reference_field) or ""):
            return {
                "required": True,
                "allowed": False,
                "reason": f"nonlegacy_code_patch_{patch_field}_mismatch",
            }
    gate = _hypothesis_gate_for_candidate(
        runtime,
        scope=scope,
        hypothesis_reference=hypothesis_reference,
    )
    if not gate.get("allowed"):
        return gate
    for field in required_reference_fields[1:]:
        gate_field = "hypothesis_id" if field == "capability_hypothesis_id" else field
        expected = str(hypothesis_reference.get(field) or "")
        actual = str(gate.get(gate_field) or "")
        if actual != expected:
            return {
                **gate,
                "required": True,
                "allowed": False,
                "reason": f"nonlegacy_code_patch_hypothesis_{field}_mismatch",
            }
    if not isinstance(gate.get("expected_metric"), dict) or not gate.get("expected_metric"):
        return {
            **gate,
            "required": True,
            "allowed": False,
            "reason": "nonlegacy_code_patch_hypothesis_metric_missing",
        }
    if not isinstance(gate.get("candidate_bounds"), dict) or not gate.get("candidate_bounds"):
        return {
            **gate,
            "required": True,
            "allowed": False,
            "reason": "nonlegacy_code_patch_hypothesis_bounds_missing",
        }
    gate["required"] = True
    return gate


def _hypothesis_trace_context(
    hypothesis_reference: dict[str, str],
    hypothesis_gate: dict[str, Any],
) -> dict[str, Any]:
    """Carry durable link/revision/bounds into an evaluated candidate trace."""

    result: dict[str, Any] = dict(hypothesis_reference)
    aliases = {
        "hypothesis_id": "capability_hypothesis_id",
        "link_id": "link_id",
        "link_digest": "link_digest",
        "capability_id": "capability_id",
        "capability_revision_id": "capability_revision_id",
        "capability_scope": "capability_scope",
    }
    for source, target in aliases.items():
        value = hypothesis_gate.get(source)
        if str(value or "").strip():
            result[target] = str(value)
    for field in ("expected_metric", "candidate_bounds"):
        value = hypothesis_gate.get(field)
        if isinstance(value, dict) and value:
            result[field] = dict(value)
    return result


def _record_non_authorizing_hypothesis_eval_feedback(
    runtime: Any,
    *,
    scope: ScopeRef,
    hypothesis_reference: dict[str, str],
    eval_result: dict[str, Any],
    loop_id: str,
) -> tuple[str, str]:
    """Keep same-loop evaluation traceable without letting it self-authorize."""

    hypothesis_id = str(hypothesis_reference.get("capability_hypothesis_id") or "").strip()
    eval_record_id = str(eval_result.get("record_id") or "").strip()
    if not hypothesis_id or not eval_record_id:
        return "", "hypothesis_evaluation_record_missing"
    try:
        feedback = record_hypothesis_experiment_feedback(
            runtime,
            runtime_scope=scope,
            hypothesis_id=hypothesis_id,
            artifact_type="evaluation",
            artifact_id=eval_record_id,
            verdict=str(eval_result.get("verdict") or "blocked"),
            # The loop generated this evaluation, so it is intentionally not
            # independent and cannot satisfy the behavior gate by itself.
            verifier={
                "id": "autonomous_learning",
                "revision": "learning_eval.v1",
                "independent": False,
            },
            details={
                "loop_id": loop_id,
                "eval_record_id": eval_record_id,
                "measurement_source": "autonomous_learning_gates",
            },
        )
        return feedback.record_id, ""
    except CapabilityHypothesisError as exc:
        return "", f"hypothesis_feedback_blocked:{str(exc)}"
    except Exception as exc:  # keep an evidence-write fault from opening a behavior path
        return "", f"hypothesis_feedback_failed_{type(exc).__name__}"


SUCCESS_LABELS = {"pass", "passed", "success", "succeeded", "ok", "true", "green"}
FAILURE_LABELS = {"fail", "failed", "failure", "error", "blocked", "reject", "rejected", "false", "red"}


def _status_passed(payload: dict[str, Any]) -> bool:
    if not payload:
        return False
    if payload.get("ok") is False or payload.get("success") is False:
        return False
    for key in ("verdict", "status", "result", "decision"):
        if key not in payload:
            continue
        label = str(payload.get(key) or "").strip().lower()
        if label in FAILURE_LABELS:
            return False
        if label in SUCCESS_LABELS:
            return True
    if payload.get("ok") is True or payload.get("success") is True:
        return True
    return False


def _aggregate_isolation_debt(verdicts: list[dict[str, Any]]) -> dict[str, int]:
    aggregate = {
        "verification_debt": 0,
        "unverified_generator_claims": 0,
        "comprehension_rot": 0,
        "cognitive_surrender": 0,
        "token_blowout": 0,
    }
    for verdict in verdicts:
        debt = dict(verdict.get("debt_metrics") or {})
        for key in aggregate:
            try:
                aggregate[key] += max(0, _as_int(debt.get(key), default=0))
            except (TypeError, ValueError):
                continue
    return aggregate


def _replay_seed_records_from_cases(cases: list[dict[str, Any]], *, scope: ScopeRef) -> list[dict[str, Any]]:
    seeds: list[dict[str, Any]] = []
    for index, case in enumerate(cases):
        if not isinstance(case, dict):
            continue
        query = str(case.get("query") or case.get("input") or "").strip()
        expected_text = [str(item).strip() for item in list(case.get("expected_text") or case.get("expect_any_text") or []) if str(item).strip()]
        correction = str(case.get("correction_from_user") or "").strip()
        expected = str(case.get("expected") or "").strip()
        body_parts = [part for part in [query, expected, correction, " ".join(expected_text)] if part]
        if not query or not body_parts:
            continue
        seeds.append(
            {
                "title": f"Replay learning seed {index + 1}",
                "text": "\n".join(body_parts),
                "memory_type": "learning.replay_seed",
                "scope": asdict(scope),
                "source": "eimemory.autonomous_learning.replay_seed",
                "tags": ["autonomous_learning", "real_task_replay"],
                "meta": {
                    "case_id": str(case.get("case_id") or index),
                    "target_capability": str(case.get("target_capability") or ""),
                    "task_type": str(case.get("task_type") or ""),
                },
            }
        )
    return seeds


def _legacy_self_model_capabilities(runtime: Any, *, scope: ScopeRef) -> list[dict[str, Any]]:
    """Render the historical cohort only for an explicit compatibility run."""

    ledger = build_capability_ledger(
        runtime,
        scope=scope,
        legacy_compatibility=True,
    )
    capabilities = ledger.get("capabilities") if isinstance(ledger.get("capabilities"), dict) else {}
    return [
        {
            "capability": str(capability_id),
            "kind": str(capability_id),
            "score": _as_float(
                details.get("score") if isinstance(details, dict) else 0.0,
                default=0.0,
            ),
        }
        for capability_id, details in capabilities.items()
        if str(capability_id).strip()
    ]


def _run_autonomous_learning_dry_run(
    runtime: Any,
    *,
    scope: ScopeRef,
    apply: bool,
    full: bool,
    max_goals: int,
    allow_network: bool = False,
    profile_key: str = "",
    capability_scope: str = "global",
    runtime_scope: ScopeRef | Mapping[str, Any] | None = None,
    at_time: str = "",
    legacy_compatibility: bool = False,
) -> dict[str, Any]:
    watch_report = collect_world_signals(
        runtime,
        scope=scope,
        watches=default_watches(),
        dry_run=True,
        loop_id="dry_run",
        profile_key=profile_key,
        capability_scope=capability_scope,
        at_time=at_time,
        legacy_compatibility=legacy_compatibility,
    )
    self_model = build_self_model(
        runtime,
        scope=scope,
        persist=False,
        profile_key=profile_key,
        capability_scope=capability_scope,
        at_time=at_time,
    )
    if legacy_compatibility:
        self_model = {
            **self_model,
            "capabilities": _legacy_self_model_capabilities(runtime, scope=scope),
            "legacy_compatibility": True,
        }
    ranked_signals = rank_learning_signals(watch_report.get("signals") or [], self_model, [], max_items=20)
    goal_registry = load_goal_registry(legacy_compatibility=legacy_compatibility)
    thought_report = generate_thoughts(
        runtime,
        signals=ranked_signals,
        self_model=self_model,
        goals=list(goal_registry.get("long_term") or []),
        scope=scope,
        loop_id="dry_run",
        persist=False,
        max_items=20,
        profile_key=profile_key,
        capability_scope=capability_scope,
        at_time=at_time,
        legacy_compatibility=legacy_compatibility,
    )
    goals = generate_learning_goals(
        self_model,
        ranked_signals,
        goal_registry=goal_registry,
        thoughts=[] if legacy_compatibility else thought_report.get("thoughts") or [],
        max_goals=max_goals,
    )
    capability_selection = _resolve_autonomous_capability_selection(
        runtime,
        scope=scope,
        runtime_scope=runtime_scope,
        profile_key=profile_key,
        capability_scope=capability_scope,
        at_time=at_time,
        legacy_compatibility=legacy_compatibility,
    )
    active_capability_ids = list(capability_selection.get("capability_ids") or [])
    selected_goal = goals[0] if goals else _fallback_goal(legacy_compatibility=legacy_compatibility)
    research_tasks = plan_research_tasks(selected_goal, source_policy={"network_enabled": allow_network})
    evidence: list[dict[str, Any]] = []
    for task in research_tasks:
        evidence.extend(collect(task, runtime=runtime, scope=scope))
    network_research = _network_research_summary(enabled=allow_network, tasks=research_tasks, evidence=evidence)
    target_capability = _explicit_goal_capability(selected_goal)
    candidate_kinds = (
        choose_candidate_kinds_for_goal(
            selected_goal,
            max_candidates=max(1, min(3, max_goals)),
            legacy_compatibility=legacy_compatibility,
        )
        if legacy_compatibility
        or (
            target_capability in set(active_capability_ids)
            and not _implicit_capability_fallback(selected_goal)
        )
        else []
    )
    candidate_kind, candidate_patch = _resolved_candidate_kind_and_patch(
        selected_goal,
        evidence,
        candidate_kind=candidate_kinds[0] if candidate_kinds else "",
        replay_dataset={},
        legacy_compatibility=legacy_compatibility,
    )
    candidate_kinds = [candidate_kind, *[kind for kind in candidate_kinds[1:] if kind != candidate_kind]] if candidate_kind else candidate_kinds
    network_research["output_gate"] = _network_output_gate(
        runtime,
        enabled=allow_network,
        scope=scope,
        loop_id="dry_run",
        goal=selected_goal,
        evidence=evidence,
        candidate_kinds=candidate_kinds,
        research_note_id="",
        replay_dataset={},
        persist=False,
    )
    eval_result = run_learning_eval(
        runtime,
        {
            "candidate_id": "dry_run_candidate",
            "candidate_kind": candidate_kind,
            "authority_tier": selected_goal.get("authority_tier") or "L0",
            "source_record_ids": [str(item.get("ref") or "") for item in evidence if item.get("ref")],
        },
        scope=scope,
        loop_id="dry_run",
        eval_suite=_measured_learning_eval_suite(
            evidence=evidence,
            replay_gate={"ok": False, "reason": "dry_run_real_task_replay_not_executed"},
            safety_replay={"ok": False, "reason": "dry_run_safety_replay_not_executed"},
            isolation_gate_passed=None,
        ),
        persist=False,
    )
    eval_result["gate_bundle"] = _gate_bundle_for_candidate(
        candidate_kind,
        evidence=evidence,
        scope=scope,
        prompt_text=_candidate_prompt_text(candidate_patch),
        prompt_safety_executor=getattr(runtime, "prompt_safety_executor", None),
        release=current_release_identity(runtime, scope),
    )
    result = {
        "ok": True,
        "loop_id": "dry_run",
        "loop_record_id": "",
        "scope": asdict(scope),
        "dry_run": True,
        "apply": bool(apply),
        "full": bool(full),
        "legacy_compatibility": bool(legacy_compatibility),
        "capability_selection": capability_selection,
        "active_capability_ids": active_capability_ids,
        "watch_signal_count": _as_int(watch_report.get("signal_count"), default=0),
        "thought_count": _as_int(thought_report.get("thought_count"), default=0),
        "goal_count": len(goals),
        "selected_goal_id": "",
        "selected_goal": selected_goal,
        "research_task_count": len(research_tasks),
        "network_research": network_research,
        "research_task_ids": [],
        "research_note_id": "",
        "experiment_id": "",
        "eval_record_id": "",
        "eval_verdict": str(eval_result.get("verdict") or ""),
        "candidate_id": "",
        "candidate_preview": {
            "candidate_kind": candidate_kind,
            "candidate_kinds": candidate_kinds,
            "target_capability": target_capability,
            "patch": candidate_patch,
        },
        "promotion": {"ok": True, "applied": False, "dry_run": True},
        "regression_watch": {"ok": True, "regressed": False, "record_id": ""},
        "capability_score_id": "",
        "ledger": build_capability_ledger(
            runtime,
            scope=scope,
            legacy_compatibility=legacy_compatibility,
        ),
        "retention": compact_learning_records(runtime, scope=scope, loop_id="dry_run", dry_run=True),
    }
    result.update(classify_autonomous_learning_activity(result))
    return result


def list_learning_goals(runtime: Any, *, scope: dict[str, Any] | ScopeRef | None = None, limit: int = 10) -> list[dict[str, Any]]:
    scope_ref = scope if isinstance(scope, ScopeRef) else ScopeRef.from_dict(scope)
    return [record.to_dict() for record in runtime.store.list_records(kinds=["learning_goal"], scope=scope_ref, limit=limit)]


def _resolve_autonomous_capability_selection(
    runtime: Any,
    *,
    scope: ScopeRef,
    runtime_scope: ScopeRef | Mapping[str, Any] | None,
    profile_key: str,
    capability_scope: str,
    at_time: str,
    legacy_compatibility: bool,
) -> dict[str, Any]:
    """Resolve a bounded autonomous-learning cohort without name inference.

    The selected Registry/Profile view is only a control-plane input.  Later
    candidate and replay paths require the stricter acceptance-result subset.
    An unavailable, empty, or cross-scope view deliberately remains empty.
    """

    if legacy_compatibility:
        return {
            "ok": True,
            "mode": "legacy_compatibility",
            "reason": "",
            "capability_ids": list(LEGACY_AUTONOMOUS_LEARNING_CAPABILITIES),
            "profile_key": "",
            "capability_scope": capability_scope,
            "runtime_scope": asdict(scope),
        }
    try:
        from eimemory.capabilities.consumer_views import dynamic_capability_views
        from eimemory.capabilities.registry import exact_runtime_scope

        exact_scope = exact_runtime_scope(runtime_scope if runtime_scope is not None else scope)
    except Exception as exc:
        return {
            "ok": False,
            "mode": "dynamic",
            "reason": f"dynamic_runtime_scope_invalid:{type(exc).__name__}",
            "capability_ids": [],
            "profile_key": str(profile_key or "").strip(),
            "capability_scope": capability_scope,
            "runtime_scope": asdict(scope),
        }
    if asdict(exact_scope) != asdict(scope):
        return {
            "ok": False,
            "mode": "dynamic",
            "reason": "dynamic_runtime_scope_mismatch",
            "capability_ids": [],
            "profile_key": str(profile_key or "").strip(),
            "capability_scope": capability_scope,
            "runtime_scope": asdict(exact_scope),
        }
    try:
        view = dynamic_capability_views(
            runtime,
            scope=exact_scope,
            profile_key=str(profile_key or "").strip(),
            capability_scope=capability_scope,
            at_time=at_time,
            limit=499,
        )
    except Exception as exc:
        return {
            "ok": False,
            "mode": "dynamic",
            "reason": f"dynamic_capability_selection_failed:{type(exc).__name__}",
            "capability_ids": [],
            "profile_key": str(profile_key or "").strip(),
            "capability_scope": capability_scope,
            "runtime_scope": asdict(exact_scope),
        }
    capability_ids = sorted(
        {
            str(item.get("capability_id") or "").strip()
            for item in view.get("capabilities") or []
            if isinstance(item, Mapping) and str(item.get("capability_id") or "").strip()
        }
    )
    return {
        "ok": bool(capability_ids),
        "mode": "dynamic",
        "reason": "" if capability_ids else "dynamic_capability_selection_empty",
        "capability_ids": capability_ids,
        "profile_key": str(profile_key or "").strip(),
        "capability_scope": capability_scope,
        "runtime_scope": asdict(exact_scope),
        "profile": dict(view.get("profile") or {}),
        "resolution_digest": str(view.get("resolution_digest") or ""),
        "registry_watermark": str(view.get("registry_watermark") or ""),
        "lifecycle_watermark": str(view.get("lifecycle_watermark") or ""),
        "source": str(view.get("source") or ""),
    }


def _explicit_goal_capability(goal: Mapping[str, Any] | object) -> str:
    """Return only a declared/attributed capability; never infer from prose."""

    if not isinstance(goal, Mapping):
        return ""
    for key in ("target_capability", "capability", "attributed_capability", "capability_id"):
        value = str(goal.get(key) or "").strip()
        if value:
            return value
    for key in ("capability_attribution", "attribution"):
        attribution = goal.get(key)
        if not isinstance(attribution, Mapping):
            continue
        for capability_key in ("capability_id", "capability", "target_capability"):
            value = str(attribution.get(capability_key) or "").strip()
            if value:
                return value
    try:
        hypothesis = explicit_hypothesis_reference(goal)
    except Exception:
        hypothesis = {}
    return str(hypothesis.get("capability_id") or "").strip() if isinstance(hypothesis, Mapping) else ""


def _implicit_capability_fallback(goal: Mapping[str, Any] | object) -> bool:
    """Identify old generic goals that named a capability without provenance.

    A registry match alone is not enough to legitimize the historical
    ``source_type=fallback`` goal: otherwise a seeded legacy descriptor would
    recreate the old proactive default.  A future planner can opt in by
    attaching an exact Profile/Registry/attribution/hypothesis reference.
    """

    if not isinstance(goal, Mapping):
        return False
    source_type = str(goal.get("source_type") or goal.get("classification") or "").strip().lower()
    if source_type not in {"fallback", "default", "legacy_fallback"}:
        return False
    try:
        if explicit_hypothesis_reference(goal):
            return False
    except Exception:
        pass
    for field in ("capability_origin", "capability_source", "target_capability_source"):
        source = str(goal.get(field) or "").strip().lower()
        if source in {"registry", "profile", "attribution", "hypothesis"}:
            return False
    for field in ("capability_attribution", "attribution"):
        attribution = goal.get(field)
        if isinstance(attribution, Mapping) and any(
            str(attribution.get(key) or "").strip()
            for key in ("capability_id", "capability", "target_capability")
        ):
            return False
    provenance = goal.get("capability_provenance")
    if isinstance(provenance, Mapping):
        source = str(provenance.get("source") or provenance.get("kind") or "").strip().lower()
        if source in {"registry", "profile", "attribution", "hypothesis"}:
            return False
    return True


def _loop_capabilities(
    goals: list[dict[str, Any]],
    *,
    allowed_capability_ids: list[str] | tuple[str, ...] | set[str] | None = None,
    legacy_compatibility: bool = False,
) -> list[str]:
    allowed = {str(value or "").strip() for value in allowed_capability_ids or [] if str(value or "").strip()}
    capabilities: list[str] = []
    for goal in goals:
        capability = _explicit_goal_capability(goal)
        if (
            capability
            and (legacy_compatibility or not _implicit_capability_fallback(goal))
            and (legacy_compatibility or capability in allowed)
            and capability not in capabilities
        ):
            capabilities.append(capability)
    if capabilities or not legacy_compatibility:
        return capabilities
    return list(LEGACY_AUTONOMOUS_LEARNING_CAPABILITIES)


def _accepted_capability_ids(acceptance_results: list[dict[str, Any]]) -> list[str]:
    """Read the actual dynamic acceptance cohort, preserving no static list."""

    observed: dict[str, set[str]] = {}
    passed: dict[str, set[str]] = {}
    for result in acceptance_results:
        if not isinstance(result, Mapping):
            continue
        capability = str(result.get("capability") or "").strip()
        case_id = str(result.get("case_id") or "").strip()
        if not capability or not case_id:
            continue
        observed.setdefault(capability, set()).add(case_id)
        if result.get("passed") is True:
            passed.setdefault(capability, set()).add(case_id)
    return sorted(
        capability
        for capability, case_ids in observed.items()
        if case_ids and case_ids.issubset(passed.get(capability, set()))
    )


def _evidence_bound_capabilities(
    capabilities: list[str],
    acceptance_results: list[dict[str, Any]],
    *,
    legacy_compatibility: bool = False,
) -> list[str]:
    """Require every selected acceptance case to pass before replay.

    Dynamic selection trusts the exact acceptance output rather than consulting
    an imported static replay catalog.  The old complete-case expectation is
    retained only for explicit legacy replay.
    """

    observed_case_ids: dict[str, set[str]] = {}
    passed_case_ids: dict[str, set[str]] = {}
    for item in acceptance_results:
        if not isinstance(item, Mapping):
            continue
        capability = str(item.get("capability") or "").strip()
        case_id = str(item.get("case_id") or "").strip()
        if not capability or not case_id:
            continue
        observed_case_ids.setdefault(capability, set()).add(case_id)
        if item.get("passed") is True:
            passed_case_ids.setdefault(capability, set()).add(case_id)

    eligible: list[str] = []
    for value in capabilities:
        capability = str(value or "").strip()
        if not capability or capability in eligible:
            continue
        observed = observed_case_ids.get(capability, set())
        if legacy_compatibility:
            required = set(capability_replay_case_ids(capability, legacy_compatibility=True))
        else:
            required = observed
        if required and required.issubset(passed_case_ids.get(capability, set())):
            eligible.append(capability)
    return eligible


def _network_enabled(value: bool | None) -> bool:
    if value is not None:
        return bool(value)
    return _env_truthy("EIMEMORY_AUTONOMOUS_LEARNING_NETWORK", default=True)


def _network_research_summary(*, enabled: bool, tasks: list[dict[str, Any]], evidence: list[dict[str, Any]]) -> dict[str, Any]:
    network_tasks = [task for task in tasks if bool(task.get("network"))]
    web_items = [item for item in evidence if str(item.get("kind") or "") == "web_learning_scout"]
    error_items = [item for item in evidence if str(item.get("kind") or "").startswith("web_learning_scout_")]
    return {
        "enabled": bool(enabled),
        "task_count": len(network_tasks),
        "hypothesis_count": len(web_items),
        "error_count": len(error_items),
        "evidence_refs": [str(item.get("ref") or "") for item in web_items[:10] if item.get("ref")],
    }


_NETWORK_OUTPUT_TARGET_BY_CANDIDATE_KIND = {
    "memory_rule": "rule",
    "eval_case": "replay",
    "skill_draft": "skill",
    "code_patch": "patch",
    "source_policy": "source_score",
}


def _network_output_gate(
    runtime: Any,
    *,
    enabled: bool,
    scope: ScopeRef,
    loop_id: str,
    goal: dict[str, Any],
    evidence: list[dict[str, Any]],
    candidate_kinds: list[str],
    research_note_id: str,
    replay_dataset: dict[str, Any] | None,
    persist: bool,
) -> dict[str, Any]:
    web_items = [item for item in evidence if str(item.get("kind") or "") == "web_learning_scout"]
    if not enabled:
        return _network_output_gate_report(
            decision="skipped",
            reason="network_disabled",
            landing_targets=[],
            web_items=web_items,
            candidate_kinds=candidate_kinds,
        )
    if not web_items:
        return _network_output_gate_report(
            decision="skipped",
            reason="no_web_hypotheses",
            landing_targets=[],
            web_items=web_items,
            candidate_kinds=candidate_kinds,
        )

    # The presence of an explicit, already-selected capability and declared
    # artifact contract is the actionable signal.  Do not infer actionability
    # from words embedded in a capability identifier.
    target_capability = _explicit_goal_capability(goal)
    actionable_goal = bool(target_capability and candidate_kinds)
    landing_targets: list[str] = []
    if actionable_goal:
        for kind in candidate_kinds:
            target = _NETWORK_OUTPUT_TARGET_BY_CANDIDATE_KIND.get(str(kind))
            if target and target not in landing_targets:
                landing_targets.append(target)

    confidence = max((_as_float(item.get("confidence"), default=0.0) for item in web_items), default=0.0)
    if confidence < 0.55:
        decision = "summary_only"
        reason = "low_confidence_web_hypotheses"
        landing_targets = ["summary"]
    elif not landing_targets:
        decision = "summary_only"
        reason = "no_actionable_landing_target"
        landing_targets = ["summary"]
    else:
        decision = "actionable"
        reason = "mapped_to_output_targets"

    report = _network_output_gate_report(
        decision=decision,
        reason=reason,
        landing_targets=landing_targets,
        web_items=web_items,
        candidate_kinds=candidate_kinds,
        target_capability=target_capability,
        research_note_id=research_note_id,
        replay_dataset=replay_dataset or {},
    )
    if persist:
        record = runtime.store.append(
            RecordEnvelope.create(
                kind="reflection",
                title="Network learning output gate",
                summary=_network_output_gate_summary(report),
                detail=f"loop_id={loop_id}; research_note_id={research_note_id}",
                scope=scope,
                source="eimemory.autonomous_learning.network_output_gate",
                content={
                    "report_type": "network_learning_output_gate",
                    "loop_id": loop_id,
                    "decision": report["decision"],
                    "reason": report["reason"],
                    "landing_targets": list(report["landing_targets"]),
                    "target_capability": report.get("target_capability", ""),
                    "candidate_kinds": list(candidate_kinds),
                    "research_note_id": research_note_id,
                    "web_evidence": _network_output_evidence(web_items),
                    "replay_case_count": _replay_case_count(replay_dataset),
                },
                tags=["autonomous_learning", "network_learning", "output_gate", str(report["decision"])],
                evidence=[str(item.get("ref") or "") for item in web_items[:10] if item.get("ref")],
                meta={"report_type": "network_learning_output_gate", "loop_id": loop_id, "decision": report["decision"]},
            )
        )
        report["summary_record_id"] = record.record_id
        if report["decision"] == "actionable" and "source_score" in report["landing_targets"]:
            report["source_score_record_ids"] = _persist_network_source_scores(
                runtime,
                scope=scope,
                loop_id=loop_id,
                goal=goal,
                report=report,
                web_items=web_items,
                research_note_id=research_note_id,
            )
    return report


def _network_output_gate_report(
    *,
    decision: str,
    reason: str,
    landing_targets: list[str],
    web_items: list[dict[str, Any]],
    candidate_kinds: list[str],
    target_capability: str = "",
    research_note_id: str = "",
    replay_dataset: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "ok": True,
        "decision": decision,
        "reason": reason,
        "landing_targets": list(landing_targets),
        "web_evidence_count": len(web_items),
        "evidence_refs": [str(item.get("ref") or "") for item in web_items[:10] if item.get("ref")],
        "candidate_kinds": list(candidate_kinds),
        "target_capability": target_capability,
        "research_note_id": research_note_id,
        "replay_case_count": _replay_case_count(replay_dataset),
        "summary_record_id": "",
        "source_score_record_ids": [],
    }


def _persist_network_source_scores(
    runtime: Any,
    *,
    scope: ScopeRef,
    loop_id: str,
    goal: dict[str, Any],
    report: dict[str, Any],
    web_items: list[dict[str, Any]],
    research_note_id: str,
) -> list[str]:
    source_scores = [
        {
            "ref": str(item.get("ref") or ""),
            "tier": str(item.get("tier") or ""),
            "confidence": _as_float(item.get("confidence"), default=0.0),
            "summary": str(item.get("summary") or "")[:400],
        }
        for item in web_items[:10]
    ]
    if not source_scores:
        return []
    avg_confidence = round(sum(item["confidence"] for item in source_scores) / len(source_scores), 3)
    record = runtime.store.append(
        RecordEnvelope.create(
            kind="reflection",
            title="Network source score",
            summary=f"Scored {len(source_scores)} network learning sources for {goal.get('target_capability') or 'autonomous learning'}.",
            detail=f"loop_id={loop_id}; research_note_id={research_note_id}",
            scope=scope,
            source="eimemory.autonomous_learning.network_source_score",
            content={
                "report_type": "network_source_score",
                "loop_id": loop_id,
                "target_capability": str(goal.get("target_capability") or ""),
                "research_note_id": research_note_id,
                "decision": str(report.get("decision") or ""),
                "landing_targets": list(report.get("landing_targets") or []),
                "average_confidence": avg_confidence,
                "source_scores": source_scores,
            },
            tags=["autonomous_learning", "network_learning", "source_score"],
            evidence=[item["ref"] for item in source_scores if item.get("ref")],
            meta={
                "report_type": "network_source_score",
                "loop_id": loop_id,
                "target_capability": str(goal.get("target_capability") or ""),
                "source_count": len(source_scores),
                "average_confidence": avg_confidence,
            },
        )
    )
    return [record.record_id]


def _network_output_evidence(web_items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "tier": str(item.get("tier") or ""),
            "ref": str(item.get("ref") or ""),
            "summary": str(item.get("summary") or "")[:400],
            "confidence": _as_float(item.get("confidence"), default=0.0),
        }
        for item in web_items[:10]
    ]


def _network_output_gate_summary(report: dict[str, Any]) -> str:
    if report.get("decision") == "actionable":
        targets = ", ".join(list(report.get("landing_targets") or []))
        return f"Network learning routed to actionable targets: {targets}."
    if report.get("decision") == "summary_only":
        return "Network learning kept as reference summary; no durable artifact was justified."
    return f"Network learning skipped: {report.get('reason') or 'no action'}."


def _as_float(value: Any, *, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _as_int(value: Any, *, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _replay_case_count(replay_dataset: dict[str, Any] | None) -> int:
    dataset = replay_dataset or {}
    cases = dataset.get("cases") if isinstance(dataset.get("cases"), list) else []
    return _as_int(dataset.get("case_count"), default=len(cases))


def _first_present(payload: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in payload and payload.get(key) is not None:
            return payload.get(key)
    return None


def _all_t6_evidence(evidence: list[dict[str, Any]]) -> bool:
    return bool(evidence) and all(str(item.get("tier") or "").upper() == "T6" for item in evidence)


def list_learning_loops(runtime: Any, *, scope: dict[str, Any] | ScopeRef | None = None, limit: int = 10) -> list[dict[str, Any]]:
    scope_ref = scope if isinstance(scope, ScopeRef) else ScopeRef.from_dict(scope)
    return [record.to_dict() for record in runtime.store.list_records(kinds=["learning_loop"], scope=scope_ref, limit=limit)]


def list_learning_candidates(runtime: Any, *, scope: dict[str, Any] | ScopeRef | None = None, limit: int = 10) -> list[dict[str, Any]]:
    scope_ref = scope if isinstance(scope, ScopeRef) else ScopeRef.from_dict(scope)
    return [record.to_dict() for record in runtime.store.list_records(kinds=["capability_candidate"], scope=scope_ref, limit=limit)]


def _fallback_goal(*, legacy_compatibility: bool = False) -> dict[str, Any]:
    """Create an observation-only fallback unless legacy replay is explicit."""

    target_capability = (
        LEGACY_AUTONOMOUS_LEARNING_FALLBACK_CAPABILITY if legacy_compatibility else ""
    )
    return {
        "goal_type": "maintenance",
        "title": "Refresh autonomous learning loop",
        "question": "Maintain learning evidence and eval coverage.",
        "success_criteria": "Create one evidence-backed maintenance candidate.",
        "authority_tier": "L0",
        "priority": 0.4,
        "target_capability": target_capability,
        "classification": "legacy_compatibility" if legacy_compatibility else "unclassified",
        "blocked_reason": "" if legacy_compatibility else "target_capability_unclassified",
        "semantic_key": stable_semantic_key(
            "fallback_learning_goal_legacy" if legacy_compatibility else "fallback_learning_goal_unclassified"
        ),
    }


def _candidate_kind_for_goal(
    goal: dict[str, Any],
    *,
    legacy_compatibility: bool = False,
) -> str:
    kinds = choose_candidate_kinds_for_goal(
        goal,
        max_candidates=1,
        legacy_compatibility=legacy_compatibility,
    )
    return kinds[0] if kinds else ""


def _candidate_specs_for_goals(
    goals: list[dict[str, Any]],
    *,
    max_goals: int = 3,
    max_candidates_per_goal: int = 1,
    replay_dataset: dict[str, Any] | None = None,
    evidence: list[dict[str, Any]] | None = None,
    runtime: Any | None = None,
    scope: ScopeRef | dict[str, Any] | None = None,
    allowed_capability_ids: list[str] | tuple[str, ...] | set[str] | None = None,
    legacy_compatibility: bool = False,
) -> list[dict[str, Any]]:
    specs: list[dict[str, Any]] = []
    for goal in _candidate_goal_portfolio(
        goals,
        max_goals=max_goals,
        allowed_capability_ids=allowed_capability_ids,
        legacy_compatibility=legacy_compatibility,
    ):
        target_capability = _explicit_goal_capability(goal)
        if not target_capability:
            continue
        for requested_target in choose_candidate_kinds_for_goal(
            goal,
            max_candidates=max_candidates_per_goal,
            legacy_compatibility=legacy_compatibility,
        ):
            promotion_target, patch = _resolved_candidate_kind_and_patch(
                goal,
                evidence or [],
                candidate_kind=requested_target,
                replay_dataset=replay_dataset,
                runtime=runtime,
                scope=scope,
                legacy_compatibility=legacy_compatibility,
            )
            spec = {
                "goal": dict(goal),
                "target_capability": target_capability,
                "requested_target": requested_target,
                "promotion_target": promotion_target,
                "patch": patch,
                # Only an explicit hypothesis id is propagated.  A candidate
                # never infers one from its goal wording or research volume.
                "capability_hypothesis": explicit_hypothesis_reference(goal),
            }
            specs.append(spec)
    if specs:
        return specs
    if not legacy_compatibility:
        return []
    fallback = _fallback_goal(legacy_compatibility=True)
    promotion_target, patch = _resolved_candidate_kind_and_patch(
        fallback,
        evidence or [],
        candidate_kind=_candidate_kind_for_goal(fallback, legacy_compatibility=True),
        replay_dataset=replay_dataset,
        runtime=runtime,
        scope=scope,
        legacy_compatibility=True,
    )
    return [
        {
            "goal": fallback,
            "target_capability": fallback["target_capability"],
            "requested_target": promotion_target,
            "promotion_target": promotion_target,
            "patch": patch,
            "capability_hypothesis": explicit_hypothesis_reference(fallback),
        }
    ]


def _candidate_goal_portfolio(
    goals: list[dict[str, Any]],
    *,
    max_goals: int = 3,
    allowed_capability_ids: list[str] | tuple[str, ...] | set[str] | None = None,
    legacy_compatibility: bool = False,
) -> list[dict[str, Any]]:
    limit = max(1, _as_int(max_goals, default=1))
    allowed = {str(value or "").strip() for value in allowed_capability_ids or [] if str(value or "").strip()}
    selected: list[dict[str, Any]] = []
    seen_categories: set[str] = set()
    for goal in goals:
        if not isinstance(goal, dict):
            continue
        capability = _explicit_goal_capability(goal)
        if (
            not capability
            or (not legacy_compatibility and _implicit_capability_fallback(goal))
            or (not legacy_compatibility and capability not in allowed)
        ):
            continue
        category = _capability_category(goal)
        if category in seen_categories:
            continue
        selected.append(dict(goal))
        seen_categories.add(category)
        if len(selected) >= limit:
            return selected
    for goal in goals:
        if not isinstance(goal, dict):
            continue
        capability = _explicit_goal_capability(goal)
        if (
            capability
            and (legacy_compatibility or capability in allowed)
            and (legacy_compatibility or not _implicit_capability_fallback(goal))
            and goal not in selected
        ):
            selected.append(dict(goal))
        if len(selected) >= limit:
            break
    return selected


def _capability_category(goal: Mapping[str, Any] | str | object) -> str:
    """Use only declared portfolio metadata or the exact capability id.

    This replaces keyword mapping (for example, translating a word containing
    ``memory`` to ``memory.recall``).  A goal with no authoritative target is
    deliberately unclassified instead of being merged into a familiar bucket.
    """

    if isinstance(goal, Mapping):
        category = str(
            goal.get("capability_category")
            or goal.get("attribution_category")
            or goal.get("portfolio_category")
            or ""
        ).strip()
        if category:
            return f"declared:{category}"
        capability = _explicit_goal_capability(goal)
    else:
        capability = str(goal or "").strip()
    return f"capability:{capability}" if capability else "unclassified"


def _goal_id_for_goal(goal: dict[str, Any], goals: list[dict[str, Any]], goal_ids: list[str]) -> str:
    semantic_key = str(goal.get("semantic_key") or "")
    for index, item in enumerate(goals):
        if semantic_key and str(item.get("semantic_key") or "") == semantic_key:
            return goal_ids[index] if index < len(goal_ids) else ""
    return ""


def choose_candidate_kinds_for_goal(
    goal: dict[str, Any],
    *,
    max_candidates: int = 3,
    legacy_compatibility: bool = False,
) -> list[str]:
    """Choose only declared artifact contracts on the dynamic path."""

    if (
        (not _explicit_goal_capability(goal) or _implicit_capability_fallback(goal))
        and not legacy_compatibility
    ):
        return []
    kinds = (
        _legacy_candidate_kinds_for_goal(goal)
        if legacy_compatibility
        else _declared_candidate_kinds_for_goal(goal)
    )
    return _bounded_candidate_kinds(kinds, max_candidates=max_candidates)


def _declared_candidate_kinds_for_goal(goal: Mapping[str, Any]) -> list[str]:
    kinds: list[str] = []
    for field in ("candidate_kinds", "promotion_targets"):
        values = goal.get(field)
        if not isinstance(values, (list, tuple, set)):
            continue
        for value in values:
            kind = str(value or "").strip().lower()
            if kind in _CANDIDATE_KINDS:
                kinds.append(kind)
    for field in ("candidate_kind", "promotion_target", "expected_artifact"):
        declared = str(goal.get(field) or "").strip().lower()
        if not declared:
            continue
        if declared in _CANDIDATE_KINDS:
            kinds.append(declared)
        else:
            kinds.extend(_EXPLICIT_ARTIFACT_KIND_ALIASES.get(declared, ()))
    return kinds


def _legacy_candidate_kinds_for_goal(goal: Mapping[str, Any]) -> list[str]:
    """Historic name-based routing, reachable only through the explicit flag."""

    capability = _explicit_goal_capability(goal).lower()
    goal_type = str(goal.get("goal_type") or "").lower()
    expected = str(goal.get("expected_artifact") or "").lower()
    kinds: list[str] = []
    if "routing" in capability or "tool" in capability:
        kinds.extend(["tool_route", "eval_case"])
    if "recall" in capability or "memory" in capability or goal_type == "benchmark_gap":
        kinds.extend(["memory_rule", "eval_case"])
    if "code" in capability:
        kinds.extend(["code_patch", "eval_case"])
    if "skill" in capability or ("skill" in expected and "code" not in capability):
        kinds.extend(["skill_draft", "eval_case"])
    if "source" in capability or "research" in capability or "knowledge" in capability:
        kinds.extend(["source_policy", "eval_case"])
    if any(term in capability for term in ("uumit", "office", "device", "operations")) or goal_type in {"long_term", "proactive_thought", "capability_gap"}:
        kinds.extend(["sop_draft", "eval_case"])
    if not kinds:
        kinds.extend(["sop_draft", "eval_case"])
    return kinds


def _bounded_candidate_kinds(kinds: list[str], *, max_candidates: int) -> list[str]:
    deduped: list[str] = []
    for kind in kinds:
        if kind in _CANDIDATE_KINDS and kind not in deduped:
            deduped.append(kind)
        if len(deduped) >= max(1, _as_int(max_candidates, default=3)):
            break
    return deduped


def _resolved_candidate_kind_and_patch(
    goal: dict[str, Any],
    evidence: list[dict[str, Any]],
    *,
    candidate_kind: str | None = None,
    replay_dataset: dict[str, Any] | None = None,
    runtime: Any | None = None,
    scope: ScopeRef | dict[str, Any] | None = None,
    legacy_compatibility: bool = False,
) -> tuple[str, dict[str, Any]]:
    requested = str(
        candidate_kind
        or _candidate_kind_for_goal(goal, legacy_compatibility=legacy_compatibility)
    ).strip()
    if not legacy_compatibility and _implicit_capability_fallback(goal):
        patch = _candidate_patch(
            goal,
            evidence,
            candidate_kind="",
            replay_dataset=replay_dataset,
            legacy_compatibility=False,
        )
        patch["promotion_target"] = ""
        return "", patch
    if not requested or requested not in _CANDIDATE_KINDS:
        patch = _candidate_patch(
            goal,
            evidence,
            candidate_kind="",
            replay_dataset=replay_dataset,
            legacy_compatibility=legacy_compatibility,
        )
        patch["promotion_target"] = ""
        return "", patch
    if requested == "code_patch":
        patch = _proposed_code_patch(
            goal,
            evidence,
            replay_dataset=replay_dataset,
            runtime=runtime,
            scope=scope,
            legacy_compatibility=legacy_compatibility,
        )
        patch["promotion_target"] = "code_patch"
        return "code_patch", patch
    patch = _candidate_patch(
        goal,
        evidence,
        candidate_kind=requested,
        replay_dataset=replay_dataset,
        legacy_compatibility=legacy_compatibility,
    )
    patch["promotion_target"] = requested
    return requested, patch


def _candidate_patch(
    goal: dict[str, Any],
    evidence: list[dict[str, Any]],
    *,
    candidate_kind: str | None = None,
    replay_dataset: dict[str, Any] | None = None,
    legacy_compatibility: bool = False,
) -> dict[str, Any]:
    target_capability = _explicit_goal_capability(goal)
    summary = str(goal.get("success_criteria") or goal.get("question") or "")
    kind = str(
        candidate_kind
        or _candidate_kind_for_goal(goal, legacy_compatibility=legacy_compatibility)
    ).strip()
    replay_case_ids = [str(case.get("case_id") or "") for case in (replay_dataset or {}).get("cases", [])[:10] if case.get("case_id")]
    base = {
        "summary": summary,
        "target_capability": target_capability,
        "goal_type": str(goal.get("goal_type") or "maintenance"),
        "pattern": str(goal.get("pattern") or target_capability),
        "policy": str(goal.get("question") or goal.get("title") or ""),
        "execution_policy": [summary or str(goal.get("question") or goal.get("title") or "Apply learned operating policy.")],
        "success_criteria": summary,
        "evidence_refs": [str(item.get("ref") or "") for item in evidence[:10] if item.get("ref")],
        "replay_case_ids": replay_case_ids,
    }
    base.update(_candidate_contract_fields(goal, kind=kind, summary=summary, replay_case_ids=replay_case_ids))
    implicit_fallback = not legacy_compatibility and _implicit_capability_fallback(goal)
    if not target_capability or kind not in _CANDIDATE_KINDS or implicit_fallback:
        blocked_reasons = [str(reason) for reason in base.get("blocked_reasons") or [] if str(reason)]
        if not target_capability:
            blocked_reasons.append("target_capability_unclassified")
        if kind not in _CANDIDATE_KINDS:
            blocked_reasons.append("candidate_kind_unclassified")
        if implicit_fallback:
            blocked_reasons.append("implicit_capability_fallback_unclassified")
        return {
            **base,
            "promotion_ready": False,
            "blocked_reasons": list(dict.fromkeys(blocked_reasons)),
            "proposal_status": "blocked",
            "proposal_blocked_reason": "target_capability_unclassified"
            if not target_capability
            else "implicit_capability_fallback_unclassified"
            if implicit_fallback
            else "candidate_kind_unclassified",
        }
    if kind == "eval_case":
        cases = list((replay_dataset or {}).get("cases") or [])
        first = cases[0] if cases else {}
        return {
            **base,
            "input": str(first.get("input") or first.get("query") or goal.get("question") or ""),
            "expected": str(first.get("expected_behavior") or first.get("expected") or (first.get("expected_text") or [""])[0] or summary or goal.get("success_criteria") or ""),
            "expected_text": list(first.get("expected_text") or []),
            "labels": [target_capability, str(goal.get("goal_type") or "maintenance")],
        }
    if kind == "code_patch":
        structured_patch = _structured_code_patch(goal=goal, replay_dataset=replay_dataset)
        verify_commands = _env_argv_commands("EIMEMORY_AUTONOMOUS_CODE_VERIFY_COMMAND")
        deploy_commands = _env_argv_commands("EIMEMORY_AUTONOMOUS_CODE_DEPLOY_COMMAND")
        # Automatic evolution may write a verified local patch when the caller
        # opted into ``apply``.  Production deployment and repository commits
        # remain explicit environment/patch decisions rather than defaults.
        deploy_default = bool(structured_patch.get("deploy_to_production")) if "deploy_to_production" in structured_patch else False
        deploy_enabled = _env_truthy("EIMEMORY_AUTONOMOUS_CODE_DEPLOY", default=deploy_default)
        commit_default = bool(structured_patch.get("commit_to_repo")) if "commit_to_repo" in structured_patch else False
        commit_enabled = _env_truthy("EIMEMORY_AUTONOMOUS_CODE_COMMIT", default=commit_default)
        return {
            **base,
            **structured_patch,
            "repo_root": str(structured_patch.get("repo_root") or os.environ.get("EIMEMORY_AUTONOMOUS_CODE_REPO") or ""),
            "apply_to_repo": True,
            "deploy_to_production": deploy_enabled,
            "commit_to_repo": commit_enabled,
            "allowed_files": list(structured_patch.get("allowed_files") or []),
            "file_updates": list(structured_patch.get("file_updates") or []),
            "verification_commands": list(structured_patch.get("verification_commands") or verify_commands),
            "deployment_commands": list(structured_patch.get("deployment_commands") or deploy_commands),
            "rollback_plan": dict(structured_patch.get("rollback_plan") or {"type": "restore_files"}),
        }
    if kind == "skill_draft":
        return {
            **base,
            "skill_name": _safe_skill_name(target_capability),
            "triggers": [str(goal.get("title") or target_capability), target_capability],
            "eval_cases": list((replay_dataset or {}).get("cases") or [])[:3],
        }
    if kind == "sop_draft":
        return {
            **base,
            "steps": [
                "Check relevant memory, outcome traces, and current constraints.",
                "Choose the lowest-risk action path that satisfies the user's real intent.",
                "Verify the outcome with explicit evidence before reporting completion.",
                "Record outcome and update replay coverage when the result is uncertain or corrected.",
            ],
        }
    if kind == "tool_route":
        dataset = replay_dataset or {}
        execution_policy = [
            str(
                _first_text(
                    dataset.get("summary"),
                    str(goal.get("policy") or goal.get("title") or ""),
                    "Prefer policy-driven routing over direct execution.",
                )
            )
        ]
        return {
            **base,
            "execution_policy": execution_policy,
            "route_policy": {
                "match": target_capability,
                "prefer": "policy_suggestions before semantic memory",
                "fallback": "ask for missing irreversible or physical execution constraints",
            },
        }
    if kind == "source_policy":
        return {
            **base,
            "source_policy": {
                "capability": target_capability,
                "promote_when": "source is recent, deduped, and linked to a long-term goal or repeated weakness",
                "reject_when": "source is stale, duplicated, unverifiable, or unrelated to active goals",
            },
        }
    return base


def _candidate_summary(goal: dict[str, Any], *, candidate_kind: str, patch: dict[str, Any]) -> str:
    capability = _explicit_goal_capability(goal) or str(patch.get("target_capability") or "").strip() or "unclassified capability"
    artifact = _candidate_artifact_label(candidate_kind)
    title = _first_text(goal.get("question"), goal.get("title"), patch.get("summary"), patch.get("policy"))
    criteria = _first_text(goal.get("success_criteria"), patch.get("success_criteria"))
    if patch.get("fallback_reason") == "code_patch_missing_file_updates":
        base = f"{capability} {artifact} fallback: {title or 'empty code patch output'}"
        return _short_text(f"{base}. Missing file_updates, so the learning is kept as SOP/eval instead of patch promotion. {criteria}", 280)
    if criteria and criteria != title:
        return _short_text(f"{capability} {artifact}: {title}. Success: {criteria}", 280)
    return _short_text(f"{capability} {artifact}: {title or criteria or 'Create a reusable learning asset.'}", 280)


def _candidate_contract_fields(goal: dict[str, Any], *, kind: str, summary: str, replay_case_ids: list[str]) -> dict[str, Any]:
    trigger = _first_text(goal.get("trigger_condition"), goal.get("title"), goal.get("question"), goal.get("target_capability"))
    action = _first_text(goal.get("action"), summary, goal.get("success_criteria"), f"Produce {kind} candidate for {goal.get('target_capability') or 'capability'}")
    verification = _first_text(goal.get("verification"), goal.get("success_criteria"), "Run replay/eval gate and inspect persisted evidence.")
    rollback = _first_text(goal.get("rollback"), "Disable candidate, keep it in candidate status, or restore previous policy/artifact.")
    blocked_reasons: list[str] = []
    if not trigger:
        blocked_reasons.append("missing_trigger_condition")
    if not action:
        blocked_reasons.append("missing_action")
    if not verification:
        blocked_reasons.append("missing_verification")
    if not rollback:
        blocked_reasons.append("missing_rollback")
    if kind != "eval_case" and not replay_case_ids:
        blocked_reasons.append("missing_replay_case_ids")
    return {
        "trigger_condition": trigger,
        "action": action,
        "verification": verification,
        "rollback": rollback,
        "promotion_ready": not blocked_reasons,
        "blocked_reasons": blocked_reasons,
    }


def _candidate_artifact_label(candidate_kind: str) -> str:
    labels = {
        "tool_route": "tool routing policy",
        "memory_rule": "memory recall rule",
        "eval_case": "replay eval case",
        "skill_draft": "skill draft",
        "sop_draft": "SOP",
        "source_policy": "knowledge intake source policy",
        "code_patch": "code patch",
    }
    return labels.get(str(candidate_kind or ""), str(candidate_kind or "candidate").replace("_", " "))


def _proposed_code_patch(
    goal: dict[str, Any],
    evidence: list[dict[str, Any]],
    *,
    replay_dataset: dict[str, Any] | None,
    runtime: Any | None,
    scope: ScopeRef | dict[str, Any] | None,
    legacy_compatibility: bool = False,
) -> dict[str, Any]:
    """Create a constrained code patch instead of silently downgrading to SOP.

    A missing generator is an explicit blocked code proposal.  It is never
    represented as a successful policy/SOP candidate, which keeps the learning
    record honest and lets a later configured proposer retry the same goal.
    """

    base = _candidate_patch(
        goal,
        evidence,
        candidate_kind="code_patch",
        replay_dataset=replay_dataset,
        legacy_compatibility=legacy_compatibility,
    )
    if not legacy_compatibility and _implicit_capability_fallback(goal):
        return {
            **base,
            "promotion_target": "",
            "proposal_status": "blocked",
            "proposal_blocked_reason": "implicit_capability_fallback_unclassified",
        }
    structured = _structured_code_patch(goal=goal, replay_dataset=replay_dataset)
    if not legacy_compatibility:
        # The historical proposer path accepts a generic environment LLM and
        # carries provider-owned verification commands.  It remains available
        # only for the explicitly named legacy replay compatibility mode.  A
        # normal scheduler run must arrive with the v2 transaction context and
        # use ``propose_code_patch_v2`` directly; silently downgrading here
        # would reopen prompt-to-command authority.
        return {
            **base,
            "promotion_target": "code_patch",
            "proposal_status": "blocked",
            "proposal_blocked_reason": "code_evolution_v2_transaction_context_required",
            "legacy_compatibility": False,
        }
    repo_root = str(
        structured.get("repo_root")
        or goal.get("repo_root")
        or os.environ.get("EIMEMORY_AUTONOMOUS_CODE_REPO")
        or ""
    ).strip()
    proposer, proposer_reason = resolve_code_patch_proposer(
        runtime=runtime,
        goal=goal,
        legacy_compatibility=legacy_compatibility,
    )
    incident = {
        "classification": "code_fixable",
        "incident_id": str(goal.get("semantic_key") or goal.get("goal_id") or goal.get("title") or "learning_goal"),
        "incident_type": "code.implementation",
        "title": str(goal.get("title") or "Autonomous code evolution"),
        "summary": str(goal.get("question") or goal.get("success_criteria") or "Generate a constrained code patch."),
        "detail": str(goal.get("success_criteria") or ""),
        "files": _code_proposal_allowed_files(goal=goal, structured=structured),
        "code_patch": dict(structured),
        "evidence": [
            {"ref": str(item.get("ref") or ""), "summary": str(item.get("summary") or "")[:500]}
            for item in evidence[:10]
            if isinstance(item, dict)
        ],
    }
    proposal = propose_code_patch(
        runtime,
        incident=incident,
        scope=_code_proposal_scope(scope),
        proposer=proposer,
        repo_root=repo_root or None,
    )
    status = str(proposal.get("proposal_status") or "proposal_invalid")
    blocked_reason = str(proposal.get("blocked_reason") or "invalid_code_proposal")
    if status == "proposal_unavailable" and not structured and proposer_reason:
        blocked_reason = proposer_reason
    automation_policy, requested_machine_actions, machine_policy_reason = _bridge_machine_policy_envelope(
        proposal
    )
    # A code proposal may be syntactically valid while still lacking machine
    # authority to perform its declared effects.  This is a terminal machine
    # gate for this candidate, never a redirect to a manual approval queue.
    if status == "proposal_ready" and machine_policy_reason:
        status = "proposal_blocked"
        blocked_reason = machine_policy_reason
    machine_policy_authorized = status == "proposal_ready" and not machine_policy_reason

    common = {
        "proposal_status": status,
        "proposal_blocked_reason": blocked_reason if status != "proposal_ready" else "",
        "proposal_source": str(proposal.get("proposal_source") or ""),
        "proposal_report_type": str(proposal.get("report_type") or ""),
        "authorization_mode": "machine_gated",
        "decision_authority": "machine_policy",
        "machine_policy_required_for_apply": True,
        # These are copied only from the code-evolution bridge.  The bridge
        # owns sanitization; goal/proposer/incident policy data is never read
        # as authority in this path.
        "automation_policy": automation_policy,
        "requested_machine_actions": requested_machine_actions,
        "machine_policy_status": "authorized" if machine_policy_authorized else "blocked",
        "machine_policy_blocked_reason": "" if machine_policy_authorized else (machine_policy_reason or blocked_reason),
        "unified_diff": str(proposal.get("unified_diff") or ""),
        "patch_digest": str(proposal.get("patch_digest") or ""),
        "base_commit": str(proposal.get("base_commit") or ""),
        "subject_commit": str(proposal.get("subject_commit") or ""),
        "subject_state_digest": str(proposal.get("subject_state_digest") or ""),
        "file_base_digest": str(proposal.get("file_base_digest") or ""),
    }
    if status == "proposal_ready":
        return {
            **base,
            **common,
            "repo_root": str(proposal.get("repo_root") or repo_root),
            "allowed_files": list(proposal.get("allowed_files") or []),
            "file_updates": list(proposal.get("file_updates") or []),
            "verification_commands": list(proposal.get("verification_commands") or []),
            "apply_to_repo": True,
            "commit_to_repo": "commit" in requested_machine_actions,
            "deploy_to_production": "deployment" in requested_machine_actions,
            "rollback_plan": dict(base.get("rollback_plan") or {"type": "restore_files"}),
        }

    previous_blocked = [str(item) for item in base.get("blocked_reasons") or [] if str(item)]
    if blocked_reason and blocked_reason not in previous_blocked:
        previous_blocked.append(blocked_reason)
    return {
        **base,
        **common,
        "repo_root": repo_root,
        "allowed_files": [],
        "file_updates": [],
        "verification_commands": [],
        "promotion_ready": False,
        "blocked_reasons": previous_blocked,
        # Do not leave a blocked proposal looking executable to a later
        # adapter.  A new machine-authorized proposal must be generated.
        "apply_to_repo": False,
        "commit_to_repo": False,
        "deploy_to_production": False,
    }


def _bridge_machine_policy_envelope(
    proposal: Mapping[str, Any],
) -> tuple[dict[str, Any], list[str], str]:
    """Return the bridge-sanitized policy and exact requested effects.

    A candidate receives no policy from its goal, incident, structured patch,
    or proposer.  The code-evolution bridge is the single handoff here; its
    policy copy is diagnostic while the promotion path independently resolves
    its trusted machine policy.  Missing, malformed, or non-authorizing
    envelopes fail closed without creating a human-review state.
    """

    raw_policy = proposal.get("automation_policy")
    if not isinstance(raw_policy, Mapping):
        reason = "automation_policy_missing" if raw_policy in (None, {}) else "automation_policy_ambiguous"
        return _empty_machine_policy_envelope(), [], reason

    raw_declared = raw_policy.get("declared")
    raw_policy_id = raw_policy.get("policy_id")
    raw_actions = raw_policy.get("actions")
    if raw_declared is not None and not isinstance(raw_declared, bool):
        return _empty_machine_policy_envelope(), [], "automation_policy_ambiguous"
    if raw_policy_id is not None and not isinstance(raw_policy_id, str):
        return _empty_machine_policy_envelope(), [], "automation_policy_ambiguous"
    if not isinstance(raw_actions, Mapping):
        return _empty_machine_policy_envelope(), [], "automation_policy_ambiguous"
    if set(raw_actions) != set(AUTOMATION_POLICY_ACTIONS) or any(
        not isinstance(raw_actions.get(action), bool) for action in AUTOMATION_POLICY_ACTIONS
    ):
        return _empty_machine_policy_envelope(), [], "automation_policy_ambiguous"

    # Preserve the bridge's bounded diagnostics (for example policy digest and
    # deployment-environment source) while normalizing the authority-bearing
    # fields into the stable candidate schema.
    policy = dict(raw_policy)
    policy_id = str(raw_policy_id or "").strip()
    policy["declared"] = bool(raw_declared) if raw_declared is not None else bool(policy_id)
    policy["policy_id"] = policy_id
    policy["actions"] = {action: bool(raw_actions[action]) for action in AUTOMATION_POLICY_ACTIONS}

    if not policy["declared"]:
        return policy, [], "automation_policy_missing"
    if not policy_id:
        return policy, [], "automation_policy_id_missing"

    raw_requested_actions = proposal.get("requested_machine_actions")
    if not isinstance(raw_requested_actions, (list, tuple)):
        reason = (
            "requested_machine_actions_missing"
            if raw_requested_actions in (None, [])
            else "requested_machine_actions_ambiguous"
        )
        return policy, [], reason
    requested_actions: list[str] = []
    for value in raw_requested_actions:
        if not isinstance(value, str):
            return policy, [], "requested_machine_actions_ambiguous"
        action = value.strip()
        if not action or action not in AUTOMATION_POLICY_ACTIONS or action in requested_actions:
            return policy, [], "requested_machine_actions_ambiguous"
        requested_actions.append(action)
    if not requested_actions:
        return policy, [], "requested_machine_actions_missing"
    if "local_apply" not in requested_actions:
        return policy, requested_actions, "requested_machine_actions_ambiguous"

    for action in requested_actions:
        if not policy["actions"][action]:
            return policy, requested_actions, f"automation_policy_{action}_not_enabled"
    # Deployment in the code-apply path is subordinate to a commit policy even
    # when the proposal's requested-actions record omits a separate commit.
    if "deployment" in requested_actions and not policy["actions"]["commit"]:
        return policy, requested_actions, "automation_policy_commit_not_enabled"
    return policy, requested_actions, ""


def _empty_machine_policy_envelope() -> dict[str, Any]:
    return {
        "declared": False,
        "policy_id": "",
        "actions": {action: False for action in AUTOMATION_POLICY_ACTIONS},
    }


def resolve_code_patch_proposer(
    *,
    runtime: Any | None,
    goal: dict[str, Any],
    legacy_compatibility: bool = False,
) -> tuple[object | None, str]:
    if not legacy_compatibility:
        return None, "code_implementation_v2_provider_required"
    for value in (
        goal.get("code_patch_proposer"),
        getattr(runtime, "code_patch_proposer", None) if runtime is not None else None,
        getattr(runtime, "autonomous_code_proposer", None) if runtime is not None else None,
    ):
        if value is not None:
            return value, ""
    try:
        command_client = llm_client_from_env("CODE_PATCH")
    except (OSError, ValueError):
        return None, "code_proposer_configuration_invalid"
    if command_client is None:
        return None, "code_proposer_unavailable"

    def propose(**kwargs: Any) -> dict[str, Any]:
        incident = dict(kwargs.get("incident") or {})
        prompt = {
            "task": "Return one safe structured code patch as JSON.",
            "incident": incident,
            "allowed_files": list(kwargs.get("allowed_files") or []),
            "required_schema": {
                "file_updates": [{"path": "relative/path.py", "content": "complete UTF-8 replacement text"}],
                "allowed_files": ["relative/path.py"],
                "verification_commands": [["python", "-m", "pytest", "-q", "tests/test_target.py"]],
            },
            "constraints": [
                "Only edit allowed_files.",
                "Use complete replacement content for each file update.",
                "Do not emit a full test-suite command.",
                "Use argv arrays for commands; do not include shell redirection, deployment, commit, push, or prose outside JSON.",
            ],
        }
        result = command_client.complete(
            system_prompt="You generate minimal, verifiable code patches for an automated but fail-closed maintenance system.",
            user_prompt=json.dumps(prompt, ensure_ascii=False),
            json_mode=True,
        )
        payload = _json_object_from_model_text(result.text)
        payload["generator"] = {"provider_id": result.provider_id, "model_id": result.model_id}
        return payload

    return propose, ""


def _json_object_from_model_text(text: str) -> dict[str, Any]:
    raw = str(text or "").strip()
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[1] if "\n" in raw else ""
        if raw.endswith("```"):
            raw = raw[:-3].strip()
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError("code_proposer_result_not_json") from exc
    if not isinstance(parsed, dict):
        raise ValueError("code_proposer_result_not_object")
    return dict(parsed)


def _code_proposal_allowed_files(*, goal: dict[str, Any], structured: dict[str, Any]) -> list[str]:
    for value in (structured.get("allowed_files"), goal.get("allowed_files"), goal.get("files"), goal.get("paths")):
        if isinstance(value, (list, tuple, set)):
            return [str(item) for item in value if str(item).strip()]
    return []


def _code_proposal_scope(scope: ScopeRef | dict[str, Any] | None) -> dict[str, Any]:
    if isinstance(scope, ScopeRef):
        return asdict(scope)
    return dict(scope or {})


def _structured_code_patch(*, goal: dict[str, Any], replay_dataset: dict[str, Any] | None) -> dict[str, Any]:
    for value in (goal.get("code_patch"), goal.get("candidate_patch"), goal.get("patch")):
        if isinstance(value, dict) and _has_file_updates(value):
            return dict(value)
    for case in list((replay_dataset or {}).get("cases") or []):
        if not isinstance(case, dict):
            continue
        for value in (case.get("code_patch"), case.get("candidate_patch"), case.get("patch")):
            if isinstance(value, dict) and _has_file_updates(value):
                return dict(value)
        if _has_file_updates(case):
            return dict(case)
    return {}


def _has_file_updates(value: dict[str, Any]) -> bool:
    updates = value.get("file_updates") or value.get("files") or []
    if not isinstance(updates, list):
        return False
    return any(isinstance(item, dict) and str(item.get("path") or item.get("file") or "").strip() and item.get("content") is not None for item in updates)


def _safe_skill_name(capability: str) -> str:
    value = "".join(ch.lower() if ch.isalnum() else "-" for ch in str(capability or "unclassified-skill"))
    value = "-".join(part for part in value.split("-") if part)
    return value or "unclassified-skill"


def _first_text(*values: Any) -> str:
    for value in values:
        text = " ".join(str(value or "").split())
        if text:
            return text
    return ""


def _short_text(text: str, limit: int) -> str:
    value = " ".join(str(text or "").split())
    if len(value) <= limit:
        return value
    return value[: max(0, limit - 3)].rstrip() + "..."


def _env_truthy(name: str, *, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "on", "y", "enabled", "apply"}


def _env_argv_commands(name: str) -> list[list[str]]:
    raw = str(os.environ.get(name) or "").strip()
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return []
    if _is_argv_command(parsed):
        return [[str(part) for part in parsed]]
    if not isinstance(parsed, list):
        return []
    return [[str(part) for part in item] for item in parsed if _is_argv_command(item)]


def _is_argv_command(value: Any) -> bool:
    return isinstance(value, (list, tuple)) and bool(value) and all(
        not isinstance(part, (dict, list, tuple)) for part in value
    )


def _evidence_score(evidence: list[dict[str, Any]]) -> float:
    if not evidence:
        return 0.0
    tier_scores = {"T0": 1.0, "T1": 0.9, "T2": 0.75, "T3": 0.7, "T4": 0.6, "T5": 0.45, "T6": 0.1}
    scores = [tier_scores.get(str(item.get("tier") or "").upper(), 0.4) for item in evidence]
    return round(min(1.0, sum(scores) / len(scores)), 3)


def _candidate_prompt_text(candidate_patch: dict[str, Any] | None) -> str:
    patch = dict(candidate_patch or {})
    for key in ("system_prompt", "prompt", "prompt_text", "replacement", "content", "text"):
        value = patch.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
        if isinstance(value, dict):
            nested = _candidate_prompt_text(value)
            if nested:
                return nested
    return ""


def _gate_bundle_for_candidate(
    candidate_kind: str,
    *,
    evidence: list[dict[str, Any]],
    scope: ScopeRef,
    prompt_text: str | None = None,
    prompt_safety_executor: Any = None,
    release: ReleaseIdentity | None = None,
    real_task_replay: dict[str, Any] | None = None,
    replay_gate: dict[str, Any] | None = None,
) -> dict[str, Any]:
    target = str(candidate_kind or "").lower()
    prompt_target = target in {"prompt_policy", "system_prompt_patch"}
    # When the candidate has no prompt body (e.g. a tool-route patch) the
    # gate is genuinely inapplicable; mark it skipped with a ``None``
    # passed value (not ``True``) so downstream code can distinguish
    # "passed" from "not run". When a prompt body IS supplied for a
    # prompt-target candidate, the real stub check must run — we no longer
    # fall through to a tautological ``True`` (Bug A, see
    # 2026-06-18-eimemory-six-bug-fix-batch §2 Task 1).
    if prompt_target:
        # Use the explicit prompt text when the caller has it; otherwise
        # fall back to the candidate kind itself (the only string the
        # gate-bundle builder reliably has). The stub scans the input for
        # known injection markers — passing the candidate kind is good
        # enough for now and will be replaced once the gate-bundle builder
        # has access to the full candidate content.
        body = str(prompt_text or "").strip()
        final_release = release or ReleaseIdentity(commit="", version="", receipt_id="", session_id="")
        assessment = run_prompt_safety_battery(prompt_safety_executor, body, final_release)
        assessment_payload = assessment.to_dict()
        static_shadow = bool(prompt_shadow_eval(body, cases=3))
        static_injection = bool(prompt_injection_check(body, cases=3))
        battery_passed = assessment.status == "passed" and assessment.complete
        not_ready = assessment.status == "not_ready"
        prompt_shadow_field: dict[str, Any] = {
            "passed": bool(battery_passed and static_shadow),
            "skipped": False,
            "cases": assessment.expected_count,
            "notready": not_ready,
            "battery": assessment_payload,
            "static_prefilter_passed": static_shadow,
        }
        prompt_injection_field: dict[str, Any] = {
            "passed": bool(battery_passed and static_injection),
            "skipped": False,
            "cases": assessment.expected_count,
            "notready": not_ready,
            "battery": assessment_payload,
            "static_prefilter_passed": static_injection,
        }
    else:
        prompt_shadow_field = {
            "passed": None,
            "skipped": True,
            "reason": "no_prompt_target",
            "cases": 0,
        }
        prompt_injection_field = {
            "passed": None,
            "skipped": True,
            "reason": "no_prompt_target",
            "cases": 0,
        }
    return {
        "evidence": [
            {
                "tier": "T1",
                "ref": "learning_eval",
                "summary": "Offline learning eval and regression checks passed before rollout.",
            },
            *[
            {
                "tier": str(item.get("tier") or item.get("evidence_tier") or "T2"),
                "ref": str(item.get("ref") or ""),
                "summary": str(item.get("summary") or "")[:240],
            }
            for item in evidence[:10]
            ],
        ],
        "rollback": {
            "available": True,
            "executable": True,
            "method": "disable_candidate_or_rollback_intent_pattern",
        },
        "canary": {
            "passed": True,
            "scope": asdict(scope),
            "blast_radius": "single_scope",
        },
        "closed_loop": {
            "doctor": {"ok": True, "source": "autonomous_learning_gate"},
            "smoke": {"ok": True, "source": "autonomous_learning_gate"},
        },
        "timeout_seconds": 900,
        "cooldown_seconds": 3600,
        "audit": {"enabled": True, "ledger": "promotion_request"},
        "real_task_replay": _gate_real_task_replay(real_task_replay or {}, replay_gate or {}),
        "prompt_shadow_eval": prompt_shadow_field,
        "prompt_injection_check": prompt_injection_field,
    }


def _gate_real_task_replay(real_task_replay: dict[str, Any], replay_gate: dict[str, Any]) -> dict[str, Any]:
    if real_task_replay:
        report = dict(real_task_replay)
        report.setdefault("ok", bool(replay_gate.get("ok")))
        report.setdefault("verdict", "pass" if replay_gate.get("ok") else "fail")
        report.setdefault("pass_rate", replay_gate.get("pass_rate") or (1.0 if replay_gate.get("ok") else 0.0))
        report.setdefault("threshold", replay_gate.get("threshold") or 0.6)
        report.setdefault("sample_count", replay_gate.get("sample_count") or report.get("case_count") or report.get("pass_count") or 0)
        return report
    return {
        "ok": bool(replay_gate.get("ok")),
        "report_type": "real_task_replay",
        "verdict": "pass" if replay_gate.get("ok") else "fail",
        "pass_rate": replay_gate.get("pass_rate") or (1.0 if replay_gate.get("ok") else 0.0),
        "threshold": replay_gate.get("threshold") or 0.6,
        "sample_count": replay_gate.get("sample_count") or 0,
        "reason": str(replay_gate.get("reason") or ""),
    }
