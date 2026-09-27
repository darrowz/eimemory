from __future__ import annotations

from typing import Any

from eimemory.governance.capability_hypotheses import explicit_hypothesis_reference
from eimemory.governance.learning_state import append_learning_record_once, stable_semantic_key
from eimemory.models.records import ScopeRef


def plan_research_tasks(goal: dict[str, Any], source_policy: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    policy = dict(source_policy or {})
    network_enabled = bool(policy.get("network_enabled") or policy.get("allow_network"))
    source_urls = _string_list(policy.get("urls") or policy.get("source_urls"))
    title = str(goal.get("title") or "")
    question = str(goal.get("question") or "")
    target = str(goal.get("target_capability") or "")
    text = f"{title} {question} {target}".lower()
    tasks: list[dict[str, Any]] = [
        _task("local_history_review", goal, expected_tiers=["T0", "T2"], max_seconds=20),
        _task("benchmark_review", goal, expected_tiers=["T1"], max_seconds=20),
    ]
    if any(term in text for term in ("code", "test", "repo", "ci")):
        tasks.append(_task("repo_scan", goal, expected_tiers=["T2"], max_seconds=30))
    if any(term in text for term in ("tool", "routing", "memory", "recall")):
        tasks.append(_task("tool_comparison", goal, expected_tiers=["T2"], max_seconds=20))
    if network_enabled:
        task = _task("docs_read", goal, expected_tiers=["T3"], max_seconds=45, network=True)
        if source_urls:
            task["source_urls"] = source_urls[:5]
        tasks.append(task)
    return _dedupe_tasks(tasks)


def create_research_task(
    runtime: Any,
    *,
    scope: dict[str, Any] | ScopeRef | None,
    loop_id: str,
    goal_id: str,
    task: dict[str, Any],
) -> str:
    semantic_key = str(task.get("semantic_key") or stable_semantic_key("research_task", goal_id, task.get("task_type"), task.get("query")))
    hypothesis_reference = explicit_hypothesis_reference(task)
    hypothesis_meta = {key: value for key, value in hypothesis_reference.items() if key != "capability_hypothesis_id"}
    record = append_learning_record_once(
        runtime,
        kind="research_task",
        title=str(task.get("title") or f"Research task: {task.get('task_type') or 'local'}"),
        summary=str(task.get("query") or ""),
        scope=scope,
        loop_id=loop_id,
        step_name="research_task",
        semantic_key=semantic_key,
        authority_tier=str(task.get("authority_tier") or "L0"),
        status="candidate",
        content={
            "goal_id": goal_id,
            "task": {**task, "semantic_key": semantic_key},
            "capability_hypothesis": hypothesis_reference,
        },
        meta={
            "goal_id": goal_id,
            "task_type": task.get("task_type"),
            "network": bool(task.get("network")),
            "capability_hypothesis_id": hypothesis_reference.get("capability_hypothesis_id", ""),
            **hypothesis_meta,
        },
    )
    return record.record_id


def create_research_note(
    runtime: Any,
    *,
    scope: dict[str, Any] | ScopeRef | None,
    loop_id: str,
    learning_goal_id: str,
    title: str,
    summary: str,
    evidence: list[dict[str, Any]],
    applicability_score: float = 0.0,
    risk_tier: str = "L0",
    capability_hypothesis: dict[str, Any] | None = None,
) -> str:
    if not evidence:
        raise ValueError("research note requires evidence")
    if all(str(item.get("tier") or "").upper() == "T6" for item in evidence):
        raise ValueError("LLM synthesis cannot be the only evidence")
    hypothesis_reference = explicit_hypothesis_reference(capability_hypothesis)
    semantic_key = stable_semantic_key(
        "research_note",
        learning_goal_id,
        title,
        summary,
        hypothesis_reference.get("capability_hypothesis_id", ""),
    )
    record = append_learning_record_once(
        runtime,
        kind="research_note",
        title=title,
        summary=summary,
        scope=scope,
        loop_id=loop_id,
        step_name="research_note",
        semantic_key=semantic_key,
        authority_tier=risk_tier,
        status="active",
        content={
            "learning_goal_id": learning_goal_id,
            "evidence": evidence,
            "applicability_score": applicability_score,
            "capability_hypothesis": hypothesis_reference,
        },
        meta={
            "learning_goal_id": learning_goal_id,
            "risk_tier": risk_tier,
            "applicability_score": applicability_score,
            "capability_hypothesis_id": hypothesis_reference.get("capability_hypothesis_id", ""),
        },
        evidence=[str(item.get("ref") or item.get("url") or item.get("path") or item.get("summary") or "") for item in evidence],
    )
    return record.record_id


def _task(
    task_type: str,
    goal: dict[str, Any],
    *,
    expected_tiers: list[str],
    max_seconds: int,
    network: bool = False,
) -> dict[str, Any]:
    title = str(goal.get("title") or "learning goal")
    query = str(goal.get("question") or title)
    hypothesis_reference = explicit_hypothesis_reference(goal)
    semantic_key = stable_semantic_key(
        "research_task",
        task_type,
        title,
        query,
        hypothesis_reference.get("capability_hypothesis_id", ""),
    )
    return {
        "task_type": task_type,
        "title": f"{task_type}: {title}",
        "query": query,
        "goal_semantic_key": goal.get("semantic_key"),
        "expected_evidence_tiers": expected_tiers,
        "max_seconds": max_seconds,
        "network": bool(network),
        "authority_tier": "L0" if not network else "L2",
        "semantic_key": semantic_key,
        "capability_hypothesis": hypothesis_reference,
    }


def _dedupe_tasks(tasks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    deduped: list[dict[str, Any]] = []
    for task in tasks:
        key = str(task.get("semantic_key") or "")
        if key in seen:
            continue
        seen.add(key)
        deduped.append(task)
    return deduped


def _string_list(value: Any) -> list[str]:
    if isinstance(value, str):
        items = [value]
    elif isinstance(value, (list, tuple, set)):
        items = list(value)
    else:
        items = []
    deduped: list[str] = []
    seen: set[str] = set()
    for item in items:
        text = str(item or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        deduped.append(text)
    return deduped
