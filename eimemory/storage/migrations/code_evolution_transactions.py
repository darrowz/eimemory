"""Forward-only schema for governed code-evolution transactions.

The transaction ledger is deliberately installed beside the existing SQLite
authority.  This module owns schema creation only: it never scans promotion
history and never creates a second state database or a JSON projection that
could become an effect authority.
"""

from __future__ import annotations

from datetime import datetime, timezone
import sqlite3
from typing import Any


CODE_EVOLUTION_SCHEMA_MIGRATION = "code.evolution.transactions.v1"
CODE_EVOLUTION_SCHEMA_VERSION = "code_evolution.v1"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _table_names(conn: sqlite3.Connection) -> set[str]:
    return {
        str(row[0])
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'code_evolution_%'"
        ).fetchall()
    }


_REQUIRED_TABLES = frozenset(
    {
        "code_evolution_transactions",
        "code_evolution_artifacts",
        "code_evolution_step_events",
        "code_evolution_verification_receipts",
        "code_evolution_policy_consumptions",
        "code_evolution_terminal_receipts",
    }
)


def _append_only_trigger_names() -> tuple[str, ...]:
    return (
        "trg_code_evolution_transactions_terminal_no_update",
        "trg_code_evolution_transactions_no_delete",
        "trg_code_evolution_transactions_terminal_requires_receipt",
        "trg_code_evolution_artifacts_no_update",
        "trg_code_evolution_artifacts_no_delete",
        "trg_code_evolution_step_events_no_update",
        "trg_code_evolution_step_events_no_delete",
        "trg_code_evolution_verification_receipts_no_update",
        "trg_code_evolution_verification_receipts_no_delete",
        "trg_code_evolution_policy_consumptions_no_update",
        "trg_code_evolution_policy_consumptions_no_delete",
        "trg_code_evolution_terminal_receipts_no_update",
        "trg_code_evolution_terminal_receipts_no_delete",
    )


def is_code_evolution_schema_ready(conn: sqlite3.Connection) -> bool:
    """Return whether the complete additive ledger schema is present."""

    try:
        if not _REQUIRED_TABLES.issubset(_table_names(conn)):
            return False
        migration = conn.execute(
            "SELECT 1 FROM schema_migrations WHERE migration_id=?",
            (CODE_EVOLUTION_SCHEMA_MIGRATION,),
        ).fetchone()
        if migration is None:
            return False
        triggers = {
            str(row[0])
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='trigger'"
            ).fetchall()
        }
        return set(_append_only_trigger_names()).issubset(triggers)
    except sqlite3.DatabaseError:
        return False


