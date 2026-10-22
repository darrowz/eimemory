"""A new failed release retains old captured facts and the enrolled topology."""
from copy import deepcopy
from hashlib import sha256
import os
from types import SimpleNamespace

import pytest

from eimemory.contracts import runtime_manager as c
from test_manual_recovery_capture import recovery
from test_user_release_recovery import installer


@pytest.fixture
def rotated(recovery, monkeypatch):
    r = recovery
    r.bridge.transition(r.registry, r.record['job_key'], 'quiesce', 7, 1001)
    source = r.bridge.record(r.record['job_key'])
    r.bridge.resume(r.registry, source)
    source.update(resumed=True, phase='resumed_awaiting_business', resume_requested=True)
    current = r.base.parent / source['request']['candidate_commit']
    current.mkdir()
    monkeypatch.setattr(r.module, 'CURRENT', SimpleNamespace(resolve=lambda **_: current))
    r.registry['runtime_commit'] = current.name
    r.registry['generation'] = c.digest({k: v for k, v in r.registry.items() if k != 'generation'})
    failure = dict(LoadState='loaded', ActiveState='failed', SubState='failed', Result='exit-code',
        InvocationID=source['original_invocation'], ExecMainStatus='1', MainPID='0', ControlGroup='',
        ExecMainExitTimestampMonotonic='345')
    source['terminal_state'] = failure
    r.states[source['unit']] = dict(failure)
    history = r.bridge.state / 'managed-incident-receipts/manual-abandon-67890'
    history.mkdir(mode=0o700)
    def publish_source():
        raw = c.canonical(source)
        r.bridge.save(r.registry, source)
        (history / 'job.original').write_bytes(raw)
        capture = installer().recovery_capture(c, source, raw, failure, current.name,
            history.name, source['enabled_before'])
        capture_raw = c.canonical(capture)
        (history / 'recovery-capture.json').write_bytes(capture_raw)
        (r.bridge.state / 'manual-recovery-capture.json').write_bytes(capture_raw)
        r.publish(history / 'native-exit.json', failure)
        r.publish(history / 'enabled-before-sources.json', dict(source='original_job', job_sha256=sha256(raw).hexdigest()))
        r.publish(history / 'complete.json', dict(complete=True, job_key=source['job_key'],
            outcome='operator_abandoned_failed_manual_release', recovery_history=history.name,
            recovery_capture_sha256=sha256(capture_raw).hexdigest(), runtime_commit=current.name,
            failed_deployment_preserved=True, release_accepted=False, autonomous_repair_credit=False))
        return capture, dict(basename='manual-recovery-capture.json', sha256=sha256(capture_raw).hexdigest())
    capture, reference = publish_source()
    r.registry['controller_commit'] = 'e' * 40
    r.registry['generation'] = c.digest({k: v for k, v in r.registry.items() if k != 'generation'})
    request = {**source['request'], 'base_commit': current.name, 'candidate_commit': 'e' * 40,
        'target_commit': 'e' * 40, 'controller_commit': 'e' * 40,
        'validation_controller_commit': 'e' * 40, 'reviewed_attempt_id': '9' * 32}
    key = c.digest(request)
    following = dict(job_key=key, request=request, registry=deepcopy(r.registry), manual_release=True,
        quiesced=False, resumed=False, active_before=None, phase='submitted', unit=c.job_unit(key),
        original_invocation='a' * 32, recovery_capture=reference, topology_intent=source['topology_intent'])
    r.bridge.save(r.registry, following)
    r.publish(r.bridge.state / 'active-transition.json', dict(job_key=source['job_key'], complete=True,
        outcome='operator_abandoned_failed_manual_release'))
    return SimpleNamespace(r=r, source=source, following=following, capture=capture,
        current=current, history=history, publish_source=publish_source)


