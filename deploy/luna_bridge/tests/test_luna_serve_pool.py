"""Persistent bridge worker (--serve) and the eimemory bridge pool client.

Fake Hermes host only: no network, credentials, or production storage.
"""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

BRIDGE = Path(__file__).resolve().parents[1] / 'luna_review_command.py'
REPO = Path(__file__).resolve().parents[3]


def _fake_host(tmp_path, *, fail_marker='FAIL'):
    (tmp_path / 'agent').mkdir()
    (tmp_path / 'agent/__init__.py').write_text('')
    (tmp_path / 'hermes_bootstrap.py').write_text(
        'import os\nif os.environ.get("FAKE_NOISE"):\n    print("host noise on stdout")\n')
    (tmp_path / 'agent/auxiliary_client.py').write_text(f'''
import os, sys
from types import SimpleNamespace
SETUPS = []
def resolve_provider_client(provider, *, model):
    SETUPS.append(provider)
    counter = os.environ.get('FAKE_SETUP_COUNTER')
    if counter:
        with open(counter, 'a') as handle:
            handle.write('setup\\n')
    def create(**kwargs):
        if os.environ.get('FAKE_NOISE'):
            print('library chatter')
            sys.stderr.write('secret-ish library warning\\n')
        user = kwargs['messages'][1]['content']
        if {fail_marker!r} in user:
            raise RuntimeError('fixture-sensitive-marker')
        return SimpleNamespace(model=model, choices=[SimpleNamespace(
            message=SimpleNamespace(content=json.dumps({{"echo": user}}), tool_calls=None))])
    import json
    return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create))), model
''')
    return {'PATH': os.environ.get('PATH', ''), 'PYTHONDONTWRITEBYTECODE': '1',
            'EIMEMORY_HERMES_AGENT_ROOT': str(tmp_path),
            'EIMEMORY_LUNA_PROVIDER': 'fixture-provider', 'EIMEMORY_LUNA_MODEL': 'fixture-model',
            'FAKE_SETUP_COUNTER': str(tmp_path / 'setups.txt')}


def _request(request_id, user, deadline_s=30):
    import time
    return json.dumps({'request_id': request_id, 'system_prompt': 's', 'user_prompt': user,
                       'json_mode': True, 'deadline_unix_ms': int((time.time() + deadline_s) * 1000)}) + '\n'


def test_serve_handles_sequential_requests_with_one_client_setup_and_clean_protocol(tmp_path):
    env = {**_fake_host(tmp_path), 'FAKE_NOISE': '1'}
    requests = _request('a' * 32, 'one') + _request('b' * 32, 'FAIL two') + _request('c' * 32, 'three')
    result = subprocess.run([sys.executable, '-B', str(BRIDGE), '--serve'], input=requests,
                            capture_output=True, text=True, env=env, timeout=30)
    assert result.returncode == 0
    lines = [json.loads(line) for line in result.stdout.splitlines()]
    # Only protocol frames on stdout: host/library prints never reach it.
    assert [line['request_id'] for line in lines] == ['a' * 32, 'b' * 32, 'c' * 32]
    assert json.loads(lines[0]['result']['text']) == {'echo': 'one'}
    assert lines[0]['result']['provider_id'] == 'fixture-provider'
    assert lines[0]['result']['model_id'] == 'fixture-model'
    assert lines[1]['error'] is True and lines[1]['reason'] == 'provider_request_failed'
    assert 'fixture-sensitive-marker' not in result.stdout + result.stderr
    assert json.loads(lines[2]['result']['text']) == {'echo': 'three'}
    # A failure discards the cached client; otherwise setup is reused.
    assert (tmp_path / 'setups.txt').read_text().count('setup') == 2


def test_serve_rejects_a_request_without_a_valid_request_id(tmp_path):
    env = _fake_host(tmp_path)
    result = subprocess.run([sys.executable, '-B', str(BRIDGE), '--serve'],
                            input='{"request_id": "x", "system_prompt": "s", "user_prompt": "u"}\n',
                            capture_output=True, text=True, env=env, timeout=30)
    assert result.returncode == 1 and result.stdout == ''


def test_one_shot_protocol_is_unchanged(tmp_path):
    env = _fake_host(tmp_path)
    result = subprocess.run([sys.executable, '-B', str(BRIDGE)],
                            input=json.dumps({'system_prompt': 's', 'user_prompt': 'u', 'json_mode': True}),
                            capture_output=True, text=True, env=env, timeout=30)
    assert result.returncode == 0
    payload = json.loads(result.stdout)
    assert set(payload) == {'text', 'provider_id', 'model_id', 'diagnostics'}


