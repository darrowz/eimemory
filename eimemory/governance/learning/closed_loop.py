from __future__ import annotations

from dataclasses import asdict, is_dataclass
from hashlib import sha256
import json
from types import SimpleNamespace
from typing import Any

from eimemory.evaluation.reward import RewardEngine
from eimemory.governance.learning.event_graph import (
    _prepare_projection_source, _projection_plan, _projection_failure,
    _revalidate_projection_source, _validated_feedback_ref, _ProjectionBlocked, project_experience_event_memory,
)
from eimemory.knowledge.evidence_contracts import versioned_record_ref
from eimemory.governance.learning.rl_policy import RLPolicy
from eimemory.models.records import RecordEnvelope, ScopeRef
from eimemory.storage.replay_buffer import ReplayBuffer, action_identity


SUCCESS_LABELS = {"success", "good", "passed", "pass", "ok"}


def evaluate_result(runtime: Any, result: dict[str, Any], *, scope: dict[str, Any] | ScopeRef | None = None, source_record: Any | None = None) -> dict[str, Any]:
    payload = dict(result or {})
    record = source_record if source_record is not None else _record_for_result(runtime, payload, scope=scope)
    meta = _mapping(getattr(record, "meta", {}) if record is not None else {})
    content = _mapping(getattr(record, "content", {}) if record is not None else {})
    diagnosis = _mapping(content.get("diagnosis"))
    primary_label = _first_text(
        meta.get("primary_label"),
        diagnosis.get("primary_label"),
        "success" if payload.get("ok") is True else "",
    )
    outcome_status = _first_text(
        meta.get("outcome_status"),
        _nested(content, "payload", "outcome", "status"),
        _nested(content, "payload", "outcome"),
        payload.get("status"),
    )
    signals = _string_list(meta.get("signals") or meta.get("diagnosis_signals") or diagnosis.get("signals"))
    cost = _float(_nested(content, "payload", "cost") or content.get("cost"))
    result_ok = payload.get("ok")
    if primary_label:
        ok = primary_label.lower() in SUCCESS_LABELS
    elif outcome_status:
        ok = outcome_status.lower() in SUCCESS_LABELS
    else:
        ok = bool(result_ok is not False)
    return {
        "ok": ok,
        "record_id": str(payload.get("record_id") or ""),
        "result_ok": result_ok is not False,
        "primary_label": primary_label or ("success" if ok else "unknown_failure"),
        "outcome_status": outcome_status,
        "signals": signals,
        "confidence": _float(diagnosis.get("confidence")),
        "cost": cost,
        "source": "closed_loop.evaluate",
    }


def post_experience_hook(runtime: Any, result: dict[str, Any], scope: dict[str, Any] | ScopeRef | None) -> dict[str, Any]:
    # This guard precedes every derived write, including feedback ingestion.
    # The already-authoritative raw outcome is retained when projection is blocked.
    try:
        scope_ref = scope if isinstance(scope, ScopeRef) else ScopeRef.from_dict(scope)
        source = _prepare_projection_source(runtime, result=result, eval_result={}, scope=scope_ref)
        source_ref = versioned_record_ref(source)
        result = {**result, "source_record_ref": source_ref}
        eval_result = evaluate_result(runtime, result, scope=scope, source_record=source)
        eval_result["source_record_ref"] = source_ref
        _projection_plan(runtime, result=result, eval_result=eval_result, memory_update={}, scope=scope_ref, feedback_required=False)
        _revalidate_projection_source(runtime.store, source_ref)
    except Exception as exc:
        reason = str(exc) if isinstance(exc, _ProjectionBlocked) else type(exc).__name__
        return _stopped_experience_projection(reason)
    memory_update = _ingest_feedback_memory(
        runtime,
        scope=scope,
        title="auto-feedback",
        memory_type="reflection",
        source="loop",
        evaluation=eval_result,
        require_record_ref=True,
    )
    try:
        _validated_feedback_ref(runtime.store, memory_update, scope=scope_ref, source_id=source.source_id)
    except Exception as exc:
        reason = str(exc) if isinstance(exc, _ProjectionBlocked) else type(exc).__name__
        # A successful-looking return is not proof that nothing was committed.
        unverified = {"ok": False, "status": "commit_uncertain", "record_id": "", "error": reason}
        return _stopped_experience_projection(reason, evaluation=eval_result, memory=unverified)
    event_graph = _safe_event_graph_projection(
        runtime,
        result=result,
        eval_result=eval_result,
        memory_update=memory_update,
        scope=scope,
    )
    if event_graph.get("ok") is not True:
        return _stopped_experience_projection(str(event_graph.get("error") or "event_projection_incomplete"),
            evaluation=eval_result, memory=memory_update, event_graph=event_graph)
    try:
        _revalidate_projection_source(runtime.store, source_ref)
    except Exception as exc:
        return _stopped_experience_projection(str(exc), evaluation=eval_result, memory=memory_update, event_graph=event_graph)
    learning_signal = _safe_generate_learning(runtime, scope=scope)
    try:
        _revalidate_projection_source(runtime.store, source_ref)
    except Exception as exc:
        return _stopped_experience_projection(str(exc), evaluation=eval_result, memory=memory_update,
            event_graph=event_graph, learning=learning_signal)
    rl_signal = _safe_rl_update(
        runtime,
        scope=scope,
        state={
            "source": "experience.outcome",
            "record_id": str(eval_result.get("record_id") or ""),
            "primary_label": str(eval_result.get("primary_label") or ""),
            "signals": list(eval_result.get("signals") or []),
        },
        action={
            "id": str(eval_result.get("primary_label") or "unknown"),
            "type": "experience_feedback",
            "value": 0.0,
        },
        eval_result=eval_result,
        outcome={
            "success": bool(eval_result.get("ok", False)),
            "status": eval_result.get("outcome_status"),
            "cost": eval_result.get("cost"),
        },
        next_state={
            "memory_record_id": str(memory_update.get("record_id") or ""),
            "learning_ok": bool(learning_signal.get("ok", False)),
        },
        source_record_id=str(eval_result.get("record_id") or ""),
    )
    complete = learning_signal.get("ok") is not False and rl_signal.get("ok") is True
    return {
        "ok": complete, "complete": complete, "status": "completed" if complete else "partial",
        "eval": eval_result,
        "memory": memory_update,
        "event_graph": event_graph,
        "learning": learning_signal,
        "rl": rl_signal,
    }


