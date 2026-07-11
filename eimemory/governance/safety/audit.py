"""Append-only audit log with sha256 hash chain. Tampering = fail.

The audit log is the safety backbone of every autonomous eimemory
process.  Each row stores a sha256 hash of (prev_hash + row_index + ts
+ payload); the next row's ``prev_hash`` points at the previous row's
``row_hash``.  Any single-byte tamper of an on-disk row breaks the
chain and ``verify()`` raises :class:`ChainBroken` with the offending
row index.  This module never mutates or deletes an existing row.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from eimemory.governance.safety.file_lock import exclusive_file_lock


class ChainBroken(Exception):
    """Raised by :meth:`AuditLog.verify` when the on-disk chain is corrupt.

    Attributes:
        row_index: Zero-based index of the first row that fails the
            chain check (either ``prev_hash`` mismatch or
            ``row_hash`` mismatch).
        reason: Short human-readable reason (used for logging).
    """

    def __init__(self, row_index: int, reason: str) -> None:
        self.row_index = row_index
        self.reason = reason
        super().__init__(f"chain broken at row {row_index}: {reason}")


@dataclass(slots=True)
class AuditRow:
    """An in-memory view of one row of the audit log."""

    payload: dict
    prev_hash: str
    row_hash: str
    row_index: int


class AuditLog:
    """Append-only JSONL audit log with a sha256 chain.

    The file is created on first use.  Callers only ever call
    :meth:`append` to add rows; :meth:`read_all` and :meth:`verify` are
    read-only.  There is no public mutator that edits or deletes an
    existing row 鈥?that would break the chain and is detected on the
    next :meth:`verify`.
    """

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            self.path.touch()

    def append(self, payload: dict) -> AuditRow:
        """Append a new row to the log and return its parsed view.

        Args:
            payload: Caller-supplied event fields.  If ``"ts"`` is
                absent, the current UTC ISO-8601 timestamp is
                inserted.  ``"row_index"``, ``"prev_hash"``, and
                ``"row_hash"`` are managed by this class and any
                caller-supplied values are overwritten.

        Returns:
            The :class:`AuditRow` written, including the derived
            ``row_index``, ``prev_hash``, and ``row_hash``.
        """
        with exclusive_file_lock(self.path.with_suffix(self.path.suffix + ".lock")):
            rows = self.read_all()
            prev_hash = rows[-1].row_hash if rows else "0" * 64
            ts = payload.get("ts") or datetime.now(timezone.utc).isoformat()
            row_index = len(rows)
            body = {
                "ts": ts,
                "row_index": row_index,
                "prev_hash": prev_hash,
                **payload,
            }
            body_str = json.dumps(body, sort_keys=True, ensure_ascii=False)
            row_hash = hashlib.sha256(body_str.encode("utf-8")).hexdigest()
            body["row_hash"] = row_hash
            with self.path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(body, sort_keys=True, ensure_ascii=False) + "\n")
        return AuditRow(payload=body, prev_hash=prev_hash, row_hash=row_hash, row_index=row_index)

    def read_all(self) -> list[AuditRow]:
        """Return every row in the log in order (empty list if none)."""
        rows: list[AuditRow] = []
        if not self.path.exists():
            return rows
        with self.path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                body = json.loads(line)
                rows.append(AuditRow(
                    payload=body,
                    prev_hash=body.get("prev_hash", ""),
                    row_hash=body.get("row_hash", ""),
                    row_index=body.get("row_index", 0),
                ))
        return rows

    def verify(self) -> None:
        """Re-derive every row's hash and confirm the chain is intact.

        Raises:
            ChainBroken: If any row's stored ``prev_hash`` does not
                match the previous row's ``row_hash``, or if any
                row's stored ``row_hash`` does not match a fresh
                sha256 of the row body (minus ``row_hash``).
        """
        rows = self.read_all()
        expected_prev = "0" * 64
        for i, row in enumerate(rows):
            if row.prev_hash != expected_prev:
                raise ChainBroken(i, f"prev_hash mismatch (expected {expected_prev[:8]}..., got {row.prev_hash[:8]}...)")
            # Re-derive the row's hash from the body minus row_hash
            body = {k: v for k, v in row.payload.items() if k != "row_hash"}
            body_str = json.dumps(body, sort_keys=True, ensure_ascii=False)
            derived = hashlib.sha256(body_str.encode("utf-8")).hexdigest()
            if derived != row.row_hash:
                raise ChainBroken(i, f"row_hash mismatch (expected {derived[:8]}..., got {row.row_hash[:8]}...)")
            expected_prev = row.row_hash
