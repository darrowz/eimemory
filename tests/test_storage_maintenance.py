from __future__ import annotations

from hashlib import sha256
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys

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
from eimemory.storage.atomic_file import atomic_write_json, read_json_strict


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


def _snapshot_with_empty_manifest_member(tmp_path: Path) -> tuple[Path, str]:
    db_path = tmp_path / "state" / "eimemory.sqlite"
    store = SqliteRecordStore(db_path)
    store.close()
    snapshot = tmp_path / "snapshot"
    create_consistent_storage_snapshot(
        db_path=db_path,
        segment_root=db_path.parent / "payload_segments",
        snapshot_dir=snapshot,
        offline=True,
    )
    empty = snapshot / "payload_segments" / "empty-member.bin"
    empty.write_bytes(b"")
    manifest_path = snapshot / "storage-snapshot.json"
    manifest = read_json_strict(manifest_path, dict)
    manifest["immutability"] = {"policy": "readonly_tree_v1"}
    manifest["files"].append(
        {
            "path": empty.relative_to(snapshot).as_posix(),
            "size": 0,
            "sha256": sha256(b"").hexdigest(),
        }
    )
    atomic_write_json(manifest_path, manifest)
    maintenance._seal_snapshot_tree(snapshot)
    return snapshot, _digest(manifest_path)


def test_zero_size_manifest_members_verify_and_restore_copy(tmp_path) -> None:
    snapshot, manifest_digest = _snapshot_with_empty_manifest_member(tmp_path)

    assert verify_storage_snapshot(snapshot)["ok"] is True
    assert maintenance.verify_sealed_snapshot_identity(
        snapshot,
        expected_manifest_sha256=manifest_digest,
    )["ok"] is True

    staging = tmp_path / "staging"
    shutil.copytree(snapshot, staging)
    maintenance._verify_staged_snapshot_copy(staging, snapshot)


def test_fresh_process_recovers_vacuum_after_live_database_was_moved(tmp_path) -> None:
    db_path = tmp_path / "state" / "eimemory.sqlite"
    store = SqliteRecordStore(db_path)
    store.upsert(_large_score())
    store.close()
    original_digest = _digest(db_path)
    temporary = db_path.parent / f".{db_path.name}.vacuum-{'a' * 32}"
    backup = db_path.parent / f".{db_path.name}.pre-vacuum-{'a' * 32}.bak"
    shutil.copy2(db_path, temporary)
    os.replace(db_path, backup)
    journal_path = db_path.parent / ".storage-vacuum-journal.json"
    atomic_write_json(
        journal_path,
        {
            "schema": "storage_vacuum_journal.v1",
            "status": "in_progress",
            "phase": "live_moved",
            "database": str(db_path),
            "temporary": str(temporary),
            "backup": str(backup),
            "before_sha256": original_digest,
        },
    )
    assert not db_path.exists()

    report = maintenance.recover_vacuum_journal(db_path)

    assert report["ok"] is True
    assert report["recovered"] == "rolled_back"
    assert _digest(db_path) == original_digest
    assert not journal_path.exists()
    assert not temporary.exists()


def test_fresh_process_recovers_partial_restore_from_journal(tmp_path) -> None:
    db_path = tmp_path / "state" / "eimemory.sqlite"
    store = SqliteRecordStore(db_path)
    store.upsert(_large_score())
    store.close()
    original_digest = _digest(db_path)
    token = "b" * 32
    backup = db_path.parent / f".{db_path.name}.restore-old-{token}"
    os.replace(db_path, backup)
    replacement = SqliteRecordStore(db_path)
    replacement.upsert(RecordEnvelope.create(kind="memory", title="partial restore", scope=SCOPE))
    replacement.close()
    segments = db_path.parent / "payload_segments"
    journal_path = db_path.parent / ".storage-restore-journal.json"
    atomic_write_json(
        journal_path,
        {
            "schema": "storage_restore_journal.v1",
            "snapshot_dir": str(tmp_path / "snapshot"),
            "status": "in_progress",
            "mutation_started": True,
            "entries": [
                {
                    "live": str(db_path),
                    "backup": str(backup),
                    "existed": True,
                    "state": "installed",
                },
                {
                    "live": str(segments),
                    "backup": str(db_path.parent / f".{segments.name}.restore-old-{token}"),
                    "existed": True,
                    "state": "planned",
                },
            ],
        },
    )

    report = maintenance.recover_storage_restore(
        db_path=db_path,
        segment_root=segments,
    )

    assert report["ok"] is True
    assert report["recovered"] == "rolled_back"
    assert _digest(db_path) == original_digest
    assert not journal_path.exists()


def test_restore_automatically_recovers_stale_journal_before_retry(tmp_path) -> None:
    db_path = tmp_path / "state" / "eimemory.sqlite"
    store = SqliteRecordStore(db_path)
    original = _large_score()
    store.upsert(original)
    store.close()
    snapshot = tmp_path / "snapshot"
    create_consistent_storage_snapshot(
        db_path=db_path,
        segment_root=db_path.parent / "payload_segments",
        snapshot_dir=snapshot,
        offline=True,
    )
    journal_path = db_path.parent / ".storage-restore-journal.json"
    token = "c" * 32
    atomic_write_json(
        journal_path,
        {
            "schema": "storage_restore_journal.v1",
            "snapshot_dir": str(snapshot),
            "status": "rolled_back_after_failure",
            "mutation_started": True,
            "entries": [
                {
                    "live": str(db_path),
                    "backup": str(db_path.parent / f".{db_path.name}.restore-old-{token}"),
                    "existed": True,
                    "state": "installed",
                }
            ],
        },
    )

    report = restore_storage_snapshot(
        snapshot_dir=snapshot,
        db_path=db_path,
        segment_root=db_path.parent / "payload_segments",
        offline=True,
    )

    assert report["ok"] is True
    assert not journal_path.exists()


