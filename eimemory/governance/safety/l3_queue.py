"""L3 action queue: pending_human by default, human approval flips to approved.

L3 actions in eimemory (spend, auth, deploy, devices, prompt mutation, deletion)
require explicit human approval. The L3Queue is the single entry point: any caller
that wants to perform an L3 action must request and wait for a human approver.

Backed by a JSONL file at ``<root>/l3_queue.jsonl`` so the queue survives process
restarts and can be inspected by a separate human-facing tool.
"""
from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from eimemory.governance.safety.file_lock import exclusive_file_lock


log = logging.getLogger(__name__)


PENDING = "pending_human"
APPROVED = "approved"
REJECTED = "rejected"


class L3Queue:
    """File-backed queue for L3 action requests awaiting human approval.

    Records are stored one per line in a JSONL file under ``root``. The file is
    read fully on every read, so this is intended for a low-volume queue (a few
    requests per day) — not for high-throughput workflows.
    """

    def __init__(self, root: Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / "l3_queue.jsonl"
        self.lock_path = self.root / "l3_queue.lock"

    def request(
        self,
        *,
        action_class: str,
        payload: dict[str, Any],
        requester: str,
    ) -> str:
        """Record a new L3 request. Returns its id. Default status is ``pending_human``."""
        rid = uuid.uuid4().hex
        record = {
            "id": rid,
            "action_class": action_class,
            "payload": payload,
            "requester": requester,
            "status": PENDING,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "approver": None,
            "approved_at": None,
        }
        with exclusive_file_lock(self.lock_path):
            with self.path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
        log.info(
            "l3_request queued id=%s action_class=%s requester=%s",
            rid,
            action_class,
            requester,
        )
        return rid

    def get(self, rid: str) -> dict[str, Any]:
        """Return the current record for ``rid``. Raises ``KeyError`` if missing."""
        for rec in self._read_all():
            if rec["id"] == rid:
                return rec
        raise KeyError(rid)

    def approve(self, rid: str, *, approver: str) -> dict[str, Any]:
        """Flip a ``pending_human`` request to ``approved``. Returns the updated record."""
        with exclusive_file_lock(self.lock_path):
            records = self._read_all()
            for rec in records:
                if rec["id"] != rid:
                    continue
                if rec["status"] != PENDING:
                    raise ValueError(
                        f"cannot approve id={rid}: current status is {rec['status']!r}"
                    )
                rec["status"] = APPROVED
                rec["approver"] = approver
                rec["approved_at"] = datetime.now(timezone.utc).isoformat()
                self._write_all_preserving_current(records)
                log.info("l3_request approved id=%s approver=%s", rid, approver)
                return rec
        raise KeyError(rid)

    def list_pending(self) -> list[dict[str, Any]]:
        """Return all requests currently in ``pending_human`` status."""
        return [rec for rec in self._read_all() if rec["status"] == PENDING]

    def _read_all(self) -> list[dict[str, Any]]:
        return self._read_all_from_disk()

    def _read_all_from_disk(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        with self.path.open("r", encoding="utf-8") as f:
            return [json.loads(line) for line in f if line.strip()]

    def _write_all(self, records: list[dict[str, Any]]) -> None:
        with self.path.open("w", encoding="utf-8") as f:
            for rec in records:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    def _write_all_preserving_current(self, records: list[dict[str, Any]]) -> None:
        updated = {str(rec.get("id") or ""): rec for rec in records if rec.get("id")}
        merged: list[dict[str, Any]] = []
        seen: set[str] = set()
        for current in self._read_all_from_disk():
            rid = str(current.get("id") or "")
            if rid in updated:
                merged.append(updated[rid])
            else:
                merged.append(current)
            seen.add(rid)
        for rec in records:
            rid = str(rec.get("id") or "")
            if rid and rid not in seen:
                merged.append(rec)
        self._write_all(merged)
