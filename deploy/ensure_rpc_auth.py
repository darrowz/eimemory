#!/usr/bin/env python3
"""Provision the mandatory RPC bearer token without exposing it."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import secrets
import shutil
import tempfile


TOKEN_ENV_NAME = "EIMEMORY_RPC_AUTH_TOKEN"
MIN_TOKEN_LENGTH = 32
MIN_DISTINCT_CHARACTERS = 12


class RPCAuthError(RuntimeError):
    """Raised when the RPC authentication file is missing its safety contract."""


def _strong_token(token: str) -> bool:
    value = str(token or "").strip()
    return len(value) >= MIN_TOKEN_LENGTH and len(set(value)) >= MIN_DISTINCT_CHARACTERS


def _read_token(path: Path) -> str:
    try:
        lines = [
            line.strip()
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        ]
    except (OSError, UnicodeError) as exc:
        raise RPCAuthError("RPC auth file is unreadable") from exc
    prefix = f"{TOKEN_ENV_NAME}="
    matches = [line[len(prefix) :].strip() for line in lines if line.startswith(prefix)]
    if len(lines) != 1 or len(matches) != 1 or not _strong_token(matches[0]):
        raise RPCAuthError("RPC auth file contains a weak or malformed token")
    return matches[0]


def _fsync_directory(path: Path) -> None:
    if os.name != "posix":
        return
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _apply_ownership(path: Path, *, user: str | None, group: str | None) -> None:
    if os.name == "posix" and (user or group):
        shutil.chown(path, user=user, group=group)


def ensure_rpc_auth_file(
    path: str | Path,
    *,
    user: str | None = None,
    group: str | None = None,
) -> dict[str, object]:
    target = Path(path)
    if target.is_symlink():
        raise RPCAuthError("RPC auth file must not be a symlink")
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        _read_token(target)
        if os.name == "posix" and target.stat(follow_symlinks=False).st_mode & 0o027:
            raise RPCAuthError("RPC auth file permissions are too broad")
        return {"ok": True, "created": False, "path": str(target)}

    token = secrets.token_urlsafe(32)
    if not _strong_token(token):  # pragma: no cover - token_urlsafe contract guard
        raise RPCAuthError("generated RPC authentication token was unexpectedly weak")
    descriptor, temporary_name = tempfile.mkstemp(prefix=".rpc-auth-", dir=target.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(f"{TOKEN_ENV_NAME}={token}\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o640)
        _apply_ownership(temporary, user=user, group=group)
        os.replace(temporary, target)
        _fsync_directory(target.parent)
    finally:
        temporary.unlink(missing_ok=True)
    return {"ok": True, "created": True, "path": str(target)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--path", required=True, type=Path)
    parser.add_argument("--user", default=None)
    parser.add_argument("--group", default=None)
    args = parser.parse_args(argv)
    try:
        report = ensure_rpc_auth_file(args.path, user=args.user, group=args.group)
    except (RPCAuthError, OSError, LookupError) as exc:
        parser.exit(2, f"RPC auth provisioning failed: {exc}\n")
    print(f"rpc_auth_file={report['path']}")
    print(f"rpc_auth_created={int(bool(report['created']))}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
