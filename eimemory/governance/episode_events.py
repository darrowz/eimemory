from __future__ import annotations

from collections import Counter
from dataclasses import asdict
from hashlib import sha256
import json
import re
from typing import Any

from eimemory.models.memory_edges import MemoryEdge
from eimemory.models.records import LinkRef, RecordEnvelope, ScopeRef


TASK_EPISODE_MEMORY_TYPE = "task_episode"


def record_task_episode(
    runtime: Any,
    *,
    task: dict[str, Any],
    scope: dict[str, Any] | ScopeRef | None = None,
    outcome: dict[str, Any] | None = None,
    decisions: list[dict[str, Any]] | None = None,
    artifacts: list[dict[str, Any]] | None = None,
    failures: list[dict[str, Any]] | None = None,
    source_record_ids: list[str] | None = None,
) -> dict[str, Any]:
    """Persist a graph-first task episode with traceable decision/artifact/failure facets."""

    scope_ref = scope if isinstance(scope, ScopeRef) else ScopeRef.from_dict(scope)
    task_payload = dict(task or {})
    task_id = _first_text(task_payload.get("task_id"), task_payload.get("semantic_key"), task_payload.get("task_type"), task_payload.get("title"))
    if not task_id:
        task_id = _stable_hash(scope=scope_ref, payload=task_payload)[:16]
    event_id = f"episode_{_stable_hash(scope=scope_ref, payload={'task_id': task_id, 'task': task_payload})[:20]}"
    record_id = f"taskevent_{_stable_hash(scope=scope_ref, payload={'event_id': event_id})[:24]}"
    outcome_payload = dict(outcome or {"status": "planned", "ok": True})
    decision_items = list(decisions or _default_decisions(task_payload))
    artifact_items = list(artifacts or _default_artifacts(task_payload))
    failure_items = list(failures or _default_failures(outcome_payload))
    entities = _episode_entities(task_payload, decision_items, artifact_items, failure_items)
    episode = {
        "event_id": event_id,
        "task_id": task_id,
        "task": task_payload,
        "entities": entities,
        "decisions": decision_items,
        "artifacts": artifact_items,
        "failures": failure_items,
        "outcome": outcome_payload,
        "source_record_ids": list(source_record_ids or []),
    }
    text = _episode_text(episode)
    record = RecordEnvelope.create(
        kind="memory",
        title=f"Task episode: {task_payload.get('title') or task_payload.get('task_type') or task_id}",
        summary=text,
        detail=json.dumps(episode, ensure_ascii=False, sort_keys=True)[:1200],
        scope=scope_ref,
        source="eimemory.task_episode",
        status="active",
        content={
            "text": text,
            "memory_type": TASK_EPISODE_MEMORY_TYPE,
            "episode": episode,
        },
        tags=["memory-3.0", "task-episode", "graph-first"],
        links=[
            LinkRef(relation="episode_source", target_kind="research_task", target_id=str(source_id))
            for source_id in (source_record_ids or [])
            if str(source_id).strip()
        ],
        evidence=[str(source_id) for source_id in (source_record_ids or []) if str(source_id).strip()],
        meta={
            "report_type": "task_episode_event",
            "memory_type": TASK_EPISODE_MEMORY_TYPE,
            "event_id": event_id,
            "task_id": task_id,
            "target_capability": str(task_payload.get("target_capability") or task_payload.get("capability") or ""),
            "projection_type": "goal_graph_episode",
            "force_capture": True,
        },
    )
    record.record_id = record_id
    existing = runtime.store.get_by_id(record_id, scope=scope_ref)
    if existing is None:
        persisted = runtime.store.append(record)
    else:
        record.time.created_at = existing.time.created_at
        record.touch()
        persisted = runtime.store.rewrite(record)
    edges = _episode_edges(
        scope=scope_ref,
        record_id=persisted.record_id,
        task_id=task_id,
        event_id=event_id,
        entities=entities,
        decisions=decision_items,
        artifacts=artifact_items,
        failures=failure_items,
    )
    runtime.store.upsert_memory_edges(edges)
    counts = Counter(edge.edge_type for edge in edges)
    return {
        "ok": True,
        "event_id": event_id,
        "record_id": persisted.record_id,
        "task_id": task_id,
        "entities": entities,
        "decision_count": len(decision_items),
        "artifact_count": len(artifact_items),
        "failure_count": len(failure_items),
        "edge_count": len(edges),
        "edge_counts": dict(sorted(counts.items())),
    }


