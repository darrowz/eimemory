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


@pytest.mark.parametrize('entry',['install','recover_completed_installation','finish_published_installation','stage_release','abandon_failed_manual','register_topology','inspect_topology_native'])
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
@pytest.mark.parametrize('fault',['none','reload','code','identity','policy','readback','sealed_crash','sealed_tamper',
    'sealed_key_tamper','sealed_ledger_tamper','sealed_key_mode','sealed_wal_tamper',
    'sealed_downgrade','sealed_missing_materials','sealed_null_materials','sealed_material_race',
    'sealed_transition_race','sealed_current_race','sealed_source_race'])
@pytest.mark.parametrize('gateway',[False,True])
def test_installation_failure_retains_fence_and_originals_until_full_readback(tmp_path,monkeypatch,fault,gateway):
    module=installer();uid=os.geteuid()
    if uid==0:pytest.skip('unprivileged operator required')
    home=tmp_path/'operator';authority=home/'.config/eimemory/control-plane'
    state=home/'.local/state/eimemory/control-plane';code=home/'.local/lib/eimemory/control-plane'
    units=home/'.config/systemd/user';knowledge=state/'managed-incident-receipts'
    for path in (authority,code,units,knowledge):path.mkdir(parents=True,exist_ok=True)
    originals={authority/'managed-incident-proof.key':b'm'*32,state/'activation-signing.key':b'a'*32,
        knowledge/'knowledge.sqlite':b'original-ledger',authority/'runtime-manager.v1.json':b'original-registry',
        code/'helper.py':b'original-helper'}
    for path,raw in originals.items():
        path.write_bytes(raw)
        if path.name.endswith('.key'):path.chmod(0o600)
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
    gateway_manifest=None;gateway_plan=[];native_gateway={}
    if gateway:
        gateway_unit='hermes-gateway-hongtai.service'
        registry['units'].update({gateway_unit:{'installed':True},'openclaw-gateway.service':{'installed':False}})
        native_gateway={units/gateway_unit:b'[Service]\nExecStart=/usr/bin/native-gateway\n',
            units/(gateway_unit+'.d')/'99-user-business.conf':b'[Service]\nEnvironment=BUSINESS_NATIVE=1\n'}
        for path,raw in native_gateway.items():
            path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(raw);path.chmod(0o664)
        (units/(gateway_unit+'.d')).chmod(0o775)
        entries=[]
        for unit,leaf,raw in [(gateway_unit,'05-eimemory-installation-guard.conf',b'[Unit]\nConditionPathExists=!old\n'),
                (gateway_unit,'zzzzzz-eimemory-user-control-plane.conf',b'[Service]\nPrivateUsers=yes\n'),
                ('openclaw-gateway.service','05-eimemory-installation-guard.conf',b'[Unit]\nConditionPathExists=!old\n')]:
            path=units/(unit+'.d')/leaf;path.parent.mkdir(exist_ok=True);path.write_bytes(raw)
            entry=gateway_entry(path.relative_to(units).as_posix(),raw,uid,path.stat().st_mode&0o777)
            entries.append(entry)
            original,identity=module.gateway_original(path,uid)
            gateway_plan.append(dict(entry=entry,original=original,identity=identity))
        gateway_manifest=module.canonical({'schema':'eimemory.gateway_policy_retirement.v1','entries':entries})
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
            if gateway:
                (units/(gateway_unit+'.d')/'zzzzzz-eimemory-user-control-plane.conf').write_bytes(
                    gateway_metadata('b'*40))
        if fault=='readback' and len(calls)==3:(code/'helper.py').write_bytes(b'tampered')
        return SimpleNamespace(returncode=1 if fail else 0)
    monkeypatch.setattr(module.subprocess,'run',run)
    recovering={'active':False}
    real_unlink=real_path.unlink
    def unlink(path,*args,**kwargs):
        if (fault.startswith('sealed_') and path==state/'controller-installation.json'
                and not recovering['active']):raise OSError('injected crash after durable seal')
        return real_unlink(path,*args,**kwargs)
    monkeypatch.setattr(real_path,'unlink',unlink)
    invoke=lambda:module._publish_installation(args,SimpleNamespace(pw_uid=uid,pw_gid=os.getegid(),pw_dir=str(home)),
        files,manifest,seed,b'{}',b'{}',b'{}',registry,SimpleNamespace(canonical=module.canonical),None,{},b'original-registry',None,
        gateway_manifest,gateway_plan)
    if fault=='none':invoke()
    else:
        with pytest.raises((OSError,ValueError)):invoke()
    marker=state/'controller-installation.json';backups=list((knowledge/'migration-backups').glob('*.tar'))
    assert len(backups)==1 and backups[0].stat().st_size>0
    guard=units/'eimemory-runtime-manager.service.d/05-eimemory-installation-guard.conf'
    assert ('ConditionPathExists=!'+str(marker)) in guard.read_text()
    if gateway:
        for unit in [gateway_unit,'openclaw-gateway.service']:
            prior_guard=units/(unit+'.d')/'05-eimemory-installation-guard.conf'
            if fault=='reload':assert prior_guard.read_bytes()==b'[Unit]\nConditionPathExists=!old\n'
            else:assert not prior_guard.exists()
        assert not (units/'openclaw-gateway.service').exists()
        for path,raw in native_gateway.items():
            assert path.read_bytes()==raw and path.stat().st_mode&0o777==0o664
        assert (units/(gateway_unit+'.d')).stat().st_mode&0o777==0o775
        assert 'start' not in [arg for argv in calls for arg in argv]
    if fault=='none':
        assert not marker.exists()
        outbox=home/'.local/state/eimemory/hermes-handoff'
        assert outbox.is_dir() and outbox.stat().st_mode&0o777==0o700
        seal=json.loads(backups[0].with_suffix('.complete.json').read_bytes())
        assert seal['release_accepted'] is False and seal['autonomous_repair_credit'] is False
        if gateway:
            assert seal['gateway_retirement']['manifest_sha256']==sha256(gateway_manifest).hexdigest()
            assert len(seal['gateway_retirement']['originals'])==3
    else:
        assert marker.exists() and json.loads(marker.read_bytes())['phase']=='publication_pending'
        assert backups[0].with_suffix('.complete.json').exists()==fault.startswith('sealed_')
    if fault in {'identity','policy'} or fault.startswith('sealed_'):
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
        if fault=='sealed_key_tamper':(state/'activation-signing.key').write_bytes(b'x'*32)
        if fault=='sealed_ledger_tamper':(knowledge/'knowledge.sqlite').write_bytes(b'changed-ledger')
        if fault=='sealed_key_mode':(state/'activation-signing.key').chmod(0o640)
        if fault=='sealed_wal_tamper':(knowledge/'knowledge.sqlite-wal').write_bytes(b'pending')
        if fault in {'sealed_downgrade','sealed_missing_materials','sealed_null_materials'}:
            value=json.loads(seal.read_bytes())
            if fault=='sealed_downgrade':
                value['schema']='eimemory.user_controller_installation_completion.v1'
                value.pop('protected_materials')
            elif fault=='sealed_missing_materials':value.pop('protected_materials')
            else:value['protected_materials']=None
            seal.write_bytes(module.canonical(value));recovery.expected_completion_sha256=sha256(seal.read_bytes()).hexdigest()
        if fault in {'sealed_material_race','sealed_transition_race','sealed_current_race','sealed_source_race'}:
            real_archive=module.write
            changed={'value':False}
            def archive_then_race(path,raw,owner,mode=0o600):
                result=real_archive(path,raw,owner,mode)
                if '.recovered-' in str(path):
                    changed['value']=True
                    if fault=='sealed_material_race':(state/'activation-signing.key').write_bytes(b'x'*32)
                    elif fault=='sealed_transition_race':(state/'active-transition.json').write_bytes(b'changed-transition')
                    elif fault=='sealed_current_race':
                        other=current.parent/('c'*40);other.mkdir();paths['/opt/eimemory/current']=other
                return result
            monkeypatch.setattr(module,'write',archive_then_race)
            if fault=='sealed_source_race':
                def changed_source(*args):
                    if changed['value']:raise ValueError('source changed')
                monkeypatch.setattr(module,'verify_controller_sources',changed_source)
        if fault=='sealed_crash':
            module.recover_completed_installation(recovery)
            assert not marker.exists() and list(backups[0].parent.glob('*.recovered-*.json'))
        else:
            with pytest.raises((OSError,ValueError)):module.recover_completed_installation(recovery)
            assert marker.exists()
    assert (knowledge/'knowledge.sqlite').read_bytes()==(b'changed-ledger' if fault=='sealed_ledger_tamper' else b'original-ledger')


