#!/usr/bin/env python3
"""Minimal OpenClaw loop ledger, verifier, and smoke runner.

This is intentionally self-contained so it can guard long-running OpenClaw tasks
without depending on the gateway process being healthy.
"""
from __future__ import annotations

import argparse
import calendar
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, field
import gzip
import hashlib
import json
import os
import shutil
import socket
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator
from urllib.error import URLError
from urllib import request
from urllib.request import urlopen

SCHEMA_VERSION = "openclaw.loop.v1"
TERMINAL_STATUSES = {"done", "failed", "rolled_back"}
ACTIVE_STATUSES = {"planned", "running", "waiting", "verifying"}
WATCH_STALE_SAMPLE_LIMIT = 20
MAX_LEDGER_TEXT_CHARS = 4096
LEDGER_NAMES = (
    "actions.jsonl",
    "lesson_candidates.jsonl",
    "reports.jsonl",
    "tasks.jsonl",
    "verifications.jsonl",
    "watch.jsonl",
)
_TEST_NOW: float | None = None


@dataclass
class _JsonlCacheEntry:
    rows: list[dict[str, Any]] = field(default_factory=list)
    offset: int = 0
    line_count: int = 0
    mtime_ns: int = 0
    size: int = 0


_JSONL_CACHE: dict[str, _JsonlCacheEntry] = {}


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


def reset_jsonl_cache_for_tests() -> None:
    _JSONL_CACHE.clear()


def _jsonl_cache_key(path: Path) -> str:
    return str(path.absolute())


