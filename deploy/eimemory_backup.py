#!/usr/bin/env python3
"""Scheduled, verified, restorable backup set for one eimemory runtime.

Each run writes one complete set under ``<root>/backups/<UTC timestamp>/``:

* ``sqlite/``   online SQLite backups (``sqlite3.Connection.backup``) of every
  authoritative database in ``<root>/state``; each copy must pass
  ``PRAGMA integrity_check`` and is re-opened to count rows.
* ``sqlite/payload_segments/`` the immutable frame prefixes referenced by those
  database snapshots, verified by native payload hydration before publication.
* ``records/``  the logical record export from ``eimemory backup create``,
  re-verified with ``eimemory backup verify`` (this is the manifest the
  governance snapshot discovers; it replaces ``no_backups_found``).
* ``state/``    the small append-only JSONL/JSON state journals.
* ``config.tar.gz``  ``/etc/eimemory`` (contains secrets: mode 0600, the set
  directory 0700, contents are never printed).
* ``backup-set.json``  per-file size and sha256 plus every check result.

The set is assembled in a hidden temporary directory and renamed into place
only after every check passed, so a partial set is never visible as a backup.
Retention removes only complete sets this job created (timestamp-named
directories under the backup root), keeping the newest ``--keep``.  The job is
read-only against runtime data and prints a bounded JSON summary.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import sys
import tarfile
import time
from typing import Any

# This helper is also executed with -I from an immutable release.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

SCHEMA = "eimemory.backup_set.v1"
SET_NAME = re.compile(r"^\d{8}T\d{6}Z$")
SQLITE_SUFFIXES = (".sqlite", ".sqlite3", ".db")
STATE_FILE_SUFFIXES = (".jsonl", ".json")
MAX_STATE_FILE_BYTES = 256 * 1024 * 1024


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _sqlite_sources(state_dir: Path) -> list[Path]:
    # Only top-level authoritative databases; deploy-job scratch copies and
    # release snapshots live in subdirectories and are not runtime authority.
    return sorted(
        path for path in state_dir.iterdir()
        if path.is_file() and not path.is_symlink() and path.suffix in SQLITE_SUFFIXES
    )


def _backup_sqlite(source: Path, target: Path) -> dict[str, Any]:
    started = time.monotonic()
    src = sqlite3.connect(f"file:{source}?mode=ro", uri=True, timeout=60)
    try:
        dst = sqlite3.connect(str(target))
        try:
            src.backup(dst, pages=4096, sleep=0.01)
            # A published snapshot is self-contained, including when the live
            # database uses WAL. Its later verification must not create sidecars.
            dst.execute("PRAGMA journal_mode=DELETE")
        finally:
            dst.close()
    finally:
        src.close()
    check = sqlite3.connect(target.as_uri() + "?mode=ro&immutable=1", uri=True)
    try:
        integrity = [str(row[0]) for row in check.execute("PRAGMA integrity_check").fetchall()]
        tables = [row[0] for row in check.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        ).fetchall()]
        rows = 0
        if "records" in tables:
            rows = int(check.execute("SELECT COUNT(*) FROM records").fetchone()[0])
    finally:
        check.close()
    ok = integrity == ["ok"]
    return {
        "source": str(source),
        "file": f"sqlite/{target.name}",
        "ok": ok,
        "integrity_check": "ok" if ok else "failed",
        "table_count": len(tables),
        "records_rows": rows,
        "seconds": round(time.monotonic() - started, 2),
    }


def _snapshot_records(database: Path):
    """Stream authority from the sealed SQLite copy, never the live database."""
    connection = sqlite3.connect(database.as_uri() + "?mode=ro&immutable=1", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(records)")}
        if "payload_json" not in columns:
            return
        selected = [name for name in ("payload_json", "payload_pointer_json", "payload_digest")
                    if name in columns]
        yield from connection.execute("SELECT " + ",".join(selected) + " FROM records")
    finally:
        connection.close()


def _copy_snapshot_payloads(sqlite_dir: Path, live_segments: Path) -> None:
    """Copy only complete, referenced append-only prefixes from the live archive.

    SQLite commits its pointer after the frame is durable. Later appends can
    therefore continue while these fixed prefixes are copied. Missing/replaced
    archives or an offline compaction race fail verification, never fall back to
    a newer live record. No live archive index/stats are used or modified.
    """
    from eimemory.storage.payload_segments import PayloadSegmentStore, _HEADER

    destination = sqlite_dir / "payload_segments"
    destination.mkdir(mode=0o700)
    required: dict[str, int] = {}
    source = None
    for database in _sqlite_sources(sqlite_dir):
        for row in _snapshot_records(database):
            if "payload_pointer_json" not in row.keys() or not row["payload_pointer_json"]:
                continue
            if source is None:
                source = PayloadSegmentStore(live_segments, read_only=True)
            pointer = json.loads(row["payload_pointer_json"])
            normalized = source._validate_pointer(pointer)
            if pointer.get("header_size") != _HEADER.size:
                raise ValueError("backup_payload_pointer_header_invalid")
            # Checks the header, bounded decompression and content digest.
            raw = source.read(pointer)
            if hashlib.sha256(raw).hexdigest() != row["payload_digest"]:
                raise ValueError("backup_payload_digest_mismatch")
            end = normalized["offset"] + _HEADER.size + normalized["compressed_size"]
            name = normalized["segment"]
            required[name] = max(required.get(name, 0), end)
    for name, length in sorted(required.items()):
        descriptor = source._open_secure(live_segments / name, os.O_RDONLY)
        with os.fdopen(descriptor, "rb") as original, (destination / name).open("xb") as copied:
            os.chmod(destination / name, 0o600)
            remaining = length
            while remaining:
                block = original.read(min(1024 * 1024, remaining))
                if not block:
                    raise ValueError("backup_payload_segment_truncated")
                copied.write(block)
                remaining -= len(block)
            copied.flush()
            os.fsync(copied.fileno())
    # Rebuild archive metadata using only staged frames. Live index/stats may
    # refer to appends after this snapshot and must not be copied into it.
    PayloadSegmentStore(destination)


def _verify_snapshot_payloads(sqlite_dir: Path) -> dict[str, Any]:
    """Verify hydrated records entirely from the backup's own files."""
    from eimemory.models.records import RecordEnvelope
    from eimemory.storage.payload_segments import PayloadSegmentStore
    from eimemory.storage.jsonl import canonical_payload_json

    archive = PayloadSegmentStore(sqlite_dir / "payload_segments", read_only=True)
    count = archived = 0
    for database in _sqlite_sources(sqlite_dir):
        for row in _snapshot_records(database):
            pointer_json = row["payload_pointer_json"] if "payload_pointer_json" in row.keys() else ""
            if pointer_json:
                raw = archive.read(json.loads(pointer_json))
                payload = json.loads(raw)
                archived += 1
            else:
                payload = json.loads(row["payload_json"])
                raw = canonical_payload_json(payload).encode("utf-8")
            if "payload_digest" in row.keys() and row["payload_digest"]:
                if hashlib.sha256(raw).hexdigest() != row["payload_digest"]:
                    raise ValueError("backup_restored_payload_digest_mismatch")
            RecordEnvelope.from_dict(payload)
            count += 1
    return {"ok": True, "validated_record_count": count, "archived_record_count": archived,
            "segment_directory": "sqlite/payload_segments"}


