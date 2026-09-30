"""Actual gate details survive summary, incident creation and repair handoff."""
from __future__ import annotations

from copy import deepcopy
from types import SimpleNamespace
from dataclasses import asdict

import pytest
from eimemory.models.records import ScopeRef
from eimemory.governance.release.closure_blockers import lineage_blockers, closure_blockers
from eimemory.governance.release.closure_verdict import summarize_release_closure, failure_signals
from eimemory.ops.release_closure_failure import detect_release_closure_failure, record_release_closure_failure

SCOPE=dict(tenant_id='default',agent_id='a',workspace_id='w',user_id='u')

def failed_report():
    return {'ok':False,'report_type':'l5_release_closure','closure_complete':False,
        'data_accumulating':False,'blocked_stage':'closure_rehearsal',
        'blocked_reason':'release_lineage_not_compatible','scope':dict(SCOPE),
        'deployment':{'commit':'a'*40,'promotion_request_id':'receipt-a'},
        'deployment_receipt':{'release_session_id':'session-a'},
        'production_recall_gate':{'ok':True,'accepted':False,'gate_status':'diagnostic',
            'sample_count':10,'blocked_reason':'recall_quality_evidence_incomplete',
            'quality_gate':{'ok':False,'vacuous':True,'blocked_reason':'recall_quality_evidence_incomplete'}},
        'release_lineage':{'ok':True,'validated':True,'compatible':False,'domains':{
            'storage.integrity':{'mode':'changed_unverified',
                'gate_errors':{'live-case-old':'gate_not_after_current_receipt'}},
            'memory.recall':{'mode':'current','gate_errors':{}}}}}


def test_domain_error_is_not_hidden_by_recall_evidence_wait():
    report=failed_report();before=deepcopy(report)
    summary=summarize_release_closure(report)
    assert summary['report_contract_ok'] is True and summary['contract_error']==''
    assert summary['disposition']=='failure_detected'
    assert any(x['code']=='gate_not_after_current_receipt' for x in summary['failure_signals']['hard_errors'])
    item=summary['closure_blockers']['items'][0]
    assert item['domain']=='storage.integrity' and item['record_id']=='live-case-old'
    assert item['action']=='regenerate_evidence_after_current_receipt'
    assert summary['closure_blockers']['recall_evidence']['quality_accepted'] is False
    assert not summary['closure_certified'] and not summary['repair_complete']
    assert report==before


def test_incident_handoff_contains_specific_gate_not_only_aggregate_code():
    result=detect_release_closure_failure(failed_report(),detected_at='2026-09-28T10:00:00Z',scope=SCOPE)
    incident=result['incident']
    assert 'lineage:storage.integrity:gate_not_after_current_receipt' in incident['diagnostic_codes']
    assert 'live-case-old' in incident['summary']
    assert result['validation']['closure_blockers']['items'][0]['state']=='open'
    assert not result['repair_complete']


def test_registered_incident_retains_identical_report_fingerprint():
    class Store:
        def __init__(self):self.records={}
        def append(self,record,*,existing_match):
            key=(tuple(asdict(record.scope).values()),record.record_id)
            existing=self.records.get(key)
            if existing is not None:
                assert existing_match(existing)
                return existing
            self.records[key]=record
            return record
    store=Store();rt=SimpleNamespace(store=store);payload=failed_report()
    result=record_release_closure_failure(rt,scope=SCOPE,closure_report=payload,
                                         detected_at='2026-09-28T10:00:00Z')
    record=next(iter(store.records.values()))
    assert result['incident_record_id']==record.record_id
    assert record.content['detector_report']['report_digest']==summarize_release_closure(payload)['report_digest']
    assert record.content['detector_report']['validation']['closure_blockers']['items']
    assert record.content['repair_complete'] is False


def test_pure_recall_wait_never_becomes_quality_pass_or_code_repair():
    payload=failed_report();payload.pop('release_lineage')
    payload['blocked_stage']='production_recall_gate'
    payload['blocked_reason']='recall_quality_evidence_incomplete'
    summary=summarize_release_closure(payload)
    assert summary['disposition']=='evidence_waiting'
    assert not summary['closure_blockers']['recall_evidence']['quality_accepted']
    assert not summary['closure_certified']
    assert detect_release_closure_failure(payload,detected_at='now',scope=SCOPE)['incident'] is None


@pytest.mark.parametrize('reason', ['record_not_found','scope_mismatch',
    'current_release_recall_replay_incomplete','strict_code_evolution_receipt_required'])
def test_exact_domain_gate_reason_survives(reason):
    payload=failed_report()
    payload['release_lineage']['domains']['storage.integrity']['gate_errors']={'record-x':reason}
    work=closure_blockers(payload)['items'][0]
    assert work['reason_code']==reason and work['record_id']=='record-x'
    assert work['repair_complete'] is False


def test_blocker_errors_are_bounded_and_not_raw_exception_dumps():
    payload=failed_report()
    payload['release_lineage']['domains']['storage.integrity']['gate_errors']={
        f'record-{i}':'private token / unsafe exception body' for i in range(200)}
    result=closure_blockers(payload)
    assert len(result['items'])==64 and result['truncated'] is True
    assert 'private token' not in str(result)


