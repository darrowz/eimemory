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
from eimemory.retrieval.postgres_vector import (
    PROJECTION_DIGEST_SCHEMA,
    PostgresCandidateRepository,
    PostgresVectorConfig,
    projection_fingerprint,
)


EMBEDDING_FINGERPRINT = "f" * 64


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
        return {"configured": True, "available": self.error is None, "circuit": "closed", "dimension": self.dimension}

    def fingerprint(self) -> str:
        return EMBEDDING_FINGERPRINT


class FakeSyncRepository:
    def __init__(self, *, progress: SyncProgress | None = None, fail_apply: bool = False) -> None:
        self.progress = progress or SyncProgress(run_id="run-1", cursor=ProjectionCursor(), resumed=False)
        self.fail_apply = fail_apply
        self.apply_calls: list[dict[str, Any]] = []
        self.release_calls: list[dict[str, str]] = []
        self.invalidate_calls: list[dict[str, str]] = []
        self.renew_calls: list[dict[str, str]] = []
        self.finalize_calls: list[dict[str, Any]] = []

    def begin_or_resume_sync(self, **kwargs: Any) -> SyncProgress:
        return self.progress

    def apply_sync_page(
        self,
        *,
        run_id: str,
        projections: list[dict[str, Any]],
        expected_cursor: ProjectionCursor,
        next_cursor: ProjectionCursor,
        complete: bool,
        **kwargs: Any,
    ) -> None:
        if self.fail_apply:
            raise RuntimeError("apply failed with postgresql://user:secret@host/db")
        self.apply_calls.append(
            {
                "run_id": run_id,
                "projections": projections,
                "expected_cursor": expected_cursor,
                "next_cursor": next_cursor,
                "complete": complete,
            }
        )

    def release_sync_lease(self, *, run_id: str, lease_owner: str) -> None:
        self.release_calls.append({"run_id": run_id, "lease_owner": lease_owner})

    def invalidate_index(self, *, watermark: str, reason: str) -> None:
        self.invalidate_calls.append({"watermark": watermark, "reason": reason})

    def renew_sync_lease(self, *, run_id: str, lease_owner: str) -> None:
        self.renew_calls.append({"run_id": run_id, "lease_owner": lease_owner})

    def finalize_sync(self, **kwargs: Any) -> None:
        self.finalize_calls.append(dict(kwargs))


class FakeReader:
    def __init__(self, pages: dict[tuple[str, str], list[dict[str, Any]]]) -> None:
        self.pages = pages
        self.calls: list[tuple[ProjectionCursor, int]] = []
        self.revision = "0"

    def page(self, cursor: ProjectionCursor, *, limit: int) -> list[dict[str, Any]]:
        self.calls.append((cursor, limit))
        return list(self.pages.get((cursor.updated_at, cursor.storage_key), []))[:limit]

    def snapshot_token(self) -> str:
        return self.revision


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
    assert "PRIMARY KEY (storage_key, index_watermark)" in ddl
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
        "projection_digest",
        "authoritative_updated_at",
        "index_watermark",
    ):
        assert field in ddl
    assert "USING hnsw" in ddl
    assert "vector_cosine_ops" in ddl
    assert "USING gin" in ddl
    assert ddl.count("IF NOT EXISTS") >= 6
    assert "payload_json" not in ddl
    assert "postgres-vector-candidates.v1" in ddl
    assert "candidate_projection_migrations" in ddl
    assert ddl.index("postgres-vector-candidates.v1") < ddl.index("CREATE TABLE IF NOT EXISTS", ddl.index("postgres-vector-candidates.v1"))


