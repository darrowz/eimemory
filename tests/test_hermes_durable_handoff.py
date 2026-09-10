"""Real Hermes imports and persistence; all records live in a temporary profile."""
from contextlib import closing
from copy import deepcopy
import json
import os
from types import SimpleNamespace

import pytest

from eimemory.api.runtime import Runtime
from eimemory.adapters.hermes.provider_core import HermesMemoryProviderCore
from eimemory.adapters.eibrain.rpc import EIBrainRPCBridge
from test_same_turn_project_context import fixture, FINAL


@pytest.fixture
def host(tmp_path, monkeypatch):
    source = os.environ.get('HERMES_SOURCE')
    if not source:
        pytest.skip('requires HERMES_SOURCE and real host dependencies')
    home = tmp_path / 'hermes'
    home.mkdir()
    monkeypatch.setenv('HERMES_HOME', str(home))
    monkeypatch.syspath_prepend(source)
    from hermes_state import SessionDB
    from agent.session_persistence import _db_flush_write, _db_flush_row
    db = SessionDB(home / 'state.db')
    db.create_session('session-a', source='cli', profile_name='default')
    yield home, db, _db_flush_write, _db_flush_row
    db.close()


def prepare(runtime, host, sensitive=False):
    home, db, write, row = host
    _, binding = fixture(runtime)
    tools = deepcopy(binding['messages'][:-1])
    for msg in tools:
        msg.pop('id')
    calls = [dict(id=m['tool_call_id'], type='function',
                  function=dict(name=m['tool_name'], arguments='{}')) for m in tools]
    messages = [dict(role='user', content='检查结果'),
                dict(role='assistant', content=None, tool_calls=calls),
                *tools, dict(role='assistant', content=FINAL)]
    if sensitive:
        from test_same_turn_project_context import sensitive_example
        messages[2]['content'] += '\n' + sensitive_example('api_key')
    agent = SimpleNamespace(_session_db=db, session_id='session-a')
    rows = [row(agent, m, i == 0) for i, m in enumerate(messages)]
    write(agent, rows, messages)
    assert all(m['_db_persisted'] is True for m in messages)
    assert [m['_row_id'] for m in messages] == [m['id'] for m in db.get_messages('session-a')]
    return messages


class LocalClient:
    def __init__(self, runtime):
        self.runtime, self.outputs, self.params = runtime, [], []

    def call_or_bypass(self, method, params):
        out = EIBrainRPCBridge(self.runtime).handle({'method': method, 'params': params})
        if method == 'adapter.sync_turn':
            self.params.append(deepcopy(params))
            self.outputs.append(out)
        return out


def provider_for(runtime, home):
    client = LocalClient(runtime)
    provider = HermesMemoryProviderCore(client=client)
    provider.initialize('session-a', hermes_home=str(home), user_id='synthetic-user',
                        agent_identity='synthetic-agent', agent_workspace='embodied')
    return provider, client


def test_real_persisted_handoff_creates_association(host, tmp_path):
    with closing(Runtime.create(root=tmp_path / 'runtime')) as runtime:
        messages = prepare(runtime, host)
        before = deepcopy(messages)
        provider, client = provider_for(runtime, host[0])
        from agent.memory_manager import MemoryManager
        manager = MemoryManager()
        manager.add_provider(provider)
        manager.sync_all('检查结果', FINAL, session_id='session-a', messages=messages)
        manager.shutdown_all()
        provider.shutdown()
        assert messages == before
        assert client.outputs[0]['ok']
        assert len(client.outputs[0]['result'].get('project_context_records', [])) == 1
        support = client.params[0]['supporting_turn']
        assert [m['id'] for m in support['messages']] == [m['_row_id'] for m in messages[2:]]
        assert support['turn_start_user_message_id'] == messages[0]['_row_id']
        assert provider.supporting_context_status == 'validated_durable_turn'
        print(json.dumps({'committed_row_ids': [m['_row_id'] for m in before],
                          'evidence_row_ids': [m['id'] for m in support['messages']],
                          'association_count': len(client.outputs[0]['result']['project_context_records'])}))


@pytest.mark.parametrize('mutation', ['unpersisted', 'missing_id', 'wrong_id', 'session',
    'profile', 'content', 'role', 'call_id', 'calls', 'partial', 'boundary', 'order', 'future', 'numeric_marker', 'bool_id', 'multimodal'])
