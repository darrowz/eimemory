from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from eimemory.retrieval.contracts import (
    CandidateBatch,
    CandidateHit,
    CandidateRef,
    CandidateRequest,
    ExactScope,
)
from eimemory.retrieval.postgres_vector import (
    IndexState,
    OpenAICompatibleEmbeddingProvider,
    PostgresCandidateRepository,
    PostgresVectorCandidateSource,
    PostgresVectorConfig,
    build_postgres_vector_candidate_source,
)


SCOPE = ExactScope("tenant", "openclaw", "workspace", "user")


class SQLiteSource:
    name = "sqlite"

    def __init__(self, hits: tuple[CandidateHit, ...] = ()) -> None:
        self.hits = hits
        self.calls = 0
        self.request_limits: list[int] = []

    def search(self, request: CandidateRequest) -> CandidateBatch:
        self.calls += 1
        self.request_limits.append(request.limit)
        return CandidateBatch(
            hits=self.hits,
            diagnostics={"source_name": "sqlite", "candidate_count": len(self.hits)},
        )


class StaticProvider:
    def __init__(self, vectors: list[tuple[float, ...]] | None = None, error: Exception | None = None) -> None:
        self.vectors = vectors or [(0.1, 0.2, 0.3)]
        self.error = error
        self.calls = 0

    def embed(self, texts: list[str], *, timeout_seconds: float | None = None) -> list[tuple[float, ...]]:
        self.calls += 1
        if self.error:
            raise self.error
        return self.vectors

    def health(self) -> dict[str, object]:
        return {"available": self.error is None, "circuit": "closed"}


class FakeRepository:
    def __init__(self, rows: list[dict[str, Any]] | None = None, *, error: Exception | None = None) -> None:
        self.rows = list(rows or [])
        self.error = error
        self.requests: list[tuple[CandidateRequest, tuple[float, ...], int, str]] = []
        self.state = IndexState(
            ready=True,
            watermark="wm-1",
            lag_seconds=0.0,
            authoritative_updated_at="2026-07-22T00:00:00Z",
            completed_at="2026-07-22T00:00:00Z",
        )

    def read_index_state(self) -> IndexState:
        return self.state

    def search(
        self,
        request: CandidateRequest,
        vector: tuple[float, ...],
        *,
        top_k: int,
        watermark: str,
    ) -> list[dict[str, Any]]:
        self.requests.append((request, vector, top_k, watermark))
        if self.error:
            raise self.error
        return list(self.rows)


def _request(*, source_ids: tuple[str, ...] | None = ("alpha",), limit: int = 5) -> CandidateRequest:
    return CandidateRequest(
        query="postgres safety",
        scope=SCOPE,
        kinds=("memory",),
        source_ids=source_ids,
        limit=limit,
        budget=20,
        task_context=(("release_commit", "abc123"),),
    )


def _hit(record_id: str, *, score: float = 0.5) -> CandidateHit:
    return CandidateHit(
        ref=CandidateRef(record_id, SCOPE, "alpha"),
        source_rank=1,
        source_score=score,
        component_hints={"keyword_score": score},
    )


def _row(record_id: str, **overrides: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "storage_key": f"key-{record_id}",
        "record_id": record_id,
        "tenant_id": SCOPE.tenant_id,
        "agent_id": SCOPE.agent_id,
        "workspace_id": SCOPE.workspace_id,
        "user_id": SCOPE.user_id,
        "source_id": "alpha",
        "kind": "memory",
        "status": "active",
        "vector_score": 0.91,
        "payload_digest": "a" * 64,
        "index_watermark": "wm-1",
    }
    row.update(overrides)
    return row


def test_default_config_is_disabled_and_imports_without_psycopg() -> None:
    root = Path(__file__).parents[1]
    script = """
import builtins
real_import = builtins.__import__
def guarded(name, *args, **kwargs):
    if name.startswith('psycopg'):
        raise AssertionError('psycopg imported eagerly')
    return real_import(name, *args, **kwargs)
builtins.__import__ = guarded
from eimemory.retrieval.postgres_vector import PostgresVectorConfig
assert PostgresVectorConfig().enabled is False
"""
    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=root,
        text=True,
        capture_output=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr


