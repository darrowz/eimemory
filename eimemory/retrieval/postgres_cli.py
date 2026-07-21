from __future__ import annotations

import os
from typing import Any

from .postgres_sync import PostgresVectorIndexSynchronizer, SQLiteProjectionReader
from .postgres_vector import (
    IndexState,
    OpenAICompatibleEmbeddingProvider,
    PostgresCandidateRepository,
    PostgresVectorConfig,
)


def config_from_env() -> PostgresVectorConfig:
    provider = _embedding_provider_from_env()
    return PostgresVectorConfig(
        enabled=_env_flag("EIMEMORY_POSTGRES_VECTOR_ENABLED"),
        dsn=os.environ.get("EIMEMORY_POSTGRES_VECTOR_DSN", ""),
        embedding_provider=provider,
        connect_timeout_seconds=_env_float("EIMEMORY_POSTGRES_CONNECT_TIMEOUT_SECONDS", 2.0),
        statement_timeout_ms=_env_int("EIMEMORY_POSTGRES_STATEMENT_TIMEOUT_MS", 1_500),
        pool_size=_env_int("EIMEMORY_POSTGRES_POOL_SIZE", 2),
        queue_bound=_env_int("EIMEMORY_POSTGRES_QUEUE_BOUND", 16),
        vector_dimension=_env_int("EIMEMORY_POSTGRES_VECTOR_DIMENSION", 1_536),
        schema=os.environ.get("EIMEMORY_POSTGRES_VECTOR_SCHEMA", "eimemory_recall"),
        table=os.environ.get("EIMEMORY_POSTGRES_VECTOR_TABLE", "vector_candidates"),
        max_index_lag_seconds=_env_float("EIMEMORY_POSTGRES_MAX_INDEX_LAG_SECONDS", 300.0),
        failure_threshold=_env_int("EIMEMORY_POSTGRES_FAILURE_THRESHOLD", 3),
        cooldown_seconds=_env_float("EIMEMORY_POSTGRES_COOLDOWN_SECONDS", 30.0),
        top_k_max=_env_int("EIMEMORY_POSTGRES_TOP_K_MAX", 100),
        cache_entries=_env_int("EIMEMORY_POSTGRES_CACHE_ENTRIES", 128),
        cache_ttl_seconds=_env_float("EIMEMORY_POSTGRES_CACHE_TTL_SECONDS", 10.0),
        release_id=os.environ.get("EIMEMORY_RUNTIME_COMMIT", ""),
    )


def handle_vector_index_command(parsed: object, runtime: Any) -> dict[str, Any]:
    try:
        config = config_from_env()
    except (TypeError, ValueError):
        return {"ok": False, "error": "invalid_vector_index_config"}
    command = str(getattr(parsed, "vector_index_command", "") or "")
    if command == "status":
        return {"ok": True, "vector_index": _status(config)}
    if not config.configured:
        return {"ok": False, "error": "postgres_not_configured"}
    repository = PostgresCandidateRepository(config)
    if command == "migrate":
        try:
            return repository.migrate()
        except Exception:
            return {"ok": False, "error": "postgres_migration_failed"}
    if command == "sync":
        if not config.enabled:
            return {"ok": False, "error": "postgres_vector_disabled"}
        if config.embedding_provider is None:
            return {"ok": False, "error": "embedding_not_configured"}
        syncer = PostgresVectorIndexSynchronizer(
            reader=SQLiteProjectionReader(runtime.store),
            repository=repository,
            embedding_provider=config.embedding_provider,
            config=config,
        )
        return syncer.sync(
            batch_size=max(1, min(256, int(getattr(parsed, "batch_size", 32)))),
            max_pages=max(1, min(10_000, int(getattr(parsed, "max_pages", 1)))),
        )
    return {"ok": False, "error": "unknown_vector_index_command"}


def _status(config: PostgresVectorConfig) -> dict[str, Any]:
    status: dict[str, Any] = {
        "enabled": config.enabled,
        "configured": config.configured and config.embedding_provider is not None,
        "available": False,
        "circuit": "closed",
        "lag_seconds": None,
        "watermark": "",
        "last_error": "",
        "embedding": config.embedding_provider.health() if config.embedding_provider is not None else {
            "configured": False,
            "available": False,
        },
    }
    if not config.enabled or not config.configured or config.embedding_provider is None:
        return status
    try:
        state: IndexState = PostgresCandidateRepository(config).read_index_state()
    except Exception:
        status["last_error"] = "postgres_unavailable"
        return status
    status.update(
        {
            "available": state.ready and bool(state.watermark),
            "lag_seconds": state.lag_seconds,
            "watermark": state.watermark,
            "last_error": "" if state.ready else "index_not_ready",
        }
    )
    return status


def _embedding_provider_from_env() -> OpenAICompatibleEmbeddingProvider | None:
    base_url = os.environ.get("EIMEMORY_EMBEDDINGS_BASE_URL", "").strip()
    api_key = os.environ.get("EIMEMORY_EMBEDDINGS_API_KEY", "").strip()
    model = os.environ.get("EIMEMORY_EMBEDDINGS_MODEL", "").strip()
    if not (base_url and api_key and model):
        return None
    dimension = _env_int("EIMEMORY_POSTGRES_VECTOR_DIMENSION", 1_536)
    return OpenAICompatibleEmbeddingProvider(
        base_url=base_url,
        api_key=api_key,
        model=model,
        dimension=dimension,
        max_batch=_env_int("EIMEMORY_EMBEDDINGS_MAX_BATCH", 32),
        max_text_chars=_env_int("EIMEMORY_EMBEDDINGS_MAX_TEXT_CHARS", 16_000),
        max_request_bytes=_env_int("EIMEMORY_EMBEDDINGS_MAX_REQUEST_BYTES", 512_000),
        max_response_bytes=_env_int("EIMEMORY_EMBEDDINGS_MAX_RESPONSE_BYTES", 4_000_000),
        timeout_seconds=_env_float("EIMEMORY_EMBEDDINGS_TIMEOUT_SECONDS", 5.0),
        failure_threshold=_env_int("EIMEMORY_EMBEDDINGS_FAILURE_THRESHOLD", 3),
        cooldown_seconds=_env_float("EIMEMORY_EMBEDDINGS_COOLDOWN_SECONDS", 30.0),
    )


def _env_flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except (TypeError, ValueError, OverflowError):
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, str(default)))
    except (TypeError, ValueError, OverflowError):
        return default
