#!/usr/bin/env python3
"""Rotate immutable releases and storage release-snapshots after a deploy.

Keeps the newest N release directories and newest M release-snapshots, and
always keeps the current release and the rollback target (previous commit).
Only plain directories named like ``<40-hex>`` (releases) or
``<40-hex>-<attempt>`` (snapshots) are candidates; symlinks, staging dirs and
anything else are never touched. ``0`` disables rotation for that kind.

Env: EIMEMORY_KEEP_RELEASES (default 5), EIMEMORY_KEEP_RELEASE_SNAPSHOTS (default 5).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
from pathlib import Path

_RELEASE = re.compile(r"[0-9a-f]{40}")
_SNAPSHOT = re.compile(r"([0-9a-f]{40})-[0-9A-Za-z]{1,64}")


def _candidates(root: Path, pattern: re.Pattern[str]) -> list[tuple[Path, str]]:
    found = []
    if not root.is_dir() or root.is_symlink():
        return found
    for entry in root.iterdir():
        match = pattern.fullmatch(entry.name)
        if match and entry.is_dir() and not entry.is_symlink():
            found.append((entry, match.group(1) if match.groups() else entry.name))
    return sorted(found, key=lambda item: (item[0].lstat().st_mtime, item[0].name), reverse=True)


def plan_rotation(root: Path, pattern: re.Pattern[str], *, keep: int, protected: set[str]) -> list[Path]:
    if keep <= 0:
        return []
    kept = 0
    remove: list[Path] = []
    for path, commit in _candidates(root, pattern):
        if commit in protected:
            continue
        if kept < keep:
            kept += 1
            continue
        remove.append(path)
    return remove


def _keep(name: str, default: int) -> int:
    try:
        return max(0, int(os.environ.get(name, str(default))))
    except ValueError:
        return default


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--releases-root", required=True)
    parser.add_argument("--snapshot-root", required=True)
    parser.add_argument("--current-commit", required=True)
    parser.add_argument("--previous-commit", default="")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    protected = {c.strip().lower() for c in (args.current_commit, args.previous_commit) if _RELEASE.fullmatch(c.strip().lower())}
    if args.current_commit.strip().lower() not in protected:
        print(json.dumps({"ok": False, "reason": "current_commit_invalid"}))
        return 2
    report = {"ok": True, "dry_run": bool(args.dry_run), "protected": sorted(protected), "removed": {}}
    for kind, root, pattern, keep in (
        ("releases", Path(args.releases_root), _RELEASE, _keep("EIMEMORY_KEEP_RELEASES", 5)),
        ("release_snapshots", Path(args.snapshot_root), _SNAPSHOT, _keep("EIMEMORY_KEEP_RELEASE_SNAPSHOTS", 5)),
    ):
        targets = plan_rotation(root, pattern, keep=keep, protected=protected)
        removed = []
        for path in targets:
            if not args.dry_run:
                try:
                    shutil.rmtree(path)
                except OSError:
                    report["ok"] = False
                    continue
            removed.append(path.name)
        report["removed"][kind] = removed
    print(json.dumps(report, sort_keys=True))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