@contextmanager
def _append_lock(name: str):
    lock_path = path_for(f"{name}.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path.touch(exist_ok=True)
    with lock_path.open("r+b") as handle:
        handle.seek(0)
        if not handle.read(1):
            handle.write(b"\0")
            handle.flush()
        handle.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
            try:
                yield
            finally:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def append_jsonl(name: str, record: dict[str, Any]) -> None:
    record = {"schema_version": SCHEMA_VERSION, "writer_version": "1", **record}
    line = json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n"
    with _append_lock(name):
        path = path_for(name)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(line)
        _append_cached_jsonl_row(path, record)


def _append_cached_jsonl_row(path: Path, record: dict[str, Any]) -> None:
    key = _jsonl_cache_key(path)
    entry = _JSONL_CACHE.get(key)
    if entry is None:
        return
    try:
        stat = path.stat()
    except OSError:
        _JSONL_CACHE.pop(key, None)
        return
    entry.rows.append(dict(record))
    entry.line_count += 1
    entry.offset = stat.st_size
    entry.size = stat.st_size
    entry.mtime_ns = stat.st_mtime_ns


def _record_corrupt_line(name: str, *, line_number: int, raw: str, error: str) -> None:
    if name == "corrupt.jsonl":
        return
    append_jsonl(
        "corrupt.jsonl",
        {
            "corrupt_id": new_id("corrupt"),
            "source_file": name,
            "line_number": line_number,
            "raw": raw[:1000],
            "error": error,
            "created_at": iso_ts(),
        },
    )


def read_jsonl(name: str) -> list[dict[str, Any]]:
    path = path_for(name)
    key = _jsonl_cache_key(path)
    if not path.exists():
        _JSONL_CACHE.pop(key, None)
        return []
    try:
        stat = path.stat()
    except OSError:
        _JSONL_CACHE.pop(key, None)
        return []

    entry = _JSONL_CACHE.get(key)
    if entry is not None and stat.st_size == entry.offset and stat.st_mtime_ns == entry.mtime_ns:
        return [dict(row) for row in entry.rows]

    if entry is not None and stat.st_size > entry.offset:
        rows = list(entry.rows)
        offset = entry.offset
        line_number = entry.line_count
    else:
        rows = []
        offset = 0
        line_number = 0

    with path.open("rb") as handle:
        handle.seek(offset)
        for raw in handle:
            line_number += 1
            try:
                line = raw.decode("utf-8")
            except UnicodeDecodeError as exc:
                _record_corrupt_line(name, line_number=line_number, raw=raw[:1000].decode("utf-8", errors="replace"), error=str(exc))
                continue
            if not line.strip():
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError as exc:
                _record_corrupt_line(name, line_number=line_number, raw=line, error=str(exc))
        offset = handle.tell()

    try:
        stat = path.stat()
    except OSError:
        _JSONL_CACHE.pop(key, None)
        return [dict(row) for row in rows]
    _JSONL_CACHE[key] = _JsonlCacheEntry(
        rows=[dict(row) for row in rows],
        offset=offset,
        line_count=line_number,
        mtime_ns=stat.st_mtime_ns,
        size=stat.st_size,
    )
    return [dict(row) for row in rows]


def latest_by_id(name: str, id_field: str) -> dict[str, dict[str, Any]]:
    latest: dict[str, dict[str, Any]] = {}
    for row in read_jsonl(name):
        value = row.get(id_field)
        if value:
            latest[str(value)] = row
    return latest


def _bounded_text(value: Any, *, max_chars: int = MAX_LEDGER_TEXT_CHARS) -> str:
    text = str(value or "")
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + f"...[truncated {len(text) - max_chars} chars]"


def _stale_summary(stale: list[dict[str, Any]], *, sample_limit: int = WATCH_STALE_SAMPLE_LIMIT) -> dict[str, Any]:
    reason_counts: dict[str, int] = {}
    task_ids: list[str] = []
    for item in stale:
        reason = str(item.get("stale_reason") or "unknown")
        reason_counts[reason] = reason_counts.get(reason, 0) + 1
        task_id = str(item.get("task_id") or "")
        if task_id and len(task_ids) < sample_limit:
            task_ids.append(task_id)
    return {
        "count": len(stale),
        "reason_counts": dict(sorted(reason_counts.items())),
        "sample_task_ids": task_ids,
        "sample_truncated": len(stale) > len(task_ids),
    }


def _bounded_checks(checks: dict[str, Any]) -> dict[str, Any]:
    bounded = dict(checks)
    stale = bounded.pop("stale_tasks", None)
    if isinstance(stale, list):
        bounded["stale_summary"] = _stale_summary([item for item in stale if isinstance(item, dict)])
    serialized = json.dumps(bounded, ensure_ascii=False, sort_keys=True, default=str)
    if len(serialized) <= 8192:
        return bounded
    return {
        "summary": "checks_truncated",
        "check_keys": sorted(str(key) for key in bounded),
        "serialized_chars": len(serialized),
    }


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


def record_dispatch(
    task_id: str,
    *,
    dispatch_type: str,
    command_or_tool: str,
    lease_seconds: int = 300,
    progress: str = "",
    result: str = "started",
) -> dict[str, Any]:
    """Record that background work was dispatched and keep its task leased."""
    action = record_action(
        task_id,
        action_type="dispatch",
        command_or_tool=command_or_tool,
        result=result,
    )
    heartbeat = record_heartbeat(
        task_id,
        lease_seconds=lease_seconds,
        progress=progress or command_or_tool,
        source=dispatch_type or "dispatch",
    )
    return {"task_id": task_id, "action": action, "heartbeat": heartbeat}


def record_lesson_candidate(
    task_id: str,
    *,
    verification_id: str,
    verifier: str,
    checks: dict[str, Any],
    failure_reason: str,
    next_action: str = "repair",
) -> dict[str, Any]:
    bounded_checks = _bounded_checks(checks)
    lesson = {
        "lesson_candidate_id": new_id("lesson"),
        "task_id": task_id,
        "verification_id": verification_id,
        "source": "openclaw_loop.verification_failed",
        "verifier": verifier,
        "checks": bounded_checks,
        "failure_reason": _bounded_text(failure_reason),
        "next_action": next_action,
        "created_at": iso_ts(),
    }
    append_jsonl("lesson_candidates.jsonl", lesson)
    return lesson


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
    bounded_checks = _bounded_checks(checks)
    verification = {
        "verification_id": new_id("verification"),
        "task_id": task_id,
        "verifier": verifier,
        "checks": bounded_checks,
        "passed": bool(passed),
        "evidence_refs": evidence_refs or [],
        "failure_reason": _bounded_text(failure_reason),
        "next_action": next_action,
        "created_at": iso_ts(),
    }
    append_jsonl("verifications.jsonl", verification)
    if not verification["passed"]:
        record_lesson_candidate(
            task_id,
            verification_id=verification["verification_id"],
            verifier=verifier,
            checks=bounded_checks,
            failure_reason=failure_reason,
            next_action=next_action,
        )
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
    report["delivery"] = deliver_report(report)
    append_jsonl("reports.jsonl", report)
    return report


def deliver_report(report: dict[str, Any]) -> dict[str, Any]:
    payload = {
        "channel": "feishu",
        "title": "OpenClaw task report",
        "task_id": str(report.get("task_id") or ""),
        "status": str(report.get("status") or ""),
        "summary": str(report.get("summary") or ""),
        "text": f"OpenClaw task {report.get('status')}: {report.get('summary')}",
        "created_at": str(report.get("created_at") or ""),
    }
    outbox = os.environ.get("OPENCLAW_LOOP_REPORT_OUTBOX")
    webhook = (
        os.environ.get("OPENCLAW_LOOP_FEISHU_WEBHOOK")
        or os.environ.get("EIMEMORY_FEISHU_WEBHOOK")
        or os.environ.get("EIMEMORY_ALERT_WEBHOOK")
    )
    delivered = False
    attempted: list[str] = []
    if outbox:
        attempted.append("outbox")
        path = Path(outbox)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")
        delivered = True
    if webhook:
        attempted.append("feishu_webhook")
        delivered = _post_feishu_webhook(webhook, payload) or delivered
    return {
        "channel": "feishu",
        "attempted": attempted,
        "delivered": delivered,
        "skipped": not attempted,
    }


def _post_feishu_webhook(url: str, payload: dict[str, Any]) -> bool:
    body = json.dumps({"msg_type": "text", "content": {"text": payload["text"]}}, ensure_ascii=False).encode("utf-8")
    req = request.Request(str(url), data=body, headers={"Content-Type": "application/json"})
    try:
        with request.urlopen(req, timeout=8) as response:
            return 200 <= int(response.status) < 300
    except Exception:
        return False


def _latest_verification(task_id: str) -> dict[str, Any] | None:
    latest: dict[str, Any] | None = None
    for row in read_jsonl("verifications.jsonl"):
        if row.get("task_id") == task_id:
            latest = row
    return latest


def _should_record_report(report_policy: str, status: str) -> bool:
    if report_policy == "silent":
        return False
    if report_policy == "always":
        return True
    if report_policy == "on_blocked":
        return status == "blocked"
    return status == "done"


def finish_task(task_id: str, *, status: str = "done", summary: str = "", force: bool = False) -> dict[str, Any]:
    if status == "done" and not force:
        verification = _latest_verification(task_id)
        if not verification or not verification.get("passed"):
            raise RuntimeError("cannot mark task done without a passing verification; use force=True to override")
    task = update_task(task_id, status=status, result_summary=summary, current_step=status, last_action="finished")
    if _should_record_report(str(task.get("report_policy") or "on_done"), status):
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


def reconcile_stale_tasks(*, apply: bool = False, limit: int | None = None) -> dict[str, Any]:
    stale = sorted(find_stale_tasks(), key=lambda item: str(item.get("task_id") or ""))
    selected = stale if limit is None else stale[: max(0, int(limit))]
    summary = _stale_summary(stale)
    reconciled: list[str] = []
    if apply:
        for item in selected:
            task_id = str(item.get("task_id") or "")
            if not task_id:
                continue
            stale_reason = str(item.get("stale_reason") or "unknown")
            reconciled_task = dict(item)
            reconciled_task.pop("stale_reason", None)
            reconciled_task.update(
                {
                    "status": "failed",
                    "current_step": "failed",
                    "last_action": "stale task reconciled",
                    "failure_class": f"{stale_reason}_reconciled",
                    "blocker": "",
                    "lease_expires_at": 0,
                    "next_check_at": "",
                    "result_summary": f"stale task reconciled after {stale_reason}",
                    "updated_at": iso_ts(),
                }
            )
            append_jsonl("tasks.jsonl", reconciled_task)
            reconciled.append(task_id)
    return {
        "ok": True,
        "applied": bool(apply),
        "stale_count": len(stale),
        "selected_count": len(selected),
        "reconciled_count": len(reconciled),
        "reconciled_task_ids": reconciled[:WATCH_STALE_SAMPLE_LIMIT],
        "stale_summary": summary,
    }


def _compact_record(name: str, row: dict[str, Any]) -> dict[str, Any]:
    compacted = dict(row)
    if isinstance(compacted.get("checks"), dict):
        compacted["checks"] = _bounded_checks(compacted["checks"])
    for field in ("failure_reason", "objective", "result_summary", "summary", "blocker"):
        if field in compacted:
            compacted[field] = _bounded_text(compacted.get(field))
    codes = compacted.get("codes")
    if isinstance(codes, list):
        compacted["codes"] = list(dict.fromkeys(str(code) for code in codes))[:100]
    stale_ids = compacted.get("stale_task_ids")
    if isinstance(stale_ids, list):
        compacted.setdefault("stale_count", len(stale_ids))
        compacted["stale_task_ids"] = [str(task_id) for task_id in stale_ids[:WATCH_STALE_SAMPLE_LIMIT]]
    return compacted


def _iter_jsonl_for_compaction(name: str) -> Iterator[dict[str, Any]]:
    path = path_for(name)
    with path.open("rb") as handle:
        for line_number, raw in enumerate(handle, start=1):
            try:
                line = raw.decode("utf-8")
            except UnicodeDecodeError as exc:
                _record_corrupt_line(
                    name,
                    line_number=line_number,
                    raw=raw[:1000].decode("utf-8", errors="replace"),
                    error=str(exc),
                )
                continue
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                _record_corrupt_line(name, line_number=line_number, raw=line, error=str(exc))
                continue
            if isinstance(row, dict):
                yield row


def _write_jsonl_atomic(name: str, rows: Iterable[dict[str, Any]]) -> int:
    path = path_for(name)
    temp_path = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    mode = (path.stat().st_mode & 0o777) if path.exists() else 0o660
    count = 0
    try:
        with temp_path.open("w", encoding="utf-8", newline="\n") as handle:
            for row in rows:
                handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
                count += 1
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temp_path, mode)
        os.replace(temp_path, path)
    finally:
        temp_path.unlink(missing_ok=True)
    return count


