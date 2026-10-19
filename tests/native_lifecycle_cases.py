"""Isolated lifecycle evidence; all clocks/repos/incidents here are test fixtures."""
from copy import deepcopy
from contextlib import closing
from datetime import datetime, timedelta, timezone
import json
import os
from types import SimpleNamespace

import pytest

from eimemory.governance.evolution import code_automation_policy_owner as owner
from eimemory.governance.evolution import code_automation_policy as policy_api
from eimemory.governance.evolution import code_automation_policy_issue as issuer
from eimemory.governance.evolution.code_evolution_observation import (
    native_observation_clock, verified_observation_clock, observation_clock_discontinuity,
    observation_monotonic_complete, OBSERVATION_OFFSETS,
)
from eimemory.governance.promotion import promotion_watch as watch
from eimemory.models.records import ScopeRef
from eimemory.ops import system_code_repair_failure as failure
from eimemory.storage.runtime_store import RuntimeStore
from eimemory.storage.code_evolution_store import CodeEvolutionStore, digest_json
from native_policy_owner_cases import native_policy_fixture, _ensure, _state
from test_code_evolution_recovery import _observing_manager, _sample

BASE = datetime(2026, 1, 1, tzinfo=timezone.utc)
BOOT = '11111111-1111-1111-1111-111111111111'


def _clock(seconds=0, *, boot=BOOT, wall=None, mono=None):
    return {'schema': 'code_observation_clock.v1', 'boot_id': boot,
            'wall_utc': (BASE + timedelta(seconds=seconds if wall is None else wall)).isoformat(),
            'monotonic_ns': int((seconds if mono is None else mono) * 1_000_000_000)}


@pytest.mark.parametrize('bad', ['naive', 'nonutc', 'mono_bool', 'boot', 'schema'])
def test_native_observation_clock_contract_rejects_unknown_time(bad):
    value = _clock()
    if bad == 'naive': value['wall_utc'] = '2026-01-01T00:00:00'
    if bad == 'nonutc': value['wall_utc'] = '2026-01-01T08:00:00+08:00'
    if bad == 'mono_bool': value['monotonic_ns'] = False
    if bad == 'boot': value['boot_id'] = 'unverified'
    if bad == 'schema': value.pop('schema')
    with pytest.raises(ValueError): verified_observation_clock(value)


@pytest.mark.parametrize('new,reason', [
    (_clock(900), ''),
    (_clock(900, wall=-1), 'observation_clock_discontinuity'),
    (_clock(900, wall=3600), 'observation_clock_discontinuity'),
    (_clock(900, mono=0), 'observation_clock_discontinuity'),
    (_clock(900, boot='22222222-2222-2222-2222-222222222222'), 'observation_boot_changed'),
])
def test_native_observation_clock_uses_real_continuity(new, reason):
    assert observation_clock_discontinuity(_clock(), new, new['wall_utc']) == reason
    assert not observation_monotonic_complete(_clock(), new)
    assert observation_monotonic_complete(_clock(), _clock(28800))


def test_native_observation_caller_future_time_never_becomes_owner_clock():
    assert observation_clock_discontinuity(None, _clock(), _clock(28800)['wall_utc']) == 'caller_observation_time_untrusted'
    for value in ('2026-01-01T00:00:00', '2026-01-01T08:00:00+08:00'):
        assert observation_clock_discontinuity(None, _clock(), value) == 'caller_observation_time_untrusted'


@pytest.mark.skipif(os.name != 'posix', reason='native Linux boot clock contract')
def test_native_observation_clock_reads_actual_kernel_boot_and_monotonic():
    before = verified_observation_clock(native_observation_clock())
    after = verified_observation_clock(native_observation_clock())
    assert before['boot_id'] == after['boot_id']
    assert after['monotonic_ns'] >= before['monotonic_ns']
    assert observation_clock_discontinuity(before, after, after['wall_utc']) == ''


