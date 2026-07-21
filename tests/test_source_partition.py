from __future__ import annotations

import sqlite3
import json

import pytest

from eimemory.models.records import RecordEnvelope, ScopeRef
from eimemory.storage.runtime_store import RuntimeStore
from eimemory.storage.sqlite_store import SqliteRecordStore


SCOPE = ScopeRef(agent_id="agent-a", workspace_id="workspace-a")


def _record(*, source_id: str = "default", title: str = "Shared title") -> RecordEnvelope:
    return RecordEnvelope.create(
        kind="memory",
        title=title,
        summary="partitioned searchable memory",
        scope=SCOPE,
        source_id=source_id,
    )


def test_envelope_normalizes_and_round_trips_top_level_source_id() -> None:
    record = _record(source_id="\uff34\uff25\uff21\uff2d-A")

    assert record.source_id == "team-a"
    assert RecordEnvelope.from_dict(record.to_dict()).source_id == "team-a"
    legacy_payload = record.to_dict()
    legacy_payload.pop("source_id")
    assert RecordEnvelope.from_dict(legacy_payload).source_id == "default"


@pytest.mark.parametrize("source_id", [" ", "team/a", "a" * 129])
def test_envelope_rejects_invalid_explicit_source_ids(source_id: str) -> None:
    with pytest.raises(ValueError, match="source_id"):
        _record(source_id=source_id)


def test_source_allowlist_rejects_normalization_collisions(tmp_path) -> None:
    store = RuntimeStore(tmp_path)
    store.append(_record(source_id="team-a"))

    with pytest.raises(ValueError, match="collision"):
        store.list_records(scope=SCOPE, source_ids=["TEAM-A", "team-a"])
    with pytest.raises(ValueError, match="allowlist"):
        store.list_records(scope=SCOPE, source_ids="team-a")  # type: ignore[arg-type]


def test_new_schema_and_crud_rewrite_project_source_id_and_filter_every_path(tmp_path) -> None:
    store = RuntimeStore(tmp_path)
    alpha = store.append(_record(source_id="alpha", title="alpha indexed secret"))
    beta = store.append(_record(source_id="beta", title="beta indexed secret"))

    assert {row["name"] for row in store.sqlite.conn.execute("PRAGMA table_info(records)")} >= {"source_id"}
    assert {row["name"] for row in store.sqlite.conn.execute("PRAGMA table_info(recall_index)")} >= {"source_id"}
    assert [record.record_id for record in store.list_records(scope=SCOPE, source_ids=["alpha"])] == [alpha.record_id]
    assert store.count_records(scope=SCOPE, source_ids=["alpha"]) == 1
    assert store.list_records(scope=SCOPE, source_ids=[]) == []
    assert store.search(query="indexed secret", scope=SCOPE, source_ids=["alpha"])[0].record_id == alpha.record_id
    assert store.search(query="indexed secret", scope=SCOPE, source_ids=[] ) == []

    alpha.summary = "rewritten alpha memory"
    store.rewrite(alpha)
    assert store.get_by_id(alpha.record_id, scope=SCOPE).source_id == "alpha"
    assert store.get_by_id(beta.record_id, scope=SCOPE).source_id == "beta"


def test_runtime_store_legacy_candidate_fallback_is_source_fail_closed(tmp_path) -> None:
    store = RuntimeStore(tmp_path)
    alpha = store.append(_record(source_id="alpha", title="legacy fallback alpha marker"))
    store.append(_record(source_id="beta", title="legacy fallback beta marker"))
    store.sqlite.conn.execute("DELETE FROM recall_index")
    if store.sqlite._has_fts_table():
        store.sqlite.conn.execute("DELETE FROM recall_index_fts")
    store.sqlite.conn.commit()

    records, diagnostics = store.search_with_diagnostics(
        query="legacy fallback marker", scope=SCOPE, source_ids=["alpha"]
    )
    assert [record.record_id for record in records] == [alpha.record_id]
    assert diagnostics["candidate_fallback"] == "legacy_scan"
    assert store.search_with_diagnostics(query="legacy fallback marker", scope=SCOPE, source_ids=[])[0] == []


