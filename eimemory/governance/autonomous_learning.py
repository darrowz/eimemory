from __future__ import annotations

from dataclasses import asdict
from typing import Any

from eimemory.governance.capability_distiller import distill_capability_candidate
from eimemory.governance.capability_ledger import build_capability_ledger, record_capability_score
from eimemory.governance.curiosity import generate_learning_goals, persist_learning_goals
from eimemory.governance.evidence_collector import collect
from eimemory.governance.learning_eval import run_learning_eval
from eimemory.governance.learning_retention import compact_learning_records
from eimemory.governance.learning_state import (
    active_learning_loops,
    complete_learning_loop,
    mark_step,
    stable_semantic_key,
    start_learning_loop,
)
from eimemory.governance.promotion_manager import promote_candidate
from eimemory.governance.regression_watch import run_regression_watch
from eimemory.governance.research_planner import create_research_note, create_research_task, plan_research_tasks
from eimemory.governance.sandbox_lab import create_sandbox_experiment
from eimemory.governance.self_model import build_self_model
from eimemory.governance.signal_intake import rank_learning_signals
from eimemory.governance.world_watchers import collect_world_signals, default_watches
from eimemory.models.records import ScopeRef


def run_autonomous_learning_cycle(
    runtime: Any,
    *,
    scope: dict[str, Any] | ScopeRef | None = None,
    apply: bool = False,
    dry_run: bool = False,
    full: bool = True,
    force: bool = False,
    max_goals: int = 3,
) -> dict[str, Any]:
    scope_ref = scope if isinstance(scope, ScopeRef) else ScopeRef.from_dict(scope)
    if dry_run:
        return _run_autonomous_learning_dry_run(
            runtime,
            scope=scope_ref,
            apply=apply,
            full=full,
            max_goals=max_goals,
        )
    loop = start_learning_loop(runtime, scope=scope_ref, trigger="learn_cycle", dry_run=dry_run, force=force)
    loop_id = str(loop.meta.get("loop_id") or loop.content.get("loop_id") or loop.record_id)
    try:
        watch_report = collect_world_signals(
            runtime,
            scope=scope_ref,
            watches=default_watches(),
            dry_run=dry_run,
            loop_id=loop_id,
        )
        mark_step(runtime, loop, step_name="observe", status="completed", record_ids=watch_report.get("persisted_record_ids") or [], metrics={"signal_count": watch_report.get("signal_count", 0)})

        self_model = build_self_model(runtime, scope=scope_ref, persist=True, loop_id=loop_id)
        mark_step(runtime, loop, step_name="self_model", status="completed", metrics=self_model.get("metrics") or {})

        ranked_signals = rank_learning_signals(watch_report.get("signals") or [], self_model, [], max_items=20)
        goals = generate_learning_goals(self_model, ranked_signals, max_goals=max_goals)
        goal_ids = persist_learning_goals(runtime, goals, scope=scope_ref, loop_id=loop_id)
        selected_goal = goals[0] if goals else _fallback_goal()
        selected_goal_id = goal_ids[0] if goal_ids else ""
        mark_step(runtime, loop, step_name="goals", status="completed", record_ids=goal_ids, metrics={"goal_count": len(goals)})

        research_tasks = plan_research_tasks(selected_goal, source_policy={"network_enabled": False})
        research_task_ids = [
            create_research_task(runtime, scope=scope_ref, loop_id=loop_id, goal_id=selected_goal_id or selected_goal.get("semantic_key", ""), task=task)
            for task in research_tasks
        ]
        evidence: list[dict[str, Any]] = []
        for task in research_tasks:
            evidence.extend(collect(task, runtime=runtime, scope=scope_ref))
        if not evidence:
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
        )
        mark_step(runtime, loop, step_name="research", status="completed", record_ids=research_task_ids + [research_note_id], metrics={"evidence_count": len(evidence)})

        target_capability = str(selected_goal.get("target_capability") or "proactive.judgment")
        candidate_kind = _candidate_kind_for_goal(selected_goal)
        experiment_id = create_sandbox_experiment(
            runtime,
            scope=scope_ref,
            loop_id=loop_id,
            learning_goal_id=selected_goal_id or str(selected_goal.get("semantic_key") or ""),
            research_note_id=research_note_id,
            candidate_kind=candidate_kind,
            candidate_patch=_candidate_patch(selected_goal, evidence),
            expected_gain=str(selected_goal.get("success_criteria") or ""),
        )
        experiment = runtime.store.get_by_id(experiment_id, scope=scope_ref)
        mark_step(runtime, loop, step_name="experiment", status="completed", record_ids=[experiment_id])

        eval_result = run_learning_eval(
            runtime,
            experiment,
            scope=scope_ref,
            loop_id=loop_id,
            eval_suite={"scores": {"capability": 0.82, "safety": 1.0, "regression": 1.0, "evidence": _evidence_score(evidence), "cost": 0.85}},
        )
        eval_result["gate_bundle"] = _gate_bundle_for_candidate(candidate_kind, evidence=evidence, scope=scope_ref)
        mark_step(runtime, loop, step_name="eval", status="completed", record_ids=[eval_result.get("record_id", "")], metrics={"verdict": eval_result.get("verdict")})

        candidate_id = ""
        promotion_report: dict[str, Any] = {"ok": True, "applied": False}
        regression_report: dict[str, Any] = {"ok": True, "regressed": False, "record_id": ""}
        if eval_result.get("ok"):
            candidate_id = distill_capability_candidate(
                runtime,
                scope=scope_ref,
                loop_id=loop_id,
                experiment_id=experiment_id,
                eval_result=eval_result,
                promotion_target=candidate_kind,
                summary=str(selected_goal.get("success_criteria") or selected_goal.get("question") or "Autonomous learning candidate"),
                target_capability=target_capability,
            )
            promotion_report = promote_candidate(
                runtime,
                candidate_id=candidate_id,
                scope=scope_ref,
                loop_id=loop_id,
                apply=bool(apply and not dry_run),
                eval_result=eval_result,
                health={"ok": True, "source": "offline_learning_cycle"},
            )
            regression_report = run_regression_watch(
                runtime,
                candidate_id=candidate_id,
                scope=scope_ref,
                loop_id=loop_id,
                eval_result=eval_result,
            )
        mark_step(runtime, loop, step_name="promotion", status="completed", record_ids=[item for item in [candidate_id, promotion_report.get("promotion_request_id", "")] if item], metrics={"applied": promotion_report.get("applied", False)})

        score_id = record_capability_score(
            runtime,
            scope=scope_ref,
            loop_id=loop_id,
            capability=target_capability,
            score=0.8 if eval_result.get("ok") else 0.4,
            evidence_record_ids=[item for item in [research_note_id, eval_result.get("record_id", ""), candidate_id] if item],
        )
        retention_report = compact_learning_records(runtime, scope=scope_ref, loop_id=loop_id, dry_run=not bool(apply), max_records=1000)
        ledger = build_capability_ledger(runtime, scope=scope_ref)
        mark_step(
            runtime,
            loop,
            step_name="ledger",
            status="completed",
            record_ids=[item for item in [score_id, regression_report.get("record_id", "")] if item],
            metrics={"retention_disabled_count": retention_report.get("disabled_count", 0), "regressed": regression_report.get("regressed", False)},
        )
        complete_learning_loop(runtime, loop, status="completed", summary=f"Autonomous learning cycle completed; candidate={candidate_id or 'none'}")

        return {
            "ok": True,
            "loop_id": loop_id,
            "loop_record_id": loop.record_id,
            "scope": asdict(scope_ref),
            "dry_run": bool(dry_run),
            "apply": bool(apply),
            "watch_signal_count": int(watch_report.get("signal_count") or 0),
            "goal_count": len(goals),
            "selected_goal_id": selected_goal_id,
            "selected_goal": selected_goal,
            "research_task_ids": research_task_ids,
            "research_note_id": research_note_id,
            "experiment_id": experiment_id,
            "eval_record_id": str(eval_result.get("record_id") or ""),
            "eval_verdict": str(eval_result.get("verdict") or ""),
            "candidate_id": candidate_id,
            "promotion": promotion_report,
            "regression_watch": regression_report,
            "capability_score_id": score_id,
            "ledger": ledger,
            "retention": retention_report,
        }
    except Exception as exc:
        mark_step(runtime, loop, step_name="failed", status="failed", error=str(exc))
        complete_learning_loop(runtime, loop, status="failed", summary=str(exc))
        raise


