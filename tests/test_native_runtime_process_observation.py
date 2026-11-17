"""Ordinary topology observation proves a real user process, never enrollment."""
from copy import deepcopy
from types import SimpleNamespace

import pytest

from stdlib_isolation import isolate_stdlib
from eimemory.contracts import hermes_topology, user_control_plane
from eimemory.core import runtime_manager
from eimemory.ops import controlled_stability as stability
from eimemory.ops import hermes_native_topology as native
from eimemory.ops import runtime_service_observation as observation


@pytest.fixture
def ordinary(monkeypatch):
    isolate_stdlib(observation, monkeypatch)
    isolate_stdlib(stability, monkeypatch)
    monkeypatch.setattr(observation.os, 'getgid', lambda: 1001, raising=False)
    monkeypatch.setattr(stability.os, 'geteuid', lambda: 1001, raising=False)
    monkeypatch.setattr(observation, 'registered_runtime_context', lambda: False)
    monkeypatch.setattr(user_control_plane, 'account', lambda: SimpleNamespace(pw_uid=1001, pw_gid=1001))
    topology = dict(runtime_uid=1001, runtime_gid=1001,
                    runtime_units=['eimemory-rpc.service', 'hermes-gateway.service'])
    monkeypatch.setattr(hermes_topology, 'load_verified_topology', lambda: deepcopy(topology))
    monkeypatch.setattr(stability, '_topology', lambda: deepcopy(topology))
    monkeypatch.setattr(stability, '_capture_environment_status',
                        lambda env, unit, commit: dict(ok=env.get('CAPTURE') == '1'))
    monkeypatch.setattr(runtime_manager, 'request',
                        lambda *a, **kw: (_ for _ in ()).throw(RuntimeError('manager_registry_missing')))
    unit = 'hermes-gateway.service'
    group = '/user.slice/user-1001.slice/user@1001.service/app.slice/' + unit
    state = dict(Id=unit, LoadState='loaded', ActiveState='active', MainPID='123',
                 InvocationID='a' * 32, ControlGroup=group)
    metadata = dict(st_uid=1001, st_dev=1, st_ino=123)
    monkeypatch.setattr(observation.Path, 'stat', lambda path: SimpleNamespace(**metadata))
    monkeypatch.setattr(stability.Path, 'stat', lambda path: SimpleNamespace(**metadata))
    files = {
        ('self', 'status'): b'Uid:\t1001 1001 1001 1001\nGid:\t1001 1001 1001 1001\n',
        ('123', 'status'): b'Uid:\t1001 1001 1001 1001\nNSpid:\t123 1\n',
        ('123', 'cgroup'): ('0::' + group + '\n').encode(),
        ('123', 'stat'): ('123 (gateway with spaces) ' + ' '.join(['S'] + ['0'] * 18 + ['4321'])).encode(),
        ('123', 'environ'): b'CAPTURE=1\0PRIVATE_TOKEN=never-emit\0',
    }
    for process in ('self', '1'):
        for name in ('uid_map', 'gid_map'):
            files[process, name] = b'0 0 4294967295\n'
    # Supply the same synthetic /proc bytes to the pre-fix legacy consumer too;
    # its baseline failure must be missing owner identity, not Windows /proc.
    monkeypatch.setattr(stability.Path, 'read_bytes', lambda path: files[path.parent.name, path.name])
    monkeypatch.setattr(stability.Path, 'read_text', lambda path: files[path.parent.name, path.name].decode())
    reads, commands = [], []
    def read(process, name, maximum=65536):
        reads.append((process.name, name, maximum))
        raw = files[process.name, name]
        if len(raw) > maximum:
            raise ValueError('runtime_process_file_limit')
        return raw
    # raising=False keeps the existing-consumer subset runnable on the old code.
    monkeypatch.setattr(observation, '_read_process_file', read, raising=False)
    def run(argv, **kwargs):
        commands.append(argv)
        assert argv[:3] == ['/usr/bin/systemctl', '--user', 'show']
        assert kwargs['timeout'] == 30 and kwargs['check'] is False
        return SimpleNamespace(returncode=0, stdout=''.join(f'{key}={value}\n' for key, value in state.items()))
    monkeypatch.setattr(observation.subprocess, 'run', run)
    return SimpleNamespace(state=state, files=files, topology=topology, metadata=metadata,
                           reads=reads, commands=commands)


@pytest.mark.parametrize('consumer', ['native_owner', 'stability_status'])
def test_existing_consumers_observe_declared_ordinary_owner(ordinary, consumer):
    if consumer == 'native_owner':
        owner, value = native._process_owner()
        assert value['namespace_pids'] == [123, 1]
    else:
        owner = stability._process_status(ordinary.state, 'a' * 40)
        assert owner['ok'] is True
    assert (owner['pid'], owner['start_ticks'], owner['invocation_id']) == (123, 4321, 'a' * 32)
    assert len(ordinary.commands) == 2


