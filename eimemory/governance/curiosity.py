from __future__ import annotations

from typing import Any

from eimemory.governance.goal_registry import derive_goal_candidates
from eimemory.governance.learning_state import append_learning_record_once, stable_semantic_key
from eimemory.governance.thoughts import promote_thoughts_to_goals
from eimemory.models.records import ScopeRef


def generate_learning_goals(
    self_model: dict[str, Any],
    ranked_signals: list[dict[str, Any]] | None = None,
    *,
    goal_registry: dict[str, Any] | None = None,
    thoughts: list[dict[str, Any]] | None = None,
    daily_cap: int = 10,
    max_goals: int | None = None,
) -> list[dict[str, Any]]:
    limit = max_goals if max_goals is not None else daily_cap
    goals: list[dict[str, Any]] = []
    seen: set[str] = set()

    for thought_goal in _high_priority_thought_goals(thoughts or [], limit=limit):
        _append_goal(goals, seen, thought_goal, limit)
        if len(goals) >= limit:
            return _finalize_goals(goals, limit)

    for candidate in _goal_registry_candidates(goal_registry):
        _append_goal(goals, seen, candidate, limit)
        if len(goals) >= limit:
            return _finalize_goals(goals, limit)

    for weakness_goal in _weakness_goals(self_model):
        _append_goal(goals, seen, weakness_goal, limit)
        if len(goals) >= limit:
            return _finalize_goals(goals, limit)

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
                "source_type": "self_model_metric",
                "source_record_ids": [],
                "evidence_tier": "T1",
            },
            limit,
        )
        if len(goals) >= limit:
            return _finalize_goals(goals, limit)

    for signal_goal in _signal_goals(ranked_signals or [], max(1, min(5, limit))):
        _append_goal(goals, seen, signal_goal, limit)
        if len(goals) >= limit:
            return _finalize_goals(goals, limit)

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
                "source_type": "fallback",
                "source_record_ids": [],
                "evidence_tier": "T2",
            },
            limit,
        )

    return _finalize_goals(goals, limit)


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
                "source_type": goal.get("source_type"),
                "source_goal_id": goal.get("source_goal_id"),
                "thought_id": goal.get("thought_id"),
                "source_record_ids": goal.get("source_record_ids") or [],
            },
        )
        record_ids.append(record.record_id)
    return record_ids


def _finalize_goals(goals: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    return sorted(
        goals,
        key=lambda item: (-float(item.get("priority") or 0.0), str(item.get("source_type") or ""), str(item.get("title") or "")),
    )[: limit]


def _high_priority_thought_goals(thoughts: list[dict[str, Any]], *, limit: int) -> list[dict[str, Any]]:
    if not thoughts or limit <= 0:
        return []
    thought_goals = promote_thoughts_to_goals(_ranked_thoughts_for_goals(thoughts), limit=limit)
    return [goal for goal in thought_goals if float(goal.get("priority") or 0.0) >= 0.4]


def _ranked_thoughts_for_goals(thoughts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(
        list(thoughts),
        key=lambda item: (
            -float(item.get("score") or 0.0),
            -float(item.get("importance") or 0.0),
            str(item.get("question") or ""),
        ),
    )


def _goal_registry_candidates(registry: dict[str, Any] | None, *, limit: int = 20) -> list[dict[str, Any]]:
    if not registry:
        return []
    candidates = derive_goal_candidates(registry, limit=max(0, int(limit)))
    for candidate in candidates:
        candidate["source_type"] = "goal_registry"
        if not candidate.get("source_goal_id"):
            candidate["source_goal_id"] = str(candidate.get("source_goal_id") or "")
        if not candidate.get("goal_type"):
            candidate["goal_type"] = "long_term"
    return candidates


def _weakness_goals(self_model: dict[str, Any]) -> list[dict[str, Any]]:
    weakness_goals: list[dict[str, Any]] = []
    for weakness in list(self_model.get("weaknesses") or []):
        capability = str(weakness.get("capability") or "proactive.judgment")
        kind = str(weakness.get("kind") or capability)
        weakness_goals.append(
            {
                "goal_type": "capability_gap",
                "title": f"Improve {capability} from {kind}",
                "question": f"How can the agent avoid this repeated weakness: {weakness.get('lesson') or kind}?",
                "expected_artifact": "playbook_or_policy_candidate",
                "success_criteria": "Replay/eval passes and future attributed outcomes improve",
                "authority_tier": "L1",
                "priority": round(0.7 + min(0.25, float(weakness.get("severity") or 0.0) / 4), 3),
                "source_record_ids": list(weakness.get("source_record_ids") or []),
                "source_type": "weakness",
                "target_capability": capability,
                "evidence_tier": weakness.get("evidence_tier") or "T2",
            }
        )
    return weakness_goals


def _signal_goals(signals: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    goals: list[dict[str, Any]] = []
    if limit <= 0:
        return goals
    for signal in signals[:limit]:
        signal_type = str(signal.get("signal_type") or signal.get("kind") or "world_signal")
        goal_type = "world_change" if str(signal.get("source_kind") or "").startswith(("github", "research", "web")) else "opportunity"
        goals.append(
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
                "source_type": str(signal.get("source_type") or signal.get("signal_type") or "world_signal"),
                "evidence_tier": str(signal.get("evidence_tier") or "T2"),
            }
        )
    return goals


def _append_goal(goals: list[dict[str, Any]], seen: set[str], goal: dict[str, Any], limit: int) -> None:
    if len(goals) >= limit:
        return
    source_type = str(goal.get("source_type") or "unknown")
    semantic_key = str(goal.get("semantic_key") or stable_semantic_key(source_type, goal.get("goal_type"), goal.get("title"), goal.get("question")))
    if semantic_key in seen:
        return
    seen.add(semantic_key)
    normalized = {**goal, "semantic_key": semantic_key}
    if "source_type" not in normalized:
        normalized["source_type"] = source_type
    goals.append(normalized)
