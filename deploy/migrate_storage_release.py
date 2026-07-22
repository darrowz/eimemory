from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import shutil
import stat
from typing import Any

from eimemory.storage.maintenance import (
    StorageMaintenanceError,
    create_consistent_storage_snapshot,
    inspect_storage_migration_need,
    preflight_storage_maintenance,
    restore_storage_snapshot,
    run_storage_migrations,
    vacuum_into_atomic,
    verify_storage_snapshot,
)


_COMMIT = re.compile(r"[0-9a-fA-F]{40}")
_ATTEMPT = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,159}")


def _is_reparse(path: Path) -> bool:
    metadata = path.stat(follow_symlinks=False)
    return bool(
        int(getattr(metadata, "st_file_attributes", 0) or 0)
        & int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
    )


def _safe_directory(value: str, *, label: str, private: bool = False) -> Path:
    root = Path(value).expanduser()
    if not root.is_absolute():
        raise StorageMaintenanceError(f"{label} must be absolute")
    root.mkdir(parents=True, exist_ok=True)
    if root.is_symlink() or _is_reparse(root):
        raise StorageMaintenanceError(f"{label} must not be a symlink or reparse point")
    if private:
        try:
            os.chmod(root, 0o700)
        except OSError as exc:
            raise StorageMaintenanceError(f"unable to make {label} private") from exc
    return root


def _release_paths(args: argparse.Namespace) -> tuple[Path, Path, Path, Path]:
    if not _COMMIT.fullmatch(str(args.candidate_commit or "")):
        raise StorageMaintenanceError("candidate commit must be a full SHA-1")
    if not _ATTEMPT.fullmatch(str(args.attempt_id or "")):
        raise StorageMaintenanceError("release storage attempt id is invalid")
    root = _safe_directory(str(args.root), label="runtime root")
    state = _safe_directory(str(root / "state"), label="runtime state directory")
    if state.resolve() != (root.resolve() / "state"):
        raise StorageMaintenanceError("runtime state directory escapes runtime root")
    snapshot_root_candidate = Path(args.snapshot_root).expanduser()
    if not snapshot_root_candidate.is_absolute():
        raise StorageMaintenanceError("snapshot root must be absolute")
    try:
        snapshot_root_candidate.resolve().relative_to(state.resolve())
    except ValueError as exc:
        raise StorageMaintenanceError("snapshot root must stay within runtime state") from exc
    # Containment is checked before mkdir so an invalid caller cannot create an
    # arbitrary directory as a side effect of validation.
    snapshot_root = _safe_directory(
        str(snapshot_root_candidate), label="snapshot root", private=True
    )
    snapshot = Path(args.snapshot_dir).expanduser()
    if not snapshot.is_absolute():
        raise StorageMaintenanceError("snapshot directory must be absolute")
    if snapshot.parent.resolve() != snapshot_root.resolve() or snapshot.name != args.attempt_id:
        raise StorageMaintenanceError("snapshot directory must be the bound attempt path")
    if snapshot.exists() and (snapshot.is_symlink() or _is_reparse(snapshot)):
        raise StorageMaintenanceError("snapshot directory must not be a symlink or reparse point")
    return root, state / "eimemory.sqlite", state / "payload_segments", snapshot


def _binding(args: argparse.Namespace) -> dict[str, str]:
    return {
        "attempt_id": str(args.attempt_id),
        "candidate_commit": str(args.candidate_commit).lower(),
    }


def _verify_bound_snapshot(args: argparse.Namespace, snapshot: Path) -> dict[str, Any]:
    verification = verify_storage_snapshot(snapshot)
    if verification.get("binding") != _binding(args):
        raise StorageMaintenanceError("storage snapshot release binding mismatch")
    return verification


