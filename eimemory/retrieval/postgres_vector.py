from __future__ import annotations

from collections import Counter, OrderedDict
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from hashlib import sha256
import http.client
import ipaddress
import json
from math import isfinite
import re
from threading import BoundedSemaphore, Lock
from time import monotonic
from typing import Any, Callable, Mapping, Protocol, Sequence, runtime_checkable
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from uuid import uuid4

from .contracts import CandidateBatch, CandidateHit, CandidateRef, CandidateRequest, CandidateSource


_IDENTIFIER = re.compile(r"[a-z_][a-z0-9_]{0,47}\Z")
_MAX_VECTOR_DIMENSION = 2_000
_MAX_TOP_K = 1_000
PROJECTION_DIGEST_SCHEMA = "candidate-projection.v1"
_EMBEDDING_HEALTH_ERROR_CODES = frozenset({
    "",
    "circuit_open",
    "embedding_batch_invalid",
    "embedding_dimension_mismatch",
    "embedding_http_error",
    "embedding_not_configured",
    "embedding_response_invalid",
    "embedding_timeout",
    "embedding_transport_error",
    "embedding_unavailable",
    "redirect_rejected",
    "request_too_large",
    "response_too_large",
})


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

    def fingerprint(self) -> str: ...


@dataclass(frozen=True, slots=True)
class IndexState:
    ready: bool = False
    watermark: str = ""
    lag_seconds: float | None = None
    authoritative_updated_at: str = ""
    authoritative_storage_key: str = ""
    completed_at: str = ""
    embedding_fingerprint: str = ""
    projection_digest_schema: str = ""
    projection_fingerprint: str = ""
    authority_revision: str = ""


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
    embedding_fingerprint: str = ""
    projection_text_chars: int = 16_000
    embedding_queue_timeout_seconds: float = 2.0
    sync_lease_seconds: float = 60.0

    def __post_init__(self) -> None:
        for label in ("schema", "table"):
            if not _IDENTIFIER.fullmatch(str(getattr(self, label) or "")):
                raise ValueError(f"invalid postgres identifier: {label}")
        object.__setattr__(self, "connect_timeout_seconds", _bounded_float(self.connect_timeout_seconds, 0.05, 60.0))
        object.__setattr__(self, "statement_timeout_ms", _bounded_int(self.statement_timeout_ms, 50, 120_000))
        object.__setattr__(self, "pool_size", _bounded_int(self.pool_size, 1, 32))
        object.__setattr__(self, "queue_bound", _bounded_int(self.queue_bound, 0, 1_024))
        object.__setattr__(self, "vector_dimension", _vector_dimension(self.vector_dimension))
        object.__setattr__(self, "max_index_lag_seconds", _bounded_float(self.max_index_lag_seconds, 0.0, 86_400.0))
        object.__setattr__(self, "failure_threshold", _bounded_int(self.failure_threshold, 1, 100))
        object.__setattr__(self, "cooldown_seconds", _bounded_float(self.cooldown_seconds, 0.05, 3_600.0))
        object.__setattr__(self, "top_k_max", _bounded_int(self.top_k_max, 1, _MAX_TOP_K))
        object.__setattr__(self, "cache_entries", _bounded_int(self.cache_entries, 0, 10_000))
        object.__setattr__(self, "cache_ttl_seconds", _bounded_float(self.cache_ttl_seconds, 0.0, 3_600.0))
        object.__setattr__(self, "release_id", str(self.release_id or "")[:128])
        supplied_fingerprint = str(self.embedding_fingerprint or "").strip().lower()
        if supplied_fingerprint and not re.fullmatch(r"[0-9a-f]{64}", supplied_fingerprint):
            raise ValueError("embedding_fingerprint must be a sha256 hex digest")
        object.__setattr__(self, "embedding_fingerprint", supplied_fingerprint)
        object.__setattr__(self, "projection_text_chars", _bounded_int(self.projection_text_chars, 1, 64_000))
        object.__setattr__(self, "embedding_queue_timeout_seconds", _bounded_float(self.embedding_queue_timeout_seconds, 0.05, 60.0))
        object.__setattr__(self, "sync_lease_seconds", _bounded_float(self.sync_lease_seconds, 5.0, 3_600.0))

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
        _validate_embedding_base_url(self._base_url)
        self._api_key = str(api_key or "")
        self._model = str(model or "")[:256]
        self.dimension = _vector_dimension(dimension)
        self.max_batch = _bounded_int(max_batch, 1, 256)
        self.max_text_chars = _bounded_int(max_text_chars, 1, 64_000)
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

    def fingerprint(self) -> str:
        payload = {
            "provider": "openai-compatible-embeddings.v1",
            "base_url": self._base_url,
            "model": self._model,
            "dimension": self.dimension,
            "max_text_chars": self.max_text_chars,
        }
        return sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()

    def effective_identity(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "provider_type": "openai-compatible-embeddings.v1",
            "model": self._model if re.fullmatch(r"[A-Za-z0-9_.:/-]{1,256}", self._model) else "",
            "fingerprint": self.fingerprint(),
        }


