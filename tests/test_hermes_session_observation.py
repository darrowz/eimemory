"""Observe owner collisions and real stored-memory retrieval through the adapter."""
from dataclasses import asdict
import json
import logging

import pytest

from eimemory.adapters.eibrain.rpc import EIBrainRPCBridge
from eimemory.adapters.hermes.outbox import digest
from eimemory.adapters.hermes.provider_core import hermes_explicit_tool_context
from eimemory.adapters.hermes.provider_registry import get_hermes_provider
from eimemory.adapters.runtime.channel import resolve_channel_scope
from eimemory.api.runtime import Runtime
from eimemory.evaluation.explicit_recall import load_explicit_capture
from eimemory.models.records import RecordEnvelope, ScopeRef
from integrations.hermes.eimemory import EIMemoryProvider

PREFIX = 'eimemory_hermes_session_observation '
LOGGER = 'eimemory.adapters.hermes.session_observation'
QUERY = 'Which repository holds the Juniper deployment manifests?'
TEXT = 'Juniper deployment manifests are stored in the infrastructure repository.'


class Client:
    timeout_seconds = 3.5
    base_url = 'injected-runtime'

    def __init__(self, runtime):
        self.bridge = EIBrainRPCBridge(runtime)
        self.mode = 'normal'
        self.calls = []

    def call_or_bypass(self, method, params):
        self.calls.append(method)
        if self.mode == 'failure':
            raise RuntimeError('PRIVATE exception payload')
        if self.mode == 'unknown':
            return {'ok': True, 'result': {'context': TEXT}}
        if self.mode == 'scope_mismatch':
            return {'ok': True, 'result': {'ok': True, 'channel': 'hermes',
                'scope': {**resolve_channel_scope('hermes', params['scope']), 'user_id': 'other'},
                'context': TEXT, 'bundle': {'items': [{'record_id': 'forged'}]}}}
        if self.mode == 'malformed_metadata':
            return {'ok': True, 'result': {'ok': True, 'channel': 'hermes',
                'scope': resolve_channel_scope('hermes', params['scope']), 'context': TEXT,
                'bundle': {'raw_citation': {'evidence': [1], 'presentation': 'PRIVATE invalid metadata'}}}}
        return self.bridge.handle({'method': method, 'params': params})


@pytest.fixture
def scenario(tmp_path, monkeypatch, caplog):
    for key in ('EIMEMORY_AGENT_ID', 'EIMEMORY_WORKSPACE_ID', 'EIMEMORY_USER_ID',
                'EIMEMORY_DEPLOY_SCOPE_AGENT', 'EIMEMORY_DEPLOY_SCOPE_WORKSPACE'):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv('EIMEMORY_HERMES_SOURCE_IDS', 'hermes')
    monkeypatch.setenv('EIMEMORY_EVIDENCE_RECEIPT_HMAC_KEY', 'synthetic-observation-signing-key-1234567890')
    runtime = Runtime.create(root=tmp_path / 'runtime')
    client = Client(runtime)
    providers = []
    context = dict(hermes_home=str(tmp_path / 'PRIVATE profile path'),
        agent_identity='observation-agent', agent_workspace='observation-lab', user_id='PRIVATE-user')
    session = 'PRIVATE-session-for-real-host'
    caplog.set_level(logging.INFO, logger=LOGGER)

    def make(*, user=None, home=None, initialize=True):
        candidate = EIMemoryProvider(client=client)
        providers.append(candidate)
        supplied = {**context, **({'user_id': user} if user else {}),
                    **({'hermes_home': home} if home else {})}
        if initialize:
            candidate.initialize(session, **supplied)
        return candidate, supplied

    owner, _ = make()
    exact = ScopeRef.from_dict(resolve_channel_scope('hermes', owner._scope))
    target = runtime.store.append(RecordEnvelope.create(kind='memory', title='Juniper deployment manifests',
        summary=TEXT, detail=TEXT, scope=exact, source='synthetic.verified', source_id='hermes',
        meta={'memory_type': 'durable_fact', 'force_capture': True}))
    yield runtime, client, owner, make, context, session, target
    for provider in providers:
        provider.shutdown()
    runtime.close()


