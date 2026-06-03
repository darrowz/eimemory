from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from eimemory.governance.learning_state import stable_semantic_key


DEFAULT_LONG_TERM_GOALS: list[dict[str, Any]] = [
    {
        "id": "lt-memory-architecture",
        "title": "AI Agent long-term memory architecture",
        "sub_capabilities": ["memory.recall_precision", "memory.salience_scoring", "preference.compaction"],
        "milestones": ["Improve recall precision", "Improve cross-session preference accuracy"],
        "evaluation_signals": ["operator corrections", "production recall eval", "memory quality stats"],
    },
    {
        "id": "lt-skill-governance",
        "title": "Skill governance and capability publishing",
        "sub_capabilities": ["skill.draft", "skill.eval_case", "skill.registry_quality"],
        "milestones": ["Draft reusable skills", "Attach eval cases before activation"],
        "evaluation_signals": ["skill replay results", "operator corrections"],
    },
    {
        "id": "lt-mcp-a2a",
        "title": "MCP and A2A tool collaboration",
        "sub_capabilities": ["tool.routing", "mcp.contract", "agent.coordination"],
        "milestones": ["Route tools by policy", "Track cross-agent outcomes"],
        "evaluation_signals": ["tool routing failures", "agent outcome traces"],
    },
    {
        "id": "lt-uumit-delivery",
        "title": "UUMit business delivery quality",
        "sub_capabilities": ["operations.uumit", "office.daily_task", "customer.delivery"],
        "milestones": ["Convert repeated delivery checks into SOPs", "Reduce rework"],
        "evaluation_signals": ["UUMit outcome traces", "operator corrections", "task verification"],
    },
    {
        "id": "lt-device-office",
        "title": "Device and office automation reliability",
        "sub_capabilities": ["device.control", "office.daily_task", "ops.health"],
        "milestones": ["Check physical constraints before actions", "Verify user-visible output"],
        "evaluation_signals": ["device control failures", "verification gaps"],
    },
    {
        "id": "lt-safety-boundary",
        "title": "Safety boundary governance",
        "sub_capabilities": ["safety.boundary", "risk.gating", "rollback.policy"],
        "milestones": ["Keep L2/L3 gated", "Make rollback evidence explicit"],
        "evaluation_signals": ["blocked promotions", "regression watches", "rollback metadata"],
    },
]


def registry_path(root: str | Path | None = None) -> Path:
    root_path = Path(root) if root is not None else Path(__file__).resolve().parents[2]
    return root_path / "goals" / "long_term.json"


def seed_long_term_goals() -> list[dict[str, Any]]:
    return [_normalize_goal(goal, index=index) for index, goal in enumerate(DEFAULT_LONG_TERM_GOALS)]


def load_goal_registry(
    path: str | Path | None = None,
    *,
    root: str | Path | None = None,
    repo_root: str | Path | None = None,
    config: str | Path | None = None,
) -> dict[str, Any]:
    fallback_root = root if root is not None else repo_root
    target = Path(config) if config is not None else (Path(path) if path is not None else registry_path(fallback_root))
    if not target.exists():
        return {
            "ok": True,
            "path": str(target),
            "long_term": seed_long_term_goals(),
            "source": "defaults",
        }
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return {
            "ok": False,
            "path": str(target),
            "long_term": seed_long_term_goals(),
            "source": "fallback",
            "error": type(exc).__name__,
            "detail": str(exc),
        }
    goals = payload.get("long_term") if isinstance(payload, dict) else payload
    if not isinstance(goals, list):
        goals = []
    normalized = [_normalize_goal(item, index=idx) for idx, item in enumerate(goals) if isinstance(item, dict)]
    return {
        "ok": True,
        "path": str(target),
        "long_term": normalized or seed_long_term_goals(),
        "source": "file",
    }


def derive_goal_signals(
    registry: dict[str, Any],
    *,
    capability_scores: dict[str, Any] | None = None,
    limit: int = 20,
) -> list[dict[str, Any]]:
    return _derive_gap_signals(list(registry.get("long_term") or []), capability_scores=capability_scores, limit=limit)


def derive_goal_candidates(
    registry: dict[str, Any],
    *,
    capability_scores: dict[str, Any] | None = None,
    limit: int = 20,
) -> list[dict[str, Any]]:
    return [
        goal_to_learning_goal(goal, priority=goal.get("priority", 0.55))
        for goal in _derive_goal_candidates(
            list(registry.get("long_term") or []),
            capability_scores=capability_scores,
            limit=limit,
        )
    ]