def _sync_backup_tree(root: Path) -> None:
    for path in root.rglob("*"):
        if path.is_file():
            with path.open("rb") as handle:
                os.fsync(handle.fileno())
    if os.name == "posix":
        for path in [*sorted((p for p in root.rglob("*") if p.is_dir()),
                             key=lambda p: len(p.parts), reverse=True), root]:
            descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)


def _run_cli(eimemory_bin: str, args: list[str], env: dict[str, str], timeout: int) -> dict[str, Any]:
    completed = subprocess.run(
        [eimemory_bin, *args],
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    try:
        payload = json.loads(completed.stdout or "{}")
    except json.JSONDecodeError:
        payload = {}
    return {"exit_code": completed.returncode, "payload": payload if isinstance(payload, dict) else {}}


def _copy_state_files(root: Path, state_dir: Path, target: Path) -> list[str]:
    copied: list[str] = []
    candidates = [
        *(p for p in root.iterdir() if p.is_file() and p.suffix == ".json"),
        *(p for p in state_dir.iterdir() if p.is_file() and p.suffix in STATE_FILE_SUFFIXES),
    ]
    for path in sorted(candidates):
        if path.is_symlink() or path.stat().st_size > MAX_STATE_FILE_BYTES:
            continue
        prefix = "root" if path.parent == root else "state"
        destination = target / prefix / path.name
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, destination)
        copied.append(f"state/{prefix}/{path.name}")
    return copied