def test_v1_upgrade_rebuilds_only_non_authoritative_candidate_tables() -> None:
    ddl = "\n".join(build_candidate_projection_ddl(PostgresVectorConfig(table="candidates")))
    start = ddl.index("DO $eimemory_v1_upgrade$")
    upgrade = ddl[start:ddl.index("$eimemory_v1_upgrade$", start + len("DO $eimemory_v1_upgrade$"))]

    assert 'DROP TABLE IF EXISTS "eimemory_recall"."candidates"' in upgrade
    assert 'DROP TABLE IF EXISTS "eimemory_recall"."candidates_sync_state"' in upgrade
    assert 'DROP TABLE IF EXISTS "eimemory_recall"."candidate_projection_migrations"' in upgrade
    assert "records" not in upgrade
    assert "payload_json" not in upgrade


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
        assert "substr(r.title, 1, 32)" in select_sql
        assert "substr(r.summary, 1, 32)" in select_sql
        assert "substr(r.detail, 1, 32)" in select_sql
        assert "substr(COALESCE" in select_sql
        assert "substr(a.normalized_alias, 1, 256)" in select_sql
        assert "LIMIT 128" in select_sql
        index_columns = [
            str(row["name"])
            for row in runtime.store.sqlite.conn.execute("PRAGMA index_info(idx_records_vector_sync_cursor)")
        ]
        assert index_columns == ["updated_at", "storage_key"]
    finally:
        runtime.close()


def test_sqlite_projection_reader_repairs_wrong_cursor_index_without_temp_sort(tmp_path: Path) -> None:
    runtime = Runtime.create(root=tmp_path)
    try:
        runtime.store.sqlite.conn.execute(
            "CREATE INDEX idx_records_vector_sync_cursor ON records(storage_key)"
        )
        runtime.store.sqlite.conn.commit()
        SQLiteProjectionReader(runtime.store).page(ProjectionCursor(), limit=1)

        columns = [
            str(row["name"])
            for row in runtime.store.sqlite.conn.execute(
                "PRAGMA index_info(idx_records_vector_sync_cursor)"
            )
        ]
        plan = " ".join(
            str(row["detail"])
            for row in runtime.store.sqlite.conn.execute(
                "EXPLAIN QUERY PLAN SELECT updated_at, storage_key FROM records "
                "ORDER BY updated_at, storage_key LIMIT 10"
            )
        )
        assert columns == ["updated_at", "storage_key"]
        assert "TEMP B-TREE" not in plan.upper()
    finally:
        runtime.close()


def test_sqlite_projection_revision_tracks_alias_only_mutations_without_upsert_amplification(
    tmp_path: Path,
) -> None:
    runtime = Runtime.create(root=tmp_path)
    scope = ScopeRef("tenant", "openclaw", "workspace", "user")
    record = runtime.store.append(
        RecordEnvelope.create(
            kind="memory", title="Alias authority", summary="stable", content={"text": "stable"},
            aliases=["first alias"], source_id="alpha", scope=scope,
        )
    )
    reader = SQLiteProjectionReader(runtime.store)
    try:
        initial = int(reader.snapshot_token())
        storage_key = runtime.store.sqlite._storage_key(record)
        runtime.store.sqlite.conn.execute(
            "INSERT INTO recall_alias_index (storage_key, normalized_alias, alias_ordinal, record_id, kind, status, source_id, tenant_id, agent_id, workspace_id, user_id) "
            "VALUES (?, 'direct alias', 99, ?, 'memory', 'active', 'alpha', 'tenant', 'openclaw', 'workspace', 'user')",
            (storage_key, record.record_id),
        )
        after_insert = int(reader.snapshot_token())
        runtime.store.sqlite.conn.execute(
            "UPDATE recall_alias_index SET normalized_alias='direct alias updated' WHERE storage_key=? AND alias_ordinal=99",
            (storage_key,),
        )
        after_update = int(reader.snapshot_token())
        runtime.store.sqlite.conn.execute(
            "DELETE FROM recall_alias_index WHERE storage_key=? AND alias_ordinal=99",
            (storage_key,),
        )
        after_delete = int(reader.snapshot_token())
        record.summary = "normal upsert"
        runtime.store.append(record)
        after_upsert = int(reader.snapshot_token())

        assert (after_insert, after_update, after_delete) == (initial + 1, initial + 2, initial + 3)
        assert after_upsert == after_delete + 1
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
        config=PostgresVectorConfig(
            enabled=True, dsn="postgresql://host/db", vector_dimension=3, projection_text_chars=24
        ),
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
    assert [call["complete"] for call in repository.apply_calls] == [False, False]
    assert len(repository.finalize_calls) == 1
    final_rows = [row for call in repository.apply_calls for row in call["projections"]]
    assert all(len(row["embedding"]) == 3 for row in final_rows)
    assert all(len(row["projection_digest"]) == 64 for row in final_rows)
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