def test_disabled_or_unconfigured_postgres_bypasses_but_sqlite_always_runs() -> None:
    sqlite = SQLiteSource((_hit("sqlite-only"),))
    source = PostgresVectorCandidateSource(
        sqlite_source=sqlite,
        config=PostgresVectorConfig(enabled=False),
    )

    batch = source.search(_request())

    assert [hit.ref.record_id for hit in batch.hits] == ["sqlite-only"]
    assert sqlite.calls == 1
    assert batch.diagnostic_dict()["postgres"]["state"] == "bypassed"
    assert source.health()["configured"] is False


def test_public_factory_always_installs_sqlite_as_the_authoritative_participant(tmp_path: Path) -> None:
    from eimemory.storage.runtime_store import RuntimeStore
    from eimemory.retrieval.sqlite_source import SQLiteCandidateSource

    store = RuntimeStore(tmp_path)
    try:
        source = build_postgres_vector_candidate_source(
            store,
            PostgresVectorConfig(enabled=False),
        )
        assert isinstance(source.sqlite_source, SQLiteCandidateSource)
        assert source.sqlite_source.store is store
    finally:
        store.close()


def test_governed_engine_preserves_sqlite_candidate_budget_for_augmented_source(tmp_path: Path) -> None:
    from eimemory.api.memory import MemoryAPI
    from eimemory.retrieval.engine import GovernedRecallEngine
    from eimemory.storage.runtime_store import RuntimeStore

    sqlite = SQLiteSource(())
    source = PostgresVectorCandidateSource(
        sqlite_source=sqlite,
        config=PostgresVectorConfig(enabled=False),
    )
    store = RuntimeStore(tmp_path)
    memory = MemoryAPI(store, recall_engine=GovernedRecallEngine(store=store, candidate_source=source))
    try:
        memory.recall(
            query="candidate budget",
            scope={
                "tenant_id": SCOPE.tenant_id,
                "agent_id": SCOPE.agent_id,
                "workspace_id": SCOPE.workspace_id,
                "user_id": SCOPE.user_id,
            },
            limit=5,
            task_context={"task_type": "chat.reply", "source_ids": ["alpha"]},
        )
        assert sqlite.calls >= 1
        assert max(sqlite.request_limits) >= GovernedRecallEngine._minimum_candidate_budget
    finally:
        store.close()


def test_successful_empty_postgres_query_is_valid_not_a_failure() -> None:
    sqlite = SQLiteSource((_hit("sqlite-only"),))
    repository = FakeRepository([])
    source = PostgresVectorCandidateSource(
        sqlite_source=sqlite,
        config=PostgresVectorConfig(enabled=True, dsn="postgresql://host/db", vector_dimension=3),
        repository=repository,
        embedding_provider=StaticProvider(),
    )

    batch = source.search(_request())

    assert [hit.ref.record_id for hit in batch.hits] == ["sqlite-only"]
    pg = batch.diagnostic_dict()["postgres"]
    assert pg["state"] == "available"
    assert pg["valid_empty"] is True
    assert pg["fallback"] is False


def test_postgres_refs_are_bounded_and_cross_boundary_rows_are_dropped() -> None:
    rows = [
        _row("valid"),
        _row("foreign-scope", user_id="other"),
        _row("foreign-source", source_id="beta"),
        _row("inactive", status="inactive"),
        _row("wrong-kind", kind="reflection"),
        _row("old-watermark", index_watermark="old"),
        _row("corrupt-digest", payload_digest="not-a-digest"),
        _row(""),
    ]
    sqlite = SQLiteSource((_hit("sqlite"),))
    source = PostgresVectorCandidateSource(
        sqlite_source=sqlite,
        config=PostgresVectorConfig(enabled=True, dsn="postgresql://host/db", vector_dimension=3, top_k_max=8),
        repository=FakeRepository(rows),
        embedding_provider=StaticProvider(),
    )

    batch = source.search(_request(limit=20))

    assert [hit.ref.record_id for hit in batch.hits] == ["sqlite", "valid"]
    assert batch.hits[1].component_dict()["vector_score"] == pytest.approx(0.91)
    assert batch.hits[1].evidence_hints == ("vector_match",)
    pg = batch.diagnostic_dict()["postgres"]
    assert pg["drops"] == {
        "digest_invalid": 1,
        "invalid_ref": 1,
        "kind_not_allowed": 1,
        "scope_not_allowed": 1,
        "source_not_allowed": 1,
        "stale_watermark": 1,
        "status_not_active": 1,
    }
    assert pg["top_k"] == 8


