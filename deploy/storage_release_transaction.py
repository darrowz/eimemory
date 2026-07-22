#!/usr/bin/env python3
"""Release-independent durable journal and systemd guard for storage releases."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import tempfile
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


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
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


def load_storage_release_transaction(marker_path: str | Path) -> dict[str, Any]:
    marker = Path(marker_path)
    if marker.is_symlink():
        raise StorageReleaseTransactionError("storage release transaction marker is invalid")
    try:
        payload = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise StorageReleaseTransactionError("storage release transaction marker is invalid") from exc
    return _validated_transaction(payload)


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
    if marker.exists() or marker.is_symlink():
        raise StorageReleaseTransactionError("storage release transaction already exists")
    now = datetime.now(timezone.utc).isoformat()
    payload = _validated_transaction(
        {
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
        }
    )
    _atomic_write_json(marker, payload)
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
    payload = load_storage_release_transaction(marker)
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
    _atomic_write_json(marker, payload)
    return payload


def clear_storage_release_transaction(
    marker_path: str | Path,
    *,
    expected_attempt_id: str,
) -> None:
    marker = Path(marker_path)
    payload = load_storage_release_transaction(marker)
    if payload["attempt_id"] != str(expected_attempt_id):
        raise StorageReleaseTransactionError("storage release transaction attempt mismatch")
    marker.unlink()
    _fsync_directory(marker.parent)


def guard_allows_start(marker_path: str | Path) -> bool:
    """A marker always blocks startup; malformed markers also fail closed."""

    marker = Path(marker_path)
    if not marker.exists() and not marker.is_symlink():
        return True
    try:
        load_storage_release_transaction(marker)
    except StorageReleaseTransactionError:
        return False
    return False


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


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("begin", "update", "show", "active-units", "guard", "clear", "classify"))
    parser.add_argument("--marker", required=True)
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
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    marker = Path(args.marker)
    try:
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
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    except StorageReleaseTransactionError as exc:
        print(f"storage release transaction failed: {exc}", file=os.sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