def _consumed(store, scope, *, tx='old-tx', base='b'*40, digest='c'*64):
    ledger = CodeEvolutionStore(store)
    ledger.create_transaction({'transaction_id': tx, 'idempotency_key': tx, 'scope': scope.to_dict() if hasattr(scope, 'to_dict') else
        {key: getattr(scope, key) for key in ('tenant_id', 'agent_id', 'workspace_id', 'user_id')},
        'incident': {'incident_id': 'original-' + tx},
        'repository': {'root': '/fixture-repo', 'ref': 'master', 'base_commit': base}})
    material = {'transaction_id': tx, 'policy_digest': digest}
    ledger.consume_policy(transaction_id=tx, policy_digest=digest, authorization_receipt_digest=digest_json(material),
        payload={'authorization_material': material, 'authorized_policy': {'ok': True, 'policy_digest': digest}})
    return ledger


def _observe(store, tx, monkeypatch, seconds, *, boot=BOOT, wall=None, key=None):
    clock = _clock(seconds, boot=boot, wall=wall)
    monkeypatch.setattr(watch, 'native_observation_clock', lambda: deepcopy(clock))
    return watch._record_code_evolution_observation_sample(store, transaction_id=tx, owner_id='watch-test',
        sample=_sample(key or f'tick-{seconds}-{boot}', observed_at=clock['wall_utc'], health_ok=True))


def test_native_observation_missed_phase_restarts_once_then_real_rollback(tmp_path, monkeypatch):
    with closing(RuntimeStore(tmp_path/'runtime')) as store:
        manager = _observing_manager(store, 'missed-phase')
        assert _observe(store, 'missed-phase', monkeypatch, 0)['status'] == 'observing'
        original = manager.store.get_transaction('missed-phase')['payload']['observation_samples']
        restarted = _observe(store, 'missed-phase', monkeypatch, 7200)
        assert restarted['window_restarted'] is True
        tx = manager.store.get_transaction('missed-phase')
        assert tx['payload']['observation_window_history'][0]['samples'] == original
        assert tx['payload']['observation_window_generation'] == 1
        assert datetime.fromisoformat(tx['payload']['observation_effective_deadline']) >= BASE + timedelta(hours=10)
        result = _observe(store, 'missed-phase', monkeypatch, 14400)
        assert result['status'] == 'rollback_required'
        tx = manager.store.get_transaction('missed-phase')
        assert tx['current_state'] == 'ROLLBACK_INTENT'
        assert tx['payload']['observation_valid'] is False
        assert manager.store.get_terminal_receipt('missed-phase') is None


def test_native_observation_reboot_restart_needs_full_new_monotonic_eight_hours(tmp_path, monkeypatch):
    with closing(RuntimeStore(tmp_path/'runtime')) as store:
        manager = _observing_manager(store, 'reboot-window')
        _observe(store, 'reboot-window', monkeypatch, 0)
        _observe(store, 'reboot-window', monkeypatch, 900)
        boot = '22222222-2222-2222-2222-222222222222'
        restarted = _observe(store, 'reboot-window', monkeypatch, 1000, boot=boot)
        assert restarted['window_restarted'] is True
        for phase in OBSERVATION_OFFSETS[1:]:
            result = _observe(store, 'reboot-window', monkeypatch, 1000 + phase, boot=boot)
        assert result['status'] == 'succeeded_sedimented'
        tx = manager.store.get_transaction('reboot-window')
        receipt = manager.store.get_terminal_receipt('reboot-window')
        assert receipt['payload']['observation_valid'] is True
        assert tx['payload']['observation_window_history'][0]['reason'] == 'observation_boot_changed'
        assert tx['payload']['observation_clock_anchor']['monotonic_ns'] + 28800 * 10**9 <= tx['payload']['observation_last_clock']['monotonic_ns']


def test_native_observation_missing_real_clock_rolls_back_without_new_evidence(tmp_path, monkeypatch):
    with closing(RuntimeStore(tmp_path/'runtime')) as store:
        manager = _observing_manager(store, 'no-native-clock')
        def unavailable(): raise OSError('fixture unavailable clock')
        monkeypatch.setattr(watch, 'native_observation_clock', unavailable)
        result = watch._record_code_evolution_observation_sample(store, transaction_id='no-native-clock', owner_id='watch-test',
            sample=_sample('unknown-clock', observed_at=BASE.isoformat(), health_ok=True))
        assert result['status'] == 'rollback_required'
        assert manager.store.get_transaction('no-native-clock')['payload']['observation_valid'] is False