def _stdlib_embedding_transport(
    *,
    url: str,
    headers: Mapping[str, str],
    body: bytes,
    timeout_seconds: float,
    max_response_bytes: int,
) -> bytes:
    parsed = urlsplit(url)
    _validate_embedding_base_url(f"{parsed.scheme}://{parsed.netloc}")
    deadline = monotonic() + timeout_seconds
    connection_class = http.client.HTTPSConnection if parsed.scheme == "https" else http.client.HTTPConnection
    connection = connection_class(
        parsed.hostname,
        port=parsed.port,
        timeout=max(0.05, deadline - monotonic()),
    )
    try:
        path = parsed.path or "/"
        if parsed.query:
            path += f"?{parsed.query}"
        connection.request("POST", path, body=body, headers=dict(headers))
        response = connection.getresponse()
        if 300 <= response.status < 400:
            raise RuntimeError("redirect_rejected")
        if response.status < 200 or response.status >= 300:
            raise RuntimeError("embedding_http_error")
        chunks: list[bytes] = []
        total = 0
        while True:
            remaining = deadline - monotonic()
            if remaining <= 0:
                raise TimeoutError("embedding_timeout")
            sock = getattr(connection, "sock", None)
            if sock is not None:
                sock.settimeout(max(0.05, remaining))
            read_fn = getattr(response, "read1", response.read)
            chunk = read_fn(min(65_536, max_response_bytes + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > max_response_bytes:
                raise RuntimeError("response_too_large")
        return b"".join(chunks)
    finally:
        connection.close()


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
        return f'"{self.config.schema}"."{_derived_identifier(self.config, "state")}"'

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
                    "CASE WHEN completed_at IS NULL THEN NULL ELSE "
                    "GREATEST(0, EXTRACT(EPOCH FROM (CURRENT_TIMESTAMP - completed_at))) END AS lag_seconds, "
                    "authoritative_updated_at, authoritative_storage_key, completed_at, "
                    "embedding_fingerprint, projection_digest_schema, projection_fingerprint, "
                    "authority_revision "
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
                authoritative_updated_at=_canonical_timestamp(mapped.get("authoritative_updated_at")),
                authoritative_storage_key=str(mapped.get("authoritative_storage_key") or "")[:512],
                completed_at=str(mapped.get("completed_at") or "")[:64],
                embedding_fingerprint=str(mapped.get("embedding_fingerprint") or "")[:64],
                projection_digest_schema=str(mapped.get("projection_digest_schema") or "")[:64],
                projection_fingerprint=str(mapped.get("projection_fingerprint") or "")[:64],
                authority_revision=str(mapped.get("authority_revision") or "")[:64],
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
                self._validate_projection_schema(cursor)
            connection.commit()
            return {"ok": True, "ddl_version": DDL_VERSION}
        except Exception:
            connection.rollback()
            raise RuntimeError("postgres_migration_failed") from None
        finally:
            connection.close()

    def begin_or_resume_sync(
        self, *, embedding_fingerprint: str, projection_digest_schema: str,
        projection_fingerprint: str, lease_owner: str,
        authority_revision: str,
    ) -> Any:
        from .postgres_sync import ProjectionCursor, SyncProgress

        connection = self._connect()
        try:
            with connection.cursor() as cursor:
                self._set_timeout(cursor)
                cursor.execute(
                    f"SELECT run_id, in_progress, cursor_updated_at, cursor_storage_key, "
                    "embedding_fingerprint, projection_digest_schema, projection_fingerprint, "
                    "lease_owner, (lease_expires_at > CURRENT_TIMESTAMP) AS lease_active "
                    ", authority_revision, committed_watermark, staging_embedding_fingerprint, "
                    "staging_projection_digest_schema, staging_projection_fingerprint, "
                    "staging_authority_revision "
                    f"FROM {self.qualified_state_table} WHERE singleton = %s FOR UPDATE",
                    (True,),
                )
                raw = cursor.fetchone()
                if raw is None:
                    raise RuntimeError("postgres_sync_state_missing")
                row = _row_mapping(raw, getattr(cursor, "description", None))
                existing_run = str(row.get("run_id") or "")[:256]
                in_progress = bool(row.get("in_progress")) and bool(existing_run)
                compatible = (
                    str(row.get("staging_embedding_fingerprint") or "") == embedding_fingerprint
                    and str(row.get("staging_projection_digest_schema") or "") == projection_digest_schema
                    and str(row.get("staging_projection_fingerprint") or "") == projection_fingerprint
                    and str(row.get("staging_authority_revision") or "") == authority_revision
                )
                if in_progress:
                    if bool(row.get("lease_active")) and str(row.get("lease_owner") or "") != lease_owner:
                        raise RuntimeError("postgres_sync_lease_held")
                if in_progress and compatible:
                    progress = SyncProgress(
                        run_id=existing_run,
                        cursor=ProjectionCursor(
                            str(row.get("cursor_updated_at") or ""),
                            str(row.get("cursor_storage_key") or ""),
                        ),
                        resumed=True,
                        embedding_fingerprint=embedding_fingerprint,
                        projection_digest_schema=projection_digest_schema,
                        projection_fingerprint=projection_fingerprint,
                        lease_owner=lease_owner,
                        authority_revision=authority_revision,
                    )
                    cursor.execute(
                        f"UPDATE {self.qualified_state_table} SET lease_owner = %s, "
                        "lease_expires_at = CURRENT_TIMESTAMP + (%s * INTERVAL '1 second'), "
                        "updated_at = CURRENT_TIMESTAMP WHERE singleton = %s AND run_id = %s",
                        (lease_owner, self.config.sync_lease_seconds, True, existing_run),
                    )
                else:
                    run_id = f"sync-{uuid4().hex}"
                    committed = str(row.get("committed_watermark") or "")[:256]
                    if committed:
                        cursor.execute(
                            f"DELETE FROM {self.qualified_table} WHERE index_watermark <> %s",
                            (committed,),
                        )
                    else:
                        cursor.execute(f"DELETE FROM {self.qualified_table}")
                    cursor.execute(
                        f"UPDATE {self.qualified_state_table} SET "
                        "in_progress = TRUE, run_id = %s, cursor_updated_at = '', "
                        "cursor_storage_key = '', staging_embedding_fingerprint = %s, "
                        "staging_projection_digest_schema = %s, staging_projection_fingerprint = %s, "
                        "staging_authority_revision = %s, "
                        "lease_owner = %s, lease_expires_at = CURRENT_TIMESTAMP + (%s * INTERVAL '1 second'), "
                        "updated_at = CURRENT_TIMESTAMP WHERE singleton = %s",
                        (run_id, embedding_fingerprint, projection_digest_schema, projection_fingerprint,
                         authority_revision,
                         lease_owner, self.config.sync_lease_seconds, True),
                    )
                    progress = SyncProgress(
                        run_id=run_id,
                        cursor=ProjectionCursor(),
                        resumed=False,
                        embedding_fingerprint=embedding_fingerprint,
                        projection_digest_schema=projection_digest_schema,
                        projection_fingerprint=projection_fingerprint,
                        lease_owner=lease_owner,
                        authority_revision=authority_revision,
                    )
            connection.commit()
            return progress
        except Exception as exc:
            connection.rollback()
            code = str(exc)
            if code in {"postgres_sync_lease_held", "postgres_sync_fingerprint_mismatch"}:
                raise RuntimeError(code) from None
            raise RuntimeError("postgres_sync_begin_failed") from None
        finally:
            connection.close()

    def _validate_projection_schema(self, cursor: Any) -> None:
        table_regclass = f"{self.config.schema}.{self.config.table}"
        state_regclass = f"{self.config.schema}.{_derived_identifier(self.config, 'state')}"
        from .postgres_ddl import DDL_VERSION
        migration_table = (
            f'"{self.config.schema}"."{_derived_identifier(self.config, "migrations")}"'
        )
        cursor.execute(
            f"""
            SELECT
                (SELECT format_type(a.atttypid, a.atttypmod)
                 FROM pg_attribute AS a
                 WHERE a.attrelid = to_regclass(%s) AND a.attname = 'embedding'
                   AND a.attnum > 0 AND NOT a.attisdropped) AS embedding_type,
                (SELECT pg_get_constraintdef(c.oid)
                 FROM pg_constraint AS c
                 WHERE c.conrelid = to_regclass(%s) AND c.contype = 'p') AS primary_key,
                (SELECT indexdef FROM pg_indexes
                 WHERE schemaname = %s AND tablename = %s AND indexname = %s) AS hnsw_index,
                (SELECT indexdef FROM pg_indexes
                 WHERE schemaname = %s AND tablename = %s AND indexname = %s) AS gin_index,
                (SELECT indexdef FROM pg_indexes
                 WHERE schemaname = %s AND tablename = %s AND indexname = %s) AS scope_index,
                (SELECT extversion FROM pg_extension WHERE extname = 'vector') AS vector_version,
                (SELECT EXISTS(SELECT 1 FROM {migration_table} WHERE version = %s)) AS migration_version,
                (SELECT array_agg(a.attname ORDER BY a.attname)
                 FROM pg_attribute AS a
                 WHERE a.attrelid = to_regclass(%s) AND a.attnum > 0 AND NOT a.attisdropped) AS candidate_columns,
                (SELECT array_agg(a.attname ORDER BY a.attname)
                 FROM pg_attribute AS a
                 WHERE a.attrelid = to_regclass(%s) AND a.attnum > 0 AND NOT a.attisdropped) AS state_columns
            """,
            (
                table_regclass,
                table_regclass,
                self.config.schema,
                self.config.table,
                _derived_identifier(self.config, "hnsw"),
                self.config.schema,
                self.config.table,
                _derived_identifier(self.config, "gin"),
                self.config.schema,
                self.config.table,
                _derived_identifier(self.config, "scope"),
                DDL_VERSION,
                table_regclass,
                state_regclass,
            ),
        )
        raw = cursor.fetchone()
        row = _row_mapping(raw, getattr(cursor, "description", None)) if raw is not None else {}
        required_candidate = {
            "storage_key", "record_id", "tenant_id", "agent_id", "workspace_id", "user_id",
            "source_id", "kind", "status", "embedding", "title_text", "alias_text", "keyword_text",
            "search_tsv", "projection_digest", "projection_digest_schema", "authoritative_updated_at",
            "index_watermark", "indexed_at",
        }
        required_state = {
            "singleton", "ready", "in_progress", "run_id", "cursor_updated_at", "cursor_storage_key",
            "committed_watermark", "authoritative_updated_at", "authoritative_storage_key",
            "completed_at", "embedding_fingerprint", "projection_digest_schema", "projection_fingerprint",
            "lease_owner", "lease_expires_at", "updated_at",
            "authority_revision",
            "staging_embedding_fingerprint", "staging_projection_digest_schema",
            "staging_projection_fingerprint", "staging_authority_revision",
        }
        normalized_pk = _normalized_definition(row.get("primary_key"))
        normalized_hnsw = _normalized_definition(row.get("hnsw_index"))
        normalized_gin = _normalized_definition(row.get("gin_index"))
        normalized_scope = _normalized_definition(row.get("scope_index"))
        if (
            str(row.get("embedding_type") or "").lower() != f"vector({self.config.vector_dimension})"
            or normalized_pk != "primarykey(storage_key,index_watermark)"
            or "usinghnsw" not in normalized_hnsw
            or "embeddingvector_cosine_ops" not in normalized_hnsw
            or "usinggin" not in normalized_gin
            or "(search_tsv)" not in normalized_gin
            or "(tenant_id,agent_id,workspace_id,user_id,source_id,status,kind)" not in normalized_scope
            or not _version_at_least(str(row.get("vector_version") or ""), (0, 8, 0))
            or row.get("migration_version") is not True
            or not required_candidate.issubset({str(item) for item in (row.get("candidate_columns") or ())})
            or not required_state.issubset({str(item) for item in (row.get("state_columns") or ())})
        ):
            raise RuntimeError("postgres_projection_schema_mismatch")

    def release_sync_lease(self, *, run_id: str, lease_owner: str) -> None:
        connection = self._connect()
        try:
            with connection.cursor() as cursor:
                self._set_timeout(cursor)
                cursor.execute(
                    f"UPDATE {self.qualified_state_table} SET lease_owner = '', "
                    "lease_expires_at = NULL, updated_at = CURRENT_TIMESTAMP "
                    "WHERE singleton = %s AND run_id = %s AND in_progress = TRUE AND lease_owner = %s",
                    (True, str(run_id or "")[:256], str(lease_owner or "")[:128]),
                )
            connection.commit()
        except Exception:
            connection.rollback()
            raise RuntimeError("postgres_sync_lease_release_failed") from None
        finally:
            connection.close()

    def renew_sync_lease(self, *, run_id: str, lease_owner: str) -> None:
        connection = self._connect()
        try:
            with connection.cursor() as cursor:
                self._set_timeout(cursor)
                cursor.execute(
                    f"UPDATE {self.qualified_state_table} SET "
                    "lease_expires_at = CURRENT_TIMESTAMP + (%s * INTERVAL '1 second'), "
                    "updated_at = CURRENT_TIMESTAMP "
                    "WHERE singleton = %s AND run_id = %s AND in_progress = TRUE AND lease_owner = %s",
                    (
                        self.config.sync_lease_seconds,
                        True,
                        str(run_id or "")[:256],
                        str(lease_owner or "")[:128],
                    ),
                )
                if int(getattr(cursor, "rowcount", -1)) == 0:
                    raise RuntimeError("postgres_sync_lease_lost")
            connection.commit()
        except Exception as exc:
            connection.rollback()
            if str(exc) == "postgres_sync_lease_lost":
                raise RuntimeError("postgres_sync_lease_lost") from None
            raise RuntimeError("postgres_sync_lease_renew_failed") from None
        finally:
            connection.close()

    def invalidate_index(self, *, watermark: str, reason: str) -> None:
        connection = self._connect()
        try:
            with connection.cursor() as cursor:
                self._set_timeout(cursor)
                cursor.execute(
                    f"UPDATE {self.qualified_state_table} SET ready = FALSE, in_progress = FALSE, "
                    "lease_owner = '', lease_expires_at = NULL, updated_at = CURRENT_TIMESTAMP "
                    "WHERE singleton = %s AND committed_watermark = %s",
                    (True, str(watermark or "")[:256]),
                )
            connection.commit()
        except Exception:
            connection.rollback()
            raise RuntimeError("postgres_index_invalidate_failed") from None
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
        embedding_fingerprint: str,
        projection_digest_schema: str,
        projection_fingerprint: str,
        lease_owner: str,
        authority_revision: str,
    ) -> None:
        normalized_run_id = str(run_id or "").strip()[:256]
        if not normalized_run_id or len(projections) > 256:
            raise RuntimeError("postgres_sync_apply_failed")
        connection = self._connect()
        try:
            with connection.cursor() as cursor:
                self._set_timeout(cursor)
                cursor.execute(
                    f"SELECT run_id, in_progress, cursor_updated_at, cursor_storage_key, "
                    "embedding_fingerprint, projection_digest_schema "
                    ", projection_fingerprint, lease_owner, "
                    "(lease_expires_at > CURRENT_TIMESTAMP) AS lease_active "
                    ", authority_revision, staging_embedding_fingerprint, "
                    "staging_projection_digest_schema, staging_projection_fingerprint, "
                    "staging_authority_revision "
                    f"FROM {self.qualified_state_table} "
                    "WHERE singleton = %s FOR UPDATE",
                    (True,),
                )
                raw_state = cursor.fetchone()
                state = _row_mapping(raw_state, getattr(cursor, "description", None)) if raw_state is not None else {}
                if str(state.get("run_id") or "") != normalized_run_id or not bool(state.get("in_progress")):
                    raise RuntimeError("postgres_sync_conflict")
                if (
                    str(state.get("staging_embedding_fingerprint") or "") != embedding_fingerprint
                    or str(state.get("staging_projection_digest_schema") or "") != projection_digest_schema
                    or str(state.get("staging_projection_fingerprint") or "") != projection_fingerprint
                    or str(state.get("lease_owner") or "") != lease_owner
                    or not bool(state.get("lease_active"))
                    or str(state.get("staging_authority_revision") or "") != authority_revision
                ):
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
                    "search_tsv, projection_digest, projection_digest_schema, authoritative_updated_at, "
                    "index_watermark, indexed_at"
                    ") VALUES ("
                    "%s, %s, %s, %s, %s, %s, %s, %s, %s, CAST(%s AS vector), %s, %s, %s, "
                    "to_tsvector('simple', %s), %s, %s, %s, %s, CURRENT_TIMESTAMP"
                    ") ON CONFLICT (storage_key, index_watermark) DO UPDATE SET "
                    "record_id=EXCLUDED.record_id, tenant_id=EXCLUDED.tenant_id, agent_id=EXCLUDED.agent_id, "
                    "workspace_id=EXCLUDED.workspace_id, user_id=EXCLUDED.user_id, source_id=EXCLUDED.source_id, "
                    "kind=EXCLUDED.kind, status=EXCLUDED.status, embedding=EXCLUDED.embedding, "
                    "title_text=EXCLUDED.title_text, alias_text=EXCLUDED.alias_text, "
                    "keyword_text=EXCLUDED.keyword_text, search_tsv=EXCLUDED.search_tsv, "
                    "projection_digest=EXCLUDED.projection_digest, "
                    "projection_digest_schema=EXCLUDED.projection_digest_schema, "
                    "authoritative_updated_at=EXCLUDED.authoritative_updated_at, "
                    "index_watermark=EXCLUDED.index_watermark, indexed_at=CURRENT_TIMESTAMP"
                )
                values = []
                for projection in projections:
                    title = str(projection.get("title_text") or "")[: self.config.projection_text_chars]
                    aliases = str(projection.get("alias_text") or "")[: self.config.projection_text_chars]
                    keywords = str(projection.get("keyword_text") or "")[: self.config.projection_text_chars]
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
                            str(projection.get("projection_digest") or "")[:64],
                            str(projection.get("projection_digest_schema") or "")[:64],
                            _canonical_timestamp(projection.get("updated_at")),
                            normalized_run_id,
                        )
                    )
                if values:
                    cursor.executemany(upsert_sql, values)
                cursor.execute(
                    f"UPDATE {self.qualified_state_table} SET cursor_updated_at = %s, "
                    "cursor_storage_key = %s, lease_expires_at = CURRENT_TIMESTAMP + (%s * INTERVAL '1 second'), "
                    "updated_at = CURRENT_TIMESTAMP "
                    "WHERE singleton = %s AND run_id = %s AND in_progress = TRUE AND lease_owner = %s",
                    (
                        str(next_cursor.updated_at or "")[:64],
                        str(next_cursor.storage_key or "")[:512],
                        self.config.sync_lease_seconds,
                        True,
                        normalized_run_id,
                        lease_owner,
                    ),
                )
            connection.commit()
        except Exception:
            connection.rollback()
            raise RuntimeError("postgres_sync_apply_failed") from None
        finally:
            connection.close()

    def finalize_sync(
        self,
        *,
        run_id: str,
        expected_cursor: Any,
        embedding_fingerprint: str,
        projection_digest_schema: str,
        projection_fingerprint: str,
        lease_owner: str,
        authority_revision: str,
    ) -> None:
        normalized_run_id = str(run_id or "")[:256]
        connection = self._connect()
        try:
            with connection.cursor() as cursor:
                self._set_timeout(cursor)
                cursor.execute(
                    f"SELECT run_id, in_progress, cursor_updated_at, cursor_storage_key, "
                    "staging_embedding_fingerprint, staging_projection_digest_schema, "
                    "staging_projection_fingerprint, staging_authority_revision, lease_owner, "
                    "(lease_expires_at > CURRENT_TIMESTAMP) AS lease_active "
                    f"FROM {self.qualified_state_table} WHERE singleton = %s FOR UPDATE",
                    (True,),
                )
                raw = cursor.fetchone()
                state = _row_mapping(raw, getattr(cursor, "description", None)) if raw is not None else {}
                if (
                    str(state.get("run_id") or "") != normalized_run_id
                    or not bool(state.get("in_progress"))
                    or str(state.get("cursor_updated_at") or "") != str(expected_cursor.updated_at or "")
                    or str(state.get("cursor_storage_key") or "") != str(expected_cursor.storage_key or "")
                    or str(state.get("staging_embedding_fingerprint") or "") != embedding_fingerprint
                    or str(state.get("staging_projection_digest_schema") or "") != projection_digest_schema
                    or str(state.get("staging_projection_fingerprint") or "") != projection_fingerprint
                    or str(state.get("staging_authority_revision") or "") != authority_revision
                    or str(state.get("lease_owner") or "") != lease_owner
                    or not bool(state.get("lease_active"))
                ):
                    raise RuntimeError("postgres_sync_conflict")
                cursor.execute(
                    f"DELETE FROM {self.qualified_table} WHERE index_watermark <> %s",
                    (normalized_run_id,),
                )
                cursor.execute(
                    f"UPDATE {self.qualified_state_table} SET "
                    "ready = TRUE, in_progress = FALSE, committed_watermark = %s, "
                    "authoritative_updated_at = NULLIF(%s, '')::timestamptz, "
                    "authoritative_storage_key = %s, embedding_fingerprint = %s, "
                    "projection_digest_schema = %s, projection_fingerprint = %s, authority_revision = %s, "
                    "completed_at = CURRENT_TIMESTAMP, lease_owner = '', lease_expires_at = NULL, "
                    "staging_embedding_fingerprint = '', staging_projection_digest_schema = '', "
                    "staging_projection_fingerprint = '', staging_authority_revision = '', "
                    "updated_at = CURRENT_TIMESTAMP WHERE singleton = %s AND run_id = %s",
                    (
                        normalized_run_id,
                        str(expected_cursor.updated_at or "")[:64],
                        str(expected_cursor.storage_key or "")[:512],
                        embedding_fingerprint,
                        projection_digest_schema,
                        projection_fingerprint,
                        authority_revision,
                        True,
                        normalized_run_id,
                    ),
                )
            connection.commit()
        except Exception:
            connection.rollback()
            raise RuntimeError("postgres_sync_finalize_failed") from None
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
            params.append(list(request.source_ids))
        if request.kinds:
            where.append("kind = ANY(%s)")
            params.append(list(request.kinds))
        where.append("index_watermark = %s")
        params.append(str(watermark or "")[:256])
        vector_literal = _vector_literal(vector, expected_dimension=self.config.vector_dimension)
        sql = (
            "SELECT left(record_id, 256) AS record_id, left(tenant_id, 256) AS tenant_id, "
            "left(agent_id, 256) AS agent_id, left(workspace_id, 512) AS workspace_id, "
            "left(user_id, 256) AS user_id, left(source_id, 256) AS source_id, "
            "left(kind, 128) AS kind, left(status, 64) AS status, "
            "left(projection_digest, 64) AS projection_digest, "
            "left(projection_digest_schema, 64) AS projection_digest_schema, "
            "left(authoritative_updated_at::text, 64) AS authoritative_updated_at, "
            "left(index_watermark, 256) AS index_watermark, "
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
                cursor.execute("SELECT set_config('hnsw.iterative_scan', %s, true)", ("strict_order",))
                cursor.execute("SELECT set_config('hnsw.max_scan_tuples', %s, true)", ("20000",))
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
        startup_error: str = "",
        configuration_fingerprint: str = "",
        clock: Callable[[], float] = monotonic,
    ) -> None:
        self.sqlite_source = sqlite_source
        self.config = config
        self.repository = repository or PostgresCandidateRepository(config)
        self.embedding_provider = embedding_provider or config.embedding_provider
        self._startup_error = _safe_candidate_error(startup_error)
        self._configuration_fingerprint = (
            str(configuration_fingerprint or "").lower()
            if re.fullmatch(r"[0-9a-f]{64}", str(configuration_fingerprint or "").lower())
            else ""
        )
        self._clock = clock
        self._circuit = _Circuit(
            threshold=config.failure_threshold,
            cooldown_seconds=config.cooldown_seconds,
            clock=clock,
        )
        self._embedding_gate = _ConnectionGate(
            pool_size=config.pool_size,
            queue_bound=config.queue_bound,
        )
        self._last_error = self._startup_error
        self._last_state = IndexState()
        self._last_query_valid = False
        self._cache: OrderedDict[tuple[Any, ...], tuple[float, tuple[dict[str, Any], ...]]] = OrderedDict()
        self._cache_lock = Lock()

    def search(self, request: CandidateRequest) -> CandidateBatch:
        sqlite_batch = self.sqlite_source.search(request)
        if self._startup_error:
            return self._batch(
                sqlite_batch,
                request=request,
                state="bypassed",
                error_code=self._startup_error,
            )
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
            expected_embedding_fingerprint = embedding_provider_fingerprint(self.embedding_provider, self.config)
            if index_state.embedding_fingerprint != expected_embedding_fingerprint:
                raise RuntimeError("embedding_fingerprint_mismatch")
            if (
                index_state.projection_digest_schema != PROJECTION_DIGEST_SCHEMA
                or index_state.projection_fingerprint != projection_fingerprint(self.config)
            ):
                raise RuntimeError("projection_fingerprint_mismatch")
            authority_cursor = self._authority_head()
            authority_revision = self._authority_revision()
            authority_lag = candidate_index_lag_seconds(
                index_state,
                authority_cursor=authority_cursor,
                authority_revision=authority_revision,
            )
            self._last_state = replace(index_state, lag_seconds=authority_lag)
            if authority_lag > self.config.max_index_lag_seconds:
                raise RuntimeError(
                    "authority_revision_changed"
                    if authority_revision is not None and authority_revision != index_state.authority_revision
                    else "index_lag_exceeded"
                )
            cache_key = self._cache_key(
                request,
                watermark=index_state.watermark,
                authority_cursor=authority_cursor,
                authority_revision=authority_revision,
            )
            cached = self._cache_get(cache_key)
            if cached is None:
                if not self._embedding_gate.acquire(self.config.embedding_queue_timeout_seconds):
                    raise RuntimeError("embedding_timeout")
                try:
                    vectors = self.embedding_provider.embed(
                        [request.query],
                    )
                finally:
                    self._embedding_gate.release()
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
            stable_state = self.repository.read_index_state()
            if (
                not stable_state.ready
                or stable_state.watermark != index_state.watermark
                or stable_state.embedding_fingerprint != expected_embedding_fingerprint
                or stable_state.projection_digest_schema != PROJECTION_DIGEST_SCHEMA
                or stable_state.projection_fingerprint != projection_fingerprint(self.config)
                or stable_state.authority_revision != index_state.authority_revision
                or stable_state.authoritative_updated_at != index_state.authoritative_updated_at
                or stable_state.authoritative_storage_key != index_state.authoritative_storage_key
                or self._authority_head() != authority_cursor
                or self._authority_revision() != authority_revision
            ):
                raise RuntimeError("index_watermark_changed")
            self._last_state = replace(
                stable_state,
                lag_seconds=(
                    candidate_index_lag_seconds(
                        stable_state,
                        authority_cursor=authority_cursor,
                        authority_revision=authority_revision,
                    )
                    if authority_cursor is not None
                    else stable_state.lag_seconds
                ),
            )
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
                valid_empty=not cached,
            )
        except Exception as exc:
            code = _error_code(exc, prefix="postgres")
            self._last_error = code
            self._last_query_valid = False
            self._circuit.failure()
            return self._batch(sqlite_batch, request=request, state="bypassed", error_code=code)

    def health(self) -> dict[str, object]:
        provider_health = sanitized_embedding_health(self.embedding_provider)
        provider_available = provider_health.get("available") is True
        return {
            "enabled": self.config.enabled,
            "configured": (
                self.config.configured
                and self.embedding_provider is not None
                and provider_health.get("configured") is True
            ),
            "available": self._last_query_valid and self._circuit.state() != "open" and provider_available,
            "circuit": self._circuit.state(),
            "lag_seconds": _public_lag_seconds(self._last_state.lag_seconds),
            "watermark": self._last_state.watermark,
            "last_error": self._last_error,
            "cache_entries": len(self._cache),
            "embedding": provider_health,
        }

    def refresh_index_identity(self) -> bool:
        """Refresh only bounded committed-state metadata; failures remain bypassable."""
        if self._startup_error:
            self._last_error = self._startup_error
            return False
        if not self.config.enabled:
            self._last_error = "disabled"
            return False
        if not self.config.configured or self.embedding_provider is None:
            self._last_error = "not_configured"
            return False
        try:
            state = self.repository.read_index_state()
            self._last_state = state
            if not state.ready or not state.watermark:
                self._last_error = "index_not_ready"
                return False
            expected = embedding_provider_fingerprint(self.embedding_provider, self.config)
            if state.embedding_fingerprint != expected:
                self._last_error = "embedding_fingerprint_mismatch"
                return False
            if (
                state.projection_digest_schema != PROJECTION_DIGEST_SCHEMA
                or state.projection_fingerprint != projection_fingerprint(self.config)
            ):
                self._last_error = "projection_fingerprint_mismatch"
                return False
            authority_revision = self._authority_revision()
            if authority_revision is not None and state.authority_revision != authority_revision:
                self._last_error = "authority_revision_changed"
                return False
            self._last_error = ""
            return True
        except Exception as exc:
            self._last_error = _error_code(exc, prefix="postgres")
            self._last_query_valid = False
            return False

    def effective_identity(self) -> dict[str, object]:
        self.refresh_index_identity()
        sqlite_identity_fn = getattr(self.sqlite_source, "effective_identity", None)
        sqlite_identity = sqlite_identity_fn() if callable(sqlite_identity_fn) else {}
        authority_revision = str(
            sqlite_identity.get("authority_revision", "")
            if isinstance(sqlite_identity, Mapping)
            else ""
        )
        provider_identity = embedding_provider_identity(self.embedding_provider, self.config)
        if not self.config.enabled:
            state = "disabled"
        elif not self.config.configured or self.embedding_provider is None:
            state = "not_configured"
        elif self._last_query_valid and not self._last_error:
            state = "available"
        else:
            state = "bypassed"
        return {
            "candidate_source_type": type(self).__name__,
            "name": self.name,
            "policy_version": self.policy_version,
            "sqlite_authority": True,
            "authority_revision": authority_revision,
            "enabled": self.config.enabled,
            "configured": self.config.configured and self.embedding_provider is not None,
            "config_fingerprint": self._configuration_fingerprint or postgres_config_fingerprint(self.config),
            "projection_fingerprint": projection_fingerprint(self.config),
            "embedding": provider_identity,
            "postgres": {
                "state": state,
                "committed_watermark": self._last_state.watermark,
                "index_revision": self._last_state.authority_revision,
                "circuit": self._circuit.state(),
                "bypass_reason": self._last_error or ("index_not_verified" if state == "bypassed" else ""),
            },
        }
        payload["identity_digest"] = sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        return payload

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
            if str(row.get("projection_digest_schema") or "") != PROJECTION_DIGEST_SCHEMA:
                drops["digest_schema_mismatch"] += 1
                continue
            if not re.fullmatch(r"[0-9a-f]{64}", str(row.get("projection_digest") or "")):
                drops["digest_invalid"] += 1
                continue
            score = _bounded_score(row.get("vector_score"))
            try:
                ref = CandidateRef(record_id, request.scope, source_id)
            except (TypeError, ValueError):
                drops["invalid_ref"] += 1
                continue
            try:
                authoritative_updated_at = _canonical_timestamp(row.get("authoritative_updated_at"))
            except RuntimeError:
                drops["timestamp_invalid"] += 1
                continue
            hits.append(
                CandidateHit(
                    ref=ref,
                    source_rank=len(hits) + 1,
                    source_score=score,
                    component_hints={
                        "vector_score": score,
                        "_candidate_projection_digest": str(row.get("projection_digest") or ""),
                        "_candidate_projection_digest_schema": str(row.get("projection_digest_schema") or "")[:64],
                        "_candidate_projection_text_chars": self.config.projection_text_chars,
                        "_candidate_authoritative_updated_at": authoritative_updated_at,
                    },
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
                "fallback": state == "bypassed",
                "fallback_reason": "candidate_source_fallback" if state == "bypassed" else "",
                "postgres": {
                    "state": state,
                    "fallback": state == "bypassed",
                    "error_code": error_code,
                    "candidate_count": pg_count,
                    "valid_empty": valid_empty,
                    "top_k": min(self.config.top_k_max, max(request.limit, 1)),
                    "drops": dict(drops or {}),
                    "watermark": self._last_state.watermark,
                    "lag_seconds": _public_lag_seconds(self._last_state.lag_seconds),
                },
            },
        )

    def _cache_key(
        self,
        request: CandidateRequest,
        *,
        watermark: str,
        authority_cursor: tuple[str, str] | None,
        authority_revision: str | None,
    ) -> tuple[Any, ...]:
        context = request.task_context_dict()
        return (
            sha256(request.query.encode("utf-8", errors="replace")).hexdigest(),
            request.scope,
            request.source_ids,
            request.kinds,
            request.limit,
            request.budget,
            self.config.top_k_max,
            tuple(request.recall_filters),
            str(context.get("retrieval_policy_digest") or self.policy_version),
            str(context.get("release_commit") or self.config.release_id),
            watermark,
            authority_cursor,
            authority_revision,
        )

    def _authority_head(self) -> tuple[str, str] | None:
        head_fn = getattr(self.sqlite_source, "authority_head", None)
        if not callable(head_fn):
            return None
        raw = head_fn()
        if not isinstance(raw, tuple) or len(raw) != 2:
            raise RuntimeError("authority_cursor_unavailable")
        return (_canonical_timestamp(raw[0]), str(raw[1] or "")[:512])

    def _authority_revision(self) -> str | None:
        revision_fn = getattr(self.sqlite_source, "authority_revision", None)
        if not callable(revision_fn):
            return None
        value = str(revision_fn() or "")[:64]
        if not value.isdigit():
            raise RuntimeError("authority_revision_unavailable")
        return value

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


