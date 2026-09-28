"""Focused consumer/writer contracts. External authority is explicitly substituted.

The integration companion tests exercise the full RuntimeStore separately.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict
from types import SimpleNamespace
import sqlite3

import pytest
from eimemory.models.records import RecordEnvelope, ScopeRef
from eimemory.governance.l5 import live_task_acceptance as live
from eimemory.governance.release import release_lineage as lineage
from eimemory.governance.release import release_closure as closure
from eimemory.ops import release_closure_failure as incidents

IDENTITY = dict(commit='a'*40, version='1.14.4', release_path='/release/'+'a'*40,
                promotion_request_id='receipt-a',release_session_id='session-a')
SCOPE = ScopeRef(tenant_id='t',agent_id='a',workspace_id='w',user_id='u')

@pytest.fixture
def live_boundary(monkeypatch):
    runtime=SimpleNamespace(records={}, traces={}, current=deepcopy(IDENTITY), poison=None)
    def append(rt, **kw):
        # Model the EXISTING shared writer's release-bound idempotency, not a
        # bare semantic-key lookup. The regression concerns trace identity.
        key=(kw['semantic_key'],tuple(asdict(kw['scope']).values()),
             rt.current['promotion_request_id'],rt.current['release_session_id'])
        if rt.poison is not None:
            return rt.poison
        if key not in rt.records:
            kw=dict(kw);kw['content']=deepcopy(kw['content'])
            kw['content']['release_session_id']=rt.current['release_session_id']
            rt.records[key]=RecordEnvelope.create(**{k:v for k,v in kw.items()
                if k not in {'loop_id','step_name','semantic_key','authority_tier'}})
        return rt.records[key]
    def valid(rt, *, evidence,scope,identity,trace_id,passed,**kw):
        p=evidence.content
        return (evidence.scope==scope and evidence.status=='active'
            and evidence.source=='eimemory.live_task_acceptance'
            and p['promotion_request_id']==identity['promotion_request_id']
            and p['release_session_id']==identity['release_session_id']
            and p['deployment_commit']==identity['commit']
            and p['release_path']==identity['release_path']
            and p['trace_id']==trace_id and p['passed'] is passed)
    def trace(rt, *,scope,case_record):
        rt.traces.setdefault(case_record.content['trace_id'],case_record)
        return {'ok':True}
    monkeypatch.setattr(live,'append_learning_record_once',append)
    monkeypatch.setattr(live,'validate_live_acceptance_case',valid)
    monkeypatch.setattr(live,'_record_case_outcome',trace)
    return runtime


def execute(runtime, identity=None, scope=SCOPE, check=None):
    return live._execute_and_record_case(runtime,scope=scope,identity=identity or runtime.current,
        definition={'case_id':'store.sqlite_query','task_type':'live.acceptance.store.sqlite_query',
                    'check':check or (lambda:{'passed':True,'query_ok':True})})


@pytest.mark.parametrize('field', ['promotion_request_id','release_session_id'])
def test_live_trace_is_bound_to_receipt_and_session(live_boundary, field):
    first=execute(live_boundary)
    live_boundary.current[field]='another-authority'
    second=execute(live_boundary)
    # Records already differed in the original shared writer; traces did not.
    assert first['record_id'] != second['record_id']
    assert first['trace_id'] != second['trace_id']
    assert first['observation_digest'] != second['observation_digest']
    assert len(live_boundary.traces)==2


def test_live_same_authority_retry_is_idempotent(live_boundary):
    first=execute(live_boundary);second=execute(live_boundary)
    assert first['record_id']==second['record_id']
    assert first['trace_id']==second['trace_id']
    assert len(live_boundary.records)==len(live_boundary.traces)==1


def test_live_version_metadata_does_not_change_trace_authority(live_boundary):
    first=execute(live_boundary)
    live_boundary.current['version']='display-metadata-only'
    second=execute(live_boundary)
    assert first['trace_id']==second['trace_id']


def test_live_trace_scope_is_part_of_observation_identity(live_boundary):
    first=execute(live_boundary)
    scope=ScopeRef(tenant_id='t',agent_id='a',workspace_id='w',user_id='other')
    second=execute(live_boundary,scope=scope)
    assert first['trace_id'] != second['trace_id']


def test_live_rejects_a_stale_persisted_case_before_emitting_trace(live_boundary):
    execute(live_boundary)
    live_boundary.poison=next(iter(live_boundary.records.values()))
    live_boundary.current['release_session_id']='new-session'
    live_boundary.traces.clear()
    result=execute(live_boundary)
    assert result['passed'] is False and result['trace_persisted'] is False
    assert result['error']=='persisted_live_case_identity_mismatch'
    assert not live_boundary.traces


def test_live_identity_change_during_persistence_is_fail_closed(live_boundary):
    requested=deepcopy(IDENTITY)
    live_boundary.current['release_session_id']='racing-session'
    result=execute(live_boundary,identity=requested)
    assert result['passed'] is False and not live_boundary.traces


def test_live_failed_probe_does_not_become_pass(live_boundary):
    def broken():raise RuntimeError('probe failed')
    result=execute(live_boundary,check=broken)
    assert result['passed'] is False


def domain_boundary(monkeypatch, tmp_path, *, mode='inherited', receipt=True,
                    old_digest='same', new_digest='same', correct_identity=True):
    old=SimpleNamespace(commit='b'*40,receipt_id='old',session_id='old-session')
    current=SimpleNamespace(commit='a'*40,receipt_id='new',session_id='new-session')
    evidence=old if mode=='inherited' or not correct_identity else current
    report={'ok':True,'validated':True,'schema_version':'release_lineage.v1',
        'record_id':'lineage-record','current_release':current,'domains':{
        'storage.integrity':{'mode':mode,'evidence_release':evidence}}}
    monkeypatch.setattr(lineage,'current_release_lineage',lambda *a,**kw:report)
    monkeypatch.setattr(lineage,'_identity_from_payload',lambda value:value)
    monkeypatch.setattr(lineage,'same_release_authority',lambda a,b:a==b)
    monkeypatch.setattr(lineage,'_scope_ref',lambda value:value)
    monkeypatch.setattr(lineage,'_receipt_identity',lambda *a:old if receipt else None)
    monkeypatch.setattr(lineage,'_domain_digest',lambda repo,commit,paths:
                        old_digest if commit==old.commit else new_digest)
    monkeypatch.setattr(lineage,'_ancestor_distances',lambda *a:{old.commit:1})
    return lambda **kw:lineage.evidence_release_for_domain(object(),scope=SCOPE,
        repo_root=tmp_path,domain='storage.integrity',current_release=current,**kw),old,current,report


@pytest.mark.parametrize(('old_digest','new_digest','receipt'), [
    (None,None,True), (None,'x',True), ('x',None,True), ('old','new',True), ('same','same',False)])
def test_inherited_domain_requires_readable_equal_bytes_and_receipt(monkeypatch,tmp_path,
                                                                old_digest,new_digest,receipt):
    call,*_=domain_boundary(monkeypatch,tmp_path,old_digest=old_digest,new_digest=new_digest,receipt=receipt)
    with pytest.raises(ValueError):call()


def test_inherited_domain_accepts_unchanged_attested_bytes(monkeypatch,tmp_path):
    call,old,*_=domain_boundary(monkeypatch,tmp_path)
    assert call()==old


def test_current_domain_does_not_enter_inherited_reverification(monkeypatch,tmp_path):
    call,_,current,_=domain_boundary(monkeypatch,tmp_path,mode='current')
    monkeypatch.setattr(lineage,'_domain_digest',lambda *a:(_ for _ in ()).throw(AssertionError('wrong branch')))
    assert call()==current


def test_current_domain_rejects_wrong_authority(monkeypatch,tmp_path):
    call,*_=domain_boundary(monkeypatch,tmp_path,mode='current',correct_identity=False)
    with pytest.raises(ValueError):call()


def test_lineage_consumer_enforces_requested_record_id(monkeypatch,tmp_path):
    call,*_=domain_boundary(monkeypatch,tmp_path)
    with pytest.raises(ValueError):call(expected_record_id='another-record')


def report():
    return {'ok':False,'report_type':'l5_release_closure','closure_complete':False,
            'data_accumulating':False,'blocked_stage':'closure_rehearsal',
            'blocked_reason':'release_lineage_not_compatible'}


def test_internal_recorder_exposes_incident_without_changing_gate(monkeypatch):
    monkeypatch.setattr(incidents,'record_release_closure_failure',lambda *a,**k:
        {'recording_ok':True,'status':'failure_detected','incident_record_id':'incident-1'})
    payload=report()
    closure._record_self_repair_incident(SimpleNamespace(store=object()),scope=asdict(SCOPE),report=payload)
    assert payload['failure_recording']['recording_ok'] is True
    assert payload['failure_recording']['incident_record_id']=='incident-1'
    assert payload['ok'] is False and payload['blocked_reason']=='release_lineage_not_compatible'
    assert payload['failure_recording']['repair_complete'] is False


@pytest.mark.parametrize('error', [OSError('private detail'),sqlite3.OperationalError('database locked')])
def test_internal_recorder_failure_never_masks_original_gate(monkeypatch,error):
    def broken(*a,**kw):raise error
    monkeypatch.setattr(incidents,'record_release_closure_failure',broken)
    payload=report()
    closure._record_self_repair_incident(SimpleNamespace(store=object()),scope=asdict(SCOPE),report=payload)
    assert payload['failure_recording']['recording_ok'] is False
    assert payload['failure_recording']['exception_type']==type(error).__name__
    assert 'private detail' not in str(payload)
    assert payload['blocked_reason']=='release_lineage_not_compatible'


def test_internal_recorder_empty_id_is_not_registered(monkeypatch):
    monkeypatch.setattr(incidents,'record_release_closure_failure',lambda *a,**k:
        {'recording_ok':True,'status':'failure_detected','incident_record_id':''})
    payload=report()
    closure._record_self_repair_incident(SimpleNamespace(store=object()),scope=asdict(SCOPE),report=payload)
    assert payload['failure_recording']['recording_ok'] is False
