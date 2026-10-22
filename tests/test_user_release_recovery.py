"""Reviewed user recovery preserves native failures and installation fences."""
from copy import deepcopy
from hashlib import sha256
import importlib.util
import json
import os
from pathlib import Path
from types import SimpleNamespace
import subprocess
import sys

import pytest

from eimemory.contracts import runtime_manager as contract


def installer():
    source=Path(__file__).resolve().parents[1]/'deploy/install_user_runtime_manager.py'
    spec=importlib.util.spec_from_file_location('reviewed_user_recovery_tests',source)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize('entry',['install','recover_completed_installation','stage_release','abandon_failed_manual'])
@pytest.mark.parametrize('original',[None,'','user','inherited-legacy-value'])
def test_public_operator_identity_failure_restores_exact_caller_profile(monkeypatch,entry,original):
    module=installer();name='EIMEMORY_CONTROL_PLANE'
    if original is None:monkeypatch.delenv(name,raising=False)
    else:monkeypatch.setenv(name,original)
    monkeypatch.setattr(module.os,'geteuid',lambda:0,raising=False)
    monkeypatch.setattr(module.os,'getuid',lambda:0,raising=False)
    # Unsupported native imports on Windows are also exceptions which must
    # restore the selection before another caller/legacy fixture executes.
    with pytest.raises((ValueError,ModuleNotFoundError)):
        getattr(module,entry)(SimpleNamespace())
    assert (name in os.environ)==(original is not None)
    assert os.environ.get(name)==original


@pytest.mark.parametrize('original',[None,'','user','inherited-legacy-value'])
@pytest.mark.parametrize('fails',[False,True])
def test_operator_scope_inherits_user_in_child_and_restores_on_success_or_error(monkeypatch,original,fails):
    module=installer();name='EIMEMORY_CONTROL_PLANE'
    if original is None:monkeypatch.delenv(name,raising=False)
    else:monkeypatch.setenv(name,original)
    @module.scoped_user_operator
    def operation():
        assert os.environ[name]=='user'
        child=subprocess.run([sys.executable,'-c',
            'import os;print(os.environ.get("EIMEMORY_CONTROL_PLANE"))'],
            text=True,capture_output=True,check=True)
        assert child.stdout.strip()=='user'
        if fails:raise RuntimeError('operator failed after starting child')
        return 'completed'
    if fails:
        with pytest.raises(RuntimeError,match='after starting child'):operation()
    else:assert operation()=='completed'
    assert (name in os.environ)==(original is not None)
    assert os.environ.get(name)==original


def attempt():
    request={'schema':'eimemory.manual_release_permit.v2','operation':'deploy',
        'base_commit':'b'*40,'candidate_commit':'a'*40,'target_commit':'a'*40,
        'controller_commit':'a'*40,'validation_controller_commit':'a'*40,
        'reviewed_attempt_id':'c'*32,
        'scope':dict(tenant_id='fixture',agent_id='fixture',workspace_id='fixture',user_id='fixture')}
    key=contract.digest(request)
    registry={'runtime_commit':'b'*40,'controller_commit':'a'*40,'generation':'f'*64,
        'units':{'eimemory-rpc.service':{},'eimemory-nightly.timer':{}}}
    record={'job_key':key,'unit':contract.job_unit(key),'request':request,'registry':registry,
        'manual_release':True,'phase':'publish','quiesced':True,'resumed':False,
        'active_before':['eimemory-rpc.service','eimemory-nightly.timer'],'original_invocation':'d'*32}
    native=dict(LoadState='loaded',ActiveState='failed',SubState='failed',Result='exit-code',
        InvocationID='d'*32,MainPID='0',ExecMainStatus='2',ExecMainExitTimestampMonotonic='123',
        ControlGroup='')
    return record,native


@pytest.mark.parametrize('fault',['none','prestart','unknown','changed_invocation','control_alive',
    'main_alive','queued','success','prestart_main_started','prestart_changed'])
