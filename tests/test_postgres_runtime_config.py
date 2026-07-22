from __future__ import annotations

import json
from pathlib import Path
import threading
from typing import Any

from eimemory.api.runtime import Runtime
from eimemory.retrieval.contracts import CandidateRequest, ExactScope
from eimemory.retrieval.postgres_vector import (
    IndexState,
    PostgresVectorCandidateSource,
    PostgresVectorConfig,
    OpenAICompatibleEmbeddingProvider,
    projection_fingerprint,
)
from eimemory.retrieval.sqlite_source import SQLiteCandidateSource


SECRET_DSN = "postgresql://runtime-user:dsn-canary@db.invalid/eimemory"
SECRET_KEY = "embedding-key-canary"


class _FailingRepository:
    reads = 0

    def __init__(self, _config: PostgresVectorConfig) -> None:
        pass

    def read_index_state(self) -> IndexState:
        type(self).reads += 1
        raise RuntimeError("connection-canary")


class _StateRepository:
    def __init__(self, state: IndexState) -> None:
        self.state = state
        self.reads = 0

    def read_index_state(self) -> IndexState:
        self.reads += 1
        return self.state

    def search(self, *_args: Any, **_kwargs: Any) -> list[dict[str, Any]]:
        return []


class _Provider:
    def embed(self, texts: list[str], *, timeout_seconds: float | None = None) -> list[tuple[float, ...]]:
        return [(0.1, 0.2, 0.3) for _ in texts]

    def health(self) -> dict[str, object]:
        return {
            "configured": True,
            "available": True,
            "circuit": "closed",
            "dimension": 3,
            "last_error": "",
        }

    def fingerprint(self) -> str:
        return "f" * 64

    def effective_identity(self) -> dict[str, object]:
        return {
            "provider_type": "test-provider",
            "model": "test-embedding-model",
            "fingerprint": self.fingerprint(),
        }


def _set_enabled_env(monkeypatch: Any) -> None:
    monkeypatch.setenv("EIMEMORY_POSTGRES_VECTOR_ENABLED", "1")
    monkeypatch.setenv("EIMEMORY_POSTGRES_VECTOR_DSN", SECRET_DSN)
    monkeypatch.setenv("EIMEMORY_POSTGRES_VECTOR_DIMENSION", "3")
    monkeypatch.setenv("EIMEMORY_EMBEDDINGS_BASE_URL", "https://embedding.example/v1")
    monkeypatch.setenv("EIMEMORY_EMBEDDINGS_API_KEY", SECRET_KEY)
    monkeypatch.setenv("EIMEMORY_EMBEDDINGS_MODEL", "safe-embedding-model")


def test_runtime_create_defaults_to_sqlite_and_exposes_effective_identity(tmp_path: Path) -> None:
    runtime = Runtime.create(root=tmp_path)
    try:
        source = runtime.memory.recall_engine.candidate_source
        assert isinstance(source, SQLiteCandidateSource)
        identity = runtime.memory.recall_engine.effective_identity()
        assert identity["engine_type"] == "GovernedRecallEngine"
        assert identity["candidate_source"]["candidate_source_type"] == "SQLiteCandidateSource"
        assert identity["candidate_source"]["authority_revision"].isdigit()
        assert identity["fusion_version"] == "governed-rrf.v1"
        assert len(identity["identity_digest"]) == 64
    finally:
        runtime.close()


def test_runtime_create_reads_enabled_postgres_env_and_failure_bypasses_to_sqlite(
    tmp_path: Path, monkeypatch: Any
) -> None:
    _set_enabled_env(monkeypatch)
    monkeypatch.setattr(
        "eimemory.retrieval.postgres_vector.PostgresCandidateRepository",
        _FailingRepository,
    )
    _FailingRepository.reads = 0
    runtime = Runtime.create(root=tmp_path)
    try:
        source = runtime.memory.recall_engine.candidate_source
        assert isinstance(source, PostgresVectorCandidateSource)
        assert _FailingRepository.reads == 0
        runtime.memory.recall_engine.effective_identity()
        runtime.memory.recall_engine.effective_identity()
        assert _FailingRepository.reads == 0
        batch = source.search(
            CandidateRequest(
                query="runtime reachability",
                scope=ExactScope("tenant", "openclaw", "workspace", "user"),
                source_ids=("default",),
                limit=5,
                budget=15,
            )
        )
        diagnostics = batch.diagnostic_dict()
        assert diagnostics["postgres"]["state"] == "bypassed"
        assert diagnostics["postgres"]["error_code"] == "postgres_unavailable"
        assert source.health()["last_error"] == "postgres_unavailable"

        serialized = json.dumps(runtime.memory.recall_engine.effective_identity())
        assert SECRET_DSN not in serialized
        assert SECRET_KEY not in serialized
        assert "dsn-canary" not in serialized
        assert "embedding-key-canary" not in serialized
    finally:
        runtime.close()


