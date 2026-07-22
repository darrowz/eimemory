from __future__ import annotations

from hashlib import sha256
import json
from pathlib import Path
import sqlite3

import pytest

import eimemory.storage.maintenance as maintenance
from eimemory.models.records import RecordEnvelope, ScopeRef
from eimemory.storage.maintenance import (
    StorageMaintenanceError,
    create_consistent_storage_snapshot,
    preflight_storage_maintenance,
    restore_storage_snapshot,
    run_storage_migrations,
    vacuum_into_atomic,
    verify_storage_snapshot,
)
from eimemory.storage.sqlite_store import SqliteRecordStore


SCOPE = ScopeRef(tenant_id="tenant", agent_id="agent", workspace_id="workspace", user_id="user")


def _large_score(index: int = 0) -> RecordEnvelope:
    return RecordEnvelope.create(
        kind="capability_score",
        title=f"score {index}",
        summary="legacy full payload",
        scope=SCOPE,
        source="eimemory.capability_ledger",
        content={
            "capability": "memory.recall",
            "score": 0.9,
            "report": {"samples": ["full-body-" + ("x" * 200_000)]},
        },
        meta={"capability": "memory.recall", "score": 0.9},
    )


def _digest(path: Path) -> str:
    return sha256(path.read_bytes()).hexdigest()


def test_low_disk_preflight_fails_closed_before_snapshot_or_vacuum(tmp_path, monkeypatch) -> None:
    db_path = tmp_path / "state" / "eimemory.sqlite"
    store = SqliteRecordStore(db_path, archive_writes=False)
    store.upsert(_large_score())
    store.close()

    monkeypatch.setattr(
        maintenance.shutil,
        "disk_usage",
        lambda _path: type("Usage", (), {"total": 100, "used": 99, "free": 1})(),
    )
    report = preflight_storage_maintenance(
        db_path=db_path,
        segment_root=db_path.parent / "payload_segments",
        include_snapshot=True,
        include_vacuum=True,
    )

    assert report["ok"] is False
    with pytest.raises(StorageMaintenanceError, match="free disk"):
        create_consistent_storage_snapshot(
            db_path=db_path,
            segment_root=db_path.parent / "payload_segments",
            snapshot_dir=tmp_path / "snapshot",
            offline=True,
        )
    assert not (tmp_path / "snapshot").exists()


def test_snapshot_migration_failure_restore_remains_readable_by_legacy_store(tmp_path) -> None:
    db_path = tmp_path / "state" / "eimemory.sqlite"
    store = SqliteRecordStore(db_path, archive_writes=False)
    record = _large_score()
    store.upsert(record)
    store.close()
    snapshot = tmp_path / "snapshots" / "candidate"

    created = create_consistent_storage_snapshot(
        db_path=db_path,
        segment_root=db_path.parent / "payload_segments",
        snapshot_dir=snapshot,
        offline=True,
    )
    assert created["ok"] is True
    assert verify_storage_snapshot(snapshot)["ok"] is True

    changed = SqliteRecordStore(db_path)
    changed.conn.execute(
        "DELETE FROM schema_migrations WHERE migration_id='records.payload_archive.v1'"
    )
    changed.conn.commit()
    while not changed.payload_archival_complete():
        changed.apply_payload_archival_batch(batch_size=1, hot_window=0)
    assert changed.conn.execute(
        "SELECT payload_pointer_json FROM records WHERE record_id=?", (record.record_id,)
    ).fetchone()[0]
    changed.close()

    restored = restore_storage_snapshot(
        snapshot_dir=snapshot,
        db_path=db_path,
        segment_root=db_path.parent / "payload_segments",
        offline=True,
    )
    assert restored["ok"] is True

    # Simulate 1.9.80: it only knows payload_json and has no lazy hydration.
    legacy = sqlite3.connect(db_path)
    payload = json.loads(legacy.execute(
        "SELECT payload_json FROM records WHERE record_id=?", (record.record_id,)
    ).fetchone()[0])
    legacy.close()
    assert RecordEnvelope.from_dict(payload).content == record.content
    reopened = SqliteRecordStore(db_path)
    reopened.upsert(
        RecordEnvelope.create(kind="memory", title="post-restore write", scope=SCOPE)
    )
    assert reopened.conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
    reopened.close()


def test_interrupted_keyset_migration_resumes_from_persisted_cursor(tmp_path) -> None:
    db_path = tmp_path / "state" / "eimemory.sqlite"
    store = SqliteRecordStore(db_path, archive_writes=False)
    for index in range(5):
        store.upsert(_large_score(index))
    store.close()
    snapshot = tmp_path / "snapshot"
    create_consistent_storage_snapshot(
        db_path=db_path,
        segment_root=db_path.parent / "payload_segments",
        snapshot_dir=snapshot,
        offline=True,
    )

    first = run_storage_migrations(
        db_path=db_path,
        offline=True,
        batch_size=1,
        max_batches=1,
        snapshot_dir=snapshot,
    )
    assert first["ok"] is False
    assert first["reason"] == "max_batches_exceeded"

    resumed = run_storage_migrations(
        db_path=db_path,
        offline=True,
        batch_size=1,
        max_batches=30,
        snapshot_dir=snapshot,
    )
    assert resumed["ok"] is True
    assert resumed["pending"] == []


