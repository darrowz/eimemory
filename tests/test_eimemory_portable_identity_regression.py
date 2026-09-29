from unittest.mock import Mock

import pytest

from eimemory.adapters.hermes.provider_core import HermesMemoryProviderCore


def test_authenticated_request_user_is_not_replaced_by_service_identity(monkeypatch):
    monkeypatch.setenv('EIMEMORY_USER_ID', 'service-account')
    monkeypatch.setenv('EIMEMORY_AGENT_ID', 'configured-agent')
    monkeypatch.setenv('EIMEMORY_WORKSPACE_ID', 'configured-workspace')
    scope = HermesMemoryProviderCore._scope_from_context({
        'agent_identity': 'runtime-agent',
        'agent_workspace': 'runtime-workspace',
        'user_id': 'authenticated-request-user',
    })
    assert scope['user_id'] == 'authenticated-request-user'


@pytest.mark.parametrize('context', [{}, {'user_id': None}, {'user_id': ''}, {'user_id': '  '}])
@pytest.mark.parametrize('env_user, expected', [
    (' service-account ', 'service-account'),
    (None, 'default'),
    ('', 'default'),
    ('  ', 'default'),
])
def test_missing_host_user_preserves_environment_fallback(monkeypatch, context, env_user, expected):
    if env_user is None:
        monkeypatch.delenv('EIMEMORY_USER_ID', raising=False)
    else:
        monkeypatch.setenv('EIMEMORY_USER_ID', env_user)
    scope = HermesMemoryProviderCore._scope_from_context(context)
    assert scope['user_id'] == expected
    assert scope['user_id'] != ''


def test_reinitialize_replaces_host_user_and_restores_environment_fallback(monkeypatch, tmp_path):
    monkeypatch.setenv('EIMEMORY_USER_ID', 'service-account')
    provider = HermesMemoryProviderCore(client=Mock())
    provider.initialize('session-a', user_id='user-a', hermes_home=str(tmp_path))
    assert provider._scope['user_id'] == 'user-a'
    provider.initialize('session-b', user_id='user-b', hermes_home=str(tmp_path))
    assert provider._scope['user_id'] == 'user-b'
    provider.initialize('session-c', hermes_home=str(tmp_path))
    assert provider._scope['user_id'] == 'service-account'