def gateway_entry(relative,raw,uid,mode=0o664):
    return {'relative_path':relative,'sha256':sha256(raw).hexdigest(),'uid':uid,'mode':mode,
        'source':{'kind':'operator_reviewed_legacy_eimemory',
            'reference':'fixture-original-native-audit.json','sha256':'e'*64}}


def gateway_metadata(commit):
    return (b'# eimemory gateway memory integration only; native process policy is unchanged.\n'
        b'[Service]\nEnvironment=EIMEMORY_ROOT=/opt/eimemory/current\n'
        b'Environment=EIMEMORY_RUNTIME_COMMIT='+commit.encode()+b'\n')


@pytest.mark.parametrize('unit,expected',[
    ('hermes-gateway.service',True),('hermes-gateway-hongtai.service',True),
    ('hermes-gateway-hongxin.service',True),('hermes-gateway-xiaomage.service',True),
    ('openclaw-gateway.service',True),('business-gateway.service',True),
    ('openclaw-loop-observer.service',False),('eimemory-rpc.service',False),
    ('hermes-gateway.service.d',False),('../hermes-gateway.service',False),
    ('hermes-gateway-.service',False),('gateway.service',False),('',False),(None,False)])
def test_gateway_classification_matches_shared_policy(unit,expected):
    module=installer()
    source=Path(__file__).resolve().parents[1]/'deploy/runtime_manager_user_policy.py'
    spec=importlib.util.spec_from_file_location('gateway_shared_classifier',source)
    shared=importlib.util.module_from_spec(spec);spec.loader.exec_module(shared)
    assert module.is_gateway_unit(unit) is expected
    assert shared.is_gateway_unit(unit) is expected


