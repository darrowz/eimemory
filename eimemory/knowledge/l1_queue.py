from __future__ import annotations

from datetime import datetime, timezone
from copy import deepcopy
import errno
import re
import stat
from pathlib import Path
from typing import Any, Callable
from uuid import uuid4

from eimemory.core.strict_json import StrictJSONError, loads as strict_json_loads
from eimemory.models.records import ScopeRef
from eimemory.storage.atomic_file import DEFAULT_MAX_JSON_STATE_BYTES, atomic_write_json, interprocess_lock


MAX_ATTEMPTS = 5
RECOVERY_INTENT = "recover-selected-dead-l1"
MAX_RECOVERY_JOBS = 32
_RECOVERY_JOB_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parent_identity(job: dict[str, Any]) -> tuple[str, str, ScopeRef]:
    # Record IDs can legitimately repeat in separate storage partitions. The
    # worker reads the exact source and scope, so enqueue must use the same key.
    return (
        str(job.get("episode_id") or "").strip(),
        str(job.get("channel_id") or "hermes"),
        ScopeRef.from_dict(job.get("channel_scope") or {}),
    )


class L1QueueStateError(RuntimeError):
    """Existing queue state is unavailable; never replace it with empty state."""

    def __init__(self, code: str, *, retryable: bool = False) -> None:
        super().__init__(code)
        self.code = code
        self.retryable = retryable
        self.context: dict[str, Any] = {}


def _validate_job_identity(job: dict[str, Any]) -> None:
    # Legacy jobs may omit channel/scope and optional turn text. Explicitly
    # malformed values must not be coerced into that default parent partition.
    if not isinstance(job.get("episode_id"), str) or not job["episode_id"].strip():
        raise L1QueueStateError("queue_schema_invalid")
    for key in ("channel_id", "session_id", "turn_id", "user_text", "assistant_text",
                "turn_text", "claim_token"):
        if key in job and not isinstance(job[key], str):
            raise L1QueueStateError("queue_schema_invalid")
    if "channel_scope" in job:
        scope = job["channel_scope"]
        if (not isinstance(scope, dict)
                or set(scope) - {"tenant_id", "agent_id", "workspace_id", "user_id"}
                or any(not isinstance(value, str) for value in scope.values())):
            raise L1QueueStateError("queue_schema_invalid")
        try:
            ScopeRef.from_dict(scope)
        except (TypeError, ValueError) as exc:
            raise L1QueueStateError("queue_schema_invalid") from exc


