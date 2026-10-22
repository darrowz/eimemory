"""One native gateway must preserve request ownership across profiles/workers."""
from contextlib import contextmanager
from contextvars import ContextVar
from hashlib import sha256
import json
import os
from pathlib import Path
import sys
import threading
from types import ModuleType, SimpleNamespace

import pytest

from eimemory.adapters.hermes import profile_context as contexts
from eimemory.adapters.hermes.provider_core import HermesMemoryProviderCore
from eimemory.contracts import hermes_topology as topology_contract


pytestmark = pytest.mark.skipif(os.name != 'posix', reason='Native Hermes POSIX routing and credential boundary')


@pytest.fixture
def native_profiles(tmp_path, monkeypatch, trusted_dataset_path_ancestors):
    home_var = ContextVar('native_test_home')
    profile_var = ContextVar('native_test_profile')
    secrets_var = ContextVar('native_test_secrets', default=None)
    secret_home_var = ContextVar('native_test_secret_home', default=None)
    user_var = ContextVar('native_test_user', default='')
    root = tmp_path / 'native-hermes'
    root.mkdir(mode=0o775)
    homes = {'default': root, **{name: root / 'profiles' / name
                                for name in ('teammate-a', 'teammate-b', 'unbound')}}
    for path in homes.values():
        path.mkdir(parents=True, exist_ok=True, mode=0o775)
    profiles = []
    for name, home in homes.items():
        scope = dict(tenant_id='tenant', agent_id='primary-canonical' if name == 'default' else name,
                     workspace_id='office', user_id='platform-' + name)
        profiles.append(dict(profile_name=name, hermes_home=str(home), agent_id=scope['agent_id'],
            identity_status='unbound' if name == 'unbound' else 'bound',
            operator_scope={**scope,'user_id':'local-operator'},
            capture_scopes=[] if name == 'unbound' else [dict(scope=scope, channel='hermes', source_ids=['hermes'])]))
    body = dict(schema=topology_contract.SCHEMA, mode='single_native_gateway',
        runtime_uid=os.geteuid(), runtime_gid=os.getegid(), owner_unit=topology_contract.MAIN_GATEWAY,
        runtime_units=list(topology_contract.RUNTIME_UNITS), profiles=profiles, retired_gateways=[])
    topology = dict(body, topology_digest=topology_contract.topology_digest(body))
    monkeypatch.setattr(topology_contract, 'load_topology', lambda: topology)
    monkeypatch.setattr(topology_contract, 'load_verified_topology', lambda: topology)
    monkeypatch.setenv('EIMEMORY_RPC_ENV_FILE', str(tmp_path / 'shared-rpc.env'))

    def module(name, **attributes):
        value = ModuleType(name)
        value.__dict__.update(attributes)
        monkeypatch.setitem(sys.modules, name, value)
        return value

    module('hermes_constants', get_hermes_home=lambda: home_var.get(), get_default_hermes_root=lambda: root)
    module('hermes_cli')
    module('hermes_cli.profiles', get_active_profile_name=lambda: profile_var.get())
    module('agent')

    def get_secret(name, default=None):
        values = secrets_var.get()
        if values is None:
            raise RuntimeError('native_secret_scope_required')
        return values.get(name, default)

    module('agent.secret_scope', get_secret=get_secret,
           current_secret_scope_home=lambda: secret_home_var.get())
    module('gateway')
    module('gateway.session_context', get_session_env=lambda name, default='':
           user_var.get() if name == 'HERMES_SESSION_USER_ID' else default)
    module('gateway.session_identity', identity_of=lambda source: getattr(source, 'routing_identity', None))

    @contextmanager
    def activate(name, *, user=None, settings=None, secret_home=None):
        values = [(home_var, homes[name]), (profile_var, name),
                  (secrets_var, dict(settings or {})), (secret_home_var, secret_home or homes[name]),
                  (user_var, 'platform-' + name if user is None else user)]
        tokens = [(var, var.set(value)) for var, value in values]
        try:
            yield
        finally:
            for var, token in reversed(tokens):
                var.reset(token)

    yield SimpleNamespace(activate=activate, homes=homes, topology=topology,
                          secrets=secrets_var, home=home_var, user=user_var)


