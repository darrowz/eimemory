from __future__ import annotations

from collections import Counter
from dataclasses import asdict
from hashlib import sha256
import json
import re
from typing import Any

from eimemory.events import outcome_id
from eimemory.knowledge.evidence_contracts import versioned_record_ref
from eimemory.models.memory_edges import MemoryEdge
from eimemory.models.records import LinkRef, RecordEnvelope, ScopeRef


EVENT_MEMORY_PROJECTION = "event_memory"
EVENT_MEMORY_TYPE = "event_trace"


class _ProjectionBlocked(ValueError):
    pass


def _projection_failure(reason: str, *, stages: dict[str, Any] | None = None) -> dict[str, Any]:
    stages = stages or {name: {"status": "not_run", "id": ""} for name in ("policy_event", "policy_outcome", "event_memory")}
    committed = [name for name, value in stages.items() if value.get("persisted")]
    uncertain = any(value.get("status") == "uncertain" for value in stages.values())
    return {
        "ok": False, "projection": "sag_event_memory", "complete": False,
        "status": "partial" if committed or uncertain else "blocked", "error": reason,
        "stages": stages, "committed_stages": committed,
        "policy_event_id": str(stages["policy_event"].get("id") or ""),
        "event_outcome_id": str(stages["policy_outcome"].get("id") or ""),
        "event_record_id": str(stages["event_memory"].get("id") or ""),
    }


def _prepare_projection_source(
    runtime: Any, *, result: dict[str, Any], eval_result: dict[str, Any], scope: ScopeRef,
) -> RecordEnvelope:
    """Shared read-only gate, including the hook's pre-feedback boundary."""
    ids = {str(data.get("record_id") or "").strip() for data in (result, eval_result)} - {""}
    if not ids:
        raise _ProjectionBlocked("missing_source_record_id")
    if len(ids) != 1:
        raise _ProjectionBlocked("conflicting_source_record_ids")
    store = runtime.store
    if not callable(getattr(store, "get_by_id", None)) or not callable(getattr(store, "get_by_exact_ref", None)):
        raise _ProjectionBlocked("exact_source_lookup_unavailable")
    record = store.get_by_id(next(iter(ids)), scope=scope, exact_scope=True)
    if record is None or record.scope != scope or record.record_id not in ids:
        raise _ProjectionBlocked("source_record_not_found_in_exact_scope")
    if record.source_id != "default":
        raise _ProjectionBlocked("unsupported_source_partition")
    if record.status != "active":
        raise _ProjectionBlocked("unsafe_source_status")
    ref = versioned_record_ref(record)
    for data in (result, eval_result):
        if "source_record_ref" in data and data["source_record_ref"] != ref:
            raise _ProjectionBlocked("source_version_changed")
    _revalidate_projection_source(store, ref)
    for owner, method in ((runtime, "record_event"), (runtime, "record_outcome"), (store, "mutate_records_atomically")):
        if not callable(getattr(owner, method, None)):
            raise _ProjectionBlocked(f"{method}_unavailable")
    return record


def _revalidate_projection_source(store: Any, ref: dict[str, Any]) -> RecordEnvelope:
    record = store.get_by_exact_ref(ref["record_id"], scope=ScopeRef.from_dict(ref["scope"]), source_id=ref["source_id"])
    if record is None or record.status != "active" or versioned_record_ref(record) != ref:
        raise _ProjectionBlocked("source_version_changed")
    return record


def _existing_projection(store: Any, *, record_id: str, source_ref: dict[str, Any]) -> RecordEnvelope | None:
    scope = ScopeRef.from_dict(source_ref["scope"])
    record = store.get_by_id(record_id, scope=scope, exact_scope=True)
    if record is None:
        return None
    if (
        record.scope != scope or record.source_id != source_ref["source_id"]
        or record.kind != "memory" or record.source != "eimemory.event_graph" or record.status != "active"
        or record.meta.get("projection_type") != EVENT_MEMORY_PROJECTION
        or record.meta.get("source_record_ref") != source_ref
        or record.provenance.get("source_record_ref") != source_ref
        or record.content.get("source_record_ref") != source_ref
    ):
        raise _ProjectionBlocked("projection_identity_or_lineage_conflict")
    return record


def _policy_row(store: Any, *, table: str, payload: dict[str, Any], scope: ScopeRef) -> bool:
    # The caller selects one of two constant table names, never external SQL.
    if table not in {"events", "event_outcomes"}:
        raise _ProjectionBlocked("invalid_projection_stage")
    row = store.execute(f"SELECT * FROM {table} WHERE id=? LIMIT 1", (payload["id"],)).fetchone()
    if row is None:
        return False
    if any(str(row[key]) != getattr(scope, key) for key in ("tenant_id", "agent_id", "workspace_id", "user_id")):
        raise _ProjectionBlocked("policy_identity_conflict")
    stored = json.loads(str(row["payload_json"]))
    if any(stored.get(key) != value for key, value in payload.items()):
        raise _ProjectionBlocked("policy_identity_or_lineage_conflict")
    return True


