from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
from typing import Any

import pytest

from eimemory.api.runtime import Runtime
from eimemory.models.records import RecordEnvelope, ScopeRef
from eimemory.retrieval.postgres_ddl import DDL_VERSION, build_candidate_projection_ddl
from eimemory.retrieval.postgres_sync import (
    PostgresVectorIndexSynchronizer,
    ProjectionCursor,
    SQLiteProjectionReader,
    SyncProgress,
)
from eimemory.retrieval.postgres_vector import PostgresCandidateRepository, PostgresVectorConfig


class BatchProvider:
    def __init__(self, *, dimension: int = 3, error: Exception | None = None) -> None:
        self.dimension = dimension
        self.error = error
        self.calls: list[list[str]] = []

    def embed(self, texts: list[str], *, timeout_seconds: float | None = None) -> list[tuple[float, ...]]:
        self.calls.append(list(texts))
        if self.error:
            raise self.error
        return [tuple(float(position + 1) / 10 for position in range(self.dimension)) for _ in texts]

    def health(self) -> dict[str, object]:
        return {"available": self.error is None}


class FakeSyncRepository:
    def __init__(self, *, progress: SyncProgress | None = None, fail_apply: bool = False) -> None:
        self.progress = progress or SyncProgress(run_id="run-1", cursor=ProjectionCursor(), resumed=False)
        self.fail_apply = fail_apply
        self.apply_calls: list[dict[str, Any]] = []

    def begin_or_resume_sync(self) -> SyncProgress:
        return self.progress

    def apply_sync_page(
        self,
        *,
        run_id: str,
        projections: list[dict[str, Any]],
        next_cursor: ProjectionCursor,
        complete: bool,
    ) -> None:
        if self.fail_apply:
            raise RuntimeError("apply failed with postgresql://user:secret@host/db")
        self.apply_calls.append(
            {
                "run_id": run_id,
                "projections": projections,
                "next_cursor": next_cursor,
                "complete": complete,
            }
        )


class FakeReader:
    def __init__(self, pages: dict[tuple[str, str], list[dict[str, Any]]]) -> None:
        self.pages = pages
        self.calls: list[tuple[ProjectionCursor, int]] = []

    def page(self, cursor: ProjectionCursor, *, limit: int) -> list[dict[str, Any]]:
        self.calls.append((cursor, limit))
        return list(self.pages.get((cursor.updated_at, cursor.storage_key), []))[:limit]


def _projection(storage_key: str, updated_at: str, *, status: str = "active") -> dict[str, Any]:
    return {
        "storage_key": storage_key,
        "record_id": f"record-{storage_key}",
        "kind": "memory",
        "status": status,
        "tenant_id": "tenant",
        "agent_id": "openclaw",
        "workspace_id": "workspace",
        "user_id": "user",
        "source_id": "alpha",
        "title": f"Title {storage_key}",
        "aliases": [f"alias-{storage_key}"],
        "keyword_text": f"bounded body {storage_key}",
        "updated_at": updated_at,
    }


def test_versioned_ddl_is_candidate_only_idempotent_and_indexed() -> None:
    config = PostgresVectorConfig(
        schema="safe_schema",
        table="safe_candidates",
        vector_dimension=384,
    )

    statements = build_candidate_projection_ddl(config)
    ddl = "\n".join(statements)

    assert DDL_VERSION in ddl
    assert 'CREATE SCHEMA IF NOT EXISTS "safe_schema"' in ddl
    assert 'CREATE TABLE IF NOT EXISTS "safe_schema"."safe_candidates"' in ddl
    assert "embedding vector(384)" in ddl
    for field in (
        "storage_key",
        "record_id",
        "tenant_id",
        "agent_id",
        "workspace_id",
        "user_id",
        "source_id",
        "kind",
        "status",
        "title_text",
        "alias_text",
        "keyword_text",
        "payload_digest",
        "authoritative_updated_at",
        "index_watermark",
    ):
        assert field in ddl
    assert "USING hnsw" in ddl
    assert "vector_cosine_ops" in ddl
    assert "USING gin" in ddl
    assert ddl.count("IF NOT EXISTS") >= 6
    assert "payload_json" not in ddl