def _archive_config(config_dir: Path, target: Path) -> dict[str, Any]:
    if not config_dir.is_dir():
        return {"ok": False, "reason": "config_dir_missing"}
    with tarfile.open(target, "w:gz") as archive:
        archive.add(str(config_dir), arcname=config_dir.name)
    os.chmod(target, 0o600)
    with tarfile.open(target, "r:gz") as archive:
        members = [m for m in archive.getmembers() if m.isfile()]
    return {"ok": True, "file": target.name, "file_count": len(members)}


def _file_inventory(set_dir: Path) -> list[dict[str, Any]]:
    inventory = []
    for path in sorted(p for p in set_dir.rglob("*") if p.is_file()):
        if path == set_dir / "backup-set.json":
            continue
        inventory.append({
            "path": path.relative_to(set_dir).as_posix(),
            "bytes": path.stat().st_size,
            "sha256": _sha256(path),
        })
    return inventory


def _prune(backup_root: Path, keep: int, current: str) -> list[str]:
    removed: list[str] = []
    complete = sorted(
        (p for p in backup_root.iterdir()
         if p.is_dir() and not p.is_symlink() and SET_NAME.match(p.name)
         and (p / "backup-set.json").is_file()),
        key=lambda p: p.name,
        reverse=True,
    )
    for stale in complete[max(1, keep):]:
        if stale.name == current:
            continue
        shutil.rmtree(stale)
        removed.append(stale.name)
    cutoff = time.time() - 24 * 3600
    for partial in backup_root.glob(".tmp-*"):
        if partial.is_dir() and not partial.is_symlink() and partial.stat().st_mtime < cutoff:
            shutil.rmtree(partial)
            removed.append(partial.name)
    return removed


def _terminate_as_exit(signum: int, _frame: Any) -> None:
    """A deploy restart sends SIGTERM; unwind so the partial set is removed."""
    raise SystemExit(128 + signum)


def _install_sigterm_unwind() -> None:
    import signal

    signal.signal(signal.SIGTERM, _terminate_as_exit)


def _sweep_orphan_partials(backup_root: Path) -> list[str]:
    """Remove in-progress partials left by a killed run (never ``-failed`` sets).

    Callers hold the exclusive backup lock, so no live run owns them.
    """
    removed: list[str] = []
    for partial in backup_root.glob(".tmp-*"):
        if partial.name.endswith("-failed") or partial.is_symlink() or not partial.is_dir():
            continue
        shutil.rmtree(partial, ignore_errors=True)
        removed.append(partial.name)
    return removed


def _acquire_backup_lock(backup_root: Path) -> Any:
    import fcntl

    # Lock the directory itself: no extra file appears in the backup root.
    descriptor = os.open(backup_root, os.O_RDONLY | os.O_DIRECTORY)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(descriptor)
        return None
    return descriptor


