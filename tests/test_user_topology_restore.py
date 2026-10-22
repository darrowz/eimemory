"""Manual topology is independent of immutable historical restore captures."""
from contextlib import nullcontext
from copy import deepcopy
from hashlib import sha256
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from eimemory.contracts import runtime_manager as c
from test_manual_recovery_capture import recovery
from test_user_release_recovery import installer


@pytest.mark.parametrize('fault',['missing_intent','partial_pair','receipt','intent_history','logical_history',
    'source_history','snapshot_history','retired_active','retired_enabled','retired_mainpid','retired_controlpid',
    'retired_cgroup','retired_link','nonretired_missing'])
def test_new_restore_never_mutates_native_units_without_a_complete_legal_plan(recovery,monkeypatch,fault):
    r=recovery;authority=r.bridge.registry_path.parent;unit=r.retired[0]
    old_job=r.bridge.record_path(r.prior['job_key']).read_bytes()
    if fault=='missing_intent':
        (authority/'hermes-topology-intent.v1.json').unlink();(authority/'hermes-topology.v1.json').unlink()
    elif fault=='partial_pair':(authority/'hermes-topology-intent.v1.json').unlink()
    elif fault=='receipt':(r.registration/'complete.json').write_bytes(b'{}')
    elif fault=='intent_history':(r.registration/'intent.original').write_bytes(b'{}')
    elif fault=='logical_history':(r.registration/'topology.original').write_bytes(b'{}')
    elif fault=='source_history':(r.registration/'job.original').write_bytes(b'{}')
    elif fault=='snapshot_history':(r.registration/'native-snapshot.original').write_bytes(b'{}')
    elif fault=='retired_active':r.states[unit].update(ActiveState='active')
    elif fault=='retired_enabled':r.states[unit].update(LoadState='loaded',UnitFileState='enabled')
    elif fault=='retired_mainpid':r.states[unit]['MainPID']='101'
    elif fault=='retired_controlpid':r.states[unit]['ControlPID']='102'
    elif fault=='retired_cgroup':r.states[unit]['ControlGroup']='/unexpected/live'
    elif fault=='retired_link':
        monkeypatch.setattr(r.bridge,'topology_links',lambda *_:(_ for _ in ()).throw(ValueError('retired_autostart_link')))
    elif fault=='nonretired_missing':r.record['registry']['units']['eimemory-rpc.service']['installed']=False;r.bridge.save(r.registry,r.record)
    with pytest.raises((ValueError,OSError)):
        r.bridge.transition(r.registry,r.record['job_key'],'quiesce',7,1001)
    assert not r.calls
    assert r.bridge.record_path(r.prior['job_key']).read_bytes()==old_job
    assert (r.bridge.state/'manual-recovery-capture.json').exists()


@pytest.mark.skipif(os.name!='posix',reason='native durable capture consumption')
@pytest.mark.parametrize('target_phase',['activate','rollback'])
def test_quiesce_activate_rollback_and_resume_share_one_sealed_plan(recovery,monkeypatch,target_phase):
    r=recovery;historical=deepcopy(r.capture)
    r.bridge.transition(r.registry,r.record['job_key'],'quiesce',7,1001)
    record=r.bridge.record(r.record['job_key']);plan=deepcopy(record['restore_plan'])
    assert record['active_before']==historical['active_before']
    assert all(unit in record['enabled_before'] for unit in r.retired)
    assert all(record['enabled_before'][unit]=='enabled' for unit in r.retired)
    assert not set(r.retired)&set(plan['active_units'])
    assert not set(r.retired)&set(plan['enabled_units'])
    monkeypatch.setattr(r.bridge,'publish_runtime',lambda *_a,**_kw:None)
    r.bridge.transition(r.registry,record['job_key'],target_phase,7,1001)
    r.bridge.resume(r.registry,record)
    proof=r.bridge.verify_restoration(r.registry,record)
    assert proof['schema']=='eimemory.native_runtime_reconciliation.v1'
    assert proof['historical_active_before']==historical['active_before']
    assert proof['autonomous_repair_credit'] is False
    assert record['restore_plan']==plan
    assert not any(unit in r.retired for unit,_ in r.calls)
    assert all(r.states[unit]['ActiveState']=='inactive' for unit in r.retired)
    assert r.prior==r.original