def test_duplicate_postgres_ref_only_augments_sqlite_hint() -> None:
    source = PostgresVectorCandidateSource(
        sqlite_source=SQLiteSource((_hit("same", score=0.4),)),
        config=PostgresVectorConfig(enabled=True, dsn="postgresql://host/db", vector_dimension=3),
        repository=FakeRepository([_row("same", vector_score=0.83)]),
        embedding_provider=StaticProvider(),
    )

    batch = source.search(_request())

    assert len(batch.hits) == 1
    assert batch.hits[0].component_dict() == {"keyword_score": 0.4, "vector_score": 0.83}


def test_cache_is_bounded_by_scope_source_query_policy_release_and_sqlite_still_runs() -> None:
    sqlite = SQLiteSource((_hit("sqlite"),))
    repository = FakeRepository([_row("pg")])
    provider = StaticProvider()
    source = PostgresVectorCandidateSource(
        sqlite_source=sqlite,
        config=PostgresVectorConfig(
            enabled=True,
            dsn="postgresql://host/db",
            vector_dimension=3,
            cache_entries=2,
            cache_ttl_seconds=30,
        ),
        repository=repository,
        embedding_provider=provider,
    )
    base = _request()

    source.search(base)
    source.search(base)
    changed_release = CandidateRequest(
        query=base.query,
        scope=base.scope,
        kinds=base.kinds,
        source_ids=base.source_ids,
        limit=base.limit,
        budget=base.budget,
        task_context=(("release_commit", "different"),),
    )
    source.search(changed_release)

    assert sqlite.calls == 3
    assert provider.calls == 2
    assert len(repository.requests) == 2
    assert source.health()["cache_entries"] == 2


def test_query_failure_opens_circuit_then_half_open_probe_recovers() -> None:
    now = [100.0]
    repository = FakeRepository(error=TimeoutError("postgresql://user:secret@host/db"))
    source = PostgresVectorCandidateSource(
        sqlite_source=SQLiteSource((_hit("sqlite"),)),
        config=PostgresVectorConfig(
            enabled=True,
            dsn="postgresql://user:secret@host/db",
            vector_dimension=3,
            failure_threshold=2,
            cooldown_seconds=5,
        ),
        repository=repository,
        embedding_provider=StaticProvider(),
        clock=lambda: now[0],
    )

    first = source.search(_request()).diagnostic_dict()["postgres"]
    second = source.search(_request()).diagnostic_dict()["postgres"]
    blocked = source.search(_request()).diagnostic_dict()["postgres"]
    assert first["state"] == second["state"] == blocked["state"] == "bypassed"
    assert source.health()["circuit"] == "open"
    assert len(repository.requests) == 2
    assert "secret" not in json.dumps(source.health())
    assert "user" not in json.dumps(source.health())

    now[0] += 6
    repository.error = None
    recovered = source.search(_request()).diagnostic_dict()["postgres"]
    assert recovered["state"] == "available"
    assert source.health()["circuit"] == "closed"


def test_real_embedding_timeout_code_is_preserved_without_provider_error_text() -> None:
    class TimedOutProvider(StaticProvider):
        def embed(self, texts: list[str], *, timeout_seconds: float | None = None) -> list[tuple[float, ...]]:
            raise RuntimeError("embedding_timeout")

    source = PostgresVectorCandidateSource(
        sqlite_source=SQLiteSource((_hit("sqlite"),)),
        config=PostgresVectorConfig(enabled=True, dsn="postgresql://host/db", vector_dimension=3),
        repository=FakeRepository([]),
        embedding_provider=TimedOutProvider(),
    )

    pg = source.search(_request()).diagnostic_dict()["postgres"]
    assert pg["state"] == "bypassed"
    assert pg["error_code"] == "embedding_timeout"


