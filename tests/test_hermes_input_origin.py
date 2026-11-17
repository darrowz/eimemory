"""Independent producer authority, append-only provenance and legacy rollback."""
from copy import deepcopy
from datetime import datetime, timezone
from hashlib import sha256
import json
from urllib.error import HTTPError
import urllib.request

import pytest

from eimemory.adapters.eibrain.rpc import EIBrainRPCBridge
from eimemory.adapters.eibrain.rpc_server import EIBrainRPCServer
from eimemory.adapters.runtime.channel import resolve_channel_scope
from eimemory.api.runtime import Runtime
from eimemory.retrieval.input_origin import EXTERNAL, INTERNAL, TABLE, SCHEMA, read_input_origin, text_digest
from eimemory.retrieval.proactive import ProactiveRecallService
from test_hermes_single_gateway_acceptance import operator_topology, declared


@pytest.fixture
def origin_case(tmp_path, monkeypatch):
    monkeypatch.setenv('EIMEMORY_EVIDENCE_RECEIPT_HMAC_KEY', 'OriginReceipt_Key_0123456789-Strong-Independent')
    topology = operator_topology()
    declared(monkeypatch, topology)
    runtime = Runtime.create(root=tmp_path / 'store')
    runtime.proactive = ProactiveRecallService(runtime, control_percent=0, release_identity={
        'release_commit':'a'*40, 'release_version':'1.15.87',
        'deployment_receipt_id':'receipt', 'release_session_id':'release'})
    bridge = EIBrainRPCBridge(runtime)
    query = 'An exact private question whose provenance must stay separate from success'

    def prepare(operator=False, profile_index=0, turn='memory-query-turn'):
        profile = topology['profiles'][profile_index]
        scope = resolve_channel_scope('hermes', profile['operator_scope'] if operator else profile['capture_scopes'][0]['scope'])
        common = dict(channel='hermes', scope=scope, source_ids=['hermes'], session_id='origin-private-session', turn_id=turn)
        reply = bridge.handle(dict(method='adapter.proactive_prefetch', params={**common, 'query':query}))
        assert reply['ok'] and reply['result']['decision_id']
        decision_id = reply['result']['decision_id']
        claim = dict(schema=SCHEMA, origin_kind=EXTERNAL, profile_name=profile['profile_name'], platform='feishu',
            home_digest=text_digest(profile['hermes_home']), actor_digest=text_digest(scope['user_id']),
            chat_digest=text_digest('private-chat'), message_digest=text_digest('private-message'),
            host_turn_digest=text_digest('exact-host-turn'), host_query_digest=text_digest(query),
            query_digest=text_digest(query), native_receipt_digest=text_digest('opaque-native-admission'))
        return {**common, 'decision_id':decision_id, 'origin':claim}

    yield runtime, bridge, prepare
    runtime.close()


def read(runtime, params):
    return runtime.store.read_consistent(lambda sqlite: read_input_origin(sqlite, sqlite.load_proactive_decision(params['decision_id'])))


def table_count(runtime):
    return runtime.store.sqlite.conn.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name=?", (TABLE,)).fetchone()[0]


def call(bridge, params, producer='hermes'):
    return bridge.handle(dict(method='adapter.attest_input_origin', params=params), attestation_producer=producer)