def _validated_feedback_ref(store: Any, receipt: dict[str, Any], *, scope: ScopeRef, source_id: str) -> dict[str, Any]:
    """Validate an ingest-time version receipt; never mint lineage from a bare ID."""
    if not isinstance(receipt, dict):
        raise _ProjectionBlocked("feedback_receipt_invalid")
    if receipt.get("ok") is False:
        raise _ProjectionBlocked("feedback_memory_failed")
    record_id = receipt.get("record_id")
    if not isinstance(record_id, str) or not record_id.strip():
        raise _ProjectionBlocked("feedback_record_id_missing")
    ref = receipt.get("record_ref")
    if not isinstance(ref, dict) or ref.get("record_id") != record_id:
        raise _ProjectionBlocked("feedback_version_receipt_missing")
    if ref.get("scope") != asdict(scope) or ref.get("source_id") != source_id:
        raise _ProjectionBlocked("feedback_scope_or_source_conflict")
    record = store.get_by_exact_ref(record_id, scope=scope, source_id=source_id)
    if record is None or record.status != "active" or record.scope != scope or record.source_id != source_id:
        raise _ProjectionBlocked("feedback_record_not_authoritative")
    if versioned_record_ref(record) != ref:
        raise _ProjectionBlocked("feedback_version_changed")
    return dict(ref)


def _projection_plan(runtime: Any, *, result: dict[str, Any], eval_result: dict[str, Any], memory_update: dict[str, Any], scope: ScopeRef, feedback_required: bool = True) -> dict[str, Any]:
    source = _prepare_projection_source(runtime, result=result, eval_result=eval_result, scope=scope)
    ref = versioned_record_ref(source)
    payload = _source_payload(source)
    event_id = _stable_event_id(scope=scope, source_record_id=source.record_id)
    record_id = _stable_event_record_id(scope=scope, source_record_id=source.record_id)
    input_summary = _first_text(payload.get("input_summary"), source.summary, source.title)
    task_type = _first_text(payload.get("task_type"), source.meta.get("task_type"), eval_result.get("primary_label"), "experience")
    outcome_name = "good" if eval_result.get("ok") is True else "bad"
    label = _first_text(eval_result.get("primary_label"), eval_result.get("outcome_status"), outcome_name)
    tools = _string_list(payload.get("selected_tools")) or _string_list(payload.get("expected_tools"))
    actions = _action_path(payload.get("actions"))
    entities = _entities_from_event(input_summary=input_summary, task_type=task_type, tools=tools, action_path=actions, source_payload=payload)
    relations = _relations_from_entities(entities, event_id=event_id, primary_label=label)
    event = {
        "id": event_id, "timestamp": source.time.occurred_at or source.time.created_at,
        "source": "eimemory.closed_loop.event_graph", "user_phrase": input_summary,
        "event_type": task_type, "interpreted_intent": input_summary,
        "goal": f"Improve future handling for {task_type}", "confidence": 0.82,
        "tools": tools, "action_path": actions, "result": label, "evidence": [source.record_id],
        "lesson": f"Outcome {label} should be considered before repeating this task.",
        "next_policy": f"For similar {task_type} work, recall event memory {source.record_id} first.",
        "source_id": source.source_id, "source_record_ref": ref,
    }
    outcome = {
        "outcome": outcome_name, "reason": label, "correction_from_user": "",
        "policy_update": f"Recall event memory from {source.record_id} before choosing actions for this task type.",
        "source_record_id": source.record_id, "source_record_ref": ref, "source_id": source.source_id,
        "eval_result": dict(eval_result), "event_id": event_id,
    }
    outcome["id"] = outcome_id(event_id, outcome)
    # Only the two fixed internal callers choose the mode; no payload flag does.
    if feedback_required:
        feedback_ref = _validated_feedback_ref(runtime.store, memory_update, scope=scope, source_id=source.source_id)
    else:
        if memory_update:
            raise _ProjectionBlocked("event_only_feedback_conflict")
        feedback_ref = None
    plan = dict(source=source, source_ref=ref, event=event, outcome=outcome, record_id=record_id,
                input_summary=input_summary, task_type=task_type, outcome_name=outcome_name,
                primary_label=label, tools=tools, action_path=actions, entities=entities, relations=relations,
                feedback_ref=feedback_ref)
    plan["record"] = _event_memory_record(
        scope=scope, event_record_id=record_id, event_id=event_id, source_record_id=source.record_id,
        source_record=source, policy_outcome_id=outcome["id"], input_summary=input_summary,
        task_type=task_type, outcome_name=outcome_name, primary_label=label, tools=tools,
        action_path=actions, entities=entities, relations=relations,
    )
    def inspect(sqlite):
        _revalidate_plan(sqlite, plan)
        _policy_row(sqlite, table="events", payload=event, scope=scope)
        _policy_row(sqlite, table="event_outcomes", payload=outcome, scope=scope)
        return None, [], []
    runtime.store.mutate_records_atomically(inspect)
    return plan


