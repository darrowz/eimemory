from pathlib import Path
from types import SimpleNamespace

import pytest

from eimemory.ops import runtime_service_observation as observation
from eimemory.ops import runtime_identity_drift as drift


@pytest.mark.parametrize('group,expected', [
    ('0::/user.slice/user-1001.slice/user@1001.service/app.slice/eimemory-nightly.service', False),
    ('0::/system.slice/eimemory-nightly.service', True),
    ('0::/system.slice/sshd.service', False),
    ('0::/user.slice/user-1001.slice/session-10.scope', False),
])
def test_native_owner_context_does_not_select_a_healthy_shadow(monkeypatch, group, expected):
    monkeypatch.delenv('EIMEMORY_RUNTIME_TRANSITION_JOB', raising=False)
    monkeypatch.setattr(Path, 'read_text', lambda self: group)
    assert observation.registered_runtime_context() is expected


def test_malformed_managed_context_never_falls_back(monkeypatch):
    monkeypatch.setenv('EIMEMORY_RUNTIME_TRANSITION_JOB', 'invalid')
    with pytest.raises(ValueError, match='runtime_transition_job_invalid'):
        observation.service_state('eimemory-rpc.service', ('MainPID',))


def test_ordinary_service_reads_user_manager_even_with_registered_shadow(monkeypatch):
    monkeypatch.setattr(observation, 'registered_runtime_context', lambda: False)
    calls = []
    def run(argv, **kwargs):
        calls.append(argv)
        return SimpleNamespace(returncode=0, stdout='LoadState=loaded\nEnvironment=EIMEMORY_RUNTIME_COMMIT='+'a'*40+'\n')
    monkeypatch.setattr(observation.subprocess, 'run', run)
    assert observation.runtime_environment('eimemory-rpc.service') == 'EIMEMORY_RUNTIME_COMMIT='+'a'*40
    assert calls == [['systemctl','--user','show','eimemory-rpc.service','--property=LoadState,Environment']]


def test_registered_service_never_queries_user_manager(monkeypatch):
    from eimemory.core import runtime_manager
    monkeypatch.setenv('EIMEMORY_RUNTIME_TRANSITION_JOB', 'a'*64)
    monkeypatch.setattr(runtime_manager, 'unit_state', lambda unit, properties: {'MainPID':'123'})
    monkeypatch.setattr(observation.subprocess, 'run', lambda *a, **kw: pytest.fail('unexpected user fallback'))
    assert observation.service_state('eimemory-rpc.service', ('MainPID',)) == {'MainPID':'123'}


@pytest.mark.parametrize('output', ['MainPID=0\nMainPID=123\n', '', 'Other=123\n'])
def test_incomplete_or_ambiguous_native_response_stays_failed(monkeypatch, output):
    monkeypatch.setattr(observation, 'registered_runtime_context', lambda: False)
    monkeypatch.setattr(observation.subprocess, 'run', lambda *a, **kw: SimpleNamespace(returncode=0,stdout=output))
    with pytest.raises(ValueError, match='runtime_user_manager_response'):
        observation.service_state('eimemory-rpc.service', ('MainPID',))


def test_query_failure_is_not_a_missing_service(monkeypatch):
    monkeypatch.setattr(observation, 'registered_runtime_context', lambda: False)
    monkeypatch.setattr(observation.subprocess, 'run', lambda *a, **kw: SimpleNamespace(returncode=1,stdout=''))
    with pytest.raises(RuntimeError, match='runtime_user_manager_read_failed'):
        observation.runtime_environment('eimemory-rpc.service')


def test_observed_absence_does_not_require_nonexistent_type_properties(monkeypatch):
    monkeypatch.setattr(observation, 'registered_runtime_context', lambda: False)
    monkeypatch.setattr(observation.subprocess, 'run', lambda *a, **kw:
        SimpleNamespace(returncode=0, stdout='LoadState=not-found\n'))
    assert observation.runtime_environment('optional.service') is None
    assert observation.service_state('optional.timer', ('LoadState','NextElapseUSecRealtime')) == {
        'LoadState':'not-found','NextElapseUSecRealtime':''}


def test_maintenance_reports_failed_read_separately_from_empty_inventory(monkeypatch):
    from eimemory.governance import evidence_contract
    from eimemory.models.records import ScopeRef
    monkeypatch.setattr(evidence_contract, 'current_release_identity', lambda *a: SimpleNamespace(commit='a'*40))
    def failed(unit):
        raise RuntimeError('native query failed')
    report = drift.inspect_live_runtime_identity(SimpleNamespace(),scope=ScopeRef(),
        detected_at='2026-10-20T08:00:00Z',runner=failed)
    assert report['ok'] is False and report['reason']=='runtime_unit_observation_failed'


def test_user_stability_journal_observes_actual_units_without_exporting_messages(monkeypatch):
    from eimemory.ops import controlled_stability as stability
    monkeypatch.setattr(observation, 'registered_runtime_context', lambda: False)
    monkeypatch.setattr(stability.os, 'geteuid', lambda: 1001, raising=False)
    calls = []
    def output(argv):
        calls.append(argv)
        return '{"MESSAGE":"private business: database is locked"}'
    monkeypatch.setattr(stability, '_journal_output', output)
    report = stability._journal()
    assert not report['ok'] and report['observed_error_counts']['database_read_write_failure']==1
    assert report['observation_scope']=='allowlisted_user_unit_journal'
    assert '--user' in calls[0] and '_SYSTEMD_USER_UNIT=eimemory-rpc.service' in calls[0]
    assert 'private business' not in str(report)


def test_stability_does_not_require_service_process_attributes_from_timers(monkeypatch):
    from eimemory.ops import controlled_stability as stability
    calls = []
    def state(unit, properties):
        calls.append((unit, properties))
        return {'Id':unit}
    monkeypatch.setattr(observation, 'service_state', state)
    stability._unit_states()
    assert all(not {'MainPID','NRestarts','ExecMainStatus'} & set(properties)
               for unit, properties in calls if unit.endswith('.timer'))
    assert all({'MainPID','NRestarts','ExecMainStatus'} <= set(properties)
               for unit, properties in calls if unit.endswith('.service'))


def test_owner_and_timer_monitor_do_not_require_timer_schedule_on_services(monkeypatch):
    from eimemory.ops import code_implementation_owner as owner, timer_monitor
    calls = []
    def state(unit, properties):
        calls.append((unit, properties))
        return {'LoadState':'loaded','ActiveState':'active','Result':'success'}
    monkeypatch.setattr(observation, 'service_state', state)
    owner._run_systemctl(['systemctl','show','eimemory-code-implementation-refresh.service',
        '--property=LoadState,ActiveState,Result,LastTriggerUSec,NextElapseUSecRealtime'])
    timer_monitor._collect_unit_states(['eimemory-rpc.service'])
    assert len(calls)==2 and all(not {'LastTriggerUSec','NextElapseUSecRealtime'} & set(props)
                                for unit, props in calls)