def test_missing_table_is_unknown_and_reader_cannot_migrate(origin_case):
    runtime, _, prepare = origin_case
    params = prepare()
    before = set(runtime.store.sqlite.conn.execute("SELECT name FROM sqlite_master WHERE type='table'"))
    assert read(runtime, params)['origin_kind'] == 'unknown'
    assert not read(runtime, params)['natural_learning_eligible']
    assert set(runtime.store.sqlite.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")) == before
    assert table_count(runtime) == 0


def test_later_time_window_cannot_recredit_a_retry_of_an_earlier_input(origin_case):
    from eimemory.governance.learning.effect_dataset import load_observations, credited_inputs
    runtime, bridge, prepare = origin_case
    first, retry = prepare(turn='before-window'), prepare(turn='retry-in-window')
    assert call(bridge, first)['ok'] and call(bridge, retry)['ok']
    with runtime.store.locked() as sqlite:
        for params, date in ((first, '2026-11-15T00:00:00+00:00'), (retry, '2026-11-17T00:00:00+00:00')):
            sqlite.execute('UPDATE proactive_decisions SET created_at=? WHERE decision_id=?', (date, params['decision_id']))
        sqlite.conn.commit()
    common = dict(channel='hermes', scope=first['scope'], source_ids=first['source_ids'])
    full = load_observations(runtime.store, **common)
    assert [d['decision_id'] for d in credited_inputs(full['decisions'])] == [first['decision_id']]
    window = load_observations(runtime.store, **common, start=datetime(2026, 11, 16, tzinfo=timezone.utc))
    assert [d['decision_id'] for d in window['decisions']] == [retry['decision_id']]
    assert window['decisions'][0]['input_origin']['canonical_decision_id'] == first['decision_id']
    assert credited_inputs(window['decisions']) == []


def test_host_attested_origin_is_signed_separate_and_not_a_verified_outcome(origin_case):
    runtime, bridge, prepare = origin_case
    params = prepare()
    assert call(bridge, params)['ok']
    origin = read(runtime, params)
    assert origin['verified'] and origin['origin_kind'] == EXTERNAL
    assert origin['natural_learning_eligible'] and len(origin['unique_input_digest']) == 64
    assert runtime.store.sqlite.conn.execute('SELECT COUNT(*) FROM proactive_effect_signals').fetchone()[0] == 0
    decision = runtime.store.load_proactive_decision(params['decision_id'])
    assert not decision['outcome_verified'] and decision['outcome_success'] is None
    stored = runtime.store.sqlite.conn.execute(f'SELECT payload_json FROM {TABLE}').fetchone()[0]
    assert 'exact private question' not in stored


@pytest.mark.parametrize('producer', ['', 'codex'])
def test_other_callers_cannot_self_report_external(origin_case, producer):
    runtime, bridge, prepare = origin_case
    params = prepare()
    assert call(bridge, params, producer)['ok'] is False
    assert table_count(runtime) == 0 and read(runtime, params)['origin_kind'] == 'unknown'


@pytest.mark.parametrize('field', ['query_digest', 'home_digest', 'actor_digest', 'profile_name'])
def test_namespace_and_query_claim_cannot_be_substituted(origin_case, field):
    runtime, bridge, prepare = origin_case
    params = prepare()
    params['origin'][field] = 'other-profile' if field == 'profile_name' else 'b'*64
    assert call(bridge, params)['ok'] is False
    assert table_count(runtime) == 0


def test_append_only_retries_are_idempotent_and_conflicts_cannot_rebind(origin_case):
    runtime, bridge, prepare = origin_case
    params = prepare()
    assert call(bridge, params)['result']['replayed'] is False
    assert call(bridge, deepcopy(params))['result']['replayed'] is True
    tampered = deepcopy(params)
    tampered['origin']['host_turn_digest'] = text_digest('another-host-turn')
    assert call(bridge, tampered)['ok'] is False
    assert read(runtime, params)['host_turn_digest'] == params['origin']['host_turn_digest']
    assert runtime.store.sqlite.conn.execute(f'SELECT COUNT(*) FROM {TABLE}').fetchone()[0] == 1


def test_tampering_or_missing_hmac_key_is_unknown(origin_case, monkeypatch):
    runtime, bridge, prepare = origin_case
    params = prepare()
    assert call(bridge, params)['ok']
    with runtime.store.locked() as sqlite:
        body = json.loads(sqlite.execute(f'SELECT payload_json FROM {TABLE}').fetchone()[0])
        body['claim']['host_turn_digest'] = 'a'*64
        sqlite.execute(f'UPDATE {TABLE} SET payload_json=?', (json.dumps(body),))
        sqlite.conn.commit()
    assert read(runtime, params)['origin_kind'] == 'unknown'
    monkeypatch.delenv('EIMEMORY_EVIDENCE_RECEIPT_HMAC_KEY')
    assert not read(runtime, params)['natural_learning_eligible']


def test_operator_decision_cannot_receive_external_credit(origin_case):
    runtime, bridge, prepare = origin_case
    params = prepare(operator=True)
    assert call(bridge, params)['ok'] is False
    assert table_count(runtime) == 0
    params['origin']['origin_kind'] = INTERNAL
    assert call(bridge, params)['ok']
    assert read(runtime, params)['verified']
    assert read(runtime, params)['natural_learning_eligible'] is False


def test_http_ordinary_bearer_cannot_self_report_external(origin_case):
    runtime, _, prepare = origin_case
    params = prepare()
    normal_token = 'NormalOriginToken_0123456789-Separated'
    host_token = 'HermesOriginToken_0123456789-Separated'
    foreign_token = 'CodexOriginToken_0123456789-Separated'
    server = EIBrainRPCServer(runtime, host='127.0.0.1', port=0, auth_token=normal_token,
        attestation_tokens={host_token:'hermes', foreign_token:'codex'})
    server.start()
    def post(token):
        request = urllib.request.Request(f'http://{server.address[0]}:{server.address[1]}/',
            data=json.dumps(dict(method='adapter.attest_input_origin', params=params)).encode(),
            headers={'Content-Type':'application/json', 'Authorization':'Bearer '+token})
        try:
            with urllib.request.urlopen(request, timeout=3) as response:
                return response.status, json.loads(response.read())
        except HTTPError as exc:
            return exc.code, json.loads(exc.read())
    try:
        assert post(normal_token)[0] == 401
        assert post(foreign_token)[0] == 400
        assert table_count(runtime) == 0
        assert post(host_token)[0] == 200
        assert read(runtime, params)['natural_learning_eligible']
    finally:
        server.stop()


def test_unique_input_includes_profile_scope_chat_and_message(origin_case):
    runtime, bridge, prepare = origin_case
    first = prepare()
    second = prepare(profile_index=1)
    third = prepare(turn='same-profile-other-chat')
    third['origin']['chat_digest'] = text_digest('other-private-chat')
    assert first['origin']['message_digest'] == second['origin']['message_digest'] == third['origin']['message_digest']
    for params in (first,second,third):
        assert call(bridge, params)['ok']
    assert len({read(runtime, params)['unique_input_digest'] for params in (first,second,third)}) == 3


def test_repeated_decisions_do_not_manufacture_unique_admitted_inputs(origin_case):
    runtime, bridge, prepare = origin_case
    first, second = prepare(turn='attempt-a'), prepare(turn='attempt-b')
    for params in (first,second):
        assert call(bridge, params)['ok']
    assert first['decision_id'] != second['decision_id']
    assert read(runtime, first)['unique_input_digest'] == read(runtime, second)['unique_input_digest']


def test_signing_unavailable_cannot_create_table_or_provenance(origin_case, monkeypatch):
    runtime, bridge, prepare = origin_case
    params = prepare()
    monkeypatch.setattr('eimemory.retrieval.input_origin.receipt_key_set', lambda: None)
    assert not call(bridge, params)['ok']
    assert table_count(runtime) == 0


def test_learning_and_readonly_stats_keep_rows_but_credit_unique_attested_inputs(origin_case):
    from eimemory.governance.learning.effect_dataset import load_observations, credited_inputs
    from eimemory.ops.natural_outcomes import outcome_stats
    runtime, bridge, prepare = origin_case
    first, second = prepare(turn='duplicate-attempt-a'), prepare(turn='duplicate-attempt-b')
    base = first['scope']
    dataset = load_observations(runtime.store, channel='hermes', scope=base, source_ids=['hermes'])
    assert len(dataset['decisions']) == 2 and credited_inputs(dataset['decisions']) == []
    stats = outcome_stats(runtime.store.root / 'state/eimemory.sqlite')
    assert stats['non_acceptance_candidates']['decisions'] == 2
    assert stats['decisions_by_origin'] == {'unknown': 2}
    assert stats['natural']['decisions'] == stats['natural']['unique_admitted_inputs'] == 0
    assert table_count(runtime) == 0
    for params in (first, second):
        assert call(bridge, params)['ok']
    dataset = load_observations(runtime.store, channel='hermes', scope=base, source_ids=['hermes'])
    assert len(dataset['decisions']) == 2
    assert [d['decision_id'] for d in credited_inputs(dataset['decisions'])] == [first['decision_id']]
    stats = outcome_stats(runtime.store.root / 'state/eimemory.sqlite')
    assert stats['natural']['decisions'] == 2 and stats['natural']['unique_admitted_inputs'] == 1
    assert stats['natural']['population'] == EXTERNAL


def test_origin_metadata_does_not_change_recall_order_or_policy_view(origin_case):
    from eimemory.models.records import RecordEnvelope, ScopeRef
    from eimemory.governance.learning.effect_learning import recall_policy_view
    runtime, bridge, prepare = origin_case
    params = prepare()
    for index in range(3):
        runtime.store.append(RecordEnvelope.create(kind='memory', source_id='hermes',
            scope=ScopeRef.from_dict(params['scope']), title=f'Provenance configuration {index}',
            summary=f'Provenance configuration {index} uses its own private origin metadata.',
            source='synthetic.origin-recall'))
    request = dict(method='adapter.prefetch', params=dict(channel='hermes', scope=params['scope'],
        query='provenance configuration', task_context={'source_ids':['hermes']}, limit=5))
    before = bridge.handle(request)
    view = recall_policy_view(runtime.store, channel='hermes', scope=params['scope'], source_ids=['hermes'], session_id=params['session_id'])
    assert call(bridge, params)['ok']
    after = bridge.handle(request)
    ids = lambda reply: [item['record_id'] for item in reply['result']['bundle']['items']]
    assert ids(before) and ids(after) == ids(before)
    assert recall_policy_view(runtime.store, channel='hermes', scope=params['scope'], source_ids=['hermes'], session_id=params['session_id']) == view


def test_unknown_origin_table_schema_and_non_platform_claims_fail_closed(origin_case):
    runtime, bridge, prepare = origin_case
    params = prepare()
    params['origin']['platform'] = 'api_server'
    assert not call(bridge, params)['ok'] and table_count(runtime) == 0
    runtime.store.sqlite.conn.execute(f'CREATE TABLE {TABLE} (unsupported TEXT)')
    runtime.store.sqlite.conn.commit()
    assert read(runtime, params)['origin_kind'] == 'unknown'
