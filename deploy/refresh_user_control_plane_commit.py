#!/usr/bin/env python3
"""Refresh only the release commit inside a user control-plane drop-in.

The final user-control-plane drop-in overrides Environment= values from earlier
managed drop-ins. Ordinary promotion must update that commit without rewriting
sandbox, command, or credential policy.
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import re
import secrets
import stat


_COMMIT_RE = re.compile(r"[0-9a-f]{40}")
_ASSIGNMENT_RE = re.compile(
    r"^(Environment=EIMEMORY_RUNTIME_COMMIT=)([0-9a-f]{40})$"
)
_RELEASE_PATH_RE = re.compile(
    r"(?<![A-Za-z0-9])(/opt/eimemory/releases/)([0-9a-f]{40})(?![0-9a-f])"
)
_PYCACHE_RE = re.compile(
    r"^(Environment=PYTHONPYCACHEPREFIX=/var/lib/eimemory/\.pycache/)([0-9a-f]{40})$"
)


class RefreshError(RuntimeError):
    """Raised when the final identity drop-in cannot be refreshed safely."""


def refresh_commit(payload: bytes, commit: str) -> bytes:
    if _COMMIT_RE.fullmatch(commit) is None:
        raise RefreshError("commit must be a 40-character hex SHA")
    try:
        text = payload.decode("utf-8")
    except UnicodeError as exc:
        raise RefreshError("drop-in must be UTF-8") from exc
    if "\x00" in text or not text.endswith("\n"):
        raise RefreshError("drop-in must be a newline-terminated text file")
    updated: list[str] = []
    assignment_count = 0
    for line in text.splitlines():
        assignment = _ASSIGNMENT_RE.fullmatch(line)
        if assignment:
            assignment_count += 1
            updated.append(assignment.group(1) + commit)
            continue
        cache = _PYCACHE_RE.fullmatch(line)
        if cache:
            updated.append(cache.group(1) + commit)
            continue
        if "/opt/eimemory/releases/" in line and not line.startswith("#"):
            rendered, count = _RELEASE_PATH_RE.subn(r"\g<1>" + commit, line)
            if count == 0 or _COMMIT_RE.search(rendered.replace(commit, "")):
                raise RefreshError("release path is not an exact commit token")
            updated.append(rendered)
            continue
        updated.append(line)
    if assignment_count != 1:
        raise RefreshError("drop-in must contain exactly one runtime commit assignment")
    rendered = "\n".join(updated) + "\n"
    if rendered.encode("utf-8") == payload:
        return payload
    return rendered.encode("utf-8")


def write_refresh(target: Path, commit: str) -> None:
    # Walk directory descriptors so no component can redirect the update.
    target = Path(os.path.abspath(target))
    directory = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    temporary = f".eimemory-control-plane-{secrets.token_hex(8)}"
    fd = -1
    source = -1
    staged = False
    try:
        for component in target.parent.parts[1:]:
            child = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                            dir_fd=directory)
            os.close(directory)
            directory = child
        source = os.open(target.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                         dir_fd=directory)
        info = os.fstat(source)
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                or info.st_uid not in {os.geteuid(), 0} or info.st_mode & 0o022
                or info.st_size > 65536):
            raise RefreshError("target must be a trusted regular file")
        with os.fdopen(os.dup(source), "rb") as stream:
            payload = stream.read(65537)
        rendered = refresh_commit(payload, commit)
        if rendered == payload:
            return
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                     0o600, dir_fd=directory)
        staged = True
        with os.fdopen(os.dup(fd), "wb") as stream:
            stream.write(rendered)
            stream.flush()
        os.fchown(fd, info.st_uid, info.st_gid)
        os.fchmod(fd, stat.S_IMODE(info.st_mode))
        os.fsync(fd)
        current = os.stat(target.name, dir_fd=directory, follow_symlinks=False)
        identity = lambda st: (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns,
                               st.st_ctime_ns, st.st_mode, st.st_uid, st.st_gid, st.st_nlink)
        os.lseek(source, 0, os.SEEK_SET)
        if identity(current) != identity(info) or os.read(source, 65537) != payload:
            raise RefreshError("target changed during refresh")
        os.replace(temporary, target.name, src_dir_fd=directory, dst_dir_fd=directory)
        staged = False
        os.fsync(directory)
    finally:
        if fd >= 0:
            os.close(fd)
        if source >= 0:
            os.close(source)
        if staged:
            os.unlink(temporary, dir_fd=directory)
        os.close(directory)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--target", required=True, type=Path)
    parser.add_argument("--commit", required=True)
    args = parser.parse_args(argv)
    try:
        write_refresh(args.target, args.commit)
    except (RefreshError, OSError) as exc:
        parser.exit(2, f"user control-plane commit refresh failed: {exc}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
