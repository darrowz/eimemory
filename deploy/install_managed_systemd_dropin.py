#!/usr/bin/env python3
"""Install an eimemory-owned systemd drop-in without overwriting local config."""

from __future__ import annotations

import argparse
import errno
import os
from pathlib import Path
import re
import secrets
import stat
import tempfile


MANAGED_MARKER = "# Managed by eimemory immutable release installer"
DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW if os.name == "posix" else 0


class ManagedDropinError(RuntimeError):
    """Raised when a managed drop-in cannot be installed safely."""


def _is_managed(payload: bytes) -> bool:
    try:
        return payload.decode("utf-8").splitlines()[0] == MANAGED_MARKER
    except (UnicodeDecodeError, IndexError):
        return False


def install_managed_dropin(
    *,
    source: Path,
    target: Path,
    root: Path,
    owner_uid: int | None = None,
    render_commit: str = "",
) -> None:
    source = Path(source)
    target = Path(target)
    root = Path(root)
    if source.is_symlink() or not source.is_file():
        raise ManagedDropinError("source must be a regular non-symlink file")
    payload = source.read_bytes()
    if not _is_managed(payload):
        raise ManagedDropinError("source is missing the managed marker")
    if render_commit:
        if not re.fullmatch(r"[0-9a-fA-F]{40}", render_commit):
            raise ManagedDropinError("render commit must be a full 40-character SHA")
        token = b"@EIMEMORY_COMMIT@"
        if token not in payload:
            raise ManagedDropinError("managed source is missing the commit token")
        payload = payload.replace(token, render_commit.encode("ascii"))

    if target.parent.parent != root or not target.parent.name.endswith(".service.d"):
        raise ManagedDropinError("target must be a direct service drop-in under systemd root")
    allowed_owners = {os.geteuid() if hasattr(os, "geteuid") else root.stat().st_uid}
    if owner_uid is not None:
        allowed_owners.add(int(owner_uid))
    if os.name == "posix":
        _install_with_directory_fds(
            payload=payload,
            target=target,
            root=root,
            allowed_owners=allowed_owners,
        )
        return
    _install_portable(payload=payload, target=target, root=root, allowed_owners=allowed_owners)


def _install_with_directory_fds(
    *, payload: bytes, target: Path, root: Path, allowed_owners: set[int]
) -> None:
    root_fd = _open_directory_without_symlinks(root)
    try:
        if os.fstat(root_fd).st_uid not in allowed_owners:
            raise ManagedDropinError("systemd root has an unexpected owner")
        parent_name = target.parent.name
        try:
            parent_fd = os.open(parent_name, DIRECTORY_FLAGS, dir_fd=root_fd)
        except FileNotFoundError:
            os.mkdir(parent_name, mode=0o755, dir_fd=root_fd)
            parent_fd = os.open(parent_name, DIRECTORY_FLAGS, dir_fd=root_fd)
        except OSError as exc:
            raise ManagedDropinError("drop-in directory must not be a symlink") from exc
        try:
            if os.fstat(parent_fd).st_uid not in allowed_owners:
                raise ManagedDropinError("drop-in directory has an unexpected owner")
            _validate_existing_target_at(parent_fd=parent_fd, target_name=target.name)
            _atomic_write_at(parent_fd=parent_fd, target_name=target.name, payload=payload)
        finally:
            os.close(parent_fd)
    finally:
        os.close(root_fd)


def _open_directory_without_symlinks(path: Path) -> int:
    if not path.is_absolute() or ".." in path.parts:
        raise ManagedDropinError("systemd root must be an absolute normalized path")
    parts = path.parts
    try:
        current_fd = os.open(parts[0], DIRECTORY_FLAGS)
        for component in parts[1:]:
            try:
                next_fd = os.open(component, DIRECTORY_FLAGS, dir_fd=current_fd)
            finally:
                os.close(current_fd)
            current_fd = next_fd
        return current_fd
    except OSError as exc:
        raise ManagedDropinError(
            "systemd root must be an existing path without symlink components"
        ) from exc


def _validate_existing_target_at(*, parent_fd: int, target_name: str) -> None:
    try:
        fd = os.open(target_name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent_fd)
    except FileNotFoundError:
        return
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise ManagedDropinError("managed target must not be a symlink") from exc
        raise
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ManagedDropinError("managed target must be a regular file")
        with os.fdopen(fd, "rb", closefd=False) as handle:
            if not _is_managed(handle.read()):
                raise ManagedDropinError("existing target is not managed by eimemory")
    finally:
        os.close(fd)


def _atomic_write_at(*, parent_fd: int, target_name: str, payload: bytes) -> None:
    temp_name = f".eimemory-dropin-{secrets.token_hex(8)}"
    temp_fd = -1
    try:
        temp_fd = os.open(
            temp_name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
            dir_fd=parent_fd,
        )
        with os.fdopen(temp_fd, "wb", closefd=False) as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.fchmod(temp_fd, 0o644)
        os.fsync(temp_fd)
        os.close(temp_fd)
        temp_fd = -1
        os.rename(temp_name, target_name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
        os.fsync(parent_fd)
    finally:
        if temp_fd >= 0:
            os.close(temp_fd)
        try:
            os.unlink(temp_name, dir_fd=parent_fd)
        except FileNotFoundError:
            pass


def _install_portable(
    *, payload: bytes, target: Path, root: Path, allowed_owners: set[int]
) -> None:

    if not root.exists() or not root.is_dir() or root.is_symlink():
        raise ManagedDropinError("systemd root must be an existing non-symlink directory")
    if Path(os.path.realpath(root)) != Path(os.path.abspath(root)):
        raise ManagedDropinError("systemd root must not traverse symlinks")
    if root.stat().st_uid not in allowed_owners:
        raise ManagedDropinError("systemd root has an unexpected owner")
    target.parent.mkdir(mode=0o755, exist_ok=True)
    if target.parent.is_symlink() or not target.parent.is_dir():
        raise ManagedDropinError("drop-in directory must not be a symlink")
    if target.parent.stat().st_uid not in allowed_owners:
        raise ManagedDropinError("drop-in directory has an unexpected owner")
    if target.is_symlink():
        raise ManagedDropinError("managed target must not be a symlink")
    if target.exists():
        if not stat.S_ISREG(target.stat(follow_symlinks=False).st_mode):
            raise ManagedDropinError("managed target must be a regular file")
        if not _is_managed(target.read_bytes()):
            raise ManagedDropinError("existing target is not managed by eimemory")

    fd, temp_name = tempfile.mkstemp(prefix=".eimemory-dropin-", dir=target.parent)
    temp_path = Path(temp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temp_path, 0o644)
        os.replace(temp_path, target)
    finally:
        temp_path.unlink(missing_ok=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--target", required=True, type=Path)
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--owner-uid", type=int)
    parser.add_argument("--render-commit", default="")
    args = parser.parse_args(argv)
    try:
        install_managed_dropin(
            source=args.source,
            target=args.target,
            root=args.root,
            owner_uid=args.owner_uid,
            render_commit=args.render_commit,
        )
    except (ManagedDropinError, OSError) as exc:
        parser.exit(2, f"managed drop-in install failed: {exc}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
