#!/usr/bin/env python3
"""Provision the private key used to attest OpenClaw tool receipts."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import secrets
import shutil
import stat
import tempfile


KEY_ENV_NAME = "EIMEMORY_EVIDENCE_RECEIPT_HMAC_KEY"
MIN_KEY_LENGTH = 32


class EvidenceReceiptKeyError(RuntimeError):
    pass


def _read_key(payload: bytes) -> str:
    try:
        lines = [
            line.strip()
            for line in payload.decode("utf-8").splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        ]
    except UnicodeError as exc:
        raise EvidenceReceiptKeyError("receipt key file is unreadable") from exc
    prefix = f"{KEY_ENV_NAME}="
    matches = [line[len(prefix) :].strip() for line in lines if line.startswith(prefix)]
    if (
        len(lines) != 1
        or len(matches) != 1
        or len(matches[0]) < MIN_KEY_LENGTH
        or len(set(matches[0])) < 12
    ):
        raise EvidenceReceiptKeyError("receipt key file contains a weak or malformed key")
    return matches[0]


def _validate(path: Path) -> str:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except (OSError, ValueError) as exc:
        raise EvidenceReceiptKeyError("receipt key file is unreadable or unsafe") from exc
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise EvidenceReceiptKeyError("receipt key file must be a single-link regular file")
        if os.name == "posix" and metadata.st_mode & 0o027:
            raise EvidenceReceiptKeyError("receipt key file permissions are too broad")
        payload = os.read(descriptor, 4097)
        if len(payload) > 4096:
            raise EvidenceReceiptKeyError("receipt key file is oversized")
        return _read_key(payload)
    finally:
        os.close(descriptor)


def _normalize_existing(
    path: Path,
    *,
    user: str | None,
    group: str | None,
) -> None:
    _validate(path)
    os.chmod(path, 0o600)
    if os.name == "posix" and (user or group):
        shutil.chown(path, user=user, group=group)
    _validate(path)


def ensure_evidence_receipt_key_file(
    path: str | Path,
    *,
    user: str | None = None,
    group: str | None = None,
) -> dict[str, object]:
    target = Path(path)
    if target.is_symlink():
        raise EvidenceReceiptKeyError("receipt key file must not be a symlink")
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        _normalize_existing(target, user=user, group=group)
        return {"ok": True, "created": False, "path": str(target)}

    key = secrets.token_urlsafe(48)
    descriptor, temporary_name = tempfile.mkstemp(prefix=".evidence-receipt-", dir=target.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(f"{KEY_ENV_NAME}={key}\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        if os.name == "posix" and (user or group):
            shutil.chown(temporary, user=user, group=group)
        try:
            os.link(temporary, target, follow_symlinks=False)
        except FileExistsError:
            _normalize_existing(target, user=user, group=group)
            return {"ok": True, "created": False, "path": str(target)}
        temporary.unlink()
        if os.name == "posix":
            directory_fd = os.open(target.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
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
        report = ensure_evidence_receipt_key_file(args.path, user=args.user, group=args.group)
    except (EvidenceReceiptKeyError, OSError, LookupError) as exc:
        parser.exit(2, f"evidence receipt key provisioning failed: {exc}\n")
    print(f"evidence_receipt_key_file={report['path']}")
    print(f"evidence_receipt_key_created={int(bool(report['created']))}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