class L1ExtractQueue:
    """Durable JSON queue. L0 write must not wait on LLM extract."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.lock_path = self.path.with_suffix(self.path.suffix + ".lock")
        self.consumer_lock_path = self.path.with_suffix(self.path.suffix + ".consumer.lock")

    def _load(self) -> dict[str, Any]:
        try:
            text = self.path.read_text(encoding="utf-8")
        except FileNotFoundError as exc:
            if exc.errno == errno.ENOENT:
                return {"jobs": [], "dead": []}
            raise L1QueueStateError("queue_unreadable", retryable=True) from exc
        except OSError as exc:
            raise L1QueueStateError("queue_unreadable", retryable=True) from exc
        except UnicodeError as exc:
            raise L1QueueStateError("queue_corrupt") from exc
        try:
            payload = strict_json_loads(text, max_bytes=DEFAULT_MAX_JSON_STATE_BYTES)
        except StrictJSONError as exc:
            raise L1QueueStateError("queue_corrupt") from exc
        if not isinstance(payload, dict):
            raise L1QueueStateError("queue_schema_invalid")
        for key in ("jobs", "dead"):
            if key not in payload:
                raise L1QueueStateError("queue_schema_invalid")
            if not isinstance(payload[key], list) or any(
                not isinstance(item, dict) for item in payload[key]
            ):
                raise L1QueueStateError("queue_schema_invalid")
            for item in payload[key]:
                if (not isinstance(item.get("job_id"), str) or not item["job_id"]
                        or not isinstance(item.get("status"), str)
                        or item.get("status") not in ({"queued", "running"} if key == "jobs" else {"dead"})
                        or type(item.get("attempts", 0)) is not int
                        or item.get("attempts", 0) < 0):
                    raise L1QueueStateError("queue_schema_invalid")
                _validate_job_identity(item)
        self._validate_recoveries(payload)
        return payload

    @staticmethod
    def _validate_recoveries(payload: dict[str, Any]) -> None:
        """Recovery acknowledgements survive removal of a completed live job."""
        records = payload.get("recoveries", {})
        if not isinstance(records, dict):
            raise L1QueueStateError("recovery_state_invalid")
        for job in [*payload["jobs"], *payload["dead"]]:
            if "recovery_of" in job:
                origin = job["recovery_of"]
                record = records.get(origin) if isinstance(origin, str) else None
                if not isinstance(record, dict) or record.get("job_id") != job["job_id"]:
                    raise L1QueueStateError("recovery_state_invalid")
        if not records:
            return
        all_jobs = [*payload["jobs"], *payload["dead"]]
        ids = [job["job_id"] for job in all_jobs]
        if len(ids) != len(set(ids)):
            raise L1QueueStateError("recovery_state_invalid")
        by_id = {job["job_id"]: job for job in all_jobs}
        parents: list[tuple[str, str, ScopeRef]] = []
        targets: set[str] = set()
        for original_id, record in records.items():
            original = by_id.get(original_id)
            if (not isinstance(original_id, str) or not isinstance(record, dict)
                    or original is None or original.get("status") != "dead"
                    or set(record) - {"job_id", "episode_id", "channel_id", "channel_scope",
                                      "original_attempts", "recovered_at", "completed_at"}
                    or not isinstance(record.get("job_id"), str)
                    or not _RECOVERY_JOB_ID.fullmatch(record["job_id"])
                    or record["job_id"] in targets
                    or record["job_id"] in records
                    or type(record.get("original_attempts")) is not int
                    or record["original_attempts"] != original.get("attempts", 0)
                    or not isinstance(record.get("recovered_at"), str)
                    or not record["recovered_at"]):
                raise L1QueueStateError("recovery_state_invalid")
            _validate_job_identity(record)
            identity = _parent_identity(record)
            if identity != _parent_identity(original) or identity in parents:
                raise L1QueueStateError("recovery_state_invalid")
            parents.append(identity)
            targets.add(record["job_id"])
            target = by_id.get(record["job_id"])
            completed = record.get("completed_at")
            if completed is not None:
                if not isinstance(completed, str) or not completed or target is not None:
                    raise L1QueueStateError("recovery_state_invalid")
            elif (target is None or target.get("recovery_of") != original_id
                    or _parent_identity(target) != identity):
                raise L1QueueStateError("recovery_state_invalid")

    @staticmethod
    def _recovery_selection(job_ids: list[str]) -> set[str]:
        if (not isinstance(job_ids, list) or not 1 <= len(job_ids) <= MAX_RECOVERY_JOBS
                or any(not isinstance(value, str) or not _RECOVERY_JOB_ID.fullmatch(value)
                       for value in job_ids)
                or len(set(job_ids)) != len(job_ids)):
            raise L1QueueStateError("recovery_selection_invalid")
        return set(job_ids)

    @staticmethod
    def _recovery_metadata(job: dict[str, Any], record: dict[str, Any] | None,
                           payload: dict[str, Any]) -> dict[str, Any]:
        # Explicit allowlist: no turn text, arbitrary exception strings or payload.
        status = None
        if record:
            status = "completed" if "completed_at" in record else next(
                row["status"] for row in [*payload["jobs"], *payload["dead"]]
                if row["job_id"] == record["job_id"])
        return {
            "job_id": job["job_id"],
            "episode_id": job["episode_id"],
            "channel_id": job.get("channel_id", "hermes"),
            "channel_scope": job.get("channel_scope", {}),
            "attempts": job.get("attempts", 0),
            "state": "already_recovered" if record else "dead",
            "recovery_job_id": record["job_id"] if record else None,
            "recovery_status": status,
        }

    def inspect_dead(self, *, job_ids: list[str] | None = None, limit: int = 32,
                     lock_timeout: float = 2) -> dict[str, Any]:
        selected = self._recovery_selection(job_ids) if job_ids is not None else None
        if type(limit) is not int or not 1 <= limit <= 200:
            raise L1QueueStateError("recovery_selection_invalid")
        self._require_recovery_queue()
        with interprocess_lock(self.lock_path, timeout=lock_timeout):
            self._require_recovery_queue()
            payload = self._load()
            jobs = [job for job in payload["dead"] if selected is None or job["job_id"] in selected]
            if selected is not None and {job["job_id"] for job in jobs} != selected:
                raise L1QueueStateError("recovery_job_unknown")
            if len({job["job_id"] for job in jobs}) != len(jobs):
                raise L1QueueStateError("recovery_state_invalid")
            truncated = selected is None and len(jobs) > limit
            if selected is None:
                jobs = jobs[-limit:]
            records = payload.get("recoveries", {})
            return {"ok": True, "dead": len(payload["dead"]), "truncated": truncated, "jobs": [
                self._recovery_metadata(job, records.get(job["job_id"]), payload) for job in jobs
            ]}

    def recover_dead(self, job_ids: list[str], *, operator_intent: str,
                     dry_run: bool = False, lock_timeout: float = 2) -> dict[str, Any]:
        """Atomically publish one bounded, explicit recovery with retained history.

        The original dead entries remain unchanged. The new jobs have a separate
        retry budget, while acknowledgements retain a permanent parent tombstone.
        The consumer lock serializes recovery with handlers and interrupted-claim
        recovery; the short state lock serializes publication with enqueue.
        """
        selected = self._recovery_selection(job_ids)
        if operator_intent != RECOVERY_INTENT:
            raise L1QueueStateError("recovery_intent_required")
        if type(dry_run) is not bool:
            raise L1QueueStateError("recovery_selection_invalid")
        self._require_recovery_queue()
        with interprocess_lock(self.consumer_lock_path, timeout=lock_timeout):
            with interprocess_lock(self.lock_path, timeout=lock_timeout):
                self._require_recovery_queue()
                payload = self._load()
                all_jobs = [*payload["jobs"], *payload["dead"]]
                all_ids = [job["job_id"] for job in all_jobs]
                if len(set(all_ids)) != len(all_ids):
                    raise L1QueueStateError("recovery_state_invalid")
                originals = [job for job in payload["dead"] if job["job_id"] in selected]
                if {job["job_id"] for job in originals} != selected:
                    raise L1QueueStateError("recovery_job_unknown")
                records = payload.get("recoveries", {})
                occupied = [_parent_identity(job) for job in payload["jobs"]]
                recovered = [_parent_identity(record) for record in records.values()]
                eligible = []
                # Validate the whole batch first. Preserve stored source order,
                # regardless of the order of operator arguments.
                for job in originals:
                    if job["job_id"] in records:
                        continue
                    identity = _parent_identity(job)
                    if identity in occupied or identity in recovered:
                        raise L1QueueStateError("recovery_parent_conflict")
                    occupied.append(identity)
                    eligible.append(job)
                report = {"ok": True, "dry_run": dry_run, "eligible": len(eligible),
                          "already_recovered": len(originals) - len(eligible), "published": 0,
                          "jobs": [self._recovery_metadata(job, records.get(job["job_id"]), payload)
                                   for job in originals]}
                if dry_run or not eligible:
                    return report
                records = dict(records)
                for job in eligible:
                    recovery_id = "recovery-" + uuid4().hex
                    if recovery_id in all_ids:
                        raise L1QueueStateError("recovery_state_invalid")
                    all_ids.append(recovery_id)
                    child = deepcopy(job)
                    child.update(job_id=recovery_id, status="queued", attempts=0,
                                 last_error="", recovery_of=job["job_id"])
                    child.pop("claim_token", None)
                    child.pop("claimed_at", None)
                    payload["jobs"].append(child)
                    records[job["job_id"]] = {
                        "job_id": recovery_id, "episode_id": job["episode_id"],
                        "channel_id": job.get("channel_id", "hermes"),
                        "channel_scope": deepcopy(job.get("channel_scope", {})),
                        "original_attempts": job.get("attempts", 0), "recovered_at": _now(),
                    }
                payload["recoveries"] = records
                self._save(payload)
                report["published"] = len(eligible)
                report["jobs"] = [self._recovery_metadata(job, records[job["job_id"]], payload) for job in originals]
                return report

    def _require_recovery_queue(self) -> None:
        # Operator mistakes must not initialize an empty queue at another root.
        # Check before acquiring locks too, because locks create their directory.
        try:
            info = self.path.lstat()
        except FileNotFoundError as exc:
            raise L1QueueStateError("recovery_queue_missing") from exc
        except OSError as exc:
            raise L1QueueStateError("queue_unreadable", retryable=True) from exc
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise L1QueueStateError("recovery_state_invalid")

    def _save(self, payload: dict[str, Any]) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_json(self.path, payload)
        except OSError as exc:
            # Atomic publication can fail before or after replace/fsync. Keep
            # the outcome unknown and preserve the actual handler/ACK phase.
            raise L1QueueStateError("queue_unwritable", retryable=True) from exc

    def enqueue(self, job: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(job, dict) or "recovery_of" in job:
            raise ValueError("queue_job_invalid")
        try:
            _validate_job_identity(job)
        except L1QueueStateError as exc:
            raise ValueError("queue_job_invalid") from exc
        episode_id = str(job.get("episode_id") or "").strip()
        with interprocess_lock(self.lock_path):
            payload = self._load()
            jobs: list[dict[str, Any]] = list(payload.get("jobs") or [])
            if episode_id:
                identity = _parent_identity(job)
                for existing in jobs:
                    if _parent_identity(existing) == identity and existing.get("status") in {
                        "queued",
                        "running",
                    }:
                        return existing
                for recovery in payload.get("recoveries", {}).values():
                    if _parent_identity(recovery) == identity:
                        for existing in payload["dead"]:
                            if existing["job_id"] == recovery["job_id"]:
                                return dict(existing)
                        # A completed recovery retains the parent identity so a
                        # repeated producer enqueue cannot resurrect the work.
                        return {**recovery, "status": "completed"}
            record = {
                "job_id": str(job.get("job_id") or uuid4().hex[:16]),
                "status": "queued",
                "attempts": 0,
                "created_at": _now(),
                "last_error": "",
                **{k: v for k, v in job.items() if k not in {"status", "attempts", "last_error", "claim_token", "job_id"}},
            }
            jobs.append(record)
            payload["jobs"] = jobs
            self._save(payload)
            return record

    def drain(self, handler: Callable[[dict[str, Any]], None], *, limit: int = 3) -> int:
        return int(self.drain_report(handler, limit=limit)["processed"])

    def drain_report(self, handler: Callable[[dict[str, Any]], None], *, limit: int = 3) -> dict[str, Any]:
        # Keep consumer ownership through the handler. OS locks survive slow
        # extraction and are released on process death; no time-based lease can
        # steal a live worker. Enqueue uses only the separate short state lock.
        # Stop pre-lock-version workers before upgrading this queue format.
        with interprocess_lock(self.consumer_lock_path):
            return self._drain_owned(handler, limit=limit)

    def _drain_owned(self, handler: Callable[[dict[str, Any]], None], *, limit: int) -> dict[str, Any]:
        processed = 0
        failed = 0
        newly_dead = 0
        errors: list[str] = []
        phase = "recover_interrupted"
        handler_error: Exception | None = None
        handler_completed: bool | None = None
        completion_recorded: bool | None = None
        last_claimed_job_id: str | None = None
        attempted_job_ids: set[str] = set()
        try:
            newly_dead = self._recover_interrupted()
            for _ in range(max(0, int(limit))):
                phase = "claim"
                job = self._claim(exclude_job_ids=attempted_job_ids)
                if job is None:
                    break
                # These diagnostics belong to the last actual claimed job.
                # Empty/failed next claims must not erase its confirmed result.
                last_claimed_job_id = str(job.get("job_id") or "") or None
                attempted_job_ids.add(str(job.get("job_id") or ""))
                handler_error = None
                claim_token = str(job.get("claim_token") or "")
                handler_completed = False
                completion_recorded = False
                try:
                    handler(job)
                except Exception as exc:
                    handler_error = exc
                    failed += 1
                    error = str(exc)[:500]
                    errors.append(error)
                    phase = "record_handler_failure"
                    newly_dead += self._fail(str(job.get("job_id") or ""), error, claim_token=claim_token)
                    continue
                handler_completed = True
                phase = "acknowledge_handler"
                completion_recorded = self._complete(str(job.get("job_id") or ""), claim_token=claim_token)
                if not completion_recorded:
                    # A completed handler is insufficient if its fenced claim
                    # disappeared or changed before the acknowledgement.
                    raise L1QueueStateError("queue_acknowledgement_lost", retryable=True)
                processed += 1
            phase = "count_pending"
            pending = self.pending_count()
            phase = "count_dead"
            dead = self.dead_count()
            return {
                "processed": processed,
                "failed": failed,
                "newly_dead": newly_dead,
                "pending": pending,
                "dead": dead,
                "errors": errors[-5:],
            }
        except (L1QueueStateError, OSError) as error:
            exc = error if isinstance(error, L1QueueStateError) else L1QueueStateError(
                "queue_unreadable", retryable=True
            )
            exc.context = {
                "phase": phase,
                "processed": processed,
                "failed": failed,
                "newly_dead": newly_dead,
                "errors": errors[-5:],
                "last_claimed_job_id": last_claimed_job_id,
                "handler_completed": handler_completed,
                "completion_recorded": completion_recorded,
                "handler_error": str(handler_error)[:500] if handler_error is not None else None,
            }
            if handler_error is not None and handler_error is not exc:
                raise exc from handler_error
            if exc is not error:
                raise exc from error
            raise

    def _recover_interrupted(self) -> int:
        """Called only while holding consumer ownership, including legacy jobs."""
        with interprocess_lock(self.lock_path):
            payload = self._load()
            remaining = []
            dead = list(payload.get("dead") or [])
            newly_dead = 0
            changed = False
            for job in payload.get("jobs") or []:
                if job.get("status") == "running":
                    changed = True
                    job["last_error"] = "worker_interrupted"
                    job.pop("claim_token", None)
                    if int(job.get("attempts") or 0) >= MAX_ATTEMPTS:
                        job["status"] = "dead"
                        dead.append(job)
                        newly_dead += 1
                        continue
                    job["status"] = "queued"
                remaining.append(job)
            if changed:
                payload["jobs"] = remaining
                pinned = set(payload.get("recoveries", {})) | {
                    row["job_id"] for row in payload.get("recoveries", {}).values()}
                payload["dead"] = [row for i, row in enumerate(dead)
                                   if i >= len(dead) - 200 or row["job_id"] in pinned]
                self._save(payload)
            return newly_dead

    def dead_count(self) -> int:
        with interprocess_lock(self.lock_path):
            payload = self._load()
            return len(list(payload.get("dead") or []))

    def recent_dead(self, *, limit: int = 5) -> list[dict[str, Any]]:
        with interprocess_lock(self.lock_path):
            payload = self._load()
            dead = list(payload.get("dead") or [])
        out: list[dict[str, Any]] = []
        for job in dead[-max(1, int(limit)) :]:
            out.append(
                {
                    "job_id": str(job.get("job_id") or ""),
                    "episode_id": str(job.get("episode_id") or ""),
                    "attempts": int(job.get("attempts") or 0),
                    "last_error": str(job.get("last_error") or "")[:300],
                }
            )
        return out

    def pending_count(self) -> int:
        with interprocess_lock(self.lock_path):
            payload = self._load()
            return sum(1 for job in payload.get("jobs") or [] if job.get("status") in {"queued", "running"})

    def _claim(self, *, exclude_job_ids: set[str] | tuple[str, ...] = ()) -> dict[str, Any] | None:
        with interprocess_lock(self.lock_path):
            payload = self._load()
            for job in payload.get("jobs") or []:
                if job.get("status") != "queued":
                    continue
                # A transient failure must wait for a later drain invocation.
                # Otherwise one outage consumes all retries in a tight loop
                # and keeps unrelated queued observations from progressing.
                if str(job.get("job_id") or "") in exclude_job_ids:
                    continue
                job["status"] = "running"
                job["attempts"] = int(job.get("attempts") or 0) + 1
                job["claimed_at"] = _now()
                job["claim_token"] = uuid4().hex
                self._save(payload)
                return dict(job)
            return None

    def _complete(self, job_id: str, *, claim_token: str) -> bool:
        with interprocess_lock(self.lock_path):
            payload = self._load()
            jobs = list(payload.get("jobs") or [])
            remaining = [job for job in jobs if not (
                str(job.get("job_id")) == job_id and job.get("status") == "running"
                and claim_token and job.get("claim_token") == claim_token
            )]
            if len(remaining) == len(jobs):
                return False
            payload["jobs"] = remaining
            for recovery in payload.get("recoveries", {}).values():
                if recovery["job_id"] == job_id:
                    recovery["completed_at"] = _now()
            self._save(payload)
            return True

    def _fail(self, job_id: str, error: str, *, claim_token: str) -> int:
        with interprocess_lock(self.lock_path):
            payload = self._load()
            remaining: list[dict[str, Any]] = []
            dead = list(payload.get("dead") or [])
            newly_dead = 0
            for job in payload.get("jobs") or []:
                if (str(job.get("job_id")) != job_id or job.get("status") != "running"
                        or not claim_token or job.get("claim_token") != claim_token):
                    remaining.append(job)
                    continue
                job["last_error"] = error[:500]
                if int(job.get("attempts") or 0) >= MAX_ATTEMPTS:
                    job["status"] = "dead"
                    dead.append(job)
                    newly_dead += 1
                else:
                    job["status"] = "queued"
                    remaining.append(job)
            payload["jobs"] = remaining
            pinned = set(payload.get("recoveries", {})) | {
                row["job_id"] for row in payload.get("recoveries", {}).values()}
            payload["dead"] = [row for i, row in enumerate(dead)
                               if i >= len(dead) - 200 or row["job_id"] in pinned]
            self._save(payload)
            return newly_dead