def test_partial_sync_releases_lease_for_immediate_bounded_resume() -> None:
    rows = [_projection("a", "2026-07-22T00:00:01Z"), _projection("b", "2026-07-22T00:00:02Z")]
    repository = FakeSyncRepository()
    syncer = PostgresVectorIndexSynchronizer(
        reader=FakeReader({("", ""): rows}),
        repository=repository,
        embedding_provider=BatchProvider(),
        config=PostgresVectorConfig(enabled=True, dsn="postgresql://host/db", vector_dimension=3),
    )

    report = syncer.sync(batch_size=2, max_pages=1)

    assert report["ok"] is True and report["complete"] is False
    assert repository.release_calls[0]["run_id"] == "run-1"


def test_revision_change_between_runs_resets_cursor_and_captures_same_timestamp_earlier_key() -> None:
    class MutableReader:
        def __init__(self) -> None:
            self.rows = [_projection("b", "2026-07-22T00:00:01Z")]
            self.revision = "0"

        def snapshot_token(self) -> str:
            return self.revision

        def page(self, cursor: ProjectionCursor, *, limit: int) -> list[dict[str, Any]]:
            rows = sorted(self.rows, key=lambda row: (row["updated_at"], row["storage_key"]))
            return [
                row for row in rows
                if (row["updated_at"], row["storage_key"]) > (cursor.updated_at, cursor.storage_key)
            ][:limit]

    class RevisionRepository(FakeSyncRepository):
        def __init__(self) -> None:
            super().__init__()
            self.bound_revision = ""
            self.cursor = ProjectionCursor()
            self.run_number = 0

        def begin_or_resume_sync(self, **kwargs: Any) -> SyncProgress:
            revision = str(kwargs["authority_revision"])
            if revision != self.bound_revision:
                self.run_number += 1
                self.bound_revision = revision
                self.cursor = ProjectionCursor()
                return SyncProgress(
                    run_id=f"run-{self.run_number}", cursor=self.cursor, resumed=False,
                    authority_revision=revision,
                )
            return SyncProgress(
                run_id=f"run-{self.run_number}", cursor=self.cursor, resumed=True,
                authority_revision=revision,
            )

        def apply_sync_page(self, **kwargs: Any) -> None:
            super().apply_sync_page(**kwargs)
            self.cursor = kwargs["next_cursor"]

    reader = MutableReader()
    repository = RevisionRepository()
    config = PostgresVectorConfig(enabled=True, dsn="postgresql://host/db", vector_dimension=3)

    first = PostgresVectorIndexSynchronizer(
        reader=reader, repository=repository, embedding_provider=BatchProvider(), config=config
    ).sync(batch_size=1, max_pages=1)
    assert first["complete"] is False
    assert repository.cursor.storage_key == "b"

    reader.rows.append(_projection("a", "2026-07-22T00:00:01Z"))
    reader.revision = "1"
    second = PostgresVectorIndexSynchronizer(
        reader=reader, repository=repository, embedding_provider=BatchProvider(), config=config
    ).sync(batch_size=1, max_pages=1)

    assert second["resumed"] is False
    assert repository.apply_calls[-1]["projections"][0]["storage_key"] == "a"