def test_vacuum_is_dry_run_by_default_and_rolls_back_failed_reopen(tmp_path, monkeypatch) -> None:
    db_path = tmp_path / "state" / "eimemory.sqlite"
    store = SqliteRecordStore(db_path)
    store.upsert(_large_score())
    store.close()
    before = _digest(db_path)

    plan = vacuum_into_atomic(db_path=db_path, offline=True)
    assert plan["applied"] is False
    assert _digest(db_path) == before

    original_validate = maintenance._validate_sqlite_database
    validations = 0

    def fail_after_replace(path):
        nonlocal validations
        validations += 1
        if validations >= 2 and Path(path) == db_path:
            raise StorageMaintenanceError("simulated post-replace validation failure")
        return original_validate(path)

    monkeypatch.setattr(maintenance, "_validate_sqlite_database", fail_after_replace)
    with pytest.raises(StorageMaintenanceError, match="post-replace"):
        vacuum_into_atomic(db_path=db_path, offline=True, apply=True)

    assert _digest(db_path) == before
    assert original_validate(db_path)["integrity_check"] == "ok"


def test_old_schema_without_pointer_column_is_included_in_archive_disk_estimate(tmp_path) -> None:
    db_path = tmp_path / "state" / "legacy.sqlite"
    db_path.parent.mkdir(parents=True)
    connection = sqlite3.connect(db_path)
    connection.execute("CREATE TABLE records(kind TEXT,payload_json TEXT)")
    connection.execute(
        "INSERT INTO records(kind,payload_json) VALUES('capability_score',?)",
        ("x" * 4096,),
    )
    connection.commit()
    connection.close()

    report = preflight_storage_maintenance(
        db_path=db_path,
        segment_root=db_path.parent / "payload_segments",
        include_snapshot=False,
        include_vacuum=False,
    )

    assert report["archive_estimate_bytes"] >= 4096


def test_active_writer_blocks_snapshot_migrate_vacuum_and_restore_without_mutation(tmp_path) -> None:
    db_path = tmp_path / "state" / "eimemory.sqlite"
    store = SqliteRecordStore(db_path, archive_writes=False)
    store.upsert(_large_score())
    store.close()
    snapshot = tmp_path / "snapshot"
    create_consistent_storage_snapshot(
        db_path=db_path,
        segment_root=db_path.parent / "payload_segments",
        snapshot_dir=snapshot,
        offline=True,
    )
    before = _digest(db_path)
    writer = sqlite3.connect(db_path)
    writer.execute("BEGIN IMMEDIATE")
    writer.execute(
        "UPDATE records SET summary='uncommitted writer' "
        "WHERE storage_key=(SELECT storage_key FROM records LIMIT 1)"
    )
    try:
        operations = (
            lambda: create_consistent_storage_snapshot(
                db_path=db_path,
                segment_root=db_path.parent / "payload_segments",
                snapshot_dir=tmp_path / "blocked-snapshot",
                offline=True,
            ),
            lambda: run_storage_migrations(
                db_path=db_path,
                offline=True,
                batch_size=1,
                max_batches=1,
                snapshot_dir=snapshot,
            ),
            lambda: vacuum_into_atomic(db_path=db_path, offline=True, apply=True),
            lambda: restore_storage_snapshot(
                snapshot_dir=snapshot,
                db_path=db_path,
                segment_root=db_path.parent / "payload_segments",
                offline=True,
            ),
        )
        for operation in operations:
            with pytest.raises(StorageMaintenanceError, match="writer|offline|lock"):
                operation()
    finally:
        writer.rollback()
        writer.close()
    assert _digest(db_path) == before
    assert not (tmp_path / "blocked-snapshot").exists()


def test_restore_staging_failure_never_touches_live_database_or_segments(tmp_path, monkeypatch) -> None:
    db_path = tmp_path / "state" / "eimemory.sqlite"
    store = SqliteRecordStore(db_path, archive_writes=False)
    store.upsert(_large_score())
    store.close()
    snapshot = tmp_path / "snapshot"
    create_consistent_storage_snapshot(
        db_path=db_path,
        segment_root=db_path.parent / "payload_segments",
        snapshot_dir=snapshot,
        offline=True,
    )
    before_db = _digest(db_path)
    before_segments = {
        path.relative_to(db_path.parent / "payload_segments").as_posix(): _digest(path)
        for path in (db_path.parent / "payload_segments").rglob("*")
        if path.is_file()
    }
    monkeypatch.setattr(
        maintenance,
        "_copy_tree",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(StorageMaintenanceError("copy failed")),
    )

    with pytest.raises(StorageMaintenanceError, match="copy failed"):
        restore_storage_snapshot(
            snapshot_dir=snapshot,
            db_path=db_path,
            segment_root=db_path.parent / "payload_segments",
            offline=True,
        )

    assert _digest(db_path) == before_db
    assert {
        path.relative_to(db_path.parent / "payload_segments").as_posix(): _digest(path)
        for path in (db_path.parent / "payload_segments").rglob("*")
        if path.is_file()
    } == before_segments
