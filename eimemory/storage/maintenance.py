from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import shutil
import sqlite3
import stat
import threading
import time
from typing import Any
from uuid import uuid4

from eimemory.storage.atomic_file import atomic_write_json, read_json_strict
from eimemory.storage.payload_segments import PayloadSegmentError, PayloadSegmentStore
from eimemory.storage.sqlite_store import SqliteRecordStore


_MANIFEST_NAME = "storage-snapshot.json"
_DB_SIDECAR_SUFFIXES = ("", "-wal", "-shm")
_ARCHIVE_KINDS = ("capability_score", "recall_view")
_MIN_SAFETY_BYTES = 64 * 1024 * 1024
_LOCAL_LOCKS: dict[str, threading.Lock] = {}
_LOCAL_LOCKS_GUARD = threading.Lock()


class StorageMaintenanceError(RuntimeError):
    pass


def _file_digest(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _fsync_file(path: Path) -> None:
    descriptor = os.open(path, os.O_RDWR | getattr(os, "O_BINARY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


@contextmanager
def _exclusive_maintenance_lock(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise StorageMaintenanceError("storage maintenance lock must not be a symlink")
    key = str(path.resolve())
    with _LOCAL_LOCKS_GUARD:
        local = _LOCAL_LOCKS.setdefault(key, threading.Lock())
    if not local.acquire(blocking=False):
        raise StorageMaintenanceError("storage maintenance lock is already held")
    descriptor = -1
    try:
        descriptor = os.open(
            path,
            os.O_RDWR | os.O_CREAT | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        if os.fstat(descriptor).st_size == 0:
            os.write(descriptor, b"\0")
            os.fsync(descriptor)
        os.lseek(descriptor, 0, os.SEEK_SET)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (OSError, BlockingIOError) as exc:
            raise StorageMaintenanceError("storage maintenance lock is held by another process") from exc
        yield
    finally:
        if descriptor >= 0:
            try:
                os.lseek(descriptor, 0, os.SEEK_SET)
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(descriptor, fcntl.LOCK_UN)
            except OSError:
                pass
            os.close(descriptor)
        local.release()


def _fsync_directory(path: Path) -> None:
    if os.name != "posix":
        return
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _validate_regular(path: Path) -> os.stat_result:
    if path.is_symlink():
        raise StorageMaintenanceError(f"storage maintenance rejects symlink: {path}")
    metadata = path.stat(follow_symlinks=False)
    attributes = int(getattr(metadata, "st_file_attributes", 0) or 0)
    if attributes & int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)):
        raise StorageMaintenanceError(f"storage maintenance rejects reparse point: {path}")
    if not stat.S_ISREG(metadata.st_mode) or int(getattr(metadata, "st_nlink", 1)) != 1:
        raise StorageMaintenanceError(f"storage maintenance requires one regular file: {path}")
    return metadata


def _tree_files(root: Path) -> list[Path]:
    if not root.exists():
        return []
    if root.is_symlink():
        raise StorageMaintenanceError(f"storage maintenance rejects symlink root: {root}")
    files: list[Path] = []
    for path in root.rglob("*"):
        if path.is_dir():
            if path.is_symlink():
                raise StorageMaintenanceError(f"storage maintenance rejects symlink directory: {path}")
            continue
        _validate_regular(path)
        files.append(path)
    return sorted(files)


def _tree_bytes(root: Path) -> int:
    return sum(int(path.stat(follow_symlinks=False).st_size) for path in _tree_files(root))


def _database_bundle_bytes(db_path: Path) -> int:
    total = 0
    for suffix in _DB_SIDECAR_SUFFIXES:
        path = Path(str(db_path) + suffix)
        if path.exists():
            total += int(_validate_regular(path).st_size)
    return total


def _archive_estimate_bytes(db_path: Path) -> int:
    if not db_path.exists():
        return 0
    connection = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
    try:
        columns = {str(row[1]) for row in connection.execute("PRAGMA table_info(records)")}
        placeholders = ",".join("?" for _ in _ARCHIVE_KINDS)
        pointer_clause = " AND payload_pointer_json=''" if "payload_pointer_json" in columns else ""
        row = connection.execute(
            "SELECT COALESCE(SUM(LENGTH(CAST(payload_json AS BLOB))),0) FROM records "
            f"WHERE kind IN ({placeholders}){pointer_clause}",
            _ARCHIVE_KINDS,
        ).fetchone()
        return max(0, int(row[0]) if row is not None else 0)
    finally:
        connection.close()


def preflight_storage_maintenance(
    *,
    db_path: str | Path,
    segment_root: str | Path,
    include_snapshot: bool,
    include_vacuum: bool,
    safety_ratio: float = 0.25,
) -> dict[str, Any]:
    database = Path(db_path)
    segments = Path(segment_root)
    database_bytes = _database_bundle_bytes(database)
    segment_bytes = _tree_bytes(segments)
    archive_estimate = _archive_estimate_bytes(database)
    snapshot_bytes = database_bytes + segment_bytes if include_snapshot else 0
    vacuum_bytes = max(database_bytes, int(database.stat().st_size) if database.exists() else 0) if include_vacuum else 0
    working_bytes = snapshot_bytes + vacuum_bytes + archive_estimate
    safety_bytes = max(_MIN_SAFETY_BYTES, int(working_bytes * max(0.0, float(safety_ratio))))
    required = working_bytes + safety_bytes
    free = int(shutil.disk_usage(database.parent).free)
    return {
        "schema": "storage_maintenance_preflight.v1",
        "ok": free >= required,
        "database_bytes": database_bytes,
        "segment_bytes": segment_bytes,
        "archive_estimate_bytes": archive_estimate,
        "snapshot_estimate_bytes": snapshot_bytes,
        "vacuum_estimate_bytes": vacuum_bytes,
        "safety_bytes": safety_bytes,
        "required_free_bytes": required,
        "available_free_bytes": free,
    }


def _checkpoint_and_exclusive_connection(db_path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(db_path, timeout=0.1, isolation_level=None)
    try:
        connection.execute("PRAGMA busy_timeout=0")
        checkpoint = connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        if checkpoint is not None and int(checkpoint[0]) != 0:
            raise StorageMaintenanceError("storage checkpoint is busy; a writer may still be active")
        connection.execute("BEGIN EXCLUSIVE")
        result = str(connection.execute("PRAGMA integrity_check").fetchone()[0])
        if result.lower() != "ok":
            raise StorageMaintenanceError(f"SQLite integrity_check failed: {result}")
        return connection
    except sqlite3.OperationalError as exc:
        connection.close()
        raise StorageMaintenanceError(
            "storage is not offline; SQLite writer or reader lock is active"
        ) from exc
    except Exception:
        connection.close()
        raise


def _copy_file(source: Path, target: Path) -> None:
    _validate_regular(source)
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target, follow_symlinks=False)
    try:
        os.chmod(target, 0o600, follow_symlinks=False)
    except (OSError, NotImplementedError):
        pass
    _fsync_file(target)


def _copy_tree(source: Path, target: Path) -> None:
    target.mkdir(parents=True, exist_ok=False)
    try:
        os.chmod(target, 0o700)
    except OSError:
        pass
    for path in _tree_files(source):
        relative = path.relative_to(source)
        _copy_file(path, target / relative)


def _snapshot_file_manifest(root: Path) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for path in _tree_files(root):
        if path.name == _MANIFEST_NAME and path.parent == root:
            continue
        entries.append(
            {
                "path": path.relative_to(root).as_posix(),
                "size": int(path.stat(follow_symlinks=False).st_size),
                "sha256": _file_digest(path),
            }
        )
    return entries


def create_consistent_storage_snapshot(
    *,
    db_path: str | Path,
    segment_root: str | Path,
    snapshot_dir: str | Path,
    offline: bool,
) -> dict[str, Any]:
    if not offline:
        raise StorageMaintenanceError("consistent storage snapshot requires offline writer stop")
    database = Path(db_path)
    segments = Path(segment_root)
    snapshot = Path(snapshot_dir)
    if snapshot.exists():
        return verify_storage_snapshot(snapshot)
    preflight = preflight_storage_maintenance(
        db_path=database,
        segment_root=segments,
        include_snapshot=True,
        include_vacuum=True,
    )
    if not preflight["ok"]:
        raise StorageMaintenanceError("insufficient free disk for snapshot, migration, and vacuum")
    snapshot.parent.mkdir(parents=True, exist_ok=True)
    staging = snapshot.parent / f".{snapshot.name}.stage-{uuid4().hex}"
    lock_path = database.parent / ".storage-maintenance.lock"
    with _exclusive_maintenance_lock(lock_path):
        connection = _checkpoint_and_exclusive_connection(database)
        try:
            staging.mkdir(mode=0o700)
            _copy_file(database, staging / database.name)
            if segments.exists():
                _copy_tree(segments, staging / "payload_segments")
            else:
                (staging / "payload_segments").mkdir(mode=0o700)
        finally:
            connection.rollback()
            connection.close()
        manifest = {
            "schema": "storage_snapshot.v1",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "database": database.name,
            "segment_directory": "payload_segments",
            "sidecar_policy": "checkpointed_and_excluded",
            "preflight": preflight,
            "files": _snapshot_file_manifest(staging),
        }
        atomic_write_json(staging / _MANIFEST_NAME, manifest)
        _fsync_directory(staging)
        os.replace(staging, snapshot)
        _fsync_directory(snapshot.parent)
    return verify_storage_snapshot(snapshot)


def _validate_sqlite_database(path: str | Path) -> dict[str, Any]:
    database = Path(path)
    connection = sqlite3.connect(f"file:{database.as_posix()}?mode=ro&immutable=1", uri=True)
    try:
        integrity = str(connection.execute("PRAGMA integrity_check").fetchone()[0])
        if integrity.lower() != "ok":
            raise StorageMaintenanceError(f"SQLite integrity_check failed: {integrity}")
        schema = [
            (str(row[0]), str(row[1]), str(row[2] or ""))
            for row in connection.execute(
                "SELECT type,name,sql FROM sqlite_master "
                "WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name"
            )
        ]
        counts: dict[str, int] = {}
        for object_type, name, _sql in schema:
            if object_type != "table" or not name.replace("_", "").isalnum():
                continue
            counts[name] = int(connection.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0])
        return {"integrity_check": integrity, "schema": schema, "table_counts": counts}
    finally:
        connection.close()


def verify_storage_snapshot(snapshot_dir: str | Path) -> dict[str, Any]:
    snapshot = Path(snapshot_dir)
    manifest_path = snapshot / _MANIFEST_NAME
    try:
        manifest = read_json_strict(manifest_path, dict)
    except (OSError, ValueError) as exc:
        raise StorageMaintenanceError("storage snapshot manifest is invalid") from exc
    if str(manifest.get("schema") or "") != "storage_snapshot.v1":
        raise StorageMaintenanceError("storage snapshot schema is invalid")
    expected: dict[str, dict[str, Any]] = {}
    for entry in list(manifest.get("files") or []):
        if not isinstance(entry, dict):
            raise StorageMaintenanceError("storage snapshot file manifest is invalid")
        relative = Path(str(entry.get("path") or ""))
        if relative.is_absolute() or ".." in relative.parts:
            raise StorageMaintenanceError("storage snapshot path escapes root")
        expected[relative.as_posix()] = entry
    actual_paths = {
        path.relative_to(snapshot).as_posix(): path
        for path in _tree_files(snapshot)
        if path != manifest_path
    }
    if set(actual_paths) != set(expected):
        raise StorageMaintenanceError("storage snapshot file set mismatch")
    for relative, path in actual_paths.items():
        entry = expected[relative]
        if int(entry.get("size") or -1) != int(path.stat().st_size):
            raise StorageMaintenanceError("storage snapshot file size mismatch")
        if str(entry.get("sha256") or "") != _file_digest(path):
            raise StorageMaintenanceError("storage snapshot file digest mismatch")
    database = snapshot / str(manifest.get("database") or "eimemory.sqlite")
    validation = _validate_sqlite_database(database)
    pointer_count = 0
    connection = sqlite3.connect(f"file:{database.as_posix()}?mode=ro&immutable=1", uri=True)
    try:
        columns = {str(row[1]) for row in connection.execute("PRAGMA table_info(records)")}
        if {"payload_pointer_json", "payload_digest"}.issubset(columns):
            segment_store = PayloadSegmentStore(
                snapshot / str(manifest["segment_directory"]), read_only=True
            )
            for row in connection.execute(
                "SELECT payload_pointer_json,payload_digest FROM records WHERE payload_pointer_json!=''"
            ):
                pointer = json.loads(str(row[0]))
                raw = segment_store.read(pointer)
                if sha256(raw).hexdigest() != str(row[1] or ""):
                    raise StorageMaintenanceError("snapshot payload pointer digest mismatch")
                pointer_count += 1
    except (json.JSONDecodeError, PayloadSegmentError) as exc:
        raise StorageMaintenanceError("snapshot payload pointer validation failed") from exc
    finally:
        connection.close()
    # PayloadSegmentStore recovery must be a no-op on an accepted snapshot.
    for relative, path in actual_paths.items():
        if str(expected[relative].get("sha256") or "") != _file_digest(path):
            raise StorageMaintenanceError("storage snapshot changed during verification")
    return {
        "schema": "storage_snapshot_verification.v1",
        "ok": True,
        "snapshot_dir": str(snapshot),
        "file_count": len(actual_paths),
        "pointer_count": pointer_count,
        "integrity_check": validation["integrity_check"],
        "manifest_sha256": _file_digest(manifest_path),
    }


def _validate_live_payload_pointers(db_path: Path, segment_root: Path) -> None:
    connection = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro&immutable=1", uri=True)
    try:
        columns = {str(row[1]) for row in connection.execute("PRAGMA table_info(records)")}
        if not {"payload_pointer_json", "payload_digest"}.issubset(columns):
            return
        store = PayloadSegmentStore(segment_root)
        for row in connection.execute(
            "SELECT payload_pointer_json,payload_digest FROM records WHERE payload_pointer_json!=''"
        ):
            raw = store.read(json.loads(str(row[0])))
            if sha256(raw).hexdigest() != str(row[1] or ""):
                raise StorageMaintenanceError("restored payload pointer digest mismatch")
    finally:
        connection.close()


def _verify_staged_snapshot_copy(staging: Path, snapshot: Path) -> None:
    manifest = read_json_strict(snapshot / _MANIFEST_NAME, dict)
    for entry in list(manifest.get("files") or []):
        if not isinstance(entry, dict):
            raise StorageMaintenanceError("storage snapshot file manifest is invalid")
        relative = Path(str(entry.get("path") or ""))
        target = staging / relative
        if not target.exists() or int(target.stat().st_size) != int(entry.get("size") or -1):
            raise StorageMaintenanceError("staged restore file size mismatch")
        if _file_digest(target) != str(entry.get("sha256") or ""):
            raise StorageMaintenanceError("staged restore file digest mismatch")


def _remove_path(path: Path) -> None:
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    else:
        path.unlink(missing_ok=True)


def restore_storage_snapshot(
    *,
    snapshot_dir: str | Path,
    db_path: str | Path,
    segment_root: str | Path,
    offline: bool,
) -> dict[str, Any]:
    if not offline:
        raise StorageMaintenanceError("storage restore requires offline writer stop")
    snapshot = Path(snapshot_dir)
    verification = verify_storage_snapshot(snapshot)
    database = Path(db_path)
    segments = Path(segment_root)
    token = uuid4().hex
    staging = database.parent / f".storage-restore-{token}"
    backups: list[tuple[Path, Path]] = []
    installed: list[Path] = []
    mutation_started = False
    lock_path = database.parent / ".storage-maintenance.lock"
    journal_path = database.parent / ".storage-restore-journal.json"
    if journal_path.exists():
        raise StorageMaintenanceError("unfinished storage restore journal requires recovery")
    with _exclusive_maintenance_lock(lock_path):
        try:
            guard = _checkpoint_and_exclusive_connection(database)
            guard.rollback()
            guard.close()
            if segments.is_symlink() or (
                segments.exists()
                and int(getattr(segments.stat(follow_symlinks=False), "st_file_attributes", 0) or 0)
                & int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
            ):
                raise StorageMaintenanceError("live payload segment root is a symlink or reparse point")
            staging.mkdir(mode=0o700)
            for suffix in _DB_SIDECAR_SUFFIXES:
                source = snapshot / (database.name + suffix)
                if source.exists():
                    _copy_file(source, staging / source.name)
            _copy_tree(snapshot / "payload_segments", staging / "payload_segments")
            _verify_staged_snapshot_copy(staging, snapshot)
            _validate_sqlite_database(staging / database.name)
            planned: list[dict[str, Any]] = []
            for suffix in _DB_SIDECAR_SUFFIXES:
                live = Path(str(database) + suffix)
                planned.append(
                    {
                        "live": str(live),
                        "backup": str(database.parent / f".{live.name}.restore-old-{token}"),
                        "existed": live.exists(),
                        "state": "planned",
                    }
                )
            planned.append(
                {
                    "live": str(segments),
                    "backup": str(database.parent / f".{segments.name}.restore-old-{token}"),
                    "existed": segments.exists(),
                    "state": "planned",
                }
            )
            journal = {
                "schema": "storage_restore_journal.v1",
                "snapshot_dir": str(snapshot),
                "mutation_started": False,
                "entries": planned,
            }
            atomic_write_json(journal_path, journal)
            for suffix in _DB_SIDECAR_SUFFIXES:
                live = Path(str(database) + suffix)
                if live.exists():
                    backup = database.parent / f".{live.name}.restore-old-{token}"
                    mutation_started = True
                    journal["mutation_started"] = True
                    next(item for item in planned if item["live"] == str(live))["state"] = "moving_old"
                    atomic_write_json(journal_path, journal)
                    os.replace(live, backup)
                    backups.append((live, backup))
                    next(item for item in planned if item["live"] == str(live))["state"] = "old_moved"
                    atomic_write_json(journal_path, journal)
            if segments.exists():
                segment_backup = database.parent / f".{segments.name}.restore-old-{token}"
                mutation_started = True
                journal["mutation_started"] = True
                next(item for item in planned if item["live"] == str(segments))["state"] = "moving_old"
                atomic_write_json(journal_path, journal)
                os.replace(segments, segment_backup)
                backups.append((segments, segment_backup))
                next(item for item in planned if item["live"] == str(segments))["state"] = "old_moved"
                atomic_write_json(journal_path, journal)
            for suffix in _DB_SIDECAR_SUFFIXES:
                source = staging / (database.name + suffix)
                if source.exists():
                    live = Path(str(database) + suffix)
                    mutation_started = True
                    journal["mutation_started"] = True
                    next(item for item in planned if item["live"] == str(live))["state"] = "installing"
                    atomic_write_json(journal_path, journal)
                    os.replace(source, live)
                    installed.append(live)
                    next(item for item in planned if item["live"] == str(live))["state"] = "installed"
                    atomic_write_json(journal_path, journal)
            next(item for item in planned if item["live"] == str(segments))["state"] = "installing"
            atomic_write_json(journal_path, journal)
            os.replace(staging / "payload_segments", segments)
            installed.append(segments)
            next(item for item in planned if item["live"] == str(segments))["state"] = "installed"
            atomic_write_json(journal_path, journal)
            _fsync_file(database)
            _fsync_directory(database.parent)
            _validate_sqlite_database(database)
            _validate_live_payload_pointers(database, segments)
        except Exception as exc:
            try:
                if mutation_started:
                    for path in reversed(installed):
                        if path.exists() or path.is_symlink():
                            _remove_path(path)
                    for live, backup in reversed(backups):
                        if backup.exists():
                            os.replace(backup, live)
                    _fsync_directory(database.parent)
                journal_path.unlink(missing_ok=True)
            except Exception as rollback_exc:
                raise StorageMaintenanceError(
                    "storage restore rollback failed; journal preserved"
                ) from rollback_exc
            raise exc
        finally:
            shutil.rmtree(staging, ignore_errors=True)
        for _live, backup in backups:
            if backup.is_dir():
                shutil.rmtree(backup)
            else:
                backup.unlink(missing_ok=True)
        journal_path.unlink(missing_ok=True)
    return {
        "schema": "storage_snapshot_restore.v1",
        "ok": True,
        "snapshot_dir": str(snapshot),
        "manifest_sha256": verification["manifest_sha256"],
    }


def run_storage_migrations(
    *,
    db_path: str | Path,
    offline: bool,
    batch_size: int = 200,
    max_batches: int = 10_000,
    max_seconds: float = 3600.0,
    snapshot_dir: str | Path | None = None,
) -> dict[str, Any]:
    if not offline:
        raise StorageMaintenanceError("production storage migration runner requires offline writer stop")
    if snapshot_dir is None:
        raise StorageMaintenanceError("storage migration requires a verified pre-migration snapshot")
    verify_storage_snapshot(snapshot_dir)
    database = Path(db_path)
    lock_path = database.parent / ".storage-maintenance.lock"
    with _exclusive_maintenance_lock(lock_path):
        guard = _checkpoint_and_exclusive_connection(database)
        guard.rollback()
        guard.close()
        return _run_storage_migrations_locked(
            db_path=database,
            offline=offline,
            batch_size=batch_size,
            max_batches=max_batches,
            max_seconds=max_seconds,
        )


def _run_storage_migrations_locked(
    *,
    db_path: Path,
    offline: bool,
    batch_size: int,
    max_batches: int,
    max_seconds: float,
) -> dict[str, Any]:
    store = SqliteRecordStore(db_path)
    reports: list[dict[str, Any]] = []
    total_batches = 0
    total_processed = 0
    started = time.monotonic()
    try:
        for _index in range(max(0, int(max_batches))):
            pending = store.pending_storage_migrations()
            if not pending:
                return {
                    "schema": "storage_migration_run.v1",
                    "ok": True,
                    "pending": [],
                    "batch_count": total_batches,
                    "processed": total_processed,
                    "reports": reports,
                }
            if time.monotonic() - started > max(0.0, float(max_seconds)):
                return {
                    "schema": "storage_migration_run.v1",
                    "ok": False,
                    "reason": "max_seconds_exceeded",
                    "pending": pending,
                    "batch_count": total_batches,
                    "processed": total_processed,
                    "reports": reports,
                }
            batch = store.apply_storage_migrations(batch_size=batch_size, offline=offline)
            total_batches += 1
            total_processed += int(batch.get("processed") or 0)
            reports.append(batch)
            reports[:] = reports[-20:]
        return {
            "schema": "storage_migration_run.v1",
            "ok": False,
            "reason": "max_batches_exceeded",
            "pending": store.pending_storage_migrations(),
            "batch_count": total_batches,
            "processed": total_processed,
            "reports": reports,
        }
    finally:
        store.close()


def vacuum_into_atomic(
    *,
    db_path: str | Path,
    offline: bool,
    apply: bool = False,
) -> dict[str, Any]:
    database = Path(db_path)
    preflight = preflight_storage_maintenance(
        db_path=database,
        segment_root=database.parent / "payload_segments",
        include_snapshot=False,
        include_vacuum=True,
    )
    report = {
        "schema": "storage_vacuum.v1",
        "ok": bool(preflight["ok"]),
        "applied": False,
        "preflight": preflight,
    }
    if not apply:
        return report
    if not offline:
        raise StorageMaintenanceError("VACUUM INTO atomic replace requires offline writer stop")
    if not preflight["ok"]:
        raise StorageMaintenanceError("insufficient free disk for VACUUM INTO")
    token = uuid4().hex
    temporary = database.parent / f".{database.name}.vacuum-{token}"
    backup = database.parent / f".{database.name}.pre-vacuum-{token}.bak"
    before_digest = _file_digest(database)
    original = _validate_sqlite_database(database)
    lock_path = database.parent / ".storage-maintenance.lock"
    with _exclusive_maintenance_lock(lock_path):
        connection = _checkpoint_and_exclusive_connection(database)
        connection.rollback()
        connection.close()
        writer = sqlite3.connect(database, timeout=0.1, isolation_level=None)
        try:
            escaped = str(temporary).replace("'", "''")
            writer.execute(f"VACUUM INTO '{escaped}'")
        finally:
            writer.close()
        _fsync_file(temporary)
        candidate = _validate_sqlite_database(temporary)
        if candidate["schema"] != original["schema"] or candidate["table_counts"] != original["table_counts"]:
            temporary.unlink(missing_ok=True)
            raise StorageMaintenanceError("VACUUM candidate schema or row counts differ")
        os.replace(database, backup)
        try:
            os.replace(temporary, database)
            Path(str(database) + "-wal").unlink(missing_ok=True)
            Path(str(database) + "-shm").unlink(missing_ok=True)
            _fsync_file(database)
            _fsync_directory(database.parent)
            _validate_sqlite_database(database)
        except Exception:
            database.unlink(missing_ok=True)
            os.replace(backup, database)
            _fsync_file(database)
            _fsync_directory(database.parent)
            raise
    return {
        **report,
        "ok": True,
        "applied": True,
        "before_sha256": before_digest,
        "after_sha256": _file_digest(database),
        "backup_path": str(backup),
    }