@pytest.mark.parametrize('fault',['none','duplicate','path_escape','native_fragment','custom_business',
    'sidecar','foreign_uid','bool_mode','large_mode','bad_mode','bad_source','missing_source',
    'invalid_sha','non_string_sha','bad_reference','extra_field','duplicate_key'])
def test_gateway_retirement_requires_explicit_bounded_review_of_each_eimemory_dropin(fault):
    module=installer();entry=gateway_entry('hermes-gateway.service.d/90-eimemory-runtime.conf',b'legacy',1001)
    manifest={'schema':'eimemory.gateway_policy_retirement.v1','entries':[entry]}
    if fault=='duplicate':manifest['entries'].append(deepcopy(entry))
    elif fault=='path_escape':entry['relative_path']='../hermes-gateway.service.d/90-eimemory-runtime.conf'
    elif fault=='native_fragment':entry['relative_path']='hermes-gateway.service'
    elif fault=='custom_business':entry['relative_path']='hermes-gateway.service.d/99-user-business.conf'
    elif fault=='sidecar':entry['relative_path']='openclaw-loop-observer.service.d/90-eimemory-runtime.conf'
    elif fault=='foreign_uid':entry['uid']=1002
    elif fault=='bool_mode':entry['mode']=True
    elif fault=='large_mode':entry['mode']=0o4755
    elif fault=='bad_mode':entry['mode']='0664'
    elif fault=='bad_source':entry['source']['kind']='filename_inferred'
    elif fault=='missing_source':entry.pop('source')
    elif fault=='invalid_sha':entry['sha256']='bad'
    elif fault=='non_string_sha':entry['source']['sha256']=None
    elif fault=='bad_reference':entry['source']['reference']='before\nnot_a_source'
    elif fault=='extra_field':entry['delete_all_gateway_files']=True
    raw=module.canonical(manifest)
    if fault=='duplicate_key':raw=raw.replace(b'"mode":436',b'"mode":436,"mode":436')
    if fault=='none':assert module.gateway_manifest_entries(raw,1001)==[entry]
    else:
        with pytest.raises(ValueError):module.gateway_manifest_entries(raw,1001)