@pytest.mark.skipif(os.name != 'posix', reason='native durable capture consumption')
def test_rotated_failed_capture_authenticates_retired_history_without_reenrollment(rotated):
    x = rotated
    r = x.r
    old_source = r.bridge.record_path(r.prior['job_key']).read_bytes()
    old_intent = (r.bridge.registry_path.parent / 'hermes-topology-intent.v1.json').read_bytes()
    old_logical = (r.bridge.registry_path.parent / 'hermes-topology.v1.json').read_bytes()
    old_ledger = (r.history / 'capture-consumption.json').read_bytes()
    for unit in r.retired:
        assert x.source['registry']['units'][unit]['installed'] is False
        assert unit in x.capture['active_before']
    proof = r.bridge.manual_recovery_capture(r.registry, x.following, x.current)
    assert proof[0] == x.capture
    r.calls.clear()
    r.bridge.transition(r.registry, x.following['job_key'], 'quiesce', 8, 1001)
    record = r.bridge.record(x.following['job_key'])
    assert set(x.capture['active_before']) <= set(record['active_before'])
    assert not set(r.retired) & set(record['restore_plan']['active_units'])
    assert record['restore_plan']['topology_intent'] == x.source['restore_plan']['topology_intent']
    assert not any(unit in r.retired for unit, _ in r.calls)
    assert r.bridge.record_path(r.prior['job_key']).read_bytes() == old_source
    assert (r.bridge.registry_path.parent / 'hermes-topology-intent.v1.json').read_bytes() == old_intent
    assert (r.bridge.registry_path.parent / 'hermes-topology.v1.json').read_bytes() == old_logical
    assert (r.history / 'capture-consumption.json').read_bytes() == old_ledger


@pytest.mark.skipif(os.name != 'posix', reason='native durable capture consumption')
@pytest.mark.parametrize('fault', ['missing_ledger', 'foreign_ledger', 'old_capture', 'applied',
    'plan', 'nonretired_missing', 'source_reference', 'intent_history'])
def test_retired_history_exception_requires_the_entire_real_consumption_chain(rotated, fault):
    x = rotated
    r = x.r
    if fault == 'missing_ledger':
        (r.history / 'capture-consumption.json').unlink()
    elif fault == 'foreign_ledger':
        raw = c.strict_json((r.history / 'capture-consumption.json').read_bytes())
        raw['job_key'] = 'f' * 64
        r.publish(r.history / 'capture-consumption.json', raw)
    elif fault == 'old_capture':
        (r.history / 'recovery-capture.json').write_bytes(b'{}')
    elif fault == 'intent_history':
        (r.registration / 'intent.original').write_bytes(b'{}')
    else:
        if fault == 'applied':
            x.source.pop('recovery_capture_applied')
        elif fault == 'plan':
            x.source['restore_plan']['active_units'].append(r.retired[0])
        elif fault == 'nonretired_missing':
            x.source['registry']['units']['eimemory-rpc.service']['installed'] = False
            x.source['registry']['generation'] = c.digest({k: v for k, v in x.source['registry'].items() if k != 'generation'})
        else:
            x.source['recovery_capture']['sha256'] = '0' * 64
        _capture, reference = x.publish_source()
        x.following['recovery_capture'] = reference
    r.calls.clear()
    with pytest.raises((ValueError, OSError)):
        r.bridge.manual_recovery_capture(r.registry, x.following, x.current)
    assert r.calls == []


def test_recovery_history_cycle_and_depth_fail_before_reading_any_source(recovery):
    r = recovery
    key = r.record['job_key']
    for chain in ((key,), tuple(str(index).zfill(64) for index in range(64))):
        with pytest.raises(ValueError, match='cycle_or_depth'):
            r.bridge.manual_recovery_capture(r.registry, r.record, r.base, _source_chain=chain)
    assert r.calls == []