@pytest.mark.skipif(os.name!='posix',reason='native plan publication')
@pytest.mark.parametrize('fault',['active','enabled','retired','reference'])
def test_a_published_restore_plan_cannot_be_reinterpreted_after_quiesce(recovery,fault):
    r=recovery;r.bridge.transition(r.registry,r.record['job_key'],'quiesce',7,1001)
    record=r.bridge.record(r.record['job_key']);before=list(r.calls)
    if fault=='active':record['restore_plan']['active_units'].append(r.retired[0])
    elif fault=='enabled':record['restore_plan']['enabled_units'][r.retired[0]]='enabled'
    elif fault=='retired':record['restore_plan']['retired_units']=[]
    else:record['topology_intent']['sha256']='0'*64
    with pytest.raises(ValueError):r.bridge.resume(r.registry,record)
    assert r.calls==before


@pytest.mark.skipif(os.name!='posix',reason='native migration reconciliation transaction')
def test_completed_reconciliation_keeps_original_history_and_prevents_future_reinheritance(recovery,monkeypatch):
    r=recovery;migration=r.bridge.state/'migration-prior-active.json'
    raw=c.canonical(dict(active_before=r.prior['active_before'],enabled_before=r.prior['enabled_before'],restored=False))
    migration.write_bytes(raw)
    source_job=r.bridge.record_path(r.prior['job_key']).read_bytes()
    r.bridge.transition(r.registry,r.record['job_key'],'quiesce',7,1001)
    record=r.bridge.record(r.record['job_key']);r.bridge.resume(r.registry,record)
    record.update(resumed=True,phase='resumed_awaiting_business');r.bridge.save(r.registry,record)
    monkeypatch.setattr(r.bridge,'verify_completed_validation',lambda *_:None)
    monkeypatch.setattr(r.bridge,'cleanup_validation_units',lambda *_:None)
    r.bridge.complete(r.registry,record['job_key'],7,1001)
    assert migration.read_bytes()==raw and r.bridge.migration_reconciled(r.registry,raw)
    assert r.bridge.record_path(r.prior['job_key']).read_bytes()==source_job
    request={**record['request'],'reviewed_attempt_id':'9'*32};key=c.digest(request)
    next_record=dict(job_key=key,request=request,registry=deepcopy(r.registry),manual_release=True,
        unit=c.job_unit(key),active_before=None,quiesced=False,resumed=False,phase='submitted',original_invocation='8'*32)
    r.bridge.save(r.registry,next_record)
    r.bridge.transition(r.registry,key,'quiesce',8,1001)
    following=r.bridge.record(key)
    assert not set(r.retired)&set(following['active_before'])
    assert following['restore_plan']['retired_units']==r.retired
    assert not any(unit in r.retired for unit,_ in r.calls)
    assert migration.read_bytes()==raw