def test_native_retry_requires_real_failed_main_or_distinct_prestart(monkeypatch,fault):
    module=installer();record,native=attempt()
    native.update(ControlPID='0',ExecMainStartTimestampMonotonic='42')
    if fault.startswith('prestart'):
        record.update(phase='submitted',quiesced=False,active_before=None,original_invocation=None)
        native.update(ExecMainStatus='0',ExecMainExitTimestampMonotonic='0',ExecMainStartTimestampMonotonic='0')
    if fault=='unknown':native.update(ActiveState='inactive',InvocationID='',Result='success',ExecMainExitTimestampMonotonic='0')
    elif fault=='changed_invocation':native['InvocationID']='e'*32
    elif fault=='control_alive':native['ControlPID']='123'
    elif fault=='main_alive':native['MainPID']='123'
    elif fault=='queued':native['ActiveState']='activating'
    elif fault=='success':native.update(ActiveState='active',SubState='exited',Result='success',ExecMainStatus='0')
    elif fault=='prestart_main_started':native['ExecMainStartTimestampMonotonic']='1'
    elif fault=='prestart_changed':record['prestart_failure']={'different':'proof'}
    monkeypatch.setattr(module,'native_state',lambda *_:deepcopy(native))
    before=deepcopy(record)
    if fault in {'none','prestart'}:
        result=module.failed_attempt_native(contract,record,{})
        assert result['Result']=='exit-code' and result['InvocationID']=='d'*32
        assert ('ExecMainStartTimestampMonotonic' in result) is (fault=='prestart')
    else:
        with pytest.raises(ValueError):module.failed_attempt_native(contract,record,{})
    assert record==before  # The adverse job/nonce is immutable to operator recovery.


@pytest.mark.parametrize('fault',['none','deferred','candidate','completed_binding_failure',
    'candidate_not_resumed','unrelated_registry','success','changed_transition'])
def test_abandon_handles_rollback_and_current_failed_finalization_without_acceptance(fault):
    module=installer();record,native=attempt();registry=deepcopy(record['registry'])
    transition={'complete':False,'job_key':record['job_key']};current='b'*40
    if fault=='deferred':record['phase']='resume_deferred_until_terminal'
    if fault in {'candidate','completed_binding_failure','candidate_not_resumed','unrelated_registry'}:
        current='a'*40;registry['runtime_commit']=current;registry['generation']='e'*64
        record.update(resumed=True,phase='resumed_awaiting_business')
    if fault=='completed_binding_failure':
        record.update(phase='completed',finalized=True)
        transition.update(complete=True,held=False)
    if fault=='candidate_not_resumed':record['resumed']=False
    if fault=='unrelated_registry':registry['units']['eimemory-rpc.service']={'installed':False}
    if fault=='success':native.update(ActiveState='active',SubState='exited',Result='success',ExecMainStatus='0')
    if fault=='changed_transition':transition['job_key']='0'*64
    original=deepcopy((record,native,registry,transition))
    if fault in {'none','deferred','candidate','completed_binding_failure'}:
        module.validate_failed_manual_recovery(contract,registry,record,transition,native,current)
    else:
        with pytest.raises(ValueError):module.validate_failed_manual_recovery(contract,registry,record,transition,native,current)
    assert (record,native,registry,transition)==original


@pytest.mark.parametrize('fault',['none','unknown_unit','unknown_state'])
def test_capture_preserves_original_active_and_enabled_sets(fault):
    module=installer();record,native=attempt();raw=contract.canonical(record)
    enabled={'eimemory-rpc.service':'enabled','eimemory-nightly.timer':'disabled'}
    if fault=='unknown_unit':enabled['foreign.service']='enabled'
    if fault=='unknown_state':enabled['eimemory-rpc.service']='active'
    if fault!='none':
        with pytest.raises(ValueError):module.recovery_capture(contract,record,raw,native,'b'*40,'manual-abandon-123',enabled)
        return
    value=module.recovery_capture(contract,record,raw,native,'b'*40,'manual-abandon-123',enabled)
    assert value['source_job_sha256']==sha256(raw).hexdigest()
    assert value['active_before']==record['active_before'] and value['enabled_before']==enabled
    assert value['source_invocation_id']==native['InvocationID']
    assert value['source_registry_generation']==record['registry']['generation']
    assert value['source_native_exit']['Result']=='exit-code'
    assert 'release_accepted' not in value and 'autonomous_repair_credit' not in value


