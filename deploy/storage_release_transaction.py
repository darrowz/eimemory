#!/usr/bin/env python3
"""Release-independent durable journal and systemd guard for storage releases."""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import secrets
import stat
import tempfile
import threading
from typing import Any


SCHEMA = "storage_release_transaction.v1"
_COMMIT_RE = re.compile(r"[0-9a-fA-F]{40}")
_DIGEST_RE = re.compile(r"[0-9a-f]{64}")
_UNIT_RE = re.compile(r"[A-Za-z0-9_.@-]+\.(?:service|timer)")
_PHASES = {
    "writers_captured",
    "writers_stopped",
    "snapshot_ready",
    "storage_destructive",
    "storage_migrated",
    "vacuum_complete",
    "current_switched",
    "metadata_ready",
    "rollback_started",
    "rollback_storage_restored",
    "rollback_link_restored",
    "rollback_metadata_ready",
}
_PRIOR_CURRENT_PHASES = {
    "writers_captured",
    "writers_stopped",
    "snapshot_ready",
    "storage_destructive",
    "storage_migrated",
    "vacuum_complete",
    "rollback_link_restored",
    "rollback_storage_restored",
    "rollback_metadata_ready",
}
_CANDIDATE_CURRENT_PHASES = {
    "current_switched",
    "metadata_ready",
    "rollback_started",
}
_PROCESS_MARKER_LOCK = threading.RLock()


class StorageReleaseTransactionError(RuntimeError):
    pass


def _fsync_directory(path: Path) -> None:
    if os.name != "posix":
        return
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _inode_identity(metadata: os.stat_result) -> tuple[int, int]:
    return (int(metadata.st_dev), int(metadata.st_ino))


def _assert_fd_matches_entry(
    descriptor: int,
    *,
    parent_fd: int,
    name: str,
    message: str,
) -> None:
    descriptor_metadata = os.fstat(descriptor)
    try:
        entry_metadata = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except OSError as exc:
        raise StorageReleaseTransactionError(message) from exc
    if _inode_identity(descriptor_metadata) != _inode_identity(entry_metadata):
        raise StorageReleaseTransactionError(message)


def _assert_directory_chain(
    entries: list[tuple[int | None, str, int]],
    *,
    message: str,
) -> None:
    for parent_fd, name, descriptor in entries[1:]:
        if parent_fd is None:
            raise StorageReleaseTransactionError(message)
        _assert_fd_matches_entry(
            descriptor,
            parent_fd=parent_fd,
            name=name,
            message=message,
        )


@contextmanager
def _open_directory_fds(path: Path, *, create: bool = False):
    path = Path(path)
    if not path.is_absolute() or ".." in path.parts:
        raise StorageReleaseTransactionError("directory path must be absolute and normalized")
    flags = os.O_RDONLY | int(getattr(os, "O_DIRECTORY", 0)) | int(
        getattr(os, "O_NOFOLLOW", 0)
    )
    entries: list[tuple[int | None, str, int]] = []
    try:
        root_fd = os.open(path.anchor, flags)
        entries.append((None, path.anchor, root_fd))
        for component in path.parts[1:]:
            parent_fd = entries[-1][2]
            created = False
            try:
                descriptor = os.open(component, flags, dir_fd=parent_fd)
            except FileNotFoundError:
                if not create:
                    raise
                os.mkdir(component, mode=0o700, dir_fd=parent_fd)
                created = True
                descriptor = os.open(component, flags, dir_fd=parent_fd)
            metadata = os.fstat(descriptor)
            if not stat.S_ISDIR(metadata.st_mode):
                os.close(descriptor)
                raise StorageReleaseTransactionError("path component is not a directory")
            entries.append((parent_fd, component, descriptor))
            _assert_fd_matches_entry(
                descriptor,
                parent_fd=parent_fd,
                name=component,
                message="directory entry changed while opening path",
            )
            if created:
                os.fsync(descriptor)
                os.fsync(parent_fd)
        yield entries
    finally:
        for _parent_fd, _name, descriptor in reversed(entries):
            os.close(descriptor)


