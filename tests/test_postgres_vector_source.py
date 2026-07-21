from __future__ import annotations

from datetime import datetime, timezone
import json
import subprocess
import sys
import threading
import time
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
    projection_fingerprint,
)


SCOPE = ExactScope("tenant", "openclaw", "workspace", "user")
EMBEDDING_FINGERPRINT = "f" * 64


def _index_state(**overrides: Any) -> IndexState:
    config = PostgresVectorConfig(vector_dimension=3)
    values: dict[str, Any] = {
        "ready": True,
        "watermark": "wm-1",
        "lag_seconds": 0.0,
        "authoritative_updated_at": "2026-07-22T00:00:00.000000Z",
        "authoritative_storage_key": "key",
        "completed_at": "2026-07-22T00:00:00Z",
        "embedding_fingerprint": EMBEDDING_FINGERPRINT,
        "projection_digest_schema": "candidate-projection.v1",
        "projection_fingerprint": projection_fingerprint(config),
    }
    values.update(overrides)
    return IndexState(**values)


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
        return {
            "configured": True,
            "available": self.error is None,
            "circuit": "closed",
            "dimension": len(self.vectors[0]) if self.vectors else 0,
            "last_error": "",
        }

    def fingerprint(self) -> str:
        return EMBEDDING_FINGERPRINT