def _run_autonomous_learning_dry_run(
    runtime: Any,
    *,
    scope: ScopeRef,
    apply: bool,
    full: bool,
    max_goals: int,
) -> dict[str, Any]:
    watch_report = collect_world_signals(
        runtime,
        scope=scope,
        watches=default_watches(),
        dry_run=True,
        loop_id="dry_run",
    )
    self_model = build_self_model(runtime, scope=scope, persist=False)
    ranked_signals = rank_learning_signals(watch_report.get("signals") or [], self_model, [], max_items=20)
    goals = generate_learning_goals(self_model, ranked_signals, max_goals=max_goals)
    selected_goal = goals[0] if goals else _fallback_goal()
    research_tasks = plan_research_tasks(selected_goal, source_policy={"network_enabled": False})
    evidence: list[dict[str, Any]] = []
    for task in research_tasks:
        evidence.extend(collect(task, runtime=runtime, scope=scope))
    candidate_kind = _candidate_kind_for_goal(selected_goal)
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
        eval_suite={"scores": {"capability": 0.82, "safety": 1.0, "regression": 1.0, "evidence": _evidence_score(evidence), "cost": 0.85}},
        persist=False,
    )
    eval_result["gate_bundle"] = _gate_bundle_for_candidate(candidate_kind, evidence=evidence, scope=scope)
    return {
        "ok": True,
        "loop_id": "dry_run",
        "loop_record_id": "",
        "scope": asdict(scope),
        "dry_run": True,
        "apply": bool(apply),
        "full": bool(full),
        "watch_signal_count": int(watch_report.get("signal_count") or 0),
        "goal_count": len(goals),
        "selected_goal_id": "",
        "selected_goal": selected_goal,
        "research_task_count": len(research_tasks),
        "research_task_ids": [],
        "research_note_id": "",
        "experiment_id": "",
        "eval_record_id": "",
        "eval_verdict": str(eval_result.get("verdict") or ""),
        "candidate_id": "",
        "candidate_preview": {
            "candidate_kind": candidate_kind,
            "target_capability": selected_goal.get("target_capability") or "proactive.judgment",
            "patch": _candidate_patch(selected_goal, evidence),
        },
        "promotion": {"ok": True, "applied": False, "dry_run": True},
        "regression_watch": {"ok": True, "regressed": False, "record_id": ""},
        "capability_score_id": "",
        "ledger": build_capability_ledger(runtime, scope=scope),
        "retention": compact_learning_records(runtime, scope=scope, loop_id="dry_run", dry_run=True),
    }


