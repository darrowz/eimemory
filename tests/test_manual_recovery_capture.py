"""Reviewed user recovery preserves failed facts and the full restore set."""
from copy import deepcopy
from hashlib import sha256
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from eimemory.contracts import runtime_manager as c
from eimemory.contracts import hermes_topology as topology
from test_runtime_manager import registry, standalone


@pytest.fixture
def recovery(monkeypatch,tmp_path):
    module=standalone('runtime_manager_bridge');bridge=module.Bridge(c,state=tmp_path)
    monkeypatch.setattr(module,'USER_MODE',True)
    monkeypatch.setattr(module,'trusted_directory',lambda *_,**__:None)
    monkeypatch.setattr(module,'read_file',lambda path,**_:Path(path).read_bytes())
    def publish(path,value,**_):
        path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(c.canonical(value))
    monkeypatch.setattr(module,'atomic_json',publish)
    old=registry();old['runtime_commit']='b'*40
    active=['eimemory-rpc.service','hermes-gateway.service','hermes-gateway-hongtai.service',
        'hermes-gateway-hongxin.service','hermes-gateway-xiaomage.service','eimemory-release-closure.path',
        *sorted(c.PRODUCT_TIMERS)]
    for unit in active:old['units'][unit]['installed']=True
    old['generation']=c.digest({k:v for k,v in old.items() if k!='generation'})
    scope=dict(tenant_id='fixture',agent_id='fixture',workspace_id='fixture',user_id='fixture')
    request=dict(schema='eimemory.manual_release_permit.v2',operation='deploy',base_commit='b'*40,
        candidate_commit='a'*40,target_commit='a'*40,controller_commit='a'*40,
        validation_controller_commit='a'*40,reviewed_attempt_id='1'*32,scope=scope)
    source=c.digest(request)
    prior=dict(job_key=source,request=request,registry=old,manual_release=True,quiesced=True,resumed=False,
        phase='publish',unit=c.job_unit(source),active_before=active,original_invocation='2'*32,
        enabled_before={unit:'enabled' for unit in active})
    current=deepcopy(old);current['controller_commit']='d'*40
    retired=sorted(unit for unit in active if unit.startswith('hermes-gateway-'))
    for unit in retired:current['units'][unit]['installed']=False
    current['generation']=c.digest({k:v for k,v in current.items() if k!='generation'})
    next_request={**request,'candidate_commit':'d'*40,'target_commit':'d'*40,'controller_commit':'d'*40,
        'validation_controller_commit':'d'*40,'reviewed_attempt_id':'3'*32}
    key=c.digest(next_request)
    record=dict(job_key=key,request=next_request,registry=current,manual_release=True,quiesced=False,resumed=False,
        phase='submitted',unit=c.job_unit(key),active_before=None,original_invocation='4'*32)
    releases=tmp_path/'releases';releases.mkdir();base=releases/('b'*40);base.mkdir()
    monkeypatch.setattr(module,'RELEASES',releases)
    monkeypatch.setattr(module,'CURRENT',SimpleNamespace(resolve=lambda **_:base))
    failure=dict(LoadState='loaded',ActiveState='failed',SubState='failed',Result='exit-code',InvocationID='2'*32,
        ExecMainStatus='1',MainPID='0',ControlGroup='',ExecMainExitTimestampMonotonic='123')
    states={unit:dict(LoadState='loaded',ActiveState='inactive',SubState='dead',Result='success',
        InvocationID='',ExecMainStatus='0',MainPID='0',ControlGroup='',ExecMainExitTimestampMonotonic='0',
        UnitFileState='disabled',Type='simple',ControlPID='0') for unit in active}
    states['eimemory-rpc.service']['ActiveState']='active'
    states['hermes-gateway.service'].update(ActiveState='active',MainPID='17',InvocationID='6'*32,
        ControlGroup='/native/hermes-gateway.service')
    for unit in retired:states[unit].update(LoadState='not-found',UnitFileState='')
    states[prior['unit']]={**failure,'Type':'oneshot'}
    monkeypatch.setattr(bridge,'unit_state',lambda _,unit,props:{name:states[unit][name] for name in props})
    calls=[]
    def native(_,unit,*args):
        calls.append((unit,args));state=states[unit]
        if args[0]=='stop':state['ActiveState']='inactive'
        elif args[0]=='enable':state['UnitFileState']='enabled-runtime' if '--runtime' in args else 'enabled'
        elif args[0]=='disable':state['UnitFileState']='disabled'
        elif args[0]=='start':
            state.update(ActiveState='active',InvocationID='f'*32)
            if state['Type']=='oneshot':
                state.update(ActiveState='inactive',SubState='dead',Result='success',MainPID='0',
                    ExecMainStatus='0',ExecMainExitTimestampMonotonic='234')
    monkeypatch.setattr(bridge,'systemctl',native)
    monkeypatch.setattr(bridge,'verify_job_peer',lambda *_:None)
    monkeypatch.setattr(bridge,'registry',lambda:deepcopy(current))
    monkeypatch.setattr(bridge,'topology_contract',lambda:topology)
    monkeypatch.setattr(bridge,'topology_links',lambda *_:True)
    monkeypatch.setattr(bridge,'fence_job_processes',lambda *_,**__:None)
    history=tmp_path/'managed-incident-receipts/manual-abandon-12345';history.mkdir(parents=True)
    def save(_,value,**__):publish(bridge.record_path(value['job_key']),value)
    monkeypatch.setattr(bridge,'save',save)
    original=deepcopy(prior)
    def persist(source_record=None,enabled=None,sources=None):
        source_record=prior if source_record is None else source_record
        job_raw=c.canonical(source_record);save(current,source_record)
        (history/'job.original').write_bytes(job_raw)
        capture=dict(schema='eimemory.manual_release_recovery_capture.v1',source_job_key=source,
            source_job_sha256=sha256(job_raw).hexdigest(),source_invocation_id='2'*32,
            source_registry_generation=old['generation'],base_commit='b'*40,scope=scope,active_before=active,
            enabled_before=source_record.get('enabled_before',{}) if enabled is None else enabled,
            source_native_exit=failure,history_basename=history.name)
        raw=c.canonical(capture);reference=dict(basename='manual-recovery-capture.json',sha256=sha256(raw).hexdigest())
        (tmp_path/reference['basename']).write_bytes(raw);(history/'recovery-capture.json').write_bytes(raw)
        publish(history/'native-exit.json',failure)
        publish(history/'complete.json',dict(complete=True,job_key=source,outcome='operator_abandoned_failed_manual_release',
            recovery_history=history.name,recovery_capture_sha256=reference['sha256'],runtime_commit='b'*40,
            failed_deployment_preserved=True,release_accepted=False,autonomous_repair_credit=False))
        publish(history/'enabled-before-sources.json',sources or dict(source='original_job',job_sha256=capture['source_job_sha256']))
        record['recovery_capture']=reference;save(current,record)
        return capture
    capture=persist()
    authority=tmp_path/'authority';authority.mkdir();bridge.registry_path=authority/'runtime-manager.v1.json'
    publish(bridge.registry_path,current)
    body=dict(schema=topology.SCHEMA,mode='single_native_gateway',runtime_uid=1001,runtime_gid=1001,
        owner_unit=topology.MAIN_GATEWAY,runtime_units=list(topology.RUNTIME_UNITS),retired_gateways=retired,
        profiles=[dict(profile_name='default',hermes_home='/profiles/default',agent_id='fixture',
            identity_status='unbound',capture_scopes=[])])
    source_transition=dict(complete=True,job_key=source,outcome='operator_abandoned_failed_manual_release',
        failed_deployment_preserved=True,release_accepted=False,autonomous_repair_credit=False,runtime_commit='b'*40)
    publish(tmp_path/'active-transition.json',source_transition)
    snapshot=c.canonical(bridge.native_topology_snapshot(current,topology.MAIN_GATEWAY,retired))
    originals={'registry.original':c.canonical(current),'transition.original':c.canonical(source_transition),
        'job.original':c.canonical(prior),'capture.original':c.canonical(capture)}
    intent=dict(schema='eimemory.hermes_topology_intent.v1',intent_id='5'*32,
        runtime_uid=1001,runtime_gid=1001,mode='single_native_gateway',main_gateway=topology.MAIN_GATEWAY,
        retired_gateways=retired,topology_sha256=topology.topology_digest(body),
        native_snapshot_sha256=sha256(snapshot).hexdigest(),authorization='explicit_user_operator',
        autonomous_repair_credit=False,source=dict(registry_sha256=sha256(originals['registry.original']).hexdigest(),
            transition_sha256=sha256(originals['transition.original']).hexdigest(),job_key=source,
            job_sha256=sha256(originals['job.original']).hexdigest(),invocation_id='2'*32,
            base_commit='b'*40,capture_sha256=sha256(originals['capture.original']).hexdigest()))
    publish(authority/'hermes-topology.v1.json',body);publish(authority/'hermes-topology-intent.v1.json',intent)
    registration=tmp_path/'managed-incident-receipts'/('topology-intent-'+intent['intent_id']);registration.mkdir()
    for name,data in {**originals,'intent.original':c.canonical(intent),'topology.original':c.canonical(body),
            'native-snapshot.original':snapshot}.items():(registration/name).write_bytes(data)
    publish(registration/'complete.json',dict(schema='eimemory.hermes_topology_registration.v1',
        intent_sha256=sha256(c.canonical(intent)).hexdigest(),source=intent['source'],
        topology_sha256=intent['topology_sha256'],native_snapshot_sha256=intent['native_snapshot_sha256'],
        registered_by_uid=1001,registered_by_gid=1001,authorization='explicit_user_operator',autonomous_repair_credit=False))
    return SimpleNamespace(module=module,bridge=bridge,registry=current,prior=prior,original=original,
        record=record,capture=capture,base=base,history=history,states=states,calls=calls,persist=persist,publish=publish,
        retired=retired,registration=registration,intent=intent)


