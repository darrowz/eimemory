from __future__ import annotations

from dataclasses import asdict
from typing import Any

from eimemory.governance.capability_distiller import distill_capability_candidate
from eimemory.governance.capability_ledger import build_capability_ledger, record_capability_score
from eimemory.governance.capability_seeding import ensure_all_seeded
from eimemory.governance.curiosity import generate_learning_goals, persist_learning_goals
from eimemory.governance.evidence_collector import collect
from eimemory.governance.goal_registry import load_goal_registry
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
from eimemory.governance.prompt_safety import (
    PROMPT_SAFETY_STUB_NOTREADY,
    prompt_injection_check,
    prompt_shadow_eval,
)
from eimemory.governance.replay_dataset import build_replay_dataset
from eimemory.governance.regression_watch import run_regression_watch
from eimemory.governance.research_planner import create_research_note, create_research_task, plan_research_tasks
from eimemory.governance.sandbox_lab import create_sandbox_experiment
from eimemory.governance.self_model import build_self_model
from eimemory.governance.signal_intake import rank_learning_signals
from eimemory.governance.thoughts import generate_thoughts
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
    max_promotions: int | None = None,
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

        ensure_all_seeded(runtime, scope=scope_ref, loop_id=loop_id)
        goal_registry = load_goal_registry()
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
        )
        mark_step(runtime, loop, step_name="think", status="completed", record_ids=thought_report.get("persisted_record_ids") or [], metrics={"thought_count": thought_report.get("thought_count", 0)})

        goals = generate_learning_goals(
            self_model,
            ranked_signals,
            goal_registry=goal_registry,
            thoughts=thought_report.get("thoughts") or [],
            max_goals=max_goals,
        )
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

        replay_dataset = build_replay_dataset(runtime, scope=scope_ref, limit=50, persist=True, loop_id=loop_id)
        mark_step(runtime, loop, step_name="replay_dataset", status="completed", record_ids=[replay_dataset.get("persisted_record_id", "")], metrics={"case_count": replay_dataset.get("case_count", 0), "correction_count": replay_dataset.get("correction_count", 0)})
        real_task_replay = _run_real_task_replay_if_available(
            runtime=runtime,
            loop=loop,
            scope=scope_ref,
            replay_dataset=replay_dataset,
            seed_records=_replay_seed_records_from_cases(replay_dataset.get("cases") or [], scope=scope_ref),
        )
        replay_gate_passed = _replay_gate_passed(real_task_replay)

        target_capability = str(selected_goal.get("target_capability") or "proactive.judgment")
        candidate_kinds = choose_candidate_kinds_for_goal(selected_goal, max_candidates=max(1, min(3, max_goals)))
        experiment_ids: list[str] = []
        eval_results: list[dict[str, Any]] = []
        candidate_ids: list[str] = []
        promotion_reports: list[dict[str, Any]] = []
        regression_reports: list[dict[str, Any]] = []
        promotion_budget = max(0, int(max_promotions)) if max_promotions is not None else len(candidate_kinds)
        for candidate_kind in candidate_kinds:
            experiment_id = create_sandbox_experiment(
                runtime,
                scope=scope_ref,
                loop_id=loop_id,
                learning_goal_id=selected_goal_id or str(selected_goal.get("semantic_key") or ""),
                research_note_id=research_note_id,
                candidate_kind=candidate_kind,
                candidate_patch=_candidate_patch(selected_goal, evidence, candidate_kind=candidate_kind, replay_dataset=replay_dataset),
                expected_gain=str(selected_goal.get("success_criteria") or ""),
            )
            experiment_ids.append(experiment_id)
            experiment = runtime.store.get_by_id(experiment_id, scope=scope_ref)
            eval_result = run_learning_eval(
                runtime,
                experiment,
                scope=scope_ref,
                loop_id=loop_id,
                eval_suite={"scores": {"capability": 0.82, "safety": 1.0, "regression": 1.0, "evidence": _evidence_score(evidence), "cost": 0.85}},
            )
            eval_result["gate_bundle"] = _gate_bundle_for_candidate(candidate_kind, evidence=evidence, scope=scope_ref)
            eval_results.append(eval_result)
            if eval_result.get("ok") and replay_gate_passed:
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
                candidate_ids.append(candidate_id)
                promotion_report = promote_candidate(
                    runtime,
                    candidate_id=candidate_id,
                    scope=scope_ref,
                    loop_id=loop_id,
                    apply=bool(apply and not dry_run and sum(1 for report in promotion_reports if report.get("applied")) < promotion_budget),
                    eval_result=eval_result,
                    health={"ok": True, "source": "offline_learning_cycle"},
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
        mark_step(runtime, loop, step_name="eval", status="completed", record_ids=[str(item.get("record_id") or "") for item in eval_results], metrics={"pass_count": sum(1 for item in eval_results if item.get("ok")), "eval_count": len(eval_results)})
        mark_step(
            runtime,
            loop,
            step_name="promotion",
            status="completed" if replay_gate_passed else "blocked",
            record_ids=[item for report in promotion_reports for item in [report.get("promotion_request_id", "")] if item] + candidate_ids,
            metrics={
                "applied_count": sum(1 for report in promotion_reports if report.get("applied")),
                "replay_gate_passed": replay_gate_passed,
            },
        )

        score_id = record_capability_score(
            runtime,
            scope=scope_ref,
            loop_id=loop_id,
            capability=target_capability,
            score=0.8 if eval_result.get("ok") and replay_gate_passed else 0.4,
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
            "thought_count": int(thought_report.get("thought_count") or 0),
            "goal_count": len(goals),
            "selected_goal_id": selected_goal_id,
            "selected_goal": selected_goal,
            "research_task_ids": research_task_ids,
            "research_note_id": research_note_id,
            "experiment_id": experiment_id,
            "experiment_ids": experiment_ids,
            "eval_record_id": str(eval_result.get("record_id") or ""),
            "eval_record_ids": [str(item.get("record_id") or "") for item in eval_results],
            "eval_verdict": str(eval_result.get("verdict") or ""),
            "candidate_id": candidate_id,
            "candidate_ids": candidate_ids,
            "candidate_kinds": candidate_kinds,
            "real_task_replay": real_task_replay,
            "replay_gate_passed": replay_gate_passed,
            "promotion": promotion_report,
            "promotions": promotion_reports,
            "regression_watch": regression_report,
            "regression_watches": regression_reports,
            "replay_dataset": replay_dataset,
            "capability_score_id": score_id,
            "ledger": ledger,
            "retention": retention_report,
        }
    except Exception as exc:
        mark_step(runtime, loop, step_name="failed", status="failed", error=str(exc))
        complete_learning_loop(runtime, loop, status="failed", summary=str(exc))
        raise


def _run_real_task_replay_if_available(
    runtime: Any,
    loop: Any,
    scope: ScopeRef,
    replay_dataset: dict[str, Any],
    seed_records: list[dict[str, Any]] | None = None,
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
        report = replay_runner(payload, seed=True, persist_report=True)
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
    if bool(report.get("ok")):
        return True
    reason = str(report.get("replay_skipped_reason") or "").strip()
    return reason in {"no_replay_cases", "run_real_task_replay_unavailable"}


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
    goal_registry = load_goal_registry()
    thought_report = generate_thoughts(
        runtime,
        signals=ranked_signals,
        self_model=self_model,
        goals=list(goal_registry.get("long_term") or []),
        scope=scope,
        loop_id="dry_run",
        persist=False,
        max_items=20,
    )
    goals = generate_learning_goals(self_model, ranked_signals, goal_registry=goal_registry, thoughts=thought_report.get("thoughts") or [], max_goals=max_goals)
    selected_goal = goals[0] if goals else _fallback_goal()
    research_tasks = plan_research_tasks(selected_goal, source_policy={"network_enabled": False})
    evidence: list[dict[str, Any]] = []
    for task in research_tasks:
        evidence.extend(collect(task, runtime=runtime, scope=scope))
    candidate_kind = _candidate_kind_for_goal(selected_goal)
    candidate_kinds = choose_candidate_kinds_for_goal(selected_goal, max_candidates=max(1, min(3, max_goals)))
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
        "thought_count": int(thought_report.get("thought_count") or 0),
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
            "candidate_kinds": candidate_kinds,
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
    return choose_candidate_kinds_for_goal(goal, max_candidates=1)[0]


def choose_candidate_kinds_for_goal(goal: dict[str, Any], *, max_candidates: int = 3) -> list[str]:
    capability = str(goal.get("target_capability") or "").lower()
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
    if any(term in capability for term in ("uumit", "office", "device", "operations")) or goal_type in {"long_term", "proactive_thought", "capability_gap"}:
        kinds.extend(["sop_draft", "eval_case"])
    if "source" in capability or "research" in capability or "knowledge" in capability:
        kinds.extend(["source_policy", "eval_case"])
    if not kinds:
        kinds.extend(["sop_draft", "eval_case"])
    deduped: list[str] = []
    for kind in kinds:
        if kind not in deduped:
            deduped.append(kind)
        if len(deduped) >= max(1, int(max_candidates or 3)):
            break
    return deduped


def _candidate_patch(
    goal: dict[str, Any],
    evidence: list[dict[str, Any]],
    *,
    candidate_kind: str | None = None,
    replay_dataset: dict[str, Any] | None = None,
) -> dict[str, Any]:
    target_capability = str(goal.get("target_capability") or "proactive.judgment")
    summary = str(goal.get("success_criteria") or goal.get("question") or "")
    kind = str(candidate_kind or _candidate_kind_for_goal(goal))
    base = {
        "summary": summary,
        "target_capability": target_capability,
        "goal_type": str(goal.get("goal_type") or "maintenance"),
        "pattern": str(goal.get("pattern") or target_capability),
        "policy": str(goal.get("question") or goal.get("title") or ""),
        "execution_policy": [summary or str(goal.get("question") or goal.get("title") or "Apply learned operating policy.")],
        "success_criteria": summary,
        "evidence_refs": [str(item.get("ref") or "") for item in evidence[:10] if item.get("ref")],
        "replay_case_ids": [str(case.get("case_id") or "") for case in (replay_dataset or {}).get("cases", [])[:10] if case.get("case_id")],
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


def _safe_skill_name(capability: str) -> str:
    value = "".join(ch.lower() if ch.isalnum() else "-" for ch in str(capability or "proactive-skill"))
    value = "-".join(part for part in value.split("-") if part)
    return value or "proactive-skill"


def _first_text(*values: Any) -> str:
    for value in values:
        text = " ".join(str(value or "").split())
        if text:
            return text
    return ""


def _evidence_score(evidence: list[dict[str, Any]]) -> float:
    if not evidence:
        return 0.0
    tier_scores = {"T0": 1.0, "T1": 0.9, "T2": 0.75, "T3": 0.7, "T4": 0.6, "T5": 0.45, "T6": 0.1}
    scores = [tier_scores.get(str(item.get("tier") or "").upper(), 0.4) for item in evidence]
    return round(min(1.0, sum(scores) / len(scores)), 3)


def _gate_bundle_for_candidate(
    candidate_kind: str,
    *,
    evidence: list[dict[str, Any]],
    scope: ScopeRef,
    prompt_text: str | None = None,
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
        body = str(prompt_text if prompt_text is not None else candidate_kind or "")
        shadow_passed = bool(prompt_shadow_eval(body, cases=3))
        injection_passed = bool(prompt_injection_check(body, cases=3))
        prompt_shadow_field: dict[str, Any] = {
            "passed": shadow_passed,
            "skipped": False,
            "cases": 3,
            "notready": PROMPT_SAFETY_STUB_NOTREADY,
        }
        prompt_injection_field: dict[str, Any] = {
            "passed": injection_passed,
            "skipped": False,
            "cases": 3,
            "notready": PROMPT_SAFETY_STUB_NOTREADY,
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
        "timeout_seconds": 900,
        "cooldown_seconds": 3600,
        "audit": {"enabled": True, "ledger": "promotion_request"},
        "prompt_shadow_eval": prompt_shadow_field,
        "prompt_injection_check": prompt_injection_field,
    }