def test_ready_legacy_database_migrates_only_unambiguous_knowledge_page_source_ids(tmp_path) -> None:
    db_path = tmp_path / "legacy.sqlite"
    connection = sqlite3.connect(db_path)
    connection.executescript(
        """
        CREATE TABLE records (
          storage_key TEXT PRIMARY KEY, record_id TEXT NOT NULL, kind TEXT NOT NULL,
          status TEXT NOT NULL, title TEXT NOT NULL, summary TEXT NOT NULL, detail TEXT NOT NULL,
          content_text TEXT NOT NULL, source TEXT NOT NULL, agent_id TEXT NOT NULL,
          workspace_id TEXT NOT NULL, user_id TEXT NOT NULL, tenant_id TEXT NOT NULL,
          embedding_json TEXT NOT NULL DEFAULT '[]', idempotency_key TEXT NOT NULL DEFAULT '',
          semantic_key TEXT NOT NULL DEFAULT '', meta_json TEXT NOT NULL, payload_json TEXT NOT NULL,
          created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        );
        CREATE TABLE schema_migrations (migration_id TEXT PRIMARY KEY, applied_at TEXT NOT NULL);
        CREATE TABLE recall_index (
          storage_key TEXT PRIMARY KEY, record_id TEXT NOT NULL, kind TEXT NOT NULL,
          status TEXT NOT NULL, source TEXT NOT NULL, tenant_id TEXT NOT NULL,
          agent_id TEXT NOT NULL, workspace_id TEXT NOT NULL, user_id TEXT NOT NULL,
          lane TEXT NOT NULL, visibility TEXT NOT NULL, source_class TEXT NOT NULL,
          memory_type TEXT NOT NULL, projection_type TEXT NOT NULL,
          quality_score REAL NOT NULL DEFAULT 0.0, title_text TEXT NOT NULL,
          body_text TEXT NOT NULL, anchor_terms TEXT NOT NULL, updated_at TEXT NOT NULL
        );
        CREATE VIRTUAL TABLE recall_index_fts
          USING fts5(storage_key UNINDEXED, title_text, body_text, anchor_terms, tokenize='unicode61');
        INSERT INTO schema_migrations VALUES
          ('storage.schema.v1', '2026-01-01T00:00:00Z'),
          ('records.meta_keys.v1', '2026-01-01T00:00:00Z'),
          ('intent_patterns.payload_status.v1', '2026-01-01T00:00:00Z');
        """
    )
    good = _record(title="legacy good")
    good.kind = "knowledge_page"
    good.content = {"source_ids": ["Paper-A"]}
    good.meta = {"source_ids": ["paper-a"]}
    ambiguous = _record(title="legacy ambiguous")
    ambiguous.kind = "knowledge_page"
    ambiguous.content = {"source_ids": ["Paper-A"]}
    ambiguous.provenance = {"source_ids": ["Paper-B"]}
    paper_only = _record(title="legacy paper source is provenance")
    paper_only.kind = "knowledge_page"
    paper_only.provenance = {"paper_source_id": "Paper-A"}
    non_page = _record(title="legacy provenance is not a partition")
    non_page.content = {"source_ids": ["not-a-partition"]}
    for record in (good, ambiguous, paper_only, non_page):
        payload = record.to_dict()
        connection.execute(
            "INSERT INTO records VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("\x1f".join(["default", SCOPE.agent_id, SCOPE.workspace_id, "", record.record_id]), record.record_id,
             record.kind, record.status, record.title, record.summary, record.detail, record.summary, record.source,
             SCOPE.agent_id, SCOPE.workspace_id, "", "default", "[]", "", "", "{}", json.dumps(payload),
             record.time.created_at, record.time.updated_at),
        )
    connection.commit()
    connection.close()

    migrated = SqliteRecordStore(db_path)

    assert migrated.get_by_id(good.record_id, scope=SCOPE).source_id == "paper-a"
    assert migrated.get_by_id(ambiguous.record_id, scope=SCOPE).source_id == "default"
    assert migrated.get_by_id(paper_only.record_id, scope=SCOPE).source_id == "default"
    assert migrated.get_by_id(non_page.record_id, scope=SCOPE).source_id == "default"
    assert migrated.conn.execute("SELECT 1 FROM schema_migrations WHERE migration_id = 'records.source_partition.v1'").fetchone()