@pytest.mark.skipif(os.name!='posix',reason='native directory fsync')
def test_quiesce_and_resume_keep_every_original_writer_and_enabled_timer(recovery):
    r=recovery
    old_raw=r.bridge.record_path(r.prior['job_key']).read_bytes()
    r.bridge.transition(r.registry,r.record['job_key'],'quiesce',7,1001)
    record=r.bridge.record(r.record['job_key'])
    assert record['active_before']==r.prior['active_before']
    assert 'eimemory-vector-sync.timer' in record['active_before'] and 'eimemory-l1-extract.timer' in record['active_before']
    assert record['enabled_before']==r.prior['enabled_before']
    assert not (r.bridge.state/'manual-recovery-capture.json').exists()
    assert r.bridge.record_path(r.prior['job_key']).read_bytes()==old_raw
    assert r.prior==r.original
    r.bridge.restore_plan(r.registry,record,create=True)
    r.bridge.resume(r.registry,record)
    proof=r.bridge.verify_restoration(r.registry,record)
    assert proof['historical_active_before']==record['active_before']
    effective=[unit for unit in record['active_before'] if unit not in r.retired]
    assert all(r.states[unit]['UnitFileState']=='enabled' for unit in effective)
    assert all(r.states[unit]['ActiveState']=='active' for unit in effective)
    assert not any(unit in r.retired for unit,_ in r.calls)


