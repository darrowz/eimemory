from __future__ import annotations

from dataclasses import dataclass
from math import isfinite
from typing import Any, Mapping, Protocol
from uuid import uuid4

from eimemory.storage.runtime_store import RuntimeStore

from .postgres_vector import (
    PROJECTION_DIGEST_SCHEMA,
    EmbeddingProvider,
    PostgresVectorConfig,
    candidate_projection_digest,
    embedding_provider_fingerprint,
    projection_fingerprint,
)


@dataclass(frozen=True, slots=True)
class ProjectionCursor:
    updated_at: str = ""
    storage_key: str = ""


@dataclass(frozen=True, slots=True)
class SyncProgress:
    run_id: str
    cursor: ProjectionCursor
    resumed: bool
    embedding_fingerprint: str = ""
    projection_digest_schema: str = PROJECTION_DIGEST_SCHEMA
    projection_fingerprint: str = ""
    lease_owner: str = ""
    authority_revision: str = ""


class ProjectionReader(Protocol):
    def page(self, cursor: ProjectionCursor, *, limit: int) -> list[dict[str, Any]]: ...

    def snapshot_token(self) -> str: ...


class SyncRepository(Protocol):
    def begin_or_resume_sync(
        self, *, embedding_fingerprint: str, projection_digest_schema: str,
        projection_fingerprint: str, lease_owner: str,
        authority_revision: str,
    ) -> SyncProgress: ...

    def apply_sync_page(
        self,
        *,
        run_id: str,
        projections: list[dict[str, Any]],
        expected_cursor: ProjectionCursor,
        next_cursor: ProjectionCursor,
        complete: bool,
        embedding_fingerprint: str,
        projection_digest_schema: str,
        projection_fingerprint: str,
        lease_owner: str,
        authority_revision: str,
    ) -> None: ...

    def release_sync_lease(self, *, run_id: str, lease_owner: str) -> None: ...

    def renew_sync_lease(self, *, run_id: str, lease_owner: str) -> None: ...

    def invalidate_index(self, *, watermark: str, reason: str) -> None: ...

    def finalize_sync(
        self, *, run_id: str, expected_cursor: ProjectionCursor,
        embedding_fingerprint: str, projection_digest_schema: str,
        projection_fingerprint: str, lease_owner: str, authority_revision: str,
    ) -> None: ...