def _stopped_experience_projection(reason: str, *, evaluation: dict | None = None,
    memory: dict | None = None, event_graph: dict | None = None, learning: dict | None = None) -> dict:
    not_run = {"ok": False, "status": "not_run"}
    derived = bool(memory and memory.get("record_id")) or bool(event_graph and event_graph.get("committed_stages"))
    uncertain = bool(memory and memory.get("ok") is False and memory.get("error") and not memory.get("record_id"))
    return {
        "ok": False, "complete": False, "status": "partial" if derived or uncertain else "blocked", "error": reason,
        "uncertain_stages": ["memory"] if uncertain else [],
        "eval": evaluation or dict(not_run), "memory": memory or dict(not_run),
        "event_graph": event_graph or _projection_failure(reason),
        "learning": learning or dict(not_run), "rl": dict(not_run),
    }


def lightweight_outcome_learning_hook(
    runtime: Any,
    result: dict[str, Any],
    scope: dict[str, Any] | ScopeRef | None,
) -> dict[str, Any]:
    """Feed reward→RL + reflection memory for RPC/OpenClaw outcomes (no learning thoughts)."""
    eval_result = evaluate_result(runtime, result, scope=scope)
    memory_update = _ingest_feedback_memory(
        runtime,
        scope=scope,
        title="auto-feedback",
        memory_type="reflection",
        source="loop",
        evaluation=eval_result,
    )
    rl_signal = _safe_rl_update(
        runtime,
        scope=scope,
        state={
            "source": "experience.outcome",
            "record_id": str(eval_result.get("record_id") or result.get("id") or result.get("record_id") or ""),
            "primary_label": str(eval_result.get("primary_label") or ""),
            "signals": list(eval_result.get("signals") or []),
        },
        action={
            "id": str(eval_result.get("primary_label") or "unknown"),
            "type": "experience_feedback",
            "value": 0.0,
        },
        eval_result=eval_result,
        outcome={
            "success": bool(eval_result.get("ok", False)),
            "status": eval_result.get("outcome_status") or result.get("outcome"),
            "cost": eval_result.get("cost"),
        },
        next_state={
            "memory_record_id": str(memory_update.get("record_id") or ""),
            "learning_ok": False,
        },
        source_record_id=str(
            eval_result.get("record_id") or result.get("id") or result.get("record_id") or ""
        ),
    )
    return {
        "eval": eval_result,
        "memory": memory_update,
        "rl": rl_signal,
        "mode": "lightweight",
    }