def test_sqlite_projection_reader_uses_keyset_bounded_projection_without_payload(tmp_path: Path) -> None:
    runtime = Runtime.create(root=tmp_path)
    scope = ScopeRef("tenant", "openclaw", "workspace", "user")
    try:
        records = [
            RecordEnvelope.create(
                kind="memory",
                title=f"Record {index}",
                summary=f"summary {index}",
                detail="detail " * 20,
                content={"text": f"body {index}"},
                aliases=[f"alias {index}"],
                source_id="alpha",
                scope=scope,
            )
            for index in range(3)
        ]
        for record in records:
            runtime.store.append(record)
        keys = [
            str(row["storage_key"])
            for row in runtime.store.sqlite.conn.execute("SELECT storage_key FROM records ORDER BY storage_key")
        ]
        for index, key in enumerate(keys):
            runtime.store.sqlite.conn.execute(
                "UPDATE records SET updated_at = ? WHERE storage_key = ?",
                (f"2026-07-22T00:00:0{index}Z", key),
            )
        runtime.store.sqlite.conn.commit()
        statements: list[str] = []
        runtime.store.sqlite.conn.set_trace_callback(statements.append)
        reader = SQLiteProjectionReader(runtime.store, max_text_chars=32)

        first = reader.page(ProjectionCursor(), limit=2)
        second = reader.page(
            ProjectionCursor(first[-1]["updated_at"], first[-1]["storage_key"]),
            limit=2,
        )

        assert len(first) == 2
        assert len(second) == 1
        assert all(len(row["keyword_text"]) <= 32 for row in [*first, *second])
        assert all(row["source_id"] == "alpha" for row in [*first, *second])
        select_sql = "\n".join(statement for statement in statements if "FROM records" in statement)
        assert "OFFSET" not in select_sql.upper()
        assert "payload_json" not in select_sql
        assert "(r.updated_at, r.storage_key) >" in select_sql
        index_columns = [
            str(row["name"])
            for row in runtime.store.sqlite.conn.execute("PRAGMA index_info(idx_records_vector_sync_cursor)")
        ]
        assert index_columns == ["updated_at", "storage_key"]
    finally:
        runtime.close()


def test_sync_pages_embeds_bounded_batches_and_only_completes_after_end() -> None:
    first = [_projection("a", "2026-07-22T00:00:01Z"), _projection("b", "2026-07-22T00:00:02Z")]
    second = [_projection("c", "2026-07-22T00:00:03Z", status="inactive")]
    reader = FakeReader(
        {
            ("", ""): first,
            ("2026-07-22T00:00:02Z", "b"): second,
        }
    )
    repository = FakeSyncRepository()
    provider = BatchProvider()
    syncer = PostgresVectorIndexSynchronizer(
        reader=reader,
        repository=repository,
        embedding_provider=provider,
        config=PostgresVectorConfig(enabled=True, dsn="postgresql://host/db", vector_dimension=3),
        max_text_chars=24,
    )

    report = syncer.sync(batch_size=2, max_pages=3)

    assert report == {
        "ok": True,
        "complete": True,
        "resumed": False,
        "pages": 2,
        "processed": 3,
        "watermark": "run-1",
        "cursor": {"updated_at": "2026-07-22T00:00:03Z", "storage_key": "c"},
    }
    assert [len(call) for call in provider.calls] == [2, 1]
    assert all(len(text) <= 24 for call in provider.calls for text in call)
    assert [call["complete"] for call in repository.apply_calls] == [False, True]
    final_rows = [row for call in repository.apply_calls for row in call["projections"]]
    assert all(len(row["embedding"]) == 3 for row in final_rows)
    assert all(len(row["payload_digest"]) == 64 for row in final_rows)
    assert all("payload" not in row for row in final_rows)
    assert final_rows[-1]["status"] == "inactive"


def test_sync_resume_is_keyset_idempotent_and_page_bounded() -> None:
    cursor = ProjectionCursor("2026-07-22T00:00:02Z", "b")
    reader = FakeReader({(cursor.updated_at, cursor.storage_key): [_projection("c", "2026-07-22T00:00:03Z")]})
    repository = FakeSyncRepository(progress=SyncProgress(run_id="same-run", cursor=cursor, resumed=True))
    syncer = PostgresVectorIndexSynchronizer(
        reader=reader,
        repository=repository,
        embedding_provider=BatchProvider(),
        config=PostgresVectorConfig(enabled=True, dsn="postgresql://host/db", vector_dimension=3),
    )

    report = syncer.sync(batch_size=2, max_pages=1)

    assert report["ok"] is True
    assert report["resumed"] is True
    assert report["complete"] is True
    assert reader.calls == [(cursor, 2)]
    assert repository.apply_calls[0]["run_id"] == "same-run"
    assert repository.apply_calls[0]["projections"][0]["index_watermark"] == "same-run"