def candidate_index_lag_seconds(
    state: IndexState,
    *,
    authority_cursor: tuple[str, str] | None,
    authority_revision: str | None,
) -> float:
    if authority_revision is not None and authority_revision != state.authority_revision:
        return float("inf")
    if authority_cursor is None:
        return float(state.lag_seconds or 0.0)
    try:
        if authority_cursor == (state.authoritative_updated_at, state.authoritative_storage_key):
            return 0.0
        head = datetime.fromisoformat(authority_cursor[0].replace("Z", "+00:00"))
        committed = datetime.fromisoformat(state.authoritative_updated_at.replace("Z", "+00:00"))
        if head.tzinfo is None:
            head = head.replace(tzinfo=timezone.utc)
        if committed.tzinfo is None:
            committed = committed.replace(tzinfo=timezone.utc)
        delta = (head - committed).total_seconds()
        if delta <= 0:
            return float("inf")
        return delta
    except (TypeError, ValueError, OverflowError):
        raise RuntimeError("authority_cursor_unavailable") from None


def build_postgres_vector_candidate_source(
    store: Any,
    config: PostgresVectorConfig,
    *,
    repository: Any | None = None,
    embedding_provider: EmbeddingProvider | None = None,
    startup_error: str = "",
    configuration_fingerprint: str = "",
) -> PostgresVectorCandidateSource:
    """Build the supported SQLite-authority/Postgres-candidate composition."""

    from .sqlite_source import SQLiteCandidateSource

    return PostgresVectorCandidateSource(
        sqlite_source=SQLiteCandidateSource(store),
        config=config,
        repository=repository,
        embedding_provider=embedding_provider,
        startup_error=startup_error,
        configuration_fingerprint=configuration_fingerprint,
    )