def autonomy_cycle(
    runtime: Any,
    scope: dict[str, Any] | ScopeRef | None,
    **kwargs: Any,
) -> dict[str, Any]:
    if kwargs.get("dry_run"):
        # Do not enter the controller/hook pipeline for a pure preview.  This
        # also keeps apply/network/legacy/smoke flags from enabling side effects.
        from eimemory.governance.learning.autonomous_learning import _run_autonomous_learning_dry_run

        scope_ref = scope if isinstance(scope, ScopeRef) else ScopeRef.from_dict(scope)
        preview = _run_autonomous_learning_dry_run(
            None,
            scope=scope_ref,
            apply=bool(kwargs.get("apply", False)),
            full=bool(kwargs.get("full", True)),
            max_goals=kwargs.get("max_goals", 3),
            allow_network=kwargs.get("allow_network"),
            profile_key=kwargs.get("profile_key", ""),
            capability_scope=kwargs.get("capability_scope", "global"),
            runtime_scope=kwargs.get("runtime_scope"),
            at_time=kwargs.get("at_time", ""),
            legacy_compatibility=bool(kwargs.get("legacy_compatibility", False)),
        )
        return _autonomy_preview_envelope(preview)
    cycle_result = runtime.run_autonomy_cycle(scope=scope, **kwargs)
    if isinstance(cycle_result, dict) and cycle_result.get("dry_run"):
        return _autonomy_preview_envelope(cycle_result)
    feedback = evaluate_result(runtime, dict(cycle_result or {}), scope=scope)
    memory_update = _ingest_feedback_memory(
        runtime,
        scope=scope,
        title="autonomy-loop",
        memory_type="autonomy_feedback",
        source="system",
        evaluation=feedback,
        cycle=cycle_result,
    )
    rl_signal = _safe_rl_update(
        runtime,
        scope=scope,
        state={
            "source": "autonomy.cycle",
            "bounded_max_goals": cycle_result.get("bounded_max_goals") if isinstance(cycle_result, dict) else None,
            "policy_decision": _mapping(cycle_result.get("policy_decision")) if isinstance(cycle_result, dict) else {},
        },
        action=_autonomy_action(cycle_result),
        eval_result=feedback,
        outcome={
            "success": bool(cycle_result.get("ok", False)) if isinstance(cycle_result, dict) else False,
            "status": "success" if isinstance(cycle_result, dict) and cycle_result.get("ok") else "failed",
        },
        next_state={
            "memory_record_id": str(memory_update.get("record_id") or ""),
            "feedback_ok": bool(feedback.get("ok", False)),
        },
        source_record_id=str(feedback.get("record_id") or ""),
    )
    payload = dict(cycle_result or {}) if isinstance(cycle_result, dict) else {}
    return {
        **payload,
        "ok": bool(cycle_result.get("ok", False)) if isinstance(cycle_result, dict) else False,
        "cycle": cycle_result,
        "feedback": feedback,
        "memory": memory_update,
        "rl": rl_signal,
    }



def _autonomy_action(cycle_result: Any) -> dict[str, Any]:
    if isinstance(cycle_result, dict):
        decision = _mapping(cycle_result.get("policy_decision"))
        if decision.get("id"):
            return {
                "id": str(decision.get("id") or "run_autonomy_cycle"),
                "type": str(decision.get("type") or "autonomy_cycle"),
                "value": _float(decision.get("policy_value")),
                "selected_by": str(decision.get("selected_by") or ""),
            }
    return {
        "id": "run_autonomy_cycle",
        "type": "autonomy_cycle",
        "value": 0.0,
    }


def _record_for_result(runtime: Any, result: dict[str, Any], *, scope: dict[str, Any] | ScopeRef | None) -> Any:
    record_id = str(result.get("record_id") or "").strip()
    getter = getattr(getattr(runtime, "store", None), "get_by_id", None)
    if not record_id or not callable(getter):
        return None
    try:
        return getter(record_id, scope=scope)
    except TypeError:
        return getter(record_id)
    except Exception:
        return None


