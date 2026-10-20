"""Early accidental-write guard and journal attribution for immutable releases.

The systemd read-only mount is the OS boundary. Python audit hooks are NOT a
sandbox against hostile Python/native code, other processes, or privileged
administrators. This hook covers the process that imports the release package.
"""
from __future__ import annotations

import errno
import json
import os
from pathlib import Path
import re
import sys
import threading

_installed: set[str] = set()
_guard_lock = threading.Lock()


def immutable_release_root(package_file: str) -> Path | None:
    root = Path(package_file).resolve().parent.parent
    return root if root.parent.name == 'releases' and re.fullmatch('[0-9a-fA-F]{40}', root.name) else None


def install_release_write_guard(root: Path) -> None:
    root = root.resolve(strict=True)
    key = str(root)
    with _guard_lock:
        if key in _installed:
            return
        _installed.add(key)
    local = threading.local()
    logged = 0

    def inside(value, dir_fd=None) -> str | None:
        if not isinstance(value, (str, bytes, os.PathLike)):
            return None
        try:
            raw = os.fsdecode(value)
            if not os.path.isabs(raw):
                parent = os.getcwd()
                if isinstance(dir_fd, int) and dir_fd >= 0 and sys.platform.startswith('linux'):
                    parent = os.readlink(f'/proc/self/fd/{dir_fd}')
                raw = os.path.join(parent, raw)
            path = os.path.abspath(raw)
            # Check lexical AND canonical names. Canonical catches plugin links;
            # lexical catches replacement/unlink of links within the release.
            for candidate in (path, os.path.realpath(path)):
                if os.path.commonpath((candidate, key)) == key:
                    return os.path.relpath(path, key)[:1024]
        except (OSError, TypeError, ValueError):
            return None
        return None

    def audit(event, args):
        nonlocal logged
        if getattr(local, 'active', False):
            return
        target = None
        local.active = True
        try:
            if event == 'open' and len(args) >= 3:
                mode, flags = args[1:3]
                writing = isinstance(mode, str) and any(x in mode for x in 'wax+')
                writing = writing or isinstance(flags, int) and bool(flags & (
                    os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND
                ))
                if writing:
                    # CPython's open audit event is (path, mode, flags). It does
                    # not include dir_fd, so a relative name is judged against cwd.
                    target = inside(args[0])
            elif event in {'os.mkdir', 'os.remove', 'os.rmdir', 'os.chmod', 'os.chown', 'os.utime', 'os.truncate'}:
                target = inside(args[0], args[-1] if len(args) > 1 else None)
            elif event in {'os.rename', 'os.link'}:
                target = inside(args[0], args[2] if len(args) > 2 else None)
                target = target or inside(args[1], args[3] if len(args) > 3 else None)
            elif event == 'os.symlink':
                target = inside(args[1], args[2] if len(args) > 2 else None)
            if target is None:
                return
            if logged < 8:
                logged += 1
                frames = []
                frame = sys._getframe(1)
                while frame is not None and len(frames) < 8:
                    frames.append({'file': frame.f_code.co_filename[-512:], 'line': frame.f_lineno})
                    frame = frame.f_back
                record = {
                    'event': 'eimemory_release_write_denied', 'pid': os.getpid(),
                    'ppid': os.getppid(), 'executable': sys.executable,
                    'release_commit': root.name, 'operation': event,
                    'relative_path': target, 'stack': frames,
                    'evidence_kind': 'blocked_write_attempt_not_historical_attribution',
                }
                try:
                    os.write(2, (json.dumps(record, ensure_ascii=True) + '\n').encode())
                except OSError:
                    pass  # Logging failure never permits the write.
            raise PermissionError(errno.EROFS, 'immutable_release_write_denied', target)
        finally:
            local.active = False

    sys.dont_write_bytecode = True
    sys.addaudithook(audit)


def open_new_file_at_directory(directory_fd: int, name: str, flags: int, mode: int = 0o600) -> int:
    """Create one file in directory_fd without looking like a release write.

    The open audit event omits dir_fd. A validation child whose cwd is the
    immutable release must not present a relative name, or the guard treats an
    external artifact as a release write. Move cwd to the external directory
    only for that audited open, then restore it. The kernel open still uses
    dir_fd. A destination inside a guarded release is left unchanged so the
    guard still denies it.
    """
    if isinstance(directory_fd, bool) or not isinstance(directory_fd, int) or directory_fd < 0:
        raise ValueError('release_guard_directory_fd_invalid')
    if (not isinstance(name, str) or not name or name in {'.', '..'}
            or any(ord(char) in {0, 47, 92} for char in name)):
        raise ValueError('release_guard_relative_name_invalid')
    held = None
    try:
        if _relative_open_would_be_misread(directory_fd):
            held = os.open('.', os.O_RDONLY | os.O_DIRECTORY)
            os.fchdir(directory_fd)
        return os.open(name, flags, mode, dir_fd=directory_fd)
    finally:
        if held is not None:
            try:
                os.fchdir(held)
            finally:
                os.close(held)


def _relative_open_would_be_misread(directory_fd: int) -> bool:
    if not sys.platform.startswith('linux'):
        return False
    try:
        cwd = os.path.realpath(os.getcwd())
        target = os.path.realpath(os.readlink(f'/proc/self/fd/{directory_fd}'))
    except OSError:
        return False
    with _guard_lock:
        roots = tuple(_installed)
    for root in roots:
        try:
            cwd_inside = os.path.commonpath((cwd, root)) == root
            target_inside = os.path.commonpath((target, root)) == root
        except ValueError:
            continue
        if cwd_inside and not target_inside:
            return True
    return False
