from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
from math import isfinite
from typing import Any, Mapping, Protocol

from eimemory.storage.runtime_store import RuntimeStore

from .postgres_vector import EmbeddingProvider, PostgresVectorConfig


@dataclass(frozen=True, slots=True)
class ProjectionCursor:
    updated_at: str = ""
    storage_key: str = ""


@dataclass(frozen=True, slots=True)
class SyncProgress:
    run_id: str
    cursor: ProjectionCursor
    resumed: bool


class ProjectionReader(Protocol):
    def page(self, cursor: ProjectionCursor, *, limit: int) -> list[dict[str, Any]]: ...


class SyncRepository(Protocol):
    def begin_or_resume_sync(self) -> SyncProgress: ...

    def apply_sync_page(
        self,
        *,
        run_id: str,
        projections: list[dict[str, Any]],
        expected_cursor: ProjectionCursor,
        next_cursor: ProjectionCursor,
        complete: bool,
    ) -> None: ...


class SQLiteProjectionReader:
    """Bounded metadata/text projection reader over the SQLite authority."""

    def __init__(self, store: RuntimeStore, *, max_text_chars: int = 16_000) -> None:
        self.store = store
        self.max_text_chars = max(1, min(1_000_000, int(max_text_chars)))
        self._index_ready = False

    def page(self, cursor: ProjectionCursor, *, limit: int) -> list[dict[str, Any]]:
        bounded_limit = max(1, min(1_000, int(limit)))
        with self.store._lock:  # preserve RuntimeStore's single-writer/read contract
            if not self._index_ready:
                self.store.sqlite.conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_records_vector_sync_cursor "
                    "ON records(updated_at ASC, storage_key ASC)"
                )
                self.store.sqlite.conn.commit()
                self._index_ready = True
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
                    r.title, r.summary, r.detail,
                    substr(r.content_text, 1, ?) AS bounded_content_text,
                    COALESCE((
                        SELECT group_concat(a.normalized_alias, char(31))
                        FROM recall_alias_index AS a
                        WHERE a.storage_key = r.storage_key
                    ), '') AS alias_text,
                    r.updated_at
                FROM records AS r
                {keyset_clause}
                ORDER BY r.updated_at ASC, r.storage_key ASC
                LIMIT ?
                """,
                (
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


class PostgresVectorIndexSynchronizer:
    """Explicit, resumable projection sync; never mutates SQLite authority."""

    def __init__(
        self,
        *,
        reader: ProjectionReader,
        repository: SyncRepository,
        embedding_provider: EmbeddingProvider,
        config: PostgresVectorConfig,
        max_text_chars: int = 16_000,
    ) -> None:
        self.reader = reader
        self.repository = repository
        self.embedding_provider = embedding_provider
        self.config = config
        self.max_text_chars = max(1, min(1_000_000, int(max_text_chars)))

    def sync(self, *, batch_size: int = 32, max_pages: int = 1) -> dict[str, Any]:
        bounded_batch = max(1, min(256, int(batch_size)))
        bounded_pages = max(1, min(10_000, int(max_pages)))
        if not self.config.enabled or not self.config.configured:
            return self._failure("not_configured")
        try:
            progress = self.repository.begin_or_resume_sync()
        except Exception as exc:
            return self._failure(_sync_error_code(exc, "postgres_sync_begin"))
        cursor = progress.cursor
        processed = 0
        pages = 0
        for _ in range(bounded_pages):
            try:
                rows = self.reader.page(cursor, limit=bounded_batch)
            except Exception as exc:
                return self._failure(_sync_error_code(exc, "sqlite_projection_read"))
            if not rows:
                try:
                    self.repository.apply_sync_page(
                        run_id=progress.run_id,
                        projections=[],
                        expected_cursor=cursor,
                        next_cursor=cursor,
                        complete=True,
                    )
                except Exception as exc:
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
                vectors = self.embedding_provider.embed(
                    texts,
                    timeout_seconds=self.config.connect_timeout_seconds,
                )
                if len(vectors) != len(rows) or any(
                    len(vector) != self.config.vector_dimension
                    or not all(isfinite(float(value)) for value in vector)
                    for vector in vectors
                ):
                    raise RuntimeError("embedding_dimension_mismatch")
            except Exception as exc:
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
            try:
                self.repository.apply_sync_page(
                    run_id=progress.run_id,
                    projections=projections,
                    expected_cursor=cursor,
                    next_cursor=next_cursor,
                    complete=complete,
                )
            except Exception as exc:
                return self._failure(_sync_error_code(exc, "postgres_sync_apply"))
            cursor = next_cursor
            pages += 1
            processed += len(rows)
            if complete:
                return self._report(
                    progress=progress,
                    cursor=cursor,
                    pages=pages,
                    processed=processed,
                    complete=True,
                )
        return self._report(
            progress=progress,
            cursor=cursor,
            pages=pages,
            processed=processed,
            complete=False,
        )

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
        authoritative = {
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
        digest = sha256(
            json.dumps(authoritative, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        return {
            **authoritative,
            "embedding": tuple(float(value) for value in vector),
            "payload_digest": digest,
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
    }
    if text in allowed:
        return text
    if isinstance(exc, TimeoutError):
        return "embedding_timeout" if default == "embedding" else f"{default}_timeout"
    return default