@pytest.mark.parametrize('fault',['none','wrong_release','unloaded','wrong_schema','autonomous_credit'])
def test_original_system_and_user_migration_audits_preserve_both_autostart_domains(fault):
    module=installer();record,_native=attempt()
    record['active_before']=['eimemory-l1-extract.timer','hermes-gateway-hongtai.service']
    record['registry']['units'].update({unit:{} for unit in record['active_before']})
    user={'schema':'manual_user_owner_before.v1','states':{
        'eimemory-l1-extract.timer':dict(LoadState='loaded',UnitFileState='disabled'),
        'hermes-gateway-hongtai.service':dict(LoadState='loaded',UnitFileState='enabled')}}
    system={'schema':'manual_user_owner_custody.v1','current_commit':'b'*40,
        'manual_bootstrap':True,'autonomous_credit':False,'system_units':{
            'eimemory-l1-extract.timer':dict(LoadState='loaded',UnitFileState='enabled')}}
    if fault=='wrong_release':system['current_commit']='c'*40
    elif fault=='unloaded':system['system_units']['eimemory-l1-extract.timer']['LoadState']='not-found'
    elif fault=='wrong_schema':user['schema']='untrusted'
    elif fault=='autonomous_credit':system['autonomous_credit']=True
    if fault=='none':
        assert module.migration_enabled_capture(contract,record,user,system)=={
            'eimemory-l1-extract.timer':'enabled','hermes-gateway-hongtai.service':'enabled'}
    else:
        with pytest.raises(ValueError):module.migration_enabled_capture(contract,record,user,system)


@pytest.mark.parametrize('fault',['none','automatic','unresumed','wrong_current','scope','no_contract','unknown_exit'])
def test_same_current_revalidation_requires_exact_failed_native_parent_and_contract(monkeypatch,fault):
    module=installer();record,_native=attempt()
    record.update(resumed=True,phase='resumed_awaiting_business')
    registry=deepcopy(record['registry']);registry.update(runtime_commit='a'*40,
        compatibility_hashes={'deploy/install_immutable_release.sh':'0'*64})
    scope=deepcopy(record['request']['scope'])
    if fault=='automatic':record['manual_release']=False
    elif fault=='unresumed':record['resumed']=False
    elif fault=='wrong_current':registry['runtime_commit']='e'*40
    elif fault=='scope':scope['agent_id']='different'
    raw=b'EIMEMORY_MANAGED_CURRENT_REVALIDATION_CONTRACT=independent-post-resume.v1\n'
    monkeypatch.setattr(module,'reviewed',lambda *_a,**_kw:b'no contract' if fault=='no_contract' else raw)
    def proof(*_):
        if fault=='unknown_exit':raise ValueError('native start unknown')
    monkeypatch.setattr(module,'failed_attempt_native',proof)
    if fault=='none':module.revalidation_parent(contract,registry,record,scope,{})
    else:
        with pytest.raises(ValueError):module.revalidation_parent(contract,registry,record,scope,{})


@pytest.mark.parametrize('fault',['none','active','main','control','cgroup','duplicate','missing'])
def test_stopped_controller_requires_native_empty_process_group(monkeypatch,fault):
    module=installer();state=dict(ActiveState='inactive',MainPID='0',ControlPID='0',ControlGroup='')
    if fault=='active':state['ActiveState']='active'
    elif fault=='main':state['MainPID']='123'
    elif fault=='control':state['ControlPID']='123'
    elif fault=='cgroup':state['ControlGroup']='/user.slice/remaining'
    if fault=='missing':state.pop('ControlPID')
    text='\n'.join(key+'='+value for key,value in state.items())
    if fault=='duplicate':text+='\nMainPID=0'
    monkeypatch.setattr(module.subprocess,'run',lambda *_a,**_kw:SimpleNamespace(returncode=0,stdout=text))
    if fault=='none':module.stopped_controller({})
    else:
        with pytest.raises(ValueError):module.stopped_controller({})


