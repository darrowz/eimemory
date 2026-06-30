from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timezone
import json
import os
import subprocess
from typing import Any, Callable
from urllib import request

from eimemory.models.records import RecordEnvelope, ScopeRef


DEFAULT_TIMER_UNITS = [
    "eimemory-learn-watch.timer",
    "eimemory-learn-think.timer",
    "eimemory-nightly.timer",
]
DEFAULT_SERVICE_UNITS = [
    "eimemory-learn-watch.service",
    "eimemory-learn-think.service",
    "eimemory-nightly.service",
]


def check_user_systemd_timers(
    runtime: Any,
    *,
    scope: dict[str, Any] | ScopeRef | None = None,
    unit_states: list[dict[str, Any]] | None = None,
    units: list[str] | None = None,
    now: str | None = None,
    stale_after_minutes: int = 90,
    runner: Callable[[list[str]], str] | None = None,
    notifier: Callable[[dict[str, Any]], Any] | None = None,
    webhook_url: str | None = None,
    persist: bool = True,
) -> dict[str, Any]:
    scope_ref = scope if isinstance(scope, ScopeRef) else ScopeRef.from_dict(scope)
    selected_units = list(units or [*DEFAULT_TIMER_UNITS, *DEFAULT_SERVICE_UNITS])
    states = list(unit_states or _collect_unit_states(selected_units, runner=runner))
    issues = _timer_issues(states, now=now, stale_after_minutes=stale_after_minutes)
    payload = _alert_payload(issues, states=states, now=now)
    alert_record_id = ""
    delivered = False
    if issues:
        if notifier is not None:
            notifier(payload)
            delivered = True
        url = webhook_url or os.environ.get("EIMEMORY_FEISHU_WEBHOOK") or os.environ.get("EIMEMORY_ALERT_WEBHOOK")
        if url:
            delivered = _post_feishu_webhook(url, payload) or delivered
        if persist:
            record = RecordEnvelope.create(
                kind="incident",
                title="eimemory user timer alert",
                summary=payload["text"],
                scope=scope_ref,
                source="eimemory.ops.timer_monitor",
                status="active",
                content={**payload, "unit_states": states},
                meta={
                    "report_type": "ops_timer_alert",
                    "issue_count": len(issues),
                    "delivered": delivered,
                    "channel": "feishu",
                },
                tags=["ops", "timer-monitor", "feishu-alert"],
            )
            runtime.store.append(record)
            alert_record_id = record.record_id
    return {
        "ok": not issues,
        "report_type": "ops_timer_monitor",
        "scope": asdict(scope_ref),
        "issue_count": len(issues),
        "issues": issues,
        "alert": payload if issues else {},
        "alert_record_id": alert_record_id,
        "delivered": delivered,
        "unit_count": len(states),
        "units": states,
    }


def _collect_unit_states(units: list[str], *, runner: Callable[[list[str]], str] | None = None) -> list[dict[str, Any]]:
    call = runner or _run_systemctl
    states: list[dict[str, Any]] = []
    for unit in units:
        try:
            raw = _show_unit(call, unit, user=True)
        except Exception as user_exc:
            try:
                raw = _show_unit(call, unit, user=False)
            except Exception as system_exc:
                states.append(
                    {
                        "unit": unit,
                        "load_state": "unknown",
                        "active_state": "unknown",
                        "error": f"user={user_exc}; system={system_exc}",
                    }
                )
                continue
        try:
            states.append(_parse_systemctl_show(unit, raw))
        except Exception as exc:
            states.append({"unit": unit, "load_state": "unknown", "active_state": "unknown", "error": str(exc)})
    return states


def _show_unit(call: Callable[[list[str]], str], unit: str, *, user: bool) -> str:
    args = ["systemctl"]
    if user:
        args.append("--user")
    args.extend(
        [
            "show",
            unit,
            "--property=LoadState,ActiveState,SubState,UnitFileState,LastTriggerUSec,NextElapseUSecRealtime,Result",
            "--no-page",
        ]
    )
    return call(args)