@pytest.mark.skipif(os.name!='posix',reason='native exact operator publication and crash recovery')
@pytest.mark.parametrize('point',['none','receipt','logical','intent','source_race'])
def test_operator_registration_has_a_durable_commit_point_and_never_changes_native_state(recovery,tmp_path,monkeypatch,point):
    r=recovery;module=installer();authority=r.bridge.registry_path.parent
    inputs=tmp_path/'reviewed';inputs.mkdir()
    paths={name:inputs/name for name in ['logical.json','intent.json','native.json']}
    paths['logical.json'].write_bytes((r.registration/'topology.original').read_bytes())
    paths['intent.json'].write_bytes((r.registration/'intent.original').read_bytes())
    paths['native.json'].write_bytes((r.registration/'native-snapshot.original').read_bytes()+b'\n')
    # The externally reviewed inspector output may include its normal newline.
    intent=c.strict_json(paths['intent.json'].read_bytes());intent['native_snapshot_sha256']=sha256(paths['native.json'].read_bytes()).hexdigest()
    paths['intent.json'].write_bytes(c.canonical(intent))
    (authority/'hermes-topology.v1.json').unlink();(authority/'hermes-topology-intent.v1.json').unlink()
    for file in r.registration.iterdir():file.unlink()
    args=SimpleNamespace(expected_registry_sha256=sha256(r.bridge.registry_path.read_bytes()).hexdigest(),
        logical_topology=paths['logical.json'],expected_logical_topology_sha256=sha256(paths['logical.json'].read_bytes()).hexdigest(),
        topology_intent=paths['intent.json'],expected_topology_intent_sha256=sha256(paths['intent.json'].read_bytes()).hexdigest(),
        native_snapshot=paths['native.json'])
    helper=SimpleNamespace(read_file=lambda path,**_:Path(path).read_bytes())
    environment={};account=SimpleNamespace(pw_uid=1001,pw_gid=1001)
    context=(account,authority,r.bridge.state,r.registry,r.bridge.registry_path.read_bytes(),helper,c,r.module,r.bridge,environment)
    monkeypatch.setattr(module,'topology_operator_context',lambda _:context)
    monkeypatch.setattr(module,'operator_lock',lambda *_:nullcontext())
    monkeypatch.setattr(module,'stopped_controller',lambda _:None)
    monkeypatch.setattr(module,'failed_attempt_native',lambda *_:deepcopy(r.capture['source_native_exit']))
    def directory(path,_uid,mode=0o750):path.mkdir(parents=True,exist_ok=True);path.chmod(mode)
    monkeypatch.setattr(module,'directory',directory)
    original_path=module.Path
    monkeypatch.setattr(module,'Path',lambda value:r.base if str(value)=='/opt/eimemory/current' else
        r.base.parent if str(value)=='/opt/eimemory/releases' else original_path(value))
    snapshots={path:path.read_bytes() for path in [r.bridge.record_path(r.prior['job_key']),
        r.bridge.registry_path,r.bridge.state/'manual-recovery-capture.json',r.bridge.state/'active-transition.json']}
    real_write=module.write;archive_write=module.archive_write;failed=False
    def publish(path,data,uid,mode=0o640):
        nonlocal failed
        result=real_write(path,data,uid,mode)
        if path==authority/'hermes-topology.v1.json' and point=='source_race':
            (r.bridge.state/'manual-recovery-capture.json').write_bytes(b'changed by another operator')
        if not failed and point in {'logical','intent'} and path==authority/('hermes-topology.v1.json' if point=='logical' else 'hermes-topology-intent.v1.json'):
            failed=True;raise OSError('injected registration publication crash')
        return result
    def archive(path,data,uid,*args):
        nonlocal failed
        result=archive_write(path,data,uid,*args)
        if not failed and point=='receipt' and path.name=='complete.json':
            failed=True;raise OSError('injected receipt publication crash')
        return result
    with monkeypatch.context() as crash:
        crash.setattr(module,'write',publish);crash.setattr(module,'archive_write',archive)
        if point=='none':module.register_topology(args)
        else:
            with pytest.raises((ValueError,OSError)):module.register_topology(args)
    if point!='source_race':
        module.register_topology(args)
        assert r.bridge.registered_topology(r.registry)[0]==intent
    else:
        assert not (authority/'hermes-topology-intent.v1.json').exists()
        with pytest.raises(ValueError):r.bridge.registered_topology(r.registry)
    for path,raw in snapshots.items():
        if point=='source_race' and path.name=='manual-recovery-capture.json':continue
        assert path.read_bytes()==raw
    assert r.calls==[]



def test_registered_operator_scope_is_bound_to_the_native_runtime_account(recovery):
    r=recovery;path=r.bridge.registry_path.parent/'hermes-topology.v1.json'
    logical=c.strict_json(path.read_bytes());logical['profiles'][0]['operator_scope']=dict(
        tenant_id='fixture',agent_id='fixture',workspace_id='fixture',user_id='foreign-owner')
    r.publish(path,logical)
    with pytest.raises(ValueError,match='operator_identity_invalid'):
        r.bridge.registered_topology(r.registry)
    assert r.calls==[]
