#!/usr/bin/env python3
"""Hold one release lock only for the lifetime of its installer parent."""

from __future__ import annotations

import argparse
import ctypes
import fcntl
import os
from pathlib import Path
import signal
import stat


class ParentBoundLockError(RuntimeError):
    """A release lock cannot be held safely."""


class ParentBoundLockContended(ParentBoundLockError):
    """Another live installer already owns the release lock."""


def _set_parent_death_signal(parent_pid: int) -> None:
    if os.name != "posix" or not Path("/proc/self/stat").is_file():
        raise ParentBoundLockError("parent death signaling is unavailable")
    if os.getppid() != parent_pid:
        raise ParentBoundLockError("deployment parent is unavailable")
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(1, signal.SIGTERM, 0, 0, 0) != 0:  # PR_SET_PDEATHSIG
        error = ctypes.get_errno()
        raise ParentBoundLockError(
            f"unable to bind parent death signal: errno={error}"
        )
    if os.getppid() != parent_pid:
        raise ParentBoundLockError("deployment parent exited during lease setup")


def _path_identity(path: Path) -> tuple[int, int, int]:
    metadata = os.stat(path, follow_symlinks=False)
    return (int(metadata.st_dev), int(metadata.st_ino), int(metadata.st_nlink))


def _validated_parent(path: Path) -> tuple[Path, tuple[int, int]]:
    parent = path.parent
    lexical = Path(os.path.abspath(os.path.normpath(parent)))
    resolved = parent.resolve(strict=True)
    if resolved != lexical or not resolved.is_dir():
        raise ParentBoundLockError("lock ancestor is unsafe")
    metadata = resolved.stat()
    return resolved, (int(metadata.st_dev), int(metadata.st_ino))


def _open_and_lock(path: Path) -> int:
    if not path.is_absolute() or path.is_symlink():
        raise ParentBoundLockError("lock path is unsafe")
    parent, parent_identity = _validated_parent(path)
    descriptor = os.open(
        path,
        os.O_RDWR | os.O_CREAT | int(getattr(os, "O_NOFOLLOW", 0)),
        0o644,
    )
    try:
        metadata = os.fstat(descriptor)
        fd_identity = (
            int(metadata.st_dev),
            int(metadata.st_ino),
            int(metadata.st_nlink),
        )
        if not stat.S_ISREG(metadata.st_mode) or fd_identity[2] != 1:
            raise ParentBoundLockError("lock is not one regular file")
        if _path_identity(path) != fd_identity:
            raise ParentBoundLockError("lock inode changed during open")
        os.fchmod(descriptor, 0o644)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ParentBoundLockContended("lock is already held") from exc
        parent_after, parent_identity_after = _validated_parent(path)
        if parent_after != parent or parent_identity_after != parent_identity:
            raise ParentBoundLockError("lock ancestor changed during acquisition")
        if _path_identity(path) != fd_identity:
            raise ParentBoundLockError("lock inode changed during acquisition")
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def hold_parent_bound_lock(
    path: str | Path,
    *,
    parent_pid: int,
    label: str,
) -> None:
    _set_parent_death_signal(parent_pid)
    descriptor = _open_and_lock(Path(path))
    try:
        print(f"{label}=ready", flush=True)
        while True:
            signal.pause()
    finally:
        os.close(descriptor)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--path", required=True)
    parser.add_argument("--parent-pid", required=True, type=int)
    parser.add_argument(
        "--label",
        choices=("storage_deploy_lock", "candidate_validation_lock"),
        required=True,
    )
    args = parser.parse_args(argv)
    try:
        hold_parent_bound_lock(
            args.path,
            parent_pid=args.parent_pid,
            label=args.label,
        )
    except ParentBoundLockContended:
        print(f"{args.label}=contended", flush=True)
        return 73
    except (ParentBoundLockError, OSError, ValueError) as exc:
        print(f"{args.label}=failed detail={exc}", flush=True)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
