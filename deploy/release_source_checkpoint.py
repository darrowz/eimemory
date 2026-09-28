#!/usr/bin/env python3
"""Retain stage-local source validation without importing or cleaning a release.

Uses the existing strict source validator. A phase locates the first observed
failure interval, NOT the PID that wrote a historic .pyc. Linux Audit records
or the Python write-denied journal event provide process attribution.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import tempfile

sys.dont_write_bytecode = True


def bytecode_inventory(root: Path, *, maximum: int = 200_000) -> dict:
    count = scanned = 0
    entries, errors = [], []
    for parent, dirs, files in os.walk(root, followlinks=False,
                                       onerror=lambda exc: errors.append(type(exc).__name__)):
        path = Path(parent)
        dirs[:] = [d for d in dirs if not (path / d).is_symlink()
                   and not (path == root and d in {'.venv', '.git'})]
        scanned += len(dirs) + len(files)
        if scanned > maximum:
            errors.append('entry_limit_exceeded')
            break
        for name in files:
            if not name.endswith(('.pyc', '.pyo')):
                continue
            count += 1
            if len(entries) >= 512:
                continue
            file = path / name
            before = file.lstat()
            row = {'path': file.relative_to(root).as_posix(), 'size': before.st_size,
                   'inode': before.st_ino, 'mtime_ns': before.st_mtime_ns, 'ctime_ns': before.st_ctime_ns}
            # Metadata alone suffices for phase correlation. Do not execute,
            # deserialize, delete or silently read a link target.
            row['regular_file'] = stat.S_ISREG(before.st_mode)
            entries.append(row)
    return {'bytecode_count': count, 'entries': entries, 'inventory_errors': errors,
            'entry_details_truncated': count > len(entries)}


def _write_report(directory: Path, report: dict, releases_root: Path) -> str:
    absolute = directory.absolute()
    if absolute.resolve().is_relative_to(releases_root.resolve()):
        raise ValueError('checkpoint_output_must_be_outside_releases')
    if absolute.resolve() != absolute:
        raise ValueError('checkpoint_output_path_must_not_use_symlinks')
    absolute.mkdir(parents=True, exist_ok=True, mode=0o700)
    meta = absolute.lstat()
    if not stat.S_ISDIR(meta.st_mode) or (os.name == 'posix' and meta.st_mode & 0o022):
        raise ValueError('checkpoint_output_directory_unsafe')
    payload = (json.dumps(report, ensure_ascii=True, indent=2, allow_nan=False) + '\n').encode()
    fd, filename = tempfile.mkstemp(prefix=report['phase'] + '-', suffix='.json', dir=absolute)
    with os.fdopen(fd, 'wb') as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    if os.name == 'posix':
        directory_fd = os.open(absolute, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    return filename


def checkpoint(*, release: Path, releases_root: Path, repo: Path, commit: str,
               phase: str, report_dir: Path | None = None, attempt_id: str = "") -> dict:
    if not re.fullmatch(r'[0-9a-fA-F]{40}', commit) or not re.fullmatch('[a-z0-9_-]{1,64}', phase):
        raise ValueError('invalid_checkpoint_identity')
    absolute_release = Path(os.path.abspath(release))
    if attempt_id and not re.fullmatch(r'[A-Za-z0-9_.:-]{1,200}', attempt_id):
        raise ValueError('invalid_checkpoint_attempt')
    release = release.resolve(strict=True)
    if absolute_release != release:
        raise ValueError('release_path_must_not_use_symlinks')
    root = releases_root.resolve(strict=True)
    if release.parent != root:
        raise ValueError('release_must_be_direct_child_of_releases_root')
    helper = repo.resolve(strict=True) / 'deploy' / 'clean_release_bytecode.py'
    report = {'schema': 'release_source_checkpoint.v1', 'phase': phase, 'attempt_id': attempt_id,
              'checked_at': datetime.now(timezone.utc).isoformat(), 'release_commit': commit.lower(),
              'release_path': str(release), 'validator_exit_code': None,
              'ok': False, 'cleanup_performed': False, 'writer_attribution': 'not_established'}
    command = [sys.executable, '-I', '-B', str(helper), '--validate-source',
               '--release-dir', str(release), '--releases-root', str(root),
               '--repo-root', str(repo.resolve()), '--commit', commit]
    if release.name.startswith('.eimemory-stage-'):
        command.append('--allow-stage')
    if helper.is_symlink() or not helper.is_file():
        report['reason'] = 'source_validator_missing_or_unsafe'
    else:
        try:
            result = subprocess.run(command, capture_output=True, text=True, check=False, timeout=120)
            report['validator_exit_code'] = result.returncode
            report['strict_validator_ok'] = result.returncode == 0
            report['ok'] = result.returncode == 0
            report['reason'] = '' if report['ok'] else 'release_source_validation_failed'
            # Fingerprint details in this compact report; the original validator
            # diagnostic is bounded and retained in the installer's stderr.
            if result.stderr:
                report['validator_error_sha256'] = hashlib.sha256(result.stderr.encode()).hexdigest()
                sys.stderr.write(result.stderr[-4096:])
        except (OSError, subprocess.TimeoutExpired) as exc:
            report['reason'] = 'source_validator_execution_failed'
            report['error_type'] = type(exc).__name__
    try:
        report.update(bytecode_inventory(release))
        report['bytecode_pollution_detected'] = report['bytecode_count'] > 0
        report['other_source_differences'] = 'not_inferred_from_bytecode_count_see_strict_validator'
        if report['bytecode_count'] or report['inventory_errors']:
            report['ok'] = False
            report['reason'] = 'release_bytecode_pollution' if report['bytecode_count'] else 'source_inventory_incomplete'
    except OSError as exc:
        report.update(ok=False, reason='source_inventory_incomplete', error_type=type(exc).__name__)
    if report_dir is not None:
        report['report_path'] = _write_report(report_dir, report, root)
    return report


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('release-dir', 'releases-root', 'repo-root'):
        parser.add_argument('--' + name, required=True, type=Path)
    parser.add_argument('--commit', required=True)
    parser.add_argument('--phase', required=True)
    parser.add_argument('--report-dir', type=Path)
    parser.add_argument('--attempt-id', default='')
    args = parser.parse_args(argv)
    try:
        report = checkpoint(release=args.release_dir, releases_root=args.releases_root,
                            repo=args.repo_root, commit=args.commit, phase=args.phase,
                            report_dir=args.report_dir, attempt_id=args.attempt_id)
    except (OSError, ValueError) as exc:
        report = {'ok': False, 'reason': 'source_checkpoint_failed', 'error_type': type(exc).__name__}
    print(json.dumps(report, ensure_ascii=True, allow_nan=False))
    return 0 if report['ok'] else 2


if __name__ == '__main__':
    raise SystemExit(main())
