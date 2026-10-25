"""Explicit retirement preserves old native proof and rejects active/racing work."""
from copy import deepcopy
from hashlib import sha256
import importlib.util
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from eimemory.contracts import runtime_manager as contract
from test_manual_recovery_capture import recovery
from test_runtime_manager import registry as example_registry

pytestmark = pytest.mark.skipif(os.name != 'posix', reason='native POSIX custody and locks')


def load():
    path = Path(__file__).resolve().parents[1] / 'deploy/retire_completed_user_transition.py'
    spec = importlib.util.spec_from_file_location('retire_completed_transition_tests', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def historical(tmp_path, monkeypatch):
    module = load()
    account = module.user.account()
    monkeypatch.setattr(contract, 'USER_MODE', True)
    state = tmp_path / 'control-state'
    authority = tmp_path / 'authority'
    storage = tmp_path / 'runtime-state'
    releases = tmp_path / 'install/releases'
    for path in (state / 'jobs', state / 'validations', authority, storage / 'deploy-jobs', releases):
        path.mkdir(parents=True, exist_ok=True)
    current_commit = 'd' * 40
    (releases / current_commit).mkdir()
    current = releases.parent / 'current'
    current.symlink_to(releases / current_commit, target_is_directory=True)
    locks = (storage / 'eimemory-deploy-worker.lock', releases.parent / '.storage-release-install.lock',
             releases.parent / '.candidate-validation.lock')
    for path in (*locks, state / 'lifecycle.lock'):
        path.write_bytes(b'')
        path.chmod(0o664 if path == locks[0] else 0o644)
    (state / 'lifecycle.lock').chmod(0o600)
    monkeypatch.setattr(module, 'LOCKS', locks)
    monkeypatch.setattr(module, 'STORAGE', storage)
    monkeypatch.setattr(module, 'CURRENT', current)
    monkeypatch.setattr(module, 'RELEASES', releases)
    registry_path = authority / 'runtime-manager.v1.json'
    monkeypatch.setattr(module.user, 'coordinates', lambda: dict(state=state, registry=registry_path,
        authority=authority, code=tmp_path / 'code', socket=tmp_path / 'bridge.sock'))
    registry = example_registry()
    registry.update(runtime_user=account.pw_name, runtime_uid=account.pw_uid, runtime_gid=account.pw_gid)
    for item in registry['units'].values():
        item['manager'] = 'user'
    registry['generation'] = contract.digest({key: value for key, value in registry.items() if key != 'generation'})
    request = dict(schema='eimemory.manual_release_permit.v2', operation='deploy',
        base_commit='a' * 40, candidate_commit='b' * 40, target_commit='b' * 40,
        controller_commit='a' * 40, validation_controller_commit='b' * 40,
        reviewed_attempt_id='c' * 32,
        scope=dict(tenant_id='fixture', agent_id='fixture', workspace_id='fixture', user_id='fixture'))
    key = contract.digest(request)
    invocation = 'e' * 32
    native = dict(LoadState='loaded', ActiveState='active', SubState='exited', Result='success',
        InvocationID=invocation, ExecMainStatus='0', MainPID='0', ControlGroup='',
        ExecMainExitTimestampMonotonic='12345', ControlPID='0')
    record = dict(job_key=key, unit=contract.job_unit(key), request=request, registry=registry,
        manual_release=True, phase='completed', finalized=True, resumed=True, background_held=False,
        original_invocation=invocation, terminal_state={name: native[name] for name in contract.NATIVE_EXIT_PROPERTIES})
    transition = dict(job_key=key, complete=True, held=False)
    states = {record['unit']: deepcopy(native)}

    def publish(path, value):
        path.write_bytes(contract.canonical(value))
        path.chmod(0o640)

    publish(registry_path, registry)
    job_path = state / 'jobs' / (key + '.json')
    publish(job_path, record)
    pointer = state / 'active-transition.json'
    publish(pointer, transition)
    for operation in contract.VALIDATION_PREFIXES:
        unit = contract.validation_unit(key, operation)
        if operation in {'receipt', 'post_resume'}:
            observed = deepcopy(native)
            observed['InvocationID'] = ('1' if operation == 'receipt' else '2') * 32
            child = dict(job_key=key, unit=unit, operation=operation,
                parent_original_invocation=invocation, original_invocation=observed['InvocationID'],
                terminal_state={name: observed[name] for name in contract.NATIVE_EXIT_PROPERTIES})
            publish(state / 'validations' / (key + '-' + operation + '.json'), child)
        else:
            observed = {**native, 'LoadState': 'not-found', 'ActiveState': 'inactive',
                        'SubState': 'dead', 'InvocationID': '', 'ExecMainExitTimestampMonotonic': '0'}
        states[unit] = observed
    states['eimemory-rpc.service'] = {**native, 'ActiveState': 'active', 'SubState': 'running', 'MainPID': '321'}
    monkeypatch.setattr(module, '_native', lambda unit: deepcopy(states[unit]))
    health = dict(ok=True, commit=current_commit, configured_runtime_commit=current_commit,
                  checks=dict(process=True, store=True, runtime_identity=True, ready=True))
    monkeypatch.setattr(module, '_health', lambda commit: deepcopy(health))
    receipt = dict(ok=True, commit=current_commit, current_commit=current_commit, rpc_service='active',
        health=health, **{name: 0 for name in ('installer_exit_code', 'binding_refresh_exit_code',
            'bytecode_cleanup_exit_code', 'settings_exit_code', 'post_resume_gate_exit_code',
            'rpc_service_exit_code', 'health_exit_code')})
    receipt_path = storage / 'deploy-jobs' / (current_commit + '.json')
    publish(receipt_path, receipt)
    args = SimpleNamespace(job_key=key, expected_current_commit=current_commit,
        expected_registry_sha256=sha256(registry_path.read_bytes()).hexdigest(),
        expected_transition_sha256=sha256(pointer.read_bytes()).hexdigest(),
        expected_job_sha256=sha256(job_path.read_bytes()).hexdigest())
    return SimpleNamespace(**locals())


def test_real_directory_retirement_preserves_all_authority_and_custody(historical):
    r = historical
    originals = {path: path.read_bytes() for path in (r.registry_path, r.job_path, r.pointer)}
    result = r.module.retire(r.args)
    assert result['ok'] and result['transition_retired']
    assert not r.pointer.exists()
    assert not result['release_accepted'] and not result['autonomous_repair_credit']
    assert r.registry_path.read_bytes() == originals[r.registry_path]
    assert r.job_path.read_bytes() == originals[r.job_path]
    archive = Path(result['archive'])
    assert archive.stat().st_mode & 0o777 == 0o700
    assert (archive / 'transition.retired').read_bytes() == originals[r.pointer]
    assert (archive / 'registry.original').read_bytes() == originals[r.registry_path]
    assert (archive / 'job.original').read_bytes() == originals[r.job_path]
    audit = contract.strict_json((archive / 'audit.json').read_bytes())
    assert audit['registry_runtime_commit'] == r.registry['runtime_commit'] != r.current_commit
    assert audit['registry_modified'] is False and audit['native_completion_modified'] is False


@pytest.mark.parametrize('fault', ['incomplete', 'unfinalized', 'not_resumed', 'held', 'failed_exit',
    'parent_pid', 'control_pid', 'parent_cgroup', 'new_invocation', 'child_pid', 'child_cgroup',
    'child_new_invocation', 'missing_validation', 'storage_marker', 'controller_marker',
    'manual_capture', 'migration_capture', 'receipt_failed', 'receipt_group_write', 'rpc_pid_changed'])
def test_refuses_incomplete_running_or_untrusted_state(historical, fault):
    r = historical
    if fault == 'incomplete':
        r.transition['complete'] = False
        r.publish(r.pointer, r.transition)
        r.args.expected_transition_sha256 = sha256(r.pointer.read_bytes()).hexdigest()
    elif fault in {'unfinalized', 'not_resumed', 'held'}:
        r.record[{'unfinalized': 'finalized', 'not_resumed': 'resumed', 'held': 'background_held'}[fault]] = fault == 'held'
        r.publish(r.job_path, r.record)
        r.args.expected_job_sha256 = sha256(r.job_path.read_bytes()).hexdigest()
    elif fault == 'failed_exit': r.states[r.record['unit']]['Result'] = 'exit-code'
    elif fault == 'parent_pid': r.states[r.record['unit']]['MainPID'] = '999'
    elif fault == 'control_pid': r.states[r.record['unit']]['ControlPID'] = '999'
    elif fault == 'parent_cgroup': r.states[r.record['unit']]['ControlGroup'] = '/leftover'
    elif fault == 'new_invocation': r.states[r.record['unit']]['InvocationID'] = '9' * 32
    elif fault.startswith('child_'):
        item = r.states[contract.validation_unit(r.key, 'receipt')]
        item[{'child_pid': 'MainPID', 'child_cgroup': 'ControlGroup', 'child_new_invocation': 'InvocationID'}[fault]] = (
            '9' * 32 if fault == 'child_new_invocation' else '999')
    elif fault == 'missing_validation': (r.state / 'validations' / (r.key + '-receipt.json')).unlink()
    elif fault == 'storage_marker': (r.storage / '.storage-restore-journal.json').write_text('{}')
    elif fault == 'controller_marker': (r.state / 'controller-installation.json').write_text('{}')
    elif fault == 'manual_capture': (r.state / 'manual-recovery-capture.json').write_text('{}')
    elif fault == 'migration_capture': r.publish(r.state / 'migration-prior-active.json', {'restored': False})
    elif fault == 'receipt_failed': r.publish(r.receipt_path, {**r.receipt, 'ok': False})
    elif fault == 'receipt_group_write': r.receipt_path.chmod(0o664)
    elif fault == 'rpc_pid_changed': r.states['eimemory-rpc.service']['MainPID'] = '0'
    pointer = r.pointer.read_bytes()
    with pytest.raises(ValueError):
        r.module.retire(r.args)
    assert r.pointer.read_bytes() == pointer


def test_busy_worker_lock_prevents_retiring_a_successful_but_stale_receipt(historical):
    import fcntl
    r = historical
    with r.locks[0].open('rb') as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(ValueError, match='lock_busy'):
            r.module.retire(r.args)
    assert r.pointer.exists()


@pytest.mark.parametrize('when', ['archive', 'second_health'])
def test_never_moves_a_newer_pointer_published_during_observation(historical, monkeypatch, when):
    r = historical
    newer = dict(job_key='f' * 64, complete=False)
    if when == 'archive':
        write = r.module._write
        def raced_write(parent, name, raw):
            write(parent, name, raw)
            if name == 'audit.json': r.publish(r.pointer, newer)
        monkeypatch.setattr(r.module, '_write', raced_write)
    else:
        health = r.module._health
        calls = 0
        def raced_health(commit):
            nonlocal calls
            calls += 1
            if calls == 2: r.publish(r.pointer, newer)
            return health(commit)
        monkeypatch.setattr(r.module, '_health', raced_health)
    with pytest.raises(ValueError):
        r.module.retire(r.args)
    assert contract.strict_json(r.pointer.read_bytes()) == newer
    assert not list(r.state.glob('completed-transition-*/transition.retired'))


def test_crash_after_durable_archive_can_be_retried_without_rewriting_evidence(historical, monkeypatch):
    r = historical
    rename = r.module.os.rename
    def interrupted(*args, **kwargs):
        raise OSError('injected crash before pointer move')
    monkeypatch.setattr(r.module.os, 'rename', interrupted)
    with pytest.raises(OSError):
        r.module.retire(r.args)
    assert r.pointer.exists()
    previous = next(r.state.glob('completed-transition-*'))
    original_archive = {path.name: path.read_bytes() for path in previous.iterdir()}
    monkeypatch.setattr(r.module.os, 'rename', rename)
    assert r.module.retire(r.args)['transition_retired']
    assert {path.name: path.read_bytes() for path in previous.iterdir()} == original_archive


def test_rpc_health_redirect_is_never_followed():
    with pytest.raises(ValueError, match='redirect_forbidden'):
        load()._NoRedirect().redirect_request(None, None, 302, '', {}, 'https://elsewhere.invalid/')


@pytest.fixture
def reconciled_migration(recovery, monkeypatch):
    """Use the real controller custody/topology verifier and native fixture."""
    r = recovery
    monkeypatch.setattr(contract, 'USER_MODE', False)
    migration = r.bridge.state / 'migration-prior-active.json'
    raw = contract.canonical(dict(active_before=r.prior['active_before'],
        enabled_before=r.prior['enabled_before'], restored=False))
    migration.write_bytes(raw)
    migration.chmod(0o640)
    r.bridge.transition(r.registry, r.record['job_key'], 'quiesce', 7, 1001)
    record = r.bridge.record(r.record['job_key'])
    r.bridge.resume(r.registry, record)
    record.update(resumed=True, finalized=True, phase='completed',
                  restoration_verification=r.bridge.verify_restoration(r.registry, record))
    r.bridge.save(r.registry, record)
    r.bridge.publish_migration_reconciliation(r.registry, record, raw)
    reconciliation = r.bridge.state / 'migration-reconciliation.json'
    reconciliation.chmod(0o640)
    r.bridge.registry_path.chmod(0o640)
    module = load()
    monkeypatch.setattr(module.user, 'coordinates', lambda: dict(state=r.bridge.state,
        registry=r.bridge.registry_path, authority=r.bridge.registry_path.parent,
        code=r.bridge.state / 'code', socket=r.bridge.state / 'bridge.sock'))
    monkeypatch.setattr(module, '_migration_reader', lambda registry: r.bridge)
    return SimpleNamespace(module=module, native=r, migration=migration, raw=raw,
                           reconciliation=reconciliation, record=record)


def test_formal_native_reconciliation_preserves_unrestored_migration_history(reconciled_migration):
    r = reconciled_migration
    proof = r.module._migration_proof(r.native.bridge.state)
    assert proof['status'] == 'formally_reconciled_preserved_history'
    assert proof['source_migration_sha256'] == sha256(r.raw).hexdigest()
    assert proof['job_key'] == r.record['job_key']
    assert r.migration.read_bytes() == r.raw
    assert contract.strict_json(r.raw)['restored'] is False


@pytest.mark.parametrize('fault', ['missing', 'changed_source_digest', 'changed_job',
    'uncompleted_job', 'changed_restore_plan', 'changed_restoration_verification',
    'missing_custody', 'changed_custody', 'changed_complete', 'changed_topology_source'])
def test_formal_migration_reconciliation_never_accepts_missing_or_changed_custody(
    reconciled_migration, fault
):
    r = reconciled_migration
    bridge = r.native.bridge
    proof = contract.strict_json(r.reconciliation.read_bytes())
    history = bridge.state / 'managed-incident-receipts' / ('migration-reconciliation-' + r.record['job_key'])
    if fault == 'missing': r.reconciliation.unlink()
    elif fault == 'changed_source_digest':
        proof['source_migration_sha256'] = '0' * 64
        r.reconciliation.write_bytes(contract.canonical(proof))
    elif fault == 'changed_job':
        proof['job_key'] = 'f' * 64
        r.reconciliation.write_bytes(contract.canonical(proof))
    elif fault in {'uncompleted_job', 'changed_restore_plan', 'changed_restoration_verification'}:
        record = deepcopy(r.record)
        if fault == 'uncompleted_job': record['finalized'] = False
        elif fault == 'changed_restore_plan': record['restore_plan'] = {}
        else: record['restoration_verification'] = {}
        bridge.save(r.native.registry, record)
    elif fault == 'missing_custody': (history / 'migration.original').unlink()
    elif fault == 'changed_custody': (history / 'migration.original').write_bytes(b'changed')
    elif fault == 'changed_complete': (history / 'complete.json').write_bytes(b'{}')
    elif fault == 'changed_topology_source':
        (r.native.registration / 'job.original').write_bytes(b'{}')
    with pytest.raises((ValueError, OSError)):
        r.module._migration_proof(bridge.state)
    assert r.migration.read_bytes() == r.raw


def test_reconciliation_loader_rejects_unpinned_adjacent_contract_before_import(historical, monkeypatch):
    r = historical
    code = r.tmp_path / 'reviewed-code'
    code.mkdir(mode=0o750)
    for name in ('user_control_plane.py', 'runtime_manager_contract.py',
                 'runtime_manager_bridge.py', 'hermes_topology_contract.py'):
        path = code / name
        path.write_text('raise AssertionError("unreviewed code executed")')
        path.chmod(0o640)
    coordinates = r.module.user.coordinates()
    monkeypatch.setattr(r.module.user, 'coordinates', lambda: {**coordinates, 'code': code})
    with pytest.raises(ValueError, match='compatibility_changed'):
        r.module._migration_reader(r.registry)
