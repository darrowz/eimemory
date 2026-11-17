"""Current enrollment keeps origin, file custody and native restoration distinct."""
from copy import deepcopy
from hashlib import sha256
import importlib.util
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from eimemory.contracts import current_user_enrollment as epoch
from eimemory.contracts import hermes_topology as topology

ROOT=Path(__file__).resolve().parents[1]
POSIX=pytest.mark.skipif(os.name!='posix',reason='Unix file/UID/native-manager boundary')


def standalone(name):
    source=ROOT/'deploy'/(name+'.py')
    spec=importlib.util.spec_from_file_location('fresh_test_'+name,source)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    return module


@pytest.fixture
def file_operator(monkeypatch):
    """Place a fake passwd home below pytest's root-owned sticky /var/tmp.

    Production writes use the fixed real passwd home. The existing installer
    deliberately excludes /tmp in directory(); retain owner/link/mode checks
    here and admit only the kernel's safe sticky ancestor for test placement.
    File writes, fsync, archive reads, CAS, journal and rollback stay real.
    """
    op=standalone('install_user_runtime_manager')
    def directory(path,uid,mode=0o750):
        import stat
        path=Path(path);path.mkdir(parents=True,exist_ok=True,mode=mode)
        for parent in (path,*path.parents):
            info=parent.lstat();sticky=parent!=path and info.st_uid==0 and info.st_mode&stat.S_ISVTX
            if (not stat.S_ISDIR(info.st_mode) or info.st_uid not in {0,uid}
                    or info.st_mode&0o022 and not sticky):raise ValueError('fixture_directory_untrusted')
        if path.stat().st_uid!=uid:raise ValueError('fixture_directory_owner_invalid')
        path.chmod(mode)
    monkeypatch.setattr(op,'directory',directory)
    return op


def receipt_fixture():
    key=bytes(range(32));registry={'runtime_uid':1001,'runtime_gid':1001,'runtime_commit':'a'*40}
    native_raw=b'{"native":"fixture"}';authority_raw=b'{"authority":"fixture"}'
    logical_raw=b'{"topology":"fixture"}';topology_intent_raw=b'{"origin":"current"}'
    intent={'schema':epoch.INTENT_SCHEMA,'enrollment_id':'b'*32,'runtime_uid':1001,'runtime_gid':1001,
        'current_commit':'a'*40,'bundle_sha256':'c'*64,'registry_sha256':epoch.digest(registry),
        'runtime_settings_sha256':'d'*64,'unit_scopes_sha256':'e'*64,
        'native_snapshot_sha256':sha256(native_raw).hexdigest(),'authority_snapshot_sha256':sha256(authority_raw).hexdigest(),
        'topology_sha256':sha256(logical_raw).hexdigest(),'authorization':'explicit_user_operator',
        'historical_custody_recovered':False,'autonomous_repair_credit':False}
    plan={'schema':epoch.PLAN_SCHEMA,**{name:intent[name] for name in
        ('enrollment_id','runtime_uid','runtime_gid','current_commit','bundle_sha256','registry_sha256',
         'native_snapshot_sha256','authority_snapshot_sha256','topology_sha256','authorization',
         'historical_custody_recovered','autonomous_repair_credit')},'operator_intent_sha256':epoch.digest(intent),
        'activation_key_sha256':sha256(key).hexdigest(),'before_inventory_sha256':'f'*64,'publication_sha256':'0'*64}
    payload={'schema':epoch.RECEIPT_SCHEMA,'enrollment_id':'b'*32,'runtime_uid':1001,'runtime_gid':1001,
        'origin':'current_native_user_operator','created_at':'2026-11-18T00:00:00+00:00','plan_sha256':epoch.digest(plan),
        'topology_intent_sha256':sha256(topology_intent_raw).hexdigest(),'publication_sha256':'0'*64,
        'authorization':'explicit_user_operator','historical_custody_recovered':False,'release_accepted':False,
        'autonomous_repair_credit':False}
    kwargs=dict(plan=plan,intent=intent,registry=registry,topology_raw=logical_raw,
        topology_intent_raw=topology_intent_raw,native_raw=native_raw,authority_raw=authority_raw)
    return epoch.sign_completion(payload,key),key,kwargs


def test_typed_current_receipt_has_no_history_acceptance_or_autonomous_credit():
    value,key,kwargs=receipt_fixture()
    result=epoch.validate_completion(value,key,**kwargs)
    assert result['origin']=='current_native_user_operator'
    assert result['historical_custody_recovered'] is result['release_accepted'] is result['autonomous_repair_credit'] is False


@pytest.mark.parametrize('fault',['key','key_type','signature','naive_time','old_origin','historical','accepted','autonomous',
    'operator','operator_uid','plan_uid','plan_nonce','plan_epoch','operator_epoch','registry','native','authority','topology','topology_intent',
    'intent_bundle','intent_registry','intent_native','extra_field'])
def test_current_receipt_rejects_cross_epoch_or_unbound_material(fault):
    value,key,kwargs=receipt_fixture()
    if fault=='key':key=b'x'*32
    elif fault=='key_type':key='not key bytes'
    elif fault=='signature':value['signature']='1'*64
    elif fault=='naive_time':value['payload']['created_at']='2026-11-18T00:00:00'
    elif fault=='old_origin':value['payload']['origin']='historical_migration'
    elif fault in {'historical','accepted','autonomous'}:
        value['payload'][{'historical':'historical_custody_recovered','accepted':'release_accepted','autonomous':'autonomous_repair_credit'}[fault]]=True
    elif fault=='operator':kwargs['intent']['authorization']='native_health'
    elif fault=='operator_uid':kwargs['intent']['runtime_uid']=True
    elif fault=='plan_uid':kwargs['plan']['runtime_uid']=1002
    elif fault=='plan_nonce':kwargs['plan']['enrollment_id']='f'*32
    elif fault=='plan_epoch':kwargs['plan']['current_commit']='f'*40
    elif fault=='operator_epoch':kwargs['intent']['current_commit']='f'*40
    elif fault=='registry':kwargs['registry']['runtime_commit']='f'*40
    elif fault in {'native','authority','topology','topology_intent'}:kwargs[fault+'_raw']=b'changed'
    elif fault.startswith('intent_'):
        name={'intent_bundle':'bundle','intent_registry':'registry','intent_native':'native_snapshot'}[fault]
        kwargs['intent'][name+'_sha256']='f'*64
    elif fault=='extra_field':value['payload']['unreviewed']=True
    with pytest.raises(ValueError):epoch.validate_completion(value,key,**kwargs)