class Client:
    def __init__(self):
        self.calls = []

    def call_or_bypass(self, method, params):
        self.calls.append((method, params, contexts.current_home()))
        if method == 'adapter.proactive_prefetch':
            return {'ok': True, 'result': {'decision_id': '', 'context': '', 'bypassed': True}}
        return {'ok': True, 'result': {'ok': True, 'context': 'useful prior memory'}}


def initialize(native, name, *, session='shared-session', client=None, **kwargs):
    provider = HermesMemoryProviderCore(client=client or Client())
    provider.initialize(session, hermes_home=str(native.homes[name]), agent_identity=name,
                        agent_workspace='hermes', user_id='platform-' + name, **kwargs)
    return provider


@pytest.mark.parametrize('name', ['default', 'teammate-a', 'teammate-b'])
def test_native_topology_wins_over_process_and_profile_generic_identity(native_profiles, monkeypatch, name):
    native = native_profiles
    monkeypatch.setenv('EIMEMORY_AGENT_ID', 'wrong-process-agent')
    monkeypatch.setenv('EIMEMORY_WORKSPACE_ID', 'wrong-process-workspace')
    monkeypatch.setenv('EIMEMORY_USER_ID', 'wrong-process-user')
    monkeypatch.setenv('EIMEMORY_SOURCE_IDS', 'wrong-process-source')
    with native.activate(name, settings={'EIMEMORY_USER_ID': 'wrong-profile-generic-user'}):
        provider = initialize(native, name)
        try:
            scope = provider._scope
            assert scope['agent_id'] == ('primary-canonical' if name == 'default' else name)
            assert scope['user_id'] == 'platform-' + name
            assert scope['workspace_id'] == 'office'
            result = json.loads(provider.handle_tool_call('eimemory_recall',
                {'query': 'prior work', 'scope': {'agent_id': 'forged'}, 'user_id': 'forged'}))
            assert result['ok'] is True
            params = [p for method, p, _home in provider._client.calls if method == 'adapter.prefetch'][-1]
            assert params['scope'] == scope
            assert params['task_context']['source_ids'] == ['hermes']
            assert provider._outbox.namespace['scope'] == scope
        finally:
            provider.shutdown()


def test_unknown_user_unbound_profile_and_mismatched_native_home_fail_before_rpc(native_profiles):
    native = native_profiles
    for name, user, reason in [('unbound', 'platform-unbound', 'identity_unbound'),
                               ('teammate-a', 'other-user', 'scope_unbound')]:
        with native.activate(name, user=user):
            client = Client()
            provider = HermesMemoryProviderCore(client=client)
            with pytest.raises(ValueError, match=reason):
                provider.initialize('s', hermes_home=str(native.homes[name]), user_id=user)
            assert client.calls == []
            assert provider._outbox is None
    with native.activate('teammate-a'):
        provider = HermesMemoryProviderCore(client=Client())
        with pytest.raises(ValueError, match='home_mismatch'):
            provider.initialize('s', hermes_home=str(native.homes['default']), user_id='platform-teammate-a')


def test_named_profile_never_uses_unregistered_legacy_or_corrupt_topology(native_profiles, monkeypatch):
    monkeypatch.setattr(topology_contract, 'load_topology', lambda: None)
    with native_profiles.activate('teammate-a'):
        with pytest.raises(ValueError, match='identity_unbound'):
            initialize(native_profiles, 'teammate-a')
    def corrupt():
        raise ValueError('hermes_topology_registration_invalid')
    monkeypatch.setattr(topology_contract, 'load_topology', corrupt)
    with native_profiles.activate('default'):
        with pytest.raises(ValueError, match='registration_invalid'):
            initialize(native_profiles, 'default')


@pytest.mark.parametrize('platform', ['cli','cron','desktop','oneshot','subagent','flush'])
@pytest.mark.parametrize('name', ['default','teammate-a','unbound'])
def test_native_local_operator_memory_is_separate_from_business_samples(native_profiles, platform, name):
    native=native_profiles
    with native.activate(name,user=''):
        provider=HermesMemoryProviderCore(client=Client())
        provider.initialize('local',hermes_home=str(native.homes[name]),platform=platform)
        try:
            assert provider._scope['user_id']=='local-operator'
            assert provider._host_binding['binding_kind']=='operator'
            assert provider._host_binding['natural_sample_eligible'] is False
            assert provider._common_params()['runtime_profile_binding']['natural_sample_eligible'] is False
        finally:provider.shutdown()