def _episode_edges(
    *,
    scope: ScopeRef,
    record_id: str,
    task_id: str,
    event_id: str,
    entities: list[str],
    decisions: list[dict[str, Any]],
    artifacts: list[dict[str, Any]],
    failures: list[dict[str, Any]],
) -> list[MemoryEdge]:
    edges = [
        MemoryEdge.create(
            from_id=task_id,
            to_id=record_id,
            edge_type="temporal",
            confidence=0.82,
            evidence_id=record_id,
            scope=scope,
            reason="task_episode_follows_planned_task",
            meta={"event_id": event_id},
        ),
        MemoryEdge.create(
            from_id=record_id,
            to_id=f"decision:{_stable_text(decisions)}",
            edge_type="causal",
            confidence=0.8,
            evidence_id=record_id,
            scope=scope,
            reason="episode_decision_trace",
            meta={"event_id": event_id, "decision_count": len(decisions)},
        ),
        MemoryEdge.create(
            from_id=record_id,
            to_id=f"artifact:{_stable_text(artifacts)}",
            edge_type="semantic",
            confidence=0.74,
            evidence_id=record_id,
            scope=scope,
            reason="episode_artifact_trace",
            meta={"event_id": event_id, "artifact_count": len(artifacts)},
        ),
    ]
    if failures:
        edges.append(
            MemoryEdge.create(
                from_id=record_id,
                to_id=f"failure:{_stable_text(failures)}",
                edge_type="causal",
                confidence=0.86,
                evidence_id=record_id,
                scope=scope,
                reason="episode_failure_or_risk_trace",
                meta={"event_id": event_id, "failure_count": len(failures)},
            )
        )
    for entity in entities[:12]:
        edges.append(
            MemoryEdge.create(
                from_id=record_id,
                to_id=f"entity:{entity}",
                edge_type="entity",
                confidence=0.78,
                evidence_id=record_id,
                scope=scope,
                reason="episode_entity_anchor",
                meta={"event_id": event_id, "entity": entity},
            )
        )
    return edges


def _default_decisions(task: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {
            "decision_id": f"decision_{_stable_text([task.get('task_type'), task.get('title')])[:12]}",
            "reason": "Task entered goal graph closure path.",
            "selected_route": str(task.get("task_type") or "goal_task"),
        }
    ]


def _default_artifacts(task: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {
            "artifact_id": str(task.get("semantic_key") or task.get("task_type") or "task_artifact"),
            "artifact_type": str(task.get("task_type") or "task"),
            "expected_evidence_tiers": list(task.get("expected_evidence_tiers") or []),
        }
    ]


def _default_failures(outcome: dict[str, Any]) -> list[dict[str, Any]]:
    if bool(outcome.get("ok", True)):
        return [{"failure_id": "none", "status": "none", "reason": "No failure observed in planning episode."}]
    return [
        {
            "failure_id": str(outcome.get("failure_id") or "episode_failure"),
            "status": str(outcome.get("status") or "failed"),
            "reason": str(outcome.get("reason") or outcome.get("error") or "Task episode outcome failed."),
        }
    ]


def _episode_entities(
    task: dict[str, Any],
    decisions: list[dict[str, Any]],
    artifacts: list[dict[str, Any]],
    failures: list[dict[str, Any]],
) -> list[str]:
    raw = [
        task.get("target_capability"),
        task.get("capability"),
        task.get("task_type"),
        task.get("title"),
        task.get("query"),
        json.dumps(decisions, ensure_ascii=False, sort_keys=True),
        json.dumps(artifacts, ensure_ascii=False, sort_keys=True),
        json.dumps(failures, ensure_ascii=False, sort_keys=True),
    ]
    entities: list[str] = []
    for token in re.findall(r"[A-Za-z][A-Za-z0-9_.:/-]{2,}|[0-9]{3,}", " ".join(str(item or "") for item in raw)):
        value = token.strip(".,:;()[]{}").lower()
        if value and value not in entities:
            entities.append(value)
    return entities or ["task_episode"]


def _episode_text(episode: dict[str, Any]) -> str:
    task = dict(episode.get("task") or {})
    return " | ".join(
        part
        for part in [
            f"Task episode {episode.get('event_id')}",
            f"task={task.get('title') or task.get('task_type') or episode.get('task_id')}",
            f"entities={', '.join(list(episode.get('entities') or [])[:8])}",
            f"artifacts={len(episode.get('artifacts') or [])}",
            f"failures={len(episode.get('failures') or [])}",
        ]
        if part
    )


def _first_text(*values: Any) -> str:
    for value in values:
        text = str(value or "").strip()
        if text:
            return text
    return ""


def _stable_hash(*, scope: ScopeRef, payload: Any) -> str:
    raw = json.dumps({"scope": asdict(scope), "payload": payload}, ensure_ascii=False, sort_keys=True, default=str)
    return sha256(raw.encode("utf-8")).hexdigest()


def _stable_text(value: Any) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    return sha256(raw.encode("utf-8")).hexdigest()[:24]