def test_source_filters_use_covering_indexes(tmp_path) -> None:
    store = RuntimeStore(tmp_path)
    store.append(_record(source_id="alpha", title="plan probe"))
    plan = store.sqlite.conn.execute(
        "EXPLAIN QUERY PLAN SELECT storage_key FROM records "
        "WHERE status != 'rejected' AND tenant_id = ? AND agent_id = ? AND workspace_id = ? AND user_id = ? AND source_id IN (?) "
        "ORDER BY updated_at DESC, record_id DESC LIMIT 10",
        ("default", SCOPE.agent_id, SCOPE.workspace_id, "", "alpha"),
    ).fetchall()
    assert any("COVERING INDEX idx_records_scope_source_updated" in row[3] for row in plan)
    recall_filters = {"_source_ids": ("alpha",), "blocked_recall_lanes": ["operational"]}
    where, params = store.sqlite._recall_index_where(
        kinds=None, scope=SCOPE, recall_filters=recall_filters, alias="i"
    )
    recall_plan = store.sqlite.conn.execute(
        "EXPLAIN QUERY PLAN SELECT i.storage_key, i.quality_score, i.updated_at FROM recall_index i WHERE "
        + " AND ".join(where)
        + " ORDER BY i.updated_at DESC LIMIT 10",
        params,
    ).fetchall()
    assert any("COVERING INDEX idx_recall_index_scope_source_updated" in row[3] for row in recall_plan)


def test_markered_schema_repairs_missing_source_index_before_ready_fast_path(tmp_path) -> None:
    store = RuntimeStore(tmp_path)
    store.sqlite.conn.execute("DROP INDEX idx_recall_index_scope_source_updated")
    store.sqlite.conn.commit()
    store.sqlite.close()

    repaired = SqliteRecordStore(tmp_path / "state" / "eimemory.sqlite")
    assert repaired._source_partition_physical_ready() is True


@pytest.mark.parametrize(
    ("index_name", "sql"),
    [
        (
            "idx_records_scope_source_updated",
            "CREATE INDEX idx_records_scope_source_updated ON records(tenant_id, agent_id, workspace_id, user_id, updated_at DESC, record_id DESC, status, storage_key)",
        ),
        (
            "idx_recall_index_scope_source_updated",
            "CREATE INDEX idx_recall_index_scope_source_updated ON recall_index(tenant_id, agent_id, workspace_id, user_id, source_id, status, updated_at DESC, lane, visibility, storage_key)",
        ),
    ],
)
def test_markered_schema_repairs_same_name_corrupt_source_index(tmp_path, index_name: str, sql: str) -> None:
    store = RuntimeStore(tmp_path)
    store.append(_record(source_id="alpha", title="corrupt index alpha"))
    store.sqlite.close()
    connection = sqlite3.connect(tmp_path / "state" / "eimemory.sqlite")
    connection.execute(f"DROP INDEX {index_name}")
    connection.execute(sql)
    connection.commit()
    connection.close()

    repaired = SqliteRecordStore(tmp_path / "state" / "eimemory.sqlite")
    assert repaired._source_partition_physical_ready() is True
    where, params = repaired._recall_index_where(
        kinds=None, scope=SCOPE, recall_filters={"_source_ids": ("alpha",), "blocked_recall_lanes": ["operational"]}, alias="i"
    )
    plan = repaired.conn.execute(
        "EXPLAIN QUERY PLAN SELECT i.storage_key, i.quality_score, i.updated_at FROM recall_index i WHERE "
        + " AND ".join(where) + " ORDER BY i.updated_at DESC LIMIT 10", params
    ).fetchall()
    assert any("COVERING INDEX idx_recall_index_scope_source_updated" in row[3] for row in plan)