def test_mutation_during_embedding_does_not_apply_or_advance_watermark() -> None:
    reader = FakeReader({("", ""): [_projection("a", "2026-07-22T00:00:01Z")]})

    class MutatingProvider(BatchProvider):
        def embed(self, texts: list[str], *, timeout_seconds: float | None = None) -> list[tuple[float, ...]]:
            reader.revision = "1"
            return super().embed(texts, timeout_seconds=timeout_seconds)

    repository = FakeSyncRepository()
    report = PostgresVectorIndexSynchronizer(
        reader=reader,
        repository=repository,
        embedding_provider=MutatingProvider(),
        config=PostgresVectorConfig(enabled=True, dsn="postgresql://host/db", vector_dimension=3),
    ).sync(batch_size=10, max_pages=1)

    assert report == {"ok": False, "complete": False, "error": "authority_changed_during_sync"}
    assert repository.apply_calls == []
    assert repository.release_calls


def test_mutation_after_staging_apply_never_finalizes_or_replaces_prior_committed_watermark() -> None:
    reader = FakeReader({("", ""): [_projection("a", "2026-07-22T00:00:01Z")]})

    class RaceRepository(FakeSyncRepository):
        def apply_sync_page(self, **kwargs: Any) -> None:
            super().apply_sync_page(**kwargs)
            reader.revision = "1"

    repository = RaceRepository()
    repository.prior_committed = "prior-wm"
    report = PostgresVectorIndexSynchronizer(
        reader=reader,
        repository=repository,
        embedding_provider=BatchProvider(),
        config=PostgresVectorConfig(enabled=True, dsn="postgresql://host/db", vector_dimension=3),
    ).sync(batch_size=10, max_pages=1)

    assert report == {"ok": False, "complete": False, "error": "authority_changed_during_sync"}
    assert len(repository.apply_calls) == 1
    assert repository.apply_calls[0]["complete"] is False
    assert repository.finalize_calls == []
    assert repository.prior_committed == "prior-wm"


def test_continuously_mutating_multi_page_sync_fails_fast_without_partial_progress() -> None:
    rows = [
        _projection(chr(ord("a") + index), f"2026-07-22T00:00:0{index + 1}Z")
        for index in range(4)
    ]
    reader = FakeReader({("", ""): rows})

    class AlwaysMutatingProvider(BatchProvider):
        def embed(self, texts: list[str], *, timeout_seconds: float | None = None) -> list[tuple[float, ...]]:
            reader.revision = str(int(reader.revision) + 1)
            return super().embed(texts, timeout_seconds=timeout_seconds)

    repository = FakeSyncRepository()
    report = PostgresVectorIndexSynchronizer(
        reader=reader,
        repository=repository,
        embedding_provider=AlwaysMutatingProvider(),
        config=PostgresVectorConfig(enabled=True, dsn="postgresql://host/db", vector_dimension=3),
    ).sync(batch_size=2, max_pages=10)

    assert report == {"ok": False, "complete": False, "error": "authority_changed_during_sync"}
    assert repository.apply_calls == []
    assert len(repository.release_calls) == 1


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


def test_sync_embedding_deadline_is_bounded_by_lease_not_database_connect_timeout() -> None:
    class TimeoutRecordingProvider(BatchProvider):
        def __init__(self) -> None:
            super().__init__()
            self.timeouts: list[float | None] = []

        def embed(self, texts: list[str], *, timeout_seconds: float | None = None) -> list[tuple[float, ...]]:
            self.timeouts.append(timeout_seconds)
            return super().embed(texts, timeout_seconds=timeout_seconds)

    provider = TimeoutRecordingProvider()
    syncer = PostgresVectorIndexSynchronizer(
        reader=FakeReader({("", ""): [_projection("a", "2026-07-22T00:00:01Z")]}),
        repository=FakeSyncRepository(),
        embedding_provider=provider,
        config=PostgresVectorConfig(
            enabled=True, dsn="postgresql://host/db", vector_dimension=3,
            connect_timeout_seconds=0.05, sync_lease_seconds=5,
        ),
    )

    assert syncer.sync(batch_size=10, max_pages=1)["ok"] is True
    assert provider.timeouts == [4.0]
    assert len(syncer.repository.renew_calls) == 3


