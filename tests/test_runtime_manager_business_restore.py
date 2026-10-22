"""Business validation runs with serving units, then restores the entire plan."""
from copy import deepcopy
import os
from types import SimpleNamespace

import pytest

from eimemory.contracts import runtime_manager as c
from test_manual_recovery_capture import recovery
from test_runtime_manager import lifecycle_fixture, validation_fixture


@pytest.fixture
def staged(recovery, monkeypatch):
    r = recovery
    r.bridge.transition(r.registry, r.record['job_key'], 'quiesce', 7, 1001)
    r.bridge.transition(r.registry, r.record['job_key'], 'resume', 7, 1001)
    target = r.base.parent / r.record['request']['target_commit']
    target.mkdir()
    monkeypatch.setattr(r.module, 'CURRENT', SimpleNamespace(resolve=lambda **_: target))
    r.registry['runtime_commit'] = target.name
    r.registry['generation'] = c.digest({k: v for k, v in r.registry.items() if k != 'generation'})
    r.publish(r.bridge.registry_path, r.registry)
    monkeypatch.setattr(r.bridge, 'runtime_compatible', lambda *_: True)
    r.calls.clear()
    return r


@pytest.mark.skipif(os.name != 'posix', reason='native durable recovery capture')
def test_core_prepare_preserves_history_and_holds_every_background_trigger(staged):
    r = staged
    key = r.record['job_key']
    original = r.bridge.record_path(r.prior['job_key']).read_bytes()
    before = r.bridge.record(key)
    first = r.bridge.prepare_business(r.registry, key, 7, 1001)
    record = r.bridge.record(key)
    assert first['phase'] == 'serving_awaiting_business'
    assert first['core_serving_ready'] and first['background_held'] and not first['resumed']
    assert set(first['background_pending']) == set(record['restore_plan']['active_units']) - c.CORE_SERVING_UNITS
    assert all(unit in c.CORE_SERVING_UNITS for unit, _ in r.calls)
    assert not any(unit in r.retired for unit, _ in r.calls)
    assert record['restore_plan'] == before['restore_plan']
    assert record['active_before'] == r.capture['active_before']
    assert record['enabled_before'] == before['enabled_before']
    for unit in first['background_pending']:
        assert r.states[unit]['ActiveState'] == 'inactive'
        assert r.states[unit]['UnitFileState'] == 'disabled'
    calls = list(r.calls)
    assert r.bridge.prepare_business(r.registry, key, 7, 1001)['idempotent']
    assert r.bridge.transition(r.registry, key, 'resume_watchers', 7, 1001)['deferred']
    assert r.calls == calls
    with pytest.raises(ValueError, match='complete_before_resume'):
        r.bridge.complete(r.registry, key, 7, 1001)
    with pytest.raises(ValueError, match='requires_background_restore'):
        r.bridge.finalize(r.registry, key, 7, 1001)
    assert r.bridge.record_path(r.prior['job_key']).read_bytes() == original


@pytest.mark.skipif(os.name != 'posix', reason='native durable recovery capture')
@pytest.mark.parametrize('outcome', ['passed', 'failed', 'exception'])
def test_finally_restores_all_background_without_forging_business_success(staged, monkeypatch, outcome):
    r = staged
    key = r.record['job_key']
    r.bridge.prepare_business(r.registry, key, 7, 1001)
    record = r.bridge.record(key)
    record['business_test_outcome'] = outcome
    r.bridge.save(r.registry, record)
    order = []
    native = r.bridge.systemctl
    monkeypatch.setattr(r.bridge, 'fence_job_processes', lambda *_a, **_kw: order.append('fenced'))
    def systemctl(registry, unit, *args):
        order.append(unit)
        return native(registry, unit, *args)
    monkeypatch.setattr(r.bridge, 'systemctl', systemctl)
    monkeypatch.setattr(r.bridge, 'recover_release_observers', lambda *_: pytest.fail('explicit observations precede worker exit'))
    result = r.bridge.restore_background(r.registry, key, 7, 1001)
    after = r.bridge.record(key)
    assert order[0] == 'fenced'
    assert result['resumed'] and not result['background_held']
    assert result['phase'] == 'resumed_awaiting_business'
    assert after['business_test_outcome'] == outcome and after.get('finalized') is not True
    assert after['restore_plan'] == record['restore_plan']
    proof = r.bridge.verify_restoration(r.registry, after)
    assert proof['historical_active_before'] == r.capture['active_before']
    assert proof['autonomous_repair_credit'] is False
    assert set(proof['restored_active_units']) == set(r.capture['active_before']) - set(r.retired)
    assert not any(unit in r.retired for unit, _ in r.calls)
    calls = list(r.calls)
    assert r.bridge.restore_background(r.registry, key, 7, 1001)['idempotent']
    assert r.calls == calls
    if outcome != 'passed':
        monkeypatch.setattr(r.bridge, 'verify_completed_validation', lambda *_: (_ for _ in ()).throw(ValueError('business_failed')))
        with pytest.raises(ValueError, match='business_failed'):
            r.bridge.complete(r.registry, key, 7, 1001)
        assert r.bridge.record(key).get('finalized') is not True
        pointer = c.strict_json((r.bridge.state / 'active-transition.json').read_bytes())
        assert pointer == dict(job_key=key, complete=False)


