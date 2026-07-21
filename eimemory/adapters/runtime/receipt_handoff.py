from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
from typing import Mapping

from eimemory.governance.tool_receipts import MAX_ELIGIBLE_RECEIPTS_PER_RUN

RECEIPT_HANDOFF_FILE_ENV = "EIMEMORY_ADAPTER_RECEIPT_HANDOFF_FILE"
MAX_RECEIPTS_PER_RUN = MAX_ELIGIBLE_RECEIPTS_PER_RUN
MAX_TOTAL_RECEIPTS = 2_048
_RECEIPT_ID = re.compile(r"[A-Za-z0-9._:-]{1,256}")


class ReceiptIdHandoff:
    """A bounded, untrusted local hint containing receipt IDs only."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    @classmethod
    def from_env(cls) -> ReceiptIdHandoff | None:
        path = str(os.environ.get(RECEIPT_HANDOFF_FILE_ENV) or "").strip()
        return cls(path) if path else None

    def append(
        self,
        *,
        channel: str,
        scope: Mapping[str, str],
        session_id: str,
        run_id: str,
        receipt_id: str,
    ) -> None:
        key = self._key(channel=channel, scope=scope, session_id=session_id, run_id=run_id)
        clean_id = str(receipt_id or "").strip()
        if not _RECEIPT_ID.fullmatch(clean_id):
            return
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "INSERT OR IGNORE INTO receipt_handoff (channel, scope_digest, session_id, run_id, receipt_id, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (*key, clean_id, datetime.now(timezone.utc).isoformat()),
            )
            conn.execute(
                "DELETE FROM receipt_handoff WHERE rowid IN (SELECT rowid FROM receipt_handoff WHERE channel = ? AND scope_digest = ? AND session_id = ? AND run_id = ? ORDER BY receipt_id DESC LIMIT -1 OFFSET ?)",
                (*key, MAX_RECEIPTS_PER_RUN),
            )
            conn.execute(
                "DELETE FROM receipt_handoff WHERE rowid IN (SELECT rowid FROM receipt_handoff ORDER BY created_at DESC, receipt_id DESC LIMIT -1 OFFSET ?)",
                (MAX_TOTAL_RECEIPTS,),
            )
            conn.commit()

    def list_ids(
        self,
        *,
        channel: str,
        scope: Mapping[str, str],
        session_id: str,
        run_id: str,
    ) -> list[str]:
        key = self._key(channel=channel, scope=scope, session_id=session_id, run_id=run_id)
        if not self.path.exists():
            return []
        with self._connect(create=False) as conn:
            rows = conn.execute(
                "SELECT receipt_id FROM receipt_handoff WHERE channel = ? AND scope_digest = ? AND session_id = ? AND run_id = ? ORDER BY receipt_id",
                key,
            ).fetchall()
        return [str(row[0]) for row in rows[:MAX_RECEIPTS_PER_RUN]]

    def clear_exact(
        self,
        *,
        channel: str,
        scope: Mapping[str, str],
        session_id: str,
        run_id: str,
        receipt_ids: list[str],
    ) -> None:
        clean_ids = list(dict.fromkeys(str(value or "").strip() for value in receipt_ids if str(value or "").strip()))
        if not clean_ids or not self.path.exists():
            return
        key = self._key(channel=channel, scope=scope, session_id=session_id, run_id=run_id)
        placeholders = ",".join("?" for _ in clean_ids)
        with self._connect(create=False) as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                f"DELETE FROM receipt_handoff WHERE channel = ? AND scope_digest = ? AND session_id = ? AND run_id = ? AND receipt_id IN ({placeholders})",
                (*key, *clean_ids),
            )
            conn.commit()

    def _connect(self, *, create: bool = True) -> sqlite3.Connection:
        if create:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            if os.name == "posix":
                self.path.parent.chmod(0o700)
            if not self.path.exists():
                try:
                    descriptor = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
                except FileExistsError:
                    pass
                else:
                    os.close(descriptor)
        if self.path.is_symlink():
            raise ValueError("receipt handoff file must not be a symlink")
        metadata = self.path.stat(follow_symlinks=False)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise ValueError("receipt handoff path must be one regular file")
        if os.name == "posix" and metadata.st_mode & 0o077:
            raise ValueError("receipt handoff file must be private")
        conn = sqlite3.connect(self.path, timeout=5)
        conn.execute("PRAGMA busy_timeout = 5000")
        conn.execute(
            "CREATE TABLE IF NOT EXISTS receipt_handoff (channel TEXT NOT NULL, scope_digest TEXT NOT NULL, session_id TEXT NOT NULL, run_id TEXT NOT NULL, receipt_id TEXT NOT NULL, created_at TEXT NOT NULL, PRIMARY KEY(channel, scope_digest, session_id, run_id, receipt_id))"
        )
        return conn

    @staticmethod
    def _key(
        *,
        channel: str,
        scope: Mapping[str, str],
        session_id: str,
        run_id: str,
    ) -> tuple[str, str, str, str]:
        channel_id = str(channel or "").strip().lower()[:32]
        session = str(session_id or "").strip()[:500]
        run = str(run_id or "").strip()[:500]
        stable_scope = json.dumps(
            {
                name: str(scope.get(name) or "").strip()
                for name in ("tenant_id", "agent_id", "workspace_id", "user_id")
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return channel_id, sha256(stable_scope.encode("utf-8")).hexdigest(), session, run