def test_current_topology_source_is_distinct_from_historical_job_custody():
    value={'schema':epoch.TOPOLOGY_SCHEMA,'intent_id':'a'*32,'runtime_uid':1001,'runtime_gid':1001,
        'mode':'single_native_gateway','main_gateway':'hermes-gateway.service','retired_gateways':['hermes-gateway-old.service'],
        'source':{'enrollment_id':'a'*32,'plan_sha256':'b'*64,'registry_sha256':'c'*64,'base_commit':'d'*40},
        'topology_sha256':'e'*64,'native_snapshot_sha256':'f'*64,'authorization':'explicit_user_operator',
        'autonomous_repair_credit':False}
    topology.validate_operator_topology_intent(value,main='hermes-gateway.service',retired=value['retired_gateways'],uid=1001,gid=1001)
    value['schema']='eimemory.hermes_topology_intent.v1'
    with pytest.raises(ValueError):topology.validate_operator_topology_intent(value,main='hermes-gateway.service',retired=value['retired_gateways'],uid=1001,gid=1001)


@POSIX
@pytest.mark.parametrize('fault',['none','before_epoch','after_epoch','before_key','after_key','before_registry','after_registry','foreign_change'])
def test_real_files_transaction_recovers_each_publication_boundary(tmp_path,monkeypatch,file_operator,fault):
    if os.geteuid()==0:pytest.skip('unprivileged account required')
    module=standalone('runtime_manager_current_user');op=file_operator
    uid=os.geteuid();gid=os.getegid();archive=tmp_path/'private';archive.mkdir(mode=0o700)
    root=tmp_path/'product';root.mkdir(mode=0o750)
    existing=root/'policy.json';op.write(existing,b'original operator policy',uid,0o640)
    paths=[existing,root/'epoch.json',root/'activation-signing.key',root/'runtime-manager.v1.json']
    before=existing.read_bytes();put={str(path):module.desired(b'new-'+path.name.encode(),uid,gid,
        0o600 if path.name=='activation-signing.key' else 0o640) for path in paths}
    transaction=module.Publication(op,archive,uid,gid,put);transaction.save()
    original_write=op.write;injected=False
    def write(path,raw,owner,mode=0o640):
        nonlocal injected
        label={'epoch.json':'epoch','activation-signing.key':'key','runtime-manager.v1.json':'registry'}.get(Path(path).name)
        if not injected and fault=='before_'+str(label):injected=True;raise OSError('injected write interruption')
        original_write(path,raw,owner,mode)
        if not injected and fault=='after_'+str(label):injected=True;raise OSError('injected fsync return interruption')
    monkeypatch.setattr(op,'write',write)
    try:
        for path in paths:transaction.apply(path)
    except OSError:pass
    if fault=='foreign_change':
        original_write(existing,b'unrelated operator change',uid)
        with pytest.raises(ValueError,match='foreign_change_preserved'):transaction.rollback()
        assert existing.read_bytes()==b'unrelated operator change'
        return
    # Resume from actual archived journal and bytes, simulating a new process.
    resumed=module.Publication(op,archive,uid,gid,put,journal=op.strict((archive/'journal.json').read_bytes()))
    resumed.rollback()
    assert existing.read_bytes()==before
    assert all(not path.exists() for path in paths[1:])
    assert op.strict((archive/'journal.json').read_bytes())['phase']=='rolled_back'


@POSIX
@pytest.mark.parametrize('damage',['unknown_key','unknown_ledger','unknown_registry','unknown_code','foreign_cpa','symlink'])
def test_fresh_preflight_preserves_unknown_existing_material(tmp_path,file_operator,damage):
    if os.geteuid()==0:pytest.skip('unprivileged account required')
    module=standalone('runtime_manager_current_user');op=file_operator
    home=tmp_path/'account';authority=home/'.config/eimemory/control-plane'
    authority.mkdir(parents=True,mode=0o750)
    targets={'unknown_key':authority/'managed-incident-proof.key',
        'unknown_ledger':home/'.local/state/eimemory/control-plane/managed-incident-receipts/knowledge.sqlite',
        'unknown_registry':authority/'runtime-manager.v1.json','unknown_code':home/'.local/lib/eimemory/control-plane/custom.py',
        'foreign_cpa':authority/'controlled-production-acceptance/policy.json','symlink':authority/'unknown.json'}
    target=targets[damage];target.parent.mkdir(parents=True,exist_ok=True,mode=0o750)
    op.write(target,b'original material',os.geteuid(),0o600)
    if damage=='symlink':
        original=target.with_suffix('.original');target.rename(original);target.symlink_to(original)
    with pytest.raises(ValueError):module.validate_empty_coordinates(op,home,{},os.geteuid())
    assert target.read_bytes()==b'original material'


@POSIX
def test_gateway_publication_retains_native_collaborative_directory_mode(tmp_path,file_operator):
    if os.geteuid()==0:pytest.skip('unprivileged account required')
    module=standalone('runtime_manager_current_user');op=file_operator
    policy=standalone('runtime_manager_user_policy');uid=os.geteuid();gid=os.getegid()
    archive=tmp_path/'private';archive.mkdir(mode=0o700)
    directory=tmp_path/'hermes-gateway.service.d';directory.mkdir(mode=0o775);directory.chmod(0o775)
    target=directory/'zzzzzz-eimemory-user-control-plane.conf'
    target.write_bytes(b'old memory metadata');target.chmod(0o600)
    tx=module.Publication(op,archive,uid,gid,{str(target):module.desired(b'new memory metadata',uid,gid,0o600)},
                          gateway_writer=policy.write_user_metadata)
    tx.save();tx.apply(target);assert target.read_bytes()==b'new memory metadata'
    tx.rollback();assert target.read_bytes()==b'old memory metadata'
    assert directory.stat().st_mode&0o777==0o775


@POSIX
def test_native_topology_rejects_live_retired_and_autostart_before_mutation(tmp_path,monkeypatch):
    if os.geteuid()==0:pytest.skip('unprivileged account required')
    from eimemory.contracts import runtime_manager as contract
    module=standalone('runtime_manager_current_user');op=standalone('install_user_runtime_manager')
    uid=os.geteuid();gid=os.getegid();retired='hermes-gateway-old.service'
    logical={'runtime_uid':uid,'runtime_gid':gid,'owner_unit':'hermes-gateway.service','retired_gateways':[retired]}
    def show(unit,props,env):
        value={key:'' for key in module.NATIVE_PROPERTIES}
        value.update(Id=unit,LoadState='not-found',ActiveState='inactive',MainPID='0',ControlPID='0',UnitFileState='not-found')
        if unit in {'eimemory-rpc.service','hermes-gateway.service',retired}:
            value.update(LoadState='loaded',ActiveState='active',MainPID='100',InvocationID='b'*32,ExecStart='synthetic fixture command',
                ControlGroup=f'/user.slice/user-{uid}.slice/user@{uid}.service/app.slice/'+unit,UnitFileState='enabled')
        return {key:value[key] for key in props}
    monkeypatch.setattr(op,'native_state',show)
    monkeypatch.setattr(module,'process_environment',lambda *a:pytest.fail('retired gateway passed native gate'))
    with pytest.raises(ValueError,match='retired_gateway_live'):module.native_snapshot(op,{},contract,logical,tmp_path)