@pytest.mark.skipif(os.name!='posix',reason='native custody ownership and filesystem writes')
@pytest.mark.parametrize('fault',['ledger_saved','ledger_missing','foreign_ledger','unapplied','lost_active','lost_enabled','different_invocation'])
def test_failed_consumer_can_retire_only_its_exact_applied_capture(tmp_path,monkeypatch,fault):
    module=installer();uid=os.geteuid()
    if uid==0:pytest.skip('unprivileged operator required')
    def fixture_directory(path,_uid,mode=0o750):
        Path(path).mkdir(parents=True,exist_ok=True);Path(path).chmod(mode)
    monkeypatch.setattr(module,'directory',fixture_directory)
    state=tmp_path/'state';source=state/'managed-incident-receipts/manual-abandon-123'
    history=state/'managed-incident-receipts/manual-abandon-456'
    source.mkdir(parents=True);history.mkdir()
    original,native=attempt();original['enabled_before']={'eimemory-rpc.service':'enabled','eimemory-nightly.timer':'disabled'}
    raw=contract.canonical(original)
    capture=module.recovery_capture(contract,original,raw,native,'b'*40,source.name,original['enabled_before'])
    capture_raw=contract.canonical(capture)
    for name,data in [('job.original',raw),('native-exit.json',contract.canonical(native)),('recovery-capture.json',capture_raw)]:
        (source/name).write_bytes(data)
    fixed=state/'manual-recovery-capture.json';fixed.write_bytes(capture_raw)
    consumer=deepcopy(original);consumer['request']['reviewed_attempt_id']='e'*32
    consumer['job_key']=contract.digest(consumer['request']);consumer['unit']=contract.job_unit(consumer['job_key'])
    consumer['original_invocation']='f'*32
    applied={'schema':'eimemory.manual_recovery_capture_consumption.v1','sha256':sha256(capture_raw).hexdigest(),
        'job_key':consumer['job_key'],'invocation_id':consumer['original_invocation'],
        'source_job_key':original['job_key'],'history_basename':source.name}
    consumer['recovery_capture_applied']=deepcopy(applied)
    if fault=='foreign_ledger':applied['job_key']='0'*64
    elif fault=='unapplied':consumer.pop('recovery_capture_applied')
    elif fault=='lost_active':consumer['active_before'].pop()
    elif fault=='lost_enabled':consumer['enabled_before']['eimemory-rpc.service']='disabled'
    elif fault=='different_invocation':consumer['original_invocation']='0'*32
    if fault!='ledger_missing':(source/'capture-consumption.json').write_bytes(contract.canonical(applied))
    helper=SimpleNamespace(read_file=lambda path:Path(path).read_bytes())
    if fault in {'ledger_saved','ledger_missing'}:
        module.reconcile_consumed_capture(helper,contract,state,consumer,history,uid)
        assert (history/'previous-consumed-capture.json').read_bytes()==capture_raw
        assert contract.strict_json((source/'capture-consumption.json').read_bytes())==consumer['recovery_capture_applied']
        assert (source/'capture-consumption-operator-recovery.json').exists()==(fault=='ledger_missing')
    else:
        with pytest.raises(ValueError):module.reconcile_consumed_capture(helper,contract,state,consumer,history,uid)
        assert not (history/'previous-consumed-capture.json').exists()
    assert fixed.read_bytes()==capture_raw and (source/'job.original').read_bytes()==raw


@pytest.mark.skipif(os.name!='posix',reason='native original custody and terminal pin writes')
def test_real_failed_terminal_pin_keeps_pre_pin_bytes_and_status_cannot_change_capture_source(tmp_path,monkeypatch):
    module=installer();uid=os.geteuid()
    if uid==0:pytest.skip('unprivileged operator required')
    def fixture_directory(path,_uid,mode=0o750):
        Path(path).mkdir(parents=True,exist_ok=True);Path(path).chmod(mode)
    monkeypatch.setattr(module,'directory',fixture_directory)
    state=tmp_path/'state';(state/'jobs').mkdir(parents=True)
    history=state/'managed-incident-receipts/manual-abandon-123';history.mkdir(parents=True)
    record,native=attempt();raw=contract.canonical(record)
    job=state/'jobs'/(record['job_key']+'.json');job.write_bytes(raw)
    helper=SimpleNamespace(read_file=lambda path:Path(path).read_bytes())
    monkeypatch.setattr(module,'failed_attempt_native',lambda *_:deepcopy(native))
    pinned,pinned_raw=module.pin_failed_terminal(helper,contract,state,record,raw,native,history,uid,{})
    assert (history/'job.pre-terminal-pin.original').read_bytes()==raw
    assert raw!=pinned_raw and job.read_bytes()==pinned_raw
    assert pinned['terminal_state']==native and pinned['phase']==record['phase']
    assert pinned['operator_terminal_pin']['autonomous_repair_credit'] is False
    # Actual status code must see a stable already pinned failure; a read can
    # no longer rewrite the exact job bytes referenced by the recovery capture.
    source=Path(contract.__file__).resolve().parents[2]/'deploy/runtime_manager_bridge.py'
    monkeypatch.delenv('EIMEMORY_CONTROL_PLANE',raising=False)
    spec=importlib.util.spec_from_file_location('status_after_manual_terminal_pin',source)
    bridge_module=importlib.util.module_from_spec(spec);spec.loader.exec_module(bridge_module)
    bridge=bridge_module.Bridge(contract,state=state)
    monkeypatch.setattr(bridge,'record',lambda _key:contract.strict_json(job.read_bytes()))
    monkeypatch.setattr(bridge,'unit_state',lambda *_:deepcopy(native))
    monkeypatch.setattr(bridge,'save',lambda *_a,**_kw:pytest.fail('status mutated an abandoned capture source'))
    result=bridge.job_status(record['registry'],record['job_key'])
    assert result['terminal_state']==native and job.read_bytes()==pinned_raw