def _revalidate_plan(store: Any, plan: dict[str, Any]) -> None:
    _revalidate_projection_source(store, plan["source_ref"])
    existing = _existing_projection(store, record_id=plan["record_id"], source_ref=plan["source_ref"])
    expected = plan["record"]
    if existing is not None and any(getattr(existing, key) != getattr(expected, key) for key in ("content", "title", "summary", "detail")):
        raise _ProjectionBlocked("projection_request_conflict")
    if plan["feedback_ref"] is not None:
        _revalidate_projection_source(store, plan["feedback_ref"])


def project_experience_event_memory(
    runtime: Any, *, result: dict[str, Any], eval_result: dict[str, Any],
    memory_update: dict[str, Any], scope: dict[str, Any] | ScopeRef | None,
) -> dict[str, Any]:
    """Closed-loop projection requires a persisted, version-bound feedback receipt."""
    return _project_event_memory(runtime, result=result, eval_result=eval_result,
        memory_update=memory_update, scope=scope, feedback_required=True)


def _project_event_memory_only(
    runtime: Any, *, result: dict[str, Any], eval_result: dict[str, Any],
    scope: dict[str, Any] | ScopeRef | None,
) -> dict[str, Any]:
    """Trusted internal event-only entry; it does not verify feedback or close a loop."""
    return _project_event_memory(runtime, result=result, eval_result=eval_result,
        memory_update={}, scope=scope, feedback_required=False)


