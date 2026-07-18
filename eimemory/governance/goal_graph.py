from __future__ import annotations

from dataclasses import asdict
from hashlib import sha256
import json
from typing import Any

from eimemory.core.clock import now_iso
from eimemory.governance.autonomy_goal_queue import build_autonomy_goal_queue
from eimemory.governance.episode_events import record_task_episode
from eimemory.governance.learning_state import append_learning_record_once, stable_semantic_key
from eimemory.governance.research_planner import create_research_task, plan_research_tasks
from eimemory.models.records import ScopeRef


CORE_GOAL_CAPABILITIES = [
    "memory.recall",
    "tool.routing",
    "knowledge.intake",
    "proactive.judgment",
    "safety.boundary",
]

LOOP_CONTRACT = {
    "invariant": "signal -> candidate -> gate -> apply -> observe -> score -> ledger -> active/rollback",
    "node_lifecycle": ["proposed", "gate_passed", "applied", "observed", "scored", "active", "rolled_back"],
    "complete_capability_requires": ["replay", "ledger", "observe", "rollback"],
}


def build_goal_graph_loop(
    runtime: Any,
    *,
    scope: dict[str, Any] | ScopeRef | None = None,
    max_goals: int = 3,
    persist: bool = False,
    capabilities: list[str] | None = None,
    loop_id: str = "goal_graph_1_6_9",
) -> dict[str, Any]:
    """Build a minimal executable goal tree for the autonomous evolution loop."""

    scope_ref = scope if isinstance(scope, ScopeRef) else ScopeRef.from_dict(scope)
    selected_limit = max(1, min(3, int(max_goals or 1)))
    target_capabilities = _dedupe(capabilities or CORE_GOAL_CAPABILITIES)
    queue = build_autonomy_goal_queue(
        runtime,
        scope=scope_ref,
        max_goals=selected_limit,
        persist=False,
        capabilities=target_capabilities,
    )
    goals = list(queue.get("goals") or [])[:selected_limit]
    generated_at = now_iso()
    nodes: list[dict[str, Any]] = []
    edges: list[dict[str, str]] = []
    episode_event_ids: list[str] = []
    research_task_ids: list[str] = []

    for goal in goals:
        capability = str(goal.get("capability") or goal.get("target_capability") or "proactive.judgment")
        root_id = _node_id(scope_ref, "root", capability, goal.get("title"))
        root = _node(
            node_id=root_id,
            node_type="root_goal",
            root_goal_id=root_id,
            parent_goal_id="",
            capability=capability,
            title=str(goal.get("title") or f"Improve {capability}"),
            success_criteria=f"{capability} has replay, ledger, observe, and rollback evidence.",
            evidence_refs=_signal_refs(goal),
        )
        nodes.append(root)

        sub_goal_id = _node_id(scope_ref, "sub", capability, "closed_loop_contract")
        sub_goal = _node(
            node_id=sub_goal_id,
            node_type="sub_goal",
            root_goal_id=root_id,
            parent_goal_id=root_id,
            capability=capability,
            title=f"Close loop for {capability}",
            success_criteria="Replay passes, safety gate holds, observation writes reward, rollback path is known.",
            evidence_refs=[],
        )
        nodes.append(sub_goal)
        edges.append(_edge(root_id, sub_goal_id, "decomposes_to"))

        task_goal = {
            "title": str(goal.get("title") or f"Improve {capability}"),
            "question": str(goal.get("explanation") or goal.get("question") or f"What closes {capability}?"),
            "target_capability": capability,
            "semantic_key": stable_semantic_key("goal_graph", capability, goal.get("title")),
        }
        for task in plan_research_tasks(task_goal, source_policy={"network_enabled": False}):
            task_payload = {
                **task,
                "target_capability": capability,
                "task_id": _node_id(scope_ref, "task", capability, task.get("task_type"), task.get("semantic_key")),
            }
            task_node_id = str(task_payload["task_id"])
            task_record_id = ""
            if persist:
                task_record_id = create_research_task(
                    runtime,
                    scope=scope_ref,
                    loop_id=loop_id,
                    goal_id=root_id,
                    task=task_payload,
                )
                research_task_ids.append(task_record_id)
                episode = record_task_episode(
                    runtime,
                    scope=scope_ref,
                    task=task_payload,
                    outcome={"ok": True, "status": "planned"},
                    decisions=[
                        {
                            "decision_id": f"route_{task_payload.get('task_type')}",
                            "reason": "Goal graph generated a task-level episode.",
                            "selected_route": str(task_payload.get("task_type") or ""),
                        }
                    ],
                    artifacts=[
                        {
                            "artifact_id": task_record_id or task_node_id,
                            "artifact_type": "research_task",
                            "record_id": task_record_id,
                        }
                    ],
                    failures=[],
                    source_record_ids=[task_record_id] if task_record_id else [],
                )
                episode_event_ids.append(str(episode.get("record_id") or ""))
            task_node = _node(
                node_id=task_node_id,
                node_type="task",
                root_goal_id=root_id,
                parent_goal_id=sub_goal_id,
                capability=capability,
                title=str(task_payload.get("title") or "Goal task"),
                success_criteria="Task episode exists and can feed replay/eval evidence.",
                evidence_refs=[task_record_id] if task_record_id else [],
                task_refs=[task_record_id] if task_record_id else [],
            )
            nodes.append(task_node)
            sub_goal["task_refs"].append(task_node_id)
            root["task_refs"].append(task_node_id)
            edges.append(_edge(sub_goal_id, task_node_id, "decomposes_to"))

    report: dict[str, Any] = {
        "ok": True,
        "report_type": "goal_graph_loop",
        "evidence_class": "structural",
        "generated_at": generated_at,
        "scope": asdict(scope_ref),
        "loop_id": loop_id,
        "loop_contract": dict(LOOP_CONTRACT),
        "root_goal_count": sum(1 for node in nodes if node["node_type"] == "root_goal"),
        "node_count": len(nodes),
        "task_count": sum(1 for node in nodes if node["node_type"] == "task"),
        "edge_count": len(edges),
        "episode_event_count": len(episode_event_ids),
        "episode_event_ids": episode_event_ids,
        "research_task_ids": research_task_ids,
        "nodes": nodes,
        "edges": edges,
        "persisted_record_id": "",
    }
    if persist:
        record = append_learning_record_once(
            runtime,
            kind="reflection",
            title="Goal graph closed-loop plan",
            summary=f"Goal graph built {report['root_goal_count']} root goal(s), {report['task_count']} task node(s).",
            scope=scope_ref,
            loop_id=loop_id,
            step_name="goal_graph",
            semantic_key=stable_semantic_key("goal_graph", loop_id, scope_ref, ",".join(target_capabilities)),
            authority_tier="L0",
            status="active",
            content=report,
            meta={
                "report_type": "goal_graph_loop",
                "evidence_class": "structural",
                "root_goal_count": report["root_goal_count"],
                "task_count": report["task_count"],
                "episode_event_count": report["episode_event_count"],
            },
            source="eimemory.goal_graph",
        )
        report["persisted_record_id"] = record.record_id
    return report