def test_native_observation_cannot_reset_restart_budget_by_metadata(tmp_path, monkeypatch):
    with closing(RuntimeStore(tmp_path/'runtime')) as store:
        manager = _observing_manager(store, 'corrupt-window')
        _observe(store, 'corrupt-window', monkeypatch, 0)
        _observe(store, 'corrupt-window', monkeypatch, 7200)
        manager.update_metadata('corrupt-window', payload_updates={'observation_window_generation': 0})
        result = _observe(store, 'corrupt-window', monkeypatch, 8100)
        assert result['status'] == 'rollback_required'
        assert manager.store.get_transaction('corrupt-window')['payload']['observation_recovery_reason'] == 'observation_window_generation_invalid'


def _freeze_authority_time(monkeypatch, instant):
    class Frozen(datetime):
        @classmethod
        def now(cls, tz=None): return instant.astimezone(tz) if tz else instant.replace(tzinfo=None)
    monkeypatch.setattr(owner, 'datetime', Frozen)
    monkeypatch.setattr(policy_api, 'datetime', Frozen)
    monkeypatch.setattr(issuer, 'datetime', Frozen)


def test_native_expired_unconsumed_policy_gets_one_audited_generation(native_policy_fixture, monkeypatch):
    f = native_policy_fixture
    f.grant['expires_at'] = None
    f.capture()
    first = _ensure(f)
    assert first['status'] == 'available', first
    state = _state(f)
    binding = owner._private_json(state/'published.json')
    original_path = state/(binding['intent_key'] + '.json')
    original = original_path.read_bytes()
    intent = json.loads(original)
    expired = datetime.fromisoformat(json.loads(intent['policy_bytes'])['expires_at']) + timedelta(seconds=1)
    _freeze_authority_time(monkeypatch, expired)
    renewed = _ensure(f)
    assert renewed['status'] == 'available', renewed
    assert renewed['policy_digest'] != first['policy_digest']
    assert original_path.read_bytes() == original
    retired = owner._private_json(state/(binding['intent_key'] + '-retirement.json'))
    assert retired['reason'] == 'expired_unconsumed_no_transaction'
    generation_path = state/(retired['next_intent_key'] + '.json')
    generation_raw = generation_path.read_bytes()
    assert _ensure(f)['policy_digest'] == renewed['policy_digest']
    assert generation_path.read_bytes() == generation_raw
    next_expired = datetime.fromisoformat(json.loads(json.loads(generation_raw)['policy_bytes'])['expires_at']) + timedelta(seconds=1)
    _freeze_authority_time(monkeypatch, next_expired)
    exhausted = _ensure(f)
    assert exhausted['reason'] == 'incident_authority_generations_exhausted'
    assert generation_path.read_bytes() == generation_raw and original_path.read_bytes() == original


def test_native_expired_policy_with_any_incident_transaction_is_not_renewed(native_policy_fixture, monkeypatch):
    f = native_policy_fixture; f.grant['expires_at'] = None; f.capture()
    assert _ensure(f)['status'] == 'available'
    state = _state(f); binding = owner._private_json(state/'published.json')
    intent = owner._private_json(state/(binding['intent_key'] + '.json'))
    from eimemory.governance.evolution.code_evolution_transaction import CodeEvolutionTransactionManager
    manager = CodeEvolutionTransactionManager(f.runtime, owner_id='fixture')
    manager.create_detected({'transaction_id': 'same-incident-pending', 'idempotency_key': 'same-incident-pending',
        'scope': intent['scope'], 'incident': {'incident_id': intent['incident_record_id'], 'incident_digest': intent['incident_digest']},
        'repository': {'root': str(f.repo), 'ref': 'fixture', 'base_commit': f.release.commit}})
    _freeze_authority_time(monkeypatch, datetime.fromisoformat(json.loads(intent['policy_bytes'])['expires_at']) + timedelta(seconds=1))
    result = _ensure(f)
    assert result.get('reason') == 'incident_already_has_transaction', result
    assert not (state/(binding['intent_key'] + '-generation-1.json')).exists()