@pytest.mark.skipif(os.name != 'posix', reason='native durable recovery capture')
@pytest.mark.parametrize('fault', ['current', 'pointer', 'compatibility', 'background_live', 'retired_live', 'changed_plan'])
def test_prepare_cannot_waive_current_plan_or_native_quiescence(staged, monkeypatch, fault):
    r = staged
    key = r.record['job_key']
    if fault == 'current':
        monkeypatch.setattr(r.module, 'CURRENT', SimpleNamespace(resolve=lambda **_: r.base.parent / ('f' * 40)))
    elif fault == 'pointer':
        r.publish(r.bridge.state / 'active-transition.json', dict(job_key='e' * 64, complete=False))
    elif fault == 'compatibility':
        monkeypatch.setattr(r.bridge, 'runtime_compatible', lambda *_: False)
    elif fault == 'background_live':
        r.states['eimemory-nightly.timer']['ActiveState'] = 'active'
    elif fault == 'retired_live':
        r.states[r.retired[0]]['MainPID'] = '99'
    else:
        record = r.bridge.record(key)
        record['restore_plan']['active_units'].append(r.retired[0])
        r.bridge.save(r.registry, record)
    with pytest.raises(ValueError):
        r.bridge.prepare_business(r.registry, key, 7, 1001)
    assert r.calls == []
    assert r.bridge.record(key).get('finalized') is not True


@pytest.mark.skipif(os.name != 'posix', reason='native durable recovery capture')
def test_background_dispatch_crash_is_not_replayed_or_mistaken_for_restore(staged, monkeypatch):
    r = staged
    key = r.record['job_key']
    r.bridge.prepare_business(r.registry, key, 7, 1001)
    native = r.bridge.systemctl
    attempted = []
    def ambiguous(registry, unit, *args):
        if unit == 'eimemory-nightly.timer' and args[0] == 'start':
            attempted.append(unit)
            raise OSError('lost native dispatch response')
        return native(registry, unit, *args)
    monkeypatch.setattr(r.bridge, 'systemctl', ambiguous)
    with pytest.raises(OSError):
        r.bridge.restore_background(r.registry, key, 7, 1001)
    record = r.bridge.record(key)
    assert record['phase'] == 'background_restore_intent'
    assert not record['resumed'] and record['background_held']
    with pytest.raises(ValueError, match='restored_unit_not_running'):
        r.bridge.restore_background(r.registry, key, 7, 1001)
    assert attempted == ['eimemory-nightly.timer']
    assert r.bridge.record(key).get('finalized') is not True


def test_core_stage_never_starts_captured_backup_and_rollback_retains_full_restore(monkeypatch, tmp_path):
    _m, b, r, _req, key, records, _stored, _target, calls, _persist = lifecycle_fixture(monkeypatch, tmp_path)
    active = ['eimemory-rpc.service', 'eimemory-backup.service', 'eimemory-nightly.timer']
    records[key].update(active_before=active)
    record = records[key]
    b.resume(r, record, stage='core')
    assert [argv[-1] for argv in calls] == ['eimemory-rpc.service']
    calls.clear()
    record['request']['operation'] = 'rollback'
    b.resume(r, record)
    assert [argv[-1] for argv in calls] == active


