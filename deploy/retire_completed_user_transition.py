#!/usr/bin/env python3
"""Explicitly archive a completed historical pointer after an ordinary release.

This operator action never completes a deployment, changes a registry or grants
acceptance. It preserves native evidence and requires an independently successful
ordinary worker receipt for a different, healthy current release.
"""
from __future__ import annotations

import argparse
from contextlib import ExitStack, contextmanager
from datetime import datetime, timezone
from hashlib import sha256
import importlib.util
import json
import os
from pathlib import Path
import shlex
import stat
import subprocess
import sys
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener
from uuid import uuid4

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from eimemory.contracts import runtime_manager as contract
from eimemory.contracts import user_control_plane as user

CURRENT = Path('/opt/eimemory/current')
RELEASES = Path('/opt/eimemory/releases')
STORAGE = Path('/var/lib/eimemory/state')
LOCKS = (STORAGE / 'eimemory-deploy-worker.lock',
         Path('/opt/eimemory/.storage-release-install.lock'),
         Path('/opt/eimemory/.candidate-validation.lock'))
RPC_ENV = Path('/etc/eimemory/rpc.env')
MARKERS = ('storage-release-transaction.json', '.storage-release-transaction.json.clearing',
           '.storage-release-transaction.json.recovery', '.storage-restore-journal.json',
           '.storage-vacuum-journal.json')


def _identity(meta):
    return (meta.st_dev, meta.st_ino, meta.st_uid, meta.st_gid, meta.st_mode,
            meta.st_nlink, meta.st_size, meta.st_mtime_ns, meta.st_ctime_ns)


def _read(path, *, modes=(0o600, 0o640), maximum=4 * 1024 * 1024):
    return user.read_file(path, modes=modes, maximum=maximum)


def _expected(path, digest, **kwargs):
    contract.hex_value(digest, 64, 'retire_expected_digest_invalid')
    raw = _read(path, **kwargs)
    if sha256(raw).hexdigest() != digest:
        raise ValueError('retire_authority_digest_changed')
    return raw


@contextmanager
def _directory(path):
    user.trusted_directory(path)
    descriptors = []
    try:
        descriptor = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        descriptors.append(descriptor)
        for part in path.parts[1:]:
            descriptor = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                                 dir_fd=descriptor)
            descriptors.append(descriptor)
        pin = _identity(os.fstat(descriptor))
        if pin != _identity(path.lstat()):
            raise ValueError('retire_directory_changed')
        yield descriptor
        if pin != _identity(os.fstat(descriptor)) or pin != _identity(path.lstat()):
            # Child creation changes directory size/mtime, not its ownership or inode.
            if pin[:5] != _identity(os.fstat(descriptor))[:5] or pin[:5] != _identity(path.lstat())[:5]:
                raise ValueError('retire_directory_changed')
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


@contextmanager
def _existing_lock(path):
    import fcntl
    with _directory(path.parent) as parent:
        descriptor = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC,
                             dir_fd=parent)
        try:
            meta = os.fstat(descriptor)
            # Contenders may truncate/touch the same flock inode before their
            # nonblocking lock attempt fails. Contents are not lock authority.
            pin = _identity(meta)[:6]
            if (not stat.S_ISREG(meta.st_mode) or meta.st_uid != user.account().pw_uid
                    or meta.st_nlink != 1 or pin != _identity(os.stat(path.name, dir_fd=parent, follow_symlinks=False))[:6]):
                raise ValueError('retire_lock_untrusted')
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise ValueError('retire_lock_busy') from None
            yield
            if pin != _identity(os.fstat(descriptor))[:6] or pin != _identity(os.stat(path.name, dir_fd=parent, follow_symlinks=False))[:6]:
                raise ValueError('retire_lock_changed')
        finally:
            os.close(descriptor)


