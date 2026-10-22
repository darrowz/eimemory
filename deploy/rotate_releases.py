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
import stat
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


def _identity(info):
    return (info.st_dev, info.st_ino, info.st_uid, info.st_gid, info.st_mode,
            info.st_nlink, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def remove_owned_directory(path: Path, root: Path, *, expected, protected: set[str]) -> None:
    """Retire a sealed, unprotected artifact without following source links.

    Preflight the complete tree before changing any mode. Keep only recursion
    depth descriptors open, including for large dependency environments, then
    recheck each named inode before removal. Only the proved artifact owner's
    directories gain owner-write; files and symlink targets never change mode.
    """
    uid = os.geteuid()
    if uid <= 0 or uid != os.getuid():
        raise OSError("rotation_nonroot_owner_required")
    root = Path(root)
    absolute = Path(os.path.abspath(root))
    path = Path(path)
    if (root.resolve(strict=True) != absolute or path.name.split('-', 1)[0] in protected
            or Path(os.path.abspath(path)).parent != absolute
            or not (_RELEASE.fullmatch(path.name) or _SNAPSHOT.fullmatch(path.name))):
        raise OSError("rotation_target_untrusted_or_protected")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    root_fd = os.open(root, flags)

    def owned(info):
        if info.st_uid != uid or not (stat.S_ISDIR(info.st_mode)
                or stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode)):
            raise OSError("rotation_foreign_or_special_entry")

    def open_child(parent, name, original):
        descriptor = os.open(name, flags, dir_fd=parent)
        try:
            if (_identity(os.fstat(descriptor)) != original
                    or _identity(os.stat(name, dir_fd=parent, follow_symlinks=False)) != original):
                raise OSError("rotation_directory_changed")
        except BaseException:
            os.close(descriptor)
            raise
        return descriptor

    def inspect(directory):
        children = {}
        for name in os.listdir(directory):
            info = os.stat(name, dir_fd=directory, follow_symlinks=False)
            owned(info)
            original = _identity(info)
            descendants = None
            if stat.S_ISDIR(info.st_mode):
                child = open_child(directory, name, original)
                try:
                    descendants = inspect(child)
                finally:
                    os.close(child)
            children[name] = (original, descendants)
        return children

    def remove(directory, children):
        info = os.fstat(directory)
        owned(info)
        if set(os.listdir(directory)) != set(children):
            raise OSError("rotation_inventory_changed")
        if not info.st_mode & stat.S_IWUSR:
            os.fchmod(directory, stat.S_IMODE(info.st_mode) | stat.S_IWUSR)
        for name, (original, descendants) in children.items():
            if _identity(os.stat(name, dir_fd=directory, follow_symlinks=False)) != original:
                raise OSError("rotation_entry_changed")
            if descendants is not None:
                child = open_child(directory, name, original)
                try:
                    remove(child, descendants)
                    # Descendant deletion changes times/link counts. Verify
                    # the held directory is still the named directory itself.
                    if _identity(os.fstat(child)) != _identity(os.stat(name,
                            dir_fd=directory, follow_symlinks=False)):
                        raise OSError("rotation_directory_changed")
                finally:
                    os.close(child)
                os.rmdir(name, dir_fd=directory)
            else:
                os.unlink(name, dir_fd=directory)

    try:
        owned(os.fstat(root_fd))
        directory = open_child(root_fd, path.name, expected)
        try:
            owned(os.fstat(directory))
            inventory = inspect(directory)
            if _identity(os.fstat(directory)) != expected:
                raise OSError("rotation_directory_changed")
            remove(directory, inventory)
            if _identity(os.fstat(directory)) != _identity(os.stat(path.name,
                    dir_fd=root_fd, follow_symlinks=False)):
                raise OSError("rotation_directory_changed")
        finally:
            os.close(directory)
        os.rmdir(path.name, dir_fd=root_fd)
    finally:
        os.close(root_fd)


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
        targets = []
        for path in plan_rotation(root, pattern, keep=keep, protected=protected):
            try:
                targets.append((path, _identity(path.lstat())))
            except OSError:
                report["ok"] = False
        removed = []
        for path, expected in targets:
            if not args.dry_run:
                try:
                    if os.name == 'posix':
                        remove_owned_directory(path, root, expected=expected, protected=protected)
                    else:
                        # Preserve portable plain-directory rotation. POSIX
                        # source permission repair is never applied to a host.
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