def test_dimension_and_lag_fail_closed_while_sqlite_continues() -> None:
    repository = FakeRepository([_row("pg")])
    repository.state = IndexState(ready=True, watermark="old", lag_seconds=61.0)
    source = PostgresVectorCandidateSource(
        sqlite_source=SQLiteSource((_hit("sqlite"),)),
        config=PostgresVectorConfig(
            enabled=True,
            dsn="postgresql://host/db",
            vector_dimension=3,
            max_index_lag_seconds=60,
        ),
        repository=repository,
        embedding_provider=StaticProvider(vectors=[(0.1, 0.2)]),
    )

    lagged = source.search(_request())
    assert [hit.ref.record_id for hit in lagged.hits] == ["sqlite"]
    assert lagged.diagnostic_dict()["postgres"]["error_code"] == "index_lag_exceeded"

    repository.state = IndexState(ready=True, watermark="new", lag_seconds=0.0)
    wrong_dim = source.search(_request())
    assert [hit.ref.record_id for hit in wrong_dim.hits] == ["sqlite"]
    assert wrong_dim.diagnostic_dict()["postgres"]["error_code"] == "embedding_dimension_mismatch"


def test_governed_engine_rehydrates_postgres_refs_from_sqlite_authority(tmp_path: Path) -> None:
    from eimemory.api.memory import MemoryAPI
    from eimemory.models.records import RecordEnvelope, ScopeRef
    from eimemory.retrieval.engine import GovernedRecallEngine
    from eimemory.storage.runtime_store import RuntimeStore

    store = RuntimeStore(tmp_path)
    scope = ScopeRef(SCOPE.tenant_id, SCOPE.agent_id, SCOPE.workspace_id, SCOPE.user_id)
    record = RecordEnvelope.create(
        kind="memory",
        title="Canonical vector result",
        content={"text": "authoritative content"},
        scope=scope,
        source_id="alpha",
    )
    store.append(record)
    rows = [_row(record.record_id), _row("stale-missing")]
    source = PostgresVectorCandidateSource(
        sqlite_source=SQLiteSource(()),
        config=PostgresVectorConfig(enabled=True, dsn="postgresql://host/db", vector_dimension=3),
        repository=FakeRepository(rows),
        embedding_provider=StaticProvider(),
    )
    memory = MemoryAPI(store, recall_engine=GovernedRecallEngine(store=store, candidate_source=source))
    try:
        bundle = memory.recall(
            query="semantic-only query",
            scope={
                "tenant_id": scope.tenant_id,
                "agent_id": scope.agent_id,
                "workspace_id": scope.workspace_id,
                "user_id": scope.user_id,
            },
            limit=5,
            task_context={"task_type": "chat.reply", "source_ids": ["alpha"]},
        )
        assert [item.record_id for item in bundle.items] == [record.record_id]
        assert bundle.explanation["engine_diagnostics"]["drops"]["missing_or_corrupt_record"] >= 1
    finally:
        store.close()


def test_health_payload_exposes_only_sanitized_optional_candidate_status(tmp_path: Path) -> None:
    from eimemory.adapters.eibrain.rpc_server import build_health_payload
    from eimemory.api.runtime import Runtime
    from eimemory.storage.runtime_store import RuntimeStore

    source = PostgresVectorCandidateSource(
        sqlite_source=SQLiteSource(()),
        config=PostgresVectorConfig(
            enabled=True,
            dsn="postgresql://user:secret@host/db",
            vector_dimension=3,
        ),
        repository=FakeRepository(error=TimeoutError("secret")),
        embedding_provider=StaticProvider(),
    )
    runtime = Runtime(RuntimeStore(tmp_path), candidate_source=source)
    try:
        source.search(_request())
        payload = build_health_payload(runtime, listen_host="127.0.0.1", listen_port=8091)
        serialized = json.dumps(payload)
        assert payload["retrieval"]["candidate_source"]["enabled"] is True
        assert payload["retrieval"]["candidate_source"]["last_error"] == "postgres_timeout"
        assert "secret" not in serialized
        assert "postgresql://" not in serialized
    finally:
        runtime.close()


class FakeCursor:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows
        self.calls: list[tuple[str, Any]] = []
        self.description = [(name,) for name in rows[0]] if rows else []

    def execute(self, sql: str, params: Any = None) -> None:
        self.calls.append((sql, params))

    def fetchall(self) -> list[dict[str, Any]]:
        return self.rows

    def fetchone(self) -> dict[str, Any] | None:
        return self.rows[0] if self.rows else None

    def __enter__(self) -> "FakeCursor":
        return self

    def __exit__(self, *args: object) -> None:
        return None