@pytest.mark.parametrize('damage', ['digest','scope','unknown_unit','source_bytes','native_restart','native_live',
    'source_success','enabled_fabricated','wrong_base','foreign_consumer'])
def test_recovery_rejects_unbound_or_unproven_captures(recovery,damage):
    r=recovery;fixed=r.bridge.state/'manual-recovery-capture.json'
    if damage=='digest':fixed.write_bytes(fixed.read_bytes()+b' ')
    elif damage=='source_bytes':(r.history/'job.original').write_bytes(b'{}')
    elif damage=='native_restart':r.states[r.prior['unit']]['InvocationID']='5'*32
    elif damage=='native_live':r.states[r.prior['unit']]['MainPID']='99'
    elif damage=='wrong_base':r.module.CURRENT.resolve=lambda **_:r.base.parent/('d'*40)
    elif damage=='foreign_consumer':
        r.publish(r.history/'capture-consumption.json',{'job_key':'6'*64})
    else:
        capture=deepcopy(r.capture)
        if damage=='scope':capture['scope']['workspace_id']='another'
        elif damage=='unknown_unit':capture['active_before']=['unknown.service']
        elif damage=='source_success':capture['source_native_exit']=dict(capture['source_native_exit'],Result='success')
        elif damage=='enabled_fabricated':capture['enabled_before']['eimemory-rpc.service']='enabled-runtime'
        raw=c.canonical(capture);fixed.write_bytes(raw);(r.history/'recovery-capture.json').write_bytes(raw)
        r.record['recovery_capture']['sha256']=sha256(raw).hexdigest()
        complete=c.strict_json((r.history/'complete.json').read_bytes())
        complete['recovery_capture_sha256']=sha256(raw).hexdigest();r.publish(r.history/'complete.json',complete)
    with pytest.raises(ValueError):
        r.bridge.manual_recovery_capture(r.registry,r.record,r.module.CURRENT.resolve())
    assert r.calls==[]