def test_durable_handoff_denials(host, tmp_path, mutation):
    with closing(Runtime.create(root=tmp_path / 'runtime')) as runtime:
        messages = prepare(runtime, host)
        provider, client = provider_for(runtime, host[0])
        session = 'session-a'
        if mutation == 'numeric_marker': messages[2]['_db_persisted'] = 1
        elif mutation == 'bool_id': messages[0]['_row_id'] = True
        elif mutation == 'multimodal': messages[2]['content'] = [{'type': 'text', 'text': 'unsupported'}]
        elif mutation == 'unpersisted': messages[2]['_db_persisted'] = False
        elif mutation == 'missing_id': messages[2].pop('_row_id')
        elif mutation == 'wrong_id': messages[2]['_row_id'] = 999999
        elif mutation == 'session': session = 'session-b'
        elif mutation == 'profile':
            host[1]._conn.execute("UPDATE sessions SET profile_name='other' WHERE id='session-a'")
            host[1]._conn.commit()
        elif mutation == 'content': messages[2]['content'] += ' changed'
        elif mutation == 'role': messages[2]['role'] = 'assistant'
        elif mutation == 'call_id': messages[2]['tool_call_id'] = 'forged'
        elif mutation == 'calls': messages[1]['tool_calls'][0]['id'] = 'forged'
        elif mutation == 'partial': messages.pop(1)
        elif mutation == 'boundary': messages.pop(0)
        elif mutation == 'order': messages[2:4] = reversed(messages[2:4])
        elif mutation == 'future': host[1].append_message('session-a', 'user', 'next turn')
        if mutation == 'future':
            provider.sync_turn('检查结果', FINAL, session_id=session, messages=messages)
        else:
            from agent.memory_manager import MemoryManager
            manager = MemoryManager()
            manager.add_provider(provider)
            manager.sync_all('检查结果', FINAL, session_id=session, messages=messages)
            manager.shutdown_all()
        provider.shutdown()
        assert client.outputs[0]['ok']
        assert 'supporting_turn' not in client.params[0]
        assert not client.outputs[0]['result'].get('project_context_records')
        assert provider.supporting_context_status != 'validated_durable_turn'


def test_snapshot_before_plugin_queue(host, tmp_path, monkeypatch):
    with closing(Runtime.create(root=tmp_path / 'runtime')) as runtime:
        messages = prepare(runtime, host)
        provider, client = provider_for(runtime, host[0])
        queued = []
        enqueue = provider._enqueue_write
        monkeypatch.setattr(provider, '_enqueue_write', lambda method, params: queued.append((method, params)))
        provider.sync_turn('检查结果', FINAL, messages=messages)
        messages[2]['content'] = 'mutated after handoff'
        messages.clear()
        for method, params in queued: enqueue(method, params)
        provider.shutdown()
        assert len(client.outputs[0]['result'].get('project_context_records', [])) == 1


@pytest.mark.parametrize("race", ["list", "nested", "next_turn"])
def test_host_manager_snapshots_before_its_queue(host, tmp_path, monkeypatch, race):
    from agent.memory_manager import MemoryManager
    with closing(Runtime.create(root=tmp_path / 'runtime')) as runtime:
        messages = prepare(runtime, host)
        provider, client = provider_for(runtime, host[0])
        manager = MemoryManager()
        manager.add_provider(provider)
        queued = []
        monkeypatch.setattr(manager, '_submit_background', lambda fn, **kwargs: queued.append(fn))
        manager.sync_all('检查结果', FINAL, session_id='session-a', messages=messages)
        if race == 'list': messages.clear()
        elif race == 'nested': messages[1]['tool_calls'][0]['function']['arguments'] = 'changed'
        else: host[1].append_message('session-a', 'user', 'next turn')
        queued[0]()
        provider.shutdown()
        assert client.outputs[0]['ok']
        assert 'supporting_turn' in client.params[0]
        assert len(client.outputs[0]['result'].get('project_context_records', [])) == 1


def test_failed_real_persistence_has_no_committed_ids(host, tmp_path, monkeypatch):
    with closing(Runtime.create(root=tmp_path / 'runtime')) as runtime:
        messages = prepare(runtime, host)
        for message in messages:
            message.pop('_row_id')
            message.pop('_db_persisted')
        agent = SimpleNamespace(_session_db=host[1], session_id='session-a')
        rows = [host[3](agent, m, i == 0) for i, m in enumerate(messages)]
        # Real batch transaction aborts after row insertion, before commit/stamp.
        insert = host[1]._insert_message_rows
        def abort(*args, **kwargs):
            insert(*args, **kwargs)
            raise RuntimeError('synthetic transaction failure')
        monkeypatch.setattr(host[1], '_insert_message_rows', abort)
        with pytest.raises(RuntimeError, match='synthetic transaction failure'):
            host[2](agent, rows, messages)
        assert all('_row_id' not in m and '_db_persisted' not in m for m in messages)
        assert len(host[1].get_messages('session-a')) == len(messages)
        provider, client = provider_for(runtime, host[0])
        from agent.memory_manager import MemoryManager
        manager = MemoryManager()
        manager.add_provider(provider)
        manager.sync_all('检查结果', FINAL, session_id='session-a', messages=messages)
        manager.shutdown_all()
        provider.shutdown()
        assert client.outputs[0]['ok']
        assert 'supporting_turn' not in client.params[0]


@pytest.mark.parametrize('missing_db', [False, True])
def test_wrong_active_profile_and_unavailable_db(host, tmp_path, monkeypatch, missing_db):
    with closing(Runtime.create(root=tmp_path / 'runtime')) as runtime:
        messages = prepare(runtime, host)
        provider, client = provider_for(runtime, host[0])
        other = tmp_path / 'other-profile'
        other.mkdir()
        monkeypatch.setenv('HERMES_HOME', str(other))
        if missing_db:
            provider.initialize('session-a', hermes_home=str(other), user_id='synthetic-user')
        from agent.memory_manager import MemoryManager
        manager = MemoryManager()
        manager.add_provider(provider)
        manager.sync_all('检查结果', FINAL, session_id='session-a', messages=messages)
        manager.shutdown_all()
        provider.shutdown()
        assert client.outputs[0]['ok']
        assert 'supporting_turn' not in client.params[0]
        assert not (other / 'state.db').exists()