@pytest.mark.skipif(os.name!='posix',reason='native descriptor and durable installation protocol')
@pytest.mark.parametrize('fault',['none','reload','code','identity','policy','readback','sealed_crash','sealed_tamper'])
def test_installation_failure_retains_fence_and_originals_until_full_readback(tmp_path,monkeypatch,fault):
    module=installer();uid=os.geteuid()
    if uid==0:pytest.skip('unprivileged operator required')
    home=tmp_path/'operator';authority=home/'.config/eimemory/control-plane'
    state=home/'.local/state/eimemory/control-plane';code=home/'.local/lib/eimemory/control-plane'
    units=home/'.config/systemd/user';knowledge=state/'managed-incident-receipts'
    for path in (authority,code,units,knowledge):path.mkdir(parents=True,exist_ok=True)
    originals={authority/'managed-incident-proof.key':b'm'*32,state/'activation-signing.key':b'a'*32,
        knowledge/'knowledge.sqlite':b'original-ledger',authority/'runtime-manager.v1.json':b'original-registry',
        code/'helper.py':b'original-helper'}
    for path,raw in originals.items():path.write_bytes(raw)
    current=tmp_path/'releases'/('b'*40);current.mkdir(parents=True)
    real_path=Path
    paths={'/opt/eimemory/current':current,'/opt/eimemory/releases':current.parent}
    monkeypatch.setattr(module,'Path',lambda value:paths.get(str(value),real_path(value)))
    # Security of ancestry/descriptor checks is exercised separately; this test
    # executes the durable publication protocol using real fsync/rename/tar.
    def fixture_directory(path,_uid,mode=0o750):
        path=real_path(path);path.mkdir(parents=True,exist_ok=True);path.chmod(mode)
    monkeypatch.setattr(module,'directory',fixture_directory)
    monkeypatch.setattr(module,'stopped_controller',lambda _:None)
    monkeypatch.setattr(module,'verify_controller_sources',lambda *_:None)
    files={'eimemory-runtime-manager.service':b'[Service]\nExecStart=/bin/true\n',
        'user_control_plane.py':b'# reviewed fixture user helper\n',
        'runtime_manager_contract.py':b'# reviewed fixture contract\n',
        'helper.py':b'reviewed-helper','templates/eimemory-rpc.service':b'[Service]\nExecStart=/bin/true\n',
        'runtime-policy.conf':b'[Service]\nEnvironment=TARGET=@COMMIT@\n'}
    import pwd
    native_account=pwd.getpwuid(uid)
    registry={'runtime_commit':'b'*40,'controller_commit':'a'*40,'generation':'f'*64,
        'runtime_uid':uid,'runtime_gid':os.getegid(),'runtime_user':native_account.pw_name,
        'template_hashes':{'eimemory-rpc.service':sha256(files['templates/eimemory-rpc.service']).hexdigest()},
        'compatibility_hashes':{source:sha256(files[name]).hexdigest() for name,source in
            [('user_control_plane.py','eimemory/contracts/user_control_plane.py'),
             ('runtime_manager_contract.py','eimemory/contracts/runtime_manager.py')]},
        'units':{'eimemory-rpc.service':{'installed':True}}}
    seed={'runtime_commit':'b'*40,'generation':'f'*64};manifest={'controller_commit':'a'*40}
    args=SimpleNamespace(expected_bundle_sha256='e'*64,
        expected_existing_registry_sha256=sha256(b'original-registry').hexdigest(),
        expected_managed_key_sha256=sha256(b'm'*32).hexdigest(),
        expected_activation_key_sha256=sha256(b'a'*32).hexdigest(),
        expected_knowledge_sha256=sha256(b'original-ledger').hexdigest())
    real_write=module.write
    def write(path,raw,owner,mode=0o640):
        if fault=='code' and real_path(path)==code/'helper.py':raise OSError('injected code publication failure')
        return real_write(path,raw,owner,mode)
    monkeypatch.setattr(module,'write',write)
    calls=[]
    def run(argv,**_kw):
        calls.append(argv)
        if fault=='reload' and len(calls)==1:raise OSError('injected reload failure')
        fail=(fault=='identity' and len(calls)==2 or fault=='policy' and len(calls)==3)
        if len(calls)==3:
            policy=units/'eimemory-rpc.service.d/zzzzzz-eimemory-user-control-plane.conf'
            policy.write_bytes(b'[Service]\nNoNewPrivileges=yes\n')
        if fault=='readback' and len(calls)==3:(code/'helper.py').write_bytes(b'tampered')
        return SimpleNamespace(returncode=1 if fail else 0)
    monkeypatch.setattr(module.subprocess,'run',run)
    recovering={'active':False}
    real_unlink=real_path.unlink
    def unlink(path,*args,**kwargs):
        if (fault in {'sealed_crash','sealed_tamper'} and path==state/'controller-installation.json'
                and not recovering['active']):raise OSError('injected crash after durable seal')
        return real_unlink(path,*args,**kwargs)
    monkeypatch.setattr(real_path,'unlink',unlink)
    invoke=lambda:module._publish_installation(args,SimpleNamespace(pw_uid=uid,pw_gid=os.getegid(),pw_dir=str(home)),
        files,manifest,seed,b'{}',b'{}',b'{}',registry,SimpleNamespace(canonical=module.canonical),None,{},b'original-registry',None)
    if fault=='none':invoke()
    else:
        with pytest.raises((OSError,ValueError)):invoke()
    marker=state/'controller-installation.json';backups=list((knowledge/'migration-backups').glob('*.tar'))
    assert len(backups)==1 and backups[0].stat().st_size>0
    guard=units/'eimemory-runtime-manager.service.d/05-eimemory-installation-guard.conf'
    assert ('ConditionPathExists=!'+str(marker)) in guard.read_text()
    if fault=='none':
        assert not marker.exists()
        seal=json.loads(backups[0].with_suffix('.complete.json').read_bytes())
        assert seal['release_accepted'] is False and seal['autonomous_repair_credit'] is False
    else:
        assert marker.exists() and json.loads(marker.read_bytes())['phase']=='publication_pending'
        assert backups[0].with_suffix('.complete.json').exists()==(fault in {'sealed_crash','sealed_tamper'})
    if fault in {'identity','policy','sealed_crash','sealed_tamper'}:
        recovering['active']=True
        monkeypatch.setattr(pwd,'getpwuid',lambda _:SimpleNamespace(pw_uid=uid,pw_gid=os.getegid(),
            pw_name=native_account.pw_name,pw_dir=str(home)))
        helper=SimpleNamespace(trusted_directory=lambda *_a,**_kw:None,
            read_file=lambda path,**kwargs:module.owned_bytes(path,uid,kwargs.get('maximum',65536)))
        reviewed_contract=SimpleNamespace(validate_registry=lambda value:value)
        monkeypatch.setattr(module,'execute_reviewed_module',lambda name,*_a:
            helper if name=='user_control_plane.py' else reviewed_contract)
        seal=backups[0].with_suffix('.complete.json')
        recovery=SimpleNamespace(expected_marker_sha256=sha256(marker.read_bytes()).hexdigest(),
            expected_registry_sha256=sha256((authority/'runtime-manager.v1.json').read_bytes()).hexdigest(),
            expected_completion_sha256=sha256(seal.read_bytes()).hexdigest() if seal.exists() else '0'*64)
        if fault=='sealed_tamper':
            (units/'eimemory-rpc.service.d/zzzzzz-eimemory-user-control-plane.conf').write_bytes(b'tampered policy')
        if fault=='sealed_crash':
            module.recover_completed_installation(recovery)
            assert not marker.exists() and list(backups[0].parent.glob('*.recovered-*.json'))
        else:
            with pytest.raises((OSError,ValueError)):module.recover_completed_installation(recovery)
            assert marker.exists()
    assert (knowledge/'knowledge.sqlite').read_bytes()==b'original-ledger'
