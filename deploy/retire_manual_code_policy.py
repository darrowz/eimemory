#!/usr/bin/env python3
"""Explicit user-operator retirement of a stale, unconsumed manual permit.

This does not consume permission, create an incident, or write the business
database. Original policy bytes and read-only ledger facts are retained before
the exact named policy is removed. Native-owned permits are never retired here.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import subprocess

POLICY = Path('/etc/eimemory/code-automation-policy.v2.json')
DATABASE = Path('/var/lib/eimemory/state/eimemory.sqlite')
OWNER = Path('/var/lib/eimemory/state/code-automation-owner')
CURRENT = Path('/opt/eimemory/current')
RELEASES = Path('/opt/eimemory/releases')


def _json(raw):
    def pairs(items):
        value = {}
        for key, item in items:
            if key in value:
                raise ValueError('manual_policy_duplicate_json_key')
            value[key] = item
        return value
    return json.loads(raw, object_pairs_hook=pairs,
        parse_constant=lambda _: (_ for _ in ()).throw(ValueError('manual_policy_nonfinite_json')))


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()


def _identity(info):
    return (info.st_dev, info.st_ino, info.st_uid, info.st_mode, info.st_nlink,
        info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _operator():
    if os.name != 'posix' or os.getuid() != os.geteuid() or os.geteuid() <= 0:
        raise ValueError('manual_policy_nonroot_operator_required')
    # An application sandbox cannot act as the host operator, even when its
    # unprivileged numeric ID is preserved in a nested user namespace.
    rows = [tuple(map(int, row.split())) for row in Path('/proc/self/uid_map').read_text().splitlines()]
    status = dict(row.split(':', 1) for row in Path('/proc/self/status').read_text().splitlines() if ':' in row)
    if rows != [(0, 0, 4294967295)] or status.get('NoNewPrivs', '').strip() != '0':
        raise ValueError('manual_policy_host_operator_context_required')
    import pwd
    return Path(pwd.getpwuid(os.geteuid()).pw_dir)


def _parents(path):
    if not path.is_absolute() or '..' in path.parts:
        raise ValueError('manual_policy_absolute_path_required')
    for item in (path, *path.parents):
        info = item.lstat()
        sticky = item != path and info.st_uid == 0 and info.st_mode & stat.S_ISVTX
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid not in {0, os.geteuid()}
                or info.st_mode & 0o022 and not sticky):
            raise ValueError('manual_policy_parent_untrusted')


def _read(path, *, maximum=65536, private=True):
    _parents(path.parent)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_nlink != 1
                or info.st_mode & (0o077 if private else 0o022) or not 0 < info.st_size <= maximum):
            raise ValueError('manual_policy_file_untrusted')
        raw = os.read(fd, maximum + 1)
        if (len(raw) != info.st_size or _identity(info) != _identity(os.fstat(fd))
                or _identity(info) != _identity(path.lstat())):
            raise ValueError('manual_policy_file_changed')
        return raw, _identity(info)
    finally:
        os.close(fd)


def _private_directory(path):
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    _parents(path)
    if path.stat().st_uid != os.geteuid() or stat.S_IMODE(path.stat().st_mode) != 0o700:
        raise ValueError('manual_policy_archive_not_private')


@contextmanager
def _lock(path):
    import fcntl
    _parents(path.parent)
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_nlink != 1
                or stat.S_IMODE(info.st_mode) != 0o600
                or _identity(info) != _identity(path.lstat())):
            raise ValueError('manual_policy_lock_untrusted')
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    finally:
        os.close(fd)


def _current(commit):
    target = RELEASES / commit
    if target.is_symlink() or not target.is_dir() or CURRENT.resolve(strict=True) != target:
        raise ValueError('manual_policy_current_release_changed')


def _ledger(policy_digest):
    _parents(DATABASE.parent)
    info = DATABASE.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_nlink != 1:
        raise ValueError('manual_policy_database_untrusted')
    connection = sqlite3.connect(DATABASE.as_uri() + '?mode=ro', uri=True, timeout=5)
    try:
        connection.execute('PRAGMA query_only=ON')
        connection.execute('BEGIN')
        active = connection.execute('SELECT count(*) FROM code_evolution_transactions WHERE terminal IS NOT 1').fetchone()[0]
        consumed = connection.execute('SELECT count(*) FROM code_evolution_policy_consumptions WHERE policy_digest=?',
            (policy_digest,)).fetchone()[0]
        terminal = connection.execute('SELECT transaction_id,current_state,terminal_receipt_digest '
            'FROM code_evolution_transactions WHERE policy_digest=? AND terminal=1 ORDER BY transaction_id',
            (policy_digest,)).fetchall()
        if active or consumed:
            raise ValueError('manual_policy_active_or_consumed')
        if info.st_dev != DATABASE.lstat().st_dev or info.st_ino != DATABASE.lstat().st_ino:
            raise ValueError('manual_policy_database_replaced')
        return dict(active_transaction_count=active, policy_consumption_count=consumed,
            terminal_transactions=[dict(zip(('transaction_id', 'state', 'terminal_receipt_digest'), row)) for row in terminal])
    finally:
        connection.close()


def _write(path, raw):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        with os.fdopen(fd, 'wb', closefd=False) as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(fd)
    finally:
        os.close(fd)


def _fsync(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def retire(*, expected_sha256, expected_policy_id, expected_current_commit):
    home = _operator()
    if (re.fullmatch('[0-9a-f]{64}', expected_sha256) is None
            or re.fullmatch('[0-9a-f]{40}', expected_current_commit) is None
            or not expected_policy_id or expected_policy_id.startswith('native-incident-')):
        raise ValueError('manual_policy_exact_expected_identity_required')
    _current(expected_current_commit)
    _private_directory(OWNER)
    # Same lock ordering as the native owner and atomic policy publisher.
    with _lock(OWNER / 'owner.lock'), _lock(POLICY.with_name('.' + POLICY.name + '.publish.lock')):
        if os.path.lexists(OWNER / 'published.json'):
            raise ValueError('manual_policy_native_owner_present')
        raw, identity = _read(POLICY)
        if sha256(raw).hexdigest() != expected_sha256:
            raise ValueError('manual_policy_compare_and_swap_failed')
        policy = _json(raw)
        repository = policy.get('repository', {}) if isinstance(policy, dict) else {}
        base = repository.get('base_commit')
        if (policy.get('schema_version') != 'code_automation_policy.v2'
                or policy.get('policy_id') != expected_policy_id
                or re.fullmatch('[0-9a-f]{40}', str(base)) is None
                or base == expected_current_commit):
            raise ValueError('manual_policy_not_stale_manual_permission')
        root = Path(repository.get('root', ''))
        _parents(root)
        # No shell or network. Prove the old permit's base is a strict ancestor
        # of the actual reviewed checkout/current release, not another branch.
        git_env = {'PATH': '/usr/bin:/bin', 'HOME': str(home), 'LC_ALL': 'C',
            'GIT_CONFIG_NOSYSTEM': '1', 'GIT_CONFIG_GLOBAL': '/dev/null'}
        head = subprocess.run(['/usr/bin/git', '-C', str(root), 'rev-parse', 'HEAD'],
            env=git_env, capture_output=True, text=True, timeout=10, check=True).stdout.strip()
        ancestry = subprocess.run(['/usr/bin/git', '-C', str(root), 'merge-base', '--is-ancestor', base, expected_current_commit],
            env=git_env, capture_output=True, timeout=10, check=False)
        if head != expected_current_commit or ancestry.returncode != 0:
            raise ValueError('manual_policy_current_repository_unverified')
        policy_digest = sha256(_canonical(policy)).hexdigest()
        facts = _ledger(policy_digest)
        archive_root = home / '.local/state/eimemory/control-plane/managed-incident-receipts/manual-policy-retirement'
        _private_directory(archive_root)
        archive = archive_root / (datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ') + '-' + expected_sha256)
        archive.mkdir(mode=0o700)
        _write(archive / 'policy.original.json', raw)
        metadata = dict(schema='manual_code_policy_retirement.v1', policy_id=expected_policy_id,
            original_sha256=expected_sha256, policy_digest=policy_digest, original_identity=list(identity),
            prior_base_commit=base, current_commit=expected_current_commit, reason='base_release_superseded',
            ledger=facts, policy_consumed=False, autonomous_credit=False,
            retired_by_uid=os.geteuid(), prepared_at=datetime.now(timezone.utc).isoformat())
        _write(archive / 'prepared.json', _canonical(metadata) + b'\n')
        _fsync(archive)
        _fsync(archive_root)
        # A new physical read/snapshot, not an earlier cached permission check.
        again, named = _read(POLICY)
        _current(expected_current_commit)
        if (again != raw or named != identity or os.path.lexists(OWNER / 'published.json')
                or _ledger(policy_digest) != facts):
            raise ValueError('manual_policy_changed_before_retirement')
        if _read(POLICY) != (raw, identity):
            raise ValueError('manual_policy_changed_before_retirement')
        os.unlink(POLICY)
        _fsync(POLICY.parent)
        complete = {**metadata, 'retired': True, 'completed_at': datetime.now(timezone.utc).isoformat()}
        _write(archive / 'complete.json', _canonical(complete) + b'\n')
        _fsync(archive)
        return dict(ok=True, retired=True, archive=str(archive), original_sha256=expected_sha256,
            policy_digest=policy_digest, policy_consumed=False, autonomous_credit=False)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--expected-sha256', required=True)
    parser.add_argument('--expected-policy-id', required=True)
    parser.add_argument('--expected-current-commit', required=True)
    args = parser.parse_args()
    print(json.dumps(retire(expected_sha256=args.expected_sha256,
        expected_policy_id=args.expected_policy_id, expected_current_commit=args.expected_current_commit), sort_keys=True))


if __name__ == '__main__':
    main()