def candidate_projection_digest(
    projection: Mapping[str, Any], *, max_text_chars: int
) -> str:
    bounded_chars = _bounded_int(max_text_chars, 1, 64_000)
    payload = {
        "schema": PROJECTION_DIGEST_SCHEMA,
        "max_text_chars": bounded_chars,
        **{
            key: str(projection.get(key) or default)
            for key, default in (
                ("storage_key", ""), ("record_id", ""), ("kind", ""), ("status", ""),
                ("tenant_id", "default"), ("agent_id", ""), ("workspace_id", ""),
                ("user_id", ""), ("source_id", "default"), ("updated_at", ""),
            )
        },
        "title_text": str(projection.get("title_text") or "")[:bounded_chars],
        "alias_text": str(projection.get("alias_text") or "")[:bounded_chars],
        "keyword_text": str(projection.get("keyword_text") or "")[:bounded_chars],
    }
    payload["updated_at"] = _canonical_timestamp(projection.get("updated_at"))
    return sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def candidate_record_projection_digest(record: Any, *, max_text_chars: int) -> str:
    bounded_chars = _bounded_int(max_text_chars, 1, 64_000)
    content = record.content if isinstance(record.content, Mapping) else {}
    raw_parts: list[str] = []
    if str(record.kind or "") == "raw_chunk":
        raw_parts = [
            str(record.record_id or ""),
            *(str(content.get(key) or "") for key in (
                "source_event_id", "session_id", "turn_id", "chunk_id", "raw_text_hash"
            )),
        ]
    content_text = "\n".join(
        part for part in (
            str(record.title or ""), str(record.summary or ""), str(record.detail or ""),
            str(content.get("text") or ""), str(content.get("excerpt") or ""), *raw_parts,
        ) if part
    )[:bounded_chars]
    keyword_text = "\n".join(
        part for part in (
            str(record.title or "")[:bounded_chars],
            str(record.summary or "")[:bounded_chars],
            str(record.detail or "")[:bounded_chars],
            content_text,
        ) if part
    )[:bounded_chars]
    scope = record.scope
    storage_key = "\x1f".join((
        str(getattr(scope, "tenant_id", "default") or "default"),
        str(getattr(scope, "agent_id", "") or ""),
        str(getattr(scope, "workspace_id", "") or ""),
        str(getattr(scope, "user_id", "") or ""),
        str(record.record_id or ""),
    ))
    return candidate_projection_digest(
        {
            "storage_key": storage_key,
            "record_id": record.record_id,
            "kind": record.kind,
            "status": record.status,
            "tenant_id": getattr(scope, "tenant_id", "default"),
            "agent_id": getattr(scope, "agent_id", ""),
            "workspace_id": getattr(scope, "workspace_id", ""),
            "user_id": getattr(scope, "user_id", ""),
            "source_id": record.source_id,
            "updated_at": record.time.updated_at,
            "title_text": str(record.title or "")[:bounded_chars],
            "alias_text": " ".join(str(item) for item in list(record.aliases or ())[:128])[:bounded_chars],
            "keyword_text": keyword_text,
        },
        max_text_chars=bounded_chars,
    )


