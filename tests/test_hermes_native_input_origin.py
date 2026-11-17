"""Actual native companion capability and real provider callback isolation."""
from contextlib import contextmanager
from dataclasses import dataclass, field
from importlib.util import spec_from_file_location, module_from_spec
from pathlib import Path
import json
import sys
from types import ModuleType, SimpleNamespace

import pytest

from eimemory.adapters.hermes.input_origin import OriginObserver, MAX_ITEMS, MAX_BYTES, MAX_TURN_SECONDS
from eimemory.adapters.hermes.effect_observer import EffectObserver
from eimemory.adapters.runtime.channel import resolve_channel_scope
from eimemory.retrieval.input_origin import read_input_origin, EXTERNAL, INTERNAL, UNKNOWN
from test_real_effect_signals import live


@pytest.fixture
def native_origin(live, monkeypatch):
    runtime, bridge, provider, host, calls = live
    provider._origin_test_runtime = runtime
    monkeypatch.setenv('EIMEMORY_CAPTURE_INPUT_ORIGIN', '1')
    monkeypatch.setenv('EIMEMORY_EVIDENCE_RECEIPT_HMAC_KEY', 'NativeOrigin_Key_0123456789-Independent')
    monkeypatch.setattr('eimemory.adapters.hermes.input_origin.hermes_producer_token',
                        lambda: 'SyntheticHostOriginToken_0123456789-Separated')
    state = dict(profile='default', home=provider._hermes_home)
    gateway = ModuleType('gateway')
    gateway.__path__ = []
    event_module = ModuleType('gateway.platforms.event')
    @dataclass
    class MessageEvent:
        source: object
        message_id: str = 'platform-message'
        internal: bool = False
        metadata: dict = field(default_factory=dict)
        inherit_channel_pin: bool = False
        edit_revision: str = ''
        _gateway_accepted: bool = field(default=False, init=False)
    event_module.MessageEvent = MessageEvent
    filters = ModuleType('gateway.response_filters')
    filters.display_kind_for_event = lambda event: 'internal_notification' if event.internal else None
    filters.is_machinery_display_kind = lambda value: value == 'internal_notification'
    display = ModuleType('gateway.session_display')
    display.admission_submission = lambda event: {}
    constants = ModuleType('hermes_constants')
    constants.get_hermes_home = lambda: state['home']
    profiles = ModuleType('hermes_cli.profiles')
    profiles.get_active_profile_name = lambda: state['profile']
    for name,module in {'gateway':gateway, 'gateway.platforms.event':event_module,
        'gateway.response_filters':filters, 'gateway.session_display':display,
        'hermes_constants':constants, 'hermes_cli.profiles':profiles}.items():
        monkeypatch.setitem(sys.modules, name, module)
    path = Path(__file__).resolve().parents[1] / 'integrations/hermes/host/input_origin.py'
    name = 'gateway.eimemory_input_origin'
    spec = spec_from_file_location(name, path)
    native = module_from_spec(spec)
    monkeypatch.setitem(sys.modules, name, native)
    spec.loader.exec_module(native)
    scope = resolve_channel_scope('hermes', provider._scope)
    provider._host_binding = dict(native=True, profile_name='default', hermes_home=provider._hermes_home, scope=provider._scope)
    expected = dict(profile_name='default', hermes_home=provider._hermes_home,
        binding_kind='business', natural_sample_eligible=True, topology_digest='test')
    def binding(*, channel, scope: dict, source_ids, claim=None):
        assert channel == 'hermes' and resolve_channel_scope('hermes', scope) == resolve_channel_scope('hermes', provider._scope)
        assert source_ids == ['hermes']
        return expected
    from eimemory.evaluation import hermes_profile_coverage
    monkeypatch.setattr(hermes_profile_coverage, 'request_sampling_binding', binding)
    # Isolate the origin worker. The real RPC bridge still validates every
    # queued origin; explicit flushes exercise retry/restart deterministically.
    monkeypatch.setattr(OriginObserver, 'request_flush', lambda self: self._load())

    @contextmanager
    def origin(message, *, internal=False, admitted=True, message_id='platform-message', chat='chat-a', profile='default', actor=None, platform='feishu'):
        source = SimpleNamespace(platform=platform, profile=profile, chat_id=chat,
            user_id=actor if actor is not None else provider._scope['user_id'], is_bot=False)
        event = MessageEvent(source=source, internal=internal, message_id=message_id)
        event._gateway_accepted = admitted
        with native.event_origin(event, session_id=provider._session_id), native.input_origin(message):
            yield
    yield runtime, provider, host, native, origin, state