def _native(unit):
    properties = (*contract.NATIVE_EXIT_PROPERTIES, 'ControlPID')
    uid = user.account().pw_uid
    result = subprocess.run(['/usr/bin/systemctl', '--user', 'show', unit, '--no-pager',
                             '--property=' + ','.join(properties)],
                            capture_output=True, text=True, timeout=30, check=True,
                            env={'PATH': '/usr/bin:/bin', 'LC_ALL': 'C',
                                 'XDG_RUNTIME_DIR': '/run/user/' + str(uid),
                                 'DBUS_SESSION_BUS_ADDRESS': 'unix:path=/run/user/' + str(uid) + '/bus'})
    values = dict(row.split('=', 1) for row in result.stdout.splitlines() if '=' in row)
    if set(values) != set(properties):
        raise ValueError('retire_native_readback_invalid')
    return values


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, newurl):
        raise ValueError('retire_rpc_health_redirect_forbidden')


def _health(commit):
    raw = _read(RPC_ENV, maximum=65536)
    entries = shlex.split(raw.decode())
    if len(entries) != 1 or not entries[0].startswith('EIMEMORY_RPC_AUTH_TOKEN='):
        raise ValueError('retire_rpc_auth_invalid')
    token = entries[0].partition('=')[2]
    if not token:
        raise ValueError('retire_rpc_auth_invalid')
    request = Request('http://127.0.0.1:8091/health',
                      headers={'Authorization': 'Bearer ' + token})
    with build_opener(ProxyHandler({}), _NoRedirect()).open(request, timeout=20) as response:
        value = contract.strict_json(response.read(1024 * 1024 + 1), max_bytes=1024 * 1024)
    if (value.get('ok') is not True or value.get('commit') != commit
            or value.get('configured_runtime_commit') != commit
            or any(value.get('checks', {}).get(key) is not True
                   for key in ('process', 'store', 'runtime_identity', 'ready'))):
        raise ValueError('retire_current_health_unverified')
    return {key: value[key] for key in ('ok', 'commit', 'configured_runtime_commit', 'checks')}


def _migration_reader(registry):
    """Reuse the published controller's read-only custody verifier, never serve.

    The installed bridge/topology use the native fixed owner/path/mode trust
    boundary. Adjacent helper and wire contract additionally retain the exact
    compatibility digests pinned by the operator-reviewed registry.
    """
    paths = user.coordinates()
    code = paths['code']
    user.trusted_directory(code)
    if stat.S_IMODE(code.stat().st_mode) != 0o750:
        raise ValueError('retire_controller_code_directory_untrusted')
    blobs = {name: _read(code / name, modes=(0o640,), maximum=1024 * 1024)
             for name in ('user_control_plane.py', 'runtime_manager_contract.py',
                          'runtime_manager_bridge.py', 'hermes_topology_contract.py')}
    for name, source in (('user_control_plane.py', 'eimemory/contracts/user_control_plane.py'),
                         ('runtime_manager_contract.py', 'eimemory/contracts/runtime_manager.py')):
        if sha256(blobs[name]).hexdigest() != registry['compatibility_hashes'][source]:
            raise ValueError('retire_controller_compatibility_changed')

    def execute(name):
        path = code / name
        spec = importlib.util.spec_from_file_location('retire_reviewed_' + path.stem, path)
        module = importlib.util.module_from_spec(spec)
        exec(compile(blobs[name], str(path), 'exec'), module.__dict__)
        return module

    original_mode = os.environ.get('EIMEMORY_CONTROL_PLANE')
    os.environ['EIMEMORY_CONTROL_PLANE'] = 'user'
    try:
        installed_contract = execute('runtime_manager_contract.py')
        installed_contract.validate_registry(registry)
        module = execute('runtime_manager_bridge.py')
        if (module.USER_MODE is not True or module.ROOT != code or module.STATE != paths['state']
                or module.REGISTRY != paths['registry']):
            raise ValueError('retire_controller_coordinates_changed')
        bridge = module.Bridge(installed_contract, registry=paths['registry'], state=paths['state'])
        bridge.retirement_code_sha256 = {name: sha256(raw).hexdigest() for name, raw in blobs.items()}
        return bridge
    finally:
        if original_mode is None:
            os.environ.pop('EIMEMORY_CONTROL_PLANE', None)
        else:
            os.environ['EIMEMORY_CONTROL_PLANE'] = original_mode