def _ingest_feedback_memory(
    runtime: Any,
    *,
    scope: dict[str, Any] | ScopeRef | None,
    title: str,
    memory_type: str,
    source: str,
    evaluation: dict[str, Any],
    cycle: dict[str, Any] | None = None,
    require_record_ref: bool = False,
) -> dict[str, Any]:
    text_payload = {
        "evaluation": evaluation,
    }
    if cycle is not None:
        text_payload["cycle"] = cycle
    try:
        record = runtime.memory.ingest(
            text=json.dumps(_json_safe(text_payload), ensure_ascii=False, sort_keys=True),
            memory_type=memory_type,
            title=title,
            scope=_scope_dict(scope),
            source=source,
            force_capture=True,
            meta={
                "report_type": "closed_loop_feedback",
                "closed_loop_stage": title,
                "evaluation_ok": bool(evaluation.get("ok", False)),
                "primary_label": str(evaluation.get("primary_label") or ""),
            },
            content=text_payload,
        )
    except Exception as exc:
        return {"ok": False, "error": exc.__class__.__name__, "detail": str(exc)}
    if require_record_ref:
        if not isinstance(record, RecordEnvelope) or not record.record_id:
            return {"ok": False, "status": "commit_uncertain", "record_id": "", "error": "feedback_version_receipt_unavailable"}
        try:
            receipt = record.to_dict()
            receipt["record_ref"] = versioned_record_ref(record)
            return receipt
        except Exception as exc:
            return {"ok": False, "status": "commit_uncertain", "record_id": "", "error": type(exc).__name__}
    return record.to_dict() if hasattr(record, "to_dict") else {"record_id": getattr(record, "record_id", "")}


def _safe_generate_learning(runtime: Any, *, scope: dict[str, Any] | ScopeRef | None) -> dict[str, Any]:
    generator = getattr(runtime, "generate_learning_thoughts", None)
    if not callable(generator):
        return {"ok": False, "error": "learning_generator_unavailable"}
    try:
        return dict(generator(scope=_scope_dict(scope), persist=True, max_items=3))
    except Exception as exc:
        return {"ok": False, "error": exc.__class__.__name__, "detail": str(exc)}


def _safe_event_graph_projection(
    runtime: Any,
    *,
    result: dict[str, Any],
    eval_result: dict[str, Any],
    memory_update: dict[str, Any],
    scope: dict[str, Any] | ScopeRef | None,
) -> dict[str, Any]:
    try:
        return project_experience_event_memory(
            runtime,
            result=result,
            eval_result=eval_result,
            memory_update=memory_update,
            scope=scope,
        )
    except Exception as exc:
        return {
            "ok": False,
            "projection": "sag_event_memory",
            "error": exc.__class__.__name__,
            "detail": str(exc),
        }


def _safe_rl_update(
    runtime: Any,
    *,
    scope: dict[str, Any] | ScopeRef | None,
    state: dict[str, Any],
    action: dict[str, Any],
    eval_result: dict[str, Any],
    outcome: dict[str, Any],
    next_state: dict[str, Any],
    source_record_id: str,
) -> dict[str, Any]:
    try:
        reward = RewardEngine().compute(experience=state, eval_result=eval_result, outcome=outcome)
        if source_record_id:
            return _atomic_rl_update(
                runtime, scope=scope, state=state, action=action, reward=reward,
                next_state=next_state, source_record_id=source_record_id,
            )
        transition = ReplayBuffer(runtime.store).add_transition(
            state=state,
            action=action,
            reward=reward,
            next_state=next_state,
            scope=scope,
            source_record_id=source_record_id,
        )
        policy_update = RLPolicy(runtime.store).update(
            state=state,
            action=action,
            reward=reward,
            scope=scope,
        )
    except Exception as exc:
        return {"ok": False, "error": exc.__class__.__name__, "detail": str(exc)}
    return {
        "ok": True,
        "reward": reward,
        "transition_record_id": transition.record_id,
        "policy_update": policy_update,
    }


def _atomic_rl_update(
    runtime: Any, *, scope: dict[str, Any] | ScopeRef | None,
    state: dict[str, Any], action: dict[str, Any], reward: dict[str, Any],
    next_state: dict[str, Any], source_record_id: str,
) -> dict[str, Any]:
    scope_ref = scope if isinstance(scope, ScopeRef) else ScopeRef.from_dict(scope)
    identity = sha256(json.dumps(
        [asdict(scope_ref), source_record_id, action_identity(action)], sort_keys=True,
    ).encode("utf-8")).hexdigest()
    transition_id = f"rl_transition_{identity}"

    def mutation(sqlite):
        existing = sqlite.get_by_id(transition_id, scope=scope_ref)
        if existing is not None:
            if existing.scope != scope_ref or existing.kind != "rl_transition":
                raise ValueError("feedback transition identity conflict")
            receipt = existing.content.get("policy_update")
            if not isinstance(receipt, dict):
                raise ValueError("feedback transition receipt missing")
            return {"ok": True, "reward": existing.content["reward"],
                    "transition_record_id": existing.record_id,
                    "policy_update": receipt, "idempotent": True}, [], []

        changed_records = []

        def append(record):
            record.record_id = f"{record.kind}_{identity}"
            sqlite.upsert(record, commit=False)
            changed_records.append(record)
            return record

        # Reuse the existing record builders with transaction-local writes;
        # neither component may commit independently of the other.
        writer = SimpleNamespace(append=append, list_records=runtime.store.list_records)
        transition = ReplayBuffer(writer).add_transition(
            state=state, action=action, reward=reward, next_state=next_state,
            scope=scope_ref, source_record_id=source_record_id,
        )
        policy_update = RLPolicy(writer).update(state=state, action=action, reward=reward, scope=scope_ref)
        transition.content["policy_update"] = policy_update
        sqlite.rewrite(transition, commit=False)
        return {"ok": True, "reward": reward, "transition_record_id": transition.record_id,
                "policy_update": policy_update, "idempotent": False}, changed_records, []

    return runtime.store.mutate_records_atomically(mutation)


