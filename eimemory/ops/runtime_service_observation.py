"""Read the service owner selected by native execution context, never health."""
from __future__ import annotations

import os
from pathlib import Path
import re
import stat
import subprocess


def registered_runtime_context() -> bool:
    from eimemory.contracts.user_control_plane import user_mode
    if user_mode():
        # The user controller owns native process inspection outside the
        # product PID namespace; product services cannot inspect host peers.
        return True
    job = os.environ.get('EIMEMORY_RUNTIME_TRANSITION_JOB', '')
    if job:
        if re.fullmatch('[0-9a-f]{64}', job) is None:
            raise ValueError('runtime_transition_job_invalid')
        return True
    # Registered product entry points run directly in PID1's system slice.
    # User services and an ordinary operator shell retain the user manager.
    try:
        rows = Path('/proc/self/cgroup').read_text().splitlines()
    except FileNotFoundError:
        return False
    for row in rows:
        parts = row.split(':', 2)[-1].split('/')
        if 'system.slice' in parts and re.fullmatch(r'eimemory-[A-Za-z0-9_.@\\-]+\.service', parts[-1]):
            return True
    return False


def service_state(unit: str, properties: tuple[str, ...]) -> dict[str, str]:
    if re.fullmatch(r'[A-Za-z0-9_.@-]+\.(service|timer|path)', unit) is None:
        raise ValueError('runtime_service_unit_invalid')
    if registered_runtime_context():
        from eimemory.core.runtime_manager import unit_state
        return unit_state(unit, properties)
    return _user_service_state(unit, properties)


def _user_service_state(unit: str, properties: tuple[str, ...], *, host_binary=False) -> dict[str, str]:
    """Observe the already-selected user transport; do not reselect on failure."""
    result = subprocess.run(['/usr/bin/systemctl' if host_binary else 'systemctl', '--user', 'show', unit,
        '--property=' + ','.join(properties)], capture_output=True, text=True,
        timeout=30, check=False)
    if result.returncode:
        raise RuntimeError('runtime_user_manager_read_failed')
    state = {}
    for line in result.stdout.splitlines():
        name, separator, value = line.partition('=')
        if not separator or name not in properties or name in state:
            raise ValueError('runtime_user_manager_response_invalid')
        state[name] = value
    if set(state) != set(properties):
        if state.get('LoadState') == 'not-found':
            # systemd omits type-specific attributes for an absent unit.
            # Keep its observed absence; empty values grant no running owner.
            return {name: state.get(name, '') for name in properties}
        raise ValueError('runtime_user_manager_response_incomplete')
    return state


def _validate_process_observation(value, unit, state=None):
    fields = {'schema', 'unit', 'pid', 'start_ticks', 'invocation_id', 'environment'}
    if (not isinstance(value, dict) or set(value) not in (fields, fields | {'namespace_pids'})
            or value['schema'] != 'eimemory.runtime_process_environment.v1' or value['unit'] != unit
            or type(value['pid']) is not int or value['pid'] <= 0
            or type(value['start_ticks']) is not int or value['start_ticks'] <= 0
            or not isinstance(value['invocation_id'], str)
            or re.fullmatch('[0-9a-f]{32}', value['invocation_id']) is None
            or not isinstance(value['environment'], dict)
            or any(not isinstance(k, str) or not isinstance(v, str) or '\0' in k + v
                   for k, v in value['environment'].items())):
        raise ValueError('runtime_host_process_observation_invalid')
    if state is not None and (state.get('Id') != unit or str(value['pid']) != state.get('MainPID')):
        raise ValueError('runtime_host_process_observation_invalid')
    pids = value.get('namespace_pids', [])
    if (not isinstance(pids, list) or len(pids) > 64
            or any(type(pid) is not int or pid <= 0 for pid in pids)
            or pids and pids[0] != value['pid'] or len(set(pids)) != len(pids)):
        raise ValueError('runtime_host_process_observation_invalid')
    return value