def test_sync_preserves_shorter_provider_timeout_inside_long_lease() -> None:
    class ShortProvider(BatchProvider):
        timeout_seconds = 5.0

        def __init__(self) -> None:
            super().__init__()
            self.timeouts: list[float | None] = []

        def embed(self, texts: list[str], *, timeout_seconds: float | None = None) -> list[tuple[float, ...]]:
            self.timeouts.append(timeout_seconds)
            return super().embed(texts, timeout_seconds=timeout_seconds)

    provider = ShortProvider()
    report = PostgresVectorIndexSynchronizer(
        reader=FakeReader({("", ""): [_projection("a", "2026-07-22T00:00:01Z")]}),
        repository=FakeSyncRepository(),
        embedding_provider=provider,
        config=PostgresVectorConfig(
            enabled=True, dsn="postgresql://host/db", vector_dimension=3, sync_lease_seconds=60,
        ),
    ).sync(batch_size=10, max_pages=1)

    assert report["ok"] is True
    assert provider.timeouts == [5.0]


def test_sync_renews_lease_before_and_after_embedding_and_again_before_apply() -> None:
    events: list[str] = []

    class EventRepository(FakeSyncRepository):
        def renew_sync_lease(self, *, run_id: str, lease_owner: str) -> None:
            events.append("renew")
            super().renew_sync_lease(run_id=run_id, lease_owner=lease_owner)

        def apply_sync_page(self, **kwargs: Any) -> None:
            events.append("apply")
            super().apply_sync_page(**kwargs)

    class SlowWindowProvider(BatchProvider):
        timeout_seconds = 120.0

        def embed(self, texts: list[str], *, timeout_seconds: float | None = None) -> list[tuple[float, ...]]:
            assert timeout_seconds == 4.0  # 80% of the minimum five-second lease.
            events.append("embed")
            return super().embed(texts, timeout_seconds=timeout_seconds)

    report = PostgresVectorIndexSynchronizer(
        reader=FakeReader({("", ""): [_projection("a", "2026-07-22T00:00:01Z")]}),
        repository=EventRepository(),
        embedding_provider=SlowWindowProvider(),
        config=PostgresVectorConfig(
            enabled=True, dsn="postgresql://host/db", vector_dimension=3, sync_lease_seconds=5,
        ),
    ).sync(batch_size=10, max_pages=1)

    assert report["ok"] is True
    assert events == ["renew", "embed", "renew", "renew", "apply"]


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
        return {
            "run_id": "run-1",
            "in_progress": True,
            "cursor_updated_at": "",
            "cursor_storage_key": "",
            "embedding_fingerprint": EMBEDDING_FINGERPRINT,
            "projection_digest_schema": PROJECTION_DIGEST_SCHEMA,
            "projection_fingerprint": projection_fingerprint(PostgresVectorConfig(vector_dimension=3)),
            "lease_owner": "owner",
            "lease_active": True,
            "authority_revision": "0",
            "staging_embedding_fingerprint": EMBEDDING_FINGERPRINT,
            "staging_projection_digest_schema": PROJECTION_DIGEST_SCHEMA,
            "staging_projection_fingerprint": projection_fingerprint(PostgresVectorConfig(vector_dimension=3)),
            "staging_authority_revision": "0",
        }

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