def test_pre_quiesce_abandoned_source_is_consumable(recovery):
    """Abandonable pre-quiesce failures must bind as recovery sources."""
    r=recovery
    prior=dict(r.prior,phase='quiesce_intent',quiesced=False,resumed=False,finalized=None)
    r.persist(prior)
    capture,_,_=r.bridge.manual_recovery_capture(r.registry,r.record,r.base)
    assert capture['source_job_key']==prior['job_key']
    assert capture['active_before']==prior['active_before']


def test_migration_audits_keep_system_timer_enablement_and_user_gateways(recovery):
    r=recovery;prior=deepcopy(r.prior);prior.pop('enabled_before')
    user=dict(schema='manual_user_owner_before.v1',states={unit:dict(LoadState='loaded',UnitFileState='disabled') for unit in prior['active_before']})
    for unit in user['states']:
        if unit.startswith('hermes-gateway'):user['states'][unit]['UnitFileState']='enabled'
    host=dict(schema='manual_user_owner_custody.v1',manual_bootstrap=True,autonomous_credit=False,
        current_commit=prior['request']['base_commit'],system_units={unit:dict(LoadState='loaded',UnitFileState='enabled')
            for unit in prior['active_before'] if not unit.startswith('hermes-gateway')})
    audits=[]
    for kind,value in [('user_units',user),('system_units',host)]:
        basename='enabled-before-'+('user' if kind=='user_units' else 'system')+'-audit.original'
        raw=c.canonical(value);(r.history/basename).write_bytes(raw)
        audits.append(dict(kind=kind,basename=basename,sha256=sha256(raw).hexdigest()))
    r.persist(prior,enabled={unit:'enabled' for unit in prior['active_before']},
        sources=dict(source='original_migration_native_audits',audits=audits))
    capture,_,_=r.bridge.manual_recovery_capture(r.registry,r.record,r.base)
    assert capture['enabled_before']['eimemory-l1-extract.timer']=='enabled'
    assert capture['enabled_before']['hermes-gateway-hongtai.service']=='enabled'
    (r.history/'enabled-before-system-audit.original').write_bytes(b'{}')
    with pytest.raises(ValueError,match='source_changed'):
        r.bridge.manual_recovery_capture(r.registry,r.record,r.base)


