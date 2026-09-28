"""Report format, admission and incident disposition are independent contracts."""
from copy import deepcopy
import json
import pytest

from eimemory.governance.release.closure_verdict import summarize_release_closure, failure_signals
from eimemory.ops.release_closure_failure import detect_release_closure_failure
from release_report_fixtures import complete_report, accumulating_report, wait_report


def blocked(reason='bootstrap_pending_non_recall_l5_evidence_incomplete'):
    return {'report_type':'l5_release_closure','ok':False,'closure_complete':False,
            'data_accumulating':False,'blocked_stage':'readiness','blocked_reason':reason,
            'deployment':{'commit':'a'*40,'promotion_request_id':'r1'},
            'deployment_receipt':{'release_session_id':'s1'}}


def test_valid_blocked_report_is_not_contract_error_or_admission():
    out=summarize_release_closure(blocked())
    assert out['report_contract_ok'] is True and out['contract_error']==''
    assert out['disposition']=='diagnosis_required'
    assert not out['ok'] and not out['admission_ok'] and out['exit_code']==1
    assert not out['closure_certified'] and not out['repair_complete']


@pytest.mark.parametrize('reason',['production_dataset_not_ready','recall_quality_evidence_incomplete',
                                  'current_release_channel_receipt_not_found','awaiting_evidence'])
def test_pure_wait_does_not_fabricate_failure_or_completion(reason):
    report=blocked(reason)
    out=summarize_release_closure(report)
    assert out['disposition']=='evidence_waiting' and out['contract_error']==''
    detected=detect_release_closure_failure(report,detected_at='2026-09-28T08:00:00Z')
    assert detected['incident'] is None and not detected['repair_complete']


@pytest.mark.parametrize('container',['release_lineage','readiness','closure_rehearsal','replay_bootstrap'])
@pytest.mark.parametrize('field',['error','contract_error','gate_errors','blocked_reasons'])
def test_hard_failure_dominates_wait_at_control_boundaries(container,field):
    report=blocked('recall_quality_evidence_incomplete')
    value='release_closure_report_contract_invalid'
    if field=='gate_errors': value={'__contract__':value}
    if field=='blocked_reasons': value=[value]
    report[container]={'ok':False,field:value}
    report['data_accumulating']=True
    out=detect_release_closure_failure(report,detected_at='2026-09-28T08:00:00Z')
    assert out['status']=='failure_detected' and out['incident']
    assert out['incident']['incident_class']=='release.closure_report_failure'


@pytest.mark.parametrize('value',[True,False])
def test_success_bit_cannot_hide_explicit_errors(value):
    r=blocked('production_dataset_not_ready');r['ok']=value
    r['error']='report_binding_mismatch'
    assert summarize_release_closure(r)['disposition']=='failure_detected'


@pytest.mark.parametrize('reason',['terminal_transaction_lineage_mismatch','quality_repair_release_unbound'])
def test_identity_break_is_not_a_sample_wait(reason):
    assert summarize_release_closure(blocked(reason))['disposition']=='failure_detected'


def test_unknown_aggregate_creates_diagnosis_not_auto_repair_authority():
    out=detect_release_closure_failure(blocked(),detected_at='now')
    assert out['incident'] and out['status']=='diagnosis_required'
    assert out['repair_eligible'] is False


@pytest.mark.parametrize('mutate',[
    lambda r:r.update(ok='false'), lambda r:r.update(report_type='unknown'),
    lambda r:r.update(closure_complete=True),lambda r:r.update(blocked_stage=''),
    lambda r:r.update(readiness=[]),lambda r:r.update(readiness={'ok':1}),
])
def test_malformed_reports_fail_closed(mutate):
    r=blocked();mutate(r);out=summarize_release_closure(r)
    assert out['contract_error']=='release_closure_report_contract_invalid'
    assert not out['report_contract_ok'] and out['exit_code']!=0


@pytest.mark.parametrize('factory',[complete_report,accumulating_report,wait_report])
def test_complete_positive_controls_still_pass_without_weakening(factory):
    r=factory();out=summarize_release_closure(r)
    assert out['report_contract_ok'] and out['admission_ok'],out
    assert out['exit_code']==0
    assert out['closure_certified'] is (r['closure_complete'] is True)


def test_success_missing_evidence_remains_invalid():
    r=complete_report();r['live_acceptance']['cases'].pop()
    out=summarize_release_closure(r)
    assert out['contract_error'] and not out['closure_certified']


def test_unrun_stages_and_business_text_are_not_errors():
    r=blocked('production_dataset_not_ready')
    r['live_acceptance']={'ok':False,'status':'not_run','reason':'upstream_gate_not_run'}
    r['samples']=[{'error':'not a control-plane report'}]
    assert summarize_release_closure(r)['disposition']=='evidence_waiting'


@pytest.mark.parametrize('value',[float('nan'),float('inf'),float('-inf')])
def test_nonfinite_report_never_breaks_json_emission(value):
    r=blocked();r['readiness']={'readiness_score':value}
    out=summarize_release_closure(r)
    assert out['contract_error']
    json.dumps(out,allow_nan=False)


def test_nested_cycle_returns_controlled_failure():
    r=blocked();r['readiness']=r
    assert summarize_release_closure(r)['exit_code']!=0


def test_report_digests_bind_receipt_session_and_input():
    one=blocked('storage_failed');two=deepcopy(one)
    two['deployment_receipt']['release_session_id']='s2'
    a=detect_release_closure_failure(one,detected_at='t1')
    again=detect_release_closure_failure(one,detected_at='t2')
    b=detect_release_closure_failure(two,detected_at='t1')
    assert a['incident']['incident_digest']==again['incident']['incident_digest']
    assert a['incident']['incident_digest']!=b['incident']['incident_digest']


def test_explicit_process_error_overrides_valid_wait():
    out=summarize_release_closure(blocked('production_dataset_not_ready'),
        execution={'closure_exit_status':2,'expected_commit':'a'*40})
    assert out['disposition']=='failure_detected'


def test_wrong_commit_not_correlated_as_same_release():
    out=summarize_release_closure(blocked('production_dataset_not_ready'),
        execution={'expected_commit':'b'*40})
    assert any(x['code']=='closure_report_release_mismatch' for x in out['failure_signals']['hard_errors'])


def test_arbitrary_error_text_is_not_copied_into_diagnostic_signals():
    r=blocked();r['error']='token=private-do-not-log'
    signals=failure_signals(r)
    assert 'private-do-not-log' not in json.dumps(signals)
