"""Native failure recovery restores availability without replaying acceptance."""
from contextlib import contextmanager
from copy import deepcopy
import os
from pathlib import Path
import subprocess
import threading

import pytest

from eimemory.contracts import runtime_manager as c
from test_runtime_manager_business_restore import staged, recovery
from test_runtime_manager import job
from test_runtime_manager import standalone
from hashlib import sha256
from types import SimpleNamespace


OLD_BOOT='11111111-1111-1111-1111-111111111111'
NEW_BOOT='22222222-2222-2222-2222-222222222222'


@pytest.fixture
def interrupted(staged,monkeypatch):
    r=staged;b=r.bridge;key=r.record['job_key']
    b.prepare_business(r.registry,key,7,1001)
    record=b.record(key)
    r.states[record['unit']]=dict(LoadState='loaded',ActiveState='failed',SubState='failed',
        Result='signal',InvocationID=record['original_invocation'],MainPID='0',ControlPID='0',
        ExecMainStatus='9',ControlGroup='',ExecMainExitTimestampMonotonic='777')
    r.calls.clear();r.health=[];r.storage=[]
    monkeypatch.setattr(b,'boot_id',lambda:NEW_BOOT)
    monkeypatch.setattr(b,'runtime_process_environment',lambda *_:dict(unit='eimemory-rpc.service',
        pid=17,start_ticks=100,invocation_id='6'*32,environment={'SECRET':'must never persist'}))
    monkeypatch.setattr(b,'recovery_health',lambda *_:r.health.append('healthy'))
    @contextmanager
    def storage(_):
        r.storage.append('locked');yield
    monkeypatch.setattr(b,'clean_recovery_storage',storage)
    @contextmanager
    def lifecycle():yield
    monkeypatch.setattr(b,'lifecycle_lock',lifecycle)
    return r


pytestmark=pytest.mark.skipif(os.name!='posix',reason='native durable restoration fixture')


@pytest.mark.parametrize('result,status',[('signal','9'),('timeout','15'),('exit-code','1'),('core-dump','11')])
def test_native_kill_timeout_or_failure_restores_plan_without_replaying_worker(interrupted,monkeypatch,result,status):
    r=interrupted;b=r.bridge;key=r.record['job_key'];record=b.record(key)
    r.states[record['unit']].update(Result=result,ExecMainStatus=status)
    native=b.systemctl;observed=[]
    def systemctl(registry,unit,*args):
        saved=b.record(key);marker=saved['terminal_recovery']
        assert marker['phase']=='restoring' and marker['outcome']=='failed'
        assert saved['terminal_state']['Result']==result
        observed.append(deepcopy(marker))
        return native(registry,unit,*args)
    monkeypatch.setattr(b,'systemctl',systemctl)
    assert b.recover_failed_worker()
    after=b.record(key);marker=after['terminal_recovery']
    assert marker['schema']=='eimemory.native_failed_job_restore.v1' and len(marker)==15
    assert marker['native_exit']==after['terminal_state'] and marker['failure_kind']=='native_exit'
    assert marker['phase']=='restored' and marker['original_phase']=='serving_awaiting_business'
    assert after['resumed'] and not after['background_held'] and not after.get('finalized')
    assert after['phase']=='resumed_awaiting_business' and marker['autonomous_repair_credit'] is False
    assert set(after['active_before'])==set(r.capture['active_before'])
    assert not any(unit in r.retired or unit==record['unit'] for unit,_ in r.calls)
    assert all(args[0] not in {'restart','reset-failed'} for _,args in r.calls)
    assert 'SECRET' not in b.record_path(key).read_text()
    assert b.recover_failed_worker() is False