def compact_ledgers(*, archive_dir: str | Path | None = None) -> dict[str, Any]:
    root = data_dir()
    archive_root = Path(archive_dir) if archive_dir is not None else root / "archives"
    if archive_root.exists() and archive_root.is_symlink():
        raise RuntimeError("archive directory must not be a symlink")
    archive_root.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(now_epoch()))
    archive_path = archive_root / f"openclaw-loop-{stamp}-{uuid.uuid4().hex[:8]}.jsonl.gz"
    archive_temp = archive_path.with_name(f".{archive_path.name}.tmp")
    existing_names = [name for name in LEDGER_NAMES if path_for(name).exists()]
    original_bytes = sum(path_for(name).stat().st_size for name in existing_names)
    rows_before: dict[str, int] = {}
    rows_after: dict[str, int] = {}

    with ExitStack() as stack:
        for name in existing_names:
            stack.enter_context(_append_lock(name))
        reset_jsonl_cache_for_tests()
        try:
            with gzip.open(archive_temp, "wb") as archive:
                for name in existing_names:
                    archive.write(f"# file={name}\n".encode("utf-8"))
                    with path_for(name).open("rb") as source:
                        shutil.copyfileobj(source, archive, length=1024 * 1024)
                    archive.write(b"\n")
            os.replace(archive_temp, archive_path)

            for name in existing_names:
                if name == "tasks.jsonl":
                    latest: dict[str, dict[str, Any]] = {}
                    before_count = 0
                    for row in _iter_jsonl_for_compaction(name):
                        before_count += 1
                        task_id = str(row.get("task_id") or "")
                        if task_id:
                            latest[task_id] = row
                    rows_before[name] = before_count
                    rows_after[name] = _write_jsonl_atomic(
                        name,
                        (_compact_record(name, row) for row in latest.values()),
                    )
                    continue

                before_count = 0

                def compacted_rows() -> Iterator[dict[str, Any]]:
                    nonlocal before_count
                    for row in _iter_jsonl_for_compaction(name):
                        before_count += 1
                        yield _compact_record(name, row)

                rows_after[name] = _write_jsonl_atomic(name, compacted_rows())
                rows_before[name] = before_count
        finally:
            archive_temp.unlink(missing_ok=True)
            reset_jsonl_cache_for_tests()

    compacted_bytes = sum(path_for(name).stat().st_size for name in existing_names)
    return {
        "ok": True,
        "archive_path": str(archive_path),
        "original_bytes": original_bytes,
        "compacted_bytes": compacted_bytes,
        "reclaimed_bytes": max(0, original_bytes - compacted_bytes),
        "rows_before": rows_before,
        "rows_after": rows_after,
    }


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
        loopback_gateway_health_url = os.environ.get(
            "OPENCLAW_LOOPBACK_HEALTH_URL",
            "http://127.0.0.1:18789/health",
        )
        for code, url in [
            ("eimemory_health_failed", rpc_url),
            ("openclaw_gateway_health_failed", gateway_health_url),
            ("openclaw_loopback_health_failed", loopback_gateway_health_url),
        ]:
            try:
                payload = _http_json(url)
                if not payload.get("ok"):
                    codes.append(code)
                    findings.append({"code": code, "url": url, "payload": payload})
            except (OSError, URLError, TimeoutError, json.JSONDecodeError) as exc:
                codes.append(code)
                findings.append({"code": code, "url": url, "error": str(exc)})
        _append_service_health(codes, findings, check_openclaw_loopback_proxy_user_service())
    return {"ok": not codes, "codes": codes, "findings": findings, "config_path": str(path)}


