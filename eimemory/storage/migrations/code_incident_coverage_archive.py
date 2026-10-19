"""Add signed cold-prefix pointers; never backfill or prune at startup."""
from __future__ import annotations

from datetime import datetime, timezone
import sqlite3

MIGRATION_ID = 'code.incident.coverage.archive.v1'
ARCHIVES = 'code_incident_coverage_archives'
KEYS = 'code_incident_coverage_archive_keys'


def indexes():
    return {
        'idx_incident_archive_range': f'CREATE UNIQUE INDEX idx_incident_archive_range ON {ARCHIVES}(table_name,first_sequence,last_sequence)',
        'idx_incident_archive_end': f'CREATE INDEX idx_incident_archive_end ON {ARCHIVES}(table_name,last_sequence)',
        'idx_incident_archive_digest': f'CREATE UNIQUE INDEX idx_incident_archive_digest ON {ARCHIVES}(payload_digest)',
        'idx_incident_archive_key_scope': f'CREATE INDEX idx_incident_archive_key_scope ON {KEYS}(table_name,tenant_id,agent_id,workspace_id,user_id,record_id,original_sequence DESC)',
        'idx_incident_archive_key_head': f'CREATE INDEX idx_incident_archive_key_head ON {KEYS}(table_name,tenant_id,agent_id,workspace_id,user_id,original_sequence DESC)',
        'idx_incident_archive_key_digest': f'CREATE INDEX idx_incident_archive_key_digest ON {KEYS}(table_name,payload_digest)',
    }


def triggers():
    return {f'trg_{table}_no_{operation.lower()}':
        f"CREATE TRIGGER trg_{table}_no_{operation.lower()} BEFORE {operation} ON {table} BEGIN SELECT RAISE(ABORT,'incident archive index is append-only'); END"
        for table in (ARCHIVES, KEYS) for operation in ('UPDATE', 'DELETE')}


def schema_ready(conn):
    try:
        if conn.execute('SELECT 1 FROM schema_migrations WHERE migration_id=?', (MIGRATION_ID,)).fetchone() is None:
            return False
        found = {row[0]: str(row[1] or '').strip().rstrip(';') for row in conn.execute(
            "SELECT name,sql FROM sqlite_master WHERE type IN ('index','trigger')")}
        return all(found.get(name) == sql for name, sql in {**indexes(), **triggers()}.items())
    except sqlite3.DatabaseError:
        return False


def ensure_schema(conn):
    owns = not conn.in_transaction
    if owns: conn.execute('BEGIN IMMEDIATE')
    try:
        conn.execute(f'''CREATE TABLE IF NOT EXISTS {ARCHIVES} (
            sequence INTEGER PRIMARY KEY AUTOINCREMENT, table_name TEXT NOT NULL,
            first_sequence INTEGER NOT NULL, last_sequence INTEGER NOT NULL,
            row_count INTEGER NOT NULL, payload_json TEXT NOT NULL,
            payload_digest TEXT NOT NULL, created_at TEXT NOT NULL)''')
        conn.execute(f'''CREATE TABLE IF NOT EXISTS {KEYS} (
            sequence INTEGER PRIMARY KEY AUTOINCREMENT, table_name TEXT NOT NULL,
            tenant_id TEXT NOT NULL, agent_id TEXT NOT NULL, workspace_id TEXT NOT NULL,
            user_id TEXT NOT NULL, record_id TEXT NOT NULL, payload_digest TEXT NOT NULL,
            original_sequence INTEGER NOT NULL, archive_sequence INTEGER NOT NULL)''')
        for kind, definitions in (('index', indexes()), ('trigger', triggers())):
            current = {row[0]: str(row[1] or '').strip().rstrip(';') for row in conn.execute(
                'SELECT name,sql FROM sqlite_master WHERE type=?', (kind,))}
            for name, sql in definitions.items():
                if current.get(name) != sql:
                    conn.execute(f'DROP {kind.upper()} IF EXISTS {name}')
                    conn.execute(sql)
        conn.execute('INSERT OR IGNORE INTO schema_migrations(migration_id,applied_at) VALUES(?,?)',
            (MIGRATION_ID, datetime.now(timezone.utc).isoformat()))
        if not schema_ready(conn): raise RuntimeError('incident archive schema verification failed')
        if owns: conn.commit()
    except BaseException:
        if owns: conn.rollback()
        raise


def data_is_scheduled(_conn): return False


def apply_data_batch(_conn, **_kwargs):
    return dict(ok=True, migration_id=MIGRATION_ID, scheduled=False, processed=0, remaining=0)