def test_runtime_create_invalid_enabled_config_is_observable_non_blocking_bypass(
    tmp_path: Path, monkeypatch: Any
) -> None:
    _set_enabled_env(monkeypatch)
    monkeypatch.setenv("EIMEMORY_POSTGRES_VECTOR_SCHEMA", "INVALID;" + SECRET_KEY)

    runtime = Runtime.create(root=tmp_path)
    try:
        source = runtime.memory.recall_engine.candidate_source
        assert isinstance(source, PostgresVectorCandidateSource)
        assert source.health()["last_error"] == "invalid_vector_index_config"
        batch = source.search(
            CandidateRequest(
                query="still sqlite",
                scope=ExactScope("tenant", "openclaw", "workspace", "user"),
                limit=2,
                budget=6,
            )
        )
        diagnostics = batch.diagnostic_dict()
        assert diagnostics["postgres"]["state"] == "bypassed"
        assert diagnostics["postgres"]["error_code"] == "invalid_vector_index_config"
        serialized = json.dumps(runtime.memory.recall_engine.effective_identity())
        assert SECRET_DSN not in serialized
        assert SECRET_KEY not in serialized
    finally:
        runtime.close()


def test_runtime_create_invalid_numeric_config_fails_to_observable_sqlite_bypass(
    tmp_path: Path, monkeypatch: Any
) -> None:
    _set_enabled_env(monkeypatch)
    monkeypatch.setenv("EIMEMORY_POSTGRES_POOL_SIZE", "not-a-number-" + SECRET_KEY)

    runtime = Runtime.create(root=tmp_path)
    try:
        source = runtime.memory.recall_engine.candidate_source
        assert isinstance(source, PostgresVectorCandidateSource)
        assert source.health()["last_error"] == "invalid_vector_index_config"
        serialized = json.dumps(runtime.memory.recall_engine.effective_identity())
        assert SECRET_KEY not in serialized
    finally:
        runtime.close()


def test_effective_identity_changes_with_config_and_committed_postgres_state(tmp_path: Path) -> None:
    runtime = Runtime.create(root=tmp_path)
    try:
        sqlite_source = SQLiteCandidateSource(runtime.store)
        config = PostgresVectorConfig(
            enabled=True,
            connection_factory=lambda **_kwargs: None,
            embedding_provider=_Provider(),
            vector_dimension=3,
            top_k_max=8,
        )
        state = IndexState(
            ready=True,
            watermark="wm-1",
            authority_revision=sqlite_source.authority_revision(),
            projection_digest_schema="candidate-projection.v1",
            projection_fingerprint=projection_fingerprint(config),
            embedding_fingerprint="f" * 64,
        )
        repository = _StateRepository(state)
        source = PostgresVectorCandidateSource(
            sqlite_source=sqlite_source,
            config=config,
            repository=repository,
            embedding_provider=config.embedding_provider,
        )
        runtime.memory.recall_engine.candidate_source = source

        first_unverified = runtime.memory.recall_engine.effective_identity()
        assert repository.reads == 0
        assert first_unverified["candidate_source"]["postgres"]["state"] == "bypassed"
        assert first_unverified["candidate_source"]["postgres"]["index_verified"] is False
        assert source.refresh_index_identity() is True
        first = runtime.memory.recall_engine.effective_identity()
        assert repository.reads == 1
        assert first["candidate_source"]["postgres"]["state"] == "index_verified"
        assert first["candidate_source"]["postgres"]["index_verified"] is True
        assert first["candidate_source"]["postgres"]["query_valid"] is False
        source.refresh_index_identity()
        assert repository.reads == 1
        source.search(
            CandidateRequest(
                query="verified query",
                scope=ExactScope("default", "", "", ""),
                limit=3,
                budget=9,
            )
        )
        queried = runtime.memory.recall_engine.effective_identity()
        assert queried["candidate_source"]["postgres"]["state"] == "available"
        assert queried["candidate_source"]["postgres"]["query_valid"] is True
        repository.state = IndexState(
            ready=True,
            watermark="wm-2",
            authority_revision=sqlite_source.authority_revision(),
            projection_digest_schema="candidate-projection.v1",
            projection_fingerprint=projection_fingerprint(config),
            embedding_fingerprint="f" * 64,
        )
        source.refresh_index_identity(force=True)
        second = runtime.memory.recall_engine.effective_identity()
        assert first["identity_digest"] != second["identity_digest"]
        assert second["candidate_source"]["postgres"]["committed_watermark"] == "wm-2"
        assert second["candidate_source"]["postgres"]["state"] == "index_verified"
        assert second["candidate_source"]["postgres"]["query_valid"] is False

        changed_source = PostgresVectorCandidateSource(
            sqlite_source=sqlite_source,
            config=PostgresVectorConfig(
                enabled=True,
                connection_factory=lambda **_kwargs: None,
                embedding_provider=_Provider(),
                vector_dimension=3,
                top_k_max=9,
            ),
            repository=repository,
            embedding_provider=_Provider(),
        )
        runtime.memory.recall_engine.candidate_source = changed_source
        third = runtime.memory.recall_engine.effective_identity()
        assert second["identity_digest"] != third["identity_digest"]
    finally:
        runtime.close()


