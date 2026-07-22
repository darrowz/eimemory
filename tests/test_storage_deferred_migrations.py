from __future__ import annotations

import json
import sqlite3

from eimemory.models.records import RecordEnvelope, ScopeRef
from eimemory.api.runtime import Runtime
from eimemory.adapters.eibrain.rpc_server import build_health_payload
from eimemory.storage.runtime_store import RuntimeStore
from eimemory.storage.sqlite_store import SqliteRecordStore


SCOPE = ScopeRef(tenant_id="default", agent_id="agent", workspace_id="workspace", user_id="")


def _legacy_database(path, *, rows: int = 32) -> list[RecordEnvelope]:
    connection = sqlite3.connect(path)
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
        INSERT INTO schema_migrations VALUES
          ('storage.schema.v1', '2026-01-01T00:00:00Z'),
          ('records.meta_keys.v1', '2026-01-01T00:00:00Z'),
          ('intent_patterns.payload_status.v1', '2026-01-01T00:00:00Z');
        """
    )
    records = []
    for index in range(rows):
        record = RecordEnvelope.create(
            kind="knowledge_page",
            title=f"legacy {index}",
            content={"text": f"canary-{index}", "source_ids": ["paper-a"]},
            scope=SCOPE,
            source_id="paper-a",
            aliases=[f"alias {index}"],
        )
        records.append(record)
        storage_key = "\x1f".join(["default", "agent", "workspace", "", record.record_id])
        payload = json.dumps(record.to_dict(), ensure_ascii=False)
        connection.execute(
            "INSERT INTO records VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                storage_key, record.record_id, record.kind, record.status, record.title,
                record.summary, record.detail, f"canary-{index}", record.source,
                "agent", "workspace", "", "default", "[]", "", "", "{}", payload,
                record.time.created_at, record.time.updated_at,
            ),
        )
        connection.execute(
            "INSERT INTO recall_index VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                storage_key, record.record_id, record.kind, record.status, record.source,
                "default", "agent", "workspace", "", "knowledge", "eligible", "trusted",
                "external_knowledge", "full", 0.8, record.title, f"canary-{index}",
                "legacy", record.time.updated_at,
            ),
        )
    connection.commit()
    connection.close()
    return records


def test_legacy_source_and_identity_migrations_do_not_scan_payload_during_startup(
    tmp_path, monkeypatch
) -> None:
    db_path = tmp_path / "legacy.sqlite"
    records = _legacy_database(db_path)

    def fail_if_decoded(_self, _payload):
        raise AssertionError("startup decoded a historical payload")

    monkeypatch.setattr(SqliteRecordStore, "_payload_dict_from_json", fail_if_decoded)
    assert not hasattr(SqliteRecordStore, "_migrate_source_partition_schema")
    assert not hasattr(SqliteRecordStore, "_migrate_recall_identity_schema")
    assert not hasattr(SqliteRecordStore, "_backfill_recall_index_if_needed")
    store = SqliteRecordStore(db_path)

    assert store.conn.execute("SELECT source_id FROM records LIMIT 1").fetchone()[0] == "default"
    assert store.conn.execute("SELECT source_id FROM recall_index LIMIT 1").fetchone()[0] == "default"
    assert store.conn.execute("SELECT COUNT(*) FROM records").fetchone()[0] == len(records)
    assert "records.source_partition.v1" in store.pending_storage_migrations()
    assert "recall.identity_index.v1" in store.pending_storage_migrations()
    assert "canary-0" in store.conn.execute(
        "SELECT payload_json FROM records WHERE title='legacy 0'"
    ).fetchone()[0]
    store.close()


def test_deferred_source_migration_is_keyset_bounded_and_never_rewrites_payload(tmp_path) -> None:
    db_path = tmp_path / "legacy.sqlite"
    _legacy_database(db_path, rows=5)
    store = SqliteRecordStore(db_path)
    before = {
        row[0]: row[1]
        for row in store.conn.execute("SELECT storage_key,payload_json FROM records")
    }

    reports = []
    while not store._schema_migration_applied("records.source_partition.v1"):
        reports.append(store.apply_storage_migrations(batch_size=2))
    reports.append(store.apply_storage_migrations(batch_size=2, offline=True))

    assert reports
    assert all(report["processed"] <= 2 for report in reports)
    assert {row[0]: row[1] for row in store.conn.execute(
        "SELECT storage_key,payload_json FROM records"
    )} == before
    assert {row[0] for row in store.conn.execute("SELECT DISTINCT source_id FROM records")} == {
        "paper-a"
    }
    store.close()


def test_partition_and_identity_migration_never_decode_large_non_recall_reports(
    tmp_path, monkeypatch
) -> None:
    db_path = tmp_path / "legacy.sqlite"
    _legacy_database(db_path, rows=3)
    connection = sqlite3.connect(db_path)
    sentinel = "CAPABILITY-SENTINEL-" + ("x" * 2_000_000)
    record = RecordEnvelope.create(
        kind="capability_score",
        title="large report",
        content={"report": sentinel},
        scope=SCOPE,
        source_id="default",
    )
    storage_key = "\x1f".join(["default", "agent", "workspace", "", record.record_id])
    connection.execute(
        "INSERT INTO records VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            storage_key, record.record_id, record.kind, record.status, record.title,
            record.summary, record.detail, "", record.source, "agent", "workspace", "",
            "default", "[]", "", "", "{}", json.dumps(record.to_dict()),
            record.time.created_at, record.time.updated_at,
        ),
    )
    connection.commit()
    connection.close()
    store = SqliteRecordStore(db_path)
    original = SqliteRecordStore._record_from_payload_json

    def reject_large_report(self, payload_json):
        assert "CAPABILITY-SENTINEL" not in str(payload_json)
        return original(self, payload_json)

    monkeypatch.setattr(SqliteRecordStore, "_record_from_payload_json", reject_large_report)
    for _ in range(32):
        report = store.apply_storage_migrations(batch_size=1)
        if report.get("offline_required"):
            store.apply_storage_migrations(batch_size=1, offline=True)
        if (
            store._schema_migration_applied("records.source_partition.v1")
            and store._schema_migration_applied("recall.identity_index.v1")
        ):
            break

    assert store.source_partition_migration_diagnostics.get("corrupt", 0) == 0
    assert store._schema_migration_applied("recall.identity_index.v1")
    store.close()


def test_health_reports_pending_storage_migrations_without_marking_store_unavailable(tmp_path) -> None:
    db_path = tmp_path / "state" / "eimemory.sqlite"
    db_path.parent.mkdir(parents=True)
    _legacy_database(db_path, rows=2)
    runtime = Runtime(RuntimeStore(tmp_path))

    health = build_health_payload(runtime, listen_host="127.0.0.1", listen_port=8091)

    assert health["ok"] is True
    assert health["store"]["ready"] is True
    assert "records.source_partition.v1" in health["store"]["pending_migrations"]
    assert health["store"]["migration_complete"] is False
    runtime.close()
