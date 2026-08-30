"""Forward-only storage migration registry.

The legacy record-store migrations predate the normalized capability schema and
are deliberately kept separate.  New migrations are registered here so schema
creation and bounded data migration cannot accidentally become part of runtime
startup behavior.
"""

from __future__ import annotations

from dataclasses import dataclass
import sqlite3
from typing import Any, Callable

from .capability_v3 import (
    CAPABILITY_V3_SCHEMA_MIGRATION,
    apply_capability_v3_backfill_batch,
    capability_v3_backfill_is_scheduled,
    ensure_capability_v3_schema,
    is_capability_v3_schema_ready,
)
from .capability_profile_lineage import (
    CAPABILITY_PROFILE_LINEAGE_SCHEMA_MIGRATION,
    apply_capability_profile_lineage_backfill_batch,
    capability_profile_lineage_backfill_is_scheduled,
    ensure_capability_profile_lineage_schema,
    is_capability_profile_lineage_schema_ready,
)
from .code_evolution_transactions import (
    CODE_EVOLUTION_SCHEMA_MIGRATION,
    apply_code_evolution_backfill_batch,
    code_evolution_backfill_is_scheduled,
    ensure_code_evolution_schema,
    is_code_evolution_schema_ready,
)
from .code_evolution_quarantine_resolution import (
    CODE_EVOLUTION_QUARANTINE_RESOLUTION_MIGRATION,
    apply_code_evolution_quarantine_resolution_backfill_batch,
    code_evolution_quarantine_resolution_backfill_is_scheduled,
    ensure_code_evolution_quarantine_resolution_schema,
    is_code_evolution_quarantine_resolution_schema_ready,
)


@dataclass(frozen=True, slots=True)
class StorageMigrationSpec:
    """A forward-only storage migration registered with the record store."""

    migration_id: str
    ensure_schema: Callable[[sqlite3.Connection], None]
    is_schema_ready: Callable[[sqlite3.Connection], bool]
    data_is_scheduled: Callable[[sqlite3.Connection], bool]
    apply_data_batch: Callable[..., dict[str, Any]]


REGISTERED_STORAGE_MIGRATIONS: tuple[StorageMigrationSpec, ...] = (
    StorageMigrationSpec(
        migration_id=CAPABILITY_V3_SCHEMA_MIGRATION,
        ensure_schema=ensure_capability_v3_schema,
        is_schema_ready=is_capability_v3_schema_ready,
        data_is_scheduled=capability_v3_backfill_is_scheduled,
        apply_data_batch=apply_capability_v3_backfill_batch,
    ),
    StorageMigrationSpec(
        migration_id=CAPABILITY_PROFILE_LINEAGE_SCHEMA_MIGRATION,
        ensure_schema=ensure_capability_profile_lineage_schema,
        is_schema_ready=is_capability_profile_lineage_schema_ready,
        data_is_scheduled=capability_profile_lineage_backfill_is_scheduled,
        apply_data_batch=apply_capability_profile_lineage_backfill_batch,
    ),
    StorageMigrationSpec(
        migration_id=CODE_EVOLUTION_SCHEMA_MIGRATION,
        ensure_schema=ensure_code_evolution_schema,
        is_schema_ready=is_code_evolution_schema_ready,
        data_is_scheduled=code_evolution_backfill_is_scheduled,
        apply_data_batch=apply_code_evolution_backfill_batch,
    ),
    StorageMigrationSpec(
        migration_id=CODE_EVOLUTION_QUARANTINE_RESOLUTION_MIGRATION,
        ensure_schema=ensure_code_evolution_quarantine_resolution_schema,
        is_schema_ready=is_code_evolution_quarantine_resolution_schema_ready,
        data_is_scheduled=code_evolution_quarantine_resolution_backfill_is_scheduled,
        apply_data_batch=apply_code_evolution_quarantine_resolution_backfill_batch,
    ),
)


def ensure_registered_storage_schema(conn: sqlite3.Connection) -> None:
    """Install registered schema only; never run data backfill at startup."""

    for migration in REGISTERED_STORAGE_MIGRATIONS:
        migration.ensure_schema(conn)


def pending_registered_data_migrations(conn: sqlite3.Connection) -> list[str]:
    """Return only explicitly scheduled bounded data migrations."""

    return [
        migration.migration_id
        for migration in REGISTERED_STORAGE_MIGRATIONS
        if migration.data_is_scheduled(conn)
    ]


def apply_registered_data_migration_batch(
    conn: sqlite3.Connection,
    *,
    batch_size: int,
    max_seconds: float,
    offline: bool,
) -> dict[str, Any] | None:
    """Run at most one registered, explicitly scheduled data migration batch."""

    for migration in REGISTERED_STORAGE_MIGRATIONS:
        if migration.data_is_scheduled(conn):
            return migration.apply_data_batch(
                conn,
                batch_size=batch_size,
                max_seconds=max_seconds,
                offline=offline,
            )
    return None