def fail_and_rotate(x, monkeypatch, *, next_candidate, history_number):
    """Run the actual sealed-plan/consumption operations for another generation."""
    r = x.r
    r.bridge.transition(r.registry, x.following['job_key'], 'quiesce', 8, 1001)
    source = r.bridge.record(x.following['job_key'])
    r.bridge.resume(r.registry, source)
    source.update(resumed=True, phase='resumed_awaiting_business', resume_requested=True)
    current = r.base.parent / source['request']['candidate_commit']
    current.mkdir(exist_ok=True)
    monkeypatch.setattr(r.module, 'CURRENT', SimpleNamespace(resolve=lambda **_: current))
    r.registry.update(runtime_commit=current.name, controller_commit=next_candidate)
    r.registry['generation'] = c.digest({k: v for k, v in r.registry.items() if k != 'generation'})
    failure = dict(LoadState='loaded', ActiveState='failed', SubState='failed', Result='exit-code',
        InvocationID=source['original_invocation'], ExecMainStatus='1', MainPID='0', ControlGroup='',
        ExecMainExitTimestampMonotonic=str(history_number))
    source['terminal_state'] = failure
    r.states[source['unit']] = dict(failure)
    source_raw = c.canonical(source)
    r.bridge.save(r.registry, source)
    history = r.bridge.state / 'managed-incident-receipts' / ('manual-abandon-' + str(history_number))
    history.mkdir(mode=0o700)
    capture = installer().recovery_capture(c, source, source_raw, failure, current.name,
        history.name, source['enabled_before'])
    capture_raw = c.canonical(capture)
    (history / 'job.original').write_bytes(source_raw)
    (history / 'recovery-capture.json').write_bytes(capture_raw)
    (r.bridge.state / 'manual-recovery-capture.json').write_bytes(capture_raw)
    r.publish(history / 'native-exit.json', failure)
    r.publish(history / 'enabled-before-sources.json', dict(source='original_job', job_sha256=sha256(source_raw).hexdigest()))
    complete = dict(complete=True, job_key=source['job_key'], outcome='operator_abandoned_failed_manual_release',
        recovery_history=history.name, recovery_capture_sha256=sha256(capture_raw).hexdigest(),
        runtime_commit=current.name, failed_deployment_preserved=True, release_accepted=False, autonomous_repair_credit=False)
    r.publish(history / 'complete.json', complete)
    r.publish(r.bridge.state / 'active-transition.json', complete)
    request = {**source['request'], 'base_commit': current.name, 'candidate_commit': next_candidate,
        'target_commit': next_candidate, 'controller_commit': next_candidate,
        'validation_controller_commit': next_candidate, 'reviewed_attempt_id': str(history_number).zfill(32)}
    key = c.digest(request)
    following = dict(job_key=key, request=request, registry=deepcopy(r.registry), manual_release=True,
        quiesced=False, resumed=False, active_before=None, phase='submitted', unit=c.job_unit(key),
        original_invocation=str(history_number + 1).zfill(32),
        recovery_capture=dict(basename='manual-recovery-capture.json', sha256=sha256(capture_raw).hexdigest()),
        topology_intent=source['topology_intent'])
    r.bridge.save(r.registry, following)
    return SimpleNamespace(r=r, source=source, following=following, capture=capture, current=current, history=history)


@pytest.mark.skipif(os.name != 'posix', reason='native multiple durable consumption generations')
@pytest.mark.parametrize('fault', ['', 'live_current', 'old_ledger', 'old_original'])
def test_three_failed_consumption_generations_keep_historical_coordinates_and_same_topology(rotated, monkeypatch, fault):
    first = rotated
    r = first.r
    intent_path = r.bridge.registry_path.parent / 'hermes-topology-intent.v1.json'
    original_intent = intent_path.read_bytes()
    second = fail_and_rotate(first, monkeypatch, next_candidate='f' * 40, history_number=67891)
    third = fail_and_rotate(second, monkeypatch, next_candidate='9' * 40, history_number=67892)
    assert third.current.name not in {first.source['request']['base_commit'], first.source['request']['candidate_commit']}
    if fault == 'live_current':
        third.current = r.base.parent / ('0' * 40)
    elif fault == 'old_ledger':
        (r.history / 'capture-consumption.json').unlink()
    elif fault == 'old_original':
        (r.history / 'job.original').write_bytes(b'{}')
    r.calls.clear()
    if fault:
        with pytest.raises((ValueError, OSError)):
            r.bridge.manual_recovery_capture(r.registry, third.following, third.current)
    else:
        capture, _binding, _history = r.bridge.manual_recovery_capture(r.registry, third.following, third.current)
        assert capture == third.capture
        r.bridge.transition(r.registry, third.following['job_key'], 'quiesce', 9, 1001)
        record = r.bridge.record(third.following['job_key'])
        assert not set(r.retired) & set(record['restore_plan']['active_units'])
        assert set(first.capture['active_before']) <= set(record['active_before'])
        assert not any(unit in r.retired for unit, _ in r.calls)
    assert intent_path.read_bytes() == original_intent