def _durably_sync_path_posix(path: Path, *, boundary: Path) -> None:
    path = Path(path)
    boundary = Path(boundary)
    if (
        not path.is_absolute()
        or not boundary.is_absolute()
        or ".." in path.parts
        or ".." in boundary.parts
    ):
        raise StorageReleaseTransactionError("durable sync paths must be absolute")
    try:
        path.relative_to(boundary)
    except ValueError as exc:
        raise StorageReleaseTransactionError("durable sync path escapes its boundary") from exc
    nofollow = int(getattr(os, "O_NOFOLLOW", 0))
    directory_flags = os.O_RDONLY | int(getattr(os, "O_DIRECTORY", 0)) | nofollow
    with _open_directory_fds(path.parent) as entries:
        parent_fd = entries[-1][2]
        try:
            target_metadata = os.stat(path.name, dir_fd=parent_fd, follow_symlinks=False)
        except OSError as exc:
            raise StorageReleaseTransactionError("durable sync target is unavailable") from exc
        target_directory_fd = -1
        if stat.S_ISREG(target_metadata.st_mode):
            descriptor = os.open(path.name, os.O_RDONLY | nofollow, dir_fd=parent_fd)
            try:
                if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                    raise StorageReleaseTransactionError(
                        "durable sync target is not a regular file"
                    )
                os.fsync(descriptor)
                _assert_fd_matches_entry(
                    descriptor,
                    parent_fd=parent_fd,
                    name=path.name,
                    message="durable sync entry changed during fsync",
                )
            finally:
                os.close(descriptor)
        elif stat.S_ISDIR(target_metadata.st_mode):
            target_directory_fd = os.open(path.name, directory_flags, dir_fd=parent_fd)
            _assert_fd_matches_entry(
                target_directory_fd,
                parent_fd=parent_fd,
                name=path.name,
                message="durable sync entry changed during fsync",
            )
            entries.append((parent_fd, path.name, target_directory_fd))
        else:
            raise StorageReleaseTransactionError("durable sync target is not a file or directory")

        boundary_index = len(boundary.parts) - 1
        if boundary_index >= len(entries):
            raise StorageReleaseTransactionError("durable sync boundary is not a directory")
        for entry_parent_fd, entry_name, descriptor in reversed(entries[boundary_index:]):
            if entry_parent_fd is not None:
                _assert_fd_matches_entry(
                    descriptor,
                    parent_fd=entry_parent_fd,
                    name=entry_name,
                    message="durable sync entry changed during fsync",
                )
            os.fsync(descriptor)
            if entry_parent_fd is not None:
                _assert_fd_matches_entry(
                    descriptor,
                    parent_fd=entry_parent_fd,
                    name=entry_name,
                    message="durable sync entry changed during fsync",
                )


def _atomic_write_json(
    path: Path,
    payload: dict[str, Any],
    *,
    parent_fd: int | None = None,
) -> None:
    if parent_fd is not None:
        temporary_name = f".{path.name}.{secrets.token_hex(8)}"
        descriptor = os.open(
            temporary_name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | int(getattr(os, "O_NOFOLLOW", 0)),
            0o600,
            dir_fd=parent_fd,
        )
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n", closefd=False) as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.fchmod(descriptor, 0o600)
            os.fsync(descriptor)
            os.replace(
                temporary_name,
                path.name,
                src_dir_fd=parent_fd,
                dst_dir_fd=parent_fd,
            )
            _assert_fd_matches_entry(
                descriptor,
                parent_fd=parent_fd,
                name=path.name,
                message="storage release transaction marker changed while publishing",
            )
            os.fsync(parent_fd)
            _assert_fd_matches_entry(
                descriptor,
                parent_fd=parent_fd,
                name=path.name,
                message="storage release transaction marker changed while publishing",
            )
        finally:
            os.close(descriptor)
            try:
                os.unlink(temporary_name, dir_fd=parent_fd)
            except FileNotFoundError:
                pass
        return

    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise StorageReleaseTransactionError("storage release transaction marker is a symlink")
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def durably_sync_path(path: str | Path, *, boundary: str | Path) -> None:
    if os.name == "posix":
        _durably_sync_path_posix(Path(path), boundary=Path(boundary))


