"""Read the service owner selected by native execution context, never health."""
from __future__ import annotations

import os
from pathlib import Path
import re
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
    result = subprocess.run(['systemctl', '--user', 'show', unit,
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