def test_repository_page_only_stages_rows_before_separate_finalize() -> None:
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
        "projection_digest": "a" * 64,
        "projection_digest_schema": "candidate-projection.v1",
        "index_watermark": "run-1",
    }

    repository.apply_sync_page(
        run_id="run-1",
        projections=[projection],
        expected_cursor=ProjectionCursor(),
        next_cursor=ProjectionCursor(projection["updated_at"], projection["storage_key"]),
        complete=False,
        embedding_fingerprint=EMBEDDING_FINGERPRINT,
        projection_digest_schema=PROJECTION_DIGEST_SCHEMA,
        projection_fingerprint=projection_fingerprint(config),
        lease_owner="owner",
        authority_revision="0",
    )

    sql = "\n".join(statement for statement, _ in connection.cursor_value.calls)
    assert "ON CONFLICT (storage_key, index_watermark) DO UPDATE" in sql
    assert "DELETE FROM \"safe\".\"candidates\" WHERE index_watermark <> %s" not in sql
    assert "committed_watermark" not in sql
    assert connection.commits == 1
    assert connection.rollbacks == 0


def test_repository_finalize_atomically_switches_committed_watermark_and_cleans_old_rows() -> None:
    connection = RecordingConnection()
    config = PostgresVectorConfig(
        enabled=True, connection_factory=lambda **kwargs: connection, vector_dimension=3,
        schema="safe", table="candidates",
    )
    repository = PostgresCandidateRepository(config)

    repository.finalize_sync(
        run_id="run-1",
        expected_cursor=ProjectionCursor(),
        embedding_fingerprint=EMBEDDING_FINGERPRINT,
        projection_digest_schema=PROJECTION_DIGEST_SCHEMA,
        projection_fingerprint=projection_fingerprint(config),
        lease_owner="owner",
        authority_revision="0",
    )

    sql = "\n".join(statement for statement, _ in connection.cursor_value.calls)
    assert 'DELETE FROM "safe"."candidates" WHERE index_watermark <> %s' in sql
    assert "committed_watermark = %s" in sql
    assert "staging_embedding_fingerprint = ''" in sql
    assert connection.commits == 1
    assert connection.rollbacks == 0


def test_repository_apply_failure_rolls_back_without_commit() -> None:
    connection = RecordingConnection(fail_on="SET cursor_updated_at")
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
            expected_cursor=ProjectionCursor(),
            next_cursor=ProjectionCursor(),
            complete=True,
            embedding_fingerprint=EMBEDDING_FINGERPRINT,
            projection_digest_schema=PROJECTION_DIGEST_SCHEMA,
            projection_fingerprint=projection_fingerprint(repository.config),
            lease_owner="owner",
            authority_revision="0",
        )

    assert connection.rollbacks == 1
    assert connection.commits == 0


class MigrationCursor(RecordingCursor):
    def __init__(self, schema_row: dict[str, Any]) -> None:
        super().__init__()
        self.schema_row = schema_row

    def fetchone(self) -> dict[str, Any]:
        return self.schema_row


class MigrationConnection(RecordingConnection):
    def __init__(self, schema_row: dict[str, Any]) -> None:
        super().__init__()
        self.cursor_value = MigrationCursor(schema_row)


def _valid_postgres_projection_schema(*, dimension: int = 3) -> dict[str, Any]:
    return {
        "embedding_type": f"vector({dimension})",
        "primary_key": "PRIMARY KEY (storage_key, index_watermark)",
        "hnsw_index": "CREATE INDEX candidates_embedding_hnsw_idx ON safe.candidates USING hnsw (embedding vector_cosine_ops)",
        "gin_index": "CREATE INDEX candidates_search_gin_idx ON safe.candidates USING gin (search_tsv)",
        "scope_index": "CREATE INDEX candidates_scope_idx ON safe.candidates (tenant_id, agent_id, workspace_id, user_id, source_id, status, kind)",
        "vector_version": "0.8.1",
        "migration_version": True,
        "candidate_columns": [
            "storage_key", "record_id", "tenant_id", "agent_id", "workspace_id", "user_id",
            "source_id", "kind", "status", "embedding", "title_text", "alias_text", "keyword_text",
            "search_tsv", "projection_digest", "projection_digest_schema", "authoritative_updated_at",
            "index_watermark", "indexed_at",
        ],
        "state_columns": [
            "singleton", "ready", "in_progress", "run_id", "cursor_updated_at", "cursor_storage_key",
            "committed_watermark", "authoritative_updated_at", "authoritative_storage_key",
            "completed_at", "embedding_fingerprint", "projection_digest_schema", "projection_fingerprint",
            "lease_owner", "lease_expires_at", "updated_at",
            "authority_revision",
            "staging_embedding_fingerprint", "staging_projection_digest_schema",
            "staging_projection_fingerprint", "staging_authority_revision",
        ],
    }