@pytest.mark.parametrize('fault',['live','unknown','foreign_invocation','control_pid','current','pointer','health','rpc_changed','storage'])
def test_recovery_refuses_unknown_changed_or_busy_context(interrupted,monkeypatch,fault):
    r=interrupted;b=r.bridge;key=r.record['job_key'];record=b.record(key);state=r.states[record['unit']]
    if fault=='live':state.update(ActiveState='active',SubState='running',MainPID='99')
    elif fault=='unknown':state.update(LoadState='not-found',ActiveState='inactive',InvocationID='',ExecMainExitTimestampMonotonic='0')
    elif fault=='foreign_invocation':state['InvocationID']='e'*32
    elif fault=='control_pid':state['ControlPID']='98'
    elif fault=='current':monkeypatch.setattr(r.module,'CURRENT',type('Changed',(),{'resolve':lambda *_,**__:r.base})())
    elif fault=='pointer':r.publish(b.state/'active-transition.json',dict(job_key=key,complete=True))
    elif fault=='health':monkeypatch.setattr(b,'recovery_health',lambda *_:(_ for _ in ()).throw(ValueError('manager_health_failed')))
    elif fault=='rpc_changed':
        n=[0]
        def changed(*_):
            n[0]+=1;return dict(unit='eimemory-rpc.service',pid=n[0],start_ticks=1,invocation_id='6'*32)
        monkeypatch.setattr(b,'runtime_process_environment',changed)
    else:
        @contextmanager
        def busy(_):raise ValueError('manager_terminal_recovery_storage_busy');yield
        monkeypatch.setattr(b,'clean_recovery_storage',busy)
    try:assert b.recover_failed_worker() is False
    except ValueError:pass
    assert r.calls==[] and b.record(key).get('terminal_recovery') is None


@pytest.mark.parametrize('crash_phase',['failure_verified','restoring'])
def test_controller_restart_recovers_durable_failure_once(interrupted,monkeypatch,crash_phase):
    r=interrupted;b=r.bridge;key=r.record['job_key'];save=b.save
    def crash(registry,record,**kwargs):
        save(registry,record,**kwargs)
        if record.get('terminal_recovery',{}).get('phase')==crash_phase:raise RuntimeError('controller crash')
    monkeypatch.setattr(b,'save',crash)
    with pytest.raises(RuntimeError,match='controller crash'):b.recover_failed_worker()
    assert not r.calls
    monkeypatch.setattr(b,'save',save)
    b.mutex=threading.Lock()  # restarted controller has a new process lock
    assert b.recover_failed_worker()
    calls=list(r.calls)
    assert not b.recover_failed_worker() and r.calls==calls


def install_hold(r):
    record=r.bridge.record(r.record['job_key'])
    hold=dict(schema='eimemory.native_deployment_hold.v1',job_key=record['job_key'],
        original_invocation=record['original_invocation'],boot_id=OLD_BOOT,
        registry_generation=record['registry']['generation'],restore_plan_sha256=c.digest(record['restore_plan']),
        native_owner=dict(ActiveState='active',InvocationID=record['original_invocation'],
            ControlGroup='/system.slice/'+record['unit']),peer_pid=7)
    record['native_hold']=hold;r.bridge.save(r.registry,record)
    r.states[record['unit']].update(LoadState='not-found',ActiveState='inactive',SubState='dead',Result='',
        InvocationID='',ExecMainStatus='0',ExecMainExitTimestampMonotonic='0')
    return record,hold


def test_real_boot_change_restores_availability_without_fabricating_exit(interrupted):
    r=interrupted;b=r.bridge;record,hold=install_hold(r)
    assert b.recover_failed_worker()
    after=b.record(record['job_key']);marker=after['terminal_recovery']
    assert marker['failure_kind']=='boot_change' and marker['native_exit'] is None
    assert after.get('terminal_state') is None and after.get('finalized') is not True
    proof=marker['boot_change']
    assert c.native_boot_interruption(after,proof,proof['native_state'],current_boot=NEW_BOOT)
    assert after['native_hold']==hold and marker['autonomous_repair_credit'] is False


@pytest.mark.parametrize('crash_phase',['failure_verified','restoring'])
@pytest.mark.parametrize('damage',[None,'foreign_invocation','running'])
def test_boot_interruption_proof_survives_controller_crash_then_native_gc(interrupted,monkeypatch,crash_phase,damage):
    r=interrupted;b=r.bridge;record,_=install_hold(r);key=record['job_key']
    r.states[record['unit']].update(LoadState='loaded',InvocationID=record['original_invocation'])
    save=b.save
    def crash(registry,value,**kwargs):
        save(registry,value,**kwargs)
        if value.get('terminal_recovery',{}).get('phase')==crash_phase:raise RuntimeError('controller crash')
    monkeypatch.setattr(b,'save',crash)
    with pytest.raises(RuntimeError,match='controller crash'):b.recover_failed_worker()
    original=deepcopy(b.record(key)['terminal_recovery']['boot_change'])
    state=r.states[record['unit']];state.update(LoadState='not-found',InvocationID='')
    if damage=='foreign_invocation':state['InvocationID']='e'*32
    if damage=='running':state.update(ActiveState='active',MainPID='42')
    monkeypatch.setattr(b,'save',save);b.mutex=threading.Lock()
    if damage:
        with pytest.raises(ValueError):b.recover_failed_worker()
        assert not r.calls
    else:
        assert b.recover_failed_worker()
        after=b.record(key)
        assert after['terminal_recovery']['phase']=='restored' and after.get('terminal_state') is None
        assert after['terminal_recovery']['boot_change']==original


