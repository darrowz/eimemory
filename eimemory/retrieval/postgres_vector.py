from __future__ import annotations

from collections import Counter, OrderedDict
from dataclasses import dataclass, field
import json
from math import isfinite
import re
from threading import BoundedSemaphore, Lock
from time import monotonic
from typing import Any, Callable, Mapping, Protocol, Sequence, runtime_checkable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from uuid import uuid4

from .contracts import CandidateBatch, CandidateHit, CandidateRef, CandidateRequest, CandidateSource


_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,62}\Z")
_MAX_VECTOR_DIMENSION = 65_536
_MAX_TOP_K = 1_000


@runtime_checkable
class EmbeddingProvider(Protocol):
    """Bounded semantic-vector provider; it has no memory write authority."""

    def embed(
        self,
        texts: list[str],
        *,
        timeout_seconds: float | None = None,
    ) -> list[tuple[float, ...]]: ...

    def health(self) -> dict[str, object]: ...


@dataclass(frozen=True, slots=True)
class IndexState:
    ready: bool = False
    watermark: str = ""
    lag_seconds: float | None = None
    authoritative_updated_at: str = ""
    completed_at: str = ""


@dataclass(frozen=True, slots=True)
class PostgresVectorConfig:
    enabled: bool = False
    dsn: str = field(default="", repr=False)
    connection_factory: Callable[..., Any] | None = field(default=None, repr=False, compare=False)
    embedding_provider: EmbeddingProvider | None = field(default=None, repr=False, compare=False)
    connect_timeout_seconds: float = 2.0
    statement_timeout_ms: int = 1_500
    pool_size: int = 2
    queue_bound: int = 16
    vector_dimension: int = 1_536
    schema: str = "eimemory_recall"
    table: str = "vector_candidates"
    max_index_lag_seconds: float = 300.0
    failure_threshold: int = 3
    cooldown_seconds: float = 30.0
    top_k_max: int = 100
    cache_entries: int = 128
    cache_ttl_seconds: float = 10.0
    release_id: str = ""

    def __post_init__(self) -> None:
        for label in ("schema", "table"):
            if not _IDENTIFIER.fullmatch(str(getattr(self, label) or "")):
                raise ValueError(f"invalid postgres identifier: {label}")
        object.__setattr__(self, "connect_timeout_seconds", _bounded_float(self.connect_timeout_seconds, 0.05, 60.0))
        object.__setattr__(self, "statement_timeout_ms", _bounded_int(self.statement_timeout_ms, 50, 120_000))
        object.__setattr__(self, "pool_size", _bounded_int(self.pool_size, 1, 32))
        object.__setattr__(self, "queue_bound", _bounded_int(self.queue_bound, 0, 1_024))
        object.__setattr__(self, "vector_dimension", _bounded_int(self.vector_dimension, 1, _MAX_VECTOR_DIMENSION))
        object.__setattr__(self, "max_index_lag_seconds", _bounded_float(self.max_index_lag_seconds, 0.0, 86_400.0))
        object.__setattr__(self, "failure_threshold", _bounded_int(self.failure_threshold, 1, 100))
        object.__setattr__(self, "cooldown_seconds", _bounded_float(self.cooldown_seconds, 0.05, 3_600.0))
        object.__setattr__(self, "top_k_max", _bounded_int(self.top_k_max, 1, _MAX_TOP_K))
        object.__setattr__(self, "cache_entries", _bounded_int(self.cache_entries, 0, 10_000))
        object.__setattr__(self, "cache_ttl_seconds", _bounded_float(self.cache_ttl_seconds, 0.0, 3_600.0))
        object.__setattr__(self, "release_id", str(self.release_id or "")[:128])

    @property
    def configured(self) -> bool:
        return bool(self.dsn or self.connection_factory is not None)


class _Circuit:
    def __init__(self, *, threshold: int, cooldown_seconds: float, clock: Callable[[], float]) -> None:
        self.threshold = threshold
        self.cooldown_seconds = cooldown_seconds
        self.clock = clock
        self.failures = 0
        self.opened_at: float | None = None
        self.probe_active = False
        self._lock = Lock()

    def allow(self) -> bool:
        with self._lock:
            if self.opened_at is None:
                return True
            if self.clock() - self.opened_at < self.cooldown_seconds or self.probe_active:
                return False
            self.probe_active = True
            return True

    def success(self) -> None:
        with self._lock:
            self.failures = 0
            self.opened_at = None
            self.probe_active = False

    def failure(self) -> None:
        with self._lock:
            self.probe_active = False
            self.failures += 1
            if self.failures >= self.threshold:
                self.opened_at = self.clock()

    def state(self) -> str:
        with self._lock:
            if self.opened_at is None:
                return "closed"
            if self.clock() - self.opened_at >= self.cooldown_seconds:
                return "half_open"
            return "open"


