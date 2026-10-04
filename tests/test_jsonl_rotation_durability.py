"""Durability ordering, real fsync/inode coverage and temporary fault injection.

These are syscall/exception-boundary tests, not a claim of power-loss validation.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import stat

import pytest

from eimemory.storage import jsonl as jsonl_module
from eimemory.storage.jsonl import JsonlLog
from eimemory.storage.runtime_store import RuntimeStore


def inode(path: Path) -> tuple[int, int]:
    info = path.stat()
    return info.st_dev, info.st_ino


def empty_manifest(log: JsonlLog) -> None:
    log._write_manifest({"version": 1, "segments": [], "pending_segment": None,
                         "cleanup_pending": [], "cleanup_not_before": 0})


def row(index: int) -> dict:
    return {"record_id": f"record_{index}", "summary": "x" * 180}


def trace_fsync(monkeypatch):
    synced = set()
    events = []
    original = os.fsync
    def trace(fd):
        info = os.fstat(fd)
        identity = info.st_dev, info.st_ino
        result = original(fd)
        # Manifest temporary inodes can be reused for the new active file.
        # Record data-file barriers, rather than an unrelated temporary fsync.
        if stat.S_ISDIR(info.st_mode) or os.readlink(f"/proc/self/fd/{fd}").endswith(".jsonl"):
            synced.add(identity)
        events.append(("directory" if stat.S_ISDIR(info.st_mode) else "file", identity))
        return result
    monkeypatch.setattr(os, "fsync", trace)
    return synced, events


@pytest.mark.parametrize("manifest_mode", [False, True], ids=["legacy", "manifest"])
def test_rotation_syncs_source_before_manifest_or_rename(tmp_path, monkeypatch, manifest_mode):
    log = JsonlLog(tmp_path / "records.jsonl", max_segment_bytes=400)
    if manifest_mode:
        empty_manifest(log)
    synced, events = trace_fsync(monkeypatch)
    log.append_payload(row(0), fsync=False)
    source_identity = inode(log.path)
    assert source_identity not in synced  # batching remains deferred before sealing
    original_manifest = log._write_manifest
    original_replace = os.replace
    publications = []
    def write_manifest(manifest):
        if manifest.get("pending_segment"):
            publications.append("pending")
            assert source_identity in synced, "pending published before source file fsync"
        return original_manifest(manifest)
    def replace(source, target):
        if Path(source) == log.path:
            publications.append("rename")
            assert source_identity in synced, "segment renamed before source file fsync"
        return original_replace(source, target)
    monkeypatch.setattr(log, "_write_manifest", write_manifest)
    monkeypatch.setattr(os, "replace", replace)
    log.append_payload(row(1), fsync=False)
    assert publications == (["pending", "rename"] if manifest_mode else ["rename"])
    assert inode(log.path) not in synced  # new active row still batchable
    log.flush_durable()
    assert all(inode(path) in synced for path in log.segment_paths())
    assert ("directory", inode(tmp_path)) in events
    assert [entry.payload for entry in log.scan_strict()] == [row(0), row(1)]


@pytest.mark.parametrize("manifest_mode", [False, True], ids=["legacy", "manifest"])
def test_rotation_fsync_covers_rows_from_another_log_instance(tmp_path, monkeypatch, manifest_mode):
    writer = JsonlLog(tmp_path / "records.jsonl", max_segment_bytes=400)
    if manifest_mode:
        empty_manifest(writer)
    rotator = JsonlLog(writer.path, max_segment_bytes=400)
    synced, _ = trace_fsync(monkeypatch)
    for index in range(4):
        (writer if index % 2 == 0 else rotator).append_payload(row(index), fsync=False)
    writer.flush_durable()
    paths = writer.segment_paths()
    assert len(paths) == 4
    assert all(inode(path) in synced for path in paths)


@pytest.mark.parametrize("manifest_mode", [False, True], ids=["legacy", "manifest"])
def test_failed_seal_does_not_publish_rotation_or_append(tmp_path, monkeypatch, manifest_mode):
    log = JsonlLog(tmp_path / "records.jsonl", max_segment_bytes=400)
    if manifest_mode:
        empty_manifest(log)
    log.append_payload(row(0), fsync=False)
    source_identity = inode(log.path)
    original = os.fsync
    def fail_source(fd):
        info = os.fstat(fd)
        if (info.st_dev, info.st_ino) == source_identity:
            raise OSError("injected segment seal fsync")
        return original(fd)
    with monkeypatch.context() as patch:
        patch.setattr(os, "fsync", fail_source)
        with pytest.raises(OSError, match="injected segment seal fsync"):
            log.append_payload(row(1), fsync=False)
    assert log.segment_paths() == [log.path]
    assert [entry.payload for entry in log.scan_strict()] == [row(0)]
    if manifest_mode:
        assert log._read_manifest()["pending_segment"] is None
    log.append_payload(row(1), fsync=False)
    log.flush_durable()
    assert [entry.payload for entry in log.scan_strict()] == [row(0), row(1)]


def test_flush_syncs_directory_when_active_is_absent_after_rotation_error(tmp_path, monkeypatch):
    log = JsonlLog(tmp_path / "records.jsonl", max_segment_bytes=400)
    log.append_payload(row(0), fsync=False)
    original = jsonl_module._fsync_directory
    def fail_after_rename(path):
        if not log.path.exists() and list(tmp_path.glob("records.????????.jsonl")):
            raise OSError("injected rotation directory fsync")
        return original(path)
    with monkeypatch.context() as patch:
        patch.setattr(jsonl_module, "_fsync_directory", fail_after_rename)
        with pytest.raises(OSError, match="injected rotation directory fsync"):
            log.append_payload(row(1), fsync=False)
    assert not log.path.exists()
    synced, events = trace_fsync(monkeypatch)
    log.flush_durable()
    assert ("directory", inode(tmp_path)) in events
    assert not log.path.exists()  # durability retry need not invent an empty active file
    log.append_payload(row(1), fsync=False)
    log.flush_durable()
    assert [entry.payload for entry in log.scan_strict()] == [row(0), row(1)]


@pytest.mark.parametrize("stream", ["records", "events"])
@pytest.mark.parametrize("manifest_mode", [False, True], ids=["legacy", "manifest"])
def test_runtime_export_ack_waits_for_every_segment_inode(tmp_path, monkeypatch, stream, manifest_mode):
    store = RuntimeStore(tmp_path)
    try:
        log = store.log if stream == "records" else store._auxiliary_log(stream)
        log.max_segment_bytes = 400
        if manifest_mode:
            empty_manifest(log)
        with store.locked() as db:
            for index in range(4):
                db.enqueue_export(stream=stream, payload=row(index))
        synced, _ = trace_fsync(monkeypatch)
        original_commit = store.sqlite.commit
        ack_commits = []
        def commit_after_sync():
            paths = log.segment_paths()
            assert len(paths) == 4
            assert all(inode(path) in synced for path in paths), "outbox committed before all segment fsyncs"
            ack_commits.append(True)
            return original_commit()
        monkeypatch.setattr(store.sqlite, "commit", commit_after_sync)
        assert store.flush_exports()["remaining"] == 0
        assert ack_commits == [True]
    finally:
        store.close()


@pytest.mark.parametrize("stream", ["records", "events"])
def test_runtime_failed_rotation_keeps_outbox_pending_and_retryable(tmp_path, monkeypatch, stream):
    store = RuntimeStore(tmp_path)
    try:
        log = store.log if stream == "records" else store._auxiliary_log(stream)
        log.max_segment_bytes = 400
        with store.locked() as db:
            for index in range(3):
                db.enqueue_export(stream=stream, payload=row(index))
        original = os.fsync
        def fail_jsonl_file(fd):
            info = os.fstat(fd)
            if log.path.exists() and (info.st_dev, info.st_ino) == inode(log.path):
                raise OSError("injected JSONL durability failure")
            return original(fd)
        with monkeypatch.context() as patch:
            patch.setattr(os, "fsync", fail_jsonl_file)
            with pytest.raises(OSError, match="injected JSONL durability failure"):
                store.flush_exports()
        with store.locked() as db:
            assert len(db.pending_exports()) == 3
            assert not db.in_transaction
        synced, _ = trace_fsync(monkeypatch)
        assert store.flush_exports()["remaining"] == 0
        assert all(inode(path) in synced for path in log.segment_paths())
        entries = list(log.scan_strict())
        assert {entry.payload["record_id"] for entry in entries} == {"record_0", "record_1", "record_2"}
        # At-least-once duplicate operation IDs remain byte-equivalent.
        by_operation = {}
        for entry in entries:
            assert by_operation.setdefault(entry.operation_id, entry.payload_digest) == entry.payload_digest
    finally:
        store.close()


@pytest.mark.parametrize("manifest_mode", [False, True], ids=["legacy", "manifest"])
@pytest.mark.parametrize("failure_phase", ["before_rename", "after_rename"])
def test_runtime_rotation_namespace_failure_never_acks_and_can_retry(
    tmp_path, monkeypatch, manifest_mode, failure_phase,
):
    store = RuntimeStore(tmp_path)
    try:
        log = store.log
        log.max_segment_bytes = 400
        if manifest_mode:
            empty_manifest(log)
        with store.locked() as db:
            for index in range(3):
                db.enqueue_export(stream="records", payload=row(index))
        synced, _ = trace_fsync(monkeypatch)
        original_replace = os.replace
        original_dirsync = jsonl_module._fsync_directory
        def failing_replace(source, target):
            if failure_phase == "before_rename" and Path(source) == log.path:
                raise OSError("injected rename boundary")
            return original_replace(source, target)
        def failing_dirsync(path):
            if failure_phase == "after_rename" and not log.path.exists():
                raise OSError("injected rename boundary")
            return original_dirsync(path)
        with monkeypatch.context() as patch:
            patch.setattr(os, "replace", failing_replace)
            patch.setattr(jsonl_module, "_fsync_directory", failing_dirsync)
            with pytest.raises(OSError, match="injected rename boundary"):
                store.flush_exports()
        with store.locked() as db:
            assert len(db.pending_exports()) == 3
        assert store.flush_exports()["remaining"] == 0
        assert all(inode(path) in synced for path in log.segment_paths())
        assert {entry.payload["record_id"] for entry in log.scan_strict()} == {"record_0", "record_1", "record_2"}
    finally:
        store.close()


@pytest.mark.parametrize("failure_phase", ["pending_backup", "pending_primary", "final_backup", "final_primary"])
def test_runtime_manifest_publication_failure_keeps_durable_rows_and_pending_outbox(
    tmp_path, monkeypatch, failure_phase,
):
    store = RuntimeStore(tmp_path)
    try:
        log = store.log
        log.max_segment_bytes = 400
        empty_manifest(log)
        with store.locked() as db:
            for index in range(3):
                db.enqueue_export(stream="records", payload=row(index))
        synced, _ = trace_fsync(monkeypatch)
        original_write = jsonl_module.atomic_write_bytes
        injected = []
        def fail_manifest_write(path, raw, *args, **kwargs):
            manifest = json.loads(raw)
            publication = "pending" if manifest.get("pending_segment") else "final"
            copy = "backup" if Path(path) == log.manifest_backup_path else "primary"
            if f"{publication}_{copy}" == failure_phase:
                injected.append(f"{publication}_{copy}")
                raise OSError("injected manifest publication")
            return original_write(path, raw, *args, **kwargs)
        with monkeypatch.context() as patch:
            patch.setattr(jsonl_module, "atomic_write_bytes", fail_manifest_write)
            with pytest.raises(OSError, match="injected manifest publication"):
                store.flush_exports()
        assert injected == [failure_phase]
        with store.locked() as db:
            assert len(db.pending_exports()) == 3
        assert store.flush_exports()["remaining"] == 0
        assert all(inode(path) in synced for path in log.segment_paths())
        assert {entry.payload["record_id"] for entry in log.scan_strict()} == {"record_0", "record_1", "record_2"}
    finally:
        store.close()


def test_empty_log_flush_is_safe_and_does_not_create_data_file(tmp_path, monkeypatch):
    log = JsonlLog(tmp_path / "empty" / "records.jsonl")
    synced, events = trace_fsync(monkeypatch)
    log.flush_durable()
    assert not log.path.exists()
    assert not log.manifest_path.exists()
    assert ("directory", inode(log.path.parent)) in events
    assert log._last_needs_fsync is False


def test_flush_without_pending_outbox_does_not_write_log(tmp_path, monkeypatch):
    store = RuntimeStore(tmp_path)
    try:
        def unexpected_append(*args, **kwargs):
            raise AssertionError("empty outbox should not append")
        monkeypatch.setattr(store.log, "append_payload", unexpected_append)
        assert store.flush_exports() == {"ok": True, "exported": 0, "remaining": 0}
        assert not store.log.path.exists()
    finally:
        store.close()


@pytest.mark.parametrize("durable", [False, True])
def test_nonrotating_append_preserves_batching_and_payload_contract(tmp_path, monkeypatch, durable):
    log = JsonlLog(tmp_path / "records.jsonl", max_segment_bytes=1024)
    synced, _ = trace_fsync(monkeypatch)
    digest = log.append_payload(row(0), operation_id="op-0", fsync=durable)
    assert (inode(log.path) in synced) is durable
    assert log._last_needs_fsync is (not durable)
    log.flush_durable()
    assert inode(log.path) in synced
    entries = list(log.scan_strict())
    assert len(entries) == 1
    assert entries[0].payload == row(0)
    assert entries[0].operation_id == "op-0"
    assert entries[0].payload_digest == digest


@pytest.mark.parametrize("manifest_mode", [False, True], ids=["legacy", "manifest"])
def test_rotation_seals_another_process_deferred_rows(tmp_path, monkeypatch, manifest_mode):
    import subprocess
    import sys

    log = JsonlLog(tmp_path / "records.jsonl", max_segment_bytes=400)
    if manifest_mode:
        empty_manifest(log)
    synced, _ = trace_fsync(monkeypatch)
    log.append_payload(row(0), fsync=False)
    first_identity = inode(log.path)
    assert first_identity not in synced
    child_code = r'''
import json, os, sys
from pathlib import Path
from eimemory.storage.jsonl import JsonlLog
synced = []
original = os.fsync
def track(fd):
    info = os.fstat(fd)
    result = original(fd)
    if os.readlink(f"/proc/self/fd/{fd}").endswith(".jsonl"):
        synced.append([info.st_dev, info.st_ino])
    return result
os.fsync = track
log = JsonlLog(Path(sys.argv[1]), max_segment_bytes=400)
log.append_payload(json.loads(sys.argv[2]), fsync=False)
print(json.dumps(synced))
'''
    result = subprocess.run(
        [sys.executable, "-c", child_code, str(log.path), json.dumps(row(1))],
        capture_output=True, text=True, timeout=15, check=True,
    )
    child_synced = {tuple(item) for item in json.loads(result.stdout)}
    assert first_identity in child_synced
    assert inode(log.path) not in child_synced
    log.flush_durable()
    assert all(inode(path) in synced | child_synced for path in log.segment_paths())
    assert [entry.payload for entry in log.scan_strict()] == [row(0), row(1)]


@pytest.mark.parametrize("manifest_mode", [False, True], ids=["legacy", "manifest"])
def test_rotating_fsync_true_syncs_old_and_new_inodes(tmp_path, monkeypatch, manifest_mode):
    log = JsonlLog(tmp_path / "records.jsonl", max_segment_bytes=400)
    if manifest_mode:
        empty_manifest(log)
    synced, _ = trace_fsync(monkeypatch)
    log.append_payload(row(0), fsync=False)
    log.append_payload(row(1), fsync=True)
    assert all(inode(path) in synced for path in log.segment_paths())
    assert log._last_needs_fsync is False


@pytest.mark.parametrize("failure_kind", ["active_file", "directory"])
def test_runtime_final_batch_barrier_failure_preserves_pending_and_dirty_state(tmp_path, monkeypatch, failure_kind):
    store = RuntimeStore(tmp_path)
    try:
        with store.locked() as db:
            db.enqueue_export(stream="records", payload=row(0))
        original_fsync = os.fsync
        original_dirsync = jsonl_module._fsync_directory
        def fail_file(fd):
            info = os.fstat(fd)
            if failure_kind == "active_file" and store.log.path.exists() and (info.st_dev, info.st_ino) == inode(store.log.path):
                raise OSError("injected final batch barrier")
            return original_fsync(fd)
        def fail_directory(path):
            if failure_kind == "directory":
                raise OSError("injected final batch barrier")
            return original_dirsync(path)
        with monkeypatch.context() as patch:
            patch.setattr(os, "fsync", fail_file)
            patch.setattr(jsonl_module, "_fsync_directory", fail_directory)
            with pytest.raises(OSError, match="injected final batch barrier"):
                store.flush_exports()
        with store.locked() as db:
            assert len(db.pending_exports()) == 1
        assert store.log._last_needs_fsync is True
        assert store.flush_exports()["remaining"] == 0
        assert store.log._last_needs_fsync is False
    finally:
        store.close()