def test_runtime_config_fingerprint_tracks_sanitized_dsn_target_not_credentials(
    tmp_path: Path, monkeypatch: Any
) -> None:
    _set_enabled_env(monkeypatch)
    monkeypatch.setattr(
        "eimemory.retrieval.postgres_vector.PostgresCandidateRepository",
        _FailingRepository,
    )
    first_runtime = Runtime.create(root=tmp_path / "one")
    try:
        first = first_runtime.memory.recall_engine.effective_identity()
    finally:
        first_runtime.close()

    monkeypatch.setenv(
        "EIMEMORY_POSTGRES_VECTOR_DSN",
        "postgresql://other-user:other-secret@other-db.invalid/other-memory",
    )
    second_runtime = Runtime.create(root=tmp_path / "two")
    try:
        second = second_runtime.memory.recall_engine.effective_identity()
    finally:
        second_runtime.close()

    first_fingerprint = first["candidate_source"]["config_fingerprint"]
    second_fingerprint = second["candidate_source"]["config_fingerprint"]
    assert first_fingerprint != second_fingerprint
    serialized = json.dumps([first, second])
    assert "runtime-user" not in serialized
    assert "other-user" not in serialized
    assert "dsn-canary" not in serialized
    assert "other-secret" not in serialized
    assert "db.invalid" not in serialized


def test_openai_embedding_effective_identity_returns_model_without_secrets() -> None:
    provider = OpenAICompatibleEmbeddingProvider(
        base_url="https://embedding.example/v1",
        api_key=SECRET_KEY,
        model="safe-embedding-model",
        dimension=3,
    )

    identity = provider.effective_identity()

    assert identity["provider_type"] == "openai-compatible-embeddings.v1"
    assert identity["model"] == "safe-embedding-model"
    assert len(identity["fingerprint"]) == 64
    assert SECRET_KEY not in json.dumps(identity)


def test_index_identity_refresh_is_single_flight(tmp_path: Path) -> None:
    runtime = Runtime.create(root=tmp_path)
    entered = threading.Event()
    release = threading.Event()

    class BlockingRepository(_StateRepository):
        def read_index_state(self) -> IndexState:
            self.reads += 1
            entered.set()
            assert release.wait(2)
            return self.state

    try:
        sqlite_source = SQLiteCandidateSource(runtime.store)
        config = PostgresVectorConfig(
            enabled=True,
            connection_factory=lambda **_kwargs: None,
            embedding_provider=_Provider(),
            vector_dimension=3,
        )
        repository = BlockingRepository(
            IndexState(
                ready=True,
                watermark="wm-single",
                authority_revision=sqlite_source.authority_revision(),
                projection_digest_schema="candidate-projection.v1",
                projection_fingerprint=projection_fingerprint(config),
                embedding_fingerprint="f" * 64,
            )
        )
        source = PostgresVectorCandidateSource(
            sqlite_source=sqlite_source,
            config=config,
            repository=repository,
            embedding_provider=config.embedding_provider,
        )
        results: list[bool] = []
        first = threading.Thread(target=lambda: results.append(source.refresh_index_identity(force=True)))
        second = threading.Thread(target=lambda: results.append(source.refresh_index_identity(force=True)))
        first.start()
        assert entered.wait(1)
        second.start()
        second.join(1)
        release.set()
        first.join(2)

        assert repository.reads == 1
        assert sorted(results) == [False, True]
    finally:
        runtime.close()
