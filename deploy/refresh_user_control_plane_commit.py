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
    if target.is_symlink() or not target.is_file():
        raise RefreshError("target must be a regular non-symlink file")
    info = target.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise RefreshError("target must be a regular non-symlink file")
    if info.st_uid not in {os.geteuid(), 0}:
        raise RefreshError("target owner is unexpected")
    payload = target.read_bytes()
    rendered = refresh_commit(payload, commit)
    if rendered == payload:
        return
    temporary = target.with_name(f".eimemory-control-plane-{secrets.token_hex(8)}")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        os.write(fd, rendered)
        os.fsync(fd)
        os.fchmod(fd, stat.S_IMODE(info.st_mode))
        os.fchown(fd, info.st_uid, info.st_gid)
        os.close(fd)
        fd = -1
        os.replace(temporary, target)
    finally:
        if fd >= 0:
            os.close(fd)
        temporary.unlink(missing_ok=True)


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