class FakeConnection:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.cursor_value = FakeCursor(rows)

    def cursor(self, **kwargs: object) -> FakeCursor:
        return self.cursor_value

    def close(self) -> None:
        return None


def test_repository_uses_validated_identifiers_prepared_exact_predicates_and_bounds() -> None:
    connection = FakeConnection([_row("one")])
    config = PostgresVectorConfig(
        enabled=True,
        dsn="postgresql://host/db",
        connection_factory=lambda **kwargs: connection,
        schema="safe_schema",
        table="safe_table",
        vector_dimension=3,
        statement_timeout_ms=777,
        top_k_max=7,
    )
    repository = PostgresCandidateRepository(config)

    rows = repository.search(
        _request(source_ids=("alpha", "beta")),
        (0.1, 0.2, 0.3),
        top_k=999,
        watermark="wm-1",
    )

    assert rows[0]["record_id"] == "one"
    statements = connection.cursor_value.calls
    select_sql, params = next(item for item in statements if "FROM \"safe_schema\".\"safe_table\"" in item[0])
    assert "tenant_id = %s" in select_sql
    assert "source_id = ANY(%s)" in select_sql
    assert "status = %s" in select_sql
    assert "index_watermark = %s" in select_sql
    assert "LIMIT %s" in select_sql
    assert params[-1] == 7
    assert "postgres safety" not in select_sql
    assert ("alpha", "beta") in params
    assert "wm-1" in params
    assert any("set_config" in sql for sql, _ in statements)


@pytest.mark.parametrize("field,value", [("schema", "safe;DROP TABLE x"), ("table", "x--")])
def test_repository_rejects_identifier_injection(field: str, value: str) -> None:
    kwargs = {field: value}
    with pytest.raises(ValueError, match="identifier"):
        PostgresVectorConfig(enabled=True, dsn="postgresql://host/db", **kwargs)


def test_openai_compatible_embedding_is_bounded_and_dimension_checked() -> None:
    calls: list[dict[str, Any]] = []

    def transport(**kwargs: Any) -> bytes:
        calls.append(kwargs)
        return json.dumps({"data": [{"index": 0, "embedding": [0.1, 0.2, 0.3]}]}).encode()

    provider = OpenAICompatibleEmbeddingProvider(
        base_url="https://embeddings.example/v1",
        api_key="top-secret",
        model="embed-model",
        dimension=3,
        transport=transport,
        max_batch=2,
        max_text_chars=5,
        max_response_bytes=2048,
        timeout_seconds=1.5,
    )

    vectors = provider.embed(["abcdefghij"])

    assert vectors == [(0.1, 0.2, 0.3)]
    body = json.loads(calls[0]["body"])
    assert body == {"input": ["abcde"], "model": "embed-model"}
    assert calls[0]["timeout_seconds"] == 1.5
    assert calls[0]["max_response_bytes"] == 2048
    assert calls[0]["headers"]["Authorization"] == "Bearer top-secret"
    assert "top-secret" not in json.dumps(provider.health())


def test_openai_embedding_rejects_oversized_or_wrong_shaped_response_and_opens_circuit() -> None:
    now = [0.0]
    responses = [
        b"x" * 257,
        json.dumps({"data": [{"index": 0, "embedding": [0.1]}]}).encode(),
        json.dumps({"data": [{"index": 0, "embedding": [0.1, 0.2]}]}).encode(),
    ]

    def transport(**kwargs: Any) -> bytes:
        return responses.pop(0)

    provider = OpenAICompatibleEmbeddingProvider(
        base_url="https://embeddings.example/v1",
        api_key="secret",
        model="embed",
        dimension=2,
        transport=transport,
        max_response_bytes=256,
        failure_threshold=2,
        cooldown_seconds=3,
        clock=lambda: now[0],
    )

    with pytest.raises(RuntimeError, match="response_too_large"):
        provider.embed(["a"])
    with pytest.raises(RuntimeError, match="embedding_dimension_mismatch"):
        provider.embed(["a"])
    with pytest.raises(RuntimeError, match="circuit_open"):
        provider.embed(["a"])
    assert provider.health()["circuit"] == "open"

    now[0] += 4
    assert provider.embed(["a"]) == [(0.1, 0.2)]
    assert provider.health()["circuit"] == "closed"