@pytest.mark.parametrize('point',['before_ledger','before_unlink','after_unlink'])
@pytest.mark.skipif(os.name!='posix',reason='native directory fsync')
def test_capture_consumption_recovers_each_crash_boundary_without_replay(recovery,monkeypatch,point):
    r=recovery;bridge=r.bridge;record=r.record
    authenticated=bridge.manual_recovery_capture(r.registry,record,r.base)
    record['active_before']=list(r.capture['active_before']);record['enabled_before']=dict(r.capture['enabled_before'])
    original_source=bridge.record_path(r.prior['job_key']).read_bytes()
    with monkeypatch.context() as patch:
        if point=='before_ledger':
            def fail_ledger(path,value,**kwargs):
                if path.name=='capture-consumption.json':raise OSError('simulated_crash')
                r.publish(path,value,**kwargs)
            patch.setattr(r.module,'atomic_json',fail_ledger)
        elif point=='before_unlink':
            original_unlink=Path.unlink
            def fail_unlink(path,*args,**kwargs):
                if path.name=='manual-recovery-capture.json':raise OSError('simulated_crash')
                return original_unlink(path,*args,**kwargs)
            patch.setattr(Path,'unlink',fail_unlink)
        else:patch.setattr(r.module.os,'fsync',lambda _:(_ for _ in ()).throw(OSError('simulated_crash')))
        with pytest.raises(OSError,match='simulated_crash'):
            bridge.consume_manual_recovery_capture(r.registry,record,authenticated)
    saved=bridge.record(record['job_key'])
    authenticated=bridge.manual_recovery_capture(r.registry,saved,r.base)
    bridge.consume_manual_recovery_capture(r.registry,saved,authenticated)
    assert not (bridge.state/'manual-recovery-capture.json').exists()
    assert bridge.record_path(r.prior['job_key']).read_bytes()==original_source
    foreign=deepcopy(saved);foreign['job_key']='7'*64
    with pytest.raises(ValueError,match='consumer_changed|already_consumed'):
        bridge.manual_recovery_capture(r.registry,foreign,r.base)


def test_restore_enablement_is_exact_and_never_enables_originally_disabled(recovery):
    r=recovery;record=r.record
    record['active_before']=list(r.capture['active_before'])
    record['enabled_before']=dict(r.capture['enabled_before'])
    record['enabled_before']['eimemory-l1-extract.timer']='enabled-runtime'
    record['enabled_before']['eimemory-vector-sync.timer']='disabled'
    r.bridge.restore_plan(r.registry,record,create=True)
    r.bridge.resume(r.registry,record)
    assert r.states['eimemory-l1-extract.timer']['UnitFileState']=='enabled-runtime'
    assert r.states['eimemory-vector-sync.timer']['UnitFileState']=='disabled'
    assert not any(unit=='eimemory-vector-sync.timer' and args[0]=='enable' for unit,args in r.calls)
    r.bridge.verify_restoration(r.registry,record)
    r.states['eimemory-l1-extract.timer']['ActiveState']='inactive'
    with pytest.raises(ValueError,match='restored_unit_not_running'):r.bridge.verify_restoration(r.registry,record)


def test_restore_oneshot_accepts_only_real_new_success_and_never_replays_unknown_start(recovery):
    r=recovery;unit='eimemory-rpc.service';record=r.record
    record['active_before']=[unit];record['enabled_before']={unit:'disabled'}
    r.states[unit].update(Type='oneshot',ActiveState='inactive',InvocationID='a'*32)
    r.bridge.restore_plan(r.registry,record,create=True)
    r.bridge.resume(r.registry,record)
    r.bridge.verify_restoration(r.registry,record)
    calls=list(r.calls);r.bridge.resume(r.registry,record)
    assert r.calls==calls
    r.states[unit]['InvocationID']='a'*32
    with pytest.raises(ValueError,match='restored_unit_not_running'):r.bridge.verify_restoration(r.registry,record)
    record['restore_progress'][unit]['phase']='start_intent'
    with pytest.raises(ValueError,match='restored_unit_not_running'):r.bridge.resume(r.registry,record)
    assert r.calls==calls


