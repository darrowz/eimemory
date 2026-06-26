from __future__ import annotations

from collections import Counter
from dataclasses import asdict
from hashlib import sha256
import json
import re
from typing import Any

from eimemory.models.memory_edges import MemoryEdge
from eimemory.models.records import LinkRef, RecordEnvelope, ScopeRef


EVENT_MEMORY_PROJECTION = "event_memory"
EVENT_MEMORY_TYPE = "event_trace"


def project_experience_event_memory(
    runtime: Any,
    *,
    result: dict[str, Any],
    eval_result: dict[str, Any],
    memory_update: dict[str, Any],
    scope: dict[str, Any] | ScopeRef | None,
) -> dict[str, Any]:
    """Project a closed-loop outcome into SAG-style event memory.

    The projection intentionally reuses existing eimemory stores: a durable
    memory record for recall, events/event_outcomes for policy search, and
    memory_edges for graph traversal.
    """

    scope_ref = scope if isinstance(scope, ScopeRef) else ScopeRef.from_dict(scope)
    source_record_id = str(eval_result.get("record_id") or result.get("record_id") or "").strip()
    if not source_record_id:
        return {"ok": False, "projection": "sag_event_memory", "error": "missing_source_record_id"}
    source_record = runtime.store.get_by_id(source_record_id, scope=scope_ref)
    if source_record is None:
        return {
            "ok": False,
            "projection": "sag_event_memory",
            "error": "source_record_not_found",
            "source_record_id": source_record_id,
        }

    source_payload = _source_payload(source_record)
    event_id = _stable_event_id(scope=scope_ref, source_record_id=source_record_id)
    event_record_id = _stable_event_record_id(scope=scope_ref, source_record_id=source_record_id)
    input_summary = _first_text(
        source_payload.get("input_summary"),
        source_record.summary,
        source_record.title,
    )
    task_type = _first_text(source_payload.get("task_type"), source_record.meta.get("task_type"), eval_result.get("primary_label"), "experience")
    outcome_name = "good" if bool(eval_result.get("ok", False)) else "bad"
    primary_label = _first_text(eval_result.get("primary_label"), eval_result.get("outcome_status"), outcome_name)
    tools = _string_list(source_payload.get("selected_tools")) or _string_list(source_payload.get("expected_tools"))
    action_path = _action_path(source_payload.get("actions"))
    entities = _entities_from_event(
        input_summary=input_summary,
        task_type=task_type,
        tools=tools,
        action_path=action_path,
        source_payload=source_payload,
    )
    relations = _relations_from_entities(entities, event_id=event_id, primary_label=primary_label)

    policy_event = _record_policy_event(
        runtime,
        scope=scope_ref,
        event_id=event_id,
        input_summary=input_summary,
        task_type=task_type,
        primary_label=primary_label,
        tools=tools,
        action_path=action_path,
        source_record_id=source_record_id,
        source_record=source_record,
    )
    policy_outcome = _record_policy_outcome(
        runtime,
        scope=scope_ref,
        event_id=event_id,
        outcome_name=outcome_name,
        primary_label=primary_label,
        source_record_id=source_record_id,
        eval_result=eval_result,
    )
    event_record = _upsert_event_memory_record(
        runtime,
        scope=scope_ref,
        event_record_id=event_record_id,
        event_id=event_id,
        source_record_id=source_record_id,
        source_record=source_record,
        policy_outcome_id=str(policy_outcome.get("id") or ""),
        input_summary=input_summary,
        task_type=task_type,
        outcome_name=outcome_name,
        primary_label=primary_label,
        tools=tools,
        action_path=action_path,
        entities=entities,
        relations=relations,
    )
    edges = _upsert_event_edges(
        runtime,
        scope=scope_ref,
        source_record_id=source_record_id,
        event_record_id=event_record.record_id,
        memory_update_record_id=str(memory_update.get("record_id") or ""),
        entities=entities,
    )
    edge_counts = Counter(edge.edge_type for edge in edges)
    return {
        "ok": True,
        "projection": "sag_event_memory",
        "event_id": event_id,
        "event_record_id": event_record.record_id,
        "source_record_id": source_record_id,
        "policy_event_id": str(policy_event.get("id") or ""),
        "event_outcome_id": str(policy_outcome.get("id") or ""),
        "entities": entities,
        "relation_count": len(relations),
        "edge_count": len(edges),
        "edge_counts": dict(sorted(edge_counts.items())),
    }


