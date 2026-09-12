from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any, Callable
from uuid import uuid4

from eimemory.storage.atomic_file import atomic_write_json, interprocess_lock


MAX_ATTEMPTS = 5


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class L1ExtractQueue:
    """Durable JSON queue. L0 write must not wait on LLM extract."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.lock_path = self.path.with_suffix(self.path.suffix + ".lock")
        self.consumer_lock_path = self.path.with_suffix(self.path.suffix + ".consumer.lock")

    def _load(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"jobs": [], "dead": []}
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            return {"jobs": [], "dead": []}
        if not isinstance(payload, dict):
            return {"jobs": [], "dead": []}
        payload.setdefault("jobs", [])
        payload.setdefault("dead", [])
        return payload

    def _save(self, payload: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_json(self.path, payload)

    def enqueue(self, job: dict[str, Any]) -> dict[str, Any]:
        episode_id = str(job.get("episode_id") or "").strip()
        with interprocess_lock(self.lock_path):
            payload = self._load()
            jobs: list[dict[str, Any]] = list(payload.get("jobs") or [])
            if episode_id:
                for existing in jobs:
                    if str(existing.get("episode_id") or "") == episode_id and existing.get("status") in {
                        "queued",
                        "running",
                    }:
                        return existing
            record = {
                "job_id": str(job.get("job_id") or uuid4().hex[:16]),
                "status": "queued",
                "attempts": 0,
                "created_at": _now(),
                "last_error": "",
                **{k: v for k, v in job.items() if k not in {"status", "attempts", "last_error", "claim_token"}},
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
        newly_dead = self._recover_interrupted()
        errors: list[str] = []
        for _ in range(max(0, int(limit))):
            job = self._claim()
            if job is None:
                break
            claim_token = str(job.get("claim_token") or "")
            try:
                handler(job)
            except Exception as exc:
                failed += 1
                error = str(exc)[:500]
                errors.append(error)
                before = self.dead_count()
                self._fail(str(job.get("job_id") or ""), error, claim_token=claim_token)
                if self.dead_count() > before:
                    newly_dead += 1
                continue
            if self._complete(str(job.get("job_id") or ""), claim_token=claim_token):
                processed += 1
        return {
            "processed": processed,
            "failed": failed,
            "newly_dead": newly_dead,
            "pending": self.pending_count(),
            "dead": self.dead_count(),
            "errors": errors[-5:],
        }

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
                payload["dead"] = dead[-200:]
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

    def _claim(self) -> dict[str, Any] | None:
        with interprocess_lock(self.lock_path):
            payload = self._load()
            for job in payload.get("jobs") or []:
                if job.get("status") != "queued":
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
            self._save(payload)
            return True

    def _fail(self, job_id: str, error: str, *, claim_token: str) -> None:
        with interprocess_lock(self.lock_path):
            payload = self._load()
            remaining: list[dict[str, Any]] = []
            dead = list(payload.get("dead") or [])
            for job in payload.get("jobs") or []:
                if (str(job.get("job_id")) != job_id or job.get("status") != "running"
                        or not claim_token or job.get("claim_token") != claim_token):
                    remaining.append(job)
                    continue
                job["last_error"] = error[:500]
                if int(job.get("attempts") or 0) >= MAX_ATTEMPTS:
                    job["status"] = "dead"
                    dead.append(job)
                else:
                    job["status"] = "queued"
                    remaining.append(job)
            payload["jobs"] = remaining
            payload["dead"] = dead[-200:]
            self._save(payload)
