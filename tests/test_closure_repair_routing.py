"""Exercise the real router with isolated provider/policy/ledger boundaries."""
from copy import deepcopy
import importlib
import json
import sqlite3
from types import SimpleNamespace
import pytest

from eimemory.governance.evolution import system_code_repair as router
from eimemory.governance.evolution.code_evolution_test_plans import (
    RELEASE_REPORT_FAILURE_TEST_PLAN, RELEASE_REPORT_FAILURE_TEST_PLAN_ID,
    RELEASE_CLOSURE_FAILURE_TEST_PLAN, allowed_files_for_incident,
    protected_test_plan_command_error,
)
from eimemory.models.records import RecordEnvelope, ScopeRef
from eimemory.ops.release_closure_failure import record_release_closure_failure
from test_closure_capture_pipeline import SQLiteFixtureStore,SCOPE
from test_closure_pipeline_contract import blocked

class Store(SQLiteFixtureStore):
    def list_records(self,*,kinds,scope,limit):
        with sqlite3.connect(self.path) as c:
            records=[RecordEnvelope.from_dict(json.loads(row[0])) for row in c.execute('SELECT payload FROM records')]
        # Deliberately return shared scopes; the router must filter exactly.
        return [r for r in records if r.kind in kinds][:limit]

@pytest.fixture
def setup_router(tmp_path,monkeypatch):
    runtime=SimpleNamespace(store=Store(tmp_path/'db.sqlite'),run_autonomous_evolution=lambda **kw:{'ok':True})
    report=blocked('release_closure_report_contract_invalid')
    registered=record_release_closure_failure(runtime,scope=SCOPE,closure_report=report,detected_at='now')
    record=runtime.store.list_records(kinds=['incident'],scope=ScopeRef.from_dict(SCOPE),limit=100)[0]
    proposals=[]
    def propose(*a,**kw):proposals.append(kw);return {'ok':True}
    monkeypatch.setattr(router,'_repository_identity',lambda root:{'ok':True,'base_commit':'a'*40})
    monkeypatch.setattr(router,'_automation_policy_identity',lambda:(registered['incident']['incident_digest'],'b'*64,'profile'))
    monkeypatch.setattr(router,'protected_paths_digest',lambda *a:'c'*64)
    class Ledger:
        def __init__(self,store):pass
        def get_policy_consumption(self,digest):return None
        def get_transaction(self,txid):return None
    monkeypatch.setattr(router,'CodeEvolutionStore',Ledger)
    evidence=importlib.import_module('eimemory.governance.release.evidence_contract')
    monkeypatch.setattr(evidence,'current_release_identity',lambda *a:SimpleNamespace(commit='a'*40))
    bridge=importlib.import_module('eimemory.governance.evolution.code_evolution_bridge')
    monkeypatch.setattr(bridge,'propose_code_patch_v2',propose)
    profiles=importlib.import_module('eimemory.capabilities.profiles')
    class Profiles:
        def __init__(self,store):pass
        def resolve(self,*a,**kw):return {}
    monkeypatch.setattr(profiles,'CapabilityProfiles',Profiles)
    return runtime,record,proposals,tmp_path,bridge


def test_report_fault_has_separate_narrow_plan():
    assert RELEASE_REPORT_FAILURE_TEST_PLAN.allowed_files==('eimemory/governance/release/closure_verdict.py',)
    assert not RELEASE_REPORT_FAILURE_TEST_PLAN.allowed_path_globs
    assert RELEASE_REPORT_FAILURE_TEST_PLAN.full_suite_required is True
    assert allowed_files_for_incident('release.closure_report_failure')==RELEASE_REPORT_FAILURE_TEST_PLAN.allowed_files
    assert RELEASE_CLOSURE_FAILURE_TEST_PLAN.allowed_files==('eimemory/governance/release/release_closure_gate_evidence.py',)
    assert not allowed_files_for_incident('release.closure_report_failure',test_plan_id=RELEASE_CLOSURE_FAILURE_TEST_PLAN.plan_id)
    argv=RELEASE_REPORT_FAILURE_TEST_PLAN.argv('focused',candidate_python='/opt/venv/bin/python')
    assert protected_test_plan_command_error([argv],plan_id=RELEASE_REPORT_FAILURE_TEST_PLAN_ID,candidate_python='/opt/venv/bin/python')==''
    assert protected_test_plan_command_error([argv+['-k','nothing']],plan_id=RELEASE_REPORT_FAILURE_TEST_PLAN_ID,candidate_python='/opt/venv/bin/python')