def test_operator_entry_remains_source_reviewed_and_autoevolution_forbidden():
    from eimemory.governance.evolution import code_evolution_path_policy as paths
    text=(ROOT/'deploy/install_user_runtime_manager.py').read_text()
    assert "files['runtime_manager_current_user.py']" in text
    assert 'initialize-current-user' in text and 'inspect-current-user' in text and 'recover-current-user' in text
    assert 'deploy/runtime_manager_*.py' in (ROOT/'eimemory/governance/evolution/code_evolution_path_policy.py').read_text(encoding='utf-8')


@POSIX
@pytest.mark.parametrize('fault',['none','controller_rejected','after_epoch','after_key','after_registry',
    'kill_after_epoch','kill_after_key','kill_after_registry','kill_after_controller_start','kill_after_resume',
    'kill_before_plan','kill_after_staged_key','kill_after_private_journal','kill_after_barrier',
    'kill_after_resume_with_state','kill_after_resume_foreign_job'])
def test_complete_entry_uses_real_publication_and_bridge_receipt_then_restores_or_rolls_back(
        tmp_path,monkeypatch,file_operator,fault):
    """Mock the native manager boundary, not the archive/Bridge/file contract."""
    if os.geteuid()==0:pytest.skip('unprivileged account required')
    import pwd
    module=standalone('runtime_manager_current_user');op=file_operator
    original_account=pwd.getpwuid(os.geteuid());uid=original_account.pw_uid;gid=original_account.pw_gid
    home=tmp_path/'account';home.mkdir(mode=0o750)
    account=SimpleNamespace(pw_uid=uid,pw_gid=gid,pw_name=original_account.pw_name,pw_dir=str(home))
    monkeypatch.setattr(pwd,'getpwuid',lambda number:account if number==uid else original_account)
    bundle=standalone('build_runtime_manager_bundle').build_user(ROOT,'a'*40,account=account)
    manifest=op.strict(bundle.pop('manifest.json'))
    # Same pure registry contract as the real entry, compiled in user mode.
    stage=tmp_path/'review';stage.mkdir(mode=0o750)
    for name in ('user_control_plane.py','runtime_manager_contract.py'):op.write(stage/name,bundle[name],uid)
    with op.user_operator_profile():
        contract=op.execute_reviewed_module('fixture_registry',stage/'runtime_manager_contract.py',bundle['runtime_manager_contract.py'])
    authority=home/'.config/eimemory/control-plane';op.directory(authority,uid)
    logical={'schema':topology.SCHEMA,'mode':'single_native_gateway','runtime_uid':uid,'runtime_gid':gid,
        'owner_unit':'hermes-gateway.service','runtime_units':list(topology.RUNTIME_UNITS),
        'profiles':[{'profile_name':name,'hermes_home':str(home/('.hermes-'+name)),'agent_id':name,
                     'identity_status':'unbound','capture_scopes':[]} for name in ('default','hongtai','xiaomage')],
        'retired_gateways':['hermes-gateway-default.service','hermes-gateway-hongtai.service','hermes-gateway-xiaomage.service']}
    logical_raw=topology.canonical_bytes(topology.validate_topology(logical))
    prior={'schema':'eimemory.hermes_topology_intent.v1','origin':'native pre-enrollment fixture'}
    prior_raw=op.canonical(prior)
    op.write(authority/'hermes-topology.v1.json',logical_raw,uid)
    op.write(authority/'hermes-topology-intent.v1.json',prior_raw,uid)
    unit_home=home/'.config/systemd/user';op.directory(unit_home,uid)
    original_unit=b'[Service]\nExecStart=/original/native/rpc\n'
    op.write(unit_home/'eimemory-rpc.service',original_unit,uid,0o644)
    names=sorted(contract.REGISTERED_UNITS|set(logical['retired_gateways']))
    native={'schema':'eimemory.current_user_native_snapshot.v1','runtime_uid':uid,'runtime_gid':gid,
        'authority_environment':{key:('user' if key=='EIMEMORY_CONTROL_PLANE' else None) for key in module.SOURCE_ENV},
        'protected_intent_sha256':sha256(prior_raw).hexdigest(),'units':{}}
    for name in names:
        value={key:'' for key in module.NATIVE_PROPERTIES}
        value.update(Id=name,LoadState='not-found',ActiveState='inactive',SubState='dead',UnitFileState='not-found',
                     MainPID='0',ControlPID='0')
        if name in {'eimemory-rpc.service','hermes-gateway.service'}:
            value.update(LoadState='loaded',ActiveState='active',SubState='running',UnitFileState='enabled',MainPID='100',
                InvocationID='b'*32,ExecStart='synthetic fixture command',
                ControlGroup=f'/user.slice/user-{uid}.slice/user@{uid}.service/app.slice/'+name)
        native['units'][name]={key:value[key] for key in module.native_properties(name,loaded=value['LoadState']=='loaded')}
    live=deepcopy(native['units']);operations=[];interruption_enabled=True
    for value in live.values():value['ExecMainStartTimestampMonotonic']='123'
    live['eimemory-runtime-manager.service']=dict(Id='eimemory-runtime-manager.service',LoadState='not-found',
        ActiveState='inactive',MainPID='0',ControlPID='0',ControlGroup='',UnitFileState='not-found')
    def native_state(name,properties,environment):return {key:live[name][key] for key in properties}
    monkeypatch.setattr(op,'native_state',native_state)
    def systemctl(environment,*arguments):
        operations.append(arguments)
        if arguments[0] not in {'start','stop'}:return
        for name in arguments[1:]:
            live[name].update(ActiveState='active' if arguments[0]=='start' else 'inactive',
                MainPID='100' if arguments[0]=='start' else '0',ControlPID='0',
                ControlGroup=(f'/user.slice/user-{uid}.slice/user@{uid}.service/app.slice/'+name if arguments[0]=='start' else ''))
        if interruption_enabled and arguments[0]=='start' and ('eimemory-runtime-manager.service' in arguments and fault=='kill_after_controller_start'
                or 'eimemory-rpc.service' in arguments and fault.startswith('kill_after_resume')):
            os._exit(78)
    monkeypatch.setattr(module,'systemctl',systemctl)
    scopes={'eimemory-rpc.service':dict(tenant_id='fixture',agent_id='default',workspace_id='fixture',user_id=account.pw_name)}
    registry=module.registry(op,contract,bundle,manifest,account,native,scopes,str(tmp_path/'repository'))
    authorities={'schema':'eimemory.current_user_authority_snapshot.v1','sources':{},'publication':{},'source_uid':uid,
        'source_control_plane':'user','identity_roots':{},'identity_validation':{'identity':{},'repository_root':registry['repository_root']}}
    authority_files={}
    if fault in {'kill_after_resume_with_state','kill_after_resume_foreign_job'}:
        from eimemory import identity_authority as api
        identity_directory=authority/'identity';policy={'schema':'identity_operator_policy.v2','requester_uid':uid,
            'owner_scope':scopes['eimemory-rpc.service'],'source_ids':['hermes'],'retained_policy_digests':[]}
        original_event={'operation':'authorize','event_id':'before-enrollment','requester_uid':uid,'previous_hash':'',
            'policy_digest':api.digest(policy),'manifest':{'owner_scope':policy['owner_scope'],'source_id':'hermes'},
            'manifest_digest':api.digest({'owner_scope':policy['owner_scope'],'source_id':'hermes'})}
        original_event['event_hash']=api.digest(original_event)
        op.write(identity_directory/'policy.json',op.canonical(policy)+b'\n',uid)
        op.write(identity_directory/'requests.jsonl',op.canonical(original_event)+b'\n',uid,0o600)
        authority_files={str(path):module.record(path,owners={uid}) for path in identity_directory.iterdir()}
        authorities.update(sources=deepcopy(authority_files),publication=deepcopy(authority_files),identity_roots={'identity':str(identity_directory)})
    publication=module.publication(op,bundle,registry,b'{}',home,authority_files,enrollment_id='c'*32)
    before={name:module.record(name,owners={uid}) for name in publication}
    before[str(authority/'hermes-topology-intent.v1.json')]=module.record(authority/'hermes-topology-intent.v1.json',owners={uid})
    before[str(home/'.local/state/eimemory/control-plane/activation-signing.key')]=None
    native['publication_before_sha256']=contract.digest(before)
    intent={'schema':epoch.INTENT_SCHEMA,'enrollment_id':'c'*32,'runtime_uid':uid,'runtime_gid':gid,'current_commit':'a'*40,
        'bundle_sha256':'d'*64,'registry_sha256':contract.digest(registry),'runtime_settings_sha256':'e'*64,
        'unit_scopes_sha256':'f'*64,'native_snapshot_sha256':contract.digest(native),
        'authority_snapshot_sha256':contract.digest(authorities),'topology_sha256':sha256(logical_raw).hexdigest(),
        'authorization':'explicit_user_operator','historical_custody_recovered':False,'autonomous_repair_credit':False}
    data=dict(intent=intent,registry=registry,native=native,authority=authorities,topology_raw=logical_raw,
        prior_intent_raw=prior_raw,environment={},release=tmp_path/'releases'/('a'*40),publication=publication,identity_roots={})
    intent_path=tmp_path/'intent.json';op.write(intent_path,op.canonical(intent),uid)
    args=SimpleNamespace(operation='initialize-current-user',enrollment_id='c'*32,expected_bundle_sha256='d'*64,
        operator_intent=intent_path,expected_operator_intent_sha256=sha256(intent_path.read_bytes()).hexdigest())
    monkeypatch.setattr(module,'prepare',lambda *a:deepcopy(data))
    # The source-authority product helper is separately exercised with real
    # journals below. This empty-authority fixture has no policy to migrate.
    monkeypatch.setattr(module,'verify_authority',lambda *a:None)
    def controller(*a):
        if fault=='controller_rejected':raise ValueError('current_enrollment_controller_process_unverified')
        return {'kernel_peer_fixture':True,'registry_generation':registry['generation']}
    monkeypatch.setattr(module,'verify_controller',controller)
    original_execute=op.execute_reviewed_module
    def execute(name,path,raw):
        result=original_execute(name,path,raw)
        if name=='fresh_bridge':
            result.RELEASES=tmp_path/'releases'
            result.CURRENT=tmp_path/'current'
            def command(_self,arguments):
                unit=next(value for value in arguments if value in live)
                properties=next(value.split('=',1)[1].split(',') for value in arguments if value.startswith('--property='))
                return '\n'.join(key+'='+live[unit][key] for key in properties)+'\n'
            monkeypatch.setattr(result.Bridge,'_command',command)
        return result
    monkeypatch.setattr(op,'execute_reviewed_module',execute)
    for installed,relative in (('current_user_enrollment_contract.py','eimemory/contracts/current_user_enrollment.py'),
            ('runtime_manager_bridge.py','deploy/runtime_manager_bridge.py'),('hermes_topology_contract.py','eimemory/contracts/hermes_topology.py'),
            ('user_control_plane.py','eimemory/contracts/user_control_plane.py')):
        op.write(data['release']/relative,bundle[installed],uid,0o644)
    (tmp_path/'current').symlink_to(data['release'],target_is_directory=True)
    original_write=op.write;interrupted=False
    def write(path,raw,owner,mode=0o640):
        nonlocal interrupted
        if fault=='kill_before_plan' and Path(path).name=='plan.json':os._exit(78)
        original_write(path,raw,owner,mode)
        if (fault=='kill_after_staged_key' and Path(path).name=='activation-key.original'
                or fault=='kill_after_private_journal' and Path(path).name=='journal.json'):
            os._exit(78)
        labels={'installation.v1.json':'after_epoch','activation-signing.key':'after_key','runtime-manager.v1.json':'after_registry'}
        if not interrupted and labels.get(Path(path).name)==fault.removeprefix('kill_'):
            interrupted=True
            if fault.startswith('kill_'):os._exit(78)
            raise OSError('injected publication interruption')
    monkeypatch.setattr(op,'write',write)
    original_barrier=module.ensure_barrier
    def ensure_barrier(*arguments):
        original_barrier(*arguments)
        if interruption_enabled and fault=='kill_after_barrier':os._exit(78)
    monkeypatch.setattr(module,'ensure_barrier',ensure_barrier)
    if fault=='none':
        result=module.run(args,op,bundle,manifest)
        assert result['complete'] and result['release_accepted'] is result['historical_custody_recovered'] is False
        assert (authority/'runtime-manager.v1.json').is_file()
        archive=home/'.local/state/eimemory/control-plane/current-enrollments'/('c'*32)
        receipt=op.strict((archive/'complete.json').read_bytes())
        assert receipt['payload']['origin']=='current_native_user_operator'
        assert op.strict((archive/'journal.json').read_bytes())['phase']=='completed'
        key_before=(archive/'activation-key.original').read_bytes()
        monkeypatch.setattr(module,'prepare',lambda *a:pytest.fail('completed nonce re-ran native preflight'))
        assert module.run(args,op,bundle,manifest)['already_enrolled'] is True
        assert (archive/'activation-key.original').read_bytes()==key_before
        with op.user_operator_profile():
            bridge_module=execute('fresh_bridge',home/'.local/lib/eimemory/control-plane/runtime_manager_bridge.py',bundle['runtime_manager_bridge.py'])
        bridge=bridge_module.Bridge(contract,registry=authority/'runtime-manager.v1.json',state=archive.parents[1])
        request={'schema':'eimemory.manual_release_permit.v2','operation':'deploy','controller_commit':'a'*40,
            'validation_controller_commit':'b'*40,'base_commit':'a'*40,'candidate_commit':'b'*40,'target_commit':'b'*40,
            'reviewed_attempt_id':'e'*32,'scope':scopes['eimemory-rpc.service']}
        job_key=contract.digest(request)
        record={'job_key':job_key,'unit':contract.job_unit(job_key),'manual_release':True,'request':request,
            'registry':registry,'active_before':['eimemory-rpc.service','hermes-gateway.service'],
            'enabled_before':{'eimemory-rpc.service':'enabled','hermes-gateway.service':'enabled'}}
        plan=bridge.restore_plan(registry,record,create=True)
        assert plan['active_units']==record['active_before'] and plan['retired_units']==sorted(logical['retired_gateways'])
        assert bridge.record(job_key)['restore_plan']==plan
        # A valid birth HMAC cannot excuse a changed current controller source.
        changed=data['release']/'eimemory/contracts/current_user_enrollment.py'
        changed.write_bytes(changed.read_bytes()+b'\n# changed after enrollment\n')
        with pytest.raises(ValueError,match='controller_changed'):module.run(args,op,bundle,manifest)
        assert (archive/'activation-key.original').read_bytes()==key_before
    else:
        if fault.startswith('kill_'):
            child=os.fork()
            if child==0:
                try:module.run(args,op,bundle,manifest)
                except BaseException:os._exit(79)
                os._exit(0)
            assert os.waitpid(child,0)[1]==78<<8
            interruption_enabled=False
            archive=home/'.local/state/eimemory/control-plane/current-enrollments'/('c'*32)
            if not archive.exists():
                stages=list(archive.parent.glob('.preparing-*'));assert len(stages)==1
                key=stages[0]/'activation-key.original';saved_key=key.read_bytes() if key.exists() else None
                with pytest.raises(ValueError,match='unpublished_stage_preserved'):module.run(args,op,bundle,manifest)
                recovery=SimpleNamespace(operation='recover-current-user',enrollment_id='c'*32,
                    expected_bundle_sha256='d'*64,expected_operator_intent_sha256=args.expected_operator_intent_sha256)
                assert module.run(recovery,op,bundle,manifest)['outcome']=='unpublished_stage_abandoned'
                assert module.run(recovery,op,bundle,manifest)['already_abandoned'] is True
                abandoned=list(archive.parent.glob('.abandoned-*'));assert len(abandoned)==1
                if saved_key is not None:assert (abandoned[0]/'activation-key.original').read_bytes()==saved_key
                with pytest.raises(ValueError,match='nonce_consumed'):module.run(args,op,bundle,manifest)
            else:
                saved_key=(archive/'activation-key.original').read_bytes()
                with pytest.raises(ValueError,match='nonce_consumed'):module.run(args,op,bundle,manifest)
                assert (archive/'activation-key.original').read_bytes()==saved_key
                if fault=='kill_after_controller_start' or fault.startswith('kill_after_resume'):
                    live['eimemory-runtime-manager.service'].update(ActiveState='active',MainPID='100',ControlGroup='/fixture/controller')
                recovery=SimpleNamespace(operation='recover-current-user',enrollment_id='c'*32,
                    expected_bundle_sha256='d'*64,expected_plan_sha256=sha256((archive/'plan.json').read_bytes()).hexdigest())
                if fault in {'kill_after_resume_with_state','kill_after_resume_foreign_job'}:
                    import subprocess
                    import sys
                    from eimemory import identity_authority as api
                    # A lawful append after serving resumed must survive. The
                    # original event bytes and chain remain the exact prefix.
                    identity_journal=identity_directory/'requests.jsonl';prefix_raw=identity_journal.read_bytes()
                    later={**original_event,'event_id':'after-serving-resumed','previous_hash':original_event['event_hash']}
                    later.pop('event_hash');later['event_hash']=api.digest(later)
                    with identity_journal.open('ab') as stream:stream.write(op.canonical(later)+b'\n');stream.flush();os.fsync(stream.fileno())
                    grown=identity_journal.read_bytes();assert grown.startswith(prefix_raw)
                    with op.user_operator_profile():
                        bridge_module=execute('fresh_bridge',home/'.local/lib/eimemory/control-plane/runtime_manager_bridge.py',bundle['runtime_manager_bridge.py'])
                    proc=tmp_path/'proc';process=proc/'100';op.directory(process,uid)
                    op.write(process/'status',('Uid: '+(' '.join([str(uid)]*4))+'\nNoNewPrivs: 1\n').encode(),uid)
                    op.write(process/'cgroup',('0::'+live['eimemory-rpc.service']['ControlGroup']+'\n').encode(),uid)
                    op.write(process/'stat',('100 (rpc) '+' '.join(['0']*19+['99'])+'\n').encode(),uid)
                    op.write(proc/'sys/kernel/random/boot_id',b'12345678-1234-1234-1234-123456789abc\n',uid)
                    bridge=bridge_module.Bridge(contract,registry=authority/'runtime-manager.v1.json',state=archive.parents[1],proc=proc)
                    assert bridge.recover_failed_worker() is False
                    bridge.activation_cause=lambda *a:{'trigger_kind':'unknown'}
                    reference=bridge.activation_attestation(registry,100,uid)
                    receipt_file=archive.parents[1]/'activation-receipts'/(reference['receipt_id']+'.json')
                    receipt_raw=receipt_file.read_bytes();assert bridge.verify_activation(reference['receipt_id'],reference['digest'])['signature_verified']
                    assert not (archive.parents[1]/'managed-incident-receipts/knowledge.sqlite').exists()
                    dynamic_process=proc/'101';op.directory(dynamic_process,uid)
                    op.write(dynamic_process/'status',(process/'status').read_bytes(),uid)
                    op.write(dynamic_process/'stat',('101 (worker) '+' '.join(['0']*19+['100'])+'\n').encode(),uid)
                    op.write(dynamic_process/'cgroup',(f'0::/user.slice/user-{uid}.slice/user@{uid}.service/app.slice/'+contract.job_unit('e'*64)+'\n').encode(),uid)
                    with pytest.raises(ValueError,match='manager_current_enrollment_pending'):
                        bridge.activation_attestation(registry,101,uid)
                    original_registry_raw=(authority/'runtime-manager.v1.json').read_bytes()
                    for operation in (lambda:bridge.submit(registry,'e'*64),
                            lambda:bridge.validate_job(registry,'e'*64,'receipt',100,uid),
                            lambda:bridge.transition(registry,'e'*64,'activate',100,uid),
                            lambda:bridge.publish_runtime(registry,{},'b'*40,activate=True),
                            lambda:bridge.job_status(registry,'e'*64),
                            lambda:bridge.validation_status(registry,'e'*64,'receipt'),
                            lambda:bridge.fused_closure(registry,'begin',100,uid),
                            lambda:bridge.restore_fused(registry,{}),
                            lambda:bridge.dispatch({'schema':contract.REQUEST_SCHEMA,'operation':'submit_job',
                                'registry_generation':registry['generation'],'job_key':'e'*64},pid=100,uid=uid)):
                        with pytest.raises(ValueError,match='manager_current_enrollment_pending'):operation()
                    assert (authority/'runtime-manager.v1.json').read_bytes()==original_registry_raw
                    current_intent=authority/'hermes-topology-intent.v1.json';current_intent_raw=current_intent.read_bytes()
                    current_intent.unlink()
                    with pytest.raises(ValueError,match='manager_current_enrollment_state_invalid'):bridge.publish_runtime(registry,{},'b'*40,activate=True)
                    op.write(current_intent,prior_raw,uid)
                    with pytest.raises(ValueError,match='manager_current_enrollment_state_invalid'):bridge.publish_runtime(registry,{},'b'*40,activate=True)
                    op.write(current_intent,current_intent_raw,uid)
                    selector_file=authority/'current-user-enrollment.v1.json';selector_raw=selector_file.read_bytes();selector_file.unlink()
                    with pytest.raises(ValueError,match='manager_current_enrollment_state_invalid'):bridge.publish_runtime(registry,{},'b'*40,activate=True)
                    op.write(selector_file,selector_raw,uid)
                    pointer=archive.parents[1]/'active-transition.json';foreign_pointer=b'{"job_key":"foreign","complete":false}'
                    op.write(pointer,foreign_pointer,uid)
                    native_before=len(operations)
                    with pytest.raises(ValueError,match='manager_current_enrollment_pending'):bridge.recover_failed_worker()
                    assert pointer.read_bytes()==foreign_pointer and len(operations)==native_before
                    pointer.unlink()
                    assert bridge.dispatch({'schema':contract.REQUEST_SCHEMA,'operation':'unit_state',
                        'registry_generation':registry['generation'],'unit':'eimemory-rpc.service','properties':['LoadState']},
                        pid=100,uid=uid)=={'state':{'LoadState':'loaded'}}
                    request={'schema':'eimemory.manual_release_permit.v2','operation':'deploy','controller_commit':'a'*40,
                        'validation_controller_commit':'b'*40,'base_commit':'a'*40,'candidate_commit':'b'*40,'target_commit':'b'*40,
                        'reviewed_attempt_id':'f'*32,'scope':scopes['eimemory-rpc.service']}
                    job_key=contract.digest(request);job={'job_key':job_key,'unit':contract.job_unit(job_key),'manual_release':True,
                        'request':request,'registry':registry,'phase':'submitted','original_invocation':'d'*32}
                    live[job['unit']]={**live['eimemory-rpc.service'],'Id':job['unit'],'InvocationID':'d'*32,
                        'ControlGroup':f'/user.slice/user-{uid}.slice/user@{uid}.service/app.slice/'+job['unit']}
                    if fault=='kill_after_resume_foreign_job':
                        # Bypass the protected public/internal producer solely
                        # to test preservation of an unexplained foreign file.
                        bridge.save(registry,job)
                    job_file=archive.parents[1]/'jobs'/(job_key+'.json');job_raw=job_file.read_bytes() if job_file.exists() else None
                    with pytest.raises(ValueError):module.run(recovery,op,bundle,manifest)
                    assert identity_journal.read_bytes()==grown and receipt_file.read_bytes()==receipt_raw
                    if job_raw is not None:assert job_file.read_bytes()==job_raw
                    actual_run=subprocess.run
                    child_prefix=('import pwd,types,os; original=pwd.getpwuid; pwd.getpwuid=lambda uid: types.SimpleNamespace(pw_uid='+str(uid)+
                        ',pw_gid='+str(gid)+',pw_name='+repr(account.pw_name)+',pw_dir='+repr(str(home))+') if uid==os.geteuid() else original(uid);\n')
                    def forward_probe(arguments,**kwargs):
                        assert arguments[4]==module.FORWARD_PROBE
                        return actual_run([sys.executable,'-I','-B','-c',child_prefix+module.FORWARD_PROBE,str(ROOT)],**kwargs)
                    monkeypatch.setattr(module.subprocess,'run',forward_probe)
                    forward=SimpleNamespace(operation='finish-current-user',enrollment_id='c'*32,
                        expected_bundle_sha256='d'*64,expected_plan_sha256=recovery.expected_plan_sha256)
                    operations_before=len(operations)
                    if fault=='kill_after_resume_foreign_job':
                        with pytest.raises(ValueError,match='forward_job_state_preserved'):module.run(forward,op,bundle,manifest)
                        assert job_file.read_bytes()==job_raw and identity_journal.read_bytes()==grown and receipt_file.read_bytes()==receipt_raw
                        assert (archive/'activation-key.original').read_bytes()==saved_key
                        return
                    result=module.run(forward,op,bundle,manifest)
                    assert result['completed_forward'] and result['runtime_resume_deferred_to_registered_job'] is False
                    assert result['preserved_activation_receipts']==1 and result['release_accepted'] is False
                    assert len(operations)==operations_before
                    assert module.run(forward,op,bundle,manifest)['already_enrolled'] is True
                    assert len(operations)==operations_before
                    assert identity_journal.read_bytes()==grown and not job_file.exists() and receipt_file.read_bytes()==receipt_raw
                    # Completed birth no longer blocks the mutation endpoint;
                    # the next normal request validation is now reached.
                    with pytest.raises(ValueError,match='manager_target_invalid'):
                        bridge.publish_runtime(registry,{},'invalid',activate=True)
                    assert (archive/'activation-key.original').read_bytes()==saved_key
                    return
                assert module.run(recovery,op,bundle,manifest)['rolled_back'] is True
                after_recovery=len(operations)
                assert module.run(recovery,op,bundle,manifest)['already_rolled_back'] is True
                assert len(operations)==after_recovery
                assert (archive/'activation-key.original').read_bytes()==saved_key
        else:
            with pytest.raises((OSError,ValueError)):module.run(args,op,bundle,manifest)
        assert not (authority/'runtime-manager.v1.json').exists()
        assert not (authority/'installation.v1.json').exists()
        assert (authority/'hermes-topology-intent.v1.json').read_bytes()==prior_raw
        assert (unit_home/'eimemory-rpc.service').read_bytes()==original_unit
        assert not (home/'.local/state/eimemory/control-plane/activation-signing.key').exists()
    assert live['eimemory-rpc.service']['ActiveState']=='active'
    assert live['hermes-gateway.service']['ActiveState']=='active'
    assert all(live[name]['ActiveState']=='inactive' for name in logical['retired_gateways'])


