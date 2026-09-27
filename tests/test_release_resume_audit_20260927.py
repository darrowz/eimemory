"""Actual closure orchestration with mocked external services; no live certification."""
from __future__ import annotations
from copy import deepcopy
import pytest

from eimemory.governance.release import release_closure as closure
from eimemory.governance.release import release_closure_pending as pending
from eimemory.governance.release.evidence_contract import ReleaseIdentity
from test_release_governance_audit_20260927 import acceptance, packs_for, receipt, live_report, checkpoint


class RuntimeStages:
    def __init__(self, *, fail='', channel_missing=False, drift=''):
        self.calls=[];self.fail=fail;self.channel_missing=channel_missing;self.drift=drift
    def verify_and_record_deployment(self,**kw):
        self.calls.append('deployment')
        r=receipt()
        if self.drift:r[self.drift]='b'*40 if self.drift=='commit' else 'different'
        return r
    def run_configured_production_recall_gate(self,**kw):
        self.calls.append('recall')
        return {'ok':self.fail!='recall','accepted':self.fail!='recall','error':'recall_failed' if self.fail=='recall' else ''}
    def verify_production_recall_gate(self,**kw):
        self.calls.append('verify_recall');return {'ok':True,'status':'accepted','record_id':'recall-1'}
    def activate_production_recall_strict_state(self,**kw):
        self.calls.append('strict');return {'ok':True,'status':'strict_activated','record_id':'strict-1','candidate_commit':'a'*40}
    def run_weak_capability_replay_gate(self,**kw):
        self.calls.append('replay')
        a=acceptance()
        return {'ok':self.fail!='replay','legacy_compatibility':True,
                'blocked_reasons':['capability_acceptance_failed'] if self.fail=='replay' else [],
                'capability_acceptance':a,'capability_replay':packs_for(a)}
    def run_live_task_acceptance(self,**kw):
        self.calls.append('live');r=live_report()
        if self.fail=='live':r['cases'][0]['passed']=False
        return r
    def record_external_channel_acceptance(self,**kw):
        self.calls.append('channel')
        return {'ok':False,'error':'current_release_channel_receipt_not_found'} if self.channel_missing else {
            'ok':True,'evidence_class':'external_channel_receipt','record_id':'channel-1'}


@pytest.fixture
def setup_stages(monkeypatch):
    monkeypatch.delenv('EIMEMORY_CODE_EVOLUTION_TRANSACTION_MODE',raising=False)
    import eimemory.governance.safety.prompt_safety_executor as safety
    import eimemory.governance.l5.l5_readiness as readiness
    monkeypatch.setattr(safety,'bind_prompt_safety_from_service',lambda runtime:None)
    monkeypatch.setattr(readiness,'_storage_migration_status',lambda runtime:{'ok':True})
    def continue_stage(runtime,**kwargs):
        runtime.calls.append('continue')
        return {**kwargs['report'],'continuation_reached':True}
    monkeypatch.setattr(closure,'_continue_release_closure',continue_stage)
    # Recursively entering checkpoint housekeeping while reconciling is a bug.
    def forbidden(**kwargs):raise AssertionError('recursive checkpoint lock')
    monkeypatch.setattr(pending,'supersede_release_closure_pending',forbidden)


def resume(runtime,tmp_path):
    path=tmp_path/'pending.json';data=checkpoint(path)
    current=ReleaseIdentity(commit='a'*40,version='1.14.3',receipt_id='receipt-1',session_id='session-1')
    return closure.resume_release_closure(runtime,checkpoint=data,current_release=current,
                                          channel_acceptance={'ok':True,'record_id':'stale-channel'})


def test_resume_reruns_gates_instead_of_trusting_cached_success(setup_stages,tmp_path):
    runtime=RuntimeStages();report=resume(runtime,tmp_path)
    assert runtime.calls==['deployment','recall','verify_recall','strict','replay','live','channel','continue']
    assert report['deployment_receipt']['release_session_id']=='session-1'
    assert report['production_recall_gate']['status']=='accepted'
    assert report['channel_acceptance']['record_id']=='channel-1'
    assert report.get('closure_complete') is not True # continuation mocked, not certified


@pytest.mark.parametrize('failure,stage',[('recall','production_recall_gate'),('replay','replay_bootstrap'),('live','live_acceptance')])
def test_resume_failure_stops_before_later_gates(setup_stages,tmp_path,failure,stage):
    runtime=RuntimeStages(fail=failure);report=resume(runtime,tmp_path)
    assert report['blocked_stage']==stage and report['ok'] is False and report['closure_complete'] is False
    assert 'continue' not in runtime.calls
    if failure=='replay':assert 'live' not in runtime.calls


@pytest.mark.parametrize('key',['commit','promotion_request_id','release_session_id'])
def test_resume_rechecks_current_receipt_identity(setup_stages,tmp_path,key):
    runtime=RuntimeStages(drift=key);report=resume(runtime,tmp_path)
    assert report['blocked_reason']=='pending_release_authority_mismatch'
    assert runtime.calls==['deployment']


def test_channel_disappeared_during_resume_does_not_recurse_or_clear_checkpoint(setup_stages,tmp_path):
    path=tmp_path/'pending.json';pending.write_release_closure_pending(checkpoint(path),path=path)
    before=path.read_bytes()
    with pending._release_closure_reconcile_lock(path):
        runtime=RuntimeStages(channel_missing=True);report=resume(runtime,tmp_path)
    assert report['blocked_stage']=='channel_acceptance'
    assert report['closure_complete'] is False
    assert path.read_bytes()==before and 'continue' not in runtime.calls


def test_legacy_checkpoint_cannot_enter_strict_transaction(setup_stages,tmp_path,monkeypatch):
    monkeypatch.setenv('EIMEMORY_CODE_EVOLUTION_TRANSACTION_MODE','1')
    runtime=RuntimeStages();report=resume(runtime,tmp_path)
    assert report['ok'] is False and report['blocked_reason']=='checkpoint_resume_in_strict_transaction'
    assert not runtime.calls