def _migration_proof(state):
    migration = state / 'migration-prior-active.json'
    if not os.path.lexists(migration):
        return {'status': 'absent'}
    raw = _read(migration)
    if contract.strict_json(raw).get('restored') is True:
        return {'status': 'restored', 'source_migration_sha256': sha256(raw).hexdigest()}
    reconciliation = state / 'migration-reconciliation.json'
    if not os.path.lexists(reconciliation):
        raise ValueError('retire_migration_capture_not_restored')
    registry = contract.validate_registry(contract.strict_json(_read(user.coordinates()['registry'])))
    bridge = _migration_reader(registry)
    if bridge.migration_reconciled(registry, raw) is not True:
        raise ValueError('retire_migration_reconciliation_unverified')
    proof_raw = _read(reconciliation)
    proof = contract.strict_json(proof_raw)
    return {'status': 'formally_reconciled_preserved_history',
            'source_migration_sha256': sha256(raw).hexdigest(),
            'reconciliation_sha256': sha256(proof_raw).hexdigest(), 'job_key': proof['job_key'],
            'topology_intent': proof['topology_intent'],
            'verifier_code_sha256': getattr(bridge, 'retirement_code_sha256', {})}


def _clean(state):
    if any(os.path.lexists(STORAGE / name) for name in MARKERS):
        raise ValueError('retire_storage_transaction_present')
    if any(os.path.lexists(state / name) for name in
           ('controller-installation.json', 'manual-recovery-capture.json')):
        raise ValueError('retire_controller_transaction_present')
    return _migration_proof(state)


def _no_processes(native):
    return (native.get('LoadState') in {'loaded', 'not-found'}
            and native.get('MainPID') in {'', '0'} and native.get('ControlPID') == '0'
            and native.get('ControlGroup') == ''
            and (native.get('ActiveState') in {'inactive', 'failed'}
                 or native.get('ActiveState') == 'active' and native.get('SubState') == 'exited'))


def _observations(record, state):
    unit = record['unit']
    pinned = record.get('terminal_state')
    invocation = record.get('original_invocation')
    native = _native(unit)
    if (not isinstance(pinned, dict) or pinned.get('Result') != 'success'
            or pinned.get('ExecMainStatus') != '0' or not _no_processes(native)
            or not contract.native_terminal_readback(pinned, native, invocation, unit)):
        raise ValueError('retire_native_success_unverified')
    children = {}
    required = {'receipt', 'post_resume'} if record['request']['operation'] == 'deploy' else {'receipt'}
    if record.get('manual_release') is not True:
        required.add('pre_observation')
    for operation in contract.VALIDATION_PREFIXES:
        child_unit = contract.validation_unit(record['job_key'], operation)
        observed = _native(child_unit)
        if not _no_processes(observed):
            raise ValueError('retire_validation_process_present')
        path = state / 'validations' / (record['job_key'] + '-' + operation + '.json')
        child_raw = _read(path) if os.path.lexists(path) else None
        if child_raw is None:
            if operation in required or observed.get('InvocationID'):
                raise ValueError('retire_validation_evidence_missing')
        else:
            child = contract.strict_json(child_raw)
            terminal = child.get('terminal_state')
            if (child.get('job_key') != record['job_key'] or child.get('operation') != operation
                    or child.get('unit') != child_unit or child.get('parent_original_invocation') != invocation
                    or not isinstance(terminal, dict) or terminal.get('Result') != 'success'
                    or terminal.get('ExecMainStatus') != '0'
                    or not contract.native_terminal_readback(terminal, observed,
                                                             child.get('original_invocation'), child_unit)):
                raise ValueError('retire_validation_success_unverified')
        children[operation] = {'native': observed,
                               'record_sha256': sha256(child_raw).hexdigest() if child_raw else None}
    rpc = _native('eimemory-rpc.service')
    if (rpc.get('LoadState') != 'loaded' or rpc.get('ActiveState') != 'active'
            or not rpc.get('MainPID', '').isdigit() or int(rpc['MainPID']) <= 0):
        raise ValueError('retire_current_rpc_not_active')
    return {'native': native, 'validations': children, 'rpc': rpc}