@pytest.mark.skipif(os.name!='posix',reason='native durable reconciliation')
def test_completion_requires_real_active_and_enabled_restore_before_finalizing_migration(recovery,monkeypatch):
    r=recovery;record=r.record
    record.update(active_before=list(r.capture['active_before']),enabled_before=dict(r.capture['enabled_before']),
        resumed=True,phase='resumed_awaiting_business')
    r.bridge.restore_plan(r.registry,record,create=True)
    r.bridge.save(r.registry,record)
    monkeypatch.setattr(r.bridge,'verify_completed_validation',lambda *_:None)
    monkeypatch.setattr(r.bridge,'cleanup_validation_units',lambda *_:None)
    r.publish(r.bridge.state/'active-transition.json',dict(job_key=record['job_key'],complete=False))
    migration=r.bridge.state/'migration-prior-active.json'
    r.publish(migration,dict(active_before=list(record['active_before']),restored=False))
    for unit in record['active_before']:
        if unit not in r.retired:r.states[unit].update(ActiveState='active',UnitFileState='enabled')
    r.states['eimemory-vector-sync.timer']['ActiveState']='inactive'
    with pytest.raises(ValueError,match='restored_unit_not_running'):
        r.bridge.complete(r.registry,record['job_key'],7,1001)
    assert r.bridge.record(record['job_key']).get('finalized') is not True
    assert c.strict_json(migration.read_bytes())['restored'] is False
    r.states['eimemory-vector-sync.timer']['ActiveState']='active'
    r.states['hermes-gateway.service']['UnitFileState']='disabled'
    with pytest.raises(ValueError,match='enablement_unverified'):
        r.bridge.complete(r.registry,record['job_key'],7,1001)
    assert r.bridge.record(record['job_key']).get('finalized') is not True
    r.states['hermes-gateway.service']['UnitFileState']='enabled'
    r.bridge.complete(r.registry,record['job_key'],7,1001)
    assert r.bridge.record(record['job_key'])['finalized'] is True
    assert c.strict_json(migration.read_bytes())['restored'] is False
    assert r.bridge.migration_reconciled(r.registry,migration.read_bytes())
    assert not any(unit in r.retired for unit,_ in r.calls)


@pytest.mark.parametrize('point',['after_record','after_migration','after_pointer'])
@pytest.mark.skipif(os.name!='posix',reason='native durable reconciliation')
def test_completion_reconciles_each_durable_crash_point_with_sealed_revalidation(recovery,monkeypatch,point):
    r=recovery;record=r.record
    record.update(active_before=list(r.capture['active_before']),enabled_before=dict(r.capture['enabled_before']),
        resumed=True,phase='resumed_awaiting_business')
    r.bridge.restore_plan(r.registry,record,create=True)
    r.bridge.save(r.registry,record)
    pointer=r.bridge.state/'active-transition.json';migration=r.bridge.state/'migration-prior-active.json'
    r.publish(pointer,dict(job_key=record['job_key'],complete=False))
    r.publish(migration,dict(active_before=list(record['active_before']),restored=False))
    for unit in record['active_before']:
        if unit not in r.retired:r.states[unit].update(ActiveState='active',UnitFileState='enabled')
    sealed=[]
    monkeypatch.setattr(r.bridge,'verify_completed_validation',lambda _,__,operation,___:sealed.append(operation))
    monkeypatch.setattr(r.bridge,'cleanup_validation_units',lambda *_:None)
    with monkeypatch.context() as patch:
        if point=='after_record':
            original_save=r.bridge.save
            def fail_saved(registry,value,**kwargs):
                original_save(registry,value,**kwargs)
                if value.get('finalized') is True:raise OSError('simulated_controller_crash')
            patch.setattr(r.bridge,'save',fail_saved)
        else:
            def fail_published(path,value,**kwargs):
                r.publish(path,value,**kwargs)
                if path==((r.bridge.state/'migration-reconciliation.json') if point=='after_migration' else pointer):raise OSError('simulated_controller_crash')
            patch.setattr(r.module,'atomic_json',fail_published)
        with pytest.raises(OSError,match='simulated_controller_crash'):
            r.bridge.complete(r.registry,record['job_key'],7,1001)
    assert r.bridge.record(record['job_key'])['finalized'] is True
    result=r.bridge.complete(r.registry,record['job_key'],7,1001)
    assert result['idempotent'] is True and result['operational_complete'] is True
    assert sealed==['receipt','post_resume','receipt','post_resume']
    assert c.strict_json(pointer.read_bytes())==dict(job_key=record['job_key'],complete=True,held=False)
    assert c.strict_json(migration.read_bytes())['restored'] is False
    assert r.bridge.migration_reconciled(r.registry,migration.read_bytes())
    assert not any(unit in r.retired for unit,_ in r.calls)