def test_named_profile_is_supported(host, tmp_path, monkeypatch):
    from hermes_state import SessionDB
    home = host[0] / 'profiles' / 'work'
    home.mkdir(parents=True)
    monkeypatch.setenv('HERMES_HOME', str(home))
    with closing(SessionDB(home / 'state.db')) as db:
        db.create_session('session-a', source='cli', profile_name='work')
        with closing(Runtime.create(root=tmp_path / 'runtime')) as runtime:
            messages = prepare(runtime, (home, db, *host[2:]))
            provider, client = provider_for(runtime, home)
            provider.sync_turn('检查结果', FINAL, messages=messages)
            provider.shutdown()
            assert len(client.outputs[0]['result'].get('project_context_records', [])) == 1


@pytest.mark.parametrize("change", ["continuation", "inactive", "profile", "session", "content", "flag"])
def test_delayed_snapshot_cannot_bypass_boundary_checks(host, tmp_path, monkeypatch, change):
    from agent.memory_manager import MemoryManager
    with closing(Runtime.create(root=tmp_path / 'runtime')) as runtime:
        messages = prepare(runtime, host)
        provider, client = provider_for(runtime, host[0])
        manager = MemoryManager()
        manager.add_provider(provider)
        queued = []
        monkeypatch.setattr(manager, '_submit_background', lambda fn, **kw: queued.append(fn))
        manager.sync_all('检查结果', FINAL, session_id='session-a', messages=messages)
        if change == 'continuation': host[1].append_message('session-a', 'assistant', 'extra')
        else: host[1].append_message('session-a', 'user', 'next')
        if change == 'inactive':
            host[1]._conn.execute('UPDATE messages SET active=0 WHERE id=?', (messages[2]['_row_id'],))
            host[1]._conn.commit()
        elif change == 'content':
            host[1]._conn.execute('UPDATE messages SET content=? WHERE id=?', ('changed', messages[2]['_row_id']))
            host[1]._conn.commit()
        elif change == 'profile': monkeypatch.setenv('HERMES_HOME', str(tmp_path / 'different'))
        elif change == 'session': provider._session_id = 'other'
        if change == 'flag':
            messages[0]['host_snapshot'] = True
            provider.sync_turn('检查结果', FINAL, session_id='session-a', messages=messages)
        else: queued[0]()
        provider.shutdown()
        assert client.outputs[0]['ok']
        assert 'supporting_turn' not in client.params[0]
        assert not client.outputs[0]['result'].get('project_context_records')


def test_host_snapshot_sensitive_support_is_rejected(host, tmp_path):
    from agent.memory_manager import MemoryManager
    with closing(Runtime.create(root=tmp_path / 'runtime')) as runtime:
        messages = prepare(runtime, host, sensitive=True)
        provider, client = provider_for(runtime, host[0])
        manager = MemoryManager()
        manager.add_provider(provider)
        manager.sync_all('检查结果', FINAL, session_id='session-a', messages=messages)
        manager.shutdown_all()
        provider.shutdown()
        assert client.outputs[0]['ok']
        assert provider.supporting_context_status == 'validated_durable_turn'
        assert not client.outputs[0]['result'].get('project_context_records')


def test_two_queued_completed_turns_keep_their_own_committed_ids(host, tmp_path, monkeypatch):
    from agent.memory_manager import MemoryManager
    with closing(Runtime.create(root=tmp_path / 'runtime')) as runtime:
        first = prepare(runtime, host)
        second = deepcopy(first)
        for msg in second:
            msg.pop('_row_id')
            msg.pop('_db_persisted')
        second[0]['content'] = '再次检查结果'
        provider, client = provider_for(runtime, host[0])
        manager = MemoryManager()
        manager.add_provider(provider)
        queued = []
        monkeypatch.setattr(manager, '_submit_background', lambda fn, **kw: queued.append(fn))
        manager.sync_all('检查结果', FINAL, session_id='session-a', messages=first)
        agent = SimpleNamespace(_session_db=host[1], session_id='session-a')
        rows = [host[3](agent, m, i == 0) for i, m in enumerate(second)]
        host[2](agent, rows, second)
        manager.sync_all('再次检查结果', FINAL, session_id='session-a', messages=first + second)
        for fn in queued: fn()
        provider.shutdown()
        assert len(client.params) == 2
        for params, original in zip(client.params, [first, second]):
            support = params['supporting_turn']
            assert support['turn_start_user_message_id'] == original[0]['_row_id']
            assert [m['id'] for m in support['messages']] == [m['_row_id'] for m in original[2:]]
        assert all(len(out['result'].get('project_context_records', [])) == 1 for out in client.outputs)