@pytest.mark.parametrize("repair", ["records_index", "recall_index", "recall_column", "records_column"])
def test_markered_nonempty_database_repairs_physical_source_schema_without_remapping_alpha(tmp_path, repair: str) -> None:
    root = tmp_path / repair
    store = RuntimeStore(root)
    alpha = store.append(_record(source_id="alpha", title="marked alpha"))
    store.sqlite.close()
    connection = sqlite3.connect(root / "state" / "eimemory.sqlite")
    if repair == "records_index":
        connection.execute("DROP INDEX idx_records_scope_source_updated")
    elif repair == "recall_index":
        connection.execute("DROP INDEX idx_recall_index_scope_source_updated")
    elif repair == "recall_column":
        connection.execute("ALTER TABLE recall_index RENAME COLUMN source_id TO legacy_source_id")
    else:
        connection.execute("ALTER TABLE records RENAME COLUMN source_id TO legacy_source_id")
    connection.commit()
    connection.close()

    repaired = SqliteRecordStore(root / "state" / "eimemory.sqlite")
    assert repaired.get_by_id(alpha.record_id, scope=SCOPE).source_id == "alpha"
    assert repaired.conn.execute("SELECT source_id FROM records WHERE record_id = ?", (alpha.record_id,)).fetchone()[0] == "alpha"
    assert repaired.conn.execute("SELECT source_id FROM recall_index WHERE record_id = ?", (alpha.record_id,)).fetchone()[0] == "alpha"


def test_sqlite_upsert_rejects_same_identity_source_partition_move(tmp_path) -> None:
    store = RuntimeStore(tmp_path)
    record = store.append(_record(source_id="alpha"))
    record.source_id = "beta"
    with pytest.raises(ValueError, match="source_id move"):
        store.sqlite.upsert(record)


def test_rewrite_rejects_source_partition_move_across_scope(tmp_path) -> None:
    store = RuntimeStore(tmp_path)
    record = store.append(_record(source_id="alpha"))
    prior_scope = record.scope
    record.scope = ScopeRef(agent_id="agent-b", workspace_id=prior_scope.workspace_id)
    record.source_id = "beta"
    with pytest.raises(ValueError, match="source_id move"):
        store.rewrite(record, previous_scope=prior_scope)


def test_evaluation_framework_seed_preserves_explicit_source_partition(tmp_path) -> None:
    from eimemory.api.runtime import Runtime
    from eimemory.evaluation.framework import run_evaluation

    runtime = Runtime.create(root=tmp_path)
    report = run_evaluation(
        runtime,
        {"scope": {"agent_id": "agent-a"}, "seed": [{"text": "evaluation alpha", "source_id": "alpha"}]},
    )
    seeded = runtime.store.get_by_id(report["seeded_record_ids"][0], scope={"agent_id": "agent-a"})
    assert seeded.source_id == "alpha"


def test_active_intake_snapshot_preserves_source_partition(tmp_path) -> None:
    from eimemory.api.runtime import Runtime
    from eimemory.governance.snapshot import build_governance_snapshot

    runtime = Runtime.create(root=tmp_path)
    scope = ScopeRef(agent_id="agent-a")
    runtime.store.append(RecordEnvelope.create(kind="paper_source", title="snapshot alpha", scope=scope, source_id="alpha"))
    runtime.store.append(RecordEnvelope.create(kind="knowledge_candidate", title="candidate alpha", scope=scope, source_id="alpha"))
    runtime.store.append(RecordEnvelope.create(kind="knowledge_page", title="page alpha", scope=scope, source_id="alpha"))
    runtime.store.append(
        RecordEnvelope.create(
            kind="memory", title="projected alpha", scope=scope, source_id="alpha", meta={"projection_type": "operational_knowledge"}
        )
    )
    snapshot = build_governance_snapshot(runtime, scope)
    assert snapshot["active_intake"]["recent_paper_sources"][0]["source_id"] == "alpha"
    assert snapshot["active_intake"]["recent_candidates"][0]["source_id"] == "alpha"
    assert snapshot["active_intake"]["recent_knowledge_pages"][0]["source_id"] == "alpha"
    assert snapshot["active_intake"]["operational_projection"]["recent_projected_memories"][0]["source_id"] == "alpha"


@pytest.mark.parametrize("source_id", ["", 7])
def test_evaluation_seed_rejects_explicit_invalid_source_partition(tmp_path, source_id: object) -> None:
    from eimemory.api.runtime import Runtime
    from eimemory.evaluation.framework import run_evaluation

    report = run_evaluation(
        Runtime.create(root=tmp_path),
        {"seed": [{"text": "invalid fixture", "source_id": source_id}]},
    )
    assert report["seeded_record_ids"] == []
    assert report["seed_error_count"] == 1