def _verify_binding_shallow(args: argparse.Namespace, snapshot: Path) -> None:
    manifest_path = snapshot / "storage-snapshot.json"
    if (
        not manifest_path.is_file()
        or manifest_path.is_symlink()
        or _is_reparse(manifest_path)
        or manifest_path.stat(follow_symlinks=False).st_size > 1024 * 1024
    ):
        raise StorageMaintenanceError("storage snapshot manifest is missing or unsafe")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise StorageMaintenanceError("storage snapshot manifest is invalid") from exc
    if not isinstance(manifest, dict) or manifest.get("binding") != _binding(args):
        raise StorageMaintenanceError("storage snapshot release binding mismatch")


def _cleanup_vacuum_backup(db_path: Path, value: str) -> dict[str, Any]:
    if not value:
        return {"schema": "storage_vacuum_cleanup.v1", "ok": True, "removed": False}
    backup = Path(value).expanduser()
    expected_name = re.compile(rf"\.{re.escape(db_path.name)}\.pre-vacuum-[0-9a-f]+\.bak")
    if (
        not backup.is_absolute()
        or backup.parent.resolve() != db_path.parent.resolve()
        or not expected_name.fullmatch(backup.name)
    ):
        raise StorageMaintenanceError("vacuum backup path is outside the storage transaction")
    if not backup.exists():
        return {"schema": "storage_vacuum_cleanup.v1", "ok": True, "removed": False}
    if backup.is_symlink() or _is_reparse(backup) or not backup.is_file():
        raise StorageMaintenanceError("vacuum backup is not one regular file")
    backup.unlink()
    if os.name == "posix":
        descriptor = os.open(db_path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    return {"schema": "storage_vacuum_cleanup.v1", "ok": True, "removed": True}


def _snapshot_manifest_for_prune(path: Path) -> dict[str, Any] | None:
    if not path.is_dir() or path.is_symlink() or _is_reparse(path):
        return None
    manifest_path = path / "storage-snapshot.json"
    if not manifest_path.is_file() or manifest_path.is_symlink() or _is_reparse(manifest_path):
        return None
    if manifest_path.stat(follow_symlinks=False).st_size > 1024 * 1024:
        return None
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(manifest, dict):
        return None
    binding = manifest.get("binding")
    if (
        manifest.get("schema") != "storage_snapshot.v1"
        or not isinstance(binding, dict)
        or not _COMMIT.fullmatch(str(binding.get("candidate_commit") or ""))
        or str(binding.get("attempt_id") or "") != path.name
        or not _ATTEMPT.fullmatch(path.name)
    ):
        return None
    return manifest


def _assert_safe_snapshot_tree(path: Path) -> None:
    for root, directories, files in os.walk(path, topdown=True, followlinks=False):
        root_path = Path(root)
        if root_path.is_symlink() or _is_reparse(root_path):
            raise StorageMaintenanceError("snapshot retention rejects linked directory")
        for name in directories:
            candidate = root_path / name
            if candidate.is_symlink() or _is_reparse(candidate):
                raise StorageMaintenanceError("snapshot retention rejects linked directory")
        for name in files:
            candidate = root_path / name
            if candidate.is_symlink() or _is_reparse(candidate) or not candidate.is_file():
                raise StorageMaintenanceError("snapshot retention rejects unsafe file")


def _prune_snapshots(
    snapshot_root: Path,
    *,
    retain: int,
    current_attempt: str,
) -> dict[str, Any]:
    accepted: list[tuple[str, Path]] = []
    for path in snapshot_root.iterdir():
        manifest = _snapshot_manifest_for_prune(path)
        if manifest is None:
            continue
        accepted.append((str(manifest.get("created_at") or ""), path))
    accepted.sort(key=lambda item: (item[0], item[1].name), reverse=True)
    keep = max(1, min(10, int(retain)))
    protected = [item for item in accepted if item[1].name == current_attempt]
    selected = protected[:1]
    selected.extend(
        item for item in accepted if item[1].name != current_attempt
    )
    selected = selected[:keep]
    selected_paths = {path for _created_at, path in selected}
    removed: list[str] = []
    for _created_at, path in accepted:
        if path in selected_paths:
            continue
        _assert_safe_snapshot_tree(path)
        shutil.rmtree(path)
        removed.append(str(path))
    if os.name == "posix" and removed:
        descriptor = os.open(snapshot_root, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    return {
        "schema": "storage_snapshot_retention.v1",
        "ok": True,
        "retained": len(selected),
        "removed": removed,
        "ignored": max(0, len(list(snapshot_root.iterdir())) - len(selected)),
    }


def run_action(args: argparse.Namespace) -> dict[str, Any]:
    _root, db_path, segment_root, snapshot = _release_paths(args)
    if not db_path.is_file() or db_path.is_symlink() or _is_reparse(db_path):
        raise StorageMaintenanceError("runtime SQLite database is missing or unsafe")
    if args.action == "needs":
        return inspect_storage_migration_need(db_path)
    if args.action == "preflight":
        return preflight_storage_maintenance(
            db_path=db_path,
            segment_root=segment_root,
            include_snapshot=True,
            include_vacuum=True,
        )
    if args.action == "snapshot":
        return create_consistent_storage_snapshot(
            db_path=db_path,
            segment_root=segment_root,
            snapshot_dir=snapshot,
            offline=True,
            binding=_binding(args),
        )
    if args.action == "verify":
        return _verify_bound_snapshot(args, snapshot)
    if args.action == "migrate":
        return run_storage_migrations(
            db_path=db_path,
            offline=True,
            batch_size=int(args.batch_size),
            max_batches=int(args.max_batches),
            max_seconds=float(args.max_seconds),
            snapshot_dir=snapshot,
            expected_binding=_binding(args),
        )
    if args.action == "vacuum":
        _verify_bound_snapshot(args, snapshot)
        return vacuum_into_atomic(db_path=db_path, offline=True, apply=True)
    if args.action == "restore":
        return restore_storage_snapshot(
            snapshot_dir=snapshot,
            db_path=db_path,
            segment_root=segment_root,
            offline=True,
            expected_binding=_binding(args),
        )
    if args.action == "cleanup-vacuum":
        _verify_binding_shallow(args, snapshot)
        return _cleanup_vacuum_backup(db_path, str(args.backup_path or ""))
    if args.action == "status":
        _verify_binding_shallow(args, snapshot)
        need = inspect_storage_migration_need(db_path)
        return {
            "schema": "storage_release_status.v1",
            "ok": not need["needed"],
            "pending": need["pending"],
        }
    if args.action == "prune-snapshots":
        return _prune_snapshots(
            Path(args.snapshot_root),
            retain=int(args.retain_snapshots),
            current_attempt=str(args.attempt_id),
        )
    raise StorageMaintenanceError("unsupported storage release action")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Offline release-bound eimemory storage maintenance")
    parser.add_argument(
        "action",
        choices=(
            "preflight",
            "needs",
            "snapshot",
            "verify",
            "migrate",
            "vacuum",
            "restore",
            "cleanup-vacuum",
            "status",
            "prune-snapshots",
        ),
    )
    parser.add_argument("--root", required=True)
    parser.add_argument("--snapshot-root", required=True)
    parser.add_argument("--snapshot-dir", required=True)
    parser.add_argument("--candidate-commit", required=True)
    parser.add_argument("--attempt-id", required=True)
    parser.add_argument("--backup-path", default="")
    parser.add_argument("--retain-snapshots", type=int, default=2)
    parser.add_argument("--batch-size", type=int, default=200)
    parser.add_argument("--max-batches", type=int, default=10_000)
    parser.add_argument("--max-seconds", type=float, default=3600.0)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        report = run_action(args)
    except (OSError, ValueError, StorageMaintenanceError) as exc:
        print(
            json.dumps(
                {"ok": False, "error": type(exc).__name__, "detail": str(exc)},
                ensure_ascii=False,
            )
        )
        return 2
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report.get("ok") is True else 1


if __name__ == "__main__":
    raise SystemExit(main())