def _absolute_path(value: str | Path, *, label: str) -> str:
    path = Path(value)
    if not path.is_absolute() or ".." in path.parts:
        raise StorageReleaseTransactionError(f"{label} must be an absolute normalized path")
    return str(path)


def _nonblank(value: Any, *, label: str) -> str:
    text = str(value or "")
    if not text.strip() or any(character in text for character in "\r\n\0"):
        raise StorageReleaseTransactionError(f"{label} must be non-blank")
    return text


def _validated_transaction(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict) or payload.get("schema") != SCHEMA:
        raise StorageReleaseTransactionError("storage release transaction marker is invalid")
    if payload.get("status") != "in_progress":
        raise StorageReleaseTransactionError("storage release transaction status is invalid")
    if str(payload.get("phase") or "") not in _PHASES:
        raise StorageReleaseTransactionError("storage release transaction phase is invalid")
    for field in ("prior_commit", "candidate_commit"):
        if _COMMIT_RE.fullmatch(str(payload.get(field) or "")) is None:
            raise StorageReleaseTransactionError(f"storage release transaction {field} is invalid")
    _nonblank(payload.get("attempt_id"), label="attempt id")
    _absolute_path(str(payload.get("current_link") or ""), label="current link")
    _absolute_path(str(payload.get("snapshot_dir") or ""), label="snapshot directory")
    digest = str(payload.get("snapshot_manifest_sha256") or "")
    if digest and _DIGEST_RE.fullmatch(digest) is None:
        raise StorageReleaseTransactionError("snapshot manifest digest is invalid")
    if not isinstance(payload.get("storage_destructive"), bool):
        raise StorageReleaseTransactionError("storage destructive flag is invalid")
    if payload["storage_destructive"] and _DIGEST_RE.fullmatch(digest) is None:
        raise StorageReleaseTransactionError(
            "destructive storage transaction requires a sealed snapshot digest"
        )
    units = payload.get("active_writer_units")
    if not isinstance(units, list) or any(
        not isinstance(unit, str) or _UNIT_RE.fullmatch(unit) is None for unit in units
    ):
        raise StorageReleaseTransactionError("active writer unit list is invalid")
    if len(units) != len(set(units)):
        raise StorageReleaseTransactionError("active writer unit list contains duplicates")
    backup = str(payload.get("vacuum_backup_path") or "")
    if backup:
        _absolute_path(backup, label="vacuum backup path")
    return dict(payload)


