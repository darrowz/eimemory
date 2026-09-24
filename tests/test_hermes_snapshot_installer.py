"""Host compatibility patch is explicit, idempotent and fail-closed."""
from dataclasses import FrozenInstanceError
from pathlib import Path
import runpy

import pytest

ROOT = Path(__file__).resolve().parents[1]
PATCH = runpy.run_path(str(ROOT / 'deploy/ensure_hermes_sync_snapshot.py'))
Snapshot = runpy.run_path(str(ROOT / 'integrations/hermes/host/memory_sync_snapshot.py'))['CompletedTurnSnapshot']


def host(tmp_path):
    agent = tmp_path / 'agent'
    agent.mkdir()
    p = agent / 'memory_manager.py'
    p.write_text('class Manager:\n    def sync_all(self, messages, turn_author):\n'
                 + PATCH['OLD'] + '\n        def sync(provider):\n'
                 + PATCH['LOOP'] + '                pass\n')
    return p


def test_installer_preflight_apply_and_idempotence(tmp_path):
    p = host(tmp_path)
    original = p.read_bytes()
    report = PATCH['install'](tmp_path)
    assert report['ok'] and not report['applied']
    assert p.read_bytes() == original
    assert not (p.parent / 'memory_sync_snapshot.py').exists()
    report = PATCH['install'](tmp_path, apply=True)
    assert Path(report['backup']).read_bytes() == original
    changed = p.read_bytes()
    again = PATCH['install'](tmp_path, apply=True)
    assert again['already_installed']
    assert p.read_bytes() == changed


@pytest.mark.parametrize('conflict', ['unknown_manager', 'different_module', 'symlink'])
def test_installer_refuses_unknown_host_without_mutation(tmp_path, conflict):
    p = host(tmp_path)
    m = p.parent / 'memory_sync_snapshot.py'
    if conflict == 'unknown_manager':
        p.write_text('class Other: pass\n')
    elif conflict == 'different_module':
        m.write_text('raise RuntimeError("upstream implementation")\n')
    else:
        m.symlink_to(p)
    before = p.read_bytes()
    with pytest.raises(RuntimeError):
        PATCH['install'](tmp_path, apply=True)
    assert p.read_bytes() == before


def test_snapshot_freezes_nested_turn_and_returns_independent_copies(tmp_path):
    messages = [{'role': 'user', 'content': 'first'}, {'role': 'assistant', 'content': 'done'},
                {'role': 'user', 'content': 'second'},
                {'role': 'assistant', 'content': None, 'tool_calls': [{'id': 'a'}]},
                {'role': 'assistant', 'content': 'complete'}]
    snap = Snapshot.capture(messages, session_id='s', hermes_home=tmp_path)
    assert snap.session_id == 's'
    assert snap.hermes_home == str(tmp_path)
    messages[-2]['tool_calls'][0]['id'] = 'changed'
    messages.clear()
    assert snap.messages()[1]['tool_calls'][0]['id'] == 'a'
    assert snap.messages()[0]['content'] == 'second'
    out = snap.messages()
    out.clear()
    assert len(snap.messages()) == 3
    with pytest.raises(FrozenInstanceError):
        snap.session_id = 'forged'


@pytest.mark.parametrize('messages', [None, [], [{'role': 'assistant'}], [object()]])
def test_snapshot_rejects_unsupported_shape(messages, tmp_path):
    assert Snapshot.capture(messages, session_id='s', hermes_home=tmp_path) is None