def _run_systemctl(args: list[str]) -> str:
    return subprocess.run(args, check=True, text=True, capture_output=True).stdout


def _parse_systemctl_show(unit: str, raw: str) -> dict[str, Any]:
    values: dict[str, str] = {}
    for line in str(raw or "").splitlines():
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key] = value
    return {
        "unit": unit,
        "load_state": values.get("LoadState", ""),
        "active_state": values.get("ActiveState", ""),
        "sub_state": values.get("SubState", ""),
        "unit_file_state": values.get("UnitFileState", ""),
        "last_trigger_at": values.get("LastTriggerUSec", ""),
        "next_elapse_at": values.get("NextElapseUSecRealtime", ""),
        "result": values.get("Result", ""),
    }


def _timer_issues(states: list[dict[str, Any]], *, now: str | None, stale_after_minutes: int) -> list[dict[str, Any]]:
    current = _parse_time(now) or datetime.now(timezone.utc)
    stale_after = max(1, int(stale_after_minutes or 90))
    issues: list[dict[str, Any]] = []
    for state in states:
        unit = str(state.get("unit") or "")
        load_state = str(state.get("load_state") or state.get("unit_file_state") or "").lower()
        active_state = str(state.get("active_state") or "").lower()
        result = str(state.get("result") or "").lower()
        masked = "masked" in load_state
        if masked:
            issues.append(_issue(state, reason="masked"))
        if active_state == "failed" or result == "failed":
            issues.append(_issue(state, reason="failed"))
        if unit.endswith(".timer") and not masked and active_state not in {"active", "activating"}:
            issues.append(_issue(state, reason="inactive"))
        last_trigger = _parse_time(state.get("last_trigger_at"))
        if unit.endswith(".timer") and last_trigger is not None:
            age_minutes = int((current - last_trigger).total_seconds() // 60)
            if age_minutes > stale_after:
                issues.append(_issue(state, reason="stale", age_minutes=age_minutes))
    deduped: dict[tuple[str, str], dict[str, Any]] = {}
    for issue in issues:
        deduped.setdefault((issue["unit"], issue["reason"]), issue)
    return list(deduped.values())


def _issue(state: dict[str, Any], *, reason: str, age_minutes: int | None = None) -> dict[str, Any]:
    payload = {
        "unit": str(state.get("unit") or ""),
        "reason": reason,
        "load_state": str(state.get("load_state") or ""),
        "active_state": str(state.get("active_state") or ""),
        "result": str(state.get("result") or ""),
    }
    if age_minutes is not None:
        payload["age_minutes"] = age_minutes
    return payload


def _alert_payload(issues: list[dict[str, Any]], *, states: list[dict[str, Any]], now: str | None) -> dict[str, Any]:
    lines = [f"{issue['unit']}:{issue['reason']}" for issue in issues]
    return {
        "ok": not issues,
        "channel": "feishu",
        "title": "eimemory timer monitor alert",
        "text": "eimemory timer monitor alert: " + (", ".join(lines) if lines else "all clear"),
        "issue_count": len(issues),
        "issues": issues,
        "checked_at": now or datetime.now(timezone.utc).isoformat(),
        "unit_count": len(states),
    }


def _post_feishu_webhook(url: str, payload: dict[str, Any]) -> bool:
    body = json.dumps({"msg_type": "text", "content": {"text": payload["text"]}}, ensure_ascii=False).encode("utf-8")
    req = request.Request(str(url), data=body, headers={"Content-Type": "application/json"})
    try:
        with request.urlopen(req, timeout=8) as response:
            return 200 <= int(response.status) < 300
    except Exception:
        return False


def _parse_time(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text or text.lower() in {"n/a", "never"}:
        return None
    if text.endswith(" UTC"):
        text = text[:-4] + "+00:00"
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)
