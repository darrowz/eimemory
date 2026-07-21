from __future__ import annotations

from .postgres_vector import PROJECTION_DIGEST_SCHEMA, PostgresVectorConfig, _derived_identifier


DDL_VERSION = "postgres-vector-candidates.v2"


def build_candidate_projection_ddl(config: PostgresVectorConfig) -> tuple[str, ...]:
    """Return explicit, idempotent DDL for the candidate-only projection."""

    schema = f'"{config.schema}"'
    table = f'{schema}."{config.table}"'
    state_table = f'{schema}."{_derived_identifier(config, "state")}"'
    migrations = f'{schema}."{_derived_identifier(config, "migrations")}"'
    scope_index = _derived_identifier(config, "scope")
    hnsw_index = _derived_identifier(config, "hnsw")
    gin_index = _derived_identifier(config, "gin")
    return (
        "CREATE EXTENSION IF NOT EXISTS vector",
        f"CREATE SCHEMA IF NOT EXISTS {schema}",
        f"""
        DO $eimemory_v1_upgrade$
        DECLARE
            v1_present BOOLEAN := FALSE;
        BEGIN
            IF to_regclass('{config.schema}.candidate_projection_migrations') IS NOT NULL THEN
                EXECUTE 'SELECT EXISTS (SELECT 1 FROM {schema}."candidate_projection_migrations" '
                        'WHERE version = ''postgres-vector-candidates.v1'')'
                INTO v1_present;
                IF v1_present THEN
                    DROP TABLE IF EXISTS {schema}."{config.table}_sync_state";
                    DROP TABLE IF EXISTS {table};
                    DROP TABLE IF EXISTS {schema}."candidate_projection_migrations";
                END IF;
            END IF;
        END
        $eimemory_v1_upgrade$
        """.strip(),
        f"""
        CREATE TABLE IF NOT EXISTS {migrations} (
            version TEXT PRIMARY KEY,
            applied_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """.strip(),
        f"""
        CREATE TABLE IF NOT EXISTS {table} (
            storage_key TEXT NOT NULL,
            record_id TEXT NOT NULL,
            tenant_id TEXT NOT NULL,
            agent_id TEXT NOT NULL,
            workspace_id TEXT NOT NULL,
            user_id TEXT NOT NULL,
            source_id TEXT NOT NULL,
            kind TEXT NOT NULL,
            status TEXT NOT NULL,
            embedding vector({config.vector_dimension}),
            title_text TEXT NOT NULL DEFAULT '',
            alias_text TEXT NOT NULL DEFAULT '',
            keyword_text TEXT NOT NULL DEFAULT '',
            search_tsv TSVECTOR NOT NULL DEFAULT ''::tsvector,
            projection_digest TEXT NOT NULL CHECK (projection_digest ~ '^[0-9a-f]{{64}}$'),
            projection_digest_schema TEXT NOT NULL DEFAULT '{PROJECTION_DIGEST_SCHEMA}',
            authoritative_updated_at TIMESTAMPTZ NOT NULL,
            index_watermark TEXT NOT NULL,
            indexed_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (storage_key, index_watermark)
        )
        """.strip(),
        f"""
        CREATE TABLE IF NOT EXISTS {state_table} (
            singleton BOOLEAN PRIMARY KEY DEFAULT TRUE CHECK (singleton),
            ready BOOLEAN NOT NULL DEFAULT FALSE,
            in_progress BOOLEAN NOT NULL DEFAULT FALSE,
            run_id TEXT NOT NULL DEFAULT '',
            cursor_updated_at TEXT NOT NULL DEFAULT '',
            cursor_storage_key TEXT NOT NULL DEFAULT '',
            committed_watermark TEXT NOT NULL DEFAULT '',
            authoritative_updated_at TIMESTAMPTZ,
            authoritative_storage_key TEXT NOT NULL DEFAULT '',
            completed_at TIMESTAMPTZ,
            embedding_fingerprint TEXT NOT NULL DEFAULT '',
            projection_digest_schema TEXT NOT NULL DEFAULT '{PROJECTION_DIGEST_SCHEMA}',
            projection_fingerprint TEXT NOT NULL DEFAULT '',
            lease_owner TEXT NOT NULL DEFAULT '',
            lease_expires_at TIMESTAMPTZ,
            authority_revision TEXT NOT NULL DEFAULT '',
            staging_embedding_fingerprint TEXT NOT NULL DEFAULT '',
            staging_projection_digest_schema TEXT NOT NULL DEFAULT '',
            staging_projection_fingerprint TEXT NOT NULL DEFAULT '',
            staging_authority_revision TEXT NOT NULL DEFAULT '',
            updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """.strip(),
        f"""
        INSERT INTO {state_table} (singleton) VALUES (TRUE)
        ON CONFLICT (singleton) DO NOTHING
        """.strip(),
        f"""
        CREATE INDEX IF NOT EXISTS "{scope_index}"
        ON {table} (tenant_id, agent_id, workspace_id, user_id, source_id, status, kind)
        """.strip(),
        f"""
        CREATE INDEX IF NOT EXISTS "{hnsw_index}"
        ON {table} USING hnsw (embedding vector_cosine_ops)
        """.strip(),
        f"""
        CREATE INDEX IF NOT EXISTS "{gin_index}"
        ON {table} USING gin (search_tsv)
        """.strip(),
        f"""
        INSERT INTO {migrations} (version) VALUES ('{DDL_VERSION}')
        ON CONFLICT (version) DO NOTHING
        """.strip(),
    )
