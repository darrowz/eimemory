#!/usr/bin/env python3
"""Scheduled, verified, restorable backup set for one eimemory runtime.

Each run writes one complete set under ``<root>/backups/<UTC timestamp>/``:

* ``sqlite/``   online SQLite backups (``sqlite3.Connection.backup``) of every
  authoritative database in ``<root>/state``; each copy must pass
  ``PRAGMA integrity_check`` and is re-opened to count rows.
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
        finally:
            dst.close()
    finally:
        src.close()
    check = sqlite3.connect(f"file:{target}?mode=ro", uri=True)
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
        )
        summary["files"] = _file_inventory(work)
        summary["total_bytes"] = sum(item["bytes"] for item in summary["files"])
        summary["ok"] = checks_ok
        (work / "backup-set.json").write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        if not checks_ok:
            failed = backup_root / f".tmp-{stamp}-failed"
            work.rename(failed)
            return {**_public(summary), "path": str(failed)}
        work.rename(final)
    except Exception:
        shutil.rmtree(work, ignore_errors=True)
        raise
    removed = _prune(backup_root, keep, stamp)
    return {**_public(summary), "path": str(final), "pruned": removed}


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
    parser.add_argument("--keep", type=int, default=5)
    args = parser.parse_args(argv)
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
