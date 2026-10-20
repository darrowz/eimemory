"""A reviewed recovery must preserve failures and stop neither a live job nor
the current business runtime during preflight."""
from hashlib import sha256
import importlib.util
from pathlib import Path
from types import SimpleNamespace
import sys

import pytest

from eimemory.contracts import runtime_manager as c


def module():
    path=Path(__file__).resolve().parents[1]/'deploy/bootstrap_runtime_manager.py'
    spec=importlib.util.spec_from_file_location('handoff_bootstrap_test',path)
    result=importlib.util.module_from_spec(spec);spec.loader.exec_module(result)
    return result


@pytest.mark.parametrize('unit',['eimemory-nightly.timer','eimemory-release-closure.path'])
def test_timer_and_path_handoff_does_not_require_service_only_properties(monkeypatch,unit):
    m=module();queries=[]
    def call(argv):
        queries.append(argv)
        return 'LoadState=loaded\nActiveState=active\nUnitFileState=enabled\n'
    monkeypatch.setattr(m,'call',call)
    state=m._handoff_unit_state(SimpleNamespace(pw_name='darrow',pw_uid=1001),'user',unit)
    assert state['ActiveState']=='active'
    assert 'MainPID' not in queries[0][-1] and 'ExecMainStatus' not in m._handoff_capture_properties(unit)


@pytest.mark.parametrize('fault',['none','different_invocation','failed','simple','missing_exit'])
def test_same_oneshot_can_finish_during_snapshot_without_masking_real_changes(monkeypatch,fault):
    m=module();unit='eimemory-vector-sync.service'
    before={unit:dict(ActiveState='activating',LoadState='loaded',UnitFileState='static',
        InvocationID='a'*32,Result='success',ExecMainStatus='0')}
    after={unit:{**before[unit],'ActiveState':'inactive'}}
    state=dict(LoadState='loaded',ActiveState='inactive',SubState='dead',MainPID='0',
        InvocationID='a'*32,ControlGroup='',Result='success',ExecMainStatus='0',
        ExecMainExitTimestampMonotonic='12345',Type='oneshot')
    if fault=='different_invocation':after[unit]['InvocationID']='b'*32;state['InvocationID']='b'*32
    if fault=='failed':state['ExecMainStatus']='1';state['Result']='exit-code'
    if fault=='simple':state['Type']='simple'
    if fault=='missing_exit':state['ExecMainExitTimestampMonotonic']='0'
    monkeypatch.setattr(m,'_handoff_unit_state',lambda *_args:dict(state))
    assert m._handoff_user_states_unchanged(c,SimpleNamespace(),before,after) is (fault=='none')


@pytest.fixture
def jobs(tmp_path,monkeypatch):
    m=module();(tmp_path/'jobs').mkdir();(tmp_path/'validations').mkdir()
    request={'schema':'eimemory.manual_release_permit.v2','operation':'deploy',
        'controller_commit':'a'*40,'validation_controller_commit':'b'*40,
        'base_commit':'a'*40,'candidate_commit':'b'*40,'target_commit':'b'*40,
        'reviewed_attempt_id':'a'*32,'scope':dict(tenant_id='t',agent_id='a',workspace_id='w',user_id='u')}
    key=c.digest(request)
    record={'manual_release':True,'request':request,'registry':{},'job_key':key,'unit':c.job_unit(key)}
    path=tmp_path/'jobs'/(key+'.json');path.write_bytes(c.canonical(record))
    active=c.canonical({'complete':False,'job_key':key})
    (tmp_path/'active-transition.json').write_bytes(active)
    monkeypatch.setattr(m,'STATE',tmp_path)
    monkeypatch.setattr(m,'read',lambda path,**_:Path(path).read_bytes())
    monkeypatch.setattr(m,'exited_job_state',lambda *_:({'ActiveState':'failed','MainPID':'0','Result':'exit-code'},True))
    contract=SimpleNamespace(**{name:getattr(c,name) for name in ('hex_value','strict_json','validate_manual_permit','validate_job','VALIDATION_PREFIXES')},
        validate_registry=lambda value:value)
    args=SimpleNamespace(expected_transition_sha256=sha256(active).hexdigest(),abandon_failed_job_key=key)
    return m,contract,args,path