def _append_service_health(codes: list[str], findings: list[dict[str, Any]], service: dict[str, Any]) -> None:
    if service.get("ok"):
        return
    reason_codes = [part.strip() for part in str(service.get("reason") or "").split(",") if part.strip()]
    if not reason_codes:
        reason_codes = ["openclaw_loopback_proxy_unhealthy"]
    codes.extend(reason_codes)
    findings.append({"code": "openclaw_loopback_proxy_unhealthy", "reasons": reason_codes, "service": service})


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


def _systemctl_output(args: list[str], *, timeout: float = 3.0) -> str:
    try:
        result = subprocess.run(
            ["systemctl", *args],
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as exc:
        return f"unavailable:{type(exc).__name__}"
    return (result.stdout or result.stderr or "").strip()


def _user_systemctl_output(args: list[str], *, timeout: float = 3.0) -> str:
    if os.name == "nt":
        return "unavailable:windows"
    service_user = os.environ.get("SERVICE_USER") or os.environ.get("EIMEMORY_SERVICE_USER")
    if service_user and hasattr(os, "geteuid") and os.geteuid() == 0 and service_user != "root":
        import pwd

        try:
            user_info = pwd.getpwnam(service_user)
        except KeyError:
            return "unavailable:service_user_missing"
        command = [
            "runuser",
            "-u",
            service_user,
            "--",
            "env",
            f"XDG_RUNTIME_DIR=/run/user/{user_info.pw_uid}",
            "systemctl",
            "--user",
            *args,
        ]
        try:
            result = subprocess.run(
                command,
                check=False,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as exc:
            return f"unavailable:{type(exc).__name__}"
        return (result.stdout or result.stderr or "").strip()
    return _systemctl_output(["--user", *args], timeout=timeout)


def check_rpc_user_systemd_owner() -> dict[str, Any]:
    if os.name == "nt":
        return {"ok": True, "skipped": True, "reason": "systemctl_unavailable_on_windows"}

    system_owner_active = _systemctl_output(["is-active", "eimemory-rpc.service"])
    system_owner_enabled = _systemctl_output(["is-enabled", "eimemory-rpc.service"])
    system_owner_fragment = _systemctl_output(["show", "eimemory-rpc.service", "-p", "FragmentPath", "--value"])
    user_owner_active = _user_systemctl_output(["is-active", "eimemory-rpc.service"])
    user_owner_enabled = _user_systemctl_output(["is-enabled", "eimemory-rpc.service"])
    reasons: list[str] = []
    if system_owner_active == "active":
        reasons.append("system_rpc_service_active")
    if system_owner_enabled == "enabled":
        reasons.append("system_rpc_service_enabled")
    if system_owner_fragment:
        reasons.append("system_rpc_service_unit_present")
    if user_owner_active != "active":
        reasons.append("user_rpc_service_not_active")
    if user_owner_enabled != "enabled":
        reasons.append("user_rpc_service_not_enabled")
    return {
        "ok": not reasons,
        "reason": ",".join(reasons),
        "system_owner_active": system_owner_active or "unknown",
        "system_owner_enabled": system_owner_enabled or "unknown",
        "system_owner_fragment": system_owner_fragment,
        "user_owner_active": user_owner_active or "unknown",
        "user_owner_enabled": user_owner_enabled or "unknown",
    }


def check_openclaw_loopback_proxy_user_service() -> dict[str, Any]:
    if os.name == "nt":
        return {"ok": True, "skipped": True, "reason": "systemctl_unavailable_on_windows"}

    active = _user_systemctl_output(["is-active", "openclaw-loopback-proxy.service"])
    enabled = _user_systemctl_output(["is-enabled", "openclaw-loopback-proxy.service"])
    reasons: list[str] = []
    if active != "active":
        reasons.append("openclaw_loopback_proxy_inactive")
    if enabled not in {"enabled", "static"}:
        reasons.append("openclaw_loopback_proxy_not_enabled")
    return {
        "ok": not reasons,
        "reason": ",".join(reasons),
        "active": active or "unknown",
        "enabled": enabled or "unknown",
    }


def run_deploy_verify(
    *,
    commit: str = "",
    release_path: str = "",
    config_path: str | Path | None = None,
    run_live_checks: bool = True,
    service_owner_checker: Callable[[], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    release_label = commit or Path(release_path).name or "unknown"
    task = create_task(
        title=f"eimemory deploy verify {release_label}",
        objective="verify immutable release switch and OpenClaw/eimemory loop health after deploy",
        source="deploy",
        owner="system",
        risk_level="medium",
        report_policy="always",
        dedupe_key=f"deploy:{release_label}",
    )
    task_id = task["task_id"]
    dispatch = record_dispatch(
        task_id,
        dispatch_type="deploy",
        command_or_tool="deploy/install_immutable_release.sh",
        lease_seconds=300,
        progress=f"release switched: {release_path or release_label}",
    )
    drift = check_config_drift(config_path=config_path, run_live_checks=run_live_checks)
    rpc_service_owner = (service_owner_checker or check_rpc_user_systemd_owner)()
    checks = {
        "commit": commit,
        "release_path": release_path,
        "config_drift": drift,
        "rpc_service_owner": rpc_service_owner,
    }
    passed = bool(drift.get("ok")) and bool(rpc_service_owner.get("ok"))
    failure_reason = ",".join(
        [part for part in [";".join(drift.get("codes") or []), str(rpc_service_owner.get("reason") or "")] if part]
    )
    verification = record_verification(
        task_id,
        verifier="deploy.install_immutable_release",
        checks=checks,
        passed=passed,
        evidence_refs=[f"action:{dispatch['action']['action_id']}"],
        failure_reason=failure_reason,
        next_action="report_done" if passed else "repair",
    )
    finish_task(
        task_id,
        status="done" if passed else "blocked",
        summary="deploy verify passed" if passed else "deploy verify failed: " + failure_reason,
    )
    return {
        "ok": passed,
        "task_id": task_id,
        "verification_id": verification["verification_id"],
        "commit": commit,
        "release_path": release_path,
        "drift": drift,
        "rpc_service_owner": rpc_service_owner,
    }



def run_watch(*, config_path: str | Path | None = None, run_live_checks: bool = True) -> dict[str, Any]:
    drift = check_config_drift(config_path=config_path, run_live_checks=run_live_checks)
    stale = find_stale_tasks()
    stale_summary = _stale_summary(stale)
    ok = bool(drift.get("ok")) and not stale
    watch = {
        "watch_id": new_id("watch"),
        "created_at": iso_ts(),
        "ok": ok,
        "drift_codes": drift.get("codes") or [],
        "stale_count": stale_summary["count"],
        "stale_reason_counts": stale_summary["reason_counts"],
        "stale_task_ids": stale_summary["sample_task_ids"],
        "stale_sample_truncated": stale_summary["sample_truncated"],
    }
    append_jsonl("watch.jsonl", watch)
    if ok:
        return {"ok": True, "watch_id": watch["watch_id"], "tasks_created": 0, "drift": drift, "stale_count": 0}

    codes = list(
        dict.fromkeys(
            list(drift.get("codes") or [])
            + [f"stale:{reason}" for reason in stale_summary["reason_counts"]]
        )
    )
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
        checks={"drift": drift, "stale_summary": stale_summary},
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
    p_action.add_argument("--cmd", required=True, dest="command_or_tool")
    p_action.add_argument("--exit-code", type=int, default=0)
    p_action.add_argument("--result", default="success")

    p_dispatch = sub.add_parser("dispatch")
    p_dispatch.add_argument("task_id")
    p_dispatch.add_argument("--type", default="background")
    p_dispatch.add_argument("--cmd", required=True, dest="command_or_tool")
    p_dispatch.add_argument("--lease-seconds", type=int, default=300)
    p_dispatch.add_argument("--progress", default="")

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
    p_done.add_argument("--force", action="store_true")

    sub.add_parser("list")
    sub.add_parser("stale")
    p_reconcile = sub.add_parser("reconcile-stale")
    p_reconcile.add_argument("--apply", action="store_true")
    p_reconcile.add_argument("--limit", type=int)
    p_compact = sub.add_parser("compact")
    p_compact.add_argument("--archive-dir")
    p_doctor = sub.add_parser("doctor")
    p_doctor.add_argument("--no-live", action="store_true")
    p_doctor.add_argument("--config")
    p_smoke = sub.add_parser("smoke")
    p_smoke.add_argument("--no-live", action="store_true")
    p_smoke.add_argument("--config")
    p_deploy_verify = sub.add_parser("deploy-verify")
    p_deploy_verify.add_argument("--commit", default="")
    p_deploy_verify.add_argument("--release-path", default="")
    p_deploy_verify.add_argument("--no-live", action="store_true")
    p_deploy_verify.add_argument("--config")
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
        return emit(record_action(args.task_id, action_type=args.type, command_or_tool=args.command_or_tool, exit_code=args.exit_code, result=args.result))
    if args.cmd == "dispatch":
        return emit(record_dispatch(args.task_id, dispatch_type=args.type, command_or_tool=args.command_or_tool, lease_seconds=args.lease_seconds, progress=args.progress))
    if args.cmd == "verify":
        checks = {item.split("=", 1)[0]: item.split("=", 1)[1] if "=" in item else True for item in args.check}
        return emit(record_verification(args.task_id, verifier=args.verifier, checks=checks, passed=args.passed, evidence_refs=args.evidence))
    if args.cmd == "done":
        return emit(finish_task(args.task_id, status=args.status, summary=args.summary, force=args.force))
    if args.cmd == "list":
        return emit(load_tasks())
    if args.cmd == "stale":
        return emit(find_stale_tasks())
    if args.cmd == "reconcile-stale":
        return emit(reconcile_stale_tasks(apply=args.apply, limit=args.limit))
    if args.cmd == "compact":
        return emit(compact_ledgers(archive_dir=args.archive_dir))
    if args.cmd == "doctor":
        result = check_config_drift(config_path=args.config, run_live_checks=not args.no_live)
        emit(result)
        return 0 if result.get("ok") else 2
    if args.cmd == "smoke":
        result = run_smoke(config_path=args.config, run_live_checks=not args.no_live)
        emit(result)
        return 0 if result.get("ok") else 2
    if args.cmd == "deploy-verify":
        result = run_deploy_verify(
            commit=args.commit,
            release_path=args.release_path,
            config_path=args.config,
            run_live_checks=not args.no_live,
        )
        emit(result)
        return 0 if result.get("ok") else 2
    if args.cmd == "watch":
        result = run_watch(config_path=args.config, run_live_checks=not args.no_live)
        emit(result)
        return 0 if result.get("ok") else 2
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