def test_migrate_validates_existing_dimension_primary_key_and_index_definitions() -> None:
    connection = MigrationConnection(_valid_postgres_projection_schema())
    repository = PostgresCandidateRepository(
        PostgresVectorConfig(
            enabled=True,
            connection_factory=lambda **kwargs: connection,
            vector_dimension=3,
            schema="safe",
            table="candidates",
        )
    )

    assert repository.migrate()["ok"] is True
    validation_sql = "\n".join(statement for statement, _ in connection.cursor_value.calls)
    assert "format_type" in validation_sql
    assert "pg_get_constraintdef" in validation_sql
    assert "pg_indexes" in validation_sql
    validation_params = next(
        params for sql, params in connection.cursor_value.calls if "format_type" in sql
    )
    assert validation_params[-3] == DDL_VERSION
    assert validation_params[-2] == "safe.candidates"
    assert str(validation_params[-1]).startswith("safe.candidates_state_")
    assert connection.commits == 1


@pytest.mark.parametrize(
    "field,value",
    [
        ("embedding_type", "vector(384)"),
        ("primary_key", "PRIMARY KEY (storage_key)"),
        ("hnsw_index", "CREATE INDEX bad USING btree (embedding)"),
        ("gin_index", "CREATE INDEX bad USING btree (search_tsv)"),
        ("scope_index", "CREATE INDEX bad USING btree (tenant_id)"),
        ("vector_version", "0.7.4"),
        ("migration_version", False),
        ("candidate_columns", ["storage_key", "embedding"]),
    ],
)
def test_migrate_fails_closed_on_existing_projection_schema_drift(field: str, value: str) -> None:
    schema = _valid_postgres_projection_schema()
    schema[field] = value
    connection = MigrationConnection(schema)
    repository = PostgresCandidateRepository(
        PostgresVectorConfig(
            enabled=True,
            connection_factory=lambda **kwargs: connection,
            vector_dimension=3,
            schema="safe",
            table="candidates",
        )
    )

    with pytest.raises(RuntimeError, match="postgres_migration_failed"):
        repository.migrate()

    assert connection.rollbacks == 1
    assert connection.commits == 0


def test_repository_rejects_out_of_order_concurrent_cursor_without_regression() -> None:
    connection = RecordingConnection()
    connection.cursor_value.fetchone = lambda: {
        "run_id": "run-1",
        "in_progress": True,
        "cursor_updated_at": "2026-07-22T00:00:09Z",
        "cursor_storage_key": "later",
        "embedding_fingerprint": EMBEDDING_FINGERPRINT,
        "projection_digest_schema": PROJECTION_DIGEST_SCHEMA,
        "projection_fingerprint": projection_fingerprint(PostgresVectorConfig(vector_dimension=3)),
        "lease_owner": "owner",
        "lease_active": True,
        "authority_revision": "0",
        "staging_embedding_fingerprint": EMBEDDING_FINGERPRINT,
        "staging_projection_digest_schema": PROJECTION_DIGEST_SCHEMA,
        "staging_projection_fingerprint": projection_fingerprint(PostgresVectorConfig(vector_dimension=3)),
        "staging_authority_revision": "0",
    }
    repository = PostgresCandidateRepository(
        PostgresVectorConfig(enabled=True, connection_factory=lambda **kwargs: connection, vector_dimension=3)
    )

    with pytest.raises(RuntimeError, match="postgres_sync_apply_failed"):
        repository.apply_sync_page(
            run_id="run-1",
            projections=[],
            expected_cursor=ProjectionCursor("2026-07-22T00:00:01Z", "earlier"),
            next_cursor=ProjectionCursor("2026-07-22T00:00:02Z", "next"),
            complete=False,
            embedding_fingerprint=EMBEDDING_FINGERPRINT,
            projection_digest_schema=PROJECTION_DIGEST_SCHEMA,
            projection_fingerprint=projection_fingerprint(repository.config),
            lease_owner="owner",
            authority_revision="0",
        )

    sql = "\n".join(statement for statement, _ in connection.cursor_value.calls)
    assert "cursor_updated_at" in sql
    assert connection.rollbacks == 1
    assert connection.commits == 0