def test_only_explicit_current_failed_manual_job_can_be_abandoned(jobs):
    m,contract,args,path=jobs
    original=path.read_bytes()
    result=m._handoff_jobs(contract,args)
    assert result['abandoned']==path.stem
    assert path.read_bytes()==original
    assert c.strict_json(result['original'])['complete'] is False


@pytest.mark.parametrize('fault',['cas','wrong_job','running','successful','automatic','missing_job','child_running'])
def test_handoff_denies_ambiguous_effects_without_replacing_originals(jobs,monkeypatch,fault):
    m,contract,args,path=jobs
    active=m.STATE/'active-transition.json';original=active.read_bytes()
    if fault=='cas':args.expected_transition_sha256='0'*64
    if fault=='wrong_job':args.abandon_failed_job_key='0'*64
    if fault=='running':monkeypatch.setattr(m,'exited_job_state',lambda *_:({'ActiveState':'active','MainPID':'42','Result':'success'},False))
    if fault=='successful':monkeypatch.setattr(m,'exited_job_state',lambda *_:({'ActiveState':'inactive','MainPID':'0','Result':'success'},True))
    if fault=='automatic':
        record=c.strict_json(path.read_bytes());record['manual_release']=False;path.write_bytes(c.canonical(record))
        contract.validate_job=lambda *_:None
    if fault=='missing_job':path.unlink()
    if fault=='child_running':
        operation=next(iter(c.VALIDATION_PREFIXES))
        (m.STATE/'validations'/(path.stem+'-'+operation+'.json')).write_text('{}')
        def refuse(*_args,**kwargs):
            assert kwargs['allow_running'] is False
            raise ValueError('child_still_running')
        monkeypatch.setattr(m,'fence_owner_validation',refuse)
    monkeypatch.setattr(m,'write',lambda *_args,**_kwargs:pytest.fail('no transition rewrite allowed'))
    with pytest.raises(ValueError):m._handoff_jobs(contract,args)
    assert active.read_bytes()==original


@pytest.mark.parametrize('failure',[False,True])
def test_handoff_retains_failure_and_uses_complete_original_rollback_boundary(tmp_path,monkeypatch,failure):
    m=module();history=tmp_path/'history';history.mkdir()
    active=tmp_path/'active-transition.json';original=c.canonical({'complete':False,'job_key':'a'*64});active.write_bytes(original)
    registry=tmp_path/'registry';registry.write_bytes(b'{}')
    owner={'commit':'b'*40,'prior_user_state':{'eimemory-rpc.service':{'LoadState':'loaded','UnitFileState':'enabled','ActiveState':'active'}}}
    context={'owner':owner,'jobs':{'original':original,'abandoned':'a'*64}}
    calls=[]
    monkeypatch.setattr(m,'STATE',tmp_path);monkeypatch.setattr(m,'REGISTRY',registry)
    monkeypatch.setattr(m,'load_contract',lambda:SimpleNamespace(canonical=c.canonical,strict_json=c.strict_json,
        validate_registry=lambda _:dict(generation='f'*64),USER_COMPAT_UNITS=()))
    monkeypatch.setitem(sys.modules,'pwd',SimpleNamespace(getpwnam=lambda _:SimpleNamespace(pw_gid=1001)))
    monkeypatch.setattr(m,'handoff_preflight',lambda *_:context)
    monkeypatch.setattr(m,'read',lambda p,**_:Path(p).read_bytes())
    def write(path,raw,**_):Path(path).write_bytes(raw)
    monkeypatch.setattr(m,'write',write)
    def install(_args,*,handoff):
        assert handoff is context
        (history/'owner-snapshot.json').write_bytes(c.canonical(owner));context['history']=history
        if failure:raise ValueError('native_listener_failed')
    monkeypatch.setattr(m,'install',install)
    monkeypatch.setattr(m,'healthy_original',lambda _:dict(ok=True))
    monkeypatch.setattr(m,'call',lambda argv:calls.append(argv))
    monkeypatch.setattr(m,'_handoff_unit_state',lambda _a,manager,_u:dict(ActiveState='inactive' if manager=='user' else 'active'))
    rollback=[];monkeypatch.setattr(m,'restore_owner',lambda value:rollback.append(value))
    args=SimpleNamespace(expected_current_commit='b'*40)
    if failure:
        with pytest.raises(ValueError,match='native_listener_failed'):m.handoff(args)
        assert len(rollback)==1
        assert rollback[0].expected_history_sha256==sha256((history/'owner-snapshot.json').read_bytes()).hexdigest()
        assert c.strict_json(active.read_bytes())['complete'] is False
        assert (history/'handoff-failure.json').is_file()
        assert calls==[]
    else:
        m.handoff(args)
        outcome=c.strict_json(active.read_bytes())
        assert outcome['outcome']=='operator_abandoned_failed_manual_release'
        assert (history/'abandoned-transition.original').read_bytes()==original
        assert c.strict_json((history/'handoff-complete.json').read_bytes())['l5_certified'] is False
        assert not rollback
        assert any('start' in argv and 'eimemory-rpc.service' in argv for argv in calls)


