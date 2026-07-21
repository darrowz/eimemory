from __future__ import annotations

import json
from pathlib import Path
import tomllib
from typing import Any

from eimemory.cli.main import _build_parser, main as cli_main


def test_vector_index_cli_parser_is_explicit_and_bounded() -> None:
    parsed = _build_parser().parse_args(
        ["vector-index", "sync", "--batch-size", "17", "--max-pages", "3"]
    )
    assert parsed.command == "vector-index"
    assert parsed.vector_index_command == "sync"
    assert parsed.batch_size == 17
    assert parsed.max_pages == 3


def test_vector_index_status_default_is_disabled_without_optional_dependencies(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    monkeypatch.setenv("EIMEMORY_ROOT", str(tmp_path))
    monkeypatch.delenv("EIMEMORY_POSTGRES_VECTOR_ENABLED", raising=False)
    monkeypatch.delenv("EIMEMORY_POSTGRES_VECTOR_DSN", raising=False)
    monkeypatch.delenv("EIMEMORY_EMBEDDINGS_API_KEY", raising=False)

    assert cli_main(["vector-index", "status"]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert payload["vector_index"]["enabled"] is False
    assert payload["vector_index"]["configured"] is False
    assert payload["vector_index"]["available"] is False


def test_vector_index_sync_requires_dedicated_embedding_configuration_and_redacts_secrets(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    monkeypatch.setenv("EIMEMORY_ROOT", str(tmp_path))
    monkeypatch.setenv("EIMEMORY_POSTGRES_VECTOR_ENABLED", "1")
    monkeypatch.setenv("EIMEMORY_POSTGRES_VECTOR_DSN", "postgresql://user:pg-secret@host/db")
    monkeypatch.setenv("EIMEMORY_EMBEDDINGS_API_KEY", "embedding-secret")
    monkeypatch.delenv("EIMEMORY_EMBEDDINGS_MODEL", raising=False)

    assert cli_main(["vector-index", "sync", "--max-pages", "1"]) == 1

    output = capsys.readouterr().out
    payload = json.loads(output)
    assert payload["ok"] is False
    assert payload["error"] == "embedding_not_configured"
    assert "pg-secret" not in output
    assert "embedding-secret" not in output
    assert "user" not in output


def test_vector_index_migrate_and_sync_use_explicit_one_shot_commands(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    from eimemory.retrieval import postgres_cli

    monkeypatch.setenv("EIMEMORY_ROOT", str(tmp_path))
    monkeypatch.setenv("EIMEMORY_POSTGRES_VECTOR_ENABLED", "1")
    monkeypatch.setenv("EIMEMORY_POSTGRES_VECTOR_DSN", "postgresql://user:secret@host/db")
    monkeypatch.setenv("EIMEMORY_POSTGRES_VECTOR_DIMENSION", "3")
    monkeypatch.setenv("EIMEMORY_EMBEDDINGS_BASE_URL", "https://embedding.example/v1")
    monkeypatch.setenv("EIMEMORY_EMBEDDINGS_API_KEY", "embedding-secret")
    monkeypatch.setenv("EIMEMORY_EMBEDDINGS_MODEL", "embed-3")

    class Repository:
        def __init__(self, config: Any) -> None:
            self.config = config

        def migrate(self) -> dict[str, object]:
            return {"ok": True, "ddl_version": "postgres-vector-candidates.v1"}

    sync_calls: list[dict[str, int]] = []

    class Synchronizer:
        def __init__(self, **kwargs: Any) -> None:
            assert kwargs["config"].vector_dimension == 3

        def sync(self, *, batch_size: int, max_pages: int) -> dict[str, object]:
            sync_calls.append({"batch_size": batch_size, "max_pages": max_pages})
            return {"ok": True, "complete": False, "watermark": ""}

    monkeypatch.setattr(postgres_cli, "PostgresCandidateRepository", Repository)
    monkeypatch.setattr(postgres_cli, "PostgresVectorIndexSynchronizer", Synchronizer)

    assert cli_main(["vector-index", "migrate"]) == 0
    migrate_output = capsys.readouterr().out
    assert "secret" not in migrate_output
    assert cli_main(["vector-index", "sync", "--batch-size", "7", "--max-pages", "2"]) == 0
    sync_output = capsys.readouterr().out
    assert sync_calls == [{"batch_size": 7, "max_pages": 2}]
    assert "secret" not in sync_output


def test_postgres_driver_is_an_optional_extra_not_a_default_dependency() -> None:
    project = tomllib.loads((Path(__file__).parents[1] / "pyproject.toml").read_text(encoding="utf-8"))
    assert project["project"]["dependencies"] == []
    assert project["project"]["optional-dependencies"]["postgres"] == ["psycopg[binary]>=3.1,<4"]


def test_status_uses_sqlite_authority_revision_and_fails_closed_after_write(tmp_path: Path, monkeypatch) -> None:
    from eimemory.api.runtime import Runtime
    from eimemory.models.records import RecordEnvelope, ScopeRef
    from eimemory.retrieval import postgres_cli
    from eimemory.retrieval.postgres_sync import SQLiteProjectionReader
    from eimemory.retrieval.postgres_vector import (
        PROJECTION_DIGEST_SCHEMA,
        IndexState,
        PostgresVectorConfig,
        projection_fingerprint,
    )

    class Provider:
        def fingerprint(self) -> str:
            return "f" * 64

        def health(self) -> dict[str, object]:
            return {"configured": True, "available": True, "circuit": "closed", "dimension": 3}

    runtime = Runtime.create(root=tmp_path)
    try:
        revision = SQLiteProjectionReader(runtime.store).snapshot_token()
        config = PostgresVectorConfig(
            enabled=True, dsn="postgresql://host/db", vector_dimension=3, embedding_provider=Provider()
        )
        state = IndexState(
            ready=True,
            watermark="wm-1",
            lag_seconds=0.0,
            authoritative_updated_at="",
            authoritative_storage_key="",
            embedding_fingerprint="f" * 64,
            projection_digest_schema=PROJECTION_DIGEST_SCHEMA,
            projection_fingerprint=projection_fingerprint(config),
            authority_revision=revision,
        )

        class Repository:
            def __init__(self, _config: Any) -> None:
                pass

            def read_index_state(self) -> IndexState:
                return state

        monkeypatch.setattr(postgres_cli, "PostgresCandidateRepository", Repository)
        assert postgres_cli._status(config, runtime=runtime)["available"] is True

        runtime.store.append(RecordEnvelope.create(
            kind="memory", title="new authority write", content={"text": "content"},
            scope=ScopeRef("tenant", "agent", "workspace", "user"), meta={"force_capture": True},
        ))
        stale = postgres_cli._status(config, runtime=runtime)
        assert stale["available"] is False
        assert stale["last_error"] == "index_lag_exceeded"
    finally:
        runtime.close()