def embedding_provider_fingerprint(provider: EmbeddingProvider, config: PostgresVectorConfig) -> str:
    if config.embedding_fingerprint:
        return config.embedding_fingerprint
    fingerprint_fn = getattr(provider, "fingerprint", None)
    value = str(fingerprint_fn() if callable(fingerprint_fn) else "").strip().lower()
    if not re.fullmatch(r"[0-9a-f]{64}", value):
        raise RuntimeError("embedding_fingerprint_unavailable")
    return value


def projection_fingerprint(config: PostgresVectorConfig) -> str:
    return sha256(
        json.dumps(
            {
                "schema": PROJECTION_DIGEST_SCHEMA,
                "max_text_chars": config.projection_text_chars,
                "vector_dimension": config.vector_dimension,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def postgres_config_fingerprint(config: PostgresVectorConfig) -> str:
    """Fingerprint effective non-secret retrieval semantics and the DSN target."""
    dsn_target = ""
    if config.dsn:
        try:
            parsed = urlsplit(config.dsn)
            dsn_target = json.dumps(
                {
                    "scheme": parsed.scheme.lower(),
                    "host": (parsed.hostname or "").lower(),
                    "port": parsed.port,
                    "database": parsed.path,
                },
                sort_keys=True,
                separators=(",", ":"),
            )
        except (TypeError, ValueError):
            dsn_target = "invalid"
    payload = {
        "enabled": config.enabled,
        "configured": config.configured,
        "dsn_target_fingerprint": sha256(dsn_target.encode("utf-8")).hexdigest() if dsn_target else "",
        "connect_timeout_seconds": config.connect_timeout_seconds,
        "statement_timeout_ms": config.statement_timeout_ms,
        "pool_size": config.pool_size,
        "queue_bound": config.queue_bound,
        "vector_dimension": config.vector_dimension,
        "schema": config.schema,
        "table": config.table,
        "max_index_lag_seconds": config.max_index_lag_seconds,
        "failure_threshold": config.failure_threshold,
        "cooldown_seconds": config.cooldown_seconds,
        "top_k_max": config.top_k_max,
        "cache_entries": config.cache_entries,
        "cache_ttl_seconds": config.cache_ttl_seconds,
        "projection_fingerprint": projection_fingerprint(config),
    }
    return sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def embedding_provider_identity(
    provider: EmbeddingProvider | None,
    config: PostgresVectorConfig,
) -> dict[str, object]:
    if provider is None:
        return {"provider_type": "", "model": "", "fingerprint": ""}
    identity_fn = getattr(provider, "effective_identity", None)
    try:
        raw = identity_fn() if callable(identity_fn) else {}
    except Exception:
        raw = {}
    if not isinstance(raw, Mapping):
        raw = {}
    provider_type = str(raw.get("provider_type") or type(provider).__name__)[:64]
    model = str(raw.get("model") or "")[:256] if isinstance(provider, OpenAICompatibleEmbeddingProvider) else ""
    try:
        fingerprint = embedding_provider_fingerprint(provider, config)
    except Exception:
        fingerprint = ""
    return {
        "provider_type": provider_type if re.fullmatch(r"[A-Za-z0-9_.:/-]{1,64}", provider_type) else "",
        "model": model if re.fullmatch(r"[A-Za-z0-9_.:/-]{0,256}", model) else "",
        "fingerprint": fingerprint,
    }


def sanitized_embedding_health(provider: EmbeddingProvider | None) -> dict[str, object]:
    if provider is None:
        return {"configured": False, "available": False, "circuit": "open", "dimension": 0, "last_error": ""}
    try:
        raw = provider.health()
    except Exception:
        raw = {}
    if not isinstance(raw, Mapping):
        raw = {}
    circuit = str(raw.get("circuit") or "open")
    if circuit not in {"closed", "open", "half_open"}:
        circuit = "open"
    last_error = str(raw.get("last_error") or "")
    if last_error not in _EMBEDDING_HEALTH_ERROR_CODES:
        last_error = "embedding_unavailable"
    dimension = _bounded_int(raw.get("dimension"), 0, _MAX_VECTOR_DIMENSION)
    return {
        "configured": raw.get("configured") is True,
        "available": raw.get("available") is True and circuit != "open",
        "circuit": circuit,
        "dimension": dimension,
        "last_error": last_error,
    }


def _validate_embedding_base_url(value: str) -> None:
    if not value:
        return
    parsed = urlsplit(value)
    if parsed.username or parsed.password or parsed.fragment or parsed.query or not parsed.hostname:
        raise ValueError("invalid embedding base URL")
    if parsed.scheme == "https":
        return
    if parsed.scheme == "http":
        host = parsed.hostname.lower()
        if host == "localhost":
            return
        try:
            if ipaddress.ip_address(host).is_loopback:
                return
        except ValueError:
            pass
    raise ValueError("embedding base URL must use HTTPS or loopback HTTP")


def _canonical_timestamp(value: object) -> str:
    if value in (None, ""):
        return ""
    try:
        parsed = value if isinstance(value, datetime) else datetime.fromisoformat(
            str(value).strip().replace("Z", "+00:00")
        )
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc).isoformat(timespec="microseconds").replace(
            "+00:00", "Z"
        )
    except (TypeError, ValueError, OverflowError):
        raise RuntimeError("timestamp_invalid") from None


def _merge_hits(
    sqlite_hits: Sequence[CandidateHit],
    postgres_hits: Sequence[CandidateHit],
    *,
    limit: int,
) -> tuple[CandidateHit, ...]:
    bounded_limit = max(0, int(limit))
    sqlite_ordered = list(sqlite_hits)
    sqlite_positions = {
        (hit.ref.record_id, hit.ref.scope, hit.ref.source_id): index
        for index, hit in enumerate(sqlite_ordered)
    }
    postgres_only: list[CandidateHit] = []
    postgres_positions: dict[tuple[str, Any, str], int] = {}
    for hit in postgres_hits:
        key = (hit.ref.record_id, hit.ref.scope, hit.ref.source_id)
        existing_index = sqlite_positions.get(key)
        if existing_index is not None:
            combined = _combine_candidate_hits(sqlite_ordered[existing_index], hit)
            hints = combined.component_dict()
            hints["_candidate_sqlite_authority_duplicate"] = True
            sqlite_ordered[existing_index] = CandidateHit(
                ref=combined.ref,
                source_rank=combined.source_rank,
                source_score=combined.source_score,
                component_hints=hints,
                evidence_hints=combined.evidence_hints,
            )
            continue
        postgres_index = postgres_positions.get(key)
        if postgres_index is None:
            postgres_positions[key] = len(postgres_only)
            postgres_only.append(hit)
            continue
        postgres_only[postgres_index] = _combine_candidate_hits(postgres_only[postgres_index], hit)
    if bounded_limit <= 0:
        return ()
    sqlite_identity = [
        hit
        for hit in sqlite_ordered
        if set(hit.evidence_hints) & {"exact_title", "alias_hit"}
    ]
    identity_keys = {(hit.ref.record_id, hit.ref.scope, hit.ref.source_id) for hit in sqlite_identity}
    sqlite_remaining = [
        hit
        for hit in sqlite_ordered
        if (hit.ref.record_id, hit.ref.scope, hit.ref.source_id) not in identity_keys
    ]
    selected = sqlite_identity[:bounded_limit]
    remaining_slots = bounded_limit - len(selected)
    vector_quota = min(len(postgres_only), max(1, bounded_limit // 4), remaining_slots)
    selected.extend(postgres_only[:vector_quota])
    selected.extend(sqlite_remaining[: bounded_limit - len(selected)])
    selected_keys = {(hit.ref.record_id, hit.ref.scope, hit.ref.source_id) for hit in selected}
    for hit in (*postgres_only[vector_quota:], *sqlite_ordered):
        key = (hit.ref.record_id, hit.ref.scope, hit.ref.source_id)
        if len(selected) >= bounded_limit:
            break
        if key in selected_keys:
            continue
        selected_keys.add(key)
        selected.append(hit)
    return tuple(
        CandidateHit(
            ref=hit.ref,
            source_rank=rank,
            source_score=hit.source_score,
            component_hints=hit.component_hints,
            evidence_hints=hit.evidence_hints,
        )
        for rank, hit in enumerate(selected, start=1)
    )


def _combine_candidate_hits(existing: CandidateHit, incoming: CandidateHit) -> CandidateHit:
    hints = existing.component_dict()
    for key, value in incoming.component_dict().items():
        if key == "vector_score":
            hints[key] = max(_bounded_score(hints.get(key)), _bounded_score(value))
        else:
            hints[key] = value
    return CandidateHit(
        ref=existing.ref,
        source_rank=min(existing.source_rank, incoming.source_rank),
        source_score=max(existing.source_score, incoming.source_score),
        component_hints=hints,
        evidence_hints=tuple(dict.fromkeys((*existing.evidence_hints, *incoming.evidence_hints))),
    )


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
    if text in {"redirect_rejected", "embedding_http_error"}:
        return f"{prefix}_transport_error"
    allowed = {
        "circuit_open",
        "embedding_not_configured",
        "embedding_batch_invalid",
        "embedding_dimension_mismatch",
        "embedding_timeout",
        "embedding_response_invalid",
        "index_lag_exceeded",
        "index_not_ready",
        "index_watermark_changed",
        "embedding_fingerprint_mismatch",
        "embedding_fingerprint_unavailable",
        "projection_fingerprint_mismatch",
        "authority_cursor_unavailable",
        "authority_revision_changed",
        "authority_revision_unavailable",
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


def _safe_candidate_error(value: object) -> str:
    text = str(value or "")
    return text if text in {"invalid_vector_index_config", "postgres_dependency_unavailable"} else ""


def _bounded_int(value: object, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        parsed = minimum
    return max(minimum, min(maximum, parsed))


def _vector_dimension(value: object) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        raise ValueError("vector_dimension must be an integer") from None
    if not 1 <= parsed <= _MAX_VECTOR_DIMENSION:
        raise ValueError(f"vector_dimension must be between 1 and {_MAX_VECTOR_DIMENSION}")
    return parsed


def _normalized_definition(value: object) -> str:
    return re.sub(r'[\s"]+', "", str(value or "").lower())


def _derived_identifier(config: PostgresVectorConfig, role: str) -> str:
    clean_role = re.sub(r"[^a-z0-9_]", "", str(role or ""))[:12] or "object"
    digest = sha256(f"{config.schema}.{config.table}:{clean_role}".encode("utf-8")).hexdigest()[:10]
    stem_limit = 63 - len(clean_role) - len(digest) - 2
    return f"{config.table[:stem_limit]}_{clean_role}_{digest}"


def _version_at_least(value: str, minimum: tuple[int, int, int]) -> bool:
    match = re.match(r"^(\d+)\.(\d+)(?:\.(\d+))?", value)
    if match is None:
        return False
    current = tuple(int(item or 0) for item in match.groups())
    return current >= minimum


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


def _public_lag_seconds(value: object) -> float | None:
    if value is None:
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return max(0.0, min(10_000_000.0, parsed)) if isfinite(parsed) else None


def _bounded_score(value: object) -> float:
    return _bounded_float(value, -1.0, 1.0)