@pytest.mark.parametrize('platform', ['gateway','telegram','feishu','', 'forged-local'])
def test_external_missing_user_never_downgrades_to_local_operator(native_profiles, platform):
    with native_profiles.activate('teammate-a',user=''):
        provider=HermesMemoryProviderCore(client=Client())
        with pytest.raises(ValueError,match='identity_unbound'):
            provider.initialize('external',hermes_home=str(native_profiles.homes['teammate-a']),platform=platform)
        assert provider._outbox is None


def test_local_operator_requires_real_os_owner_and_explicit_operator_user_replay_is_marked(native_profiles,monkeypatch):
    with native_profiles.activate('teammate-a',user=''):
        monkeypatch.setattr(os,'getuid',lambda: native_profiles.topology['runtime_uid']+1)
        with pytest.raises(ValueError,match='identity_unbound'):
            HermesMemoryProviderCore(client=Client()).initialize('bad-owner',platform='cli')
    with native_profiles.activate('teammate-a',user='local-operator'):
        provider=HermesMemoryProviderCore(client=Client())
        provider.initialize('replay',user_id='local-operator',platform='gateway')
        try:assert provider._host_binding['binding_kind']=='operator'
        finally:provider.shutdown()


def test_native_secret_home_mismatch_and_absent_scope_do_not_borrow_process_secrets(native_profiles, monkeypatch):
    monkeypatch.setenv('EIMEMORY_RPC_TOKEN', 'wrong-process-token')
    with native_profiles.activate('teammate-a', secret_home=native_profiles.homes['teammate-b']):
        with pytest.raises(ValueError, match='secret_home_mismatch'):
            contexts.native_context()
    with native_profiles.activate('teammate-a'):
        token = native_profiles.secrets.set(None)
        try:
            with pytest.raises(RuntimeError, match='secret_scope_required'):
                contexts.rpc_configuration()
        finally:
            native_profiles.secrets.reset(token)


def test_rpc_auth_alias_shared_file_and_profile_credentials_are_separate(native_profiles, monkeypatch, tmp_path):
    shared = tmp_path / 'shared-rpc.env'
    shared.write_text('EIMEMORY_RPC_AUTH_TOKEN=shared-test-token\n', encoding='utf-8')
    shared.chmod(0o640)
    before = dict(os.environ)
    with native_profiles.activate('teammate-a'):
        config = contexts.rpc_configuration()
        assert config['auth_token'] == 'shared-test-token'
    for name in ('teammate-a', 'teammate-b'):
        with native_profiles.activate(name, settings={'EIMEMORY_RPC_AUTH_TOKEN': name + '-token'}):
            assert contexts.rpc_configuration()['auth_token'] == name + '-token'
        with native_profiles.activate(name, settings={'EIMEMORY_RPC_TOKEN': name + '-token'}):
            assert contexts.rpc_configuration()['auth_token'] == name + '-token'
    with native_profiles.activate('teammate-a', settings={'EIMEMORY_RPC_URL': 'https://other.example/rpc'}):
        assert contexts.rpc_configuration()['auth_token'] == ''
    assert dict(os.environ) == before


@pytest.mark.parametrize('fault', ['mode', 'symlink', 'hardlink', 'duplicate', 'oversized'])
def test_shared_rpc_file_keeps_strict_trust_boundary(native_profiles, tmp_path, fault):
    path = tmp_path / 'shared-rpc.env'
    path.write_text('EIMEMORY_RPC_AUTH_TOKEN=test\n', encoding='utf-8')
    path.chmod(0o640)
    if fault == 'mode':
        path.chmod(0o666)
    elif fault == 'symlink':
        target = tmp_path / 'original'
        path.rename(target)
        path.symlink_to(target)
    elif fault == 'hardlink':
        os.link(path, tmp_path / 'alias')
    elif fault == 'duplicate':
        path.write_text('EIMEMORY_RPC_AUTH_TOKEN=first\nEIMEMORY_RPC_AUTH_TOKEN=second\n')
    else:
        path.write_bytes(b'x' * 65537)
    with native_profiles.activate('teammate-a'):
        with pytest.raises((ValueError, OSError)):
            contexts.rpc_configuration()


