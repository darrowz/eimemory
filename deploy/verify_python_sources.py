#!/usr/bin/env python3
"""Compile source bytes in memory: no imports, pyc files, or cleanup."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import stat
import sys

sys.dont_write_bytecode = True


def verify(root: Path, *, max_entries: int = 20_000, max_bytes: int = 512 * 1024 * 1024) -> dict:
    root = root.resolve(strict=True)
    if not root.is_dir() or max_entries <= 0 or max_bytes <= 0:
        raise ValueError('invalid_syntax_check_parameters')
    scanned = compiled = total = 0
    stack = [root]
    while stack:
        directory = stack.pop()
        with os.scandir(directory) as iterator:
            for entry in iterator:
                scanned += 1
                if scanned > max_entries:
                    raise ValueError('source_entry_limit_exceeded')
                path = Path(entry.path)
                if entry.is_symlink():
                    raise ValueError('source_symlink_not_allowed')
                if entry.is_dir(follow_symlinks=False):
                    if entry.name == '__pycache__':
                        raise ValueError('source_bytecode_pollution')
                    stack.append(path)
                    continue
                if path.suffix in {'.pyc', '.pyo'}:
                    raise ValueError('source_bytecode_pollution')
                if path.suffix != '.py':
                    continue
                fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) |
                             getattr(os, 'O_NONBLOCK', 0) | getattr(os, 'O_CLOEXEC', 0))
                with os.fdopen(fd, 'rb') as handle:
                    before = os.fstat(handle.fileno())
                    if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
                        raise ValueError('source_must_be_single_link_regular_file')
                    if before.st_size > max_bytes - total:
                        raise ValueError('source_byte_limit_exceeded')
                    raw = handle.read(max_bytes - total + 1)
                    after = os.fstat(handle.fileno())
                fields = ('st_dev', 'st_ino', 'st_size', 'st_mtime_ns', 'st_ctime_ns')
                if len(raw) != before.st_size or any(getattr(before, k) != getattr(after, k) for k in fields):
                    raise ValueError('source_changed_during_read')
                if (path.lstat().st_dev, path.lstat().st_ino) != (after.st_dev, after.st_ino):
                    raise ValueError('source_changed_during_read')
                total += len(raw)
                if total > max_bytes:
                    raise ValueError('source_byte_limit_exceeded')
                compile(raw, str(path), 'exec', dont_inherit=True)
                compiled += 1
    if compiled == 0:
        raise ValueError('no_python_sources')
    return {'ok': True, 'schema': 'python_source_syntax.v1', 'compiled_count': compiled,
            'bytes': total, 'code_executed': False, 'bytecode_written': False}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        result = verify(args.root)
    except (OSError, ValueError, SyntaxError) as exc:
        result = {'ok': False, 'error_type': type(exc).__name__}
        if isinstance(exc, SyntaxError):
            result['file'] = str(exc.filename)
            result['line'] = exc.lineno
    print(json.dumps(result, ensure_ascii=True))
    return 0 if result['ok'] else 2


if __name__ == '__main__':
    raise SystemExit(main())
