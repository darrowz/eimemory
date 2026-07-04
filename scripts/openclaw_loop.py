#!/usr/bin/env python3
"""Minimal OpenClaw loop ledger, verifier, and smoke runner.

This is intentionally self-contained so it can guard long-running OpenClaw tasks
without depending on the gateway process being healthy.
"""
from __future__ import annotations

import argparse
import calendar
import hashlib
import json
import os
import socket
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any
from urllib.error import URLError
from urllib.request import urlopen

SCHEMA_VERSION = "openclaw.loop.v1"
TERMINAL_STATUSES = {"done", "failed", "blocked", "rolled_back"}
ACTIVE_STATUSES = {"planned", "running", "waiting", "verifying"}
_TEST_NOW: float | None = None


def reset_clock_for_tests() -> None:
    global _TEST_NOW
    _TEST_NOW = None


def now_epoch() -> float:
    return _TEST_NOW if _TEST_NOW is not None else time.time()


def iso_ts(epoch: float | None = None) -> str:
    value = now_epoch() if epoch is None else epoch
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(value))


def data_dir() -> Path:
    configured = os.environ.get("OPENCLAW_LOOP_HOME")
    if configured:
        root = Path(configured)
    else:
        primary = Path("/var/lib/openclaw/task-ledger")
        try:
            primary.mkdir(parents=True, exist_ok=True)
            probe = primary / ".write-probe"
            probe.write_text("ok", encoding="utf-8")
            probe.unlink(missing_ok=True)
            root = primary
        except OSError:
            root = Path.home() / ".openclaw" / "task-ledger"
    root.mkdir(parents=True, exist_ok=True)
    return root


def path_for(name: str) -> Path:
    return data_dir() / name


def append_jsonl(name: str, record: dict[str, Any]) -> None:
    record = {"schema_version": SCHEMA_VERSION, "writer_version": "1", **record}
    with path_for(name).open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")


def read_jsonl(name: str) -> list[dict[str, Any]]:
    path = path_for(name)
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        rows.append(json.loads(line))
    return rows


def latest_by_id(name: str, id_field: str) -> dict[str, dict[str, Any]]:
    latest: dict[str, dict[str, Any]] = {}
    for row in read_jsonl(name):
        value = row.get(id_field)
        if value:
            latest[str(value)] = row
    return latest


def load_tasks() -> list[dict[str, Any]]:
    return list(latest_by_id("tasks.jsonl", "task_id").values())


def get_task(task_id: str) -> dict[str, Any]:
    task = latest_by_id("tasks.jsonl", "task_id").get(task_id)
    if not task:
        raise KeyError(f"unknown task_id: {task_id}")
    return task


def new_id(prefix: str) -> str:
    return f"{prefix}_{time.strftime('%Y%m%d_%H%M%S', time.gmtime(now_epoch()))}_{uuid.uuid4().hex[:8]}"


def create_task(
    *,
    title: str,
    objective: str,
    source: str = "user",
    owner: str = "main",
    risk_level: str = "low",
    report_policy: str = "on_done",
    dedupe_key: str | None = None,
    parent_task_id: str | None = None,
) -> dict[str, Any]:
    if dedupe_key:
        for task in load_tasks():
            if task.get("dedupe_key") == dedupe_key and task.get("status") not in TERMINAL_STATUSES:
                reused = dict(task)
                reused["reused"] = True
                return reused
    ts = iso_ts()
    task = {
        "task_id": new_id("task"),
        "parent_task_id": parent_task_id or "",
        "source": source,
        "title": title,
        "objective": objective,
        "status": "planned",
        "owner": owner,
        "risk_level": risk_level,
        "started_at": ts,
        "updated_at": ts,
        "next_check_at": "",
        "deadline_at": "",
        "last_action": "created",
        "current_step": "intake",
        "evidence_refs": [],
        "result_summary": "",
        "blocker": "",
        "report_policy": report_policy,
        "report_target": "",
        "dedupe_key": dedupe_key or "",
        "idempotency_key": dedupe_key or "",
        "lease_expires_at": 0,
        "heartbeat_at": "",
        "heartbeat_source": "",
        "last_progress_hash": "",
    }
    append_jsonl("tasks.jsonl", task)
    result = dict(task)
    result["reused"] = False
    return result


def update_task(task_id: str, **updates: Any) -> dict[str, Any]:
    task = dict(get_task(task_id))
    task.update({key: value for key, value in updates.items() if value is not None})
    task["updated_at"] = iso_ts()
    append_jsonl("tasks.jsonl", task)
    return task