@POSIX
@pytest.mark.parametrize('fault',['foreign_schema','foreign_nonce','foreign_plan','writable','none'])
def test_barrier_is_owned_by_exact_epoch_and_foreign_marker_survives(tmp_path,file_operator,fault):
    if os.geteuid()==0:pytest.skip('unprivileged account required')
    module=standalone('runtime_manager_current_user');op=file_operator;uid=os.geteuid()
    plan={'enrollment_id':'a'*32};expected=module.barrier_bytes(op,plan);target=tmp_path/'controller-installation.json'
    value=op.strict(expected)
    if fault=='foreign_schema':value['schema']='another_operation'
    elif fault=='foreign_nonce':value['enrollment_id']='b'*32
    elif fault=='foreign_plan':value['plan_sha256']='f'*64
    original=op.canonical(value);op.write(target,original,uid,0o600)
    if fault=='writable':target.chmod(0o640)
    if fault=='none':
        module.ensure_barrier(op,target,expected,uid);module.remove_barrier(op,target,expected,uid)
        assert not target.exists()
    else:
        with pytest.raises(ValueError,match='foreign_barrier_preserved'):module.ensure_barrier(op,target,expected,uid)
        with pytest.raises(ValueError,match='foreign_barrier_preserved'):module.remove_barrier(op,target,expected,uid)
        assert target.read_bytes()==original