def test_producer_credentials_follow_profile_without_mutating_gateway_env(native_profiles, tmp_path, monkeypatch):
    from eimemory.adapters.hermes import host_context
    monkeypatch.setattr(host_context, '_PRODUCER_TOKENS', {})
    monkeypatch.setattr(host_context, '_PRODUCER_TOKEN', '')
    before = dict(os.environ)
    for name in ('teammate-a', 'teammate-b'):
        token = sha256(name.encode()).hexdigest()
        path = tmp_path / (name + '-producer')
        path.write_text(token)
        path.chmod(0o600)
        settings = {'EIMEMORY_ATTESTATION_HOST_PROFILE': 'operator-separated-v1',
                    'EIMEMORY_HERMES_ATTESTATION_TOKEN_FILE': str(path)}
        with native_profiles.activate(name, settings=settings):
            assert host_context.hermes_producer_token() == token
        with native_profiles.activate(name):
            assert host_context.hermes_producer_token() == ''
    assert dict(os.environ) == before


def test_same_session_in_two_profiles_isolated_and_worker_context_is_native(native_profiles):
    from eimemory.adapters.hermes.provider_registry import bind_hermes_provider, get_hermes_provider, unbind_hermes_provider
    native = native_profiles
    providers = []
    try:
        for name in ('teammate-a', 'teammate-b'):
            with native.activate(name):
                provider = initialize(native, name)
                providers.append(provider)
                bind_hermes_provider(session_id='shared-session', provider=provider)
        for name, provider in zip(('teammate-a', 'teammate-b'), providers):
            with native.activate(name):
                assert get_hermes_provider('shared-session') is provider
                worker = contexts.spawn_owned_thread(target=lambda p=provider: p.prefetch('prior work'))
                worker.start()
                worker.join(timeout=5)
                assert not worker.is_alive()
                calls = provider._client.calls
                assert calls and {home for _method, _params, home in calls} == {str(native.homes[name])}
        with native.activate('default'):
            assert get_hermes_provider('shared-session') is None
        with native.activate('teammate-b'):
            before = len(providers[0]._client.calls)
            response = providers[0]._safe_call('adapter.status', {})
            assert response['ok'] is False
            assert len(providers[0]._client.calls) == before
    finally:
        for name, provider in zip(('teammate-a', 'teammate-b'), providers):
            with native.activate(name):
                unbind_hermes_provider(provider)
                provider.shutdown()


def test_session_registry_reservation_rejects_conflict_without_mutation(native_profiles):
    from eimemory.adapters.hermes import provider_registry as registry
    with native_profiles.activate('teammate-a'):
        first = initialize(native_profiles, 'teammate-a', session='first')
        second = initialize(native_profiles, 'teammate-a', session='second')
        registry.bind_hermes_provider(session_id='first', provider=first)
        registry.bind_hermes_provider(session_id='second', provider=second)
        try:
            with pytest.raises(ValueError, match='already_bound'):
                with registry.moving_hermes_provider(old_session_id='first', new_session_id='second', provider=first):
                    first.on_session_switch('second')
            assert first._session_id == 'first'
            assert registry.get_hermes_provider('first') is first
            assert registry.get_hermes_provider('second') is second
            with pytest.raises(RuntimeError, match='cancelled'):
                with registry.moving_hermes_provider(old_session_id='first', new_session_id='third', provider=first):
                    raise RuntimeError('cancelled')
            assert registry.get_hermes_provider('third') is None
            assert registry.get_hermes_provider('first') is first
            with pytest.raises(ValueError, match='owner_change'):
                first.on_session_switch('third', hermes_home=str(native_profiles.homes['teammate-b']))
            assert first._session_id == 'first'
        finally:
            for provider in (first, second):
                registry.unbind_hermes_provider(provider)
                provider.shutdown()


def test_each_worker_takes_an_independent_copy_of_native_context(native_profiles):
    barrier = threading.Barrier(3)
    seen = []
    def work():
        barrier.wait(timeout=5)
        seen.append((contexts.current_home(), contexts.profile_value('profile-test-secret')))
    with native_profiles.activate('teammate-a', settings={'profile-test-secret': 'a'}):
        workers = [contexts.spawn_owned_thread(target=work) for _ in range(2)]
        for worker in workers:
            worker.start()
    with native_profiles.activate('teammate-b', settings={'profile-test-secret': 'b'}):
        barrier.wait(timeout=5)
        for worker in workers:
            worker.join(timeout=5)
    assert seen == [(str(native_profiles.homes['teammate-a']), 'a')] * 2