@pytest.mark.parametrize('consumer', ['native_owner', 'stability_status'])
def test_existing_consumers_managed_denial_never_falls_back(ordinary, monkeypatch, consumer):
    monkeypatch.setattr(observation, 'registered_runtime_context', lambda: True)
    with pytest.raises(RuntimeError, match='manager_registry_missing'):
        if consumer == 'native_owner':
            native._process_owner()
        else:
            stability._process_status(ordinary.state, 'a' * 40)
    assert ordinary.commands == ordinary.reads == []


@pytest.mark.parametrize('fault', ['uid', 'restart'])
@pytest.mark.parametrize('consumer', ['native_owner', 'stability_status'])
def test_existing_consumers_reject_wrong_owner_or_restart(ordinary, monkeypatch, fault, consumer):
    if fault == 'uid':
        ordinary.metadata['st_uid'] = 9
        ordinary.files['123', 'status'] = b'Uid: 1001 1001 0 1001\nNSpid: 123 1\n'
    else:
        read = observation._read_process_file
        def changed(process, name, maximum=65536):
            raw = read(process, name, maximum)
            if name == 'environ':
                ordinary.state['InvocationID'] = 'b' * 32
            return raw
        monkeypatch.setattr(observation, '_read_process_file', changed)
        legacy_read = stability.Path.read_bytes
        def changed_legacy(path):
            raw = legacy_read(path)
            if path.name == 'environ':
                ordinary.state['InvocationID'] = 'b' * 32
            return raw
        monkeypatch.setattr(stability.Path, 'read_bytes', changed_legacy)
    with pytest.raises((ValueError, RuntimeError, OSError)):
        if consumer == 'native_owner':
            native._process_owner()
        else:
            stability._process_status(ordinary.state, 'a' * 40)


@pytest.mark.parametrize('fault', [
    'topology_uid', 'topology_gid', 'topology_absent', 'arbitrary_unit',
    'unit_id', 'inactive', 'unloaded', 'pid_zero', 'invocation', 'system_group', 'other_user_group',
    'wrong_slice', 'nested_unit', 'proc_owner', 'real_uid', 'duplicate_uid',
    'nspid_absent', 'nspid_foreign', 'nspid_zero', 'nspid_duplicate', 'cgroup_mismatch',
    'cgroup_duplicate', 'start_zero', 'stat_pid', 'env_duplicate', 'env_malformed', 'env_limit',
    'observer_uid', 'observer_gid', 'observer_real_gid', 'self_uid_map', 'pid1_gid_map',
])
def test_native_identity_and_declaration_fail_closed(ordinary, monkeypatch, fault):
    unit = ordinary.state['Id']
    if fault == 'topology_uid': ordinary.topology['runtime_uid'] = 9
    elif fault == 'topology_gid': ordinary.topology['runtime_gid'] = 9
    elif fault == 'topology_absent':
        monkeypatch.setattr(hermes_topology, 'load_verified_topology',
                            lambda: (_ for _ in ()).throw(ValueError('hermes_topology_required')))
    elif fault == 'arbitrary_unit': unit = 'sshd.service'
    elif fault == 'unit_id': ordinary.state['Id'] = 'sshd.service'
    elif fault == 'inactive': ordinary.state['ActiveState'] = 'inactive'
    elif fault == 'unloaded': ordinary.state['LoadState'] = 'not-found'
    elif fault == 'pid_zero': ordinary.state['MainPID'] = '0'
    elif fault == 'invocation': ordinary.state['InvocationID'] = ''
    elif fault in {'system_group', 'other_user_group', 'wrong_slice', 'nested_unit'}:
        ordinary.state['ControlGroup'] = {
            'system_group': '/system.slice/' + unit,
            'other_user_group': '/user.slice/user-9.slice/user@9.service/app.slice/' + unit,
            'wrong_slice': '/user.slice/user-1001.slice/user@1001.service/arbitrary.slice/' + unit,
            'nested_unit': ordinary.state['ControlGroup'] + '/nested/' + unit}[fault]
    elif fault == 'proc_owner': ordinary.metadata['st_uid'] = 9
    elif fault == 'real_uid': ordinary.files['123', 'status'] = b'Uid: 0 1001 1001 1001\nNSpid: 123\n'
    elif fault == 'duplicate_uid': ordinary.files['123', 'status'] += b'Uid: 1001 1001 1001 1001\n'
    elif fault.startswith('nspid_'):
        ordinary.files['123', 'status'] = b'Uid: 1001 1001 1001 1001\n' + {
            'nspid_absent': b'', 'nspid_foreign': b'NSpid: 9\n',
            'nspid_zero': b'NSpid: 123 0\n', 'nspid_duplicate': b'NSpid: 123 123\n'}[fault]
    elif fault == 'cgroup_mismatch': ordinary.files['123', 'cgroup'] = b'0::/different\n'
    elif fault == 'cgroup_duplicate': ordinary.files['123', 'cgroup'] *= 2
    elif fault == 'start_zero': ordinary.files['123', 'stat'] = ordinary.files['123', 'stat'].replace(b'4321', b'0')
    elif fault == 'stat_pid': ordinary.files['123', 'stat'] = ordinary.files['123', 'stat'].replace(b'123 (', b'999 (')
    elif fault == 'env_duplicate': ordinary.files['123', 'environ'] += b'CAPTURE=2\0'
    elif fault == 'env_malformed': ordinary.files['123', 'environ'] += b'not-an-entry\0'
    elif fault == 'env_limit': ordinary.files['123', 'environ'] = b'a' * 1048577
    elif fault in {'observer_uid', 'observer_gid'}:
        ordinary.files['self', 'status'] = ordinary.files['self', 'status'].replace(
            b'Uid:' if fault == 'observer_uid' else b'Gid:', b'Invalid:')
    elif fault == 'observer_real_gid': monkeypatch.setattr(observation.os, 'getgid', lambda: 9)
    elif fault == 'self_uid_map': ordinary.files['self', 'uid_map'] = b'0 1001 1\n'
    elif fault == 'pid1_gid_map': ordinary.files['1', 'gid_map'] = b'0 1001 1\n'
    with pytest.raises((ValueError, RuntimeError, OSError)):
        observation.runtime_process_observation(unit)