def run_backup(
    *,
    root: Path,
    config_dir: Path,
    backup_root: Path,
    eimemory_bin: str,
    keep: int,
    now: datetime | None = None,
) -> dict[str, Any]:
    instant = now if now is not None else datetime.now(timezone.utc)
    if instant.utcoffset() is None:
        instant = instant.replace(tzinfo=timezone.utc)
    stamp = instant.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    state_dir = root / "state"
    backup_root.mkdir(parents=True, exist_ok=True)
    os.chmod(backup_root, 0o700)
    lock = _acquire_backup_lock(backup_root)
    if lock is None:
        return {"ok": False, "reason": "backup_already_running", "path": str(backup_root)}
    _sweep_orphan_partials(backup_root)
    try:
        work = backup_root / f".tmp-{stamp}"
        final = backup_root / stamp
        if final.exists():
            return {"ok": False, "reason": "backup_set_exists", "path": str(final)}
        work.mkdir(mode=0o700)
        summary: dict[str, Any] = {"schema": SCHEMA, "created_at": stamp, "root": str(root)}
        try:
            (work / "sqlite").mkdir()
            sqlite_reports = [
                _backup_sqlite(source, work / "sqlite" / source.name)
                for source in _sqlite_sources(state_dir)
            ]
            summary["sqlite"] = sqlite_reports
            _copy_snapshot_payloads(work / "sqlite", state_dir / "payload_segments")
            summary["payload_archive"] = _verify_snapshot_payloads(work / "sqlite")
            env = {
                **{k: v for k, v in os.environ.items() if k in {"PATH", "HOME", "LANG", "LC_ALL", "TZ"}},
                "EIMEMORY_ROOT": str(root),
                "EIMEMORY_CONFIG_DIR": str(config_dir),
                "PYTHONDONTWRITEBYTECODE": "1",
            }
            # An existing directory selects the helper's directory mode
            # (records/backup.jsonl + records/backup.manifest.json).
            (work / "records").mkdir()
            created = _run_cli(eimemory_bin, ["backup", "create", str(work / "records")], env, 1800)
            verified = _run_cli(eimemory_bin, ["backup", "verify", str(work / "records" / "backup")], env, 1800)
            verify_payload = verified["payload"]
            summary["records"] = {
                "ok": created["exit_code"] == 0 and verified["exit_code"] == 0 and verify_payload.get("ok") is True,
                "create_exit_code": created["exit_code"],
                "verify_exit_code": verified["exit_code"],
                "record_count": int(verify_payload.get("record_count") or 0),
                "sha256": str(verify_payload.get("sha256") or ""),
            }
            summary["state_files"] = _copy_state_files(root, state_dir, work / "state")
            summary["config"] = _archive_config(config_dir, work / "config.tar.gz")
            checks_ok = (
                bool(sqlite_reports)
                and all(item["ok"] for item in sqlite_reports)
                and summary["records"]["ok"]
                and summary["records"]["record_count"] > 0
                and summary["config"].get("ok") is True
                and summary["payload_archive"].get("ok") is True
            )
            # Recheck after all auxiliary work, before certifying the published set
            # or deleting any previous complete backup.
            summary["payload_archive"] = _verify_snapshot_payloads(work / "sqlite")
            summary["files"] = _file_inventory(work)
            summary["total_bytes"] = sum(item["bytes"] for item in summary["files"])
            summary["ok"] = checks_ok
            (work / "backup-set.json").write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            if not checks_ok:
                failed = backup_root / f".tmp-{stamp}-failed"
                work.rename(failed)
                return {**_public(summary), "path": str(failed)}
            _sync_backup_tree(work)
            work.rename(final)
            if os.name == "posix":
                descriptor = os.open(backup_root, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
        except BaseException:  # includes SIGTERM (see _terminate_as_exit) and Ctrl-C
            shutil.rmtree(work, ignore_errors=True)
            raise
        removed = _prune(backup_root, keep, stamp)
        return {**_public(summary), "path": str(final), "pruned": removed}
    finally:
        os.close(lock)


def _public(summary: dict[str, Any]) -> dict[str, Any]:
    return {
        "ok": summary.get("ok") is True,
        "schema": SCHEMA,
        "created_at": summary.get("created_at"),
        "sqlite": [
            {k: item[k] for k in ("file", "ok", "integrity_check", "records_rows", "seconds")}
            for item in summary.get("sqlite") or []
        ],
        "records": summary.get("records"),
        "payload_archive": summary.get("payload_archive"),
        "state_file_count": len(summary.get("state_files") or []),
        "config": summary.get("config"),
        "file_count": len(summary.get("files") or []),
        "total_bytes": summary.get("total_bytes"),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", default=os.environ.get("EIMEMORY_ROOT", "/var/lib/eimemory"))
    parser.add_argument("--config-dir", default=os.environ.get("EIMEMORY_CONFIG_DIR", "/etc/eimemory"))
    parser.add_argument("--backup-root", default="")
    parser.add_argument("--eimemory-bin", default="/opt/eimemory/current/.venv/bin/eimemory")
    parser.add_argument("--keep", type=int, default=int(os.environ.get("EIMEMORY_BACKUP_KEEP") or 5))
    args = parser.parse_args(argv)
    _install_sigterm_unwind()
    try:
        os.umask(0o077)
        root = Path(args.root).resolve()
        backup_root = Path(args.backup_root).resolve() if args.backup_root else root / "backups"
        report = run_backup(
            root=root,
            config_dir=Path(args.config_dir),
            backup_root=backup_root,
            eimemory_bin=args.eimemory_bin,
            keep=max(1, min(60, int(args.keep))),
        )
    except Exception as exc:
        report = {"ok": False, "reason": "backup_failed", "error_type": type(exc).__name__[:80]}
    print(json.dumps(report, sort_keys=True))
    return 0 if report.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