def test_native_expiry_never_resets_consumption_even_if_policy_file_is_absent(native_policy_fixture, monkeypatch):
    f = native_policy_fixture; f.grant['expires_at'] = None; f.capture()
    first = _ensure(f)
    state = _state(f); binding = owner._private_json(state/'published.json')
    path = state/(binding['intent_key'] + '.json'); original = path.read_bytes(); intent = json.loads(original)
    ledger = _consumed(f.runtime.store, f.scope, tx='consumed-original', base=f.release.commit, digest=first['policy_digest'])
    consumed = deepcopy(ledger.get_policy_consumption(first['policy_digest']))
    f.destination.unlink()
    _freeze_authority_time(monkeypatch, datetime.fromisoformat(json.loads(intent['policy_bytes'])['expires_at']) + timedelta(seconds=1))
    result = _ensure(f)
    assert result['status'] == 'consumed' and result['transaction_id'] == 'consumed-original'
    assert ledger.get_policy_consumption(first['policy_digest']) == consumed and path.read_bytes() == original
    assert not f.destination.exists() and not (state/(binding['intent_key'] + '-generation-1.json')).exists()


@pytest.mark.parametrize('change', ['none', 'grant', 'scope', 'base', 'intent'])
def test_native_effect_fresh_standing_admission_is_exact_and_readonly(native_policy_fixture, change):
    f = native_policy_fixture; f.capture(); first = _ensure(f)
    binding = owner._private_json(_state(f)/'published.json')
    intent_path = _state(f)/(binding['intent_key'] + '.json')
    raw = intent_path.read_bytes(); original = json.loads(raw); policy = json.loads(original['policy_bytes'])
    transaction = {'payload': {'authorized_policy': {**policy, 'policy_digest': first['policy_digest']}},
        **original['scope'], 'repository_root': str(f.repo), 'incident_id': first['incident_record_id'],
        'base_commit': f.release.commit, 'policy_digest': first['policy_digest']}
    if change == 'grant': f.grant['enabled'] = False
    if change == 'scope': transaction['user_id'] = 'other'
    if change == 'base': transaction['base_commit'] = 'b'*40
    if change == 'intent': intent_path.unlink()
    if change == 'none':
        assert owner.verify_native_standing_authority(f.runtime, transaction=transaction, repo_root=f.repo)['status'] == 'verified'
    else:
        with pytest.raises((ValueError, OSError)):
            owner.verify_native_standing_authority(f.runtime, transaction=transaction, repo_root=f.repo)
    if intent_path.exists(): assert intent_path.read_bytes() == raw


def test_native_router_current_fence_preserves_original_and_retires_only_actual_success(tmp_path, monkeypatch):
    scope = ScopeRef(tenant_id='fixture', agent_id='fixture', workspace_id='fixture', user_id='fixture')
    commit = 'a'*40
    monkeypatch.setattr(failure, 'current_release_identity', lambda *_a: SimpleNamespace(commit=commit))
    policy = {'ok': True, 'status': 'enabled', 'policy_digest': 'c'*64, 'repository': {'base_commit': 'b'*40}}
    monkeypatch.setattr(failure, 'load_code_automation_policy', lambda **_k: policy)
    with closing(RuntimeStore(tmp_path/'runtime')) as store:
        runtime = SimpleNamespace(store=store)
        ledger = _consumed(store, scope)
        router = {'ok': False, 'status': 'blocked', 'reason': 'automation_policy_already_consumed', 'policy_transaction_id': 'old-tx'}
        first = failure.record_system_code_repair_failure(runtime, scope=scope, repair_report=router, detected_at=BASE.isoformat())
        original = store.get_by_id(first['incident_record_id']).to_dict()
        assert failure.current_system_code_repair_failure(store, scope=scope, expected_commit=commit) == first['incident']['incident_digest']
        failure.record_system_code_repair_failure(runtime, scope=scope,
            repair_report={'ok': False, 'status': 'blocked', 'reason': 'native_policy_owner_unverified'},
            detected_at=(BASE+timedelta(seconds=1)).isoformat())
        assert failure.current_system_code_repair_failure(store, scope=scope, expected_commit=commit) is None
        failure.record_system_code_repair_failure(runtime, scope=scope, repair_report={'ok': True, 'status': 'idle'},
            detected_at=(BASE+timedelta(seconds=2)).isoformat())
        assert failure.current_system_code_repair_failure(store, scope=scope, expected_commit=commit) == ''
        assert store.get_by_id(first['incident_record_id']).to_dict() == original
        stale = failure.record_system_code_repair_failure(runtime, scope=scope, repair_report=router, detected_at=BASE.isoformat())
        assert stale['status'] == 'superseded'
        assert failure.current_system_code_repair_failure(store, scope=scope, expected_commit=commit) == ''