def test_replay_replace_and_channel_scope_do_not_leak_source_partition(tmp_path) -> None:
    store = RuntimeStore(tmp_path)
    local = store.append(_record(source_id="alpha", title="channel-local alpha"))
    other_scope = ScopeRef(agent_id="agent-b", workspace_id="workspace-a")
    other = RecordEnvelope.create(
        kind="memory", title="other-channel alpha", summary="channel-local alpha", scope=other_scope, source_id="alpha"
    )
    store.append(other)

    assert [item.record_id for item in store.search(query="channel-local alpha", scope=SCOPE, source_ids=["alpha"])] == [local.record_id]
    assert store.list_records(scope=SCOPE, source_ids=["beta"]) == []
    assert store.rebuild_sqlite_from_jsonl(replace=True)["ok"] is True
    assert store.get_by_id(local.record_id, scope=SCOPE).source_id == "alpha"
    assert store.get_by_id(other.record_id, scope=other_scope).source_id == "alpha"


def test_memory_service_accepts_only_explicit_validated_top_level_source_id(tmp_path) -> None:
    from eimemory.api.memory import MemoryAPI

    memory = MemoryAPI(RuntimeStore(tmp_path))
    record = memory.ingest(
        text="service partition", memory_type="task_context", title="explicit source", scope={"agent_id": "agent-a"}, source_id="\uff34\uff25\uff21\uff2d-A"
    )
    assert record.source_id == "team-a"
    with pytest.raises(ValueError, match="source_id"):
        memory.ingest(
            text="invalid", memory_type="task_context", title="invalid source", scope={"agent_id": "agent-a"}, source_id="bridge/source"
        )


def test_source_migration_rolls_back_records_and_recall_projection_on_failure(tmp_path, monkeypatch) -> None:
    db_path = tmp_path / "rollback.sqlite"
    legacy = SqliteRecordStore(db_path)
    legacy.conn.execute("DELETE FROM schema_migrations WHERE migration_id = 'records.source_partition.v1'")
    legacy.conn.execute("ALTER TABLE records RENAME COLUMN source_id TO legacy_source_id")
    legacy.conn.execute("ALTER TABLE recall_index RENAME COLUMN source_id TO legacy_source_id")
    legacy.conn.commit()
    legacy.close()

    original = SqliteRecordStore._backfill_recall_index_if_needed
    monkeypatch.setattr(SqliteRecordStore, "_backfill_recall_index_if_needed", lambda _self: (_ for _ in ()).throw(RuntimeError("inject rollback")))
    with pytest.raises(RuntimeError, match="inject rollback"):
        SqliteRecordStore(db_path)
    monkeypatch.setattr(SqliteRecordStore, "_backfill_recall_index_if_needed", original)
    connection = sqlite3.connect(db_path)
    assert "source_id" not in {row[1] for row in connection.execute("PRAGMA table_info(records)")}
    assert "source_id" not in {row[1] for row in connection.execute("PRAGMA table_info(recall_index)")}
    assert not connection.execute("SELECT 1 FROM schema_migrations WHERE migration_id = 'records.source_partition.v1'").fetchone()
    connection.close()


def test_source_allowlist_is_bounded() -> None:
    from eimemory.models.source_partitions import normalize_source_ids

    with pytest.raises(ValueError, match="64"):
        normalize_source_ids([f"source-{index}" for index in range(65)])


def test_reflection_dedupe_does_not_merge_distinct_source_partitions(tmp_path) -> None:
    store = RuntimeStore(tmp_path)
    alpha = RecordEnvelope.create(kind="reflection", title="same", summary="same reflection", scope=SCOPE, source_id="alpha")
    beta = RecordEnvelope.create(kind="reflection", title="same", summary="same reflection", scope=SCOPE, source_id="beta")
    assert store.append(alpha).record_id == alpha.record_id
    assert store.append(beta).record_id == beta.record_id
    assert {record.source_id for record in store.list_records(kinds=["reflection"], scope=SCOPE)} == {"alpha", "beta"}


def test_rewrite_rejects_implicit_same_scope_source_partition_move(tmp_path) -> None:
    store = RuntimeStore(tmp_path)
    record = store.append(_record(source_id="alpha"))
    record.source_id = "beta"
    with pytest.raises(ValueError, match="source_id move"):
        store.rewrite(record)