def test_gateway_retirement_cli_keeps_manifest_and_hash_paired(monkeypatch,tmp_path):
    module=installer();captured=[]
    monkeypatch.setattr(module,'install',lambda args:captured.append(args))
    argv=['installer']
    for name in ['bundle','registry-seed','runtime-settings','managed-manifest']:
        argv+=['--'+name,str(tmp_path/name),'--expected-'+name+'-sha256','a'*64]
    for name in ['knowledge','managed-key','activation-key']:argv+=['--expected-'+name+'-sha256','b'*64]
    argv+=['--gateway-retirement-manifest',str(tmp_path/'reviewed.json'),
        '--expected-gateway-retirement-sha256','c'*64]
    monkeypatch.setattr(module.sys,'argv',argv);module.main()
    assert captured[0].gateway_retirement_manifest==tmp_path/'reviewed.json'
    assert captured[0].expected_gateway_retirement_sha256=='c'*64
    for args in [SimpleNamespace(gateway_retirement_manifest=tmp_path/'reviewed.json'),
            SimpleNamespace(expected_gateway_retirement_sha256='c'*64)]:
        with pytest.raises(ValueError,match='review_required'):
            module.gateway_retirement_plan(args,1001,tmp_path)


@pytest.mark.skipif(os.name!='posix',reason='native gateway descriptor custody')
@pytest.mark.parametrize('fault',['none','stale_sha','changed_bytes','changed_mode','replacement',
    'symlink','hardlink','parent_symlink'])
def test_gateway_retirement_cas_archives_all_originals_and_preserves_business(tmp_path,monkeypatch,fault):
    module=installer();uid=os.geteuid()
    if uid==0:pytest.skip('unprivileged operator required')
    units=tmp_path/'user';dropins=units/'hermes-gateway.service.d';dropins.mkdir(parents=True);dropins.chmod(0o775)
    originals={dropins/'89-eimemory-runtime-dir.conf':b'[Service]\nExecStartPre=/old/profile-guard\n',
        dropins/'90-eimemory-runtime.conf':b'[Service]\nExecStart=/old/wrapper\n'}
    business=dropins/'99-business.conf';business.write_bytes(b'[Service]\nEnvironment=BUSINESS=1\n');business.chmod(0o664)
    for path,raw in originals.items():path.write_bytes(raw);path.chmod(0o664)
    selected=next(iter(originals))
    entries=[gateway_entry(path.relative_to(units).as_posix(),raw,uid) for path,raw in originals.items()]
    if fault=='stale_sha':entries[0]['sha256']='0'*64
    manifest=tmp_path/'manifest.json';raw=module.canonical({'schema':'eimemory.gateway_policy_retirement.v1','entries':entries})
    manifest.write_bytes(raw);args=SimpleNamespace(gateway_retirement_manifest=manifest,
        expected_gateway_retirement_sha256=sha256(raw).hexdigest())
    if fault=='symlink':
        target=tmp_path/'other';target.write_bytes(originals[selected]);selected.unlink();selected.symlink_to(target)
    elif fault=='hardlink':os.link(selected,tmp_path/'alias')
    elif fault=='parent_symlink':
        other=tmp_path/'moved';dropins.rename(other);dropins.symlink_to(other,target_is_directory=True)
    if fault in {'stale_sha','symlink','hardlink','parent_symlink'}:
        with pytest.raises((ValueError,OSError)):module.gateway_retirement_plan(args,uid,units)
        assert all(path.exists() for path in originals)
        return
    prepared,plan=module.gateway_retirement_plan(args,uid,units)
    if fault=='changed_bytes':selected.write_bytes(b'operator edit after review')
    elif fault=='changed_mode':selected.chmod(0o644)
    elif fault=='replacement':
        replacement=dropins/'replacement';replacement.write_bytes(originals[selected]);replacement.chmod(0o664)
        os.replace(replacement,selected)
    # Native temp ancestors need no production home fixture; private archive
    # creation still keeps immutable owned reads and real fsync/CAS operations.
    def fixture_directory(path,_uid,mode=0o750):
        path.mkdir(parents=True,exist_ok=True);path.chmod(mode)
    monkeypatch.setattr(module,'directory',fixture_directory)
    archive=tmp_path/'retired'
    if fault!='none':
        with pytest.raises(ValueError,match='cas_mismatch'):
            module.retire_gateway_policies(prepared,plan,units,archive,uid)
        assert all(path.exists() for path in originals)
    else:
        real_unlink=module.os.unlink;removed=[]
        def unlink(path,*args,**kwargs):
            if kwargs.get('dir_fd') is not None:
                # Every source in the batch is durable before any deletion.
                assert all((archive/source.relative_to(units)).read_bytes()==value for source,value in originals.items())
                removed.append(path)
            return real_unlink(path,*args,**kwargs)
        monkeypatch.setattr(module.os,'unlink',unlink)
        module.retire_gateway_policies(prepared,plan,units,archive,uid)
        assert len(removed)==len(originals) and all(not path.exists() for path in originals)
        result=module.gateway_retirement_evidence(archive,uid,sha256(raw).hexdigest(),units,{'units':{}})
        assert result['originals']=={entry['relative_path']:entry['sha256'] for entry in entries}
        assert all((archive/path.relative_to(units)).stat().st_mode&0o777==0o600 for path in originals)
        assert (archive/'original-identities.json').exists()
    assert business.read_bytes()==b'[Service]\nEnvironment=BUSINESS=1\n'
    assert business.stat().st_mode&0o777==0o664 and dropins.stat().st_mode&0o777==0o775


