"""Forward-only Profile lineage schema for the dynamic capability registry.

The initial v3 table stores immutable profile revision descriptors keyed by
``profile_id``.  WP4 adds the separate logical ``profile_key`` index without
rewriting that released table or scanning historical payloads at startup.
Historical profile rows are deliberately backfilled only by a later bounded
migration; new registry writes populate both authorities in one transaction.
"""

from __future__ import annotations

from datetime import datetime, timezone
import sqlite3
from typing import Any


CAPABILITY_PROFILE_LINEAGE_SCHEMA_MIGRATION = "capability.v3.profile-lineage.v1"
CAPABILITY_PROFILE_LINEAGE_BACKFILL_MIGRATION = "capability.v3.profile-lineage.backfill.v1"

_SCOPE_COLUMNS: tuple[str, ...] = (
    "tenant_id",
    "agent_id",
    "workspace_id",
    "user_id",
    "capability_scope",
)
_SCOPE_DDL = ",\n".join(f"{column} TEXT NOT NULL" for column in _SCOPE_COLUMNS)
_SCOPE_KEY = ", ".join(_SCOPE_COLUMNS)
_TABLE = "capability_profile_lineage"
_INDEX = "idx_capability_profile_lineage_scope_key_effective"
_REQUIRED_COLUMNS = frozenset(
    {
        *_SCOPE_COLUMNS,
        "profile_key",
        "profile_id",
        "profile_revision",
        "profile_digest",
        "status",
        "effective_at",
        "created_at",
    }
)


class CapabilityProfileLineageSchemaError(RuntimeError):
    """The forward Profile-lineage schema is absent or drifted."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _marked(conn: sqlite3.Connection) -> bool:
    row = conn.execute(
        "SELECT 1 FROM schema_migrations WHERE migration_id=?",
        (CAPABILITY_PROFILE_LINEAGE_SCHEMA_MIGRATION,),
    ).fetchone()
    return row is not None


def _assert_schema(conn: sqlite3.Connection) -> None:
    columns = {
        str(row[1])
        for row in conn.execute(f"PRAGMA table_info({_TABLE})").fetchall()
    }
    missing = _REQUIRED_COLUMNS - columns
    if missing:
        raise CapabilityProfileLineageSchemaError(
            f"capability Profile lineage table is missing columns: {sorted(missing)}"
        )
    indexes = {
        str(row[0])
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='index'").fetchall()
    }
    if _INDEX not in indexes:
        raise CapabilityProfileLineageSchemaError("capability Profile lineage index is missing")
    targets = {
        str(row[2])
        for row in conn.execute(f"PRAGMA foreign_key_list({_TABLE})").fetchall()
    }
    if "capability_profiles" not in targets:
        raise CapabilityProfileLineageSchemaError("capability Profile lineage foreign key is missing")


def is_capability_profile_lineage_schema_ready(conn: sqlite3.Connection) -> bool:
    try:
        _assert_schema(conn)
    except (sqlite3.DatabaseError, CapabilityProfileLineageSchemaError):
        return False
    return True


def ensure_capability_profile_lineage_schema(conn: sqlite3.Connection) -> None:
    """Install only additive schema; never scan or backfill profile data."""

    if is_capability_profile_lineage_schema_ready(conn) and _marked(conn):
        return
    owns_transaction = not conn.in_transaction
    if owns_transaction:
        conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations (migration_id TEXT PRIMARY KEY, applied_at TEXT NOT NULL)"
        )
        conn.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {_TABLE} (
                {_SCOPE_DDL},
                profile_key TEXT NOT NULL,
                profile_id TEXT NOT NULL,
                profile_revision TEXT NOT NULL,
                profile_digest TEXT NOT NULL,
                status TEXT NOT NULL,
                effective_at TEXT NOT NULL,
                created_at TEXT NOT NULL,
                -- ``profile_id`` is the immutable revision identity.  The
                -- human-readable ``profile_revision`` is descriptive only:
                -- older callers may legitimately use the same value (for
                -- example ``v1``) for independent immutable profile ids.
                PRIMARY KEY ({_SCOPE_KEY}, profile_id),
                FOREIGN KEY ({_SCOPE_KEY}, profile_id)
                  REFERENCES capability_profiles ({_SCOPE_KEY}, profile_id)
            )
            """
        )
        conn.execute(
            f"CREATE INDEX IF NOT EXISTS {_INDEX} "
            f"ON {_TABLE} ({_SCOPE_KEY}, profile_key, status, effective_at DESC, profile_id)"
        )
        conn.execute(
            "INSERT OR IGNORE INTO schema_migrations(migration_id, applied_at) VALUES (?, ?)",
            (CAPABILITY_PROFILE_LINEAGE_SCHEMA_MIGRATION, _utc_now()),
        )
        _assert_schema(conn)
        if owns_transaction:
            conn.commit()
    except Exception:
        if owns_transaction:
            conn.rollback()
        raise


def capability_profile_lineage_backfill_is_scheduled(_conn: sqlite3.Connection) -> bool:
    """Historical payload scans are owned by a later bounded migration."""

    return False


def apply_capability_profile_lineage_backfill_batch(
    _conn: sqlite3.Connection,
    *,
    batch_size: int,
    max_seconds: float,
    offline: bool = False,
) -> dict[str, Any]:
    """Report the deliberately unscheduled historical backfill boundary."""

    return {
        "ok": True,
        "migration_id": CAPABILITY_PROFILE_LINEAGE_BACKFILL_MIGRATION,
        "scheduled": False,
        "processed": 0,
        "batch_size": max(1, min(2_000, int(batch_size))),
        "max_seconds": max(0.001, min(60.0, float(max_seconds))),
        "offline": bool(offline),
        "reason": "profile_lineage_backfill_not_scheduled",
    }