def _record_policy_event(
    runtime: Any,
    *,
    scope: ScopeRef,
    event_id: str,
    input_summary: str,
    task_type: str,
    primary_label: str,
    tools: list[str],
    action_path: list[str],
    source_record_id: str,
    source_record: RecordEnvelope,
) -> dict[str, Any]:
    recorder = getattr(runtime, "record_event", None)
    if not callable(recorder):
        return {"id": event_id, "ok": False, "error": "record_event_unavailable"}
    return dict(
        recorder(
            {
                "id": event_id,
                "timestamp": source_record.time.occurred_at or source_record.time.created_at,
                "source": "eimemory.closed_loop.event_graph",
                "user_phrase": input_summary,
                "event_type": task_type,
                "interpreted_intent": input_summary,
                "goal": f"Improve future handling for {task_type}",
                "confidence": 0.82,
                "tools": tools,
                "action_path": action_path,
                "result": primary_label,
                "evidence": [source_record_id],
                "lesson": f"Outcome {primary_label} should be considered before repeating this task.",
                "next_policy": f"For similar {task_type} work, recall event memory {source_record_id} first.",
            },
            scope=asdict(scope),
        )
    )


def _record_policy_outcome(
    runtime: Any,
    *,
    scope: ScopeRef,
    event_id: str,
    outcome_name: str,
    primary_label: str,
    source_record_id: str,
    eval_result: dict[str, Any],
) -> dict[str, Any]:
    recorder = getattr(runtime, "record_outcome", None)
    if not callable(recorder):
        return {"id": "", "ok": False, "error": "record_outcome_unavailable"}
    policy_update = (
        f"Recall event memory from {source_record_id} before choosing actions for this task type."
    )
    return dict(
        recorder(
            event_id,
            {
                "outcome": outcome_name,
                "reason": primary_label,
                "correction_from_user": "",
                "policy_update": policy_update,
                "source_record_id": source_record_id,
                "eval_result": dict(eval_result or {}),
            },
            scope=asdict(scope),
        )
    )


def _upsert_event_memory_record(
    runtime: Any,
    *,
    scope: ScopeRef,
    event_record_id: str,
    event_id: str,
    source_record_id: str,
    source_record: RecordEnvelope,
    policy_outcome_id: str,
    input_summary: str,
    task_type: str,
    outcome_name: str,
    primary_label: str,
    tools: list[str],
    action_path: list[str],
    entities: list[str],
    relations: list[dict[str, Any]],
) -> RecordEnvelope:
    text = _event_memory_text(
        input_summary=input_summary,
        task_type=task_type,
        outcome_name=outcome_name,
        primary_label=primary_label,
        tools=tools,
        action_path=action_path,
        entities=entities,
    )
    existing = runtime.store.get_by_id(event_record_id, scope=scope)
    record = RecordEnvelope.create(
        kind="memory",
        title=f"Event memory: {task_type}",
        summary=text,
        detail=json.dumps(
            {
                "event_id": event_id,
                "source_record_id": source_record_id,
                "entities": entities,
                "relations": relations,
            },
            ensure_ascii=False,
            sort_keys=True,
        )[:1200],
        content={
            "text": text,
            "memory_type": EVENT_MEMORY_TYPE,
            "projection_type": EVENT_MEMORY_PROJECTION,
            "event_id": event_id,
            "outcome_id": source_record_id,
            "source_outcome_id": source_record_id,
            "event_outcome_id": policy_outcome_id,
            "source_record_id": source_record_id,
            "task_type": task_type,
            "input_summary": input_summary,
            "outcome": outcome_name,
            "primary_label": primary_label,
            "entities": entities,
            "relations": relations,
            "tools": tools,
            "action_path": action_path,
        },
        tags=["memory-3.0", "sag-event", "event-trace", task_type],
        links=[
            LinkRef(relation="source_outcome_trace", target_kind=source_record.kind, target_id=source_record_id),
        ],
        evidence=[source_record_id],
        source="eimemory.event_graph",
        scope=scope,
        meta={
            "memory_type": EVENT_MEMORY_TYPE,
            "projection_type": EVENT_MEMORY_PROJECTION,
            "event_id": event_id,
            "outcome_id": source_record_id,
            "source_outcome_id": source_record_id,
            "event_outcome_id": policy_outcome_id,
            "source_record_id": source_record_id,
            "task_type": task_type,
            "memory_layer": "L2-experience",
            "context_graph": "sag_event_graph",
            "confidence": 0.86,
            "force_capture": True,
        },
    )
    record.record_id = event_record_id
    if existing is not None:
        record.time.created_at = existing.time.created_at
        record.touch()
        return runtime.store.rewrite(record)
    return runtime.store.append(record)