def events(caplog):
    records = [*caplog.get_records('setup'), *caplog.get_records('call')]
    return [json.loads(record.getMessage()[len(PREFIX):]) for record in records
            if record.name == LOGGER and record.getMessage().startswith(PREFIX)]


def recall(owner):
    args = {'query': QUERY}
    with hermes_explicit_tool_context(provider=owner, tool_name='eimemory_recall', args=args,
                                     session_id=owner._session_id, tool_call_id='real-observed-call'):
        return json.loads(owner.handle_tool_call('eimemory_recall', args))


def test_compatible_collision_has_same_namespace_and_real_signed_recall(scenario, caplog):
    runtime, _client, owner, make, context, session, target = scenario
    duplicate, supplied = make(initialize=False)
    with pytest.raises(ValueError, match='hermes_provider_session_already_bound'):
        duplicate.initialize(session, **supplied)
    assert get_hermes_provider(session, hermes_home=context['hermes_home']) is owner
    response = recall(owner)
    assert response['ok'] and any(item['record_id'] == target.record_id for item in response['result']['bundle']['items'])
    packet = load_explicit_capture(runtime, response['result']['capture']['record_id'], scope=target.scope)
    assert any(item['record_ref'] == target.record_id for item in packet.content['references'])
    observed = events(caplog)
    collision = next(item for item in observed if item['event'] == 'initialization_conflict')
    returned = next(item for item in observed if item['event'] == 'recall_result')
    assert collision['namespace_digest'] == collision['retained_namespace_digest'] == returned['namespace_digest']
    assert returned['session_digest'] == digest(packet.content['request']['session_id'])
    assert returned['scope_digest'] == digest(asdict(target.scope))
    assert returned['outcome'] == 'returned' and returned['capture_ref'] == packet.record_id
    assert all(item['observation_only'] is True for item in observed)


def test_foreign_attempt_cannot_inherit_retained_owner_recall(scenario, caplog):
    _runtime, _client, owner, make, context, session, _target = scenario
    duplicate, supplied = make(user='PRIVATE-other-user', initialize=False)
    with pytest.raises(ValueError, match='hermes_provider_session_already_bound'):
        duplicate.initialize(session, **supplied)
    assert get_hermes_provider(session, hermes_home=context['hermes_home']) is owner
    assert not duplicate._active
    assert recall(owner)['ok']
    collision = next(item for item in events(caplog) if item['event'] == 'initialization_conflict')
    returned = next(item for item in events(caplog) if item['event'] == 'recall_result')
    assert collision['namespace_digest'] != collision['retained_namespace_digest']
    assert collision['namespace_digest'] != returned['namespace_digest']
    assert collision['retained_namespace_digest'] == returned['namespace_digest']


def test_same_session_in_another_home_has_separate_namespace(scenario, caplog, tmp_path):
    _runtime, _client, owner, make, _context, session, _target = scenario
    another, supplied = make(home=str(tmp_path / 'PRIVATE another profile'))
    assert get_hermes_provider(session, hermes_home=supplied['hermes_home']) is another
    initialized = [item for item in events(caplog) if item['event'] == 'initialized']
    assert len(initialized) == 2
    assert initialized[0]['session_digest'] == initialized[1]['session_digest']
    assert initialized[0]['namespace_digest'] != initialized[1]['namespace_digest']
    assert initialized[0]['home_digest'] != initialized[1]['home_digest']
    assert owner._active and another._active


def test_attempted_home_uses_the_initialization_environment_fallback(scenario, caplog, monkeypatch):
    _runtime, _client, owner, make, context, session, _target = scenario
    monkeypatch.setenv('HERMES_HOME', context['hermes_home'])
    duplicate, supplied = make(initialize=False)
    supplied.pop('hermes_home')
    with pytest.raises(ValueError, match='hermes_provider_session_already_bound'):
        duplicate.initialize(session, **supplied)
    collision = next(item for item in events(caplog) if item['event'] == 'initialization_conflict')
    assert collision['home_digest'] == digest(owner._hermes_home)
    assert not duplicate._active