class FakeRepository:
    def __init__(self, rows: list[dict[str, Any]] | None = None, *, error: Exception | None = None) -> None:
        self.rows = list(rows or [])
        self.error = error
        self.requests: list[tuple[CandidateRequest, tuple[float, ...], int, str]] = []
        self.states: list[IndexState] = []
        self.state_reads = 0
        self.state = _index_state()

    def read_index_state(self) -> IndexState:
        self.state_reads += 1
        if self.states:
            return self.states.pop(0)
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
        "projection_digest": "a" * 64,
        "projection_digest_schema": "candidate-projection.v1",
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
        bundle = memory.recall(
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
        assert bundle.explanation["engine_diagnostics"]["fallback"] is True
        assert bundle.explanation["engine_diagnostics"]["fallback_reason"] == "candidate_source_fallback"
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
        _row("corrupt-digest", projection_digest="not-a-digest"),
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

    assert {hit.ref.record_id for hit in batch.hits} == {"sqlite", "valid"}
    vector_hit = next(hit for hit in batch.hits if hit.ref.record_id == "valid")
    assert vector_hit.component_dict()["vector_score"] == pytest.approx(0.91)
    assert vector_hit.evidence_hints == ("vector_match",)
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


def test_duplicate_postgres_ref_augments_sqlite_without_giving_postgres_authority() -> None:
    source = PostgresVectorCandidateSource(
        sqlite_source=SQLiteSource((_hit("same", score=0.4),)),
        config=PostgresVectorConfig(enabled=True, dsn="postgresql://host/db", vector_dimension=3),
        repository=FakeRepository([_row("same", vector_score=0.83)]),
        embedding_provider=StaticProvider(),
    )

    batch = source.search(_request())

    assert len(batch.hits) == 1
    hints = batch.hits[0].component_dict()
    assert hints["keyword_score"] == 0.4
    assert hints["vector_score"] == 0.83
    assert hints["_candidate_sqlite_authority_duplicate"] is True


def test_postgres_only_candidates_survive_a_full_sqlite_candidate_budget() -> None:
    sqlite_hits = tuple(
        CandidateHit(
            ref=CandidateRef(f"sqlite-{index}", SCOPE, "alpha"),
            source_rank=index + 1,
            source_score=0.5,
            component_hints={"keyword_score": 0.5},
            evidence_hints=("exact_title",) if index == 0 else (),
        )
        for index in range(8)
    )
    source = PostgresVectorCandidateSource(
        sqlite_source=SQLiteSource(sqlite_hits),
        config=PostgresVectorConfig(enabled=True, dsn="postgresql://host/db", vector_dimension=3),
        repository=FakeRepository([_row("postgres-only")]),
        embedding_provider=StaticProvider(),
    )

    batch = source.search(_request(limit=8))

    ids = [hit.ref.record_id for hit in batch.hits]
    assert len(ids) == 8
    assert "sqlite-0" in ids  # SQLite exact identity is never sacrificed.
    assert "postgres-only" in ids  # Vector augmentation can expand the pool.


def test_duplicate_postgres_rows_are_deduplicated_without_bypassing() -> None:
    source = PostgresVectorCandidateSource(
        sqlite_source=SQLiteSource(()),
        config=PostgresVectorConfig(enabled=True, dsn="postgresql://host/db", vector_dimension=3),
        repository=FakeRepository([_row("same", vector_score=0.8), _row("same", vector_score=0.9)]),
        embedding_provider=StaticProvider(),
    )

    batch = source.search(_request())

    assert [hit.ref.record_id for hit in batch.hits] == ["same"]
    assert batch.diagnostic_dict()["postgres"]["state"] == "available"
    assert batch.hits[0].component_dict()["vector_score"] == pytest.approx(0.9)


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


def test_cache_key_includes_limit_and_top_k_shape() -> None:
    repository = FakeRepository([_row(f"pg-{index}") for index in range(10)])
    provider = StaticProvider()
    source = PostgresVectorCandidateSource(
        sqlite_source=SQLiteSource(()),
        config=PostgresVectorConfig(
            enabled=True,
            dsn="postgresql://host/db",
            vector_dimension=3,
            cache_entries=10,
        ),
        repository=repository,
        embedding_provider=provider,
    )

    source.search(_request(limit=1))
    source.search(_request(limit=8))

    assert provider.calls == 2
    assert [request.limit for request, _vector, _top_k, _watermark in repository.requests] == [1, 8]


def test_cache_key_hashes_unbounded_query_instead_of_retaining_raw_text() -> None:
    source = PostgresVectorCandidateSource(
        sqlite_source=SQLiteSource(()),
        config=PostgresVectorConfig(enabled=True, dsn="postgresql://host/db", vector_dimension=3),
        repository=FakeRepository([]),
        embedding_provider=StaticProvider(),
    )
    query = "sensitive-and-large-" * 100_000
    request = CandidateRequest(
        query=query,
        scope=SCOPE,
        kinds=("memory",),
        source_ids=("alpha",),
        limit=5,
        budget=20,
    )

    source.search(request)

    assert len(source._cache) == 1
    assert all(query not in str(part) for key in source._cache for part in key)
    assert len(next(iter(source._cache))[0]) == 64


def test_watermark_change_between_state_and_query_is_bypassed_not_valid_empty() -> None:
    repository = FakeRepository([])
    repository.states = [
        _index_state(watermark="old"),
        _index_state(watermark="new"),
    ]
    source = PostgresVectorCandidateSource(
        sqlite_source=SQLiteSource((_hit("sqlite"),)),
        config=PostgresVectorConfig(enabled=True, dsn="postgresql://host/db", vector_dimension=3),
        repository=repository,
        embedding_provider=StaticProvider(),
    )

    pg = source.search(_request()).diagnostic_dict()["postgres"]

    assert pg["state"] == "bypassed"
    assert pg["valid_empty"] is False
    assert pg["error_code"] == "index_watermark_changed"


def test_embedding_calls_share_a_bounded_queue_and_bypass_when_full() -> None:
    class BlockingProvider(StaticProvider):
        def __init__(self) -> None:
            super().__init__()
            self.started = threading.Event()
            self.release = threading.Event()

        def embed(self, texts: list[str], *, timeout_seconds: float | None = None) -> list[tuple[float, ...]]:
            self.calls += 1
            self.started.set()
            self.release.wait(timeout=2)
            return self.vectors

    provider = BlockingProvider()
    source = PostgresVectorCandidateSource(
        sqlite_source=SQLiteSource((_hit("sqlite"),)),
        config=PostgresVectorConfig(
            enabled=True,
            dsn="postgresql://host/db",
            vector_dimension=3,
            pool_size=1,
            queue_bound=0,
            connect_timeout_seconds=0.05,
            cache_entries=0,
        ),
        repository=FakeRepository([]),
        embedding_provider=provider,
    )
    first_result: list[CandidateBatch] = []
    worker = threading.Thread(target=lambda: first_result.append(source.search(_request())))
    worker.start()
    assert provider.started.wait(timeout=1)

    second = source.search(_request())
    provider.release.set()
    worker.join(timeout=2)

    assert provider.calls == 1
    assert second.diagnostic_dict()["postgres"]["state"] == "bypassed"
    assert second.diagnostic_dict()["postgres"]["error_code"] == "embedding_timeout"
    assert len(first_result) == 1


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
    repository.state = _index_state(watermark="old", lag_seconds=61.0)
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

    repository.state = _index_state(watermark="new", lag_seconds=0.0)
    wrong_dim = source.search(_request())
    assert [hit.ref.record_id for hit in wrong_dim.hits] == ["sqlite"]
    assert wrong_dim.diagnostic_dict()["postgres"]["error_code"] == "embedding_dimension_mismatch"


def test_governed_engine_rehydrates_postgres_refs_from_sqlite_authority(tmp_path: Path) -> None:
    from eimemory.api.memory import MemoryAPI
    from eimemory.models.records import RecordEnvelope, ScopeRef
    from eimemory.retrieval.engine import GovernedRecallEngine
    from eimemory.retrieval.postgres_vector import candidate_record_projection_digest
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
    stale_record = RecordEnvelope.create(
        kind="memory",
        title="Changed authoritative record",
        content={"text": "new current content"},
        scope=scope,
        source_id="alpha",
    )
    store.append(stale_record)
    valid_digest = candidate_record_projection_digest(record, max_text_chars=16_000)
    original_updated_at = stale_record.time.updated_at
    stale_record.time.updated_at = "2000-01-01T00:00:00Z"
    stale_digest = candidate_record_projection_digest(stale_record, max_text_chars=16_000)
    stale_record.time.updated_at = original_updated_at
    rows = [
        _row(
            record.record_id,
            projection_digest=valid_digest,
            authoritative_updated_at=datetime.fromisoformat(
                record.time.updated_at.replace("Z", "+00:00")
            ),
        ),
        _row(
            stale_record.record_id,
            projection_digest=stale_digest,
            authoritative_updated_at="2000-01-01T00:00:00Z",
        ),
        _row("stale-missing"),
    ]
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
        assert bundle.explanation["engine_diagnostics"]["drops"]["candidate_projection_digest_mismatch"] >= 1
    finally:
        store.close()


def test_stale_postgres_duplicate_is_audited_but_never_vetoes_sqlite_authority(tmp_path: Path) -> None:
    from eimemory.api.memory import MemoryAPI
    from eimemory.models.records import RecordEnvelope, ScopeRef
    from eimemory.retrieval.engine import GovernedRecallEngine
    from eimemory.storage.runtime_store import RuntimeStore

    store = RuntimeStore(tmp_path)
    scope = ScopeRef(SCOPE.tenant_id, SCOPE.agent_id, SCOPE.workspace_id, SCOPE.user_id)
    record = RecordEnvelope.create(
        kind="memory",
        title="SQLite must survive stale PG",
        content={"text": "authoritative"},
        scope=scope,
        source_id="alpha",
        meta={"force_capture": True},
    )
    store.append(record)
    sqlite_hit = CandidateHit(
        ref=CandidateRef(record.record_id, SCOPE, "alpha"),
        source_rank=1,
        source_score=0.4,
        component_hints={"keyword_score": 0.4},
    )
    source = PostgresVectorCandidateSource(
        sqlite_source=SQLiteSource((sqlite_hit,)),
        config=PostgresVectorConfig(enabled=True, dsn="postgresql://host/db", vector_dimension=3),
        repository=FakeRepository([
            _row(
                record.record_id,
                projection_digest="0" * 64,
                authoritative_updated_at="2000-01-01T00:00:00Z",
            )
        ]),
        embedding_provider=StaticProvider(),
    )
    memory = MemoryAPI(store, recall_engine=GovernedRecallEngine(store=store, candidate_source=source))
    try:
        bundle = memory.recall(
            query="SQLite must survive stale PG",
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
        diagnostics = bundle.explanation["engine_diagnostics"]
        assert diagnostics["drops"]["candidate_projection_digest_mismatch"] >= 1
        serialized = json.dumps(bundle.explanation)
        assert '"vector_score": 0.91' not in serialized
        assert "_candidate" not in serialized
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


def test_repository_canonicalizes_psycopg_datetime_index_state() -> None:
    config = PostgresVectorConfig(
        enabled=True,
        connection_factory=lambda **kwargs: connection,
        vector_dimension=3,
    )
    connection = FakeConnection([
        {
            "ready": True,
            "committed_watermark": "wm-1",
            "lag_seconds": 0.0,
            "authoritative_updated_at": datetime(2026, 7, 22, tzinfo=timezone.utc),
            "authoritative_storage_key": "key",
            "completed_at": datetime(2026, 7, 22, 0, 0, 1, tzinfo=timezone.utc),
            "embedding_fingerprint": EMBEDDING_FINGERPRINT,
            "projection_digest_schema": "candidate-projection.v1",
            "projection_fingerprint": projection_fingerprint(config),
        }
    ])

    state = PostgresCandidateRepository(config).read_index_state()

    assert state.authoritative_updated_at == "2026-07-22T00:00:00.000000Z"


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
    assert ["alpha", "beta"] in params
    assert ["memory"] in params
    assert "wm-1" in params
    assert any("set_config" in sql for sql, _ in statements)
    assert "hnsw.iterative_scan" in "\n".join(sql for sql, _ in statements)
    assert "hnsw.max_scan_tuples" in "\n".join(sql for sql, _ in statements)
    assert "left(record_id, 256)" in select_sql
    assert "left(workspace_id, 512)" in select_sql


@pytest.mark.parametrize("field,value", [("schema", "safe;DROP TABLE x"), ("table", "x--")])
def test_repository_rejects_identifier_injection(field: str, value: str) -> None:
    kwargs = {field: value}
    with pytest.raises(ValueError, match="identifier"):
        PostgresVectorConfig(enabled=True, dsn="postgresql://host/db", **kwargs)


@pytest.mark.parametrize("dimension", [0, 2001, 65_536])
def test_vector_dimension_is_rejected_outside_pgvector_hnsw_bounds(dimension: int) -> None:
    with pytest.raises(ValueError, match="vector_dimension"):
        PostgresVectorConfig(vector_dimension=dimension)


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


def test_openai_embedding_does_not_forward_bearer_across_redirects() -> None:
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    received_authorization: list[str] = []

    class TargetHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            received_authorization.append(str(self.headers.get("Authorization") or ""))
            self.send_response(200)
            self.end_headers()

        do_POST = do_GET

        def log_message(self, format: str, *args: object) -> None:
            return None

    target = ThreadingHTTPServer(("127.0.0.1", 0), TargetHandler)

    class RedirectHandler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802
            self.send_response(302)
            self.send_header("Location", f"http://127.0.0.1:{target.server_port}/capture")
            self.end_headers()

        def log_message(self, format: str, *args: object) -> None:
            return None

    redirect = ThreadingHTTPServer(("127.0.0.1", 0), RedirectHandler)
    target_thread = threading.Thread(target=target.serve_forever)
    redirect_thread = threading.Thread(target=redirect.serve_forever)
    target_thread.start()
    redirect_thread.start()
    try:
        provider = OpenAICompatibleEmbeddingProvider(
            base_url=f"http://127.0.0.1:{redirect.server_port}/v1",
            api_key="must-not-leak",
            model="embed",
            dimension=3,
        )
        with pytest.raises(RuntimeError, match="embedding_transport_error"):
            provider.embed(["hello"])
        assert received_authorization == []
    finally:
        redirect.shutdown()
        target.shutdown()
        redirect.server_close()
        target.server_close()
        redirect_thread.join(timeout=2)
        target_thread.join(timeout=2)


def test_openai_embedding_rejects_remote_plaintext_http() -> None:
    with pytest.raises(ValueError, match="HTTPS"):
        OpenAICompatibleEmbeddingProvider(
            base_url="http://embeddings.example/v1",
            api_key="secret",
            model="embed",
            dimension=3,
        )


def test_openai_embedding_enforces_total_deadline_on_slow_drip() -> None:
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class SlowHandler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802
            self.send_response(200)
            self.send_header("Content-Length", "100")
            self.end_headers()
            for _ in range(10):
                try:
                    self.wfile.write(b"x")
                    self.wfile.flush()
                except OSError:
                    break
                time.sleep(0.04)

        def log_message(self, format: str, *args: object) -> None:
            return None

    server = ThreadingHTTPServer(("127.0.0.1", 0), SlowHandler)
    worker = threading.Thread(target=server.serve_forever)
    worker.start()
    try:
        provider = OpenAICompatibleEmbeddingProvider(
            base_url=f"http://127.0.0.1:{server.server_port}/v1",
            api_key="secret",
            model="embed",
            dimension=3,
            timeout_seconds=0.08,
        )
        started = time.monotonic()
        with pytest.raises(RuntimeError, match="embedding_timeout"):
            provider.embed(["hello"])
        assert time.monotonic() - started < 0.3
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=2)


def test_embedding_and_projection_fingerprint_mismatch_bypass_to_sqlite() -> None:
    repository = FakeRepository([_row("pg")])
    repository.state = _index_state(embedding_fingerprint="0" * 64)
    source = PostgresVectorCandidateSource(
        sqlite_source=SQLiteSource((_hit("sqlite"),)),
        config=PostgresVectorConfig(enabled=True, dsn="postgresql://host/db", vector_dimension=3),
        repository=repository,
        embedding_provider=StaticProvider(),
    )

    batch = source.search(_request())

    assert [hit.ref.record_id for hit in batch.hits] == ["sqlite"]
    assert batch.diagnostic_dict()["postgres"]["error_code"] == "embedding_fingerprint_mismatch"
    assert repository.requests == []


def test_static_authority_cursor_is_fresh_but_non_monotonic_cursor_fails_closed() -> None:
    class AuthoritySQLite(SQLiteSource):
        def __init__(self, head: tuple[str, str]) -> None:
            super().__init__((_hit("sqlite"),))
            self.head = head

        def authority_head(self) -> tuple[str, str]:
            return self.head

    sqlite = AuthoritySQLite(("2026-07-22T00:00:00Z", "key"))
    repository = FakeRepository([])
    repository.state = _index_state(lag_seconds=999_999.0)
    source = PostgresVectorCandidateSource(
        sqlite_source=sqlite,
        config=PostgresVectorConfig(enabled=True, dsn="postgresql://host/db", vector_dimension=3),
        repository=repository,
        embedding_provider=StaticProvider(),
    )

    assert source.search(_request()).diagnostic_dict()["postgres"]["state"] == "available"
    assert source.health()["lag_seconds"] == 0.0

    sqlite.head = ("2026-07-22T00:00:00Z", "higher-key")
    stale = source.search(_request()).diagnostic_dict()["postgres"]
    assert stale["state"] == "bypassed"
    assert stale["error_code"] == "index_lag_exceeded"
    assert stale["lag_seconds"] is None
    json.dumps(source.health(), allow_nan=False)
    json.dumps(stale, allow_nan=False)


def test_hostile_provider_health_is_allowlisted_and_cannot_claim_availability() -> None:
    class HostileHealthProvider(StaticProvider):
        def health(self) -> dict[str, object]:
            return {
                "available": True,
                "configured": True,
                "circuit": "postgresql://user:secret@host/db",
                "last_error": "Bearer secret",
                "url": "https://secret.example",
            }

    source = PostgresVectorCandidateSource(
        sqlite_source=SQLiteSource(()),
        config=PostgresVectorConfig(enabled=True, dsn="postgresql://host/db", vector_dimension=3),
        repository=FakeRepository([]),
        embedding_provider=HostileHealthProvider(),
    )

    health = source.health()
    assert health["available"] is False
    assert health["embedding"] == {
        "configured": True,
        "available": False,
        "circuit": "open",
        "dimension": 0,
        "last_error": "embedding_unavailable",
    }
    assert "secret" not in json.dumps(health)