def _project_event_memory(
    runtime: Any, *, result: dict[str, Any], eval_result: dict[str, Any],
    memory_update: dict[str, Any], scope: dict[str, Any] | ScopeRef | None, feedback_required: bool,
) -> dict[str, Any]:
    """Exact default-origin projection with explicit internal completion scope."""
    stages = {name: {"status": "not_run", "id": ""} for name in ("policy_event", "policy_outcome", "event_memory")}
    try:
        scope_ref = scope if isinstance(scope, ScopeRef) else ScopeRef.from_dict(scope)
        plan = _projection_plan(runtime, result=result, eval_result=eval_result, memory_update=memory_update, scope=scope_ref, feedback_required=feedback_required)
    except Exception as exc:
        return _projection_failure(str(exc) if isinstance(exc, _ProjectionBlocked) else type(exc).__name__, stages=stages)
    for stage, table, payload in (("policy_event", "events", plan["event"]), ("policy_outcome", "event_outcomes", plan["outcome"])):
        try:
            def prewrite(sqlite):
                _revalidate_plan(sqlite, plan)
                _policy_row(sqlite, table=table, payload=payload, scope=scope_ref)
                return None, [], []
            runtime.store.mutate_records_atomically(prewrite)
        except Exception as exc:
            return _projection_failure(str(exc) if isinstance(exc, _ProjectionBlocked) else type(exc).__name__, stages=stages)
        stages[stage] = {"status": "uncertain", "id": ""}
        error = ""
        try:
            returned = runtime.record_event(payload, scope=asdict(scope_ref)) if stage == "policy_event" else runtime.record_outcome(plan["event"]["id"], payload, scope=asdict(scope_ref))
            if not isinstance(returned, dict) or returned.get("ok") is False or returned.get("error") or returned.get("id") != payload["id"]:
                error = f"{stage}_writer_result_invalid"
        except Exception as exc:
            error = f"{stage}_write_failed:{type(exc).__name__}"
        try:
            def verify(sqlite):
                return _policy_row(sqlite, table=table, payload=payload, scope=scope_ref), [], []
            persisted = runtime.store.mutate_records_atomically(verify)
            stages[stage] = {"status": "persisted" if persisted else "failed", "persisted": bool(persisted), "id": payload["id"] if persisted else ""}
            if not persisted:
                error = error or f"{stage}_not_persisted"
        except Exception as exc:
            error = error or (str(exc) if isinstance(exc, _ProjectionBlocked) else f"{stage}_verification_failed:{type(exc).__name__}")
        if error:
            return _projection_failure(error, stages=stages)
    edges = _event_edges(scope=scope_ref, source_record_id=plan["source"].record_id, event_record_id=plan["record_id"],
                         memory_update_record_id=str((plan["feedback_ref"] or {}).get("record_id") or ""), entities=plan["entities"])
    try:
        def mutation(sqlite):
            _revalidate_plan(sqlite, plan)
            for table, payload in (("events", plan["event"]), ("event_outcomes", plan["outcome"])):
                if not _policy_row(sqlite, table=table, payload=payload, scope=scope_ref):
                    raise _ProjectionBlocked("policy_stage_no_longer_persisted")
            existing = _existing_projection(sqlite, record_id=plan["record_id"], source_ref=plan["source_ref"])
            record = plan["record"]
            if existing is not None and existing.content != record.content:
                raise _ProjectionBlocked("projection_request_conflict")
            if existing is None:
                sqlite.upsert(record, commit=False)
            return record.record_id, [] if existing is not None else [record], edges
        stages["event_memory"] = {"status": "uncertain", "id": ""}
        memory_error = ""
        try:
            runtime.store.mutate_records_atomically(mutation)
        except Exception as exc:
            memory_error = str(exc) if isinstance(exc, _ProjectionBlocked) else f"event_memory_failed:{type(exc).__name__}"
        def verify_memory(sqlite):
            record = _existing_projection(sqlite, record_id=plan["record_id"], source_ref=plan["source_ref"])
            if record is None or record.content.get("event_outcome_id") != plan["outcome"]["id"]:
                raise _ProjectionBlocked("event_memory_not_persisted")
            for edge in edges:
                row = sqlite.execute("SELECT from_id,to_id,evidence_id,tenant_id,agent_id,workspace_id,user_id FROM memory_edges WHERE edge_id=?", (edge.edge_id,)).fetchone()
                if row is None or any(row[key] != value for key, value in {"from_id":edge.from_id,"to_id":edge.to_id,"evidence_id":edge.evidence_id,**asdict(scope_ref)}.items()):
                    raise _ProjectionBlocked("event_edges_not_persisted")
            return record.record_id, [], []
        verified_id = runtime.store.mutate_records_atomically(verify_memory)
        stages["event_memory"] = {"status": "persisted", "persisted": True, "id": verified_id}
        if memory_error:
            return _projection_failure(memory_error, stages=stages)
        _revalidate_projection_source(runtime.store, plan["source_ref"])
    except Exception as exc:
        # The owner may fail after commit; an unknown commit is never reported as rollback.
        if isinstance(exc, _ProjectionBlocked) and str(exc) == "event_memory_not_persisted":
            stages["event_memory"] = {"status": "failed", "persisted": False, "id": ""}
        reason = memory_error or (str(exc) if isinstance(exc, _ProjectionBlocked) else f"event_memory_failed:{type(exc).__name__}")
        return _projection_failure(reason, stages=stages)
    return {
        "ok": True, "projection": "sag_event_memory", "status": "completed", "complete": True,
        "event_id": plan["event"]["id"], "event_record_id": verified_id,
        "source_record_id": plan["source"].record_id, "source_record_ref": plan["source_ref"],
        "policy_event_id": plan["event"]["id"], "event_outcome_id": plan["outcome"]["id"],
        "entities": plan["entities"], "relation_count": len(plan["relations"]),
        "edge_count": len(edges), "edge_counts": dict(sorted(Counter(e.edge_type for e in edges).items())),
        "stages": stages, "committed_stages": list(stages),
        "completion_scope": "event_projection_with_feedback" if feedback_required else "event_projection_only",
        "feedback": {"required": feedback_required, "verified": feedback_required,
                     "status": "verified" if feedback_required else "not_verified",
                     "record_ref": plan["feedback_ref"]},
    }


def _event_memory_record(
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
            "source_record_ref": versioned_record_ref(source_record),
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
        source_id=source_record.source_id,
        scope=source_record.scope,
        provenance={"source_record_ref": versioned_record_ref(source_record)},
        meta={
            "memory_type": EVENT_MEMORY_TYPE,
            "projection_type": EVENT_MEMORY_PROJECTION,
            "event_id": event_id,
            "outcome_id": source_record_id,
            "source_outcome_id": source_record_id,
            "event_outcome_id": policy_outcome_id,
            "source_record_id": source_record_id,
            "source_record_ref": versioned_record_ref(source_record),
            "task_type": task_type,
            "memory_layer": "L2-experience",
            "context_graph": "sag_event_graph",
            "confidence": 0.86,
            "force_capture": True,
        },
    )
    record.record_id = event_record_id
    return record


def _event_edges(
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