@pytest.mark.parametrize('boot',[OLD_BOOT,'33333333-3333-3333-3333-333333333333','not-a-boot'])
def test_second_reboot_preserves_first_interruption_without_claiming_a_new_exit(interrupted,monkeypatch,boot):
    r=interrupted;b=r.bridge;record,_=install_hold(r);key=record['job_key'];save=b.save
    def crash(registry,value,**kwargs):
        save(registry,value,**kwargs)
        if value.get('terminal_recovery',{}).get('phase')=='restoring':raise RuntimeError('controller crash')
    monkeypatch.setattr(b,'save',crash)
    with pytest.raises(RuntimeError):b.recover_failed_worker()
    original=deepcopy(b.record(key)['terminal_recovery']['boot_change'])
    monkeypatch.setattr(b,'save',save);monkeypatch.setattr(b,'boot_id',lambda:boot)
    if boot in {OLD_BOOT,'not-a-boot'}:
        with pytest.raises(ValueError):b.recover_failed_worker()
        assert not r.calls
    else:
        assert b.recover_failed_worker()
        after=b.record(key)
        assert after['terminal_recovery']['boot_change']==original
        assert after.get('terminal_state') is None
        assert c.restored_boot_failure(after,r.states[record['unit']],current_boot=boot)


@pytest.mark.parametrize('damage',[None,'boot_proof','native_live','original_hold'])
def test_boot_failure_manual_abandon_preserves_original_and_next_capture_is_authenticated(interrupted,monkeypatch,damage):
    r=interrupted;b=r.bridge;record,_hold=install_hold(r);assert b.recover_failed_worker()
    key=record['job_key'];record=b.record(key);raw=b.record_path(key).read_bytes()
    installer=standalone('install_user_runtime_manager')
    monkeypatch.setattr(installer,'native_boot_id',lambda:NEW_BOOT)
    state=r.states[record['unit']]
    monkeypatch.setattr(installer,'native_state',lambda _unit,props,_env:
        {name:state.get(name,'0') for name in props})
    native=installer.failed_attempt_native(c,record,{})
    installer.validate_failed_manual_recovery(c,r.registry,record,
        dict(job_key=key,complete=False),native,r.registry['runtime_commit'])
    history=b.state/'managed-incident-receipts/manual-abandon-987654';history.mkdir()
    helper=SimpleNamespace(read_file=lambda path:Path(path).read_bytes())
    pinned,pinned_raw=installer.pin_failed_terminal(helper,c,b.state,record,raw,native,history,1001,{})
    assert pinned==record and pinned_raw==raw and b.record_path(key).read_bytes()==raw
    assert pinned.get('terminal_state') is None
    capture=installer.recovery_capture(c,record,raw,native,r.registry['runtime_commit'],history.name,record['enabled_before'])
    assert capture['source_invocation_id']==record['original_invocation'] and native['InvocationID']==''
    assert capture['source_native_exit']==native and capture['source_boot_change']==record['terminal_recovery']['boot_change']
    if damage=='boot_proof':capture['source_boot_change']=dict(capture['source_boot_change'],previous_boot_id=NEW_BOOT)
    elif damage=='native_live':state['MainPID']='77'
    elif damage=='original_hold':
        record['native_hold']['boot_id']=NEW_BOOT;b.save(r.registry,record);raw=b.record_path(key).read_bytes()
        capture['source_job_sha256']=sha256(raw).hexdigest()
    r.publish(history/'native-exit.json',native)
    (history/'job.original').write_bytes(raw)
    r.publish(history/'enabled-before-sources.json',dict(source='original_job',job_sha256=sha256(raw).hexdigest()))
    capture_raw=c.canonical(capture);(history/'recovery-capture.json').write_bytes(capture_raw)
    (b.state/'manual-recovery-capture.json').write_bytes(capture_raw)
    r.publish(history/'complete.json',dict(complete=True,job_key=key,outcome='operator_abandoned_failed_manual_release',
        recovery_history=history.name,recovery_capture_sha256=sha256(capture_raw).hexdigest(),
        runtime_commit=r.registry['runtime_commit'],failed_deployment_preserved=True,
        release_accepted=False,autonomous_repair_credit=False))
    request={**record['request'],'base_commit':r.registry['runtime_commit'],'candidate_commit':'e'*40,
        'target_commit':'e'*40,'reviewed_attempt_id':'9'*32}
    next_key=c.digest(request)
    next_record=dict(job_key=next_key,request=request,registry=deepcopy(r.registry),unit=c.job_unit(next_key),
        manual_release=True,phase='submitted',active_before=None,quiesced=False,resumed=False,
        original_invocation='8'*32,recovery_capture=dict(basename='manual-recovery-capture.json',sha256=sha256(capture_raw).hexdigest()))
    b.save(r.registry,next_record)
    if damage:
        with pytest.raises(ValueError):b.manual_recovery_capture(r.registry,next_record,r.module.CURRENT.resolve())
    else:
        verified=b.manual_recovery_capture(r.registry,next_record,r.module.CURRENT.resolve())
        assert verified[0]==capture and verified[1]['source_job_key']==key
    assert b.record_path(key).read_bytes()==raw


