"""Forward-only append-only resolution ledger for repository quarantine locks."""

from __future__ import annotations

from datetime import datetime, timezone
import sqlite3
from typing import Any


CODE_EVOLUTION_QUARANTINE_RESOLUTION_MIGRATION = "code-evolution.quarantine-resolution.v1"
_TABLE = "code_evolution_quarantine_resolutions"
_INDEX = "idx_code_evolution_repo_ref"
_TRIGGER = "trg_code_evolution_repository_lock"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _marked(conn: sqlite3.Connection) -> bool:
    return conn.execute(
        "SELECT 1 FROM schema_migrations WHERE migration_id=?",
        (CODE_EVOLUTION_QUARANTINE_RESOLUTION_MIGRATION,),
    ).fetchone() is not None


def is_code_evolution_quarantine_resolution_schema_ready(conn: sqlite3.Connection) -> bool:
    table = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (_TABLE,)
    ).fetchone()
    index = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='index' AND name=?", (_INDEX,)
    ).fetchone()
    trigger = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='trigger' AND name=?", (_TRIGGER,)
    ).fetchone()
    return table is not None and index is not None and trigger is not None


def ensure_code_evolution_quarantine_resolution_schema(conn: sqlite3.Connection) -> None:
    if is_code_evolution_quarantine_resolution_schema_ready(conn) and _marked(conn):
        return
    owns_transaction = not conn.in_transaction
    if owns_transaction:
        conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations "
            "(migration_id TEXT PRIMARY KEY, applied_at TEXT NOT NULL)"
        )
        conn.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {_TABLE} (
                transaction_id TEXT PRIMARY KEY,
                evidence_digest TEXT NOT NULL UNIQUE,
                event_digest TEXT NOT NULL UNIQUE,
                created_at TEXT NOT NULL,
                FOREIGN KEY (transaction_id)
                  REFERENCES code_evolution_transactions(transaction_id)
            )
            """
        )
        conn.execute("DROP INDEX IF EXISTS idx_code_evolution_nonterminal_repo_ref")
        conn.execute(
            f"CREATE INDEX IF NOT EXISTS {_INDEX} "
            "ON code_evolution_transactions(repository_root, repository_ref, created_at)"
        )
        conn.execute(f"DROP TRIGGER IF EXISTS {_TRIGGER}")
        conn.execute(
            f"CREATE TRIGGER {_TRIGGER} BEFORE INSERT ON code_evolution_transactions "
            "WHEN EXISTS (SELECT 1 FROM code_evolution_transactions t WHERE "
            "t.repository_root=NEW.repository_root AND t.repository_ref=NEW.repository_ref AND ("
            "t.terminal=0 OR (t.current_state='RECOVERY_QUARANTINED' AND NOT EXISTS ("
            f"SELECT 1 FROM {_TABLE} r WHERE r.transaction_id=t.transaction_id)))) "
            "BEGIN SELECT RAISE(ABORT, 'code evolution repository ref locked'); END"
        )
        for suffix, operation in (("no_update", "UPDATE"), ("no_delete", "DELETE")):
            conn.execute(
                f"CREATE TRIGGER IF NOT EXISTS trg_code_evolution_quarantine_resolutions_{suffix} "
                f"BEFORE {operation} ON {_TABLE} "
                "BEGIN SELECT RAISE(ABORT, 'code evolution append-only row'); END"
            )
        conn.execute(
            "INSERT OR IGNORE INTO schema_migrations(migration_id, applied_at) VALUES (?, ?)",
            (CODE_EVOLUTION_QUARANTINE_RESOLUTION_MIGRATION, _utc_now()),
        )
        if not is_code_evolution_quarantine_resolution_schema_ready(conn):
            raise RuntimeError("code evolution quarantine resolution schema unavailable")
        if owns_transaction:
            conn.commit()
    except Exception:
        if owns_transaction:
            conn.rollback()
        raise


def code_evolution_quarantine_resolution_backfill_is_scheduled(_conn: sqlite3.Connection) -> bool:
    return False


def apply_code_evolution_quarantine_resolution_backfill_batch(
    _conn: sqlite3.Connection,
    *,
    batch_size: int,
    max_seconds: float,
    offline: bool = False,
) -> dict[str, Any]:
    return {
        "ok": True,
        "migration_id": CODE_EVOLUTION_QUARANTINE_RESOLUTION_MIGRATION,
        "scheduled": False,
        "processed": 0,
        "remaining": 0,
        "offline": bool(offline),
    }