@contextmanager
def _marker_lock(marker: Path):
    lock_path = marker.with_name(f".{marker.name}.lock")
    if os.name == "posix":
        parent_context = _open_directory_fds(lock_path.parent, create=True)
    else:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            if lock_path.parent.resolve(strict=True) != Path(os.path.abspath(lock_path.parent)):
                raise StorageReleaseTransactionError(
                    "storage release transaction lock parent traverses a symlink"
                )
        except OSError as exc:
            raise StorageReleaseTransactionError(
                "storage release transaction lock parent is invalid"
            ) from exc
        parent_context = _portable_marker_parent_context()
    with parent_context as parent_entries:
        parent_fd = parent_entries[-1][2] if parent_entries else None
        if parent_fd is not None:
            descriptor = os.open(
                lock_path.name,
                os.O_RDWR | os.O_CREAT | int(getattr(os, "O_NOFOLLOW", 0)),
                0o600,
                dir_fd=parent_fd,
            )
        else:
            if lock_path.is_symlink():
                raise StorageReleaseTransactionError(
                    "storage release transaction lock is a symlink"
                )
            descriptor = os.open(
                lock_path,
                os.O_RDWR | os.O_CREAT | int(getattr(os, "O_NOFOLLOW", 0)),
                0o600,
            )
        try:
            metadata = os.fstat(descriptor)
            if (
                int(getattr(metadata, "st_file_attributes", 0) or 0)
                & int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
                or not stat.S_ISREG(metadata.st_mode)
                or int(getattr(metadata, "st_nlink", 1)) != 1
            ):
                raise StorageReleaseTransactionError("storage release transaction lock is unsafe")
            if metadata.st_size == 0:
                os.write(descriptor, b"0")
                os.fsync(descriptor)
                if parent_fd is not None:
                    os.fsync(parent_fd)
            with _PROCESS_MARKER_LOCK:
                if os.name == "posix":
                    import fcntl

                    fcntl.flock(descriptor, fcntl.LOCK_EX)
                elif os.name == "nt":
                    import msvcrt

                    os.lseek(descriptor, 0, os.SEEK_SET)
                    msvcrt.locking(descriptor, msvcrt.LK_LOCK, 1)
                try:
                    _assert_directory_chain(
                        parent_entries,
                        message=(
                            "storage release transaction lock parent changed while held"
                        ),
                    )
                    _assert_lock_binding(
                        descriptor,
                        lock_path=lock_path,
                        parent_fd=parent_fd,
                    )
                    yield parent_fd
                    _assert_directory_chain(
                        parent_entries,
                        message=(
                            "storage release transaction lock parent changed while held"
                        ),
                    )
                    _assert_lock_binding(
                        descriptor,
                        lock_path=lock_path,
                        parent_fd=parent_fd,
                    )
                finally:
                    if os.name == "posix":
                        import fcntl

                        fcntl.flock(descriptor, fcntl.LOCK_UN)
                    elif os.name == "nt":
                        import msvcrt

                        os.lseek(descriptor, 0, os.SEEK_SET)
                        msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
        finally:
            os.close(descriptor)


@contextmanager
def _portable_marker_parent_context():
    yield []


def _assert_lock_binding(
    descriptor: int,
    *,
    lock_path: Path,
    parent_fd: int | None,
) -> None:
    message = "storage release transaction lock changed while held"
    if parent_fd is not None:
        _assert_fd_matches_entry(
            descriptor,
            parent_fd=parent_fd,
            name=lock_path.name,
            message=message,
        )
        return
    try:
        entry_metadata = lock_path.stat(follow_symlinks=False)
    except OSError as exc:
        raise StorageReleaseTransactionError(message) from exc
    if _inode_identity(os.fstat(descriptor)) != _inode_identity(entry_metadata):
        raise StorageReleaseTransactionError(message)


def _load_storage_release_transaction_unlocked(
    marker: Path,
    *,
    parent_fd: int | None = None,
) -> dict[str, Any]:
    marker = Path(marker)
    if marker.is_symlink():
        raise StorageReleaseTransactionError("storage release transaction marker is invalid")
    try:
        if parent_fd is not None:
            descriptor = os.open(
                marker.name,
                os.O_RDONLY | int(getattr(os, "O_NOFOLLOW", 0)),
                dir_fd=parent_fd,
            )
        else:
            descriptor = os.open(marker, os.O_RDONLY | int(getattr(os, "O_NOFOLLOW", 0)))
        try:
            metadata = os.fstat(descriptor)
            if (
                int(getattr(metadata, "st_file_attributes", 0) or 0)
                & int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
                or not stat.S_ISREG(metadata.st_mode)
                or int(getattr(metadata, "st_nlink", 1)) != 1
            ):
                raise StorageReleaseTransactionError(
                    "storage release transaction marker is invalid"
                )
            with os.fdopen(descriptor, "r", encoding="utf-8", closefd=False) as handle:
                payload = json.load(handle)
            if parent_fd is not None:
                _assert_fd_matches_entry(
                    descriptor,
                    parent_fd=parent_fd,
                    name=marker.name,
                    message="storage release transaction marker changed while reading",
                )
        finally:
            os.close(descriptor)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise StorageReleaseTransactionError("storage release transaction marker is invalid") from exc
    return _validated_transaction(payload)


