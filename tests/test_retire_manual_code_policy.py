"""Explicit retirement preserves original permission and ledger evidence."""
from __future__ import annotations

from hashlib import sha256
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import subprocess
from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.skipif(os.name != 'posix', reason='native user-operator policy retirement')


def load_retirement_module():
    spec = importlib.util.spec_from_file_location('manual_policy_retirement_test',
        Path(__file__).parents[1] / 'deploy/retire_manual_code_policy.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def retirement(tmp_path, monkeypatch):
    module = load_retirement_module()
    # These functional tests model the permitted *host operator*. The actual
    # regression runner can itself be in bwrap, where host-root ancestors are
    # shown as overflowuid. Model only that OS view for fixed system ancestors;
    # keep real permissions, symlinks, file identity and every user leaf owner.
    # Genuine private-namespace rejection is tested separately below, without
    # replacing _operator or weakening any product path validation.
    system_ancestors = {Path('/'), Path('/tmp'), Path('/var'), Path('/var/tmp')}
    overflow = int(Path('/proc/sys/kernel/overflowuid').read_text().strip())
    private_mapping = Path('/proc/self/uid_map').read_text().split() != ['0', '0', '4294967295']

    class HostPath(type(tmp_path)):
        def lstat(self):
            info = super().lstat()
            if private_mapping and Path(self) in system_ancestors and info.st_uid == overflow:
                values = {name: getattr(info, name) for name in dir(info) if name.startswith('st_')}
                values['st_uid'] = 0
                return SimpleNamespace(**values)
            return info

    monkeypatch.setattr(module, 'Path', HostPath)
    root = tmp_path / 'repo'
    root.mkdir()
    env = {**os.environ, 'GIT_AUTHOR_NAME': 'test', 'GIT_AUTHOR_EMAIL': 'test@example.invalid',
        'GIT_COMMITTER_NAME': 'test', 'GIT_COMMITTER_EMAIL': 'test@example.invalid'}
    def git(*args):
        return subprocess.run(['git', '-C', str(root), *args], env=env, check=True,
            capture_output=True, text=True).stdout.strip()
    git('init', '-q')
    (root / 'fixture').write_text('before')
    git('add', 'fixture')
    git('commit', '-qm', 'before')
    base = git('rev-parse', 'HEAD')
    (root / 'fixture').write_text('after')
    git('commit', '-qam', 'after')
    current_commit = git('rev-parse', 'HEAD')
    releases = tmp_path / 'releases'
    (releases / current_commit).mkdir(parents=True)
    current = tmp_path / 'current'
    current.symlink_to(releases / current_commit)
    config = tmp_path / 'config'
    config.mkdir()
    policy = config / 'code-automation-policy.v2.json'
    data = dict(schema_version='code_automation_policy.v2', policy_id='operator-history-v4',
        repository=dict(root=str(root), base_commit=base), source_fact='original unchanged')
    policy.write_text(json.dumps(data, indent=2) + '\n')
    policy.chmod(0o600)
    database = tmp_path / 'eimemory.sqlite'
    conn = sqlite3.connect(database)
    conn.executescript('CREATE TABLE code_evolution_transactions(transaction_id TEXT, '
        'policy_digest TEXT,current_state TEXT,terminal INTEGER,terminal_receipt_digest TEXT);'
        'CREATE TABLE code_evolution_policy_consumptions(policy_digest TEXT);')
    digest = sha256(module._canonical(data)).hexdigest()
    conn.execute('INSERT INTO code_evolution_transactions VALUES(?,?,?,?,?)',
        ('real-aborted-transaction', digest, 'ABORTED_CANDIDATE_RESTORED', 1, 'f' * 64))
    conn.commit()
    conn.close()
    monkeypatch.setattr(module, 'POLICY', HostPath(policy))
    monkeypatch.setattr(module, 'DATABASE', HostPath(database))
    monkeypatch.setattr(module, 'OWNER', HostPath(tmp_path / 'owner'))
    monkeypatch.setattr(module, 'CURRENT', HostPath(current))
    monkeypatch.setattr(module, 'RELEASES', HostPath(releases))
    monkeypatch.setattr(module, '_operator', lambda: HostPath(tmp_path))
    args = dict(expected_sha256=sha256(policy.read_bytes()).hexdigest(),
        expected_policy_id=data['policy_id'], expected_current_commit=current_commit)
    return module, args, policy, database, data


def test_retirement_archives_original_bytes_and_unconsumed_aborted_facts(retirement):
    module, args, policy, database, _ = retirement
    before_policy = policy.read_bytes()
    before_database = database.read_bytes()
    result = module.retire(**args)
    archive = Path(result['archive'])
    assert result['ok'] is result['retired'] is True
    assert result['autonomous_credit'] is result['policy_consumed'] is False
    assert not policy.exists()
    assert (archive / 'policy.original.json').read_bytes() == before_policy
    assert database.read_bytes() == before_database
    assert archive.stat().st_mode & 0o777 == 0o700
    assert all(p.stat().st_mode & 0o777 == 0o600 for p in archive.iterdir())
    report = json.loads((archive / 'complete.json').read_text())
    assert report['ledger']['policy_consumption_count'] == 0
    assert report['ledger']['terminal_transactions'][0]['state'] == 'ABORTED_CANDIDATE_RESTORED'


@pytest.mark.parametrize('fault', ['hash', 'native_owned', 'active', 'consumed', 'current_base', 'symlink'])
def test_retirement_refuses_inexact_owned_active_or_consumed_permission(retirement, fault):
    module, args, policy, database, data = retirement
    if fault == 'hash':
        args['expected_sha256'] = '0' * 64
    elif fault == 'native_owned':
        module.OWNER.mkdir(mode=0o700)
        (module.OWNER / 'published.json').write_text('{}')
    elif fault in {'active', 'consumed'}:
        conn = sqlite3.connect(database)
        if fault == 'active':
            conn.execute('INSERT INTO code_evolution_transactions VALUES(?,?,?,?,?)',
                ('in-progress', 'other-policy', 'DEPLOY_INTENT', 0, ''))
        else:
            conn.execute('INSERT INTO code_evolution_policy_consumptions VALUES(?)',
                (sha256(module._canonical(data)).hexdigest(),))
        conn.commit()
        conn.close()
    elif fault == 'current_base':
        data['repository']['base_commit'] = args['expected_current_commit']
        policy.write_bytes(module._canonical(data))
        args['expected_sha256'] = sha256(policy.read_bytes()).hexdigest()
    else:
        original = policy.with_name('untouched-original.json')
        policy.rename(original)
        policy.symlink_to(original)
    before = policy.read_bytes()
    with pytest.raises((ValueError, OSError)):
        module.retire(**args)
    assert policy.read_bytes() == before
    assert not list(policy.parents[1].glob('.local/state/**/complete.json'))


def test_retirement_rechecks_policy_after_durable_archive_before_unlink(retirement, monkeypatch):
    module, args, policy, _, _ = retirement
    original = module._ledger
    count = 0
    def ledger(digest):
        nonlocal count
        count += 1
        if count > 1:
            policy.write_text('{"replaced":true}')
        return original(digest)
    monkeypatch.setattr(module, '_ledger', ledger)
    # Replacing policy after its final re-read must still be detected by the
    # final named identity check, even if the ledger facts stayed unchanged.
    with pytest.raises(ValueError, match='changed_before_retirement'):
        module.retire(**args)
    assert policy.exists()


@pytest.mark.parametrize('uid,uid_map,no_new_privs,reason', [
    (0, '0 0 4294967295\n', '0', 'nonroot_operator_required'),
    (0, '0 1001 1\n', '1', 'nonroot_operator_required'),
    (1001, '1001 1001 1\n', '0', 'host_operator_context_required'),
    (1001, '1001 0 1\n', '0', 'host_operator_context_required'),
    (1001, '0 0 4294967295\n', '1', 'host_operator_context_required'),
])
def test_genuine_operator_rejects_root_private_namespace_and_nnp(monkeypatch, uid, uid_map, no_new_privs, reason):
    module = load_retirement_module()
    # Execute the real product admission function against precise kernel-view
    # facts. In particular, partial maps fail even with NoNewPrivs=0: the host
    # functional fixture above cannot grant namespace access to this entry.
    class ProcPath(type(Path('/'))):
        def read_text(self, *args, **kwargs):
            if str(self) == '/proc/self/uid_map':
                return uid_map
            if str(self) == '/proc/self/status':
                return 'NoNewPrivs:\t' + no_new_privs + '\n'
            return super().read_text(*args, **kwargs)
    monkeypatch.setattr(module, 'Path', ProcPath)
    monkeypatch.setattr(module, 'os', SimpleNamespace(name='posix', getuid=lambda: uid, geteuid=lambda: uid))
    with pytest.raises(ValueError, match=reason):
        module._operator()