def _write(parent, name, raw):
    descriptor = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                         0o600, dir_fd=parent)
    try:
        with os.fdopen(descriptor, 'wb', closefd=False) as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(descriptor)
    finally:
        os.close(descriptor)


def retire(args):
    account = user.account()
    paths = user.coordinates()
    state = paths['state']
    registry_path = paths['registry']
    pointer = state / 'active-transition.json'
    contract.hex_value(args.job_key, 64, 'retire_job_key_invalid')
    contract.hex_value(args.expected_current_commit, 40, 'retire_current_commit_invalid')
    job_path = state / 'jobs' / (args.job_key + '.json')
    receipt_path = STORAGE / 'deploy-jobs' / (args.expected_current_commit + '.json')
    with ExitStack() as stack:
        for path in (*LOCKS, state / 'lifecycle.lock'):
            stack.enter_context(_existing_lock(path))
        state_fd = stack.enter_context(_directory(state))
        registry_raw = _expected(registry_path, args.expected_registry_sha256)
        transition_raw = _expected(pointer, args.expected_transition_sha256)
        pointer_identity = _identity(os.stat(pointer.name, dir_fd=state_fd, follow_symlinks=False))
        job_raw = _expected(job_path, args.expected_job_sha256)
        registry = contract.validate_registry(contract.strict_json(registry_raw))
        if (registry['runtime_uid'] != account.pw_uid or registry['runtime_gid'] != account.pw_gid
                or registry['runtime_user'] != account.pw_name
                or any(item['manager'] != 'user' for item in registry['units'].values())):
            raise ValueError('retire_user_registry_identity_invalid')
        record = contract.strict_json(job_raw)
        transition = contract.strict_json(transition_raw)
        if (transition != {'job_key': args.job_key, 'complete': True, 'held': False}
                or record.get('job_key') != args.job_key or record.get('unit') != contract.job_unit(args.job_key)
                or record.get('phase') != 'completed' or record.get('finalized') is not True
                or record.get('resumed') is not True or record.get('background_held') is not False
                or record.get('held_background') not in (None, [])):
            raise ValueError('retire_job_not_completed')
        request = record['request']
        if record.get('manual_release') is True:
            contract.validate_manual_permit(request, args.job_key)
        else:
            contract.validate_job(request, args.job_key)
        if request['operation'] != 'deploy':
            raise ValueError('retire_historical_deploy_required')
        prior_registry = contract.validate_registry(record['registry'])
        if (prior_registry['runtime_uid'] != account.pw_uid
                or prior_registry['runtime_gid'] != account.pw_gid
                or args.expected_current_commit in {request['base_commit'], request['candidate_commit']}):
            raise ValueError('retire_job_not_historical')

        def current():
            target = CURRENT.resolve(strict=True)
            if (not CURRENT.is_symlink() or target.parent != RELEASES
                    or target.name != args.expected_current_commit
                    or not target.is_dir() or (RELEASES / target.name).is_symlink()):
                raise ValueError('retire_current_release_changed')
            return target

        target = current()
        receipt_raw = _read(receipt_path, modes=(0o600, 0o640, 0o644))
        receipt = contract.strict_json(receipt_raw)
        exits = ('installer_exit_code', 'binding_refresh_exit_code', 'bytecode_cleanup_exit_code',
                 'settings_exit_code', 'post_resume_gate_exit_code', 'rpc_service_exit_code', 'health_exit_code')
        if (receipt.get('ok') is not True or receipt.get('commit') != target.name
                or receipt.get('current_commit') != target.name or receipt.get('rpc_service') != 'active'
                or any(type(receipt.get(key)) is not int or receipt[key] != 0 for key in exits)
                or receipt.get('health', {}).get('ok') is not True
                or receipt.get('health', {}).get('commit') != target.name):
            raise ValueError('retire_ordinary_receipt_unverified')
        migration_proof = _clean(state)
        observed = _observations(record, state)
        health = _health(target.name)
        archive_name = 'completed-transition-' + args.job_key[:12] + '-' + uuid4().hex
        os.mkdir(archive_name, 0o700, dir_fd=state_fd)
        archive_fd = os.open(archive_name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=state_fd)
        stack.callback(os.close, archive_fd)
        audit = {'schema': 'eimemory.completed_transition_retirement.v1',
                 'action': 'archive_completed_historical_pointer',
                 'created_at': datetime.now(timezone.utc).isoformat(), 'job_key': args.job_key,
                 'current_commit': target.name, 'registry_runtime_commit': registry['runtime_commit'],
                 'registry_controller_commit': registry['controller_commit'],
                 'registry_runtime_matches_current': registry['runtime_commit'] == target.name,
                 'registry_sha256': sha256(registry_raw).hexdigest(),
                 'transition_sha256': sha256(transition_raw).hexdigest(),
                 'job_sha256': sha256(job_raw).hexdigest(),
                 'ordinary_receipt_sha256': sha256(receipt_raw).hexdigest(),
                 'observations': observed, 'current_health': health,
                 'migration_proof': migration_proof,
                 'release_accepted': False, 'autonomous_repair_credit': False,
                 'native_completion_modified': False, 'registry_modified': False}
        for name, raw in (('registry.original', registry_raw), ('job.original', job_raw),
                          ('transition.original', transition_raw), ('ordinary-receipt.original', receipt_raw),
                          ('audit.json', contract.canonical(audit))):
            _write(archive_fd, name, raw)
        os.fsync(archive_fd)
        os.fsync(state_fd)
        if (current() != target or _expected(registry_path, args.expected_registry_sha256) != registry_raw
                or _expected(job_path, args.expected_job_sha256) != job_raw
                or _expected(pointer, args.expected_transition_sha256) != transition_raw
                or _read(receipt_path, modes=(0o600, 0o640, 0o644)) != receipt_raw
                or _identity(os.stat(pointer.name, dir_fd=state_fd, follow_symlinks=False)) != pointer_identity
                or _observations(record, state) != observed):
            raise ValueError('retire_context_changed')
        if _clean(state) != migration_proof:
            raise ValueError('retire_migration_reconciliation_changed')
        _health(target.name)
        final_rpc = _native('eimemory-rpc.service')
        if _clean(state) != migration_proof:
            raise ValueError('retire_migration_reconciliation_changed')
        # Network observation above is not a CAS. Recheck the exact named inode
        # and authority immediately before moving it under the held locks.
        if (final_rpc != observed['rpc'] or current() != target
                or _expected(registry_path, args.expected_registry_sha256) != registry_raw
                or _expected(job_path, args.expected_job_sha256) != job_raw
                or _read(receipt_path, modes=(0o600, 0o640, 0o644)) != receipt_raw
                or _expected(pointer, args.expected_transition_sha256) != transition_raw
                or _identity(os.stat(pointer.name, dir_fd=state_fd, follow_symlinks=False)) != pointer_identity):
            raise ValueError('retire_context_changed')
        os.rename(pointer.name, 'transition.retired', src_dir_fd=state_fd, dst_dir_fd=archive_fd)
        if _read(state / archive_name / 'transition.retired') != transition_raw:
            raise ValueError('retire_custody_pointer_changed')
        os.fsync(archive_fd)
        os.fsync(state_fd)
        return {'ok': True, 'transition_retired': True, 'archive': str(state / archive_name),
                'current_commit': target.name, 'job_key': args.job_key,
                'release_accepted': False, 'autonomous_repair_credit': False,
                'registry_modified': False, 'native_completion_modified': False}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('job-key', 'expected-current-commit', 'expected-registry-sha256',
                 'expected-transition-sha256', 'expected-job-sha256'):
        parser.add_argument('--' + name, required=True)
    args = parser.parse_args(argv)
    try:
        result = retire(args)
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        print(json.dumps({'ok': False, 'error_type': type(exc).__name__,
                          'reason': str(exc) if isinstance(exc, ValueError) else 'retire_operator_failed',
                          'release_accepted': False, 'autonomous_repair_credit': False}))
        return 2
    print(json.dumps(result))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