def test_changed_domain_without_details_remains_an_open_blocker():
    result=lineage_blockers({'compatible':False,'domains':{
        'memory.governance':{'mode':'changed_unverified','gate_errors':{}}}})
    assert result['items'][0]['reason_code']=='lineage_domain_evidence_missing'


def test_aggregate_incompatibility_without_domains_is_not_sample_wait():
    result=lineage_blockers({'compatible':False})
    assert result['items'][0]['reason_code']=='lineage_failure_detail_missing'


def test_recording_failure_is_a_hard_control_error():
    report=failed_report();report['failure_recording']={'recording_ok':False,'status':'incident_recording_failed'}
    assert 'incident_recording_failed' in {x['code'] for x in failure_signals(report)['hard_errors']}


def test_domain_control_shape_is_checked():
    report=failed_report();report['release_lineage']['domains']=['invalid']
    assert 'lineage_domains_invalid' in {x['code'] for x in failure_signals(report)['hard_errors']}


def test_unreported_quality_is_unknown_not_an_invented_failure():
    report={'production_recall_gate':{'ok':True,'status':'accepted'}}
    result=closure_blockers(report)['recall_evidence']
    assert result['quality_accepted'] is None
    assert result['quality_assessment_status']=='not_reported'
    assert result['production_accepted'] is True


def test_quality_diagnostic_does_not_hide_explicit_errors():
    for key in ('error','errors','blocking_metrics'):
        report={'recall_quality_gate':{'ok':True,key:'failed'}}
        assert closure_blockers(report)['recall_evidence']['quality_accepted'] is False


def _non_recall_report(deficits):
    report=failed_report()
    report['blocked_reason']='bootstrap_pending_non_recall_l5_evidence_incomplete'
    report['release_lineage']={'ok':True,'validated':True,'compatible':True,'domains':{
        'memory.recall':{'mode':'current','gate_errors':{}}}}
    report['closure_rehearsal']={'ok':False,'bootstrap_pending_verification':{
        'ok':False,'status':'blocked',
        'reason':'bootstrap_pending_non_recall_l5_evidence_incomplete',
        'non_recall_evidence_deficits':deficits}}
    return report


def test_non_recall_deficits_are_surfaced_without_changing_diagnosis():
    report=_non_recall_report(['historical_verified_real_tasks_below_minimum',
                               'verified_real_replay_missing_or_failed'])
    before=deepcopy(report)
    summary=summarize_release_closure(report)
    block=summary['closure_blockers']['non_recall_evidence']
    assert block['status']=='incomplete'
    assert block['deficits']==['historical_verified_real_tasks_below_minimum',
                               'verified_real_replay_missing_or_failed']
    assert block['action']=='accumulate_verified_real_tasks_or_run_verified_real_replay'
    assert block['repair_complete'] is False
    # The diagnosis is not reclassified as a wait or a pass.
    assert summary['disposition']=='diagnosis_required'
    assert summary['business_closure_outcome']=='failed'
    assert not summary['closure_certified'] and not summary['data_accumulating']
    assert report==before


def test_non_recall_deficit_codes_are_sanitized_and_absence_is_not_reported():
    report=_non_recall_report(['ok_code','bad code with spaces\n'])
    block=closure_blockers(report)['non_recall_evidence']
    assert block['deficits']==['ok_code','non_recall_deficit_invalid']
    assert closure_blockers(failed_report())['non_recall_evidence']['status']=='not_reported'


def test_pre_closure_baseline_lineage_marks_missing_domain_evidence_as_awaiting_closure():
    baseline={'ok':True,'validated':True,'compatible':False,
        'gate_evidence':{'memory.recall':[],'channel.delivery':[]},
        'domains':{'memory.recall':{'mode':'changed_unverified','gate_errors':{}},
                   'channel.delivery':{'mode':'inherited','gate_errors':{}}}}
    result=lineage_blockers(baseline)
    assert result['phase']=='pre_closure_baseline'
    [item]=result['items']
    assert item['domain']=='memory.recall'
    assert item['reason_code']=='lineage_domain_evidence_missing'
    assert item['state']=='awaiting_release_closure'
    assert item['action']=='run_release_closure_gates'
    assert baseline['compatible'] is False  # admission unchanged


def test_gate_bound_lineage_keeps_missing_domain_evidence_open():
    bound={'ok':True,'validated':True,'compatible':False,
        'gate_evidence':{'memory.recall':['prbs-1'],'channel.delivery':[]},
        'domains':{'memory.recall':{'mode':'changed_unverified','gate_errors':{}}}}
    result=lineage_blockers(bound)
    assert result['phase']=='gate_evidence_bound'
    assert result['items'][0]['state']=='open'
    assert result['items'][0]['action']=='inspect_exact_gate_contract'
    legacy={'ok':True,'validated':True,'compatible':False,
        'domains':{'memory.recall':{'mode':'changed_unverified','gate_errors':{}}}}
    assert lineage_blockers(legacy)['items'][0]['state']=='open'


def test_pre_closure_baseline_never_softens_explicit_gate_errors():
    baseline={'ok':True,'validated':True,'compatible':False,
        'gate_evidence':{'code.evolution':[]},
        'domains':{'code.evolution':{'mode':'changed_unverified',
            'gate_errors':{'__contract__':'strict_code_evolution_receipt_required'}}}}
    [item]=lineage_blockers(baseline)['items']
    assert item['state']=='open'
    assert item['action']=='obtain_required_operator_authorization'
