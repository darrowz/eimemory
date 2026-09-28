from copy import deepcopy
import pytest
from eimemory.scheduler.result_contract import _nightly_step, _aggregate_nightly_ok


def wait():
    return {'ok': False, 'blocked_reason': 'recall_quality_evidence_incomplete',
            'blocking_metrics': {}, 'evidence_status': 'insufficient'}


def test_wait_step_and_aggregate_agree_without_forging_quality():
    steps = []; gate = wait(); before = deepcopy(gate)
    result = _nightly_step(steps, 'recall_quality_gate', lambda: gate)
    assert result == before and result['ok'] is False
    assert steps[0]['ok'] is True
    assert _aggregate_nightly_ok({'recall_quality_gate': result}, steps)


def test_production_wait_requires_explicit_execution_success():
    r = {'ok': False, 'execution_ok': True, 'blocked_reason': 'recall_quality_evidence_incomplete',
         'quality_gate': wait()}
    steps = []; _nightly_step(steps, 'production_recall', lambda: r)
    assert _aggregate_nightly_ok({'production_recall': r, 'recall_quality_gate': r['quality_gate']}, steps)
    r.pop('execution_ok'); steps = []; _nightly_step(steps, 'production_recall', lambda: r)
    assert not _aggregate_nightly_ok({'production_recall': r}, steps)


@pytest.mark.parametrize('error', ['terminal_transaction_lineage_mismatch', 'quality_repair_release_unbound'])
def test_identity_failure_cannot_hide_under_wait_flag(error):
    r = {'ok': False, 'awaiting_evidence': True, 'blocked_reason': error}
    assert not _aggregate_nightly_ok({'l5_loop': r}, [])


@pytest.mark.parametrize('boundary', ['source_validation', 'release_lineage', 'provider_binding'])
def test_successful_wrapper_cannot_mask_failed_authority(boundary):
    r = {'ok': True, boundary: {'ok': False}}
    steps = []; _nightly_step(steps, 'production_recall', lambda: r)
    assert not _aggregate_nightly_ok({'production_recall': r}, steps)


@pytest.mark.parametrize('failure', [{'error': 'sqlite_failed'}, {'errors': ['failed']},
                                    {'blocking_metrics': {'cross_channel_leakage_count': {'actual': 1}}}])
def test_real_errors_remain_fatal(failure):
    r = wait(); r.update(failure)
    steps = []; _nightly_step(steps, 'recall_quality_gate', lambda: r)
    assert not _aggregate_nightly_ok({'recall_quality_gate': r}, steps)


def test_wait_does_not_erase_other_step_failure():
    steps = []; _nightly_step(steps, 'recall_quality_gate', wait)
    _nightly_step(steps, 'storage_maintenance', lambda: {'ok': False, 'error': 'flush_failed'})
    assert not _aggregate_nightly_ok({'recall_quality_gate': wait()}, steps)


def test_l5_legitimate_wait_is_execution_success_not_l5():
    r = {'ok': False, 'awaiting_evidence': True, 'blocked_reason': 'awaiting_evidence'}
    steps = []; out = _nightly_step(steps, 'l5_loop', lambda: r)
    assert out['ok'] is False
    assert _aggregate_nightly_ok({'l5_loop': out}, steps)


def test_diagnostics_report_actual_first_failure():
    from eimemory.scheduler.result_contract import nightly_result_diagnostics
    steps = []; _nightly_step(steps, 'recall_quality_gate', wait)
    _nightly_step(steps, 'binding_check', lambda: {'ok': False})
    d = nightly_result_diagnostics({'recall_quality_gate': wait()}, steps)
    assert d['first_failed_step'] == 'binding_check'
    assert d['recall_quality_accepted'] is False
    assert d['release_acceptance'] == 'not_evaluated_by_scheduler'


def test_inconsistent_wait_with_failed_execution_is_not_hidden():
    r = wait(); r['recall_quality_evidence'] = {'execution_ok': False}
    assert not _aggregate_nightly_ok({'recall_quality_gate': r}, [])


def test_supervisor_reader_preserves_first_failed_step():
    from types import SimpleNamespace
    from eimemory.governance.learning.supervisor import _summary_from_record
    d={'execution_ok':False,'first_failed_step':'provider_binding','failed_steps':['provider_binding']}
    record=SimpleNamespace(content={'ok':False,'nightly_diagnostics':d})
    assert _summary_from_record(record,command='nightly')['nightly_diagnostics']==d


def test_supervisor_legacy_record_does_not_invent_diagnostics():
    from types import SimpleNamespace
    from eimemory.governance.learning.supervisor import _summary_from_record
    result=_summary_from_record(SimpleNamespace(content={'ok':False}),command='nightly')
    assert result['ok'] is False and not result.get('nightly_diagnostics')