@pytest.mark.parametrize('fault',['none','registry','release','manifest','missing_manifest','dual_owner','missing_user_owner','storage'])
def test_handoff_preflight_rejects_changed_authority_before_stopping_any_service(tmp_path,monkeypatch,fault):
    m=module();repo=tmp_path/'repo';repo.mkdir();releases=tmp_path/'releases';releases.mkdir()
    current=releases/('b'*40);current.mkdir()
    registry=tmp_path/'registry';registry.write_bytes(b'{}')
    bundle=tmp_path/'bundle';bundle.write_bytes(b'pinned bundle')
    manifest=tmp_path/'manifest';manifest.write_bytes(b'{}')
    scope=tmp_path/'scope';scope.write_bytes(c.canonical({'schema':'eimemory.runtime_unit_scopes.v1','units':{}}))
    units=frozenset({'eimemory-rpc.service','eimemory-nightly.service'})
    contract=SimpleNamespace(strict_json=c.strict_json,validate_registry=lambda _:dict(repository_root=str(repo.resolve())),
        REGISTERED_UNITS=units,PRODUCT_SERVICES=tuple(units),USER_COMPAT_UNITS=(),validate_scope=c.validate_scope)
    args=SimpleNamespace(repository_root=repo,expected_registry_sha256=sha256(b'{}').hexdigest(),
        bundle=bundle,expected_bundle_sha256=sha256(bundle.read_bytes()).hexdigest(),
        expected_current_commit='b'*40,unit_scope_manifest=scope,expected_unit_scope_sha256=sha256(scope.read_bytes()).hexdigest(),
        managed_incident_manifest=manifest,expected_managed_incident_manifest_sha256=sha256(b'{}').hexdigest())
    monkeypatch.setattr(m,'load_contract',lambda:contract);monkeypatch.setattr(m,'REGISTRY',registry)
    monkeypatch.setattr(m,'CURRENT',current);monkeypatch.setattr(m,'RELEASES',releases)
    monkeypatch.setattr(m,'read',lambda p,**_:Path(p).read_bytes())
    monkeypatch.setattr(m,'bundle',lambda *_:({},dict(controller_commit='b'*40)))
    monkeypatch.setattr(m,'_handoff_jobs',lambda *_:dict(original=b'',abandoned=None))
    monkeypatch.setattr(m,'owner_files',lambda *_:dict(original=True))
    monkeypatch.setattr(m,'healthy_original',lambda _:dict(ok=True))
    monkeypatch.setattr(m,'call',lambda *_:pytest.fail('preflight must not stop or start services'))
    def state(_account,manager,unit,properties):
        value=dict(LoadState='loaded',ActiveState='active' if manager=='user' and unit=='eimemory-rpc.service' else 'inactive',
            UnitFileState='enabled',Result='success',InvocationID='',ExecMainStatus='0')
        if fault=='dual_owner' and manager=='system' and unit=='eimemory-rpc.service':value['ActiveState']='active'
        if fault=='missing_user_owner' and manager=='user':value['ActiveState']='inactive'
        return {key:value[key] for key in properties.split(',')}
    monkeypatch.setattr(m,'_handoff_unit_state',state)
    def clean():
        if fault=='storage':raise ValueError('pending_storage')
    monkeypatch.setattr(m,'storage_clean',clean)
    if fault=='registry':args.expected_registry_sha256='0'*64
    if fault=='release':args.expected_current_commit='c'*40
    if fault=='manifest':args.expected_managed_incident_manifest_sha256='0'*64
    if fault=='missing_manifest':args.managed_incident_manifest=None
    if fault=='none':
        result=m.handoff_preflight(args,SimpleNamespace())
        assert result['owner']['commit']=='b'*40
    else:
        with pytest.raises(ValueError):m.handoff_preflight(args,SimpleNamespace())