def run(provider, query, *, turn='origin-host-turn', post_turn=None, **kwargs):
    provider.on_pre_llm_call(user_message=query, session_id=provider._session_id, turn_id=turn, **kwargs)
    provider.prefetch(query, session_id=provider._session_id)
    pending = dict(next(iter(provider._pending_proactive.values())))
    assert pending.get('decision_id')
    provider.on_post_llm_call(user_message=query, assistant_message='Unverified final callback',
        session_id=provider._session_id, turn_id=turn if post_turn is None else post_turn)
    observer = getattr(provider, '_input_origins', None)
    if observer:
        observer.flush()
    result = provider._origin_test_runtime.store.read_consistent(
        lambda sqlite: read_input_origin(sqlite, sqlite.load_proactive_decision(pending['decision_id'])))
    return result, pending


def test_real_callback_attests_external_platform_without_claiming_task_success(native_origin):
    runtime, provider, _, _, origin, _ = native_origin
    query = 'Actual input whose callback and admission must match'
    with origin(query):
        result, pending = run(provider, query)
    assert result['origin_kind'] == EXTERNAL and result['natural_learning_eligible']
    decision = runtime.store.load_proactive_decision(pending['decision_id'])
    assert not decision['outcome_verified'] and decision['outcome_success'] is None


def test_external_input_pasting_internal_marker_remains_external(native_origin):
    _, provider, _, _, origin, _ = native_origin
    query = '[INTERNAL NOTIFICATION] These are literal words an external user pasted'
    with origin(query):
        result, _ = run(provider, query)
    assert result['origin_kind'] == EXTERNAL


def test_internal_event_without_marker_never_receives_external_credit(native_origin):
    _, provider, _, _, origin, _ = native_origin
    query = 'Ordinary looking text with no special body marker'
    with origin(query, internal=True):
        result, _ = run(provider, query)
    assert result['origin_kind'] == INTERNAL and not result['natural_learning_eligible']


@pytest.mark.parametrize('mode', ['no_receipt','unadmitted','no_message_id','no_chat','wrong_profile','wrong_actor','changed_query','missing_post_turn'])
def test_missing_or_foreign_native_identity_stays_unknown(native_origin, mode):
    _, provider, _, _, origin, _ = native_origin
    query = 'A private query requiring an exact admitted native identity'
    changes = dict(admitted=mode!='unadmitted', message_id='' if mode=='no_message_id' else 'platform-message',
        chat='' if mode=='no_chat' else 'chat-a', profile='other' if mode=='wrong_profile' else 'default',
        actor='other' if mode=='wrong_actor' else None)
    if mode == 'no_receipt':
        result, _ = run(provider, query, input_origin={'origin_kind':EXTERNAL})
    else:
        with origin(query + ' changed' if mode == 'changed_query' else query, **changes):
            result, _ = run(provider, query, post_turn='' if mode=='missing_post_turn' else None)
    assert result['origin_kind'] == UNKNOWN and not result['natural_learning_eligible']


def test_profile_context_and_receipt_end_with_the_admitted_input(native_origin):
    _, provider, _, native, origin, _ = native_origin
    query = 'An input whose capability must not escape its own execution'
    with origin(query):
        assert native.snapshot(session_id=provider._session_id, message=query)
        result, _ = run(provider, query)
    assert result['verified']
    assert native.snapshot(session_id=provider._session_id, message=query) is None
    result, _ = run(provider, 'Next turn without native receipt', turn='next-turn')
    assert not result['verified']


def test_origin_retry_has_its_own_schema_and_does_not_poison_legacy_effect_retry(native_origin):
    _, provider, host, _, origin, _ = native_origin
    query = 'A callback while the trusted origin endpoint is temporarily unavailable'
    host.unavailable = True
    with origin(query):
        result, _ = run(provider, query)
    assert not result['verified']
    observer = provider._input_origins
    assert observer.pending
    legacy = provider._effects.path
    before = legacy.read_bytes()
    payload = json.loads(observer.path.read_text())
    assert payload['schema'] == 'hermes.input_origin_outbox.v1'
    assert len(observer.path.read_bytes()) <= MAX_BYTES
    old = EffectObserver(provider)
    old._load()
    assert old.ledger_error == ''
    assert all(item['phase'] in {'turn_completed','next_user','explicit_rating'} for item in old.pending.values())
    restarted = OriginObserver(provider)
    restarted._load()
    assert len(restarted.pending) == len(observer.pending)
    host.unavailable = False
    restarted.flush()
    assert restarted.sent == 1 and not restarted.pending
    assert legacy.read_bytes() == before