@POSIX
def test_probe_environment_ignores_operator_selectors_and_preserves_serving_anchor(monkeypatch):
    if os.geteuid()==0:pytest.skip('unprivileged account required')
    import pwd
    module=standalone('runtime_manager_current_user');owner=pwd.getpwuid(os.geteuid())
    for key in ('HOME','USER','LOGNAME','PATH','EIMEMORY_TRUSTED_REPOSITORY_ROOT','EIMEMORY_CONFIG_DIR','PYTHONPATH'):
        monkeypatch.setenv(key,'/operator/poison')
    value=module.probe_environment(owner.pw_uid,{'EIMEMORY_CONTROL_PLANE':'user','EIMEMORY_TRUSTED_REPOSITORY_ROOT':'/serving/repo',
        'EIMEMORY_CONFIG_DIR':'/serving/config'},user_mode=True)
    assert value['HOME']==owner.pw_dir and value['USER']==value['LOGNAME']==owner.pw_name
    assert value['PATH']=='/usr/bin:/bin' and 'PYTHONPATH' not in value
    assert value['EIMEMORY_TRUSTED_REPOSITORY_ROOT']=='/serving/repo' and value['EIMEMORY_CONFIG_DIR']=='/serving/config'


@POSIX
def test_identical_locked_journal_keeps_inode_and_queued_writer_event(tmp_path,file_operator):
    if os.geteuid()==0:pytest.skip('unprivileged account required')
    import fcntl
    import subprocess
    import sys
    module=standalone('runtime_manager_current_user');op=file_operator;uid=os.geteuid();gid=os.getegid()
    archive=tmp_path/'private';archive.mkdir(mode=0o700)
    journal=tmp_path/'requests.jsonl';op.write(journal,b'original\n',uid,0o600)
    before=module.record(journal,owners={uid});original_inode=journal.stat().st_ino
    fd=os.open(journal,os.O_RDONLY);fcntl.flock(fd,fcntl.LOCK_SH)
    code="import os,fcntl,sys;f=os.open(sys.argv[1],os.O_WRONLY|os.O_APPEND);print('opened',flush=True);fcntl.flock(f,fcntl.LOCK_EX);os.write(f,b'queued-event\\n');os.fsync(f);os.close(f)"
    child=subprocess.Popen([sys.executable,'-I','-c',code,str(journal)],stdout=subprocess.PIPE,text=True)
    try:
        assert child.stdout.readline().strip()=='opened'
        tx=module.Publication(op,archive,uid,gid,{str(journal):before});tx.save();tx.apply(journal)
        assert journal.stat().st_ino==original_inode
        fcntl.flock(fd,fcntl.LOCK_UN);child.wait(timeout=5)
        assert child.returncode==0 and journal.read_bytes()==b'original\nqueued-event\n'
    finally:
        os.close(fd)
        if child.poll() is None:child.kill();child.wait()


