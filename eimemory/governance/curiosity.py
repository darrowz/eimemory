from __future__ import annotations

from typing import Any

from eimemory.governance.learning_state import append_learning_record_once, stable_semantic_key
from eimemory.models.records import ScopeRef


def generate_learning_goals(
    self_model: dict[str, Any],
    ranked_signals: list[dict[str, Any]] | None = None,
    *,
    daily_cap: int = 10,
    max_goals: int | None = None,
) -> list[dict[str, Any]]:
    limit = max_goals if max_goals is not None else daily_cap
    goals: list[dict[str, Any]] = []
    seen: set[str] = set()

    for weakness in list(self_model.get("weaknesses") or []):
        capability = str(weakness.get("capability") or "proactive.judgment")
        kind = str(weakness.get("kind") or capability)
        semantic_key = stable_semantic_key("capability_gap", capability, kind, weakness.get("lesson"))
        if semantic_key in seen:
            continue
        seen.add(semantic_key)
        goals.append(
            {
                "goal_type": "capability_gap",
                "title": f"Improve {capability} from {kind}",
                "question": f"How can the agent avoid this repeated weakness: {weakness.get('lesson') or kind}?",
                "expected_artifact": "playbook_or_policy_candidate",
                "success_criteria": "Replay/eval passes and future attributed outcomes improve",
                "authority_tier": "L1",
                "priority": round(0.7 + min(0.25, float(weakness.get("severity") or 0.0) / 4), 3),
                "semantic_key": semantic_key,
                "source_record_ids": list(weakness.get("source_record_ids") or []),
                "target_capability": capability,
                "evidence_tier": weakness.get("evidence_tier") or "T2",
            }
        )
        if len(goals) >= limit:
            return goals

    metrics = dict(self_model.get("metrics") or {})
    if float(metrics.get("replay_pass_rate") or 0.0) < 0.8:
        _append_goal(
            goals,
            seen,
            {
                "goal_type": "benchmark_gap",
                "title": "Improve replay pass rate",
                "question": "Which replay failures should become new eval cases or policy candidates?",
                "expected_artifact": "eval_case",
                "success_criteria": "Replay pass rate improves without safety regression",
                "authority_tier": "L1",
                "priority": 0.62,
                "target_capability": "memory.recall",
                "source_record_ids": [],
                "evidence_tier": "T1",
            },
            limit,
        )

    for signal in ranked_signals or []:
        signal_type = str(signal.get("signal_type") or signal.get("kind") or "world_signal")
        goal_type = "world_change" if str(signal.get("source_kind") or "").startswith(("github", "research", "web")) else "opportunity"
        _append_goal(
            goals,
            seen,
            {
                "goal_type": goal_type,
                "title": f"Learn from {signal_type}",
                "question": str(signal.get("summary") or signal.get("title") or "What should be learned from this signal?"),
                "expected_artifact": "research_note",
                "success_criteria": "Evidence-backed candidate or explicit rejection",
                "authority_tier": "L0",
                "priority": float(signal.get("score") or 0.5),
                "target_capability": str(signal.get("target_capability") or "proactive.judgment"),
                "source_record_ids": [str(signal.get("record_id"))] if signal.get("record_id") else [],
                "evidence_tier": str(signal.get("evidence_tier") or "T2"),
            },
            limit,
        )

    if not goals:
        _append_goal(
            goals,
            seen,
            {
                "goal_type": "maintenance",
                "title": "Refresh autonomous learning eval coverage",
                "question": "Which existing learning policies need replay coverage or retention cleanup?",
                "expected_artifact": "eval_case",
                "success_criteria": "At least one stale or missing eval gap is resolved",
                "authority_tier": "L0",
                "priority": 0.4,
                "target_capability": "proactive.judgment",
                "source_record_ids": [],
                "evidence_tier": "T2",
            },
            limit,
        )

    return sorted(goals, key=lambda item: (-float(item.get("priority") or 0.0), str(item.get("title") or "")))[:limit]


def persist_learning_goals(
    runtime: Any,
    goals: list[dict[str, Any]],
    *,
    scope: dict[str, Any] | ScopeRef | None = None,
    loop_id: str,
) -> list[str]:
    scope_ref = scope if isinstance(scope, ScopeRef) else ScopeRef.from_dict(scope)
    record_ids: list[str] = []
    for goal in goals:
        semantic_key = str(goal.get("semantic_key") or stable_semantic_key(goal.get("goal_type"), goal.get("title"), goal.get("question")))
        record = append_learning_record_once(
            runtime,
            kind="learning_goal",
            title=str(goal.get("title") or "Learning goal"),
            summary=str(goal.get("question") or goal.get("success_criteria") or ""),
            scope=scope_ref,
            loop_id=loop_id,
            step_name="goals",
            semantic_key=semantic_key,
            authority_tier=str(goal.get("authority_tier") or "L0"),
            status="candidate",
            content={"goal": {**goal, "semantic_key": semantic_key}},
            meta={
                "goal_type": goal.get("goal_type"),
                "target_capability": goal.get("target_capability"),
                "priority": goal.get("priority"),
            },
        )
        record_ids.append(record.record_id)
    return record_ids


def _append_goal(goals: list[dict[str, Any]], seen: set[str], goal: dict[str, Any], limit: int) -> None:
    if len(goals) >= limit:
        return
    key = stable_semantic_key(goal.get("goal_type"), goal.get("title"), goal.get("question"))
    if key in seen:
        return
    seen.add(key)
    goals.append({**goal, "semantic_key": key})