@pytest.mark.parametrize('fault',['foreign_pointer','changed_registry','changed_current','failed_seal','failed_restore'])
def test_finalized_completion_never_overwrites_foreign_or_unverified_context(recovery,monkeypatch,fault):
    r=recovery;record=r.record
    record.update(active_before=list(r.capture['active_before']),enabled_before=dict(r.capture['enabled_before']),
        resumed=True,phase='completed',finalized=True)
    r.bridge.restore_plan(r.registry,record,create=True)
    r.bridge.save(r.registry,record)
    pointer=r.bridge.state/'active-transition.json'
    value=dict(job_key='8'*64 if fault=='foreign_pointer' else record['job_key'],complete=False)
    r.publish(pointer,value);original=pointer.read_bytes()
    for unit in record['active_before']:
        if unit not in r.retired:r.states[unit].update(ActiveState='active',UnitFileState='enabled')
    monkeypatch.setattr(r.bridge,'verify_completed_validation',lambda *_:None)
    monkeypatch.setattr(r.bridge,'cleanup_validation_units',lambda *_:None)
    if fault=='changed_registry':monkeypatch.setattr(r.bridge,'registry',lambda:dict(r.registry,generation='9'*64))
    elif fault=='changed_current':r.module.CURRENT.resolve=lambda **_:r.base.parent/('d'*40)
    elif fault=='failed_seal':
        monkeypatch.setattr(r.bridge,'verify_completed_validation',lambda *_:(_ for _ in ()).throw(ValueError('sealed_validation_failed')))
    elif fault=='failed_restore':r.states['eimemory-vector-sync.timer']['ActiveState']='inactive'
    with pytest.raises(ValueError):r.bridge.complete(r.registry,record['job_key'],7,1001)
    assert pointer.read_bytes()==original


@pytest.mark.parametrize('manager',['system','user'])
@pytest.mark.parametrize('fault',[None,'wrong_type','wrong_substate','unknown_start','zero_pid','invalid_pid',
    'empty_cgroup','foreign_cgroup','wrong_manager','empty_invocation','old_invocation','missing_before',
    'unregistered','not_installed','different_pid','different_invocation','different_cgroup'])