def observe_goal_graph_node(
    runtime: Any,
    *,
    graph: dict[str, Any],
    node_id: str,
    status: str,
    reward: float,
    ledger_refs: list[str] | None = None,
    rollback_refs: list[str] | None = None,
    persist: bool = False,
    scope: dict[str, Any] | ScopeRef | None = None,
    loop_id: str = "goal_graph_observe_1_6_9",
) -> dict[str, Any]:
    scope_ref = scope if isinstance(scope, ScopeRef) else ScopeRef.from_dict(scope)
    updated_graph = json.loads(json.dumps(graph, ensure_ascii=False, default=str))
    target = None
    for node in updated_graph.get("nodes") or []:
        if str(node.get("goal_id") or "") == str(node_id):
            target = node
            break
    if target is None:
        return {"ok": False, "error": "goal_graph_node_not_found", "node_id": str(node_id), "graph": updated_graph}
    target["status"] = _safe_status(status)
    target["reward"] = _clamp_reward(reward)
    target["ledger_refs"] = list(ledger_refs or [])
    target["rollback_refs"] = list(rollback_refs or [])
    target["observed_at"] = now_iso()
    target["closed_loop_stage"] = "active" if target["status"] == "active" else "observed"
    persisted_record_id = ""
    if persist:
        record = append_learning_record_once(
            runtime,
            kind="learning_eval",
            title=f"Goal graph observation: {node_id}",
            summary=f"{node_id} status={target['status']} reward={target['reward']}",
            scope=scope_ref,
            loop_id=loop_id,
            step_name="goal_graph_observation",
            semantic_key=stable_semantic_key("goal_graph_observation", node_id, target["status"], target["reward"]),
            authority_tier="L0",
            status="active",
            content={"graph": updated_graph, "observed_node": target},
            meta={
                "report_type": "goal_graph_observation",
                "node_id": str(node_id),
                "status": target["status"],
                "reward": target["reward"],
            },
            source="eimemory.goal_graph",
        )
        persisted_record_id = record.record_id
    return {"ok": True, "graph": updated_graph, "observed_node": target, "persisted_record_id": persisted_record_id}


def _node(
    *,
    node_id: str,
    node_type: str,
    root_goal_id: str,
    parent_goal_id: str,
    capability: str,
    title: str,
    success_criteria: str,
    evidence_refs: list[str] | None = None,
    task_refs: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "goal_id": node_id,
        "node_type": node_type,
        "parent_goal_id": parent_goal_id,
        "root_goal_id": root_goal_id,
        "target_capability": capability,
        "title": title,
        "status": "proposed",
        "success_criteria": success_criteria,
        "evidence_refs": list(evidence_refs or []),
        "task_refs": list(task_refs or []),
        "candidate_refs": [],
        "reward": 0.0,
        "ledger_refs": [],
        "rollback_refs": [],
        "closed_loop_contract": dict(LOOP_CONTRACT),
    }


def _edge(source: str, target: str, relation: str) -> dict[str, str]:
    return {"from": source, "to": target, "relation": relation}


def _signal_refs(goal: dict[str, Any]) -> list[str]:
    refs: list[str] = []
    for value in (goal.get("source_record_ids") or []):
        text = str(value or "").strip()
        if text:
            refs.append(text)
    counts = goal.get("source_signal_counts") if isinstance(goal.get("source_signal_counts"), dict) else {}
    if counts:
        refs.append(f"signals:{json.dumps(counts, sort_keys=True)}")
    return refs


def _node_id(scope: ScopeRef, *parts: Any) -> str:
    raw = json.dumps({"scope": asdict(scope), "parts": [str(part or "") for part in parts]}, sort_keys=True)
    return f"goal_{sha256(raw.encode('utf-8')).hexdigest()[:20]}"


def _dedupe(values: list[str]) -> list[str]:
    result: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if text and text not in result:
            result.append(text)
    return result or list(CORE_GOAL_CAPABILITIES)


def _safe_status(value: str) -> str:
    status = str(value or "observed").strip().lower()
    allowed = {"proposed", "gate_passed", "gate_failed", "applied", "observed", "scored", "active", "rolled_back", "quarantined"}
    return status if status in allowed else "observed"


def _clamp_reward(value: float) -> float:
    try:
        return round(max(-1.0, min(1.0, float(value))), 3)
    except (TypeError, ValueError):
        return 0.0