def test_vacuum_persists_completed_swap_journal_until_cleanup(tmp_path) -> None:
    db_path = tmp_path / "state" / "eimemory.sqlite"
    store = SqliteRecordStore(db_path)
    store.upsert(_large_score())
    store.close()

    report = vacuum_into_atomic(db_path=db_path, offline=True, apply=True)

    journal_path = db_path.parent / ".storage-vacuum-journal.json"
    journal = read_json_strict(journal_path, dict)
    assert journal["schema"] == "storage_vacuum_journal.v1"
    assert journal["status"] == "complete"
    assert journal["phase"] == "complete"
    assert Path(report["backup_path"]).is_file()
    recovered = maintenance.recover_vacuum_journal(db_path)
    assert recovered["recovered"] == "complete"
    assert recovered["backup_path"] == report["backup_path"]


def test_vacuum_os_exit_after_live_move_is_recovered_by_fresh_process(tmp_path) -> None:
    db_path = tmp_path / "state" / "eimemory.sqlite"
    store = SqliteRecordStore(db_path)
    store.upsert(_large_score())
    store.close()
    original_digest = _digest(db_path)
    crash_code = r"""
import os
from pathlib import Path
import sys
import eimemory.storage.maintenance as maintenance

database = Path(sys.argv[1])
real_replace = maintenance.os.replace

def replace_then_crash(source, target):
    real_replace(source, target)
    if Path(source) == database and ".pre-vacuum-" in Path(target).name:
        os._exit(91)

maintenance.os.replace = replace_then_crash
maintenance.vacuum_into_atomic(db_path=database, offline=True, apply=True)
"""
    crashed = subprocess.run(
        [sys.executable, "-c", crash_code, str(db_path)],
        cwd=Path.cwd(),
        check=False,
    )
    assert crashed.returncode == 91
    assert not db_path.exists()
    assert (db_path.parent / ".storage-vacuum-journal.json").is_file()

    recover_code = r"""
import json
from pathlib import Path
import sys
from eimemory.storage.maintenance import recover_vacuum_journal

print(json.dumps(recover_vacuum_journal(Path(sys.argv[1]))))
"""
    recovered = subprocess.run(
        [sys.executable, "-c", recover_code, str(db_path)],
        cwd=Path.cwd(),
        check=False,
        capture_output=True,
        text=True,
    )
    assert recovered.returncode == 0, recovered.stderr
    assert json.loads(recovered.stdout)["recovered"] == "rolled_back"
    assert _digest(db_path) == original_digest


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


def test_restore_payload_validation_is_strictly_read_only(tmp_path, monkeypatch) -> None:
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
    migrated = run_storage_migrations(
        db_path=db_path,
        offline=True,
        batch_size=10,
        max_batches=20,
        snapshot_dir=snapshot,
    )
    assert migrated["ok"] is True

    original_store = maintenance.PayloadSegmentStore

    class ReadOnlyProbe:
        def __init__(self, root, *, read_only=False):
            assert read_only is True
            self.delegate = original_store(root, read_only=True)

        def read(self, pointer):
            return self.delegate.read(pointer)

    monkeypatch.setattr(maintenance, "PayloadSegmentStore", ReadOnlyProbe)
    maintenance._validate_live_payload_pointers(
        db_path,
        db_path.parent / "payload_segments",
    )


def test_restore_failure_after_mutation_rolls_back_and_preserves_journal(tmp_path, monkeypatch) -> None:
    db_path = tmp_path / "state" / "eimemory.sqlite"
    store = SqliteRecordStore(db_path, archive_writes=False)
    record = _large_score()
    store.upsert(record)
    store.close()
    snapshot = tmp_path / "snapshot"
    create_consistent_storage_snapshot(
        db_path=db_path,
        segment_root=db_path.parent / "payload_segments",
        snapshot_dir=snapshot,
        offline=True,
    )
    migrated = run_storage_migrations(
        db_path=db_path,
        offline=True,
        batch_size=10,
        max_batches=20,
        snapshot_dir=snapshot,
    )
    assert migrated["ok"] is True
    migrated_digest = _digest(db_path)

    monkeypatch.setattr(
        maintenance,
        "_validate_live_payload_pointers",
        lambda *_args: (_ for _ in ()).throw(StorageMaintenanceError("injected validation failure")),
    )
    with pytest.raises(StorageMaintenanceError, match="injected validation failure"):
        restore_storage_snapshot(
            snapshot_dir=snapshot,
            db_path=db_path,
            segment_root=db_path.parent / "payload_segments",
            offline=True,
        )

    assert _digest(db_path) == migrated_digest
    journal = json.loads(
        (db_path.parent / ".storage-restore-journal.json").read_text(encoding="utf-8")
    )
    assert journal["schema"] == "storage_restore_journal.v1"
    assert journal["status"] == "rolled_back_after_failure"
    assert journal["mutation_started"] is True
