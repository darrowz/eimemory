from pathlib import Path
from types import SimpleNamespace

import pytest

from eimemory.contracts import runtime_manager as c
from test_runtime_manager import standalone
from test_runtime_manager_handoff import module


@pytest.mark.parametrize('unit', ['openclaw-loop-watch.timer', 'openclaw-loop-compact.timer',
    'openclaw-loop-watch.service', 'openclaw-loop-compact.service'])
def test_optional_adapter_absence_is_native_read_only_user_observation(monkeypatch, unit):
    bridge=standalone('runtime_manager_bridge').Bridge(c)
    calls=[]
    monkeypatch.setattr(bridge,'command',lambda args:calls.append(args) or 'LoadState=not-found\nActiveState=inactive\n')
    registry={'runtime_uid':1001,'runtime_user':'darrow','units':{}}
    result=bridge.unit_state(registry,unit,['LoadState','ActiveState','UnitFileState'])
    assert result=={'LoadState':'not-found','ActiveState':'inactive','UnitFileState':''}
    assert calls[0][-5:]==['--user','show','--no-pager','--property=LoadState,ActiveState,UnitFileState',unit]
    with pytest.raises(ValueError):bridge.systemctl(registry,unit,'start')
    with pytest.raises(ValueError):bridge.unit_state(registry,unit,['Environment'])
    assert len(calls)==1


def test_optional_adapter_query_failure_remains_failure(monkeypatch):
    bridge=standalone('runtime_manager_bridge').Bridge(c)
    def denied(_):raise ValueError('manager_command_failed')
    monkeypatch.setattr(bridge,'command',denied)
    with pytest.raises(ValueError,match='manager_command_failed'):
        bridge.unit_state({'runtime_uid':1001,'runtime_user':'darrow'},'openclaw-loop-watch.timer',['LoadState'])


@pytest.mark.parametrize('uid,euid',[(1001,1001),(0,1001),(1001,0)])
def test_shell_repository_coordinate_is_root_registered_and_runtime_uid_only(tmp_path,monkeypatch,uid,euid):
    launcher=standalone('runtime_manager_launch')
    registry={'runtime_uid':1001,'repository_root':str(tmp_path)}
    monkeypatch.setattr(launcher,'read_root',lambda _:registry)
    monkeypatch.setattr(launcher,'root_contract',lambda:SimpleNamespace(validate_registry=lambda value:value))
    monkeypatch.setattr(launcher.os,'getuid',lambda:uid,raising=False)
    monkeypatch.setattr(launcher.os,'geteuid',lambda:euid,raising=False)
    monkeypatch.setenv('EIMEMORY_TRUSTED_REPOSITORY_ROOT','/unreviewed/override')
    if (uid,euid)==(1001,1001):assert launcher.repository_root()==str(tmp_path)
    else:
        with pytest.raises(ValueError,match='runtime_uid_required'):launcher.repository_root()


@pytest.mark.parametrize('fault',['none','scope','target','automatic','unresumed','phase','running','success','legacy','drift','linked'])
def test_current_release_revalidation_preserves_failed_parent(tmp_path,monkeypatch,fault):
    bootstrap=module();commit='b'*40
    scope=dict(tenant_id='t',agent_id='a',workspace_id='w',user_id='u')
    request=dict(schema='eimemory.manual_release_permit.v2',operation='deploy',
        base_commit='a'*40,candidate_commit=commit,target_commit=commit,
        controller_commit='a'*40,validation_controller_commit=commit,scope=scope,reviewed_attempt_id='d'*32)
    key=c.digest(request)
    record=dict(request=request,manual_release=True,resumed=True,phase='resumed_awaiting_business')
    if fault=='automatic':record['manual_release']=False
    if fault=='unresumed':record['resumed']=False
    if fault=='phase':record['phase']='submitted'
    state=tmp_path/'state';(state/'jobs').mkdir(parents=True)
    parent=state/'jobs'/(key+'.json');parent.write_bytes(c.canonical(record));original=parent.read_bytes()
    monkeypatch.setattr(bootstrap,'STATE',state)
    monkeypatch.setattr(bootstrap,'read',lambda path,**_:Path(path).read_bytes())
    monkeypatch.setattr(bootstrap,'exited_job_state',lambda *_:({'Result':'success' if fault=='success' else 'exit-code'},fault!='running'))
    checks=[];monkeypatch.setattr(bootstrap,'healthy_original',lambda current:checks.append(current))
    release=tmp_path/'releases'/commit
    (release/'deploy').mkdir(parents=True)
    installer=release/'deploy/install_immutable_release.sh'
    installer.write_bytes(b'legacy installer\n' if fault=='legacy' else b'EIMEMORY_MANAGED_CURRENT_REVALIDATION_CONTRACT=independent-post-resume.v1\n')
    monkeypatch.setattr(bootstrap,'RELEASES',release.parent)
    if fault=='linked':
        original_resolve=Path.resolve
        monkeypatch.setattr(Path,'resolve',lambda path,*a,**kw:tmp_path/'other-installer' if path==installer else original_resolve(path,*a,**kw))
    registry={'runtime_commit':'c'*40 if fault=='target' else commit,'compatibility_hashes':{
        'deploy/install_immutable_release.sh':'0'*64 if fault=='drift' else c.sha256(installer.read_bytes()).hexdigest()}}
    supplied={**scope,'user_id':'other'} if fault=='scope' else scope
    if fault=='none':
        assert bootstrap.revalidation_parent(c,registry,key,supplied)==key
        assert checks==[commit]
    else:
        with pytest.raises(ValueError):bootstrap.revalidation_parent(c,registry,key,supplied)
        assert checks==[]
    assert parent.read_bytes()==original and sorted((state/'jobs').iterdir())==[parent]
