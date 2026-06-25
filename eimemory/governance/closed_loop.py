from __future__ import annotations

from dataclasses import asdict, is_dataclass
import json
from typing import Any

from eimemory.models.records import ScopeRef


SUCCESS_LABELS = {"success", "good", "passed", "pass", "ok"}


def evaluate_result(runtime: Any, result: dict[str, Any], *, scope: dict[str, Any] | ScopeRef | None = None) -> dict[str, Any]:
    payload = dict(result or {})
    record = _record_for_result(runtime, payload, scope=scope)
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
        "source": "closed_loop.evaluate",
    }


def post_experience_hook(runtime: Any, result: dict[str, Any], scope: dict[str, Any] | ScopeRef | None) -> dict[str, Any]:
    eval_result = evaluate_result(runtime, result, scope=scope)
    memory_update = _ingest_feedback_memory(
        runtime,
        scope=scope,
        title="auto-feedback",
        memory_type="reflection",
        source="loop",
        evaluation=eval_result,
    )
    learning_signal = _safe_generate_learning(runtime, scope=scope)
    return {
        "eval": eval_result,
        "memory": memory_update,
        "learning": learning_signal,
    }


def autonomy_cycle(
    runtime: Any,
    scope: dict[str, Any] | ScopeRef | None,
    **kwargs: Any,
) -> dict[str, Any]:
    cycle_result = runtime.run_autonomy_cycle(scope=scope, **kwargs)
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
    return {
        "ok": bool(cycle_result.get("ok", False)) if isinstance(cycle_result, dict) else False,
        "cycle": cycle_result,
        "feedback": feedback,
        "memory": memory_update,
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
    return record.to_dict() if hasattr(record, "to_dict") else {"record_id": getattr(record, "record_id", "")}


def _safe_generate_learning(runtime: Any, *, scope: dict[str, Any] | ScopeRef | None) -> dict[str, Any]:
    generator = getattr(runtime, "generate_learning_thoughts", None)
    if not callable(generator):
        return {"ok": False, "error": "learning_generator_unavailable"}
    try:
        return dict(generator(scope=_scope_dict(scope), persist=True, max_items=3))
    except Exception as exc:
        return {"ok": False, "error": exc.__class__.__name__, "detail": str(exc)}


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
