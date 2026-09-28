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