def test_unknown_api_producer_is_not_external_even_when_non_internal(native_origin):
    _, provider, _, _, origin, _ = native_origin
    query = 'An API input without a verified external platform author'
    with origin(query, platform='api_server'):
        result, _ = run(provider, query)
    assert result['origin_kind'] == UNKNOWN


def test_default_disabled_keeps_old_callbacks_and_creates_no_origin_outbox(native_origin, monkeypatch):
    _, provider, _, _, origin, _ = native_origin
    monkeypatch.delenv('EIMEMORY_CAPTURE_INPUT_ORIGIN')
    query = 'A legacy callback with the optional capability disabled'
    with origin(query):
        result, _ = run(provider, query)
    assert result['origin_kind'] == UNKNOWN and not hasattr(provider, '_input_origins')
    assert list((Path(provider._hermes_home)/'logs').glob('eimemory-input-origin-v1-*')) == []


@pytest.mark.parametrize('mode', ['expired', 'different_admitted_message'])
def test_stale_or_replaced_native_input_cannot_reuse_pending_identity(native_origin, mode):
    import time
    _, provider, _, _, origin, _ = native_origin
    query, turn = 'One pending decision attached to exactly one native input', 'pending-origin-turn'
    with origin(query, message_id='first-message'):
        provider.on_pre_llm_call(user_message=query, session_id=provider._session_id, turn_id=turn)
        provider.prefetch(query, session_id=provider._session_id)
        pending = dict(next(iter(provider._pending_proactive.values())))
        if mode == 'expired':
            provider._input_origins.turns[(provider._session_id,turn)]['started'] = time.monotonic()-MAX_TURN_SECONDS-1
            provider.on_post_llm_call(user_message=query, assistant_message='Late answer', session_id=provider._session_id, turn_id=turn)
    if mode == 'different_admitted_message':
        with origin(query, message_id='second-message'):
            provider.on_post_llm_call(user_message=query, assistant_message='Another input callback', session_id=provider._session_id, turn_id=turn)
    provider._input_origins.flush()
    result = provider._origin_test_runtime.store.read_consistent(
        lambda sqlite: read_input_origin(sqlite, sqlite.load_proactive_decision(pending['decision_id'])))
    assert not result['verified']


def test_origin_capacity_and_corrupt_origin_schema_are_isolated_from_legacy_queue(native_origin):
    from copy import deepcopy
    _, provider, _, _, origin, _ = native_origin
    query = 'An isolated bounded origin queue'
    with origin(query):
        result, pending = run(provider, query)
    assert result['verified']
    runtime = provider._origin_test_runtime
    body = json.loads(runtime.store.sqlite.conn.execute(
        'SELECT payload_json FROM proactive_input_origins_v1 WHERE decision_id=?', (pending['decision_id'],)).fetchone()[0])
    params = {**body['namespace'], 'origin':body['claim']}
    observer = OriginObserver(provider)
    for index in range(MAX_ITEMS):
        assert observer.enqueue({**params, 'decision_id':f'bounded-{index}'})
    assert not observer.enqueue({**params, 'decision_id':'too-many'})
    assert len(observer.pending) == MAX_ITEMS and observer.path.stat().st_size <= MAX_BYTES
    original_legacy = provider._effects.path.read_bytes()
    corrupt = dict(schema='hermes.input_origin_outbox.v999', items=[])
    observer.path.write_text(json.dumps(corrupt))
    restarted = OriginObserver(provider)
    restarted._load()
    assert restarted.error == 'input_origin_queue_unreadable'
    assert not restarted.enqueue(params)
    legacy = EffectObserver(provider)
    legacy._load()
    assert legacy.ledger_error == '' and provider._effects.path.read_bytes() == original_legacy


def test_copied_context_cannot_replay_a_closed_native_input_lease(native_origin):
    from contextvars import copy_context
    from concurrent.futures import ThreadPoolExecutor
    _, provider, _, native, origin, _ = native_origin
    query = 'A native capability must expire even in copied contexts'
    with origin(query):
        stale = copy_context()
        other_thread = copy_context()
        with ThreadPoolExecutor(max_workers=1) as pool:
            assert pool.submit(other_thread.run, native.snapshot,
                session_id=provider._session_id, message=query).result() is None
        assert native.snapshot(session_id=provider._session_id, message=query)
    assert stale.run(native.snapshot, session_id=provider._session_id, message=query) is None


