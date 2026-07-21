from __future__ import annotations

from .postgres_vector import PostgresVectorConfig


DDL_VERSION = "postgres-vector-candidates.v1"


def build_candidate_projection_ddl(config: PostgresVectorConfig) -> tuple[str, ...]:
    """Return explicit, idempotent DDL for the candidate-only projection."""

    schema = f'"{config.schema}"'
    table = f'{schema}."{config.table}"'
    state_table = f'{schema}."{config.table}_sync_state"'
    migrations = f'{schema}."candidate_projection_migrations"'
    prefix = config.table[:38]
    return (
        "CREATE EXTENSION IF NOT EXISTS vector",
        f"CREATE SCHEMA IF NOT EXISTS {schema}",
        f"""
        CREATE TABLE IF NOT EXISTS {migrations} (
            version TEXT PRIMARY KEY,
            applied_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """.strip(),
        f"""
        CREATE TABLE IF NOT EXISTS {table} (
            storage_key TEXT PRIMARY KEY,
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
            payload_digest TEXT NOT NULL,
            authoritative_updated_at TIMESTAMPTZ NOT NULL,
            index_watermark TEXT NOT NULL,
            indexed_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
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
            completed_at TIMESTAMPTZ,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """.strip(),
        f"""
        INSERT INTO {state_table} (singleton) VALUES (TRUE)
        ON CONFLICT (singleton) DO NOTHING
        """.strip(),
        f"""
        CREATE INDEX IF NOT EXISTS "{prefix}_scope_status_idx"
        ON {table} (tenant_id, agent_id, workspace_id, user_id, source_id, status, kind)
        """.strip(),
        f"""
        CREATE INDEX IF NOT EXISTS "{prefix}_embedding_hnsw_idx"
        ON {table} USING hnsw (embedding vector_cosine_ops)
        """.strip(),
        f"""
        CREATE INDEX IF NOT EXISTS "{prefix}_search_gin_idx"
        ON {table} USING gin (search_tsv)
        """.strip(),
        f"""
        INSERT INTO {migrations} (version) VALUES ('{DDL_VERSION}')
        ON CONFLICT (version) DO NOTHING
        """.strip(),
    )