def test_provider_and_effect_worker_entrypoints_preserve_request_context(native_profiles,monkeypatch):
    seen=[]
    release=threading.Event()
    def record(kind):
        release.wait(timeout=5)
        seen.append((kind,contexts.current_home(),contexts.profile_value('request-marker')))
    with native_profiles.activate('teammate-a',settings={'request-marker':'owned-request'}):
        provider=initialize(native_profiles,'teammate-a')
        if provider._write_thread:provider._write_thread.join(timeout=5)
        monkeypatch.setattr(provider._outbox,'entries',lambda kind:['write-fixture'] if kind=='write' else [])
        monkeypatch.setattr(provider,'_write_worker',lambda *args:record('write'))
        provider._start_write_worker()
        monkeypatch.setattr(provider,'_prefetch_worker',lambda *args:record('prefetch'))
        provider.queue_prefetch('prior work')
        provider._receipt_handoff=SimpleNamespace(list_ids=lambda **kwargs:['receipt'])
        provider._verified_host_turns[('shared-session','turn')]='verified'
        monkeypatch.setattr(provider,'_close_host_turn',lambda **kwargs:record('task-end'))
        assert provider._auto_close_completed_host_turn(session_id='shared-session',turn_id='turn')
        observer=provider._effects
        monkeypatch.setattr(observer,'_load',lambda:None)
        monkeypatch.setattr(observer,'_client',lambda:object())
        observer.pending['signal']={}
        monkeypatch.setattr(observer,'flush',lambda:record('effect'))
        observer.request_flush()
    with native_profiles.activate('teammate-b',settings={'request-marker':'foreign-request'}):
        release.set()
        for worker in (provider._write_thread,provider._prefetch_thread,provider._auto_task_end_thread,observer.worker):
            worker.join(timeout=5)
            assert not worker.is_alive()
    assert {kind for kind,home,marker in seen}=={'write','prefetch','task-end','effect'}
    assert {(home,marker) for kind,home,marker in seen}=={(str(native_profiles.homes['teammate-a']),'owned-request')}
    # The fake effect queue is not a real delivery and must not be persisted.
    observer.pending.clear()
    with native_profiles.activate('teammate-a'):provider.shutdown()


def test_rpc_refresh_is_per_call_and_never_mutates_shared_client(native_profiles,monkeypatch):
    from eimemory.adapters.runtime.http_client import AgentRuntimeRPCClient
    calls=[]
    def call(client,method,params):
        calls.append((client.auth_token,contexts.current_home()))
        return {'ok':True,'result':{'ok':True}}
    monkeypatch.setattr(AgentRuntimeRPCClient,'call_or_bypass',call)
    with native_profiles.activate('teammate-a',settings={'EIMEMORY_RPC_TOKEN':'initial-profile-token'}):
        provider=HermesMemoryProviderCore()
        provider.initialize('rpc',hermes_home=str(native_profiles.homes['teammate-a']),user_id='platform-teammate-a')
        original=provider._client
    with native_profiles.activate('teammate-a',settings={'EIMEMORY_RPC_TOKEN':'rotated-profile-token'}):
        assert provider._safe_call('adapter.status',provider._common_params())['ok']
        provider.shutdown()
    assert calls[-1]==('rotated-profile-token',str(native_profiles.homes['teammate-a']))
    assert original.auth_token=='initial-profile-token'