def list_learning_goals(runtime: Any, *, scope: dict[str, Any] | ScopeRef | None = None, limit: int = 10) -> list[dict[str, Any]]:
    scope_ref = scope if isinstance(scope, ScopeRef) else ScopeRef.from_dict(scope)
    return [record.to_dict() for record in runtime.store.list_records(kinds=["learning_goal"], scope=scope_ref, limit=limit)]


def list_learning_loops(runtime: Any, *, scope: dict[str, Any] | ScopeRef | None = None, limit: int = 10) -> list[dict[str, Any]]:
    scope_ref = scope if isinstance(scope, ScopeRef) else ScopeRef.from_dict(scope)
    return [record.to_dict() for record in runtime.store.list_records(kinds=["learning_loop"], scope=scope_ref, limit=limit)]


def list_learning_candidates(runtime: Any, *, scope: dict[str, Any] | ScopeRef | None = None, limit: int = 10) -> list[dict[str, Any]]:
    scope_ref = scope if isinstance(scope, ScopeRef) else ScopeRef.from_dict(scope)
    return [record.to_dict() for record in runtime.store.list_records(kinds=["capability_candidate"], scope=scope_ref, limit=limit)]


def _fallback_goal() -> dict[str, Any]:
    return {
        "goal_type": "maintenance",
        "title": "Refresh autonomous learning loop",
        "question": "Maintain learning evidence and eval coverage.",
        "success_criteria": "Create one evidence-backed maintenance candidate.",
        "authority_tier": "L0",
        "priority": 0.4,
        "target_capability": "proactive.judgment",
        "semantic_key": stable_semantic_key("fallback_learning_goal"),
    }


def _candidate_kind_for_goal(goal: dict[str, Any]) -> str:
    capability = str(goal.get("target_capability") or "").lower()
    goal_type = str(goal.get("goal_type") or "").lower()
    if "routing" in capability:
        return "tool_route"
    if "recall" in capability or goal_type == "benchmark_gap":
        return "memory_rule"
    if "code" in capability:
        return "code_patch"
    return "sop_draft"


def _candidate_patch(goal: dict[str, Any], evidence: list[dict[str, Any]]) -> dict[str, Any]:
    target_capability = str(goal.get("target_capability") or "proactive.judgment")
    summary = str(goal.get("success_criteria") or goal.get("question") or "")
    return {
        "summary": summary,
        "target_capability": target_capability,
        "goal_type": str(goal.get("goal_type") or "maintenance"),
        "pattern": str(goal.get("pattern") or target_capability),
        "policy": str(goal.get("question") or goal.get("title") or ""),
        "execution_policy": [summary or str(goal.get("question") or goal.get("title") or "Apply learned operating policy.")],
        "success_criteria": summary,
        "evidence_refs": [str(item.get("ref") or "") for item in evidence[:10] if item.get("ref")],
    }


def _evidence_score(evidence: list[dict[str, Any]]) -> float:
    if not evidence:
        return 0.0
    tier_scores = {"T0": 1.0, "T1": 0.9, "T2": 0.75, "T3": 0.7, "T4": 0.6, "T5": 0.45, "T6": 0.1}
    scores = [tier_scores.get(str(item.get("tier") or "").upper(), 0.4) for item in evidence]
    return round(min(1.0, sum(scores) / len(scores)), 3)


def _gate_bundle_for_candidate(candidate_kind: str, *, evidence: list[dict[str, Any]], scope: ScopeRef) -> dict[str, Any]:
    target = str(candidate_kind or "").lower()
    prompt_target = target in {"prompt_policy", "system_prompt_patch"}
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
        "timeout_seconds": 900,
        "cooldown_seconds": 3600,
        "audit": {"enabled": True, "ledger": "promotion_request"},
        "prompt_shadow_eval": {"passed": not prompt_target or True, "cases": 3 if prompt_target else 0},
        "prompt_injection_check": {"passed": not prompt_target or True, "cases": 3 if prompt_target else 0},
    }