def test_active_sync_lease_rejects_concurrent_embedding_worker() -> None:
    connection = RecordingConnection()
    connection.cursor_value.fetchone = lambda: {
        **RecordingCursor().fetchone(),
        "lease_owner": "other-worker",
        "lease_active": True,
        "in_progress": True,
        "run_id": "existing-run",
        "committed_watermark": "committed",
    }
    repository = PostgresCandidateRepository(
        PostgresVectorConfig(enabled=True, connection_factory=lambda **kwargs: connection, vector_dimension=3)
    )

    with pytest.raises(RuntimeError, match="postgres_sync_lease_held"):
        repository.begin_or_resume_sync(
            embedding_fingerprint=EMBEDDING_FINGERPRINT,
            projection_digest_schema=PROJECTION_DIGEST_SCHEMA,
            projection_fingerprint=projection_fingerprint(repository.config),
            lease_owner="new-worker",
            authority_revision="0",
        )

    assert connection.rollbacks == 1


def test_incompatible_restart_preserves_committed_metadata_and_gc_keeps_committed_rows() -> None:
    connection = RecordingConnection()
    old_fingerprint = "a" * 64
    connection.cursor_value.fetchone = lambda: {
        "run_id": "abandoned-run",
        "in_progress": True,
        "cursor_updated_at": "2026-07-22T00:00:01Z",
        "cursor_storage_key": "b",
        "embedding_fingerprint": old_fingerprint,
        "projection_digest_schema": PROJECTION_DIGEST_SCHEMA,
        "projection_fingerprint": "b" * 64,
        "authority_revision": "7",
        "staging_embedding_fingerprint": old_fingerprint,
        "staging_projection_digest_schema": PROJECTION_DIGEST_SCHEMA,
        "staging_projection_fingerprint": "b" * 64,
        "staging_authority_revision": "7",
        "lease_owner": "old-worker",
        "lease_active": False,
        "committed_watermark": "committed-run",
    }
    repository = PostgresCandidateRepository(
        PostgresVectorConfig(enabled=True, connection_factory=lambda **kwargs: connection, vector_dimension=3)
    )

    progress = repository.begin_or_resume_sync(
        embedding_fingerprint=EMBEDDING_FINGERPRINT,
        projection_digest_schema=PROJECTION_DIGEST_SCHEMA,
        projection_fingerprint=projection_fingerprint(repository.config),
        lease_owner="new-worker",
        authority_revision="8",
    )

    assert progress.resumed is False
    assert progress.cursor == ProjectionCursor()
    calls = connection.cursor_value.calls
    gc_sql, gc_params = next((sql, params) for sql, params in calls if "DELETE FROM" in sql)
    assert "index_watermark <> %s" in gc_sql
    assert gc_params == ("committed-run",)
    staging_sql, staging_params = next(
        (sql, params) for sql, params in calls if "staging_embedding_fingerprint = %s" in sql
    )
    assert "ready = FALSE" not in staging_sql
    assert " embedding_fingerprint = %s" not in staging_sql.replace("staging_embedding_fingerprint", "")
    assert old_fingerprint not in staging_params