@pytest.mark.parametrize('fault', ['environment', 'start', 'nspid', 'cgroup', 'proc_inode', 'pid', 'invocation'])
def test_observation_rereads_process_and_service_identity(ordinary, monkeypatch, fault):
    read = observation._read_process_file
    def changed(process, name, maximum=65536):
        raw = read(process, name, maximum)
        if name == 'environ':
            if fault == 'environment': ordinary.files['123', 'environ'] = b'CAPTURE=2\0'
            elif fault == 'start': ordinary.files['123', 'stat'] = ordinary.files['123', 'stat'].replace(b'4321', b'4322')
            elif fault == 'nspid': ordinary.files['123', 'status'] = b'Uid: 1001 1001 1001 1001\nNSpid: 123 2\n'
            elif fault == 'cgroup': ordinary.files['123', 'cgroup'] += b'0::/changed\n'
            elif fault == 'proc_inode': ordinary.metadata['st_ino'] = 999
            elif fault == 'pid': ordinary.state['MainPID'] = '456'
            elif fault == 'invocation': ordinary.state['InvocationID'] = 'b' * 32
        return raw
    monkeypatch.setattr(observation, '_read_process_file', changed)
    with pytest.raises(ValueError):
        observation.runtime_process_observation(ordinary.state['Id'])


def test_non_topology_stability_keeps_legacy_observer(ordinary, monkeypatch):
    monkeypatch.setattr(stability, '_topology', lambda: None)
    class Process:
        def __truediv__(self, name): return self
        def read_text(self): return '123 (legacy) ' + ' '.join(['S'] + ['0'] * 18 + ['4321'])
    monkeypatch.setattr(stability, '_process_environment', lambda state: (123, Process(), {'CAPTURE': '1'}))
    value = stability._process_status(ordinary.state, 'a' * 40)
    assert value == dict(ok=True, pid=123, start_ticks=4321)
    assert ordinary.commands == ordinary.reads == []


def test_ordinary_rpc_does_not_claim_managed_exec_seal(ordinary):
    ordinary.state['Id'] = 'eimemory-rpc.service'
    ordinary.state['ControlGroup'] = ordinary.state['ControlGroup'].replace('hermes-gateway', 'eimemory-rpc')
    ordinary.files['123', 'cgroup'] = ('0::' + ordinary.state['ControlGroup'] + '\n').encode()
    value = observation.runtime_process_observation('eimemory-rpc.service')
    assert 'EIMEMORY_RUNTIME_SETTINGS_APPLIED' not in value['environment']
    assert value['unit'] == 'eimemory-rpc.service'


def test_native_proof_ignores_a_path_selected_systemctl(ordinary, monkeypatch, tmp_path):
    (tmp_path / 'systemctl').write_text('#!/bin/sh\nexit 99\n')
    monkeypatch.setenv('PATH', str(tmp_path))
    observation.runtime_process_observation(ordinary.state['Id'])
    assert len(ordinary.commands) == 2
    assert all(argv[0] == '/usr/bin/systemctl' for argv in ordinary.commands)


def test_bounded_file_reader_rejects_excess_bytes(tmp_path, monkeypatch):
    isolate_stdlib(observation, monkeypatch)
    for name in ('O_NOFOLLOW', 'O_CLOEXEC', 'O_NONBLOCK'):
        if not hasattr(observation.os, name): monkeypatch.setattr(observation.os, name, 0, raising=False)
    (tmp_path / 'environ').write_bytes(b'x' * 65)
    with pytest.raises(ValueError, match='file_limit'):
        observation._read_process_file(tmp_path, 'environ', 64)
    assert observation._read_process_file(tmp_path, 'environ', 65) == b'x' * 65
