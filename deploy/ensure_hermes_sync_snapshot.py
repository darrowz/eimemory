"""Install a bounded, reversible Hermes enqueue-time snapshot compatibility patch.

Only the known sync_all seams are accepted. Unknown host changes fail closed.
The immutable source module ships in the eimemory release, not in site-packages.

Two upstream host shapes are recognized:

* legacy (<= v0.21.5+2084, e.g. 2ef41d2b): ``messages`` is forwarded as-is;
* redacted (v0.21.6 / main, upstream #115104): ``sync_all`` first runs
  ``_redact_for_provider`` and forwards ``redacted_messages``.  The snapshot is
  captured from ``redacted_messages`` so upstream secret redaction is kept for
  snapshot-opted providers too; the raw transcript never reaches a provider.
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
# Hermes v0.21.6+ (upstream #115104): provider egress is redacted first.
OLD_REDACTED = '        optional_kwargs = {"messages": redacted_messages, "turn_author": turn_author}\n'
NEW_REDACTED = '''        # eimemory-completed-turn-snapshot.v1 (redacted host): freeze the
        # already-redacted provider egress before entering the queue.
        from copy import deepcopy
        from agent.memory_sync_snapshot import CompletedTurnSnapshot
        from hermes_constants import get_hermes_home
        completed_snapshot = CompletedTurnSnapshot.capture(
            redacted_messages, session_id=session_id, hermes_home=get_hermes_home())
        optional_kwargs = {"messages": deepcopy(redacted_messages), "turn_author": deepcopy(turn_author)}
'''
REDACTION_CALL = '_redact_for_provider('
LOOP = '            for keyword, value in optional_kwargs.items():\n'
NEW_LOOP = '''            provider_kwargs = dict(optional_kwargs)
            if getattr(provider, "sync_turn_snapshot_version", None) == 1:
                provider_kwargs["messages"] = completed_snapshot
            for keyword, value in provider_kwargs.items():
'''
# (contract name, seam, replacement, extra required marker)
CONTRACTS = (
    ('legacy_messages', OLD, NEW, None),
    ('redacted_messages', OLD_REDACTED, NEW_REDACTED, REDACTION_CALL),
)


def _detect(original):
    """Return ``(contract, seam, replacement, installed)`` or fail closed."""
    for name, seam, replacement, marker in CONTRACTS:
        if replacement in original and NEW_LOOP in original:
            if seam in original or original.count(replacement) != 1:
                raise RuntimeError('host_sync_contract_unknown')
            return name, seam, replacement, True
    matches = [
        (name, seam, replacement)
        for name, seam, replacement, marker in CONTRACTS
        if original.count(seam) == 1 and (marker is None or marker in original)
    ]
    if len(matches) != 1 or original.count(LOOP) != 1:
        raise RuntimeError('host_sync_contract_unknown')
    if matches[0][0] == 'legacy_messages' and 'redacted_messages' in original:
        # A redacting host must never be patched to forward raw messages.
        raise RuntimeError('host_sync_contract_unknown')
    return (*matches[0], False)

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
    contract, seam, replacement, installed = _detect(original)
    if module.exists() and module.read_bytes() != content:
        raise RuntimeError('host_snapshot_module_conflict')
    target = original if installed else original.replace(seam, replacement).replace(LOOP, NEW_LOOP)
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
    return {'ok': True, 'applied': apply, 'already_installed': installed, 'contract': contract,
            'manager_sha256': hashlib.sha256(target.encode()).hexdigest(),
            'snapshot_sha256': hashlib.sha256(content).hexdigest(),
            'backup': str(backup) if not installed else ''}


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--hermes-agent-root', required=True)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    print(json.dumps(install(args.hermes_agent_root, apply=args.apply), sort_keys=True))