@POSIX
@pytest.mark.parametrize('version',['v1','v2'])
def test_real_identity_probe_reads_same_locked_user_journal_and_preserves_original_uid(
        tmp_path,file_operator,version):
    if os.geteuid()==0:pytest.skip('unprivileged account required')
    import fcntl
    import pwd
    import subprocess
    import sys
    from eimemory import identity_authority as api
    module=standalone('runtime_manager_current_user');op=file_operator;owner=pwd.getpwuid(os.geteuid())
    home=tmp_path/'operator';directory=home/'.config/eimemory/control-plane/identity'
    op.directory(directory,owner.pw_uid)
    legacy={'schema':'identity_operator_policy.v1','requester_uid':0,'owner_scope':dict(
        tenant_id='fixture',agent_id='default',workspace_id='fixture',user_id=owner.pw_name),'source_ids':['hermes']}
    current,reference=api.user_policy_migration(legacy,owner.pw_uid)
    event={'operation':'authorize','event_id':'original-root-grant','requester_uid':0,'requested_at':'2026-11-01T00:00:00Z',
        'previous_hash':'','policy_digest':api.digest(legacy),'manifest':{},'manifest_digest':api.digest({})}
    event['event_hash']=api.digest(event)
    raw=(api.canonical(event)+'\n').encode();journal=directory/'requests.jsonl'
    op.write(journal,raw,owner.pw_uid)
    op.write(directory/'policy.json',op.canonical(legacy if version=='v1' else current)+b'\n',owner.pw_uid)
    if version=='v2':op.write(directory/'retained-policies'/(reference+'.json'),op.canonical(legacy)+b'\n',owner.pw_uid)
    prefix=('import pwd,types,os; original=pwd.getpwuid; '
        'pwd.getpwuid=lambda uid: types.SimpleNamespace(pw_uid='+str(owner.pw_uid)+',pw_gid='+str(owner.pw_gid)+
        ',pw_name='+repr(owner.pw_name)+',pw_dir='+repr(str(home))+') if uid==os.geteuid() else original(uid);\n')
    environment={'PATH':'/usr/bin:/bin','HOME':str(home),'EIMEMORY_CONTROL_PLANE':'user',
                 'EIMEMORY_TRUSTED_REPOSITORY_ROOT':str(tmp_path/'repository')}
    fd=os.open(journal,os.O_RDONLY);fcntl.flock(fd,fcntl.LOCK_SH);inode=journal.stat().st_ino
    try:
        before=subprocess.run([sys.executable,'-I','-B','-c',prefix+module.SOURCE_PROBE,str(ROOT)],
            input=op.canonical({'identity':{'identity':str(directory)},'uid':owner.pw_uid}),env=environment,capture_output=True,timeout=5)
        assert before.returncode==0,before.stderr.decode()
        validated=op.strict(before.stdout)['identity']['identity']
        assert validated['events']==1 and validated['journal_sha256']==sha256(raw).hexdigest()
        if version=='v1':
            op.write(directory/'retained-policies'/(reference+'.json'),op.canonical(legacy)+b'\n',owner.pw_uid)
            op.write(directory/'policy.json',op.canonical(current)+b'\n',owner.pw_uid)
        # This is the real post-publication script: no exclusive flock, no
        # second migration, and no rewritten requester UID/history chain.
        after=subprocess.run([sys.executable,'-I','-B','-c',prefix+module.USER_PROBE,str(ROOT)],
            input=op.canonical({'identity':str(directory)}),env=environment,capture_output=True,timeout=5)
        assert after.returncode==0,after.stderr.decode()
        assert op.strict(after.stdout)['identity']['journal_sha256']==sha256(raw).hexdigest()
        assert journal.stat().st_ino==inode and journal.read_bytes()==raw
        assert op.strict(journal.read_bytes())['requester_uid']==0
    finally:os.close(fd)