def _read_process_file(process, name, maximum=65536):
    fd = os.open(process / name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ValueError('runtime_process_file_invalid')
        chunks, remaining = [], maximum + 1
        while remaining:
            part = os.read(fd, remaining)
            if not part:
                break
            chunks.append(part)
            remaining -= len(part)
        raw = b''.join(chunks)
        if len(raw) > maximum:
            raise ValueError('runtime_process_file_limit')
        return raw
    finally:
        os.close(fd)


def _native_process_identity(process, uid, pid, group):
    info = process.stat()
    status = {}
    for line in _read_process_file(process, 'status').decode().splitlines():
        key, separator, value = line.partition(':')
        if not separator or key in status:
            raise ValueError('runtime_process_status_invalid')
        status[key] = value.split()
    if info.st_uid != uid or status.get('Uid') != [str(uid)] * 4:
        raise ValueError('runtime_process_owner_mismatch')
    tokens = status.get('NSpid', [])
    if (not 1 <= len(tokens) <= 32 or any(re.fullmatch('[0-9]+', item) is None
            or int(item) <= 0 for item in tokens) or int(tokens[0]) != pid
            or len({int(item) for item in tokens}) != len(tokens)):
        raise ValueError('runtime_process_namespace_pids_invalid')
    groups = _read_process_file(process, 'cgroup').decode().splitlines()
    unified = [row[3:] for row in groups if row.startswith('0::')]
    if len(unified) != 1 or not (unified[0] == group or unified[0].startswith(group + '/')):
        raise ValueError('runtime_process_cgroup_mismatch')
    raw_stat = _read_process_file(process, 'stat').decode()
    try:
        if raw_stat.split('(', 1)[0].strip() != str(pid):
            raise ValueError
        start = int(raw_stat.rsplit(')', 1)[1].split()[19])
        if start <= 0:
            raise ValueError
    except (IndexError, ValueError) as exc:
        raise ValueError('runtime_process_start_invalid') from exc
    return (info.st_dev, info.st_ino, info.st_uid, tuple(tokens), unified[0], start)


def _ordinary_host_identity(owner):
    """Do not treat a product's partial user namespace as the host account."""
    if os.getgid() != owner.pw_gid:
        raise ValueError('runtime_process_observer_identity_invalid')
    status = {}
    for line in _read_process_file(Path('/proc/self'), 'status').decode().splitlines():
        key, separator, value = line.partition(':')
        if not separator or key in status:
            raise ValueError('runtime_process_observer_identity_invalid')
        status[key] = value.split()
    if (status.get('Uid') != [str(owner.pw_uid)] * 4
            or status.get('Gid') != [str(owner.pw_gid)] * 4):
        raise ValueError('runtime_process_observer_identity_invalid')
    for name in ('uid_map', 'gid_map'):
        for process in ('self', '1'):
            rows = [row.split() for row in _read_process_file(Path('/proc') / process, name).decode().splitlines()]
            if rows != [['0', '0', str(2**32 - 1)]]:
                raise ValueError('runtime_process_host_namespace_required')


def runtime_process_observation(unit: str, *, state=None) -> dict:
    """Observe a registered owner or a declared ordinary user's fixed runtime.

    The declaration authorizes no manager operation. Managed errors never fall
    back to /proc; ordinary reads never synthesize the manager's RPC exec seal.
    """
    if registered_runtime_context():
        from eimemory.core.runtime_manager import request
        return _validate_process_observation(
            request('runtime_process_environment', unit=unit)['observation'], unit, state)
    from eimemory.contracts.hermes_topology import RUNTIME_UNITS, load_verified_topology
    from eimemory.contracts.user_control_plane import account
    owner = account()
    _ordinary_host_identity(owner)
    topology = load_verified_topology()
    if (unit not in RUNTIME_UNITS or topology['runtime_units'] != list(RUNTIME_UNITS)
            or topology['runtime_uid'] != owner.pw_uid or topology['runtime_gid'] != owner.pw_gid):
        raise ValueError('runtime_process_topology_owner_mismatch')
    properties = ('Id', 'LoadState', 'ActiveState', 'MainPID', 'InvocationID', 'ControlGroup')
    before = _user_service_state(unit, properties, host_binary=True)
    if (before['Id'] != unit or before['LoadState'] != 'loaded' or before['ActiveState'] != 'active'
            or re.fullmatch('[1-9][0-9]*', before['MainPID']) is None
            or re.fullmatch('[0-9a-f]{32}', before['InvocationID']) is None):
        raise ValueError('runtime_process_unit_not_running')
    group = before['ControlGroup']
    prefix = f'/user.slice/user-{owner.pw_uid}.slice/user@{owner.pw_uid}.service/'
    if group not in {prefix + section + '/' + unit for section in
                      ('app.slice', 'background.slice', 'session.slice')}:
        raise ValueError('runtime_process_user_cgroup_invalid')
    pid = int(before['MainPID'])
    process = Path('/proc') / str(pid)
    identity = _native_process_identity(process, owner.pw_uid, pid, group)
    raw = _read_process_file(process, 'environ', 1048576)
    environment = {}
    for item in raw.decode().split('\0'):
        if not item:
            continue
        key, separator, value = item.partition('=')
        if not separator or not key or key in environment:
            raise ValueError('runtime_process_environment_invalid')
        environment[key] = value
    if (_read_process_file(process, 'environ', 1048576) != raw
            or _native_process_identity(process, owner.pw_uid, pid, group) != identity
            or _user_service_state(unit, properties, host_binary=True) != before):
        raise ValueError('runtime_process_identity_changed')
    value = dict(schema='eimemory.runtime_process_environment.v1', unit=unit, pid=pid,
                 start_ticks=identity[-1], invocation_id=before['InvocationID'],
                 namespace_pids=[int(item) for item in identity[3]], environment=environment)
    return _validate_process_observation(value, unit, state)


def runtime_environment(unit: str) -> str | None:
    registered = registered_runtime_context()
    properties = ('LoadState',) if registered else ('LoadState', 'Environment')
    state = service_state(unit, properties)
    if state['LoadState'] == 'not-found':
        return None
    if state['LoadState'] != 'loaded':
        raise ValueError('runtime_service_load_state_invalid')
    if registered:
        from eimemory.core.runtime_manager import runtime_commit
        return 'EIMEMORY_RUNTIME_COMMIT=' + runtime_commit(unit)
    return state['Environment']