def queued_params(native_origin):
    _, provider, _, _, origin, _ = native_origin
    query = 'Durable origin ownership uses the exact signed current disk state'
    with origin(query):
        result, pending = run(provider, query)
    assert result['verified']
    body = json.loads(provider._origin_test_runtime.store.sqlite.conn.execute(
        'SELECT payload_json FROM proactive_input_origins_v1 WHERE decision_id=?', (pending['decision_id'],)).fetchone()[0])
    return {**body['namespace'], 'origin':body['claim']}


def test_multiple_instances_and_concurrent_enqueues_preserve_signed_disk_entries(native_origin):
    from concurrent.futures import ThreadPoolExecutor
    _, provider, _, _, _, _ = native_origin
    params = queued_params(native_origin)
    observers = [OriginObserver(provider) for _ in range(8)]
    for observer in observers:
        observer._load()
    assert observers[0].enqueue({**params, 'decision_id':'queued-0'})
    assert observers[1].enqueue({**params, 'decision_id':'queued-1'})
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(observers[i].enqueue, {**params,'decision_id':f'queued-{i}'}) for i in range(2,8)]
        assert all(future.result() for future in futures)
    restarted = OriginObserver(provider)
    restarted._load()
    assert set(restarted.pending) == {f'queued-{i}' for i in range(8)}


def test_inflight_sender_ack_preserves_another_instances_new_entry(native_origin, monkeypatch):
    _, provider, host, _, _, _ = native_origin
    params = queued_params(native_origin)
    first, second = {**params,'decision_id':'inflight-first'}, {**params,'decision_id':'concurrent-second'}
    sender, writer = OriginObserver(provider), OriginObserver(provider)
    sender._load(); writer._load()
    assert sender.enqueue(first)
    original = host.call_or_bypass
    def response(method, arguments):
        if method == 'adapter.attest_input_origin' and arguments['decision_id'] == first['decision_id']:
            assert writer.enqueue(second)
            return {'ok':True,'result':{'ok':True}}
        return original(method, arguments)
    monkeypatch.setattr(host,'call_or_bypass',response)
    sender.flush()
    restarted = OriginObserver(provider)
    restarted._load()
    assert set(restarted.pending) == {second['decision_id']}
    assert sender.enqueue({**params,'decision_id':'after-ack'})
    restarted._load()
    assert set(restarted.pending) == {second['decision_id'],'after-ack'}


def test_late_ack_cannot_remove_a_later_payload_with_the_same_key(native_origin):
    from copy import deepcopy
    _, provider, _, _, _, _ = native_origin
    original = {**queued_params(native_origin), 'decision_id':'reused-decision-key'}
    sender, writer = OriginObserver(provider), OriginObserver(provider)
    assert sender.enqueue(original)
    assert sender._ack(original['decision_id'],original)
    replacement = deepcopy(original)
    replacement['origin']['native_receipt_digest'] = 'a'*64
    assert writer.enqueue(replacement)
    assert sender._ack(original['decision_id'],original) is False
    restarted = OriginObserver(provider)
    restarted._load()
    assert restarted.pending[original['decision_id']] == replacement


def test_missing_queue_key_and_bad_mac_cannot_authorize_or_overwrite_retry(native_origin, monkeypatch):
    _, provider, _, _, _, _ = native_origin
    params = {**queued_params(native_origin), 'decision_id':'private-queue-key-check'}
    observer = OriginObserver(provider)
    before = observer.path.read_bytes()
    with monkeypatch.context() as patch:
        patch.setattr('eimemory.adapters.hermes.input_origin.hermes_producer_token',lambda:'')
        assert observer.enqueue(params) is False
        assert observer.path.read_bytes() == before
    assert observer.enqueue(params)
    value = json.loads(observer.path.read_text())
    value['items'][0]['origin']['host_turn_digest'] = 'b'*64
    observer.path.write_text(json.dumps(value))
    tampered = observer.path.read_bytes()
    restarted = OriginObserver(provider)
    restarted._load()
    assert restarted.error == 'input_origin_queue_unreadable'
    assert restarted.enqueue({**params,'decision_id':'cannot-overwrite-invalid-file'}) is False
    assert observer.path.read_bytes() == tampered
    legacy = EffectObserver(provider)
    legacy._load()
    assert not legacy.ledger_error