class SQLiteProjectionReader:
    """Bounded metadata/text projection reader over the SQLite authority."""

    def __init__(self, store: RuntimeStore, *, max_text_chars: int = 16_000) -> None:
        self.store = store
        self.max_text_chars = max(1, min(64_000, int(max_text_chars)))
        self._index_ready = False

    def page(self, cursor: ProjectionCursor, *, limit: int) -> list[dict[str, Any]]:
        bounded_limit = max(1, min(1_000, int(limit)))
        with self.store._lock:  # preserve RuntimeStore's single-writer/read contract
            self._ensure_contract_locked()
            if cursor.updated_at:
                keyset_clause = "WHERE (r.updated_at, r.storage_key) > (?, ?)"
                keyset_params: tuple[object, ...] = (cursor.updated_at, cursor.storage_key)
            else:
                keyset_clause = ""
                keyset_params = ()
            rows = self.store.sqlite.conn.execute(
                f"""
                SELECT
                    r.storage_key, r.record_id, r.kind, r.status,
                    r.tenant_id, r.agent_id, r.workspace_id, r.user_id, r.source_id,
                    substr(r.title, 1, ?) AS title,
                    substr(r.summary, 1, ?) AS summary,
                    substr(r.detail, 1, ?) AS detail,
                    substr(r.content_text, 1, ?) AS bounded_content_text,
                    substr(COALESCE((
                        SELECT group_concat(bounded_alias, char(31))
                        FROM (
                            SELECT substr(a.normalized_alias, 1, ?) AS bounded_alias
                            FROM recall_alias_index AS a
                            WHERE a.storage_key = r.storage_key
                            ORDER BY a.alias_ordinal ASC, a.normalized_alias ASC
                            LIMIT 128
                        ) AS bounded_aliases
                    ), ''), 1, ?) AS alias_text,
                    r.updated_at
                FROM records AS r
                {keyset_clause}
                ORDER BY r.updated_at ASC, r.storage_key ASC
                LIMIT ?
                """,
                (
                    self.max_text_chars,
                    self.max_text_chars,
                    self.max_text_chars,
                    self.max_text_chars,
                    256,
                    self.max_text_chars,
                    *keyset_params,
                    bounded_limit,
                ),
            ).fetchall()
        projected: list[dict[str, Any]] = []
        for row in rows:
            aliases = [item for item in str(row["alias_text"] or "").split("\x1f") if item]
            keyword_text = "\n".join(
                part
                for part in (
                    str(row["title"] or ""),
                    str(row["summary"] or ""),
                    str(row["detail"] or ""),
                    str(row["bounded_content_text"] or ""),
                )
                if part
            )[: self.max_text_chars]
            projected.append(
                {
                    "storage_key": str(row["storage_key"] or ""),
                    "record_id": str(row["record_id"] or ""),
                    "kind": str(row["kind"] or ""),
                    "status": str(row["status"] or ""),
                    "tenant_id": str(row["tenant_id"] or "default"),
                    "agent_id": str(row["agent_id"] or ""),
                    "workspace_id": str(row["workspace_id"] or ""),
                    "user_id": str(row["user_id"] or ""),
                    "source_id": str(row["source_id"] or "default"),
                    "title": str(row["title"] or "")[: self.max_text_chars],
                    "aliases": aliases[:128],
                    "keyword_text": keyword_text,
                    "updated_at": str(row["updated_at"] or ""),
                }
            )
        return projected

    def snapshot_token(self) -> str:
        with self.store._lock:
            self._ensure_contract_locked()
            row = self.store.sqlite.conn.execute(
                "SELECT revision FROM vector_sync_revision WHERE singleton = 1"
            ).fetchone()
        if row is None:
            raise RuntimeError("authority_revision_unavailable")
        return str(int(row["revision"]))

    def _ensure_contract_locked(self) -> None:
        if self._index_ready:
            return
        columns = [
            str(row["name"] or "")
            for row in self.store.sqlite.conn.execute(
                "PRAGMA index_info(idx_records_vector_sync_cursor)"
            ).fetchall()
        ]
        if columns != ["updated_at", "storage_key"]:
            self.store.sqlite.conn.execute("DROP INDEX IF EXISTS idx_records_vector_sync_cursor")
            self.store.sqlite.conn.execute(
                "CREATE INDEX idx_records_vector_sync_cursor ON records(updated_at ASC, storage_key ASC)"
            )
        self.store.sqlite.conn.execute(
            "CREATE TABLE IF NOT EXISTS vector_sync_revision ("
            "singleton INTEGER PRIMARY KEY CHECK (singleton = 1), revision INTEGER NOT NULL)"
        )
        self.store.sqlite.conn.execute(
            "INSERT OR IGNORE INTO vector_sync_revision(singleton, revision) VALUES (1, 0)"
        )
        self.store.sqlite.conn.execute(
            "CREATE TABLE IF NOT EXISTS vector_sync_alias_guard ("
            "singleton INTEGER PRIMARY KEY CHECK (singleton = 1), suppress_revision INTEGER NOT NULL)"
        )
        self.store.sqlite.conn.execute(
            "INSERT OR IGNORE INTO vector_sync_alias_guard(singleton, suppress_revision) VALUES (1, 0)"
        )
        for operation in ("INSERT", "UPDATE", "DELETE"):
            name = f"trg_records_vector_sync_{operation.lower()}"
            self.store.sqlite.conn.execute(f"DROP TRIGGER IF EXISTS {name}")
            self.store.sqlite.conn.execute(
                f"CREATE TRIGGER {name} AFTER {operation} ON records BEGIN "
                "UPDATE vector_sync_revision SET revision = revision + 1 WHERE singleton = 1; END"
            )
            alias_name = f"trg_recall_alias_vector_sync_{operation.lower()}"
            self.store.sqlite.conn.execute(f"DROP TRIGGER IF EXISTS {alias_name}")
            self.store.sqlite.conn.execute(
                f"CREATE TRIGGER {alias_name} AFTER {operation} ON recall_alias_index "
                "WHEN COALESCE((SELECT suppress_revision FROM vector_sync_alias_guard WHERE singleton = 1), 0) = 0 "
                "BEGIN UPDATE vector_sync_revision SET revision = revision + 1 WHERE singleton = 1; END"
            )
        self.store.sqlite.conn.commit()
        self._index_ready = True


