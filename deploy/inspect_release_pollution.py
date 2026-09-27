#!/usr/bin/env python3
"""Inventory bytecode without importing, cleaning or modifying a release.

This is a collection tool, not an acceptance gate. It cannot attribute a past
write to a process from timestamps alone. Parent directories must be trusted.
"""
from __future__ import annotations

import argparse
import errno
import hashlib
import json
import os
from pathlib import Path
import stat
import sys
from datetime import datetime, timezone

sys.dont_write_bytecode = True
SAFE_ENV = frozenset({"PYTHONDONTWRITEBYTECODE", "PYTHONPYCACHEPREFIX", "PYTHONHOME",
                      "PYTHONPATH", "EIMEMORY_RUNTIME_COMMIT", "EIMEMORY_RUNTIME_RELEASE_DIR"})


def _stat(info):
    return {"device": info.st_dev, "inode": info.st_ino, "size": info.st_size,
            "mtime_ns": info.st_mtime_ns, "ctime_ns": info.st_ctime_ns,
            "uid": info.st_uid, "gid": info.st_gid, "mode": oct(stat.S_IMODE(info.st_mode)),
            "link_count": info.st_nlink}


def _digest_file(path: Path, *, maximum: int) -> dict:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_CLOEXEC", 0)
    noatime = getattr(os, "O_NOATIME", 0)
    atime_preserved = bool(noatime)
    try:
        descriptor = os.open(path, flags | noatime)
    except OSError as exc:
        if not noatime or exc.errno not in {errno.EPERM, errno.EINVAL, errno.EOPNOTSUPP}:
            raise
        descriptor = os.open(path, flags)
        atime_preserved = False
    with os.fdopen(descriptor, "rb") as handle:
        before = os.fstat(handle.fileno())
        if not stat.S_ISREG(before.st_mode) or before.st_size > maximum:
            raise ValueError("nonregular_or_oversized_bytecode")
        digest = hashlib.sha256()
        total, header = 0, b""
        while block := handle.read(min(1024 * 1024, maximum + 1 - total)):
            if not header:
                header = block[:16]
            total += len(block)
            if total > maximum:
                raise ValueError("bytecode_grew_past_limit")
            digest.update(block)
        after = os.fstat(handle.fileno())
        stable = _stat(before) == _stat(after) and total == after.st_size
        named = path.lstat()
        stable = stable and (after.st_dev, after.st_ino) == (named.st_dev, named.st_ino)
    return {**_stat(after), "sha256": digest.hexdigest(), "header_hex": header.hex(),
            "stable_during_read": stable, "noatime_requested": atime_preserved}


def _process(pid: int) -> dict:
    result = {"pid": pid, "attribution": "configuration_only_not_proof_of_write"}
    root = Path('/proc') / str(pid)
    try:
        result['executable'] = os.readlink(root / 'exe')
        result['cwd'] = os.readlink(root / 'cwd')
        with (root / 'environ').open('rb') as handle:
            raw = handle.read(1024 * 1024 + 1)
        if len(raw) > 1024 * 1024:
            raise ValueError('process_environment_too_large')
        environment = {}
        for item in raw.split(b'\0'):
            key, _, value = item.partition(b'=')
            name = key.decode('ascii', errors='replace')
            if name in SAFE_ENV:
                environment[name] = value.decode('utf-8', errors='replace')[:4096]
        result['selected_environment'] = environment
    except (OSError, ValueError) as exc:
        result['error_type'] = type(exc).__name__
    return result


def inspect(root: Path, *, exclude: tuple[str, ...] = ('.venv', '.git'),
            max_entries: int = 200_000, max_file_bytes: int = 32 * 1024 * 1024,
            pids: tuple[int, ...] = ()) -> dict:
    root = root.resolve(strict=True)
    if not root.is_dir() or max_entries < 1 or max_file_bytes < 1:
        raise ValueError('invalid_inventory_parameters')
    entries, errors, count = [], [], 0
    pending = [root]
    while pending:
        directory = pending.pop()
        try:
            with os.scandir(directory) as iterator:
                for entry in iterator:
                    count += 1
                    if count > max_entries:
                        errors.append({'reason': 'entry_limit_reached'})
                        pending.clear()
                        break
                    path = Path(entry.path)
                    relative = path.relative_to(root).as_posix()
                    if directory == root and entry.name in exclude:
                        continue
                    if entry.is_symlink():
                        if path.suffix in {'.pyc', '.pyo'}:
                            entries.append({'path': relative, 'symlink': True})
                        continue
                    if entry.is_dir(follow_symlinks=False):
                        pending.append(path)
                    elif path.suffix in {'.pyc', '.pyo'}:
                        try:
                            row = _digest_file(path, maximum=max_file_bytes)
                            entries.append({'path': relative, **row})
                            if not row['stable_during_read']:
                                errors.append({'path': relative, 'reason': 'file_changed_during_read'})
                        except (OSError, ValueError) as exc:
                            errors.append({'path': relative, 'error_type': type(exc).__name__})
        except OSError as exc:
            errors.append({'path': directory.relative_to(root).as_posix(), 'error_type': type(exc).__name__})
    return {
        'schema': 'release_bytecode_inventory.v1',
        'collected_at': datetime.now(timezone.utc).isoformat(),
        'release_dir': str(root), 'excluded_root_names': list(exclude),
        'inventory_complete': not errors, 'scanned_entries': count,
        'bytecode_count': len(entries), 'entries': sorted(entries, key=lambda r: r['path']),
        'errors': errors, 'processes': [_process(pid) for pid in pids],
        'writer_attribution': 'not_established', 'tracked_source_verification': 'not_performed',
        'scene_cleanup_performed': False,
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--release-dir', required=True, type=Path)
    parser.add_argument('--output', type=Path, help='new 0600 file outside the release; default stdout')
    parser.add_argument('--pid', action='append', type=int, default=[])
    parser.add_argument('--max-entries', type=int, default=200_000)
    args = parser.parse_args(argv)
    try:
        root = args.release_dir.resolve(strict=True)
        if args.output:
            output = args.output.absolute()
            if output.resolve().is_relative_to(root):
                raise ValueError('output_must_be_outside_release')
        if any(pid <= 0 for pid in args.pid):
            raise ValueError('PID_must_be_positive')
        report = inspect(root, max_entries=args.max_entries, pids=tuple(args.pid))
        payload = (json.dumps(report, ensure_ascii=True, indent=2, allow_nan=False) + '\n').encode()
        if args.output:
            fd = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0), 0o600)
            with os.fdopen(fd, 'wb') as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
        else:
            sys.stdout.write(payload.decode())
        return 0 if report['inventory_complete'] else 2
    except (OSError, ValueError) as exc:
        parser.exit(2, f'inventory_failed:{type(exc).__name__}:{exc}\n')


if __name__ == '__main__':
    raise SystemExit(main())