@POSIX
@pytest.mark.parametrize('fault',['active_user_job','inactive_user_job_with_children','active_system_job','enabled_system_rpc','unknown_gateway'])
def test_native_inventory_rejects_unregistered_work_without_stopping_anything(tmp_path,monkeypatch,fault):
    if os.geteuid()==0:pytest.skip('unprivileged account required')
    from eimemory.contracts import runtime_manager as contract
    module=standalone('runtime_manager_current_user');op=standalone('install_user_runtime_manager')
    uid=os.geteuid();gid=os.getegid();job=contract.job_unit('a'*64)
    logical={'runtime_uid':uid,'runtime_gid':gid,'owner_unit':'hermes-gateway.service','retired_gateways':[]}
    def show(unit,props,environment):
        value={key:'' for key in module.NATIVE_PROPERTIES}
        value.update(Id=unit,LoadState='not-found',ActiveState='inactive',MainPID='0',ControlPID='0',UnitFileState='not-found')
        if unit in {'eimemory-rpc.service','hermes-gateway.service'}:
            value.update(LoadState='loaded',ActiveState='active',MainPID='100',InvocationID='b'*32,ExecStart='synthetic fixture command',
                ControlGroup=f'/user.slice/user-{uid}.slice/user@{uid}.service/app.slice/'+unit,UnitFileState='enabled')
        if unit==job and fault=='inactive_user_job_with_children':value.update(ControlGroup='/unobserved/child')
        return {key:value[key] for key in props}
    monkeypatch.setattr(op,'native_state',show)
    calls=[]
    def run(arguments,**kwargs):
        calls.append(arguments)
        assert not set(arguments)&{'stop','start','enable','disable'}
        rows=[]
        if '--user' in arguments and 'list-units' in arguments:
            if fault in {'active_user_job','inactive_user_job_with_children'}:rows=[dict(unit=job,active='active' if fault=='active_user_job' else 'inactive',load='loaded',sub='running')]
            if fault=='unknown_gateway':rows=[dict(unit='hermes-gateway-foreign.service',active='active',load='loaded',sub='running')]
        elif 'list-units' in arguments and fault=='active_system_job':rows=[dict(unit=job,active='active',load='loaded',sub='running')]
        elif 'list-unit-files' in arguments and fault=='enabled_system_rpc':rows=[dict(unit_file='eimemory-rpc.service',state='enabled',preset='enabled')]
        return SimpleNamespace(returncode=0,stdout=op.canonical(rows).decode())
    monkeypatch.setattr(module.subprocess,'run',run)
    with pytest.raises(ValueError,match='unknown_native_work|system_work_present'):
        module.native_snapshot(op,{},contract,logical,tmp_path)
    assert calls