@pytest.mark.parametrize('damage',['same_boot','missing_hold','changed_hold','changed_plan','foreign_native','live_native'])
def test_boot_change_requires_original_native_hold_and_empty_exact_owner(interrupted,monkeypatch,damage):
    r=interrupted;b=r.bridge;record,hold=install_hold(r);key=record['job_key']
    if damage=='same_boot':monkeypatch.setattr(b,'boot_id',lambda:OLD_BOOT)
    elif damage=='missing_hold':record.pop('native_hold')
    elif damage=='changed_hold':record['native_hold']['original_invocation']='f'*32
    elif damage=='changed_plan':record['native_hold']['restore_plan_sha256']='f'*64
    elif damage=='foreign_native':r.states[record['unit']]['InvocationID']='e'*32
    elif damage=='live_native':r.states[record['unit']]['MainPID']='18'
    b.save(r.registry,record)
    with pytest.raises(ValueError):b.recover_failed_worker()
    assert not r.calls and b.record(key).get('terminal_recovery') is None


def test_already_restored_but_unfinalized_native_failure_still_gets_durable_marker(interrupted):
    r=interrupted;b=r.bridge;key=r.record['job_key']
    b.restore_background(r.registry,key,7,1001);r.calls.clear()
    assert b.recover_failed_worker()
    assert b.record(key)['terminal_recovery']['phase']=='restored'
    assert not r.calls


def test_failed_rollback_restores_same_sealed_background_without_completing(interrupted,monkeypatch):
    r=interrupted;b=r.bridge;before=b.record(r.record['job_key'])
    request=job();request.update(operation='rollback',base_commit=r.base.name,
        candidate_commit=r.registry['runtime_commit'],target_commit=r.base.name,
        controller_commit=r.registry['controller_commit'])
    key=c.digest(request);record=deepcopy(before)
    record.update(job_key=key,request=request,unit=c.job_unit(key),manual_release=False)
    record['restore_plan']['job_key']=key
    r.registry['runtime_commit']=r.base.name
    r.registry['generation']=c.digest({k:v for k,v in r.registry.items() if k!='generation'})
    r.publish(b.registry_path,r.registry)
    r.publish(b.state/'active-transition.json',dict(job_key=key,complete=False))
    r.states[record['unit']]=deepcopy(r.states[before['unit']])
    b.save(r.registry,record)
    monkeypatch.setattr(r.module,'CURRENT',type('Base',(),{'resolve':lambda *_,**__:r.base})())
    assert b.recover_failed_worker()
    after=b.record(key)
    assert after['request']['operation']=='rollback' and after['terminal_recovery']['outcome']=='failed'
    assert after['resumed'] and after.get('finalized') is not True
    assert not any(unit in r.retired or unit==record['unit'] for unit,_ in r.calls)


