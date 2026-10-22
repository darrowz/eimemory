"""Fixed callers hold background work through business admission, including faults."""
from pathlib import Path
from types import SimpleNamespace

import pytest

from deploy import run_code_deployment_gate as manual
from eimemory.core import runtime_manager as core
from eimemory.governance.evolution import code_deployment_job as jobs


KEY='a'*64


def ready():
    return dict(job_key=KEY,phase='serving_awaiting_business',core_serving_ready=True,
        background_held=True,resumed=False)


def restored():
    return dict(job_key=KEY,phase='resumed_awaiting_business',background_held=False,resumed=True)


@pytest.mark.parametrize('fault',['','business_failed','business_exception','prepare_lost','prepare_invalid',
    'restore_lost','restore_invalid','capture_failed','complete_lost','both_exceptions'])
def test_manual_business_child_always_precedes_background_restore_and_completion(monkeypatch,capsys,fault):
    calls=[]
    def prepare(key):
        assert key==KEY;calls.append('core')
        if fault=='prepare_lost':raise TimeoutError('prepare_reply_lost')
        return {**ready(),'resumed':True} if fault=='prepare_invalid' else ready()
    report=dict(ok=fault!='business_failed',business_qualified=fault!='business_failed',
        l5_certification='not_certified',record_ids={'controlled_production_acceptance':'cpa_original'})
    def business(key,operation,**kwargs):
        assert key==KEY and operation=='post_resume' and calls==['core']
        calls.append('business')
        if fault in {'business_exception','both_exceptions'}:raise LookupError('original_business_transport_failure')
        return report,dict(exit_status=1 if fault=='business_failed' else 0,target_transport={
            'closure_capture':dict(capture_saved=fault!='capture_failed',recording_ok=True,exit_code=0)})
    def restore(key):
        assert key==KEY;calls.append('background')
        if fault in {'restore_lost','both_exceptions'}:raise TimeoutError('restore_reply_lost')
        return {**restored(),'background_held':True} if fault=='restore_invalid' else restored()
    def complete(key):
        assert key==KEY and calls==['core','business','background'];calls.append('complete')
        if fault=='complete_lost':raise TimeoutError('completion_reply_lost')
        return dict(job_key=KEY,phase='completed',resumed=True,complete=True,operational_complete=True)
    monkeypatch.setattr(core,'prepare_deployment_business',prepare)
    monkeypatch.setattr(core,'restore_deployment_background',restore)
    monkeypatch.setattr(core,'complete_deployment_job',complete)
    monkeypatch.setattr(jobs,'managed_validation_report',business)
    if not fault:assert manual._post_resume_gate(KEY)==0
    elif fault=='business_failed':
        assert manual._post_resume_gate(KEY)==2
        assert 'cpa_original' in capsys.readouterr().out
    else:
        expected=LookupError if fault in {'business_exception','both_exceptions'} else (TimeoutError
            if fault in {'prepare_lost','restore_lost','complete_lost'} else ValueError)
        with pytest.raises(expected) as captured:manual._post_resume_gate(KEY)
        if fault=='both_exceptions':assert any('TimeoutError' in note for note in captured.value.__notes__)
    assert 'background' in calls
    assert ('business' in calls) is (fault not in {'prepare_lost','prepare_invalid'})
    assert ('complete' in calls) is (fault in {'','complete_lost'})


@pytest.mark.parametrize('fault',['','business_failed','business_exception','prepare_lost','prepare_invalid',
    'restore_lost','restore_invalid','both_exceptions','lease_failed'])
def test_automatic_admission_retains_original_business_and_restores_on_prepare_or_child_error(tmp_path,monkeypatch,fault):
    root=tmp_path/'jobs';jobs._directory(root/KEY,create=True);monkeypatch.setattr(jobs,'JOBS_ROOT',root)
    calls=[]
    def renew(_):
        if fault=='lease_failed':raise LookupError('lease_unavailable')
    manager=SimpleNamespace(renew_lease=renew)
    def prepare(key):
        assert key==KEY;calls.append('core')
        if fault=='prepare_lost':raise TimeoutError('prepare_reply_lost')
        return {**ready(),'background_held':False} if fault=='prepare_invalid' else ready()
    report=dict(ok=fault!='business_failed',business_qualified=fault!='business_failed',
        record_ids={'controlled_production_acceptance':'cpa_original'})
    def business(*args,**kwargs):
        assert calls==['core'] and not (root/KEY/'finalized.json').exists()
        assert jobs._read(root/KEY/'serving-ready.json')['native']==ready()
        calls.append('business')
        if fault in {'business_exception','both_exceptions'}:raise LookupError('original_business_transport_failure')
        return report
    def restore(key):
        assert key==KEY;calls.append('background')
        if fault in {'restore_lost','both_exceptions'}:raise TimeoutError('restore_reply_lost')
        return {**restored(),'background_held':True} if fault=='restore_invalid' else restored()
    monkeypatch.setattr(core,'prepare_deployment_business',prepare)
    monkeypatch.setattr(core,'restore_deployment_background',restore)
    invoke=lambda:jobs._managed_business_admission(object(),adapter=SimpleNamespace(run_post_resume_business=business),
        transaction={'transaction_id':'tx'},policy={},key=KEY,invocation='b'*32,manager=manager)
    if fault in {'','business_failed'}:assert invoke()==report
    else:
        expected=LookupError if fault in {'business_exception','both_exceptions','lease_failed'} else (
            TimeoutError if fault in {'prepare_lost','restore_lost'} else ValueError)
        with pytest.raises(expected) as captured:invoke()
        if fault=='both_exceptions':assert any('TimeoutError' in note for note in captured.value.__notes__)
    assert calls[-1]=='background'
    assert ('business' in calls) is (fault not in {'prepare_lost','prepare_invalid','lease_failed'})
    assert (root/KEY/'post-resume-business.json').exists() is (fault in {'','business_failed','restore_lost','restore_invalid'})
    if (root/KEY/'post-resume-business.json').exists():assert jobs._read(root/KEY/'post-resume-business.json')==report
    assert (root/KEY/'finalized.json').exists() is (fault not in {'restore_lost','restore_invalid','both_exceptions'})


def test_two_phase_native_api_uses_only_fixed_operations_and_exact_key(monkeypatch):
    seen=[]
    monkeypatch.setattr(core,'request',lambda operation,**kwargs:seen.append((operation,kwargs)) or {})
    core.prepare_deployment_business(KEY);core.restore_deployment_background(KEY)
    assert seen==[('prepare_business_job',{'job_key':KEY}),('restore_background_job',{'job_key':KEY})]
    for entry in (core.prepare_deployment_business,core.restore_deployment_background):
        with pytest.raises(ValueError):entry('foreign/path')
    assert len(seen)==2