def test_successful_handoff_is_linked_not_complete(setup_router):
    runtime,record,proposals,path,_=setup_router
    result=router.process_system_code_incidents(runtime,scope=SCOPE,repo_root=path)
    assert result['ok'] is True and not result['repair_complete']
    row=result['processed'][0]
    assert row['status']=='submitted' and row['incident_record_id']==record.record_id
    assert row['transaction_id'] and row['attempt_record_id'] and not row['repair_complete']
    assert proposals[0]['test_plan_id']==RELEASE_REPORT_FAILURE_TEST_PLAN_ID
    assert proposals[0]['allowed_files']==RELEASE_REPORT_FAILURE_TEST_PLAN.allowed_files
    assert record.status=='active'


@pytest.mark.parametrize('mode',['provider','provider_exception','evolution_false','evolution_exception'])
def test_failure_never_becomes_processed_success(setup_router,monkeypatch,mode):
    runtime,record,_,path,bridge=setup_router
    if mode=='provider':monkeypatch.setattr(bridge,'propose_code_patch_v2',lambda *a,**kw:{'ok':False,'reason':'provider_unavailable'})
    elif mode=='provider_exception':
        def bad_provider(*a,**kw):raise RuntimeError('private-provider-detail')
        monkeypatch.setattr(bridge,'propose_code_patch_v2',bad_provider)
    elif mode=='evolution_false':runtime.run_autonomous_evolution=lambda **kw:{'ok':False,'reason':'policy_denied'}
    else:
        def fail(**kw):raise RuntimeError('private-exception-detail')
        runtime.run_autonomous_evolution=fail
    result=router.process_system_code_incidents(runtime,scope=SCOPE,repo_root=path)
    assert result['ok'] is False and result['status']=='blocked'
    assert result['processed'][0]['attempt_record_id']
    assert result['processed'][0]['reason']
    assert not result['repair_complete']


def test_pending_incident_policy_mismatch_not_idle(setup_router,monkeypatch):
    runtime,_,proposals,path,_=setup_router
    monkeypatch.setattr(router,'_automation_policy_identity',lambda:('0'*64,'b'*64,'profile'))
    result=router.process_system_code_incidents(runtime,scope=SCOPE,repo_root=path)
    assert not result['ok'] and result['reason']=='incident_policy_digest_mismatch'
    assert result['pending_incident_record_ids'] and not proposals


def test_wrong_scope_not_routed(setup_router):
    runtime,_,proposals,path,_=setup_router
    result=router.process_system_code_incidents(runtime,scope={**SCOPE,'user_id':'other'},repo_root=path)
    assert result['status']=='idle' and not proposals


def test_diagnosis_cannot_authorize_proposal(setup_router,monkeypatch):
    runtime,record,proposals,path,_=setup_router
    with sqlite3.connect(runtime.store.path) as c:c.execute('DELETE FROM records')
    registered=record_release_closure_failure(runtime,scope=SCOPE,closure_report=blocked(),detected_at='now')
    monkeypatch.setattr(router,'_automation_policy_identity',lambda:(registered['incident']['incident_digest'],'b'*64,'profile'))
    result=router.process_system_code_incidents(runtime,scope=SCOPE,repo_root=path)
    assert result['status']=='diagnosis_required' and not proposals
    assert result['pending_incident_record_ids']


@pytest.mark.parametrize('mode',['scope','digest','status','class'])
def test_identity_tampering_rejected(setup_router,mode):
    _,record,_,_,_=setup_router
    assert router._trusted_incident(record,'a'*40) is not None
    record=deepcopy(record)
    if mode=='scope':record.scope=ScopeRef.from_dict({**SCOPE,'user_id':'other'})
    elif mode=='digest':record.content['detector_report']['identity']['receipt_id']='replacement'
    elif mode=='status':record.content['detector_report']['status']='evidence_waiting'
    else:record.content['incident_class']='release.closure_internal_failure'
    assert router._trusted_incident(record,'a'*40) is None