class PostgresVectorIndexSynchronizer:
    """Explicit, resumable projection sync; never mutates SQLite authority."""

    def __init__(
        self,
        *,
        reader: ProjectionReader,
        repository: SyncRepository,
        embedding_provider: EmbeddingProvider,
        config: PostgresVectorConfig,
        max_text_chars: int | None = None,
    ) -> None:
        self.reader = reader
        self.repository = repository
        self.embedding_provider = embedding_provider
        self.config = config
        configured_chars = config.projection_text_chars if max_text_chars is None else int(max_text_chars)
        if configured_chars != config.projection_text_chars:
            raise ValueError("projection_text_chars must match PostgresVectorConfig")
        self.max_text_chars = config.projection_text_chars
        self._lease_owner = f"worker-{uuid4().hex}"

    def sync(self, *, batch_size: int = 32, max_pages: int = 1) -> dict[str, Any]:
        bounded_batch = max(1, min(256, int(batch_size)))
        bounded_pages = max(1, min(10_000, int(max_pages)))
        if not self.config.enabled or not self.config.configured:
            return self._failure("not_configured")
        try:
            authority_revision = self.reader.snapshot_token()
            fingerprint = embedding_provider_fingerprint(self.embedding_provider, self.config)
            projection_fp = projection_fingerprint(self.config)
            progress = self.repository.begin_or_resume_sync(
                embedding_fingerprint=fingerprint,
                projection_digest_schema=PROJECTION_DIGEST_SCHEMA,
                projection_fingerprint=projection_fp,
                lease_owner=self._lease_owner,
                authority_revision=authority_revision,
            )
        except Exception as exc:
            return self._failure(_sync_error_code(exc, "postgres_sync_begin"))
        cursor = progress.cursor
        processed = 0
        pages = 0
        for _ in range(bounded_pages):
            try:
                rows = self.reader.page(cursor, limit=bounded_batch)
            except Exception as exc:
                self._release_lease(progress)
                return self._failure(_sync_error_code(exc, "sqlite_projection_read"))
            if not rows:
                if not self._authority_unchanged(authority_revision):
                    self._release_lease(progress)
                    return self._failure("authority_changed_during_sync")
                try:
                    self.repository.finalize_sync(
                        run_id=progress.run_id,
                        expected_cursor=cursor,
                        embedding_fingerprint=fingerprint,
                        projection_digest_schema=PROJECTION_DIGEST_SCHEMA,
                        projection_fingerprint=projection_fp,
                        lease_owner=self._lease_owner,
                        authority_revision=authority_revision,
                    )
                except Exception as exc:
                    self._release_lease(progress)
                    return self._failure(_sync_error_code(exc, "postgres_sync_apply"))
                return self._report(
                    progress=progress,
                    cursor=cursor,
                    pages=pages,
                    processed=processed,
                    complete=True,
                )
            texts = [self._embedding_text(row) for row in rows]
            try:
                self.repository.renew_sync_lease(
                    run_id=progress.run_id,
                    lease_owner=self._lease_owner,
                )
                vectors = self.embedding_provider.embed(
                    texts,
                    timeout_seconds=self._embedding_timeout_seconds(),
                )
                if len(vectors) != len(rows) or any(
                    len(vector) != self.config.vector_dimension
                    or not all(isfinite(float(value)) for value in vector)
                    for vector in vectors
                ):
                    raise RuntimeError("embedding_dimension_mismatch")
                if not self._authority_unchanged(authority_revision):
                    raise RuntimeError("authority_changed_during_sync")
                self.repository.renew_sync_lease(
                    run_id=progress.run_id,
                    lease_owner=self._lease_owner,
                )
            except Exception as exc:
                self._release_lease(progress)
                return self._failure(_sync_error_code(exc, "embedding"))
            projections = [
                self._candidate_projection(row, vector=vector, run_id=progress.run_id)
                for row, vector in zip(rows, vectors, strict=True)
            ]
            next_cursor = ProjectionCursor(
                updated_at=str(rows[-1].get("updated_at") or ""),
                storage_key=str(rows[-1].get("storage_key") or ""),
            )
            complete = len(rows) < bounded_batch
            if not self._authority_unchanged(authority_revision):
                self._release_lease(progress)
                return self._failure("authority_changed_during_sync")
            try:
                self.repository.renew_sync_lease(
                    run_id=progress.run_id,
                    lease_owner=self._lease_owner,
                )
                self.repository.apply_sync_page(
                    run_id=progress.run_id,
                    projections=projections,
                    expected_cursor=cursor,
                    next_cursor=next_cursor,
                    complete=False,
                    embedding_fingerprint=fingerprint,
                    projection_digest_schema=PROJECTION_DIGEST_SCHEMA,
                    projection_fingerprint=projection_fp,
                    lease_owner=self._lease_owner,
                    authority_revision=authority_revision,
                )
            except Exception as exc:
                self._release_lease(progress)
                return self._failure(_sync_error_code(exc, "postgres_sync_apply"))
            cursor = next_cursor
            pages += 1
            processed += len(rows)
            if complete:
                if not self._authority_unchanged(authority_revision):
                    self._release_lease(progress)
                    return self._failure("authority_changed_during_sync")
                try:
                    self.repository.finalize_sync(
                        run_id=progress.run_id,
                        expected_cursor=cursor,
                        embedding_fingerprint=fingerprint,
                        projection_digest_schema=PROJECTION_DIGEST_SCHEMA,
                        projection_fingerprint=projection_fp,
                        lease_owner=self._lease_owner,
                        authority_revision=authority_revision,
                    )
                except Exception as exc:
                    self._release_lease(progress)
                    return self._failure(_sync_error_code(exc, "postgres_sync_finalize"))
                return self._report(
                    progress=progress,
                    cursor=cursor,
                    pages=pages,
                    processed=processed,
                    complete=True,
                )
        self._release_lease(progress)
        return self._report(
            progress=progress,
            cursor=cursor,
            pages=pages,
            processed=processed,
            complete=False,
        )

    def _release_lease(self, progress: SyncProgress) -> None:
        try:
            self.repository.release_sync_lease(
                run_id=progress.run_id,
                lease_owner=self._lease_owner,
            )
        except Exception:
            return

    def _authority_unchanged(self, expected: str) -> bool:
        try:
            return self.reader.snapshot_token() == expected
        except Exception:
            return False

    def _embedding_timeout_seconds(self) -> float:
        lease_bound = max(0.05, self.config.sync_lease_seconds * 0.8)
        provider_timeout = getattr(self.embedding_provider, "timeout_seconds", lease_bound)
        try:
            return max(0.05, min(lease_bound, float(provider_timeout)))
        except (TypeError, ValueError, OverflowError):
            return lease_bound

    def _embedding_text(self, row: Mapping[str, Any]) -> str:
        return "\n".join(
            part
            for part in (
                str(row.get("title") or ""),
                " ".join(str(item) for item in list(row.get("aliases") or ())[:128]),
                str(row.get("keyword_text") or ""),
            )
            if part
        )[: self.max_text_chars]

    def _candidate_projection(
        self,
        row: Mapping[str, Any],
        *,
        vector: tuple[float, ...],
        run_id: str,
    ) -> dict[str, Any]:
        title_text = str(row.get("title") or "")[: self.max_text_chars]
        alias_text = " ".join(str(item) for item in list(row.get("aliases") or ())[:128])[: self.max_text_chars]
        keyword_text = str(row.get("keyword_text") or "")[: self.max_text_chars]
        authoritative: dict[str, Any] = {
            key: str(row.get(key) or "")
            for key in (
                "storage_key",
                "record_id",
                "kind",
                "status",
                "tenant_id",
                "agent_id",
                "workspace_id",
                "user_id",
                "source_id",
                "updated_at",
            )
        }
        authoritative.update(
            {"title_text": title_text, "alias_text": alias_text, "keyword_text": keyword_text}
        )
        digest = candidate_projection_digest(authoritative, max_text_chars=self.max_text_chars)
        return {
            **authoritative,
            "embedding": tuple(float(value) for value in vector),
            "projection_digest": digest,
            "projection_digest_schema": PROJECTION_DIGEST_SCHEMA,
            "index_watermark": str(run_id or "")[:256],
        }

    @staticmethod
    def _failure(code: str) -> dict[str, Any]:
        return {"ok": False, "complete": False, "error": str(code or "sync_unavailable")[:80]}

    @staticmethod
    def _report(
        *,
        progress: SyncProgress,
        cursor: ProjectionCursor,
        pages: int,
        processed: int,
        complete: bool,
    ) -> dict[str, Any]:
        return {
            "ok": True,
            "complete": complete,
            "resumed": progress.resumed,
            "pages": pages,
            "processed": processed,
            "watermark": progress.run_id if complete else "",
            "cursor": {"updated_at": cursor.updated_at, "storage_key": cursor.storage_key},
        }


def _sync_error_code(exc: Exception, default: str) -> str:
    text = str(exc)
    allowed = {
        "embedding_batch_invalid",
        "embedding_dimension_mismatch",
        "embedding_timeout",
        "postgres_sync_apply_failed",
        "postgres_sync_conflict",
        "postgres_sync_lease_held",
        "embedding_fingerprint_unavailable",
        "authority_changed_during_sync",
        "postgres_sync_lease_lost",
    }
    if text in allowed:
        return text
    if isinstance(exc, TimeoutError):
        return "embedding_timeout" if default == "embedding" else f"{default}_timeout"
    return default