def test_hook_attestation_is_resolved_for_each_native_profile_not_register_context(native_profiles,monkeypatch):
    from integrations.hermes import eimemory_hook as hook
    from eimemory.adapters.hermes import provider_registry as registry
    hooks={}
    calls=[]
    def producer(**kwargs):
        owner=contexts.current_home()
        class Attestation:
            def call_or_bypass(self,method,params):
                calls.append((owner,params['scope']['agent_id']))
                return {'ok':True,'result':{'receipt_id':'native-receipt','receipt':{'passed':True}}}
        return Attestation()
    monkeypatch.setattr(hook,'hermes_attestation_client_from_env',producer)
    monkeypatch.setattr(hook,'register_code_implementation_task',lambda ctx:None)
    with native_profiles.activate('default'):
        hook.register(SimpleNamespace(register_hook=lambda name,callback:hooks.update({name:callback})))
    providers=[]
    try:
        for name in ('teammate-a','teammate-b'):
            with native_profiles.activate(name):
                provider=initialize(native_profiles,name)
                providers.append(provider)
                registry.bind_hermes_provider(session_id='shared-session',provider=provider)
                hooks['post_tool_call']('terminal',{'command':'true'},{'exit_code':0},'task',1,
                    session_id='shared-session',turn_id='turn',tool_call_id='call')
                assert provider._verified_host_turns[('shared-session','turn')]=='verified'
        assert calls==[(str(native_profiles.homes[name]),name) for name in ('teammate-a','teammate-b')]
    finally:
        for name,provider in zip(('teammate-a','teammate-b'),providers):
            with native_profiles.activate(name):
                registry.unbind_hermes_provider(provider)
                provider.shutdown()


def test_named_profiles_do_not_register_the_single_main_code_provider(native_profiles,monkeypatch):
    from integrations.hermes import eimemory_hook as hook
    created=[]
    class Server:
        socket_path=Path('/not-opened-test-socket')
        def __init__(self,ctx):created.append(ctx)
        def start(self):return True
    monkeypatch.setattr(hook,'CodeImplementationSocketServer',Server)
    for name in ('teammate-a','teammate-b'):
        with native_profiles.activate(name):
            assert hook.register_code_implementation_task(SimpleNamespace(register_auxiliary_task=lambda *a,**k:None)) is None
    assert created==[]
    with native_profiles.activate('default'):
        result=hook.register_code_implementation_task(SimpleNamespace(register_auxiliary_task=lambda *a,**k:None))
        assert result['socket']=='/not-opened-test-socket'
    assert len(created)==1


def test_delivery_callback_uses_pinned_home_after_context_and_source_change(native_profiles, tmp_path):
    from eimemory.adapters.hermes.channel_delivery import register_external_delivery_capture
    from test_hermes_channel_delivery import _Adapter, _Gateway, _Event, _Source, _Platform, _create_ledger, _insert_obligation
    native = native_profiles
    for home in native.homes.values():
        _create_ledger(home / 'state.db')
    source = _Source(platform=_Platform('telegram'), user_id='platform-teammate-a')
    source.routing_identity = SimpleNamespace(runtime_home=native.homes['teammate-a'], runtime_profile='teammate-a')
    adapter = _Adapter()
    gateway = _Gateway(adapter, authorized=False)
    gateway._is_user_authorized_for_source = lambda current: current is source
    event = _Event(source=source)
    with native.activate('default'):
        register_external_delivery_capture(event=event, gateway=gateway, runtime_commit='a' * 40,
            state_path=tmp_path / 'state.json', signal_path=tmp_path / 'signal.json')
    obligation = _insert_obligation(native.homes['teammate-a'] / 'state.db')
    source.message_id = 'mutated-event-must-not-change-bound-references'
    source.routing_identity = SimpleNamespace(runtime_home=native.homes['teammate-b'], runtime_profile='teammate-b')
    with native.activate('teammate-b'):
        next(iter(adapter.callbacks.values()))()
    entry = next(iter(json.loads((tmp_path / 'state.json').read_text())['entries'].values()))
    assert entry['delivery_receipt_id'] == obligation
    assert entry['runtime_owner']['hermes_home'] == str(native.homes['teammate-a'])
    assert entry['runtime_owner']['scope']['agent_id'] == 'teammate-a'
    assert entry['runtime_owner']['identity_status'] == 'bound'
    assert entry['runtime_owner']['source_ids'] == ['hermes']


def test_explicit_delivery_home_cannot_override_native_routing(native_profiles):
    source = SimpleNamespace(routing_identity=SimpleNamespace(runtime_home=native_profiles.homes['teammate-a']))
    with pytest.raises(ValueError, match='delivery_profile_home_mismatch'):
        contexts.source_home(SimpleNamespace(), source, native_profiles.homes['teammate-b'])