def _upsert_event_edges(
    runtime: Any,
    *,
    scope: ScopeRef,
    source_record_id: str,
    event_record_id: str,
    memory_update_record_id: str,
    entities: list[str],
) -> list[MemoryEdge]:
    edges = [
        MemoryEdge.create(
            from_id=source_record_id,
            to_id=event_record_id,
            edge_type="causal",
            confidence=0.9,
            evidence_id=source_record_id,
            scope=scope,
            reason="outcome_trace_projected_to_event_memory",
            meta={"projection": "sag_event_memory"},
        ),
        MemoryEdge.create(
            from_id=source_record_id,
            to_id=event_record_id,
            edge_type="temporal",
            confidence=0.72,
            evidence_id=source_record_id,
            scope=scope,
            reason="outcome_precedes_event_memory_projection",
            meta={"projection": "sag_event_memory"},
        ),
    ]
    if memory_update_record_id:
        edges.extend(
            [
                MemoryEdge.create(
                    from_id=event_record_id,
                    to_id=memory_update_record_id,
                    edge_type="entity",
                    confidence=0.82,
                    evidence_id=source_record_id,
                    scope=scope,
                    reason="event_feedback_shares_entities",
                    meta={"projection": "sag_event_memory", "entities": entities[:12]},
                ),
                MemoryEdge.create(
                    from_id=event_record_id,
                    to_id=memory_update_record_id,
                    edge_type="semantic",
                    confidence=0.76,
                    evidence_id=source_record_id,
                    scope=scope,
                    reason="event_feedback_same_closed_loop",
                    meta={"projection": "sag_event_memory"},
                ),
            ]
        )
    runtime.store.upsert_memory_edges(edges)
    return edges


def _source_payload(record: RecordEnvelope) -> dict[str, Any]:
    content = record.content if isinstance(record.content, dict) else {}
    payload = content.get("payload") if isinstance(content.get("payload"), dict) else {}
    return dict(payload)


def _event_memory_text(
    *,
    input_summary: str,
    task_type: str,
    outcome_name: str,
    primary_label: str,
    tools: list[str],
    action_path: list[str],
    entities: list[str],
) -> str:
    return " | ".join(
        part
        for part in [
            f"SAG event memory for {task_type}",
            f"task: {input_summary}",
            f"outcome: {outcome_name}",
            f"label: {primary_label}",
            f"tools: {', '.join(tools)}" if tools else "",
            f"actions: {', '.join(action_path)}" if action_path else "",
            f"entities: {', '.join(entities)}" if entities else "",
        ]
        if part
    )


def _entities_from_event(
    *,
    input_summary: str,
    task_type: str,
    tools: list[str],
    action_path: list[str],
    source_payload: dict[str, Any],
) -> list[str]:
    text = " ".join(
        [
            input_summary,
            task_type,
            " ".join(tools),
            " ".join(action_path),
            json.dumps(source_payload.get("outcome") or {}, ensure_ascii=False, sort_keys=True),
        ]
    )
    candidates = [
        task_type,
        *_string_list(source_payload.get("expected_tools")),
        *_string_list(source_payload.get("selected_tools")),
    ]
    candidates.extend(
        token.strip(".,:;()[]{}").lower()
        for token in re.findall(r"[A-Za-z][A-Za-z0-9_.:/-]{2,}|[0-9]{3,}", text)
    )
    return list(dict.fromkeys(item for item in candidates if item))


def _relations_from_entities(entities: list[str], *, event_id: str, primary_label: str) -> list[dict[str, Any]]:
    return [
        {
            "event_id": event_id,
            "source_entity": entity,
            "target_entity": event_id,
            "relation": "participates_in_event",
            "confidence": 0.74 if primary_label else 0.62,
        }
        for entity in entities[:20]
    ]


def _action_path(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    actions: list[str] = []
    for item in value:
        if isinstance(item, dict):
            actions.append(_first_text(item.get("type"), item.get("name"), item.get("action")))
        else:
            actions.append(str(item or "").strip())
    return [item for item in actions if item]


def _string_list(value: Any) -> list[str]:
    if isinstance(value, (list, tuple, set)):
        return [str(item).strip() for item in value if str(item).strip()]
    text = str(value or "").strip()
    return [text] if text else []


def _first_text(*values: Any) -> str:
    for value in values:
        text = str(value or "").strip()
        if text:
            return text
    return ""


def _stable_event_id(*, scope: ScopeRef, source_record_id: str) -> str:
    return "evt_sag_" + _stable_hash(scope=scope, source_record_id=source_record_id)[:16]


def _stable_event_record_id(*, scope: ScopeRef, source_record_id: str) -> str:
    return "eventmem_" + _stable_hash(scope=scope, source_record_id=source_record_id)[:24]


def _stable_hash(*, scope: ScopeRef, source_record_id: str) -> str:
    payload = {
        "scope": asdict(scope),
        "source_record_id": source_record_id,
        "projection": "sag_event_memory",
    }
    return sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()