def load_storage_release_transaction(marker_path: str | Path) -> dict[str, Any]:
    marker = Path(marker_path)
    with _marker_lock(marker) as parent_fd:
        return _load_storage_release_transaction_unlocked(marker, parent_fd=parent_fd)


def begin_storage_release_transaction(
    marker_path: str | Path,
    *,
    prior_commit: str,
    candidate_commit: str,
    current_link: str | Path,
    attempt_id: str,
    snapshot_dir: str | Path,
    active_writer_units: list[str],
) -> dict[str, Any]:
    marker = Path(marker_path)
    with _marker_lock(marker) as parent_fd:
        if _marker_entry_exists(marker, parent_fd=parent_fd) or _marker_entry_exists(
            _clear_tombstone(marker), parent_fd=parent_fd
        ):
            raise StorageReleaseTransactionError("storage release transaction already exists")
        now = datetime.now(timezone.utc).isoformat()
        payload = _validated_transaction({
            "schema": SCHEMA,
            "status": "in_progress",
            "phase": "writers_captured",
            "prior_commit": str(prior_commit),
            "candidate_commit": str(candidate_commit),
            "current_link": _absolute_path(current_link, label="current link"),
            "attempt_id": _nonblank(attempt_id, label="attempt id"),
            "snapshot_dir": _absolute_path(snapshot_dir, label="snapshot directory"),
            "snapshot_manifest_sha256": "",
            "storage_destructive": False,
            "active_writer_units": list(active_writer_units),
            "vacuum_backup_path": "",
            "created_at": now,
            "updated_at": now,
        })
        _atomic_write_json(marker, payload, parent_fd=parent_fd)
    return payload


def update_storage_release_transaction(
    marker_path: str | Path,
    *,
    expected_attempt_id: str,
    phase: str,
    snapshot_manifest_sha256: str | None = None,
    storage_destructive: bool | None = None,
    vacuum_backup_path: str | None = None,
) -> dict[str, Any]:
    marker = Path(marker_path)
    with _marker_lock(marker) as parent_fd:
        payload = _load_storage_release_transaction_unlocked(marker, parent_fd=parent_fd)
        if payload["attempt_id"] != str(expected_attempt_id):
            raise StorageReleaseTransactionError("storage release transaction attempt mismatch")
        payload["phase"] = str(phase)
        if snapshot_manifest_sha256 is not None:
            payload["snapshot_manifest_sha256"] = str(snapshot_manifest_sha256)
        if storage_destructive is not None:
            payload["storage_destructive"] = bool(storage_destructive)
        if vacuum_backup_path is not None:
            payload["vacuum_backup_path"] = str(vacuum_backup_path)
        payload["updated_at"] = datetime.now(timezone.utc).isoformat()
        payload = _validated_transaction(payload)
        _atomic_write_json(marker, payload, parent_fd=parent_fd)
    return payload


def clear_storage_release_transaction(
    marker_path: str | Path,
    *,
    expected_attempt_id: str,
) -> None:
    marker = Path(marker_path)
    tombstone = _clear_tombstone(marker)
    with _marker_lock(marker) as parent_fd:
        marker_present = _marker_entry_exists(marker, parent_fd=parent_fd)
        tombstone_present = _marker_entry_exists(tombstone, parent_fd=parent_fd)
        if marker_present and tombstone_present:
            raise StorageReleaseTransactionError(
                "storage release transaction clear state is ambiguous"
            )
        source = tombstone if tombstone_present else marker
        payload = _load_storage_release_transaction_unlocked(source, parent_fd=parent_fd)
        if payload["attempt_id"] != str(expected_attempt_id):
            raise StorageReleaseTransactionError("storage release transaction attempt mismatch")
        if marker_present:
            _replace_marker_entry(marker, tombstone, parent_fd=parent_fd)
            try:
                _sync_marker_parent(marker, parent_fd=parent_fd)
            except OSError as exc:
                raise StorageReleaseTransactionError(
                    "storage release transaction clear is not durable"
                ) from exc
        try:
            _unlink_marker_entry(tombstone, parent_fd=parent_fd)
            _sync_marker_parent(marker, parent_fd=parent_fd)
        except OSError as exc:
            try:
                _restore_blocking_marker(marker, tombstone, payload, parent_fd=parent_fd)
            except OSError as restore_exc:
                try:
                    blocker_present = _marker_entry_exists(
                        marker, parent_fd=parent_fd
                    ) or _marker_entry_exists(tombstone, parent_fd=parent_fd)
                except OSError:
                    blocker_present = False
                detail = (
                    "startup remains blocked"
                    if blocker_present
                    else "manual fail-closed intervention is required"
                )
                raise StorageReleaseTransactionError(
                    f"storage release transaction blocker recovery failed; {detail}"
                ) from restore_exc
            raise StorageReleaseTransactionError(
                "storage release transaction clear is not durable"
            ) from exc