def record_heartbeat(task_id: str, *, lease_seconds: int = 300, progress: str = "", source: str = "watcher") -> dict[str, Any]:
    progress_hash = hashlib.sha256(progress.encode("utf-8")).hexdigest()[:16] if progress else ""
    return update_task(
        task_id,
        status="running",
        heartbeat_at=iso_ts(),
        heartbeat_source=source,
        lease_expires_at=now_epoch() + lease_seconds,
        next_check_at=iso_ts(now_epoch() + max(1, lease_seconds // 2)),
        last_progress_hash=progress_hash,
        last_action=f"heartbeat: {progress}" if progress else "heartbeat",
    )


def record_action(
    task_id: str,
    *,
    action_type: str,
    command_or_tool: str,
    result: str = "success",
    exit_code: int = 0,
    stdout_ref: str = "",
    stderr_ref: str = "",
    retry_of: str = "",
) -> dict[str, Any]:
    started = iso_ts()
    action = {
        "action_id": new_id("action"),
        "task_id": task_id,
        "action_type": action_type,
        "command_or_tool": command_or_tool,
        "started_at": started,
        "finished_at": iso_ts(),
        "exit_code": exit_code,
        "stdout_ref": stdout_ref,
        "stderr_ref": stderr_ref,
        "result": result,
        "retry_of": retry_of,
    }
    append_jsonl("actions.jsonl", action)
    update_task(task_id, last_action=f"{action_type}: {command_or_tool}", current_step="acting")
    return action


def record_verification(
    task_id: str,
    *,
    verifier: str,
    checks: dict[str, Any],
    passed: bool,
    evidence_refs: list[str] | None = None,
    failure_reason: str = "",
    next_action: str = "report_done",
) -> dict[str, Any]:
    verification = {
        "verification_id": new_id("verification"),
        "task_id": task_id,
        "verifier": verifier,
        "checks": checks,
        "passed": bool(passed),
        "evidence_refs": evidence_refs or [],
        "failure_reason": failure_reason,
        "next_action": next_action,
        "created_at": iso_ts(),
    }
    append_jsonl("verifications.jsonl", verification)
    task = get_task(task_id)
    evidence = list(task.get("evidence_refs") or []) + list(evidence_refs or [])
    update_task(task_id, status="verifying", evidence_refs=evidence, current_step="verifying")
    return verification


def record_report(task_id: str, *, status: str, summary: str, evidence_refs: list[str] | None = None) -> dict[str, Any]:
    report = {
        "report_id": new_id("report"),
        "task_id": task_id,
        "status": status,
        "summary": summary,
        "evidence_refs": evidence_refs or [],
        "created_at": iso_ts(),
    }
    append_jsonl("reports.jsonl", report)
    return report


def finish_task(task_id: str, *, status: str = "done", summary: str = "") -> dict[str, Any]:
    task = update_task(task_id, status=status, result_summary=summary, current_step=status, last_action="finished")
    record_report(task_id, status=status, summary=summary or status, evidence_refs=task.get("evidence_refs") or [])
    return task


def find_stale_tasks(*, now: float | None = None, older_than_seconds: int = 1200) -> list[dict[str, Any]]:
    current = now_epoch() if now is None else now
    stale: list[dict[str, Any]] = []
    for task in load_tasks():
        if task.get("status") not in ACTIVE_STATUSES:
            continue
        lease = float(task.get("lease_expires_at") or 0)
        item = dict(task)
        if lease and lease < current:
            item["stale_reason"] = "lease_expired"
            stale.append(item)
            continue
        updated = task.get("updated_at") or task.get("started_at")
        try:
            updated_epoch = calendar.timegm(time.strptime(str(updated), "%Y-%m-%dT%H:%M:%SZ"))
        except (TypeError, ValueError):
            updated_epoch = 0
        if updated_epoch and current - updated_epoch > older_than_seconds:
            item["stale_reason"] = "updated_too_old"
            stale.append(item)
    return stale


def _hash_secret(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


def _http_json(url: str, timeout: float = 3.0) -> dict[str, Any]:
    with urlopen(url, timeout=timeout) as response:  # nosec B310: local/tailnet operator health probe
        return json.loads(response.read().decode("utf-8"))


def check_config_drift(*, config_path: str | Path | None = None, run_live_checks: bool = True) -> dict[str, Any]:
    path = Path(config_path) if config_path else Path(os.environ.get("OPENCLAW_CONFIG_PATH", Path.home() / ".openclaw" / "openclaw.json"))
    findings: list[dict[str, Any]] = []
    codes: list[str] = []
    if not path.exists():
        return {"ok": False, "codes": ["openclaw_config_missing"], "findings": [{"code": "openclaw_config_missing", "path": str(path)}]}
    data = json.loads(path.read_text(encoding="utf-8"))
    gateway = data.get("gateway") or {}
    auth_token = str(((gateway.get("auth") or {}).get("token")) or "")
    remote = gateway.get("remote") or {}
    remote_token = str(remote.get("token") or "")
    remote_url = str(remote.get("url") or "")
    if auth_token and remote_token and auth_token != remote_token:
        codes.append("gateway_token_mismatch")
        findings.append({"code": "gateway_token_mismatch", "auth_token_hash": _hash_secret(auth_token), "remote_token_hash": _hash_secret(remote_token)})
    if "127.0.0.1" in remote_url or "localhost" in remote_url:
        codes.append("gateway_remote_loopback")
        findings.append({"code": "gateway_remote_loopback", "remote_url": remote_url})
    if run_live_checks:
        rpc_url = os.environ.get("EIMEMORY_HEALTH_URL", "http://100.105.189.120:8091/health")
        gateway_health_url = os.environ.get("OPENCLAW_HEALTH_URL", "http://100.105.189.120:18789/health")
        for code, url in [("eimemory_health_failed", rpc_url), ("openclaw_gateway_health_failed", gateway_health_url)]:
            try:
                payload = _http_json(url)
                if not payload.get("ok"):
                    codes.append(code)
                    findings.append({"code": code, "url": url, "payload": payload})
            except (OSError, URLError, TimeoutError, json.JSONDecodeError) as exc:
                codes.append(code)
                findings.append({"code": code, "url": url, "error": str(exc)})
    return {"ok": not codes, "codes": codes, "findings": findings, "config_path": str(path)}


def run_smoke(*, config_path: str | Path | None = None, run_live_checks: bool = True) -> dict[str, Any]:
    dedupe = f"loop-smoke:{uuid.uuid4().hex}"
    task = create_task(
        title="OpenClaw loop smoke",
        objective="prove task ledger, lease, action, verification, and report close the loop",
        source="system",
        owner="system",
        dedupe_key=dedupe,
        report_policy="always",
    )
    task_id = task["task_id"]
    record_heartbeat(task_id, lease_seconds=300, progress="smoke-start")
    action = record_action(task_id, action_type="verify", command_or_tool="check_config_drift", result="success")
    drift = check_config_drift(config_path=config_path, run_live_checks=run_live_checks)
    verification = record_verification(
        task_id,
        verifier="openclaw_loop_smoke",
        checks={"config_drift": drift},
        passed=bool(drift.get("ok")),
        evidence_refs=[f"action:{action['action_id']}"],
        failure_reason=";".join(drift.get("codes") or []),
        next_action="report_done" if drift.get("ok") else "replan",
    )
    status = "done" if drift.get("ok") else "blocked"
    finish_task(task_id, status=status, summary="loop smoke passed" if drift.get("ok") else "loop smoke found drift")
    return {
        "ok": drift.get("ok") is True,
        "task_id": task_id,
        "actions": len([row for row in read_jsonl("actions.jsonl") if row.get("task_id") == task_id]),
        "verifications": len([row for row in read_jsonl("verifications.jsonl") if row.get("task_id") == task_id]),
        "reports": len([row for row in read_jsonl("reports.jsonl") if row.get("task_id") == task_id]),
        "verification_id": verification["verification_id"],
        "drift": drift,
    }



def run_watch(*, config_path: str | Path | None = None, run_live_checks: bool = True) -> dict[str, Any]:
    drift = check_config_drift(config_path=config_path, run_live_checks=run_live_checks)
    stale = find_stale_tasks()
    ok = bool(drift.get("ok")) and not stale
    watch = {
        "watch_id": new_id("watch"),
        "created_at": iso_ts(),
        "ok": ok,
        "drift_codes": drift.get("codes") or [],
        "stale_task_ids": [item.get("task_id") for item in stale],
    }
    append_jsonl("watch.jsonl", watch)
    if ok:
        return {"ok": True, "watch_id": watch["watch_id"], "tasks_created": 0, "drift": drift, "stale_count": 0}

    codes = list(drift.get("codes") or []) + [f"stale:{item.get('stale_reason', 'unknown')}" for item in stale]
    dedupe = "loop-watch:" + hashlib.sha256("|".join(sorted(codes)).encode("utf-8")).hexdigest()[:16]
    task = create_task(
        title="OpenClaw loop watchdog finding",
        objective="repair or report loop drift/stale state: " + ",".join(codes),
        source="system",
        owner="system",
        risk_level="medium",
        report_policy="on_blocked",
        dedupe_key=dedupe,
    )
    task_id = task["task_id"]
    record_action(task_id, action_type="verify", command_or_tool="openclaw_loop.watch", result="failed" if codes else "success")
    record_verification(
        task_id,
        verifier="openclaw_loop_watch",
        checks={"drift": drift, "stale_tasks": stale},
        passed=False,
        evidence_refs=[f"watch:{watch['watch_id']}"],
        failure_reason=",".join(codes),
        next_action="replan",
    )
    finish_task(task_id, status="blocked", summary="loop watch found: " + ",".join(codes))
    return {"ok": False, "watch_id": watch["watch_id"], "tasks_created": 1 if not task.get("reused") else 0, "task_id": task_id, "codes": codes, "drift": drift, "stale_count": len(stale)}

def emit(payload: Any) -> int:
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="OpenClaw/eimemory loop ledger MVP")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_create = sub.add_parser("create")
    p_create.add_argument("--title", required=True)
    p_create.add_argument("--objective", required=True)
    p_create.add_argument("--source", default="user")
    p_create.add_argument("--owner", default="main")
    p_create.add_argument("--dedupe-key", default="")

    p_update = sub.add_parser("update")
    p_update.add_argument("task_id")
    p_update.add_argument("--status")
    p_update.add_argument("--current-step")
    p_update.add_argument("--last-action")

    p_heartbeat = sub.add_parser("heartbeat")
    p_heartbeat.add_argument("task_id")
    p_heartbeat.add_argument("--lease-seconds", type=int, default=300)
    p_heartbeat.add_argument("--progress", default="")

    p_action = sub.add_parser("action")
    p_action.add_argument("task_id")
    p_action.add_argument("--type", required=True)
    p_action.add_argument("--cmd", required=True)
    p_action.add_argument("--exit-code", type=int, default=0)
    p_action.add_argument("--result", default="success")

    p_verify = sub.add_parser("verify")
    p_verify.add_argument("task_id")
    p_verify.add_argument("--verifier", required=True)
    p_verify.add_argument("--passed", action="store_true")
    p_verify.add_argument("--check", action="append", default=[])
    p_verify.add_argument("--evidence", action="append", default=[])

    p_done = sub.add_parser("done")
    p_done.add_argument("task_id")
    p_done.add_argument("--summary", default="done")
    p_done.add_argument("--status", default="done")

    sub.add_parser("list")
    sub.add_parser("stale")
    p_doctor = sub.add_parser("doctor")
    p_doctor.add_argument("--no-live", action="store_true")
    p_doctor.add_argument("--config")
    p_smoke = sub.add_parser("smoke")
    p_smoke.add_argument("--no-live", action="store_true")
    p_smoke.add_argument("--config")
    p_watch = sub.add_parser("watch")
    p_watch.add_argument("--no-live", action="store_true")
    p_watch.add_argument("--config")

    args = parser.parse_args(argv)
    if args.cmd == "create":
        return emit(create_task(title=args.title, objective=args.objective, source=args.source, owner=args.owner, dedupe_key=args.dedupe_key or None))
    if args.cmd == "update":
        return emit(update_task(args.task_id, status=args.status, current_step=args.current_step, last_action=args.last_action))
    if args.cmd == "heartbeat":
        return emit(record_heartbeat(args.task_id, lease_seconds=args.lease_seconds, progress=args.progress))
    if args.cmd == "action":
        return emit(record_action(args.task_id, action_type=args.type, command_or_tool=args.cmd, exit_code=args.exit_code, result=args.result))
    if args.cmd == "verify":
        checks = {item.split("=", 1)[0]: item.split("=", 1)[1] if "=" in item else True for item in args.check}
        return emit(record_verification(args.task_id, verifier=args.verifier, checks=checks, passed=args.passed, evidence_refs=args.evidence))
    if args.cmd == "done":
        return emit(finish_task(args.task_id, status=args.status, summary=args.summary))
    if args.cmd == "list":
        return emit(load_tasks())
    if args.cmd == "stale":
        return emit(find_stale_tasks())
    if args.cmd == "doctor":
        result = check_config_drift(config_path=args.config, run_live_checks=not args.no_live)
        emit(result)
        return 0 if result.get("ok") else 2
    if args.cmd == "smoke":
        result = run_smoke(config_path=args.config, run_live_checks=not args.no_live)
        emit(result)
        return 0 if result.get("ok") else 2
    if args.cmd == "watch":
        result = run_watch(config_path=args.config, run_live_checks=not args.no_live)
        emit(result)
        return 0 if result.get("ok") else 2
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
