from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from eimemory.core.clock import now_iso
from eimemory.governance.learning_state import append_learning_record_once, stable_semantic_key
from eimemory.models.records import RecordEnvelope, ScopeRef


SUPERVISOR_COMMANDS = ("nightly",)
LEGACY_SUPERVISOR_COMMANDS = ("learn-think", "learn-watch")
SUMMARY_DEFAULTS = {
    "last_success_at": "",
    "last_error_at": "",
    "duration_ms": 0,
    "memory_peak": 0,
    "produced_count": 0,
    "promoted_count": 0,
    "rolled_back_count": 0,
}


def supervisor_summary(
    *,
    command: str,
    ok: bool,
    duration_ms: int,
    memory_peak: int,
    produced_count: int = 0,
    promoted_count: int = 0,
    rolled_back_count: int = 0,
    error: str = "",
) -> dict[str, Any]:
    now = now_iso()
    return {
        "command": str(command),
        **SUMMARY_DEFAULTS,
        "last_success_at": now if ok else "",
        "last_error_at": "" if ok else now,
        "duration_ms": max(0, int(duration_ms or 0)),
        "memory_peak": max(0, int(memory_peak or 0)),
        "produced_count": max(0, int(produced_count or 0)),
        "promoted_count": max(0, int(promoted_count or 0)),
        "rolled_back_count": max(0, int(rolled_back_count or 0)),
        "ok": bool(ok),
        "error": str(error or ""),
    }


def persist_supervisor_summary(
    runtime: Any,
    *,
    scope: dict[str, Any] | ScopeRef | None,
    summary: dict[str, Any],
) -> RecordEnvelope:
    scope_ref = scope if isinstance(scope, ScopeRef) else ScopeRef.from_dict(scope)
    command = str(summary.get("command") or "unknown")
    record = append_learning_record_once(
        runtime,
        kind="reflection",
        title=f"Supervisor run: {command}",
        summary=f"{command} ok={bool(summary.get('ok'))} produced={int(summary.get('produced_count') or 0)}",
        scope=scope_ref,
        loop_id="supervisor",
        step_name="supervisor_run",
        semantic_key=stable_semantic_key("supervisor_run", command, scope_ref),
        authority_tier="L0",
        status="active" if summary.get("ok") else "failed",
        content={"report_type": "supervisor_run", **dict(summary)},
        meta={"report_type": "supervisor_run", "command": command, "ok": bool(summary.get("ok"))},
    )
    record.status = "active" if summary.get("ok") else "failed"
    record.content = {"report_type": "supervisor_run", **dict(summary)}
    record.meta = {**dict(record.meta or {}), "report_type": "supervisor_run", "command": command, "ok": bool(summary.get("ok"))}
    record.touch()
    return runtime.store.rewrite(record)


def build_supervisor_contract(
    runtime: Any,
    *,
    scope: dict[str, Any] | ScopeRef | None,
    commands: tuple[str, ...] = SUPERVISOR_COMMANDS,
) -> dict[str, Any]:
    scope_ref = scope if isinstance(scope, ScopeRef) else ScopeRef.from_dict(scope)
    latest = _latest_supervisor_records(runtime, scope=scope_ref)
    runs = {command: _summary_from_record(latest.get(command), command=command) for command in commands}
    if not latest:
        status = "unknown"
    elif any(item.get("last_error_at") and not item.get("last_success_at") for item in runs.values()):
        status = "degraded"
    elif any(_is_stuck(item) for item in runs.values() if item.get("last_success_at")):
        status = "stuck"
    elif all(item.get("last_success_at") for item in runs.values()):
        status = "healthy"
    else:
        status = "degraded"
    return {"status": status, "runs": runs}


def _latest_supervisor_records(runtime: Any, *, scope: ScopeRef) -> dict[str, RecordEnvelope]:
    latest: dict[str, RecordEnvelope] = {}
    for record in runtime.store.list_records(kinds=["reflection"], scope=scope, limit=200):
        meta = record.meta if isinstance(record.meta, dict) else {}
        content = record.content if isinstance(record.content, dict) else {}
        if str(meta.get("report_type") or content.get("report_type") or "") != "supervisor_run":
            continue
        command = str(meta.get("command") or content.get("command") or "")
        if command and command not in latest:
            latest[command] = record
    return latest


def _summary_from_record(record: RecordEnvelope | None, *, command: str) -> dict[str, Any]:
    if record is None:
        return {"command": command, **SUMMARY_DEFAULTS, "ok": False, "error": "no_run_record"}
    content = record.content if isinstance(record.content, dict) else {}
    return {
        "command": command,
        **SUMMARY_DEFAULTS,
        **{key: content.get(key, default) for key, default in SUMMARY_DEFAULTS.items()},
        "ok": bool(content.get("ok")),
        "error": str(content.get("error") or ""),
    }


def _is_stuck(summary: dict[str, Any]) -> bool:
    raw = str(summary.get("last_success_at") or "")
    if not raw:
        return False
    try:
        ts = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return False
    age_seconds = (datetime.now(timezone.utc) - ts.astimezone(timezone.utc)).total_seconds()
    return age_seconds > 3 * 24 * 3600