def _scope_dict(scope: dict[str, Any] | ScopeRef | None) -> dict[str, Any]:
    if isinstance(scope, ScopeRef):
        return asdict(scope)
    return dict(scope or {})


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _nested(payload: dict[str, Any], *path: str) -> Any:
    current: Any = payload
    for key in path:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def _first_text(*values: Any) -> str:
    for value in values:
        if isinstance(value, dict):
            text = _first_text(value.get("status"), value.get("outcome"), value.get("result"), value.get("label"))
            if text:
                return text
            continue
        text = str(value or "").strip()
        if text:
            return text
    return ""


def _string_list(value: Any) -> list[str]:
    if isinstance(value, (list, tuple, set)):
        return [str(item) for item in value if str(item)]
    if value:
        return [str(value)]
    return []


def _float(value: Any) -> float:
    try:
        return round(float(value), 3)
    except (TypeError, ValueError):
        return 0.0


def _json_safe(value: Any) -> Any:
    if is_dataclass(value) and not isinstance(value, type):
        return _json_safe(asdict(value))
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json_safe(item) for item in value]
    if isinstance(value, tuple):
        return [_json_safe(item) for item in value]
    if isinstance(value, set):
        return sorted((_json_safe(item) for item in value), key=lambda item: repr(item))
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def _autonomy_preview_envelope(preview: dict[str, Any]) -> dict[str, Any]:
    """Keep preview output out of all feedback and reinforcement writes."""
    not_run = {"ok": None, "status": "not_run", "executed": False, "reason": "dry_run_preview"}
    payload = {**preview, **not_run, "dry_run": True, "apply": False}
    # Older preview providers may report successful-looking placeholders.
    # Preserve their keys, but never turn those placeholders into evidence.
    for field in ("capability_selection", "candidate_preview", "ledger", "retention"):
        value = preview.get(field)
        payload[field] = {**(value if isinstance(value, dict) else {}), **not_run}
    promotion = preview.get("promotion")
    regression = preview.get("regression_watch")
    payload["promotion"] = {**(promotion if isinstance(promotion, dict) else {}), **not_run, "applied": False, "dry_run": True}
    payload["regression_watch"] = {**(regression if isinstance(regression, dict) else {}), **not_run, "regressed": None, "record_id": ""}
    payload["eval_verdict"] = "not_run"
    # These are execution-report fields, not requested/planned configuration.
    # Copy only known report nodes; never traverse opaque payloads or rewrite
    # similarly named keys inside a caller's plan, requested data, or patch.
    network = preview.get("network_research")
    network = network if isinstance(network, dict) else {}
    output_gate = network.get("output_gate")
    output_gate = output_gate if isinstance(output_gate, dict) else {}
    payload["network_research"] = {
        **network, **not_run, "enabled": False,
        "task_count": 0, "hypothesis_count": 0, "error_count": 0, "evidence_refs": [],
        "output_gate": {
            **output_gate, **not_run, "decision": "not_run", "landing_targets": [],
            "web_evidence_count": 0, "evidence_refs": [], "research_note_id": "",
            "summary_record_id": "", "source_score_record_ids": [],
        },
    }
    payload["activity_status"] = "idle"
    payload["activity_reason"] = "dry_run_preview"
    payload["attempted_candidate_count"] = 0
    for field in ("replay_gate_passed", "safety_gate_passed", "isolation_gate_passed", "hypothesis_gate_passed"):
        if field in payload:
            payload[field] = None
    return {
        **payload,
        "cycle": payload,
        "feedback": dict(not_run),
        "memory": {**not_run, "record_id": ""},
        "rl": dict(not_run),
    }