def _clear_tombstone(marker: Path) -> Path:
    return marker.with_name(f".{marker.name}.clearing")


def _replace_marker_entry(source: Path, destination: Path, *, parent_fd: int | None) -> None:
    if parent_fd is None:
        os.replace(source, destination)
        return
    os.replace(
        source.name,
        destination.name,
        src_dir_fd=parent_fd,
        dst_dir_fd=parent_fd,
    )


def _unlink_marker_entry(path: Path, *, parent_fd: int | None) -> None:
    if parent_fd is None:
        path.unlink()
        return
    os.unlink(path.name, dir_fd=parent_fd)


def _sync_marker_parent(marker: Path, *, parent_fd: int | None) -> None:
    if parent_fd is None:
        _fsync_directory(marker.parent)
        return
    os.fsync(parent_fd)


def _restore_blocking_marker(
    marker: Path,
    tombstone: Path,
    payload: dict[str, Any],
    *,
    parent_fd: int | None,
) -> None:
    if _marker_entry_exists(marker, parent_fd=parent_fd) or _marker_entry_exists(
        tombstone, parent_fd=parent_fd
    ):
        return
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | int(getattr(os, "O_NOFOLLOW", 0))
    if parent_fd is None:
        descriptor = os.open(marker, flags, 0o600)
    else:
        descriptor = os.open(marker.name, flags, 0o600, dir_fd=parent_fd)
    serialized = (
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")
    try:
        remaining = memoryview(serialized)
        while remaining:
            written = os.write(descriptor, remaining)
            if written <= 0:
                raise OSError("failed to restore storage release transaction blocker")
            remaining = remaining[written:]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    try:
        _sync_marker_parent(marker, parent_fd=parent_fd)
    except OSError:
        # The live marker already blocks startup. Preserve it even when the
        # filesystem continues to reject durability barriers.
        pass


def _marker_entry_exists(marker: Path, *, parent_fd: int | None) -> bool:
    if parent_fd is None:
        return marker.exists() or marker.is_symlink()
    try:
        os.stat(marker.name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return False
    return True


def guard_allows_start(marker_path: str | Path) -> bool:
    """A marker always blocks startup; malformed markers also fail closed."""

    marker = Path(marker_path)
    tombstone = _clear_tombstone(marker)
    try:
        with _marker_lock(marker) as parent_fd:
            if _marker_entry_exists(tombstone, parent_fd=parent_fd):
                return False
            return not _marker_entry_exists(marker, parent_fd=parent_fd)
    except (OSError, StorageReleaseTransactionError):
        return False


def storage_release_abort_is_safe(transaction: dict[str, Any]) -> bool:
    """Only pre-snapshot, non-destructive phases may be cleared on ordinary failure."""

    payload = _validated_transaction(transaction)
    return (
        payload["phase"] in {"writers_captured", "writers_stopped"}
        and payload["storage_destructive"] is False
    )


def classify_storage_release_reconcile(
    transaction: dict[str, Any],
    *,
    current_commit: str,
    migrations_complete: bool,
) -> str:
    payload = _validated_transaction(transaction)
    current = str(current_commit)
    rolling_back = str(payload["phase"]).startswith("rollback_")
    if rolling_back:
        if current == payload["candidate_commit"]:
            return "resume_rollback"
        if current == payload["prior_commit"]:
            return "restore_prior"
    if current == payload["prior_commit"]:
        return "restore_prior" if payload["storage_destructive"] else "clear_prior"
    if current == payload["candidate_commit"] and migrations_complete:
        return "finalize_candidate"
    raise StorageReleaseTransactionError(
        "storage release transaction is inconsistent with current release or migrations"
    )


def expected_current_commit_for_reconcile(transaction: dict[str, Any]) -> str:
    """Return the one release bound to a missing current link by journal phase."""

    payload = _validated_transaction(transaction)
    phase = str(payload["phase"])
    if phase in _PRIOR_CURRENT_PHASES:
        return str(payload["prior_commit"])
    if phase in _CANDIDATE_CURRENT_PHASES:
        return str(payload["candidate_commit"])
    raise StorageReleaseTransactionError(
        "storage release transaction phase has no unambiguous current release"
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "action",
        choices=(
            "begin",
            "update",
            "show",
            "active-units",
            "guard",
            "clear",
            "classify",
            "expected-current",
            "abort-safe",
            "fsync-path",
        ),
    )
    parser.add_argument("--marker", default="")
    parser.add_argument("--prior-commit", default="")
    parser.add_argument("--candidate-commit", default="")
    parser.add_argument("--current-link", default="")
    parser.add_argument("--current-commit", default="")
    parser.add_argument("--attempt-id", default="")
    parser.add_argument("--snapshot-dir", default="")
    parser.add_argument("--snapshot-manifest-sha256")
    parser.add_argument("--phase", default="")
    parser.add_argument("--storage-destructive", choices=("0", "1"))
    parser.add_argument("--vacuum-backup-path")
    parser.add_argument("--active-unit", action="append", default=[])
    parser.add_argument("--migrations-complete", choices=("0", "1"), default="0")
    parser.add_argument("--path", default="")
    parser.add_argument("--boundary", default="")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    marker = Path(args.marker)
    try:
        if args.action == "fsync-path":
            durably_sync_path(args.path, boundary=args.boundary)
            return 0
        if not args.marker:
            raise StorageReleaseTransactionError("storage release transaction marker is required")
        if args.action == "guard":
            if guard_allows_start(marker):
                return 0
            print("storage_release_guard=blocked marker_present_or_invalid", file=os.sys.stderr)
            return 75
        if args.action == "begin":
            payload = begin_storage_release_transaction(
                marker,
                prior_commit=args.prior_commit,
                candidate_commit=args.candidate_commit,
                current_link=args.current_link,
                attempt_id=args.attempt_id,
                snapshot_dir=args.snapshot_dir,
                active_writer_units=list(args.active_unit),
            )
        elif args.action == "update":
            payload = update_storage_release_transaction(
                marker,
                expected_attempt_id=args.attempt_id,
                phase=args.phase,
                snapshot_manifest_sha256=args.snapshot_manifest_sha256,
                storage_destructive=(args.storage_destructive == "1")
                if args.storage_destructive is not None
                else None,
                vacuum_backup_path=args.vacuum_backup_path,
            )
        elif args.action == "clear":
            clear_storage_release_transaction(marker, expected_attempt_id=args.attempt_id)
            return 0
        else:
            payload = load_storage_release_transaction(marker)
            if args.action == "active-units":
                for unit in payload["active_writer_units"]:
                    print(unit)
                return 0
            if args.action == "classify":
                print(
                    classify_storage_release_reconcile(
                        payload,
                        current_commit=args.current_commit,
                        migrations_complete=args.migrations_complete == "1",
                    )
                )
                return 0
            if args.action == "expected-current":
                print(expected_current_commit_for_reconcile(payload))
                return 0
            if args.action == "abort-safe":
                return 0 if storage_release_abort_is_safe(payload) else 75
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    except StorageReleaseTransactionError as exc:
        print(f"storage release transaction failed: {exc}", file=os.sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