def test_native_router_fence_requires_real_consumption_and_blocks_older_finisher(tmp_path, monkeypatch):
    scope = ScopeRef(tenant_id='fixture', agent_id='fixture', workspace_id='fixture', user_id='fixture')
    commit = 'a'*40
    monkeypatch.setattr(failure, 'current_release_identity', lambda *_a: SimpleNamespace(commit=commit))
    policy = {'ok': True, 'status': 'enabled', 'policy_digest': 'c'*64, 'repository': {'base_commit': 'b'*40}}
    monkeypatch.setattr(failure, 'load_code_automation_policy', lambda **_k: policy)
    with closing(RuntimeStore(tmp_path/'runtime')) as store:
        runtime = SimpleNamespace(store=store)
        router = {'ok': False, 'status': 'blocked', 'reason': 'automation_policy_already_consumed', 'policy_transaction_id': 'old-tx'}
        failure.record_system_code_repair_failure(runtime, scope=scope, repair_report=router, detected_at=BASE.isoformat())
        assert failure.current_system_code_repair_failure(store, scope=scope, expected_commit=commit) is None
        _consumed(store, scope)
        assert failure.current_system_code_repair_failure(store, scope=scope, expected_commit=commit)
        older = failure.begin_system_code_repair_observation(runtime, scope=scope, detected_at=(BASE+timedelta(seconds=1)).isoformat())
        assert failure.current_system_code_repair_failure(store, scope=scope, expected_commit=commit) is None
        newer = failure.begin_system_code_repair_observation(runtime, scope=scope, detected_at=(BASE+timedelta(seconds=2)).isoformat())
        failure.record_system_code_repair_failure(runtime, scope=scope, repair_report={'ok': True, 'status': 'idle'},
            detected_at=(BASE+timedelta(seconds=2)).isoformat(), observation_id=newer)
        result = failure.record_system_code_repair_failure(runtime, scope=scope, repair_report=router,
            detected_at=(BASE+timedelta(seconds=1)).isoformat(), observation_id=older)
        assert result['status'] == 'superseded'
        assert failure.current_system_code_repair_failure(store, scope=scope, expected_commit=commit) == ''


def test_native_current_router_failure_can_receive_exact_grant_without_resetting_old_consumption(native_policy_fixture, monkeypatch):
    f = native_policy_fixture
    kind = failure.INCIDENT_CLASS
    source, plan = owner._ROUTES[kind]
    from eimemory.governance.evolution.code_evolution_test_plans import protected_test_plan_digest
    f.grant['routes'] = {kind: {'source': source, 'detector_id': source+'.v1', 'test_plan_id': plan,
                              'test_plan_digest': protected_test_plan_digest(plan)}}
    monkeypatch.setattr(failure, 'current_release_identity', lambda *_a: f.release)
    monkeypatch.setattr(failure, 'load_code_automation_policy', lambda **_k: {
        'ok': True, 'status': 'enabled', 'policy_digest': 'c'*64, 'repository': {'base_commit': 'b'*40}})
    ledger = _consumed(f.runtime.store, f.scope)
    old = deepcopy(ledger.get_policy_consumption('c'*64))
    recorded = failure.record_system_code_repair_failure(f.runtime, scope=f.scope, repair_report={
        'ok': False, 'status': 'blocked', 'reason': 'automation_policy_already_consumed', 'policy_transaction_id': 'old-tx'},
        detected_at=datetime.now(timezone.utc).isoformat())
    result = _ensure(f)
    assert result['status'] == 'available', result
    assert result['incident_record_id'] == recorded['incident_record_id']
    issued = json.loads(f.destination.read_bytes())
    assert issued['verification']['test_plan_id'] == 'code.incident-routing-repair.v1'
    assert issued['patch']['allowed_files'] == ['eimemory/governance/evolution/system_code_repair.py']
    assert ledger.get_policy_consumption('c'*64) == old
    assert recorded['known_before_detection'] is None and recorded['manual_bootstrap'] is None


__all__ = ['native_policy_fixture'] + [name for name in globals() if name.startswith('test_')]
