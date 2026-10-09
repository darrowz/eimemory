"""Snapshot installer against real upstream Hermes host code.

* 2ef41d2b (v0.21.5+2084, currently installed on honrui): legacy ``messages``.
* v0.21.6 and main: ``sync_all`` redacts provider egress first (#115104) and
  forwards ``redacted_messages``; the snapshot must be taken from that value.
"""
from __future__ import annotations

import ast
import runpy
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
HOSTS = ROOT / 'tests/fixtures/hermes_hosts'
PATCH = runpy.run_path(str(ROOT / 'deploy/ensure_hermes_sync_snapshot.py'))
SNAPSHOT_SOURCE = ROOT / 'integrations/hermes/host/memory_sync_snapshot.py'

LEGACY = 'memory_manager_2ef41d2b.py.txt'
REDACTED = ['memory_manager_v0.21.6.py.txt', 'memory_manager_main_e0550c97.py.txt']


def _host(tmp_path: Path, fixture: str) -> Path:
    agent = tmp_path / 'agent'
    agent.mkdir()
    manager = agent / 'memory_manager.py'
    manager.write_bytes((HOSTS / fixture).read_bytes())
    return manager


@pytest.mark.parametrize(('fixture', 'contract'), [
    (LEGACY, 'legacy_messages'),
    *[(name, 'redacted_messages') for name in REDACTED],
])
def test_installer_recognizes_real_hosts_and_is_idempotent(tmp_path, fixture, contract):
    manager = _host(tmp_path, fixture)
    original = manager.read_bytes()
    preflight = PATCH['install'](tmp_path)
    assert preflight['ok'] and preflight['contract'] == contract and not preflight['applied']
    assert manager.read_bytes() == original
    report = PATCH['install'](tmp_path, apply=True)
    assert report['contract'] == contract and not report['already_installed']
    assert Path(report['backup']).read_bytes() == original
    patched = manager.read_text()
    ast.parse(patched)
    assert PATCH['NEW_LOOP'] in patched
    if contract == 'redacted_messages':
        assert PATCH['NEW_REDACTED'] in patched
        # upstream redaction stays in force and still precedes the snapshot
        assert patched.index('_redact_for_provider(\n            clean_user_content') < patched.index(
            'CompletedTurnSnapshot.capture(\n            redacted_messages')
        assert 'CompletedTurnSnapshot.capture(\n            messages,' not in patched
    else:
        assert PATCH['NEW'] in patched
    again = PATCH['install'](tmp_path, apply=True)
    assert again['already_installed'] and again['contract'] == contract
    assert manager.read_text() == patched


def test_redacting_host_is_never_patched_with_raw_messages_seam(tmp_path):
    manager = _host(tmp_path, REDACTED[0])
    # A redacting host that also (somehow) carries the legacy seam is ambiguous.
    text = manager.read_text().replace(
        PATCH['OLD_REDACTED'], PATCH['OLD_REDACTED'] + PATCH['OLD'], 1)
    manager.write_text(text)
    with pytest.raises(RuntimeError, match='host_sync_contract_unknown'):
        PATCH['install'](tmp_path, apply=True)
    assert manager.read_text() == text


def test_unknown_future_seam_fails_closed_without_mutation(tmp_path):
    manager = _host(tmp_path, REDACTED[1])
    text = manager.read_text().replace(PATCH['OLD_REDACTED'], '        optional_kwargs = build()\n', 1)
    manager.write_text(text)
    with pytest.raises(RuntimeError, match='host_sync_contract_unknown'):
        PATCH['install'](tmp_path, apply=True)
    assert manager.read_text() == text
    assert not (manager.parent / 'memory_sync_snapshot.py').exists()


def _patched_sync_all(tmp_path, fixture, monkeypatch):
    """Execute the *patched* upstream ``sync_all`` with minimal host stubs."""
    manager = _host(tmp_path, fixture)
    PATCH['install'](tmp_path, apply=True)
    tree = ast.parse(manager.read_text())
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'MemoryManager')
    func = next(n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == 'sync_all')
    module = ast.Module(body=[func], type_ignores=[])
    snap_mod = types.ModuleType('agent.memory_sync_snapshot')
    snap_mod.CompletedTurnSnapshot = runpy.run_path(str(SNAPSHOT_SOURCE))['CompletedTurnSnapshot']
    monkeypatch.setitem(sys.modules, 'agent', types.ModuleType('agent'))
    monkeypatch.setitem(sys.modules, 'agent.memory_sync_snapshot', snap_mod)
    consts = types.ModuleType('hermes_constants')
    consts.get_hermes_home = lambda: tmp_path
    monkeypatch.setitem(sys.modules, 'hermes_constants', consts)

    def redact(*values):
        def walk(v):
            if isinstance(v, str):
                return v.replace('sk-SECRET', '***')
            if isinstance(v, dict):
                return {k: walk(x) for k, x in v.items()}
            if isinstance(v, list):
                return [walk(x) for x in v]
            return v
        return tuple(walk(v) for v in values)

    from typing import Any, Dict, List, Optional
    ns = {'Any': Any, 'Dict': Dict, 'List': List, 'Optional': Optional, 'logging': __import__('logging'),
          'MemoryProvider': object, '_redact_for_provider': redact}
    exec(compile(module, str(manager), 'exec'), ns)
    queued = []
    host = types.SimpleNamespace(
        _providers=[], _strip_skill_scaffolding=lambda text: text,
        _provider_sync_accepts=lambda provider, keyword: True,
        _submit_background=lambda fn, **kw: queued.append(fn),
        _each_provider=lambda label, fn, level=None, providers=(): [fn(p) for p in providers],
    )
    return ns['sync_all'], host, queued


@pytest.mark.parametrize('fixture', [LEGACY, *REDACTED])
def test_patched_host_delivers_detached_snapshot(tmp_path, monkeypatch, fixture):
    sync_all, host, queued = _patched_sync_all(tmp_path, fixture, monkeypatch)
    calls = {}
    def provider(name, version):
        return types.SimpleNamespace(
            sync_turn_snapshot_version=version,
            sync_turn=lambda u, a, **kw: calls.__setitem__(name, (u, a, kw)))
    host._providers = [provider('snap', 1), provider('legacy', 0)]
    messages = [{'role': 'user', 'content': 'key sk-SECRET please'},
                {'role': 'assistant', 'content': 'stored sk-SECRET', 'tool_calls': [{'id': 'a'}]},
                {'role': 'assistant', 'content': 'done'}]
    sync_all(host, 'key sk-SECRET please', 'done', session_id='s', messages=messages)
    messages[1]['tool_calls'][0]['id'] = 'mutated'
    queued[0]()
    snap = calls['snap'][2]['messages']
    assert snap.session_id == 's'
    decoded = snap.messages()
    assert decoded[1]['tool_calls'][0]['id'] == 'a'
    legacy_messages = calls['legacy'][2]['messages']
    if fixture == LEGACY:
        assert decoded[0]['content'] == 'key sk-SECRET please'
    else:
        # snapshot and legacy providers both see only the redacted egress
        assert 'sk-SECRET' not in repr(decoded)
        assert 'sk-SECRET' not in repr(legacy_messages)
        assert calls['snap'][0] == 'key *** please'