def test_business_child_accepts_only_verified_core_phase(monkeypatch, tmp_path):
    _m, b, r, _req, key, parent, _store, state, _artifact, calls = validation_fixture(monkeypatch, tmp_path)
    parent.update(resumed=False, business_prepare_requested=True, core_serving_ready=True,
        background_held=True, phase='serving_awaiting_business')
    b.save(r, parent)
    seen = []
    monkeypatch.setattr(b, 'verify_serving_restoration', lambda *_: seen.append('serving'))
    monkeypatch.setattr(b, 'verify_background_quiet', lambda *_: seen.append('quiet'))
    state['ControlGroup'] = '/system.slice/' + c.validation_unit(key, 'post_resume')
    result = b.validate_job(r, key, 'post_resume', 42, 1001)
    assert result['phase'] == 'serving_awaiting_business'
    assert result['admission_phase'] == 'serving_awaiting_business'
    assert seen == ['serving', 'quiet']
    assert len(calls) == 1
    parent.update(resumed=True, background_held=False, phase='resumed_awaiting_business')
    b.save(r, parent)
    monkeypatch.setattr(b, 'verify_background_quiet', lambda *_: pytest.fail('original child must be read without restarting'))
    reread = b.validate_job(r, key, 'post_resume', 43, 1001)
    assert reread['admission_phase'] == 'serving_awaiting_business'
    assert reread['phase'] == 'resumed_awaiting_business' and len(calls) == 1


def test_business_child_rejects_stale_ready_flags_after_background_release(monkeypatch, tmp_path):
    _m, b, r, _req, key, parent, *_ = validation_fixture(monkeypatch, tmp_path)
    parent.update(resumed=True, business_prepare_requested=True, core_serving_ready=True,
        background_held=False, phase='resumed_awaiting_business')
    b.save(r, parent)
    with pytest.raises(ValueError, match='business_before_resume'):
        b.validate_job(r, key, 'post_resume', 42, 1001)


@pytest.mark.skipif(os.name != 'posix', reason='native durable recovery capture')
def test_explicit_failed_nightly_recovery_waits_for_actual_successful_worker_terminal(staged, monkeypatch):
    r = staged
    key = r.record['job_key']
    unit = 'eimemory-nightly.service'
    r.bridge.prepare_business(r.registry, key, 7, 1001)
    r.bridge.restore_background(r.registry, key, 7, 1001)
    record = r.bridge.record(key)
    with pytest.raises(ValueError, match='before_native_completion'):
        r.bridge.recover_release_observers(r.registry, record)
    record.update(finalized=True, phase='completed')
    r.publish(r.bridge.state / 'active-transition.json', dict(job_key=key, complete=True, held=False))
    with pytest.raises(ValueError, match='before_native_completion'):
        r.bridge.recover_release_observers(r.registry, record)
    r.registry['units'][unit]['installed'] = True
    r.registry['generation'] = c.digest({k: v for k, v in r.registry.items() if k != 'generation'})
    terminal = dict(LoadState='loaded', ActiveState='active', SubState='exited', Result='success',
        InvocationID=record['original_invocation'], MainPID='0', ExecMainStatus='0',
        ControlGroup='', ExecMainExitTimestampMonotonic='101')
    record['terminal_state'] = terminal
    r.bridge.save(r.registry, record)
    r.states[unit] = dict(LoadState='loaded', ActiveState='failed', SubState='failed', Result='exit-code',
        InvocationID='8' * 32, MainPID='0', ExecMainStatus='1', ControlGroup='',
        ExecMainExitTimestampMonotonic='100', Type='oneshot')
    monkeypatch.setattr(r.bridge, 'job_status', lambda *_: dict(terminal_state=terminal,
        state={**terminal, 'ActiveState': 'inactive', 'SubState': 'dead'}))
    r.calls.clear()
    r.bridge.release_job(r.registry, key)
    assert r.calls == [(unit, ('start', '--no-block'))]
    r.bridge.release_job(r.registry, key)
    assert r.calls == [(unit, ('start', '--no-block'))]
    assert r.bridge.record(key)['observer_recovery'][unit]['before']['Result'] == 'exit-code'