def test_running_captured_oneshot_requires_a_stable_new_native_invocation(recovery,monkeypatch,manager,fault):
    r=recovery;record=r.record;unit='eimemory-backup.service'
    monkeypatch.setattr(r.module,'USER_MODE',manager=='user')
    monkeypatch.setattr(c,'USER_MODE',manager=='user')
    monkeypatch.setattr(c,'_user_coordinates',{'uid':1001},raising=False)
    record['registry']['units'][unit]=dict(manager=manager,installed=True)
    record['active_before']=[unit]
    record['restore_progress']={unit:dict(phase='start_submitted',before=dict(
        LoadState='loaded',Type='oneshot',ActiveState='inactive',InvocationID='a'*32))}
    group=(f'/user.slice/user-1001.slice/user@1001.service/app.slice/{unit}' if manager=='user'
        else f'/system.slice/{unit}')
    actual=dict(LoadState='loaded',Type='oneshot',ActiveState='activating',SubState='start',Result='success',
        MainPID='123',InvocationID='b'*32,ControlGroup=group,ExecMainStatus='0',ExecMainExitTimestampMonotonic='0')
    if fault=='wrong_type':actual['Type']='simple'
    elif fault=='wrong_substate':actual['SubState']='start-post'
    elif fault=='unknown_start':record['restore_progress'][unit]['phase']='start_intent'
    elif fault=='zero_pid':actual['MainPID']='0'
    elif fault=='invalid_pid':actual['MainPID']='unknown'
    elif fault=='empty_cgroup':actual['ControlGroup']=''
    elif fault=='foreign_cgroup':actual['ControlGroup']=group.replace(unit,'foreign.service')
    elif fault=='wrong_manager':record['registry']['units'][unit]['manager']='system' if manager=='user' else 'user'
    elif fault=='empty_invocation':actual['InvocationID']=''
    elif fault=='old_invocation':actual['InvocationID']='a'*32
    elif fault=='missing_before':record['restore_progress'][unit]['before']={}
    elif fault=='unregistered':record['registry']['units'].pop(unit)
    elif fault=='not_installed':record['registry']['units'][unit]['installed']=False
    reads=[]
    def observe(*_):
        reads.append(True);value=deepcopy(actual)
        if len(reads)>1 and fault in {'different_pid','different_invocation','different_cgroup'}:
            key={'different_pid':'MainPID','different_invocation':'InvocationID','different_cgroup':'ControlGroup'}[fault]
            value[key]='different'
        return value
    monkeypatch.setattr(r.bridge,'restoration_unit_state',observe)
    calls=list(r.calls)
    if fault is None:
        assert r.bridge.verify_restored_unit(record,unit)==actual
        assert len(reads)==2
    else:
        with pytest.raises(ValueError,match='restored_unit_not_running'):r.bridge.verify_restored_unit(record,unit)
    assert r.calls==calls


@pytest.mark.parametrize('manager',['system','user'])
@pytest.mark.parametrize('prior',['',None])
@pytest.mark.parametrize('load',['loaded','not-found'])
@pytest.mark.parametrize('phase',['start_submitted','start_intent'])
def test_running_oneshot_can_restore_a_released_native_invocation(recovery,monkeypatch,manager,prior,load,phase):
    r=recovery;record=r.record;unit='eimemory-backup.service'
    monkeypatch.setattr(r.module,'USER_MODE',manager=='user')
    monkeypatch.setattr(c,'USER_MODE',manager=='user')
    monkeypatch.setattr(c,'_user_coordinates',{'uid':1001},raising=False)
    record['registry']['units'][unit]=dict(manager=manager,installed=True)
    record['active_before']=[unit]
    record['restore_progress']={unit:dict(phase=phase,before=dict(
        LoadState=load,ActiveState='inactive',InvocationID=prior))}
    group=(f'/user.slice/user-1001.slice/user@1001.service/app.slice/{unit}' if manager=='user'
        else f'/system.slice/{unit}')
    actual=dict(LoadState='loaded',Type='oneshot',ActiveState='activating',SubState='start',Result='success',
        MainPID='123',InvocationID='b'*32,ControlGroup=group,ExecMainStatus='0',ExecMainExitTimestampMonotonic='0')
    monkeypatch.setattr(r.bridge,'restoration_unit_state',lambda *_:deepcopy(actual))
    if phase=='start_submitted':assert r.bridge.verify_restored_unit(record,unit)==actual
    else:
        with pytest.raises(ValueError,match='restored_unit_not_running'):r.bridge.verify_restored_unit(record,unit)
    assert r.calls==[]