class OpenAICompatibleEmbeddingProvider:
    """Small stdlib `/embeddings` client with strict resource boundaries."""

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        dimension: int,
        transport: Callable[..., bytes] | None = None,
        max_batch: int = 32,
        max_text_chars: int = 16_000,
        max_request_bytes: int = 512_000,
        max_response_bytes: int = 4_000_000,
        timeout_seconds: float = 5.0,
        failure_threshold: int = 3,
        cooldown_seconds: float = 30.0,
        clock: Callable[[], float] = monotonic,
    ) -> None:
        self._base_url = str(base_url or "").rstrip("/")
        self._api_key = str(api_key or "")
        self._model = str(model or "")[:256]
        self.dimension = _bounded_int(dimension, 1, _MAX_VECTOR_DIMENSION)
        self.max_batch = _bounded_int(max_batch, 1, 1_000)
        self.max_text_chars = _bounded_int(max_text_chars, 1, 1_000_000)
        self.max_request_bytes = _bounded_int(max_request_bytes, 128, 16_000_000)
        self.max_response_bytes = _bounded_int(max_response_bytes, 32, 64_000_000)
        self.timeout_seconds = _bounded_float(timeout_seconds, 0.05, 120.0)
        self._transport = transport or _stdlib_embedding_transport
        self._circuit = _Circuit(
            threshold=_bounded_int(failure_threshold, 1, 100),
            cooldown_seconds=_bounded_float(cooldown_seconds, 0.05, 3_600.0),
            clock=clock,
        )
        self._last_error = ""

    def embed(
        self,
        texts: list[str],
        *,
        timeout_seconds: float | None = None,
    ) -> list[tuple[float, ...]]:
        if not self._circuit.allow():
            raise RuntimeError("circuit_open")
        try:
            if not self._base_url or not self._api_key or not self._model:
                raise RuntimeError("embedding_not_configured")
            bounded = [str(text or "")[: self.max_text_chars] for text in list(texts)[: self.max_batch]]
            if len(bounded) != len(texts) or not bounded:
                raise RuntimeError("embedding_batch_invalid")
            body = json.dumps(
                {"input": bounded, "model": self._model},
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
            if len(body) > self.max_request_bytes:
                raise RuntimeError("request_too_large")
            response = self._transport(
                url=f"{self._base_url}/embeddings",
                headers={
                    "Authorization": f"Bearer {self._api_key}",
                    "Content-Type": "application/json",
                },
                body=body,
                timeout_seconds=_bounded_float(
                    self.timeout_seconds if timeout_seconds is None else timeout_seconds,
                    0.05,
                    120.0,
                ),
                max_response_bytes=self.max_response_bytes,
            )
            if not isinstance(response, (bytes, bytearray)) or len(response) > self.max_response_bytes:
                raise RuntimeError("response_too_large")
            payload = json.loads(bytes(response).decode("utf-8"))
            data = payload.get("data") if isinstance(payload, Mapping) else None
            if not isinstance(data, list) or len(data) != len(bounded):
                raise RuntimeError("embedding_response_invalid")
            ordered: list[tuple[float, ...] | None] = [None] * len(bounded)
            for position, item in enumerate(data):
                if not isinstance(item, Mapping):
                    raise RuntimeError("embedding_response_invalid")
                index = item.get("index", position)
                if isinstance(index, bool) or not isinstance(index, int) or not 0 <= index < len(ordered):
                    raise RuntimeError("embedding_response_invalid")
                raw_vector = item.get("embedding")
                if not isinstance(raw_vector, list) or len(raw_vector) != self.dimension:
                    raise RuntimeError("embedding_dimension_mismatch")
                vector = tuple(float(value) for value in raw_vector)
                if not all(isfinite(value) for value in vector):
                    raise RuntimeError("embedding_response_invalid")
                ordered[index] = vector
            if any(vector is None for vector in ordered):
                raise RuntimeError("embedding_response_invalid")
            self._last_error = ""
            self._circuit.success()
            return [vector for vector in ordered if vector is not None]
        except Exception as exc:
            code = _error_code(exc, prefix="embedding")
            self._last_error = code
            self._circuit.failure()
            raise RuntimeError(code) from None

    def health(self) -> dict[str, object]:
        return {
            "configured": bool(self._base_url and self._api_key and self._model),
            "available": bool(self._base_url and self._api_key and self._model) and self._circuit.state() != "open",
            "circuit": self._circuit.state(),
            "dimension": self.dimension,
            "last_error": self._last_error,
        }


def _stdlib_embedding_transport(
    *,
    url: str,
    headers: Mapping[str, str],
    body: bytes,
    timeout_seconds: float,
    max_response_bytes: int,
) -> bytes:
    request = Request(url, data=body, headers=dict(headers), method="POST")
    with urlopen(request, timeout=timeout_seconds) as response:  # noqa: S310 - explicit configurable provider
        data = response.read(max_response_bytes + 1)
    if len(data) > max_response_bytes:
        raise RuntimeError("response_too_large")
    return data


class _ConnectionGate:
    def __init__(self, *, pool_size: int, queue_bound: int) -> None:
        self._slots = BoundedSemaphore(pool_size)
        self._queue_bound = queue_bound
        self._waiting = 0
        self._lock = Lock()

    def acquire(self, timeout: float) -> bool:
        with self._lock:
            if self._waiting >= self._queue_bound and not self._slots.acquire(blocking=False):
                return False
            if self._waiting < self._queue_bound:
                self._waiting += 1
                queued = True
            else:
                return True
        try:
            return self._slots.acquire(timeout=timeout)
        finally:
            if queued:
                with self._lock:
                    self._waiting -= 1

    def release(self) -> None:
        self._slots.release()


class PostgresCandidateRepository:
    """Prepared-SQL access to the non-authoritative candidate projection."""

    def __init__(self, config: PostgresVectorConfig) -> None:
        self.config = config
        self._gate = _ConnectionGate(pool_size=config.pool_size, queue_bound=config.queue_bound)

    @property
    def qualified_table(self) -> str:
        return f'"{self.config.schema}"."{self.config.table}"'

    @property
    def qualified_state_table(self) -> str:
        return f'"{self.config.schema}"."{self.config.table}_sync_state"'

    def _connect(self) -> Any:
        if not self._gate.acquire(self.config.connect_timeout_seconds):
            raise TimeoutError("connection_queue_full")
        try:
            factory = self.config.connection_factory
            if factory is None:
                try:
                    import psycopg  # type: ignore[import-not-found]
                    from psycopg.rows import dict_row  # type: ignore[import-not-found]
                except ImportError:
                    raise RuntimeError("postgres_dependency_unavailable") from None
                connection = psycopg.connect(
                    self.config.dsn,
                    connect_timeout=max(1, int(self.config.connect_timeout_seconds)),
                    row_factory=dict_row,
                )
            else:
                connection = factory(
                    dsn=self.config.dsn,
                    connect_timeout_seconds=self.config.connect_timeout_seconds,
                )
            return _GatedConnection(connection, self._gate)
        except Exception:
            self._gate.release()
            raise

    def read_index_state(self) -> IndexState:
        connection = self._connect()
        try:
            with connection.cursor() as cursor:
                self._set_timeout(cursor)
                cursor.execute(
                    f"SELECT ready, committed_watermark, "
                    "CASE WHEN authoritative_updated_at IS NULL THEN NULL ELSE "
                    "GREATEST(0, EXTRACT(EPOCH FROM (CURRENT_TIMESTAMP - authoritative_updated_at))) END AS lag_seconds, "
                    "authoritative_updated_at, completed_at "
                    f"FROM {self.qualified_state_table} WHERE singleton = %s",
                    (True,),
                )
                row = cursor.fetchone()
            if row is None:
                return IndexState()
            mapped = _row_mapping(row, getattr(cursor, "description", None))
            return IndexState(
                ready=bool(mapped.get("ready")),
                watermark=str(mapped.get("committed_watermark") or "")[:256],
                lag_seconds=_optional_nonnegative_float(mapped.get("lag_seconds")),
                authoritative_updated_at=str(mapped.get("authoritative_updated_at") or "")[:64],
                completed_at=str(mapped.get("completed_at") or "")[:64],
            )
        finally:
            connection.close()

    def migrate(self) -> dict[str, object]:
        from .postgres_ddl import DDL_VERSION, build_candidate_projection_ddl

        connection = self._connect()
        try:
            with connection.cursor() as cursor:
                self._set_timeout(cursor)
                for statement in build_candidate_projection_ddl(self.config):
                    cursor.execute(statement)
            connection.commit()
            return {"ok": True, "ddl_version": DDL_VERSION}
        except Exception:
            connection.rollback()
            raise RuntimeError("postgres_migration_failed") from None
        finally:
            connection.close()

    def begin_or_resume_sync(self) -> Any:
        from .postgres_sync import ProjectionCursor, SyncProgress

        connection = self._connect()
        try:
            with connection.cursor() as cursor:
                self._set_timeout(cursor)
                cursor.execute(
                    f"SELECT run_id, in_progress, cursor_updated_at, cursor_storage_key "
                    f"FROM {self.qualified_state_table} WHERE singleton = %s FOR UPDATE",
                    (True,),
                )
                raw = cursor.fetchone()
                if raw is None:
                    raise RuntimeError("postgres_sync_state_missing")
                row = _row_mapping(raw, getattr(cursor, "description", None))
                existing_run = str(row.get("run_id") or "")[:256]
                if bool(row.get("in_progress")) and existing_run:
                    progress = SyncProgress(
                        run_id=existing_run,
                        cursor=ProjectionCursor(
                            str(row.get("cursor_updated_at") or ""),
                            str(row.get("cursor_storage_key") or ""),
                        ),
                        resumed=True,
                    )
                else:
                    run_id = f"sync-{uuid4().hex}"
                    cursor.execute(
                        f"UPDATE {self.qualified_state_table} SET "
                        "in_progress = TRUE, run_id = %s, cursor_updated_at = '', "
                        "cursor_storage_key = '', updated_at = CURRENT_TIMESTAMP WHERE singleton = %s",
                        (run_id, True),
                    )
                    progress = SyncProgress(run_id=run_id, cursor=ProjectionCursor(), resumed=False)
            connection.commit()
            return progress
        except Exception:
            connection.rollback()
            raise RuntimeError("postgres_sync_begin_failed") from None
        finally:
            connection.close()

    def apply_sync_page(
        self,
        *,
        run_id: str,
        projections: list[dict[str, Any]],
        expected_cursor: Any,
        next_cursor: Any,
        complete: bool,
    ) -> None:
        normalized_run_id = str(run_id or "").strip()[:256]
        if not normalized_run_id or len(projections) > 256:
            raise RuntimeError("postgres_sync_apply_failed")
        connection = self._connect()
        try:
            with connection.cursor() as cursor:
                self._set_timeout(cursor)
                cursor.execute(
                    f"SELECT run_id, in_progress, cursor_updated_at, cursor_storage_key "
                    f"FROM {self.qualified_state_table} "
                    "WHERE singleton = %s FOR UPDATE",
                    (True,),
                )
                raw_state = cursor.fetchone()
                state = _row_mapping(raw_state, getattr(cursor, "description", None)) if raw_state is not None else {}
                if str(state.get("run_id") or "") != normalized_run_id or not bool(state.get("in_progress")):
                    raise RuntimeError("postgres_sync_conflict")
                if (
                    str(state.get("cursor_updated_at") or "") != str(expected_cursor.updated_at or "")
                    or str(state.get("cursor_storage_key") or "") != str(expected_cursor.storage_key or "")
                ):
                    raise RuntimeError("postgres_sync_conflict")
                upsert_sql = (
                    f"INSERT INTO {self.qualified_table} ("
                    "storage_key, record_id, tenant_id, agent_id, workspace_id, user_id, "
                    "source_id, kind, status, embedding, title_text, alias_text, keyword_text, "
                    "search_tsv, payload_digest, authoritative_updated_at, index_watermark, indexed_at"
                    ") VALUES ("
                    "%s, %s, %s, %s, %s, %s, %s, %s, %s, CAST(%s AS vector), %s, %s, %s, "
                    "to_tsvector('simple', %s), %s, %s, %s, CURRENT_TIMESTAMP"
                    ") ON CONFLICT (storage_key) DO UPDATE SET "
                    "record_id=EXCLUDED.record_id, tenant_id=EXCLUDED.tenant_id, agent_id=EXCLUDED.agent_id, "
                    "workspace_id=EXCLUDED.workspace_id, user_id=EXCLUDED.user_id, source_id=EXCLUDED.source_id, "
                    "kind=EXCLUDED.kind, status=EXCLUDED.status, embedding=EXCLUDED.embedding, "
                    "title_text=EXCLUDED.title_text, alias_text=EXCLUDED.alias_text, "
                    "keyword_text=EXCLUDED.keyword_text, search_tsv=EXCLUDED.search_tsv, "
                    "payload_digest=EXCLUDED.payload_digest, "
                    "authoritative_updated_at=EXCLUDED.authoritative_updated_at, "
                    "index_watermark=EXCLUDED.index_watermark, indexed_at=CURRENT_TIMESTAMP"
                )
                values = []
                for projection in projections:
                    title = str(projection.get("title_text") or "")
                    aliases = str(projection.get("alias_text") or "")
                    keywords = str(projection.get("keyword_text") or "")
                    values.append(
                        (
                            str(projection.get("storage_key") or "")[:512],
                            str(projection.get("record_id") or "")[:256],
                            str(projection.get("tenant_id") or "default")[:256],
                            str(projection.get("agent_id") or "")[:256],
                            str(projection.get("workspace_id") or "")[:512],
                            str(projection.get("user_id") or "")[:256],
                            str(projection.get("source_id") or "default")[:256],
                            str(projection.get("kind") or "")[:128],
                            str(projection.get("status") or "")[:64],
                            _vector_literal(
                                tuple(projection.get("embedding") or ()),
                                expected_dimension=self.config.vector_dimension,
                            ),
                            title,
                            aliases,
                            keywords,
                            " ".join((title, aliases, keywords)),
                            str(projection.get("payload_digest") or "")[:64],
                            str(projection.get("updated_at") or "")[:64],
                            normalized_run_id,
                        )
                    )
                if values:
                    cursor.executemany(upsert_sql, values)
                if complete:
                    cursor.execute(
                        f"DELETE FROM {self.qualified_table} WHERE index_watermark <> %s",
                        (normalized_run_id,),
                    )
                    cursor.execute(
                        f"UPDATE {self.qualified_state_table} SET "
                        "ready = TRUE, in_progress = FALSE, committed_watermark = %s, "
                        "authoritative_updated_at = NULLIF(%s, '')::timestamptz, "
                        "completed_at = CURRENT_TIMESTAMP, cursor_updated_at = %s, cursor_storage_key = %s, "
                        "updated_at = CURRENT_TIMESTAMP WHERE singleton = %s AND run_id = %s",
                        (
                            normalized_run_id,
                            str(next_cursor.updated_at or "")[:64],
                            str(next_cursor.updated_at or "")[:64],
                            str(next_cursor.storage_key or "")[:512],
                            True,
                            normalized_run_id,
                        ),
                    )
                else:
                    cursor.execute(
                        f"UPDATE {self.qualified_state_table} SET cursor_updated_at = %s, "
                        "cursor_storage_key = %s, updated_at = CURRENT_TIMESTAMP "
                        "WHERE singleton = %s AND run_id = %s AND in_progress = TRUE",
                        (
                            str(next_cursor.updated_at or "")[:64],
                            str(next_cursor.storage_key or "")[:512],
                            True,
                            normalized_run_id,
                        ),
                    )
            connection.commit()
        except Exception:
            connection.rollback()
            raise RuntimeError("postgres_sync_apply_failed") from None
        finally:
            connection.close()

    def search(
        self,
        request: CandidateRequest,
        vector: tuple[float, ...],
        *,
        top_k: int,
        watermark: str,
    ) -> list[dict[str, Any]]:
        bounded_top_k = min(self.config.top_k_max, max(1, int(top_k)))
        where = [
            "tenant_id = %s",
            "agent_id = %s",
            "workspace_id = %s",
            "user_id = %s",
            "status = %s",
            "embedding IS NOT NULL",
        ]
        params: list[Any] = [
            request.scope.tenant_id,
            request.scope.agent_id,
            request.scope.workspace_id,
            request.scope.user_id,
            "active",
        ]
        if request.source_ids is not None:
            where.append("source_id = ANY(%s)")
            params.append(tuple(request.source_ids))
        if request.kinds:
            where.append("kind = ANY(%s)")
            params.append(tuple(request.kinds))
        where.append("index_watermark = %s")
        params.append(str(watermark or "")[:256])
        vector_literal = _vector_literal(vector, expected_dimension=self.config.vector_dimension)
        sql = (
            "SELECT storage_key, record_id, tenant_id, agent_id, workspace_id, user_id, "
            "source_id, kind, status, payload_digest, index_watermark, "
            "1 - (embedding <=> CAST(%s AS vector)) AS vector_score "
            f"FROM {self.qualified_table} WHERE "
            + " AND ".join(where)
            + " ORDER BY embedding <=> CAST(%s AS vector), storage_key LIMIT %s"
        )
        query_params = (vector_literal, *params, vector_literal, bounded_top_k)
        connection = self._connect()
        try:
            with connection.cursor() as cursor:
                self._set_timeout(cursor)
                cursor.execute(sql, query_params)
                rows = cursor.fetchall()
                description = getattr(cursor, "description", None)
            return [_row_mapping(row, description) for row in rows]
        finally:
            connection.close()

    def _set_timeout(self, cursor: Any) -> None:
        cursor.execute(
            "SELECT set_config('statement_timeout', %s, true)",
            (f"{self.config.statement_timeout_ms}ms",),
        )


class _GatedConnection:
    def __init__(self, connection: Any, gate: _ConnectionGate) -> None:
        self._connection = connection
        self._gate = gate
        self._closed = False

    def __getattr__(self, name: str) -> Any:
        return getattr(self._connection, name)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._connection.close()
        finally:
            self._gate.release()


class PostgresVectorCandidateSource:
    """SQLite-first candidate source augmented by a bypassable vector projection."""

    name = "sqlite+postgres_vector"
    policy_version = "postgres-vector-candidates.v1"
    sqlite_authority = True

    def __init__(
        self,
        *,
        sqlite_source: CandidateSource,
        config: PostgresVectorConfig,
        repository: Any | None = None,
        embedding_provider: EmbeddingProvider | None = None,
        clock: Callable[[], float] = monotonic,
    ) -> None:
        self.sqlite_source = sqlite_source
        self.config = config
        self.repository = repository or PostgresCandidateRepository(config)
        self.embedding_provider = embedding_provider or config.embedding_provider
        self._clock = clock
        self._circuit = _Circuit(
            threshold=config.failure_threshold,
            cooldown_seconds=config.cooldown_seconds,
            clock=clock,
        )
        self._last_error = ""
        self._last_state = IndexState()
        self._last_query_valid = False
        self._cache: OrderedDict[tuple[Any, ...], tuple[float, tuple[dict[str, Any], ...]]] = OrderedDict()
        self._cache_lock = Lock()

    def search(self, request: CandidateRequest) -> CandidateBatch:
        sqlite_batch = self.sqlite_source.search(request)
        if not self.config.enabled:
            return self._batch(sqlite_batch, request=request, state="bypassed", error_code="disabled")
        if not self.config.configured or self.embedding_provider is None:
            return self._batch(sqlite_batch, request=request, state="bypassed", error_code="not_configured")
        if not self._circuit.allow():
            return self._batch(sqlite_batch, request=request, state="bypassed", error_code="circuit_open")
        try:
            index_state = self.repository.read_index_state()
            self._last_state = index_state
            if not index_state.ready or not index_state.watermark:
                raise RuntimeError("index_not_ready")
            if (
                index_state.lag_seconds is not None
                and index_state.lag_seconds > self.config.max_index_lag_seconds
            ):
                raise RuntimeError("index_lag_exceeded")
            cache_key = self._cache_key(request, watermark=index_state.watermark)
            cached = self._cache_get(cache_key)
            if cached is None:
                vectors = self.embedding_provider.embed(
                    [request.query],
                    timeout_seconds=self.config.connect_timeout_seconds,
                )
                if len(vectors) != 1 or len(vectors[0]) != self.config.vector_dimension:
                    raise RuntimeError("embedding_dimension_mismatch")
                rows = self.repository.search(
                    request,
                    vectors[0],
                    top_k=min(self.config.top_k_max, max(request.limit, 1)),
                    watermark=index_state.watermark,
                )
                cached = tuple(dict(row) for row in rows[: self.config.top_k_max])
                self._cache_put(cache_key, cached)
            hits, drops = self._validated_hits(request, cached, watermark=index_state.watermark)
            merged = _merge_hits(sqlite_batch.hits, hits, limit=request.limit)
            self._last_error = ""
            self._last_query_valid = True
            self._circuit.success()
            return self._batch(
                sqlite_batch,
                request=request,
                state="available",
                hits=merged,
                pg_count=len(hits),
                drops=drops,
                valid_empty=not hits,
            )
        except Exception as exc:
            code = _error_code(exc, prefix="postgres")
            self._last_error = code
            self._last_query_valid = False
            self._circuit.failure()
            return self._batch(sqlite_batch, request=request, state="bypassed", error_code=code)

    def health(self) -> dict[str, object]:
        provider_health = self.embedding_provider.health() if self.embedding_provider is not None else {}
        return {
            "enabled": self.config.enabled,
            "configured": self.config.configured and self.embedding_provider is not None,
            "available": self._last_query_valid and self._circuit.state() != "open",
            "circuit": self._circuit.state(),
            "lag_seconds": self._last_state.lag_seconds,
            "watermark": self._last_state.watermark,
            "last_error": self._last_error,
            "cache_entries": len(self._cache),
            "embedding": {
                key: value
                for key, value in dict(provider_health).items()
                if key in {"configured", "available", "circuit", "dimension", "last_error"}
            },
        }

    def _validated_hits(
        self,
        request: CandidateRequest,
        rows: Sequence[Mapping[str, Any]],
        *,
        watermark: str,
    ) -> tuple[tuple[CandidateHit, ...], dict[str, int]]:
        drops: Counter[str] = Counter()
        hits: list[CandidateHit] = []
        for row in rows[: self.config.top_k_max]:
            record_id = str(row.get("record_id") or "").strip()[:256]
            if not record_id:
                drops["invalid_ref"] += 1
                continue
            if any(
                str(row.get(field) or default) != expected
                for field, default, expected in (
                    ("tenant_id", "default", request.scope.tenant_id),
                    ("agent_id", "", request.scope.agent_id),
                    ("workspace_id", "", request.scope.workspace_id),
                    ("user_id", "", request.scope.user_id),
                )
            ):
                drops["scope_not_allowed"] += 1
                continue
            source_id = str(row.get("source_id") or "")
            if not source_id:
                drops["invalid_ref"] += 1
                continue
            if request.source_ids is not None and source_id not in request.source_ids:
                drops["source_not_allowed"] += 1
                continue
            if request.kinds and str(row.get("kind") or "") not in request.kinds:
                drops["kind_not_allowed"] += 1
                continue
            if str(row.get("status") or "") != "active":
                drops["status_not_active"] += 1
                continue
            if str(row.get("index_watermark") or "") != watermark:
                drops["stale_watermark"] += 1
                continue
            if not re.fullmatch(r"[0-9a-f]{64}", str(row.get("payload_digest") or "")):
                drops["digest_invalid"] += 1
                continue
            score = _bounded_score(row.get("vector_score"))
            try:
                ref = CandidateRef(record_id, request.scope, source_id)
            except (TypeError, ValueError):
                drops["invalid_ref"] += 1
                continue
            hits.append(
                CandidateHit(
                    ref=ref,
                    source_rank=len(hits) + 1,
                    source_score=score,
                    component_hints={"vector_score": score},
                    evidence_hints=("vector_match",),
                )
            )
        return tuple(hits), dict(sorted(drops.items()))

    def _batch(
        self,
        sqlite_batch: CandidateBatch,
        *,
        request: CandidateRequest,
        state: str,
        error_code: str = "",
        hits: tuple[CandidateHit, ...] | None = None,
        pg_count: int = 0,
        drops: Mapping[str, int] | None = None,
        valid_empty: bool = False,
    ) -> CandidateBatch:
        final_hits = tuple(sqlite_batch.hits if hits is None else hits)
        return CandidateBatch(
            hits=final_hits,
            diagnostics={
                "source_name": self.name,
                "candidate_count": len(final_hits),
                "candidate_limit": request.limit,
                "returned_count": len(final_hits),
                "policy_version": self.policy_version,
                "postgres": {
                    "state": state,
                    "fallback": state == "bypassed",
                    "error_code": error_code,
                    "candidate_count": pg_count,
                    "valid_empty": valid_empty,
                    "top_k": min(self.config.top_k_max, max(request.limit, 1)),
                    "drops": dict(drops or {}),
                    "watermark": self._last_state.watermark,
                    "lag_seconds": self._last_state.lag_seconds,
                },
            },
        )

    def _cache_key(self, request: CandidateRequest, *, watermark: str) -> tuple[Any, ...]:
        context = request.task_context_dict()
        return (
            request.query,
            request.scope,
            request.source_ids,
            request.kinds,
            tuple(request.recall_filters),
            str(context.get("retrieval_policy_digest") or self.policy_version),
            str(context.get("release_commit") or self.config.release_id),
            watermark,
        )

    def _cache_get(self, key: tuple[Any, ...]) -> tuple[dict[str, Any], ...] | None:
        if self.config.cache_entries <= 0 or self.config.cache_ttl_seconds <= 0:
            return None
        with self._cache_lock:
            entry = self._cache.get(key)
            if entry is None:
                return None
            expires_at, rows = entry
            if expires_at < self._clock():
                self._cache.pop(key, None)
                return None
            self._cache.move_to_end(key)
            return rows

    def _cache_put(self, key: tuple[Any, ...], rows: tuple[dict[str, Any], ...]) -> None:
        if self.config.cache_entries <= 0 or self.config.cache_ttl_seconds <= 0:
            return
        with self._cache_lock:
            self._cache[key] = (self._clock() + self.config.cache_ttl_seconds, rows)
            self._cache.move_to_end(key)
            while len(self._cache) > self.config.cache_entries:
                self._cache.popitem(last=False)


def build_postgres_vector_candidate_source(
    store: Any,
    config: PostgresVectorConfig,
    *,
    repository: Any | None = None,
    embedding_provider: EmbeddingProvider | None = None,
) -> PostgresVectorCandidateSource:
    """Build the supported SQLite-authority/Postgres-candidate composition."""

    from .sqlite_source import SQLiteCandidateSource

    return PostgresVectorCandidateSource(
        sqlite_source=SQLiteCandidateSource(store),
        config=config,
        repository=repository,
        embedding_provider=embedding_provider,
    )


def _merge_hits(
    sqlite_hits: Sequence[CandidateHit],
    postgres_hits: Sequence[CandidateHit],
    *,
    limit: int,
) -> tuple[CandidateHit, ...]:
    ordered = list(sqlite_hits)
    positions = {(hit.ref.record_id, hit.ref.scope, hit.ref.source_id): index for index, hit in enumerate(ordered)}
    for hit in postgres_hits:
        key = (hit.ref.record_id, hit.ref.scope, hit.ref.source_id)
        existing_index = positions.get(key)
        if existing_index is None:
            positions[key] = len(ordered)
            ordered.append(hit)
            continue
        existing = ordered[existing_index]
        hints = existing.component_dict()
        hints.update(hit.component_dict())
        ordered[existing_index] = CandidateHit(
            ref=existing.ref,
            source_rank=existing.source_rank,
            source_score=max(existing.source_score, hit.source_score),
            component_hints=hints,
            evidence_hints=tuple(dict.fromkeys((*existing.evidence_hints, *hit.evidence_hints))),
        )
    return tuple(ordered[: max(0, int(limit))])


def _vector_literal(vector: Sequence[float], *, expected_dimension: int) -> str:
    if len(vector) != expected_dimension:
        raise RuntimeError("embedding_dimension_mismatch")
    values = [float(value) for value in vector]
    if not all(isfinite(value) for value in values):
        raise RuntimeError("embedding_response_invalid")
    return "[" + ",".join(format(value, ".12g") for value in values) + "]"


def _row_mapping(row: Any, description: Any) -> dict[str, Any]:
    if isinstance(row, Mapping):
        return dict(row)
    if isinstance(row, (list, tuple)) and description:
        names = [str(item[0] if isinstance(item, (list, tuple)) else getattr(item, "name", "")) for item in description]
        return dict(zip(names, row, strict=False))
    raise RuntimeError("postgres_row_invalid")


def _error_code(exc: Exception, *, prefix: str) -> str:
    text = str(exc)
    allowed = {
        "circuit_open",
        "embedding_not_configured",
        "embedding_batch_invalid",
        "embedding_dimension_mismatch",
        "embedding_timeout",
        "embedding_response_invalid",
        "index_lag_exceeded",
        "index_not_ready",
        "postgres_dependency_unavailable",
        "request_too_large",
        "response_too_large",
    }
    if text in allowed:
        return text
    if isinstance(exc, (TimeoutError,)):
        return f"{prefix}_timeout"
    if isinstance(exc, (HTTPError, URLError)):
        return f"{prefix}_transport_error"
    if isinstance(exc, (json.JSONDecodeError, UnicodeError, TypeError, ValueError)):
        return f"{prefix}_response_invalid"
    return f"{prefix}_unavailable"


def _bounded_int(value: object, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        parsed = minimum
    return max(minimum, min(maximum, parsed))


def _bounded_float(value: object, minimum: float, maximum: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        parsed = minimum
    if not isfinite(parsed):
        parsed = minimum
    return max(minimum, min(maximum, parsed))


def _optional_nonnegative_float(value: object) -> float | None:
    if value is None:
        return None
    return _bounded_float(value, 0.0, 10_000_000.0)


def _bounded_score(value: object) -> float:
    return _bounded_float(value, -1.0, 1.0)