@POSIX
def test_serving_user_snapshot_preserves_every_authority_coordinate_and_original_grants(
        tmp_path,monkeypatch,file_operator):
    if os.geteuid()==0:pytest.skip('unprivileged account required')
    import pwd
    import subprocess
    import sys
    from eimemory import identity_authority as api
    module=standalone('runtime_manager_current_user');op=file_operator;owner=pwd.getpwuid(os.geteuid())
    home=tmp_path/'operator';authority=home/'.config/eimemory/control-plane'
    op.directory(authority,owner.pw_uid)
    current_account=SimpleNamespace(pw_uid=owner.pw_uid,pw_gid=owner.pw_gid,pw_name=owner.pw_name,pw_dir=str(home))
    original_pwd=pwd.getpwuid;monkeypatch.setattr(pwd,'getpwuid',lambda uid:current_account if uid==owner.pw_uid else original_pwd(uid))
    scope=dict(tenant_id='fixture',agent_id='default',workspace_id='fixture',user_id=owner.pw_name)
    policy={'schema':'identity_operator_policy.v1','requester_uid':0,'owner_scope':scope,'source_ids':['hermes']}
    manifest={'owner_scope':scope,'source_id':'hermes','items':[]}
    event={'operation':'authorize','event_id':'retained-root-grant','requester_uid':0,'requested_at':'2026-11-01T00:00:00Z',
        'previous_hash':'','policy_digest':api.digest(policy),'manifest':manifest,'manifest_digest':api.digest(manifest)}
    event['event_hash']=api.digest(event);journal=(api.canonical(event)+'\n').encode()
    for name in ('identity','subject-projection'):
        op.write(authority/name/'policy.json',op.canonical(policy)+b'\n',owner.pw_uid)
        op.write(authority/name/'requests.jsonl',journal,owner.pw_uid,0o600)
    standing={'scope-'+('a'*64)+'.v1.json':b'{"original":"standing grant"}\n',
        'scope-'+('a'*64)+'.revoked':b'original scoped revocation\n','standing-grant.revoked':b'original global revocation\n'}
    for name,raw in standing.items():op.write(authority/'standing'/name,raw,owner.pw_uid)
    op.write(authority/'code-provider-delegations.v1.json',b'{"original":"provider delegation"}\n',owner.pw_uid)
    op.write(authority/'controlled-production-acceptance/policy.json',b'{"original":"CPA scope policy"}\n',owner.pw_uid)
    environment={key:None for key in module.SOURCE_ENV}
    environment.update(EIMEMORY_CONTROL_PLANE='user',EIMEMORY_TRUSTED_REPOSITORY_ROOT=str(tmp_path/'serving-repository'),
        EIMEMORY_IDENTITY_AUTHORITY_DIR='/ignored/root/identity',EIMEMORY_SUBJECT_PROJECTION_AUTHORITY_DIR='/ignored/root/subject')
    native={'authority_environment':environment}
    prefix=('import pwd,types,os; original=pwd.getpwuid; pwd.getpwuid=lambda uid: types.SimpleNamespace(pw_uid='+
        str(owner.pw_uid)+',pw_gid='+str(owner.pw_gid)+',pw_name='+repr(owner.pw_name)+',pw_dir='+repr(str(home))+
        ') if uid==os.geteuid() else original(uid);\n')
    actual_run=subprocess.run
    def run(arguments,**kwargs):
        assert arguments[4]==module.SOURCE_PROBE
        actual=[sys.executable,'-I','-B','-c',prefix+module.SOURCE_PROBE,str(ROOT)]
        return actual_run(actual,**kwargs)
    monkeypatch.setattr(module.subprocess,'run',run)
    monkeypatch.setenv('EIMEMORY_TRUSTED_REPOSITORY_ROOT','/operator/poison')
    before={str(path):module.record(path,owners={owner.pw_uid}) for path in authority.rglob('*') if path.is_file()}
    snapshot,put=module.authority_snapshot(op,native,ROOT,owner.pw_uid,owner.pw_gid,authority)
    assert snapshot['source_control_plane']=='user' and snapshot['source_uid']==owner.pw_uid
    assert snapshot['identity_validation']['repository_root']==str(tmp_path/'serving-repository')
    assert set(snapshot['sources'])==set(before)
    for name,item in snapshot['sources'].items():assert item==before[name]
    for name in ('identity','subject-projection'):
        original=before[str(authority/name/'requests.jsonl')]
        assert put[str(authority/name/'requests.jsonl')]==original
        assert op.strict(module.contents(put[str(authority/name/'policy.json')]))['requester_uid']==owner.pw_uid
        retained=authority/name/'retained-policies'/(api.digest(policy)+'.json')
        assert module.contents(put[str(retained)])==op.canonical(policy)+b'\n'
    assert all(module.record(path,owners={owner.pw_uid})==original for path,original in before.items())