@pytest.mark.skipif(os.name!='posix',reason='native inventory and preserved collaborative gateway mode')
@pytest.mark.parametrize('fault',['none','exec','resource','dependency','umask','namespace','global_python',
    'wrong_commit','old_policy','writable_metadata'])
def test_gateway_inventory_seals_memory_metadata_without_owning_native_configuration(tmp_path,fault):
    module=installer();uid=os.geteuid()
    if uid==0:pytest.skip('unprivileged operator required')
    home=tmp_path/'operator';units=home/'.config/systemd/user';unit='hermes-gateway-hongtai.service'
    dropins=units/(unit+'.d');dropins.mkdir(parents=True);dropins.chmod(0o775)
    native=units/unit;native.write_bytes(b'[Service]\nExecStart=/native/launcher\n');native.chmod(0o664)
    own=dropins/'zzzzzz-eimemory-user-control-plane.conf';own.write_bytes(gateway_metadata('a'*40));own.chmod(0o600)
    business=dropins/'90-eimemory-runtime.conf';business.write_bytes(b'[Service]\nEnvironment=NATIVE_PROFILE=1\n');business.chmod(0o664)
    additions={'exec':b'ExecStart=/forbidden\n','resource':b'MemoryMax=1G\n',
        'dependency':b'Wants=eimemory-runtime-manager.service\n','umask':b'UMask=0077\n',
        'namespace':b'PrivateUsers=yes\n','global_python':b'Environment=PYTHONPATH=/wrong\n'}
    if fault in additions:own.write_bytes(own.read_bytes()+additions[fault])
    elif fault=='wrong_commit':own.write_bytes(gateway_metadata('b'*40))
    elif fault=='old_policy':own.write_bytes(b'[Service]\nPrivateUsers=yes\n')
    elif fault=='writable_metadata':own.chmod(0o664)
    registry={'template_hashes':{},'units':{unit:{'installed':True}},'runtime_commit':'a'*40}
    if fault=='none':
        inventory=module.installation_inventory(home,registry,uid)
        assert set(inventory)=={str(own)} and inventory[str(own)]['sha256']==sha256(own.read_bytes()).hexdigest()
        business.write_bytes(b'[Service]\nEnvironment=NATIVE_PROFILE=2\n')
        assert module.installation_inventory(home,registry,uid)==inventory
    else:
        with pytest.raises(ValueError):module.installation_inventory(home,registry,uid)
    assert native.read_bytes()==b'[Service]\nExecStart=/native/launcher\n'
    assert native.stat().st_mode&0o777==0o664 and business.stat().st_mode&0o777==0o664
    assert dropins.stat().st_mode&0o777==0o775