def _derive_gap_signals(
    goals: list[dict[str, Any]],
    *,
    capability_scores: dict[str, Any] | None = None,
    limit: int = 20,
) -> list[dict[str, Any]]:
    scores = capability_scores or {}
    signals: list[dict[str, Any]] = []
    for goal in goals[:max(0, int(limit))]:
        if not isinstance(goal, dict):
            continue
        subcaps = [str(item) for item in goal.get("sub_capabilities") or [] if str(item or "").strip()]
        weakest = _weakest_subcap(subcaps, scores)
        focus = weakest or (subcaps[0] if subcaps else "proactive.judgment")
        signals.append(
            {
                "signal_type": "goal_registry_gap",
                "title": f"Long-term goal gap: {goal.get('title')}",
                "summary": f"Long-term goal needs progress on {focus}: {goal.get('title')}",
                "goal_id": str(goal.get("id") or ""),
                "source_goal_id": str(goal.get("id") or ""),
                "source_type": "goal_registry",
                "source_kind": "goal_registry",
                "target_capability": focus,
                "evidence_tier": "T0",
                "confidence": 0.75,
                "impact": 0.8,
                "urgency": 0.45,
            }
        )
    return signals


def _derive_goal_candidates(
    goals: list[dict[str, Any]],
    *,
    capability_scores: dict[str, Any] | None = None,
    limit: int = 20,
) -> list[dict[str, Any]]:
    items = [_normalize_goal(goal, index=index) for index, goal in enumerate(goals) if isinstance(goal, dict)]
    candidates_limit = max_goals_limit(limit)
    if candidates_limit <= 0:
        return []
    if not items:
        return []
    scored: list[tuple[float, dict[str, Any]]] = []
    scores = capability_scores or {}
    for goal in items:
        weight = 0.0
        capabilities = _collect_capabilities(goal)
        if not capabilities:
            continue
        for capability in capabilities:
            score = _capability_score(scores, capability)
            weight += max(0.0, 1.0 - score)
        priority = 0.55 + (min(0.35, (weight / len(capabilities)) * 0.55) if capabilities else 0.0)
        scored.append((priority, {**goal, "priority": round(priority, 3)}))
    scored.sort(key=lambda item: (-item[0], str(item[1].get("title") or "")))
    return [item[1] for item in scored[: max(0, min(candidates_limit, len(scored)))]]


def max_goals_limit(value: int | None) -> int:
    try:
        normalized = int(value or 0)
    except (TypeError, ValueError):
        return 0
    return max(0, normalized)


def goal_to_learning_goal(goal: dict[str, Any], *, priority: float = 0.58) -> dict[str, Any]:
    subcaps = [str(item) for item in goal.get("sub_capabilities") or [] if str(item or "").strip()]
    capability = subcaps[0] if subcaps else "proactive.judgment"
    title = str(goal.get("title") or "Long-term learning goal")
    return {
        "goal_type": "long_term",
        "title": f"Advance {title}",
        "question": f"What asset would measurably advance the long-term goal: {title}?",
        "expected_artifact": "rule_sop_eval_or_skill",
        "success_criteria": "Produce an evidence-backed reusable asset and a replay/eval signal.",
        "authority_tier": "L1",
        "priority": priority,
        "target_capability": capability,
        "source_goal_id": str(goal.get("id") or ""),
        "source_type": "goal_registry",
        "source_record_ids": [],
        "evidence_tier": "T0",
        "semantic_key": stable_semantic_key("long_term_goal", goal.get("id"), title),
    }


def _normalize_goal(item: dict[str, Any], *, index: int) -> dict[str, Any]:
    title = str(item.get("title") or item.get("name") or f"Long-term goal {index + 1}")
    return {
        "id": str(item.get("id") or stable_semantic_key("long_term_goal", title)),
        "title": title,
        "sub_capabilities": [str(value) for value in item.get("sub_capabilities") or []],
        "milestones": [str(value) for value in item.get("milestones") or []],
        "evaluation_signals": [str(value) for value in item.get("evaluation_signals") or []],
    }


def _capability_score(scores: dict[str, Any], capability: str) -> float:
    value = scores.get(capability)
    if isinstance(value, dict):
        value = value.get("score")
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _collect_capabilities(goal: dict[str, Any]) -> list[str]:
    return [str(value) for value in goal.get("sub_capabilities") or [] if str(value or "").strip()]


def _weakest_subcap(
    subcaps: list[str],
    capability_scores: dict[str, Any] | None,
) -> str:
    if not subcaps:
        return ""
    if not capability_scores:
        return subcaps[0]
    ranked = [(1.0 - _capability_score(capability_scores, item), item) for item in subcaps]
    ranked.sort(key=lambda item: (-item[0], item[1]))
    return ranked[0][1]