def ensure_code_evolution_schema(conn: sqlite3.Connection) -> None:
    """Create the normalized ledger without performing any data migration."""

    if is_code_evolution_schema_ready(conn):
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
            """
            CREATE TABLE IF NOT EXISTS code_evolution_transactions (
                transaction_id TEXT PRIMARY KEY,
                schema_version TEXT NOT NULL,
                idempotency_key TEXT NOT NULL UNIQUE,
                tenant_id TEXT NOT NULL,
                agent_id TEXT NOT NULL,
                workspace_id TEXT NOT NULL,
                user_id TEXT NOT NULL,
                incident_id TEXT NOT NULL,
                incident_digest TEXT NOT NULL,
                incident_class TEXT NOT NULL,
                origin TEXT NOT NULL,
                detector TEXT NOT NULL,
                source_evidence_json TEXT NOT NULL,
                known_before_detection INTEGER NOT NULL CHECK (known_before_detection IN (0, 1)),
                prior_user_reported INTEGER NOT NULL CHECK (prior_user_reported IN (0, 1)),
                manual_bootstrap INTEGER NOT NULL CHECK (manual_bootstrap IN (0, 1)),
                current_state TEXT NOT NULL,
                state_version INTEGER NOT NULL CHECK (state_version >= 0),
                terminal INTEGER NOT NULL CHECK (terminal IN (0, 1)),
                lease_owner TEXT NOT NULL DEFAULT '',
                lease_expires_at TEXT NOT NULL DEFAULT '',
                capability_id TEXT NOT NULL,
                revision_id TEXT NOT NULL,
                binding_id TEXT NOT NULL,
                provider_kind TEXT NOT NULL,
                provider_instance_id TEXT NOT NULL,
                implementation_digest TEXT NOT NULL,
                advertisement_id TEXT NOT NULL DEFAULT '',
                advertisement_digest TEXT NOT NULL DEFAULT '',
                catalog_case_id TEXT NOT NULL DEFAULT '',
                catalog_snapshot_digest TEXT NOT NULL DEFAULT '',
                repository_root TEXT NOT NULL,
                repository_remote TEXT NOT NULL,
                repository_ref TEXT NOT NULL,
                base_commit TEXT NOT NULL,
                base_tree_digest TEXT NOT NULL DEFAULT '',
                proposal_digest TEXT NOT NULL DEFAULT '',
                patch_digest TEXT NOT NULL DEFAULT '',
                candidate_tree_digest TEXT NOT NULL DEFAULT '',
                policy_digest TEXT NOT NULL DEFAULT '',
                authorization_digest TEXT NOT NULL DEFAULT '',
                candidate_commit TEXT NOT NULL DEFAULT '',
                prior_commit TEXT NOT NULL DEFAULT '',
                deployed_commit TEXT NOT NULL DEFAULT '',
                observation_started_at TEXT NOT NULL DEFAULT '',
                observation_deadline TEXT NOT NULL DEFAULT '',
                terminal_receipt_digest TEXT NOT NULL DEFAULT '',
                payload_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS code_evolution_artifacts (
                transaction_id TEXT NOT NULL,
                artifact_kind TEXT NOT NULL,
                artifact_schema TEXT NOT NULL,
                byte_count INTEGER NOT NULL CHECK (byte_count >= 0),
                sha256 TEXT NOT NULL,
                compressed_bytes BLOB NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY (transaction_id, artifact_kind),
                FOREIGN KEY (transaction_id) REFERENCES code_evolution_transactions(transaction_id)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS code_evolution_step_events (
                transaction_id TEXT NOT NULL,
                "sequence" INTEGER NOT NULL CHECK ("sequence" > 0),
                step TEXT NOT NULL,
                phase TEXT NOT NULL CHECK (phase IN ('intent', 'result', 'reconcile')),
                attempt INTEGER NOT NULL CHECK (attempt > 0),
                idempotency_key TEXT NOT NULL,
                from_state TEXT NOT NULL,
                to_state TEXT NOT NULL,
                input_digest TEXT NOT NULL DEFAULT '',
                output_digest TEXT NOT NULL DEFAULT '',
                artifact_digest TEXT NOT NULL DEFAULT '',
                evidence_digest TEXT NOT NULL DEFAULT '',
                summary TEXT NOT NULL,
                prior_event_digest TEXT NOT NULL DEFAULT '',
                event_digest TEXT NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY (transaction_id, "sequence"),
                UNIQUE (transaction_id, step, attempt, phase),
                FOREIGN KEY (transaction_id) REFERENCES code_evolution_transactions(transaction_id)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS code_evolution_verification_receipts (
                transaction_id TEXT NOT NULL,
                verification_kind TEXT NOT NULL CHECK (verification_kind IN ('focused', 'regression', 'full_suite')),
                base_commit TEXT NOT NULL,
                patch_digest TEXT NOT NULL,
                candidate_tree_digest TEXT NOT NULL,
                test_plan_id TEXT NOT NULL,
                test_plan_digest TEXT NOT NULL,
                command_digest TEXT NOT NULL,
                environment_digest TEXT NOT NULL,
                verifier_id TEXT NOT NULL,
                verifier_revision TEXT NOT NULL,
                started_at TEXT NOT NULL,
                finished_at TEXT NOT NULL,
                exit_status INTEGER NOT NULL,
                test_count INTEGER NOT NULL DEFAULT 0,
                passed_count INTEGER NOT NULL DEFAULT 0,
                failed_count INTEGER NOT NULL DEFAULT 0,
                skipped_count INTEGER NOT NULL DEFAULT 0,
                result TEXT NOT NULL,
                log_artifact_digest TEXT NOT NULL,
                receipt_digest TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY (transaction_id, verification_kind),
                UNIQUE (transaction_id, receipt_digest),
                FOREIGN KEY (transaction_id) REFERENCES code_evolution_transactions(transaction_id)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS code_evolution_policy_consumptions (
                policy_digest TEXT PRIMARY KEY,
                transaction_id TEXT NOT NULL UNIQUE,
                authorization_receipt_digest TEXT NOT NULL,
                consumed_at TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                FOREIGN KEY (transaction_id) REFERENCES code_evolution_transactions(transaction_id)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS code_evolution_terminal_receipts (
                transaction_id TEXT PRIMARY KEY,
                outcome TEXT NOT NULL CHECK (outcome IN (
                    'succeeded_sedimented',
                    'rolled_back_healthy',
                    'aborted_no_external_effect',
                    'aborted_candidate_restored',
                    'recovery_quarantined'
                )),
                incident_digest TEXT NOT NULL,
                provider_digest TEXT NOT NULL,
                policy_digest TEXT NOT NULL,
                authorization_digest TEXT NOT NULL,
                base_commit TEXT NOT NULL,
                candidate_commit TEXT NOT NULL DEFAULT '',
                deployed_commit TEXT NOT NULL DEFAULT '',
                observation_digest TEXT NOT NULL DEFAULT '',
                rollback_digest TEXT NOT NULL DEFAULT '',
                evidence_digest TEXT NOT NULL,
                receipt_digest TEXT NOT NULL UNIQUE,
                payload_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY (transaction_id) REFERENCES code_evolution_transactions(transaction_id)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS code_evolution_quarantine_resolutions (
                transaction_id TEXT PRIMARY KEY,
                evidence_digest TEXT NOT NULL UNIQUE,
                event_digest TEXT NOT NULL UNIQUE,
                created_at TEXT NOT NULL,
                FOREIGN KEY (transaction_id) REFERENCES code_evolution_transactions(transaction_id)
            )
            """
        )
        # Quarantine remains a hard repository/ref lock until an append-only
        # resolution proves that the uncertain external effect did not land.
        conn.execute("DROP INDEX IF EXISTS idx_code_evolution_nonterminal_repo_ref")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_code_evolution_repo_ref "
            "ON code_evolution_transactions(repository_root, repository_ref, created_at)"
        )
        conn.execute("DROP TRIGGER IF EXISTS trg_code_evolution_repository_lock")
        conn.execute(
            "CREATE TRIGGER trg_code_evolution_repository_lock "
            "BEFORE INSERT ON code_evolution_transactions WHEN EXISTS ("
            "SELECT 1 FROM code_evolution_transactions t WHERE "
            "t.repository_root=NEW.repository_root AND t.repository_ref=NEW.repository_ref AND ("
            "t.terminal=0 OR (t.current_state='RECOVERY_QUARANTINED' AND NOT EXISTS ("
            "SELECT 1 FROM code_evolution_quarantine_resolutions r "
            "WHERE r.transaction_id=t.transaction_id)))"
            ") BEGIN SELECT RAISE(ABORT, 'code evolution repository ref locked'); END"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_code_evolution_state_lease "
            "ON code_evolution_transactions(current_state, lease_expires_at, updated_at)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_code_evolution_incident "
            "ON code_evolution_transactions(incident_id, created_at)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_code_evolution_events_tx_seq "
            "ON code_evolution_step_events(transaction_id, \"sequence\")"
        )
        for table, stem in (
            ("code_evolution_artifacts", "artifacts"),
            ("code_evolution_step_events", "step_events"),
            ("code_evolution_verification_receipts", "verification_receipts"),
            ("code_evolution_policy_consumptions", "policy_consumptions"),
            ("code_evolution_terminal_receipts", "terminal_receipts"),
            ("code_evolution_quarantine_resolutions", "quarantine_resolutions"),
        ):
            conn.execute(
                f"CREATE TRIGGER IF NOT EXISTS trg_code_evolution_{stem}_no_update "
                f"BEFORE UPDATE ON {table} BEGIN SELECT RAISE(ABORT, 'code evolution append-only row'); END"
            )
            conn.execute(
                f"CREATE TRIGGER IF NOT EXISTS trg_code_evolution_{stem}_no_delete "
                f"BEFORE DELETE ON {table} BEGIN SELECT RAISE(ABORT, 'code evolution append-only row'); END"
            )
        conn.execute(
            "CREATE TRIGGER IF NOT EXISTS trg_code_evolution_transactions_terminal_no_update "
            "BEFORE UPDATE ON code_evolution_transactions WHEN OLD.terminal=1 "
            "BEGIN SELECT RAISE(ABORT, 'code evolution terminal transaction is immutable'); END"
        )
        conn.execute(
            "CREATE TRIGGER IF NOT EXISTS trg_code_evolution_transactions_no_delete "
            "BEFORE DELETE ON code_evolution_transactions "
            "BEGIN SELECT RAISE(ABORT, 'code evolution transaction is append-only'); END"
        )
        conn.execute(
            "CREATE TRIGGER IF NOT EXISTS trg_code_evolution_transactions_terminal_requires_receipt "
            "BEFORE UPDATE ON code_evolution_transactions "
            "WHEN OLD.terminal=0 AND NEW.terminal=1 AND NOT EXISTS ("
            "SELECT 1 FROM code_evolution_terminal_receipts WHERE transaction_id=OLD.transaction_id"
            ") BEGIN SELECT RAISE(ABORT, 'code evolution terminal receipt required'); END"
        )
        conn.execute(
            "INSERT OR IGNORE INTO schema_migrations(migration_id, applied_at) VALUES (?, ?)",
            (CODE_EVOLUTION_SCHEMA_MIGRATION, _utc_now()),
        )
        if owns_transaction:
            conn.commit()
    except Exception:
        if owns_transaction:
            conn.rollback()
        raise


def code_evolution_backfill_is_scheduled(_conn: sqlite3.Connection) -> bool:
    """Historical promotion rows are never backfilled into the new ledger."""

    return False


def apply_code_evolution_backfill_batch(
    _conn: sqlite3.Connection,
    *,
    batch_size: int,
    max_seconds: float,
    offline: bool = False,
) -> dict[str, Any]:
    return {
        "ok": True,
        "migration_id": "code.evolution.transactions.backfill.v1",
        "scheduled": False,
        "processed": 0,
        "batch_size": max(1, min(2_000, int(batch_size))),
        "max_seconds": max(0.001, min(60.0, float(max_seconds))),
        "offline": bool(offline),
        "reason": "legacy_promotion_evidence_is_not_qualifying",
    }


__all__ = [
    "CODE_EVOLUTION_SCHEMA_MIGRATION",
    "CODE_EVOLUTION_SCHEMA_VERSION",
    "apply_code_evolution_backfill_batch",
    "code_evolution_backfill_is_scheduled",
    "ensure_code_evolution_schema",
    "is_code_evolution_schema_ready",
]
