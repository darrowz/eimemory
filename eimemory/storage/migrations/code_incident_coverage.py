"""Additive metadata chronology for real records/events; no historical backfill."""
from __future__ import annotations

from datetime import datetime, timezone
import sqlite3

MIGRATION_ID = 'code.incident.coverage.v1'
JOURNAL = 'code_incident_coverage_journal'
CHECKPOINTS = 'code_incident_coverage_checkpoints'
OBSERVATIONS = 'code_incident_coverage_observations'


def index_definitions() -> dict[str, str]:
    return {
        'idx_incident_coverage_scope_sequence': f'CREATE INDEX idx_incident_coverage_scope_sequence ON {JOURNAL}(tenant_id,agent_id,workspace_id,user_id,sequence)',
        'idx_incident_coverage_control_sequence': f'CREATE INDEX idx_incident_coverage_control_sequence ON {JOURNAL}(table_name,sequence)',
        'idx_incident_coverage_record_version': f'CREATE INDEX idx_incident_coverage_record_version ON {JOURNAL}(tenant_id,agent_id,workspace_id,user_id,record_id,payload_digest,table_name,operation,sequence)',
        'idx_incident_coverage_checkpoint_scope': f'CREATE INDEX idx_incident_coverage_checkpoint_scope ON {CHECKPOINTS}(tenant_id,agent_id,workspace_id,user_id,sequence DESC)',
        'idx_incident_coverage_checkpoint_digest': f'CREATE UNIQUE INDEX idx_incident_coverage_checkpoint_digest ON {CHECKPOINTS}(payload_digest)',
        'idx_incident_coverage_observation_ref': f'CREATE INDEX idx_incident_coverage_observation_ref ON {OBSERVATIONS}(tenant_id,agent_id,workspace_id,user_id,record_id,sequence DESC)',
    }


def _extract(prefix: str, path: str) -> str:
    return f"CASE WHEN json_valid({prefix}.payload_json) THEN json_extract({prefix}.payload_json, '{path}') END"


def trigger_definitions() -> dict[str, str]:
    definitions = {}
    for table in ('records', 'events'):
        for operation, prefix in (('INSERT', 'NEW'), ('UPDATE', 'OLD'), ('DELETE', 'OLD')):
            key = 'storage_key' if table == 'records' else 'id'
            rid = 'record_id' if table == 'records' else 'id'
            kind = f'{prefix}.kind' if table == 'records' else "'event'"
            payload_digest = f'{prefix}.payload_digest' if table == 'records' else "''"
            occurred = _extract(prefix, '$.time.occurred_at') if table == 'records' else f'{prefix}.timestamp'
            origin = _extract(prefix, '$.provenance.origin')
            incident_class = _extract(prefix, '$.content.incident_class')
            commit = _extract(prefix, '$.content.detector_report.release_commit')
            expected = _extract(prefix, '$.content.detector_report.expected_commit')
            maintenance = _extract(prefix, '$.content.maintenance_report.base_commit')
            codes = _extract(prefix, '$.content.diagnostic_codes')
            human = (f"CASE WHEN coalesce({origin},'')='user_reported' OR {prefix}.source='eimemory.code_maintenance' THEN 1 ELSE 0 END"
                     if table == 'records' else f'CASE WHEN length({prefix}.user_phrase)>0 THEN 1 ELSE 0 END')
            opaque = (f"CASE WHEN {prefix}.payload_pointer_json<>'' OR NOT json_valid({prefix}.payload_json) THEN 1 ELSE 0 END"
                      if table == 'records' else '1')
            name = f'trg_code_incident_coverage_{table}_{operation.lower()}'
            timing = 'BEFORE' if operation == 'UPDATE' else 'AFTER'
            definitions[name] = f'''CREATE TRIGGER {name} {timing} {operation} ON {table}
            BEGIN INSERT INTO {JOURNAL} (table_name, operation, subject_key, record_id, tenant_id, agent_id,
                workspace_id, user_id, source, record_kind, payload_digest, source_time, observed_at,
                incident_class, release_commit, codes_json, origin, human_input, opaque_payload)
            VALUES ('{table}', '{operation}', {prefix}.{key}, {prefix}.{rid}, {prefix}.tenant_id, {prefix}.agent_id,
                {prefix}.workspace_id, {prefix}.user_id, {prefix}.source, {kind}, {payload_digest},
                coalesce({occurred},{prefix}.created_at), strftime('%Y-%m-%dT%H:%M:%fZ','now'),
                substr(coalesce({incident_class},''),1,201), substr(coalesce({commit},{expected},{maintenance},''),1,41),
                substr(coalesce({codes},'[]'),1,8193), substr(coalesce({origin},''),1,100), {human}, {opaque}); END'''
        # UPDATE preserves the original metadata above and the new version here.
        original = definitions[f'trg_code_incident_coverage_{table}_insert']
        name = f'trg_code_incident_coverage_{table}_updated_version'
        definitions[name] = original.replace(f'trg_code_incident_coverage_{table}_insert AFTER INSERT', f'{name} AFTER UPDATE').replace("'INSERT'", "'UPDATED_VERSION'")
    for table in (JOURNAL, CHECKPOINTS, OBSERVATIONS):
        for operation in ('UPDATE', 'DELETE'):
            name = f'trg_{table}_no_{operation.lower()}'
            definitions[name] = f"CREATE TRIGGER {name} BEFORE {operation} ON {table} BEGIN SELECT RAISE(ABORT,'incident coverage is append-only'); END"
    return definitions