@pytest.mark.parametrize('mode', ['failure', 'unknown', 'scope_mismatch'])
def test_failed_or_unknown_recall_never_observed_as_returned(scenario, caplog, mode):
    _runtime, client, owner, _make, _context, _session, _target = scenario
    client.mode = mode
    owner._expected_scope = dict(owner._scope)
    response = recall(owner)
    assert not response.get('ok') or response.get('result', {}).get('ok') is not True
    observed = [item for item in events(caplog) if item['event'] == 'recall_result']
    assert observed and observed[-1]['outcome'] == 'unavailable'
    assert 'PRIVATE exception payload' not in caplog.text


def test_successful_empty_recall_is_empty_observation(scenario, caplog):
    runtime, _client, owner, _make, _context, _session, target = scenario
    target.status = 'archived'
    runtime.store.append(target)
    response = recall(owner)
    assert response['ok'] and response['result']['ok']
    assert not response['result']['bundle']['items']
    observed = [item for item in events(caplog) if item['event'] == 'recall_result']
    assert observed and observed[-1]['outcome'] == 'empty'


def test_observation_exports_only_hashes_and_fixed_metadata(scenario, caplog):
    _runtime, _client, owner, make, context, session, _target = scenario
    duplicate, supplied = make(initialize=False)
    with pytest.raises(ValueError):
        duplicate.initialize(session, **supplied)
    assert recall(owner)['ok']
    observed = events(caplog)
    assert observed
    rendered = json.dumps(observed)
    for private in (session, context['hermes_home'], context['user_id'], QUERY, TEXT):
        assert private not in rendered
    for item in observed:
        assert item['schema'] == 'eimemory.hermes_session_observation.v1'
        for key in ('scope_digest', 'source_ids_digest', 'home_digest', 'session_digest', 'namespace_digest'):
            assert len(item[key]) == 64 and set(item[key]) <= set('0123456789abcdef')


def test_proactive_prefetch_observes_actual_stored_memory(scenario, caplog):
    _runtime, _client, owner, _make, _context, _session, _target = scenario
    context = owner.prefetch(QUERY)
    assert 'infrastructure repository' in context
    observed = [item for item in events(caplog) if item['event'] == 'recall_result']
    assert observed and observed[-1]['outcome'] == 'returned'
    assert observed[-1]['operation'] == 'prefetch'


def test_logging_failure_preserves_guard_and_recall(scenario, caplog, monkeypatch):
    _runtime, _client, owner, make, _context, session, _target = scenario
    logger = logging.getLogger(LOGGER)
    def fail(*args, **kwargs):
        raise OSError('logger unavailable')
    monkeypatch.setattr(logger, 'log', fail)
    duplicate, supplied = make(initialize=False)
    with pytest.raises(ValueError, match='hermes_provider_session_already_bound'):
        duplicate.initialize(session, **supplied)
    assert recall(owner)['ok']


def test_malformed_observation_metadata_keeps_original_response(scenario, caplog):
    _runtime, client, owner, _make, _context, _session, _target = scenario
    client.mode = 'malformed_metadata'
    response = recall(owner)
    assert response['ok'] and response['result']['ok']
    assert response['result']['bundle']['raw_citation']['presentation'] == 'PRIVATE invalid metadata'
    observed = [item for item in events(caplog) if item['event'] == 'recall_result']
    assert observed and observed[-1]['outcome'] == 'unavailable'
    assert 'PRIVATE invalid metadata' not in json.dumps(observed)


def test_unchecked_foreign_response_cannot_observe_owner_recovery(scenario, caplog):
    _runtime, client, owner, _make, _context, _session, _target = scenario
    client.mode = 'scope_mismatch'
    assert owner._expected_scope is None
    response = recall(owner)
    assert response['ok'] and response['result']['scope']['user_id'] == 'other'
    observed = [item for item in events(caplog) if item['event'] == 'recall_result']
    assert observed and observed[-1]['outcome'] == 'unavailable'