@pytest.mark.skipif(os.name!='posix',reason='native unit publication')
def test_even_legacy_gateway_templates_cannot_overwrite_native_unit_or_policy(tmp_path,monkeypatch):
    module=installer();uid=os.geteuid()
    if uid==0:pytest.skip('unprivileged operator required')
    units=tmp_path/'units';units.mkdir();native=units/'hermes-gateway.service'
    native.write_bytes(b'native service');native.chmod(0o664)
    dropins=units/'hermes-gateway.service.d';dropins.mkdir();dropins.chmod(0o775)
    policy=dropins/'90-eimemory-runtime.conf';policy.write_bytes(b'native policy');policy.chmod(0o664)
    def fixture_directory(path,_uid,mode=0o750):path.mkdir(parents=True,exist_ok=True);path.chmod(mode)
    monkeypatch.setattr(module,'directory',fixture_directory)
    files={'templates/hermes-gateway.service':b'old restricted template',
        'templates/eimemory-rpc.service':b'owned rpc','runtime-policy.conf':b'[Service]\nNoNewPrivileges=yes\n'}
    module.install_user_units(files,units,uid,controller_commit='a'*40,runtime_commit='b'*40)
    assert native.read_bytes()==b'native service' and policy.read_bytes()==b'native policy'
    assert native.stat().st_mode&0o777==0o664 and dropins.stat().st_mode&0o777==0o775
    assert (units/'eimemory-rpc.service.d/90-eimemory-runtime.conf').read_bytes()==files['runtime-policy.conf']


@pytest.mark.skipif(os.name!='posix',reason='native archive integrity and crash boundaries')
@pytest.mark.parametrize('fault',['none','original','identities','result','manifest','restored_policy',
    'new_metadata','bad_metadata','partial_unlink'])
def test_gateway_retirement_proof_rejects_tampering_and_incomplete_batches(tmp_path,monkeypatch,fault):
    module=installer();uid=os.geteuid()
    if uid==0:pytest.skip('unprivileged operator required')
    units=tmp_path/'user';unit='hermes-gateway.service';dropins=units/(unit+'.d');dropins.mkdir(parents=True)
    old={dropins/'05-eimemory-installation-guard.conf':b'[Unit]\nConditionPathExists=!old\n',
        dropins/'zzzzzz-eimemory-user-control-plane.conf':b'[Service]\nPrivateUsers=yes\n'}
    entries=[];plan=[]
    for path,raw in old.items():
        path.write_bytes(raw);path.chmod(0o664)
        entry=gateway_entry(path.relative_to(units).as_posix(),raw,uid)
        original,identity=module.gateway_original(path,uid)
        entries.append(entry);plan.append(dict(entry=entry,original=original,identity=identity))
    raw=module.canonical({'schema':'eimemory.gateway_policy_retirement.v1','entries':entries})
    def fixture_directory(path,_uid,mode=0o750):path.mkdir(parents=True,exist_ok=True);path.chmod(mode)
    monkeypatch.setattr(module,'directory',fixture_directory)
    archive=tmp_path/'archive';real_unlink=module.os.unlink;count=0
    def unlink(path,*args,**kwargs):
        nonlocal count
        if kwargs.get('dir_fd') is not None:
            count+=1
            if fault=='partial_unlink' and count==2:raise OSError('interrupted retirement')
        return real_unlink(path,*args,**kwargs)
    monkeypatch.setattr(module.os,'unlink',unlink)
    if fault=='partial_unlink':
        with pytest.raises(OSError,match='interrupted'):
            module.retire_gateway_policies(raw,plan,units,archive,uid)
        assert all((archive/path.relative_to(units)).read_bytes()==value for path,value in old.items())
        assert len([path for path in old if path.exists()])==1 and not (archive/'complete.json').exists()
    else:module.retire_gateway_policies(raw,plan,units,archive,uid)
    if fault=='original':(archive/entries[0]['relative_path']).write_bytes(b'changed original')
    elif fault=='identities':(archive/'original-identities.json').write_bytes(b'[]')
    elif fault=='result':(archive/'complete.json').write_bytes(b'{}')
    elif fault=='manifest':(archive/'reviewed-manifest.json').write_bytes(raw+b' ')
    elif fault=='restored_policy':next(iter(old)).write_bytes(next(iter(old.values())))
    elif fault in {'new_metadata','bad_metadata'}:
        (dropins/'zzzzzz-eimemory-user-control-plane.conf').write_bytes(gateway_metadata('a'*40)
            +(b'PrivateUsers=yes\n' if fault=='bad_metadata' else b''))
    registry={'units':{unit:{'installed':True}},'runtime_commit':'a'*40}
    verify=lambda:module.gateway_retirement_evidence(archive,uid,sha256(raw).hexdigest(),units,registry)
    if fault in {'none','new_metadata'}:
        assert verify()['manifest_sha256']==sha256(raw).hexdigest()
    else:
        with pytest.raises((ValueError,OSError)):verify()