def _normalized(value: str) -> str:
    # Preserve literal case and whitespace: changing 'user_reported' into
    # 'USER_REPORTED' changes the capture's meaning even if SQL keywords are
    # case-insensitive. SQLite retains our CREATE definitions verbatim.
    return value.strip().rstrip(';')


def schema_ready(conn: sqlite3.Connection) -> bool:
    try:
        if conn.execute('SELECT 1 FROM schema_migrations WHERE migration_id=?', (MIGRATION_ID,)).fetchone() is None:
            return False
        definitions = {str(row[0]): _normalized(str(row[1] or '')) for row in conn.execute("SELECT name,sql FROM sqlite_master WHERE type='trigger'")}
        if not all(definitions.get(name) == _normalized(sql) for name, sql in trigger_definitions().items()):
            return False
        indexes = {str(row[0]): _normalized(str(row[1] or '')) for row in conn.execute("SELECT name,sql FROM sqlite_master WHERE type='index'")}
        return all(indexes.get(name) == _normalized(sql) for name, sql in index_definitions().items())
    except sqlite3.DatabaseError:
        return False


def ensure_schema(conn: sqlite3.Connection) -> None:
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    # Capability-only isolated stores do not have the ingress tables yet.
    if not {'records', 'events', 'schema_migrations'} <= tables:
        return
    installed = conn.execute('SELECT 1 FROM schema_migrations WHERE migration_id=?', (MIGRATION_ID,)).fetchone() is not None
    if installed and schema_ready(conn):
        return
    owns = not conn.in_transaction
    if owns:
        conn.execute('BEGIN IMMEDIATE')
    try:
        conn.execute(f'''CREATE TABLE IF NOT EXISTS {JOURNAL} (
            sequence INTEGER PRIMARY KEY AUTOINCREMENT, table_name TEXT NOT NULL, operation TEXT NOT NULL,
            subject_key TEXT NOT NULL, record_id TEXT NOT NULL, tenant_id TEXT NOT NULL, agent_id TEXT NOT NULL,
            workspace_id TEXT NOT NULL, user_id TEXT NOT NULL, source TEXT NOT NULL, record_kind TEXT NOT NULL,
            payload_digest TEXT NOT NULL, source_time TEXT NOT NULL, observed_at TEXT NOT NULL,
            incident_class TEXT NOT NULL, release_commit TEXT NOT NULL, codes_json TEXT NOT NULL,
            origin TEXT NOT NULL, human_input INTEGER NOT NULL, opaque_payload INTEGER NOT NULL)''')
        for table in (CHECKPOINTS, OBSERVATIONS):
            conn.execute(f'''CREATE TABLE IF NOT EXISTS {table} (sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                tenant_id TEXT NOT NULL, agent_id TEXT NOT NULL, workspace_id TEXT NOT NULL, user_id TEXT NOT NULL,
                record_id TEXT NOT NULL DEFAULT '', payload_json TEXT NOT NULL, payload_digest TEXT NOT NULL,
                created_at TEXT NOT NULL)''')
        existing_indexes = {str(row[0]): _normalized(str(row[1] or '')) for row in conn.execute("SELECT name,sql FROM sqlite_master WHERE type='index'")}
        for name, sql in index_definitions().items():
            if existing_indexes.get(name) != _normalized(sql):
                conn.execute(f'DROP INDEX IF EXISTS {name}')
                conn.execute(sql)
        existing_triggers = {str(row[0]): _normalized(str(row[1] or '')) for row in conn.execute("SELECT name,sql FROM sqlite_master WHERE type='trigger'")}
        capture_gap = installed and any(existing_triggers.get(name) != _normalized(sql) for name, sql in trigger_definitions().items())
        for name, sql in trigger_definitions().items():
            if existing_triggers.get(name) != _normalized(sql):
                conn.execute(f'DROP TRIGGER IF EXISTS {name}')
                conn.execute(sql)
        if capture_gap:
            # Repair schema without retroactively closing the unobserved gap.
            conn.execute(f'''INSERT INTO {JOURNAL}(table_name,operation,subject_key,record_id,tenant_id,agent_id,workspace_id,user_id,
                source,record_kind,payload_digest,source_time,observed_at,incident_class,release_commit,codes_json,origin,human_input,opaque_payload)
                VALUES('coverage_control','CAPTURE_GAP','schema','schema','','','','','native_schema_repair','coverage_gap','',
                strftime('%Y-%m-%dT%H:%M:%fZ','now'),strftime('%Y-%m-%dT%H:%M:%fZ','now'),'','','[]','',0,1)''')
        conn.execute('INSERT OR IGNORE INTO schema_migrations(migration_id,applied_at) VALUES(?,?)', (MIGRATION_ID, datetime.now(timezone.utc).isoformat()))
        if not schema_ready(conn):
            raise RuntimeError('incident coverage schema verification failed')
        if owns:
            conn.commit()
    except BaseException:
        if owns:
            conn.rollback()
        raise


def data_is_scheduled(_conn):
    return False


def apply_data_batch(_conn, **_kwargs):
    return dict(ok=True, migration_id=MIGRATION_ID, scheduled=False, processed=0, remaining=0)