def test_native_peer_boot_hold_is_durable_before_first_writer_stop(recovery,monkeypatch,tmp_path):
    r=recovery;b=r.bridge;key=r.record['job_key'];record=b.record(key)
    b.proc=tmp_path/'proc';process=b.proc/'7';process.mkdir(parents=True)
    (process/'status').write_text('NoNewPrivs:\t1\nCapEff:\t0000\nCapPrm:\t0000\nCapAmb:\t0000\nUid:\t1001 1001 1001 1001\n')
    group='/system.slice/'+record['unit'];(process/'cgroup').write_text('0::'+group+'\n')
    boot=b.proc/'sys/kernel/random/boot_id';boot.parent.mkdir(parents=True);boot.write_text(OLD_BOOT+'\n')
    r.states[record['unit']]=dict(ActiveState='active',InvocationID=record['original_invocation'],ControlGroup=group)
    monkeypatch.setattr(b,'verify_job_peer',lambda *args:type(b).verify_job_peer(b,*args))
    native=b.systemctl;seen=[]
    def stop(registry,unit,*args):
        saved=b.record(key);hold=saved['native_hold']
        assert hold['boot_id']==OLD_BOOT and hold['original_invocation']==record['original_invocation']
        assert hold['restore_plan_sha256']==c.digest(saved['restore_plan'])
        seen.append(unit);return native(registry,unit,*args)
    monkeypatch.setattr(b,'systemctl',stop)
    b.transition(r.registry,key,'quiesce',7,1001)
    assert seen and b.record(key)['native_hold']['peer_pid']==7


def test_health_callback_can_take_dispatch_mutex_before_recovery_and_busy_skips(interrupted,monkeypatch):
    r=interrupted;b=r.bridge
    def callback(*_):
        def request():
            with b.mutex:pass
        child=threading.Thread(target=request);child.start();child.join(1)
        assert not child.is_alive(),'health callback deadlocked behind controller mutex'
    monkeypatch.setattr(b,'recovery_health',callback)
    assert b.recover_failed_worker()


def test_watch_survives_native_timeout_and_retries_without_worker_replay(interrupted,monkeypatch):
    r=interrupted;b=r.bridge;calls=[]
    class Stop:
        def is_set(self):return len(calls)>=2
        def wait(self,_):pass
    def recovery_once():
        calls.append('attempt')
        if len(calls)==1:raise subprocess.TimeoutExpired(['systemd-run'],1)
    monkeypatch.setattr(b,'recover_failed_worker',recovery_once)
    b.recovery_watch(Stop())
    assert calls==['attempt','attempt'] and not r.calls


def test_health_probe_is_fixed_readonly_and_keeps_controller_unix_boundary(interrupted,monkeypatch):
    r=interrupted;b=r.bridge;commands=[]
    monkeypatch.setattr(b,'command',lambda argv:commands.append(argv) or c.canonical(dict(ok=True,commit='d'*40)))
    type(b).recovery_health(b,r.registry,'d'*40)
    argv=commands[0]
    assert argv[:2]==['/usr/bin/systemd-run','--user']
    assert '--property=PrivateUsers=yes' in argv and '--property=ProtectSystem=strict' in argv
    assert '--property=RuntimeMaxSec=30' in argv and '--property=NoNewPrivileges=yes' in argv
    assert argv[-1]=='d'*40 and '127.0.0.1:8091/health' in argv[-2]
    assert 'Runtime.create' not in argv[-2]


@pytest.mark.parametrize('fault',[None,'marker','busy','replace','unsafe_mode','missing'])
def test_storage_recovery_uses_both_existing_locks_and_clean_marker_cas(interrupted,monkeypatch,tmp_path,fault):
    import fcntl
    r=interrupted;b=r.bridge;locks=[tmp_path/'deploy.lock',tmp_path/'candidate.lock']
    storage=tmp_path/'storage';storage.mkdir()
    for path in locks:path.write_bytes(b'');path.chmod(0o644)
    monkeypatch.setattr(r.module,'STORAGE_LOCKS',tuple(locks));monkeypatch.setattr(r.module,'STORAGE_STATE',storage)
    if fault=='marker':(storage/'.storage-restore-journal.json').write_text('{}')
    if fault=='unsafe_mode':locks[0].chmod(0o664)
    if fault=='missing':locks[0].unlink()
    fd=None
    if fault=='busy':fd=os.open(locks[1],os.O_RDONLY);fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
    try:
        if fault is None:
            with type(b).clean_recovery_storage(b,r.registry):pass
        else:
            with pytest.raises((ValueError,OSError)):
                with type(b).clean_recovery_storage(b,r.registry):
                    if fault=='replace':
                        locks[0].unlink();locks[0].write_bytes(b'');locks[0].chmod(0o644)
    finally:
        if fd is not None:os.close(fd)
