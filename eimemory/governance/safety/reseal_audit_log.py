#!/usr/bin/env python3
"""Reseal a legacy audit log into the sha256 hash-chain format.

Problem this solves:
    ``audit.jsonl`` contains rows written before the hash chain was
    introduced (plain ``{"event": ..., "at"/"ts": ...}`` rows).  The
    hourly ``audit_verifier`` reads row 0, sees an empty ``prev_hash``
    and raises ChainBroken -> emergency_stop() kills every eimemory
    process.  This has fired 62+ times.

What resealing does (append-preserving, tamper-evident):
    * Every legacy row is kept VERBATIM inside the new chain as the
      payload of a wrapper row of kind ``legacy_import``, together with
      the original line number for forensics.
    * A first row of kind ``reseal_baseline`` records why/when/by whom
      the log was resealed and the sha256 of the original file.
    * The rewritten file is verified with :meth:`AuditLog.verify`
      BEFORE replacing the original; the original is rotated to
      ``<path>.pre-reseal-<epoch>`` (never deleted).
    * All writes happen under the same exclusive file lock used by
      :meth:`AuditLog.append`.

Usage:
    python -m eimemory.governance.safety.reseal_audit_log PATH [--apply]

    Default is dry-run: prints what would be done.  ``--apply`` performs
    the atomic rewrite.  Run under any user that can write the file.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from eimemory.governance.safety.audit import AuditLog, ChainBroken
from eimemory.governance.safety.file_lock import exclusive_file_lock


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _is_chained(row: dict) -> bool:
    # A row written by AuditLog.append always carries a 64-hex row_hash.
    # prev_hash is "" for a legitimate row_index==0 root, so it is not
    # part of the chained-ness test; row_index presence is.
    return (
        isinstance(row.get("row_index"), int)
        and isinstance(row.get("row_hash"), str)
        and len(row.get("row_hash", "")) == 64
    )


def plan(path: Path) -> tuple[list[dict], list[str]]:
    """Return (legacy_rows_in_order, notes). Raises on mixed/corrupt input."""
    legacy: list[dict] = []
    chained_seen = False
    notes: list[str] = []
    with path.open("r", encoding="utf-8") as f:
        for lineno, line in enumerate(f, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise SystemExit(
                    f"ABORT: line {lineno} is not valid JSON ({exc}); "
                    "manual inspection required"
                )
            if _is_chained(row):
                chained_seen = True
                continue
            legacy.append({"_lineno": lineno, **row})
    if chained_seen and legacy:
        raise SystemExit(
            "ABORT: file mixes already-chained rows with legacy rows; "
            "this tool only converts fully-legacy logs"
        )
    if chained_seen and not legacy:
        raise SystemExit("Nothing to do: log is already fully chained")
    notes.append(f"{len(legacy)} legacy rows will be wrapped into the new chain")
    return legacy, notes


def reseal(path: Path, operator: str) -> Path:
    """Rewrite ``path`` as a fresh hash chain wrapping all legacy rows."""
    original_sha = _sha256_file(path)
    legacy, _notes = plan(path)

    now = datetime.now(timezone.utc).isoformat()

    # 1) Preserve the original file verbatim (copy — the live path is
    #    replaced later; the caller gets this backup back).
    import shutil

    backup = path.with_name(f"{path.name}.pre-reseal-{int(datetime.now(timezone.utc).timestamp())}")
    shutil.copy2(path, backup)

    # 2) Build the new chain in a staging location.
    staging = path.with_name(f".{path.name}.reseal-staging")
    if staging.exists():
        staging.unlink()
    log = AuditLog(staging)
    log.append({
        "event": "reseal_baseline",
        "reason": "legacy pre-chain rows caused hourly false ChainBroken + emergency_stop",
        "original_file_sha256": original_sha,
        "original_row_count": len(legacy),
        "operator": operator,
        "tool": "eimemory.governance.safety.reseal_audit_log",
        "ts": now,
    })
    for item in legacy:
        lineno = item.pop("_lineno")
        log.append({
            "event": "legacy_import",
            "original_line_number": lineno,
            "payload": item,
        })

    # 3) Verify the NEW chain before touching the original.
    try:
        AuditLog(staging).verify()
    except ChainBroken as exc:  # pragma: no cover - defensive
        raise SystemExit(f"ABORT: freshly built chain failed verification: {exc}")

    # 4) Swap atomically under the same lock family the appender uses.
    lock_path = path.with_suffix(path.suffix + ".lock")
    with exclusive_file_lock(lock_path):
        os.replace(staging, path)
    return backup


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", type=Path, help="audit.jsonl to reseal")
    parser.add_argument("--apply", action="store_true", help="perform the rewrite (default dry-run)")
    args = parser.parse_args(argv)

    path = args.path.resolve()
    if not path.exists():
        print(f"no such file: {path}", file=sys.stderr)
        return 2

    legacy, notes = plan(path)
    for n in notes:
        print(f"[dry-run] {n}")
    print(f"[dry-run] original sha256: {_sha256_file(path)}")
    if not args.apply:
        print("[dry-run] re-run with --apply to perform the rewrite")
        return 0

    operator = f"{os.uname().nodename}:{os.getuid()}"
    backup = reseal(path, operator)
    print(f"resealed OK; original preserved at {backup}")

    # Post-condition: the live file must now verify clean.
    try:
        AuditLog(path).verify()
    except ChainBroken as exc:
        print(f"UNEXPECTED: live log still fails verify: {exc}", file=sys.stderr)
        return 1
    print("post-reseal verify: OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
