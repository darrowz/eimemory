"""Install a bounded, reversible Hermes enqueue-time snapshot compatibility patch.

Only the known sync_all seam is accepted. Unknown host changes fail closed.
The immutable source module ships in the eimemory release, not in site-packages.
"""
from __future__ import annotations
import argparse
import ast
import hashlib
import json
import os
from pathlib import Path
import tempfile

OLD = '        optional_kwargs = {"messages": messages, "turn_author": turn_author}\n'
NEW = '''        # eimemory-completed-turn-snapshot.v1: freeze before entering the queue.
        from copy import deepcopy
        from agent.memory_sync_snapshot import CompletedTurnSnapshot
        from hermes_constants import get_hermes_home
        completed_snapshot = CompletedTurnSnapshot.capture(
            messages, session_id=session_id, hermes_home=get_hermes_home())
        optional_kwargs = {"messages": deepcopy(messages), "turn_author": deepcopy(turn_author)}
'''
LOOP = '            for keyword, value in optional_kwargs.items():\n'
NEW_LOOP = '''            provider_kwargs = dict(optional_kwargs)
            if getattr(provider, "sync_turn_snapshot_version", None) == 1:
                provider_kwargs["messages"] = completed_snapshot
            for keyword, value in provider_kwargs.items():
'''


def _atomic(path, data):
    mode = path.stat().st_mode & 0o777 if path.exists() else 0o644
    fd, tmp = tempfile.mkstemp(prefix='.' + path.name, dir=path.parent)
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, 'wb') as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def install(host_root, *, apply=False):
    root = Path(host_root).resolve(strict=True)
    manager = root / 'agent/memory_manager.py'
    module = root / 'agent/memory_sync_snapshot.py'
    source = Path(__file__).resolve().parents[1] / 'integrations/hermes/host/memory_sync_snapshot.py'
    if manager.is_symlink() or module.is_symlink():
        raise RuntimeError('host_snapshot_symlink_refused')
    original = manager.read_text()
    content = source.read_bytes()
    installed = NEW in original and NEW_LOOP in original
    if not installed and (original.count(OLD) != 1 or original.count(LOOP) != 1):
        raise RuntimeError('host_sync_contract_unknown')
    if module.exists() and module.read_bytes() != content:
        raise RuntimeError('host_snapshot_module_conflict')
    target = original if installed else original.replace(OLD, NEW).replace(LOOP, NEW_LOOP)
    ast.parse(target)
    ast.parse(content)
    digest = hashlib.sha256(original.encode()).hexdigest()
    backup = manager.with_name('memory_manager.py.pre-eimemory-snapshot-' + digest[:16])
    if apply:
        if not installed and not backup.exists():
            _atomic(backup, original.encode())
        _atomic(module, content)
        if not installed:
            _atomic(manager, target.encode())
        if manager.read_text() != target or module.read_bytes() != content:
            raise RuntimeError('host_snapshot_readback_failed')
    return {'ok': True, 'applied': apply, 'already_installed': installed,
            'manager_sha256': hashlib.sha256(target.encode()).hexdigest(),
            'snapshot_sha256': hashlib.sha256(content).hexdigest(),
            'backup': str(backup) if not installed else ''}


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--hermes-agent-root', required=True)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    print(json.dumps(install(args.hermes_agent_root, apply=args.apply), sort_keys=True))