def test_provider_failure_does_not_apply_page_or_advance_watermark_and_is_sanitized() -> None:
    reader = FakeReader({("", ""): [_projection("a", "2026-07-22T00:00:01Z")]})
    repository = FakeSyncRepository()
    syncer = PostgresVectorIndexSynchronizer(
        reader=reader,
        repository=repository,
        embedding_provider=BatchProvider(error=TimeoutError("Bearer secret-value")),
        config=PostgresVectorConfig(
            enabled=True,
            dsn="postgresql://user:secret@host/db",
            vector_dimension=3,
        ),
    )

    report = syncer.sync(batch_size=10, max_pages=1)

    assert report["ok"] is False
    assert report["error"] == "embedding_timeout"
    assert repository.apply_calls == []
    assert "secret" not in json.dumps(report)
    assert "user" not in json.dumps(report)


def test_wrong_provider_dimension_does_not_apply_page() -> None:
    repository = FakeSyncRepository()
    syncer = PostgresVectorIndexSynchronizer(
        reader=FakeReader({("", ""): [_projection("a", "2026-07-22T00:00:01Z")]}),
        repository=repository,
        embedding_provider=BatchProvider(dimension=2),
        config=PostgresVectorConfig(enabled=True, dsn="postgresql://host/db", vector_dimension=3),
    )

    report = syncer.sync(batch_size=10, max_pages=1)

    assert report["ok"] is False
    assert report["error"] == "embedding_dimension_mismatch"
    assert repository.apply_calls == []


class RecordingCursor:
    def __init__(self, *, fail_on: str = "") -> None:
        self.calls: list[tuple[str, Any]] = []
        self.fail_on = fail_on
        self.description: list[Any] = []

    def execute(self, sql: str, params: Any = None) -> None:
        self.calls.append((sql, params))
        if self.fail_on and self.fail_on in sql:
            raise RuntimeError("forced")

    def executemany(self, sql: str, params: Any) -> None:
        self.calls.append((sql, list(params)))
        if self.fail_on and self.fail_on in sql:
            raise RuntimeError("forced")

    def fetchone(self) -> dict[str, Any]:
        return {"run_id": "run-1", "in_progress": True}

    def __enter__(self) -> "RecordingCursor":
        return self

    def __exit__(self, *args: object) -> None:
        return None


class RecordingConnection:
    def __init__(self, *, fail_on: str = "") -> None:
        self.cursor_value = RecordingCursor(fail_on=fail_on)
        self.commits = 0
        self.rollbacks = 0
        self.closed = 0

    def cursor(self, **kwargs: object) -> RecordingCursor:
        return self.cursor_value

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1

    def close(self) -> None:
        self.closed += 1


def test_repository_final_page_upsert_state_and_stale_cleanup_are_one_transaction() -> None:
    connection = RecordingConnection()
    config = PostgresVectorConfig(
        enabled=True,
        connection_factory=lambda **kwargs: connection,
        vector_dimension=3,
        schema="safe",
        table="candidates",
    )
    repository = PostgresCandidateRepository(config)
    projection = {
        **_projection("a", "2026-07-22T00:00:01Z"),
        "title_text": "title",
        "alias_text": "alias",
        "keyword_text": "body",
        "embedding": (0.1, 0.2, 0.3),
        "payload_digest": "a" * 64,
        "index_watermark": "run-1",
    }

    repository.apply_sync_page(
        run_id="run-1",
        projections=[projection],
        next_cursor=ProjectionCursor(projection["updated_at"], projection["storage_key"]),
        complete=True,
    )

    sql = "\n".join(statement for statement, _ in connection.cursor_value.calls)
    assert "ON CONFLICT (storage_key) DO UPDATE" in sql
    assert "DELETE FROM \"safe\".\"candidates\" WHERE index_watermark <> %s" in sql
    assert "committed_watermark" in sql
    assert connection.commits == 1
    assert connection.rollbacks == 0


def test_repository_apply_failure_rolls_back_without_commit() -> None:
    connection = RecordingConnection(fail_on="DELETE FROM")
    repository = PostgresCandidateRepository(
        PostgresVectorConfig(
            enabled=True,
            connection_factory=lambda **kwargs: connection,
            vector_dimension=3,
        )
    )

    with pytest.raises(RuntimeError, match="postgres_sync_apply_failed"):
        repository.apply_sync_page(
            run_id="run-1",
            projections=[],
            next_cursor=ProjectionCursor(),
            complete=True,
        )

    assert connection.rollbacks == 1
    assert connection.commits == 0
