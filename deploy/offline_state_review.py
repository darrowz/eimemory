#!/usr/bin/env python3
"""Review a standalone offline SQLite backup. Never rename, delete, or repair data."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import sys
from time import monotonic
from urllib.parse import quote

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from eimemory.storage.atomic_file import open_regular_binary


def require_real_directory_chain(path: Path) -> None:
    """Reject linked ancestors; caller must own the tree with no competing writers."""
    import stat
    absolute = path.absolute()
    for directory in reversed((absolute, *absolute.parents)):
        info = directory.lstat()
        if (not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode)
                or getattr(info, "st_file_attributes", 0) & 0x400):
            raise ValueError("linked_or_non_directory_ancestor")


@contextmanager
def open_snapshot(path: Path, *, max_seconds: float = 30):
    if type(max_seconds) not in (int, float) or not 0 < max_seconds <= 3600:
        raise ValueError("invalid_review_budget")
    # immutable=1 is safe only for an operator-created, quiescent standalone
    # backup. Refuse sidecars; do not pretend to take a live SQLite snapshot.
    if path.is_symlink():
        raise ValueError("snapshot_must_not_be_symlink")
    require_real_directory_chain(path.parent)
    path = path.absolute()
    if any(os.path.lexists(str(path) + suffix) for suffix in ("-wal", "-shm", "-journal")):
        raise ValueError("standalone_offline_backup_required")
    with open_regular_binary(path) as handle:
        if handle.read(16) != b"SQLite format 3\x00":
            raise ValueError("not_sqlite_database")
        before = os.fstat(handle.fileno())
    uri = "file:" + quote(path.as_posix(), safe="/:") + "?mode=ro&immutable=1"
    connection = sqlite3.connect(uri, uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    deadline = monotonic() + max_seconds
    connection.set_progress_handler(lambda: int(monotonic() > deadline), 10000)
    try:
        yield connection
    finally:
        connection.close()
        if any(os.path.lexists(str(path) + suffix) for suffix in ("-wal", "-shm", "-journal")):
            raise ValueError("snapshot_sidecar_appeared_during_review")
        after = path.stat()
        if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
            after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns):
            raise ValueError("snapshot_changed_during_review")


def review(snapshot: Path, *, max_seconds: float = 30, max_groups: int = 100) -> dict:
    if type(max_groups) is not int or not 1 <= max_groups <= 1000:
        raise ValueError("invalid_group_limit")
    with open_snapshot(snapshot, max_seconds=max_seconds) as connection:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(records)")}
        required = {"record_id", "tenant_id", "agent_id", "workspace_id", "user_id", "source_id",
                    "kind", "status", "semantic_key"}
        if not required.issubset(columns):
            raise ValueError("unsupported_records_schema")
        invalid = connection.execute(
            "SELECT COUNT(*) FROM records WHERE " + " OR ".join(
                f"instr({name}, char(31)) > 0" for name in ("tenant_id", "agent_id", "workspace_id", "user_id", "record_id")
            )
        ).fetchone()[0]
        rows = connection.execute(
            "SELECT tenant_id,agent_id,workspace_id,user_id,source_id,semantic_key,COUNT(*) AS active_count "
            "FROM records WHERE kind='memory' AND status='active' AND semantic_key<>'' "
            "GROUP BY tenant_id,agent_id,workspace_id,user_id,source_id,semantic_key "
            "HAVING COUNT(*)>10000 ORDER BY active_count DESC LIMIT ?", (max_groups + 1,)
        ).fetchall()
        active_rules = connection.execute("SELECT COUNT(*) FROM records WHERE kind='rule' AND status='active'").fetchone()[0]
    groups = []
    for row in rows[:max_groups]:
        # No private semantic key or user identity is printed by default.
        identity = [row[key] for key in ("tenant_id", "agent_id", "workspace_id", "user_id", "source_id", "semantic_key")]
        digest = hashlib.sha256(json.dumps(identity, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()
        groups.append({"group_sha256": digest, "active_count": row["active_count"], "requires_reviewed_offline_plan": True})
    return {"schema": "offline_state_review.v1", "scan_complete": True,
            "reserved_separator_record_count": invalid, "overlimit_groups": groups,
            "groups_truncated": len(rows) > max_groups, "active_rule_count": active_rules,
            "active_rule_authority_verified": False, "repair_performed": False,
            "production_acceptance": "not_assessed",
            "requires_data_review": bool(invalid or rows)}


def projection_layout(root: Path) -> dict:
    legacy, current, links = [], [], []
    if not root.is_dir() or root.is_symlink():
        raise ValueError("projection_root_must_be_real_directory")
    with os.scandir(root) as entries:
        for index, entry in enumerate(entries):
            if index >= 200_000:
                raise ValueError("projection_entry_limit_exceeded")
            if entry.is_symlink():
                links.append(entry.name)
            elif entry.is_dir(follow_symlinks=False):
                if re.fullmatch(r"[0-9a-f]{12}", entry.name):
                    legacy.append(entry.name)
                elif re.fullmatch(r"v2-[0-9a-f]{64}", entry.name):
                    current.append(entry.name)
    return {"legacy_partition_count": len(legacy), "v2_partition_count": len(current),
            "symlink_count": len(links), "mixed_generations": bool(legacy and current),
            "migration_performed": False, "index_switch_performed": False}


def write_new_report(path: Path, report: dict) -> None:
    """Publish a new private report exclusively, never overwrite an artifact."""
    from eimemory.storage.private_file import private_temporary_file

    raw = (json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode("utf-8")
    require_real_directory_chain(path.parent)
    fd, name = private_temporary_file(prefix=f".{path.name}.", directory=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        # Hard-link publication is exclusive even if another writer races us.
        # The temporary is private before bytes are written on both platforms.
        os.link(temporary, path, follow_symlinks=False)
    finally:
        temporary.unlink(missing_ok=True)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--offline-snapshot", required=True, type=Path)
    parser.add_argument("--projection-root", type=Path)
    parser.add_argument("--max-seconds", type=float, default=30)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    try:
        report = review(args.offline_snapshot, max_seconds=args.max_seconds)
        if args.projection_root:
            report["projection"] = projection_layout(args.projection_root)
        if args.output:
            write_new_report(args.output, report)
        else:
            print(json.dumps(report, ensure_ascii=False, sort_keys=True, allow_nan=False))
        flagged = report["requires_data_review"] or report.get("projection", {}).get("mixed_generations") or report.get("projection", {}).get("symlink_count")
        return 1 if flagged else 0
    except (OSError, ValueError, sqlite3.Error) as exc:
        print(json.dumps({"scan_complete": False, "error_type": type(exc).__name__,
                          "repair_performed": False, "production_acceptance": "not_assessed"}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