@pytest.fixture
def pooled_env(tmp_path, monkeypatch):
    env = _fake_host(tmp_path)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv('EIMEMORY_LLM_ENV_ALLOW', 'EIMEMORY_HERMES_AGENT_ROOT,FAKE_SETUP_COUNTER')
    sys.path.insert(0, str(REPO))
    from eimemory.llm import bridge_pool
    bridge_pool.close_pool()
    yield bridge_pool, tmp_path
    bridge_pool.close_pool()


def test_pool_client_reuses_one_worker_and_passes_the_route(pooled_env):
    bridge_pool, tmp_path = pooled_env
    from eimemory.llm.command_client import bind_verifier_route, reset_verifier_route
    argv = [sys.executable, '-B', str(BRIDGE)]
    assert bridge_pool.pool_enabled(argv)
    client = bridge_pool.BridgePoolClient(argv, identity_key='k', timeout_seconds=20)
    client.prepare()
    token = bind_verifier_route({'provider': 'route-provider', 'model': 'route-model'})
    try:
        first = client.complete(system_prompt='s', user_prompt='one', json_mode=True)
        second = client.complete(system_prompt='s', user_prompt='two', json_mode=True)
    finally:
        reset_verifier_route(token)
    assert json.loads(first.text) == {'echo': 'one'} and json.loads(second.text) == {'echo': 'two'}
    assert first.provider_id == 'route-provider' and first.model_id == 'route-model'
    assert first.diagnostics['command_pooled'] is True
    pool = bridge_pool._shared_pool(tuple(argv), 'k')
    assert len(pool.workers) == 1
    assert (tmp_path / 'setups.txt').read_text().count('setup') == 1


def test_pool_error_is_fail_closed_and_discards_the_worker(pooled_env):
    bridge_pool, _tmp = pooled_env
    from eimemory.llm.command_client import CommandCompletionError
    argv = [sys.executable, '-B', str(BRIDGE)]
    client = bridge_pool.BridgePoolClient(argv, identity_key='k2', timeout_seconds=20)
    with pytest.raises(CommandCompletionError) as caught:
        client.complete(system_prompt='s', user_prompt='FAIL', json_mode=True)
    assert caught.value.failure_category == 'provider_request_failed'
    pool = bridge_pool._shared_pool(tuple(argv), 'k2')
    assert all(worker.closed for worker in pool.workers)
    # The next call gets a fresh worker and succeeds.
    assert json.loads(client.complete(system_prompt='s', user_prompt='ok').text) == {'echo': 'ok'}


def test_saturated_pool_falls_back_to_one_shot(pooled_env, monkeypatch):
    bridge_pool, _tmp = pooled_env
    argv = [sys.executable, '-B', str(BRIDGE)]
    client = bridge_pool.BridgePoolClient(argv, identity_key='k3', timeout_seconds=20)
    monkeypatch.setattr(bridge_pool._BridgePool, 'acquire', lambda self: None)
    result = client.complete(system_prompt='s', user_prompt='busy', json_mode=True)
    assert json.loads(result.text) == {'echo': 'busy'}
    assert result.diagnostics['command_pooled'] is False
    assert result.diagnostics['pool_fallback'] == 'pool_saturated'


def test_pool_can_be_disabled_and_other_commands_are_never_pooled(monkeypatch):
    sys.path.insert(0, str(REPO))
    from eimemory.llm import bridge_pool
    argv = [sys.executable, '-B', str(BRIDGE)]
    monkeypatch.setenv('EIMEMORY_RECALL_BRIDGE_POOL', '0')
    assert not bridge_pool.pool_enabled(argv)
    monkeypatch.delenv('EIMEMORY_RECALL_BRIDGE_POOL')
    assert not bridge_pool.pool_enabled(['node', '/x/other_bridge.mjs'])


def test_pool_flag_is_transport_only_and_not_part_of_verifier_identity(monkeypatch):
    sys.path.insert(0, str(REPO))
    from eimemory.retrieval import caller_assistance
    monkeypatch.setenv('EIMEMORY_RECALL_BRIDGE_POOL', '1')
    on = caller_assistance.identity()['configuration_digest']
    monkeypatch.setenv('EIMEMORY_RECALL_BRIDGE_POOL', '0')
    assert caller_assistance.identity()['configuration_digest'] == on
