from __future__ import annotations

from copy import deepcopy
from hashlib import sha256
import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import zipfile

import pytest

from eimemory.contracts import runtime_manager as c
from eimemory.core import runtime_manager as client

ROOT = Path(__file__).resolve().parents[1]


def standalone(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / 'deploy' / (name + '.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def registry():
    value = dict(schema=c.REGISTRY_SCHEMA, runtime_user='darrow', runtime_uid=1001, runtime_gid=1001,
        controller_commit='a'*40, runtime_commit='a'*40, repository_root='/dev-project/eimemory',
        units={name:dict(manager='user' if name in c.USER_COMPAT_UNITS else 'system', installed=False) for name in c.REGISTERED_UNITS},
        template_hashes={},compatibility_hashes={name:'0'*64 for name in c.COMPATIBILITY_PATHS},
        unit_scopes={name:dict(tenant_id='fixture',agent_id='fixture',workspace_id='fixture',user_id='fixture') for name in c.PRODUCT_SERVICES},
        managed_incident_authority={'enabled':False},controller_entry_digest=None,validation_entry_digest=None)
    value['generation'] = c.digest(value)
    return value


def job():
    return dict(schema=c.JOB_SCHEMA, operation='deploy', transaction_id='cet_actual_fixture',
        scope=dict(tenant_id='default',agent_id='hongrui',workspace_id='embodied::channel::hermes',user_id='ou_fixture'),
        base_commit='a'*40,candidate_commit='b'*40,target_commit='b'*40,controller_commit='a'*40,
        installer_digest='1'*64,policy_digest='2'*64,authorization_digest='3'*64,patch_digest='4'*64,
        candidate_tree_digest='5'*64,verification_receipt_digests=['6'*64,'7'*64,'8'*64],
        observation_deadline='2026-10-20T00:00:00+00:00')


def test_registry_binds_exact_managers_and_both_release_roles():
    value = registry()
    assert c.validate_registry(value) == value
    changed = deepcopy(value)
    changed['runtime_commit'] = 'b'*40
    changed['generation'] = c.digest({k:v for k,v in changed.items() if k != 'generation'})
    assert c.validate_registry(changed)['controller_commit'] == 'a'*40
    assert client.manager_for('eimemory-rpc.service', registry=value) == 'system'
    assert client.manager_for('hermes-gateway-hongtai.service', registry=value) == 'user'


@pytest.mark.parametrize('change', [
    lambda x:x['units']['eimemory-rpc.service'].update(manager='user'),
    lambda x:x['units'].pop('eimemory-nightly.service'),
    lambda x:x.update(repository_root='/dev-project/../etc'),
    lambda x:x.update(runtime_uid=0),
    lambda x:x.update(controller_commit='b'*40),
])
def test_registry_corruption_never_falls_back(change):
    value = registry();change(value)
    with pytest.raises(ValueError):c.validate_registry(value)


@pytest.mark.parametrize('raw', [b'{"a":1,"a":2}',b'{"x":NaN}',b'{"x":Infinity}',b' '*65537], ids=['duplicate','nan','infinity','oversized'])
def test_wire_rejects_ambiguous_or_unbounded_json(raw):
    with pytest.raises(ValueError):c.strict_json(raw)


def test_exact_job_digest_and_rollback_target():
    value = job();key=c.digest(value)
    assert c.validate_job(value,key)==value
    value.update(operation='rollback',target_commit=value['base_commit'])
    assert c.validate_job(value,c.digest(value))==value


@pytest.mark.parametrize('change', [
    lambda x:x.update(argv=['sh']),lambda x:x.update(scope={'user_id':'darrow'}),
    lambda x:x.update(target_commit='a'*40),lambda x:x.update(controller_commit='x'),
    lambda x:x.update(verification_receipt_digests=['6'*64]*3),
    lambda x:x.update(observation_deadline='2026-10-20T00:00:00'),
    lambda x:x['scope'].update(user_id=' ou_fixture'),
])
def test_job_request_cannot_expand_privilege(change):
    value=job();change(value)
    with pytest.raises(ValueError):c.validate_job(value,c.digest(value))


def test_bundle_is_byte_pinned_pid1_and_keeps_all_product_exec_nonroot():
    builder=standalone('build_runtime_manager_bundle')
    files=builder.build(ROOT,'a'*40)
    assert 'runtime_manager_contract.py' in files
    for name,raw in files.items():
        if name.startswith('templates/') and name.endswith('.service'):
            text=raw.decode()
            assert 'User=darrow' in text
            assert 'PrivateUsers=self' in text
            assert 'NoNewPrivileges=yes' in text
            assert 'CapabilityBoundingSet=\n' in text
            for line in text.splitlines():
                if line.startswith('Exec'):
                    assert not line.partition('=')[2].startswith(('+','!'))
    daemon=files['eimemory-runtime-manager.service'].decode()
    assert 'User=root' in daemon and 'runtime_manager_bridge.py' in daemon
    assert '/opt/eimemory/current' not in daemon
    policy=files['runtime-policy.conf'].decode()
    assert '/etc/eimemory-code-automation-authority' in policy
    assert 'ReadOnlyPaths=/etc/eimemory\n' not in policy
    assert 'ExecCondition=' in policy
    bootstrap=standalone('bootstrap_runtime_manager')
    archive=io.BytesIO()
    with zipfile.ZipFile(archive,'w') as z:
        for name,raw in files.items():z.writestr(name,raw)
    raw=archive.getvalue()
    restored,manifest=bootstrap.bundle(raw,sha256(raw).hexdigest())
    assert manifest['controller_commit']=='a'*40 and restored['runtime-policy.conf']==files['runtime-policy.conf']
    with pytest.raises(ValueError,match='digest'):bootstrap.bundle(raw,'f'*64)


def bridge_fixture(monkeypatch):
    module=standalone('runtime_manager_bridge')
    value=registry();calls=[]
    bridge=module.Bridge(c,command=lambda args:calls.append(args) or '')
    monkeypatch.setattr(bridge,'registry',lambda:deepcopy(value))
    return module,bridge,value,calls


def wire(value,op,**fields):
    return dict(schema=c.REQUEST_SCHEMA,operation=op,registry_generation=value['generation'],**fields)


def test_peer_uid_and_shape_fail_closed_before_systemctl(monkeypatch):
    _module,bridge,value,calls=bridge_fixture(monkeypatch)
    with pytest.raises(ValueError,match='uid'):bridge.dispatch(wire(value,'single_rpc_owner'),pid=12,uid=65534)
    with pytest.raises(ValueError,match='shape'):bridge.dispatch(wire(value,'transition',job_key='1'*64,phase='resume',argv=['sh']),pid=12,uid=1001)
    with pytest.raises(ValueError,match='shape'):bridge.dispatch(wire(value,'start',unit='eimemory-rpc.service'),pid=12,uid=1001)
    with pytest.raises(ValueError,match='property'):bridge.dispatch(wire(value,'unit_state',unit='eimemory-rpc.service',properties=['Environment']),pid=12,uid=1001)
    assert calls==[]


def test_manager_is_declared_not_selected_from_healthy_shadow(monkeypatch):
    _module,bridge,value,calls=bridge_fixture(monkeypatch)
    monkeypatch.setattr(bridge,'command',lambda args:calls.append(args) or 'ActiveState=failed\n')
    result=bridge.dispatch(wire(value,'unit_state',unit='eimemory-rpc.service',properties=['ActiveState']),pid=12,uid=1001)
    assert result['state']['ActiveState']=='failed'
    assert len(calls)==1 and '--user' not in calls[0]


def test_public_identity_never_returns_other_environment(monkeypatch):
    _module,bridge,value,calls=bridge_fixture(monkeypatch)
    monkeypatch.setattr(bridge,'command',lambda args:'LoadState=loaded\nEnvironment=TOKEN=secret EIMEMORY_RUNTIME_COMMIT='+('a'*40)+'\n')
    result=bridge.dispatch(wire(value,'runtime_identity',unit='eimemory-rpc.service'),pid=12,uid=1001)
    assert result=={'runtime_commit':'a'*40}
    assert 'secret' not in json.dumps(result)


def test_repeated_submit_only_reads_root_durable_record(monkeypatch,tmp_path):
    _module,bridge,value,calls=bridge_fixture(monkeypatch)
    marker=tmp_path/'already';marker.write_text('existing')
    monkeypatch.setattr(bridge,'record_path',lambda key:marker)
    monkeypatch.setattr(bridge,'job_status',lambda r,key:dict(job_key=key,state={'ActiveState':'unknown'}))
    monkeypatch.setattr(bridge,'load_request',lambda *_:pytest.fail('must not reread caller request'))
    result=bridge.submit(value,'1'*64)
    assert result['state']['ActiveState']=='unknown' and calls==[]


def test_first_submission_persists_before_ambiguous_start(monkeypatch,tmp_path):
    _module,bridge,value,calls=bridge_fixture(monkeypatch)
    marker=tmp_path/'absent';saved=[]
    request=job();key=c.digest(request)
    monkeypatch.setattr(bridge,'record_path',lambda key:marker)
    monkeypatch.setattr(bridge,'load_request',lambda *_:request)
    monkeypatch.setattr(bridge,'save',lambda reg,record,exclusive=False:saved.append((deepcopy(record),exclusive)))
    def uncertain(*args):
        assert saved and saved[0][1] is True
        raise ValueError('manager_command_failed')
    monkeypatch.setattr(bridge,'systemctl',uncertain)
    with pytest.raises(ValueError):bridge.submit(value,key)
    assert saved[0][0]['request']==request and saved[0][0]['active_before'] is None


def test_cgroup_and_nnp_required_for_lifecycle(monkeypatch,tmp_path):
    _module,bridge,value,calls=bridge_fixture(monkeypatch)
    process=tmp_path/'42';process.mkdir();bridge.proc=tmp_path
    key='1'*64;record={'job_key':key,'unit':c.job_unit(key),'registry':value}
    monkeypatch.setattr(bridge,'save',lambda *_:None)
    monkeypatch.setattr(bridge,'unit_state',lambda *_:{'ActiveState':'activating','InvocationID':'a'*32,'ControlGroup':'/system.slice/'+record['unit']})
    (process/'status').write_text('Uid:\t1001 1001 1001 1001\nNoNewPrivs:\t1\n')
    (process/'cgroup').write_text('0::/system.slice/'+record['unit']+'\n')
    bridge.verify_job_peer(value,record,42,1001)
    (process/'cgroup').write_text('0::/user.slice/user@1001.service/app.slice/'+record['unit']+'\n')
    with pytest.raises(ValueError,match='cgroup'):bridge.verify_job_peer(value,record,42,1001)
    (process/'status').write_text('Uid:\t1001 1001 1001 1001\nNoNewPrivs:\t0\n')
    with pytest.raises(ValueError,match='nnp'):bridge.verify_job_peer(value,record,42,1001)
    assert calls==[]


def test_job_invocation_is_pinned_and_cannot_be_replaced(monkeypatch):
    _module,bridge,value,calls=bridge_fixture(monkeypatch)
    record={'job_key':'1'*64,'unit':c.job_unit('1'*64),'registry':value,'phase':'submitted','original_invocation':'a'*32}
    monkeypatch.setattr(bridge,'record',lambda *_:record)
    monkeypatch.setattr(bridge,'unit_state',lambda *_:{'InvocationID':'b'*32})
    with pytest.raises(ValueError,match='invocation_changed'):bridge.job_status(value,'1'*64)


def test_error_window_preserves_failure_before_later_green(monkeypatch):
    _module,bridge,value,calls=bridge_fixture(monkeypatch)
    value['units']['eimemory-nightly.service']['installed']=True
    module=standalone('runtime_manager_bridge');now=module.time.time()
    rows=[dict(MESSAGE='database is locked',__REALTIME_TIMESTAMP=str(int((now-100)*1e6)),_SYSTEMD_INVOCATION_ID='a'*32),
          dict(MESSAGE='later successful run',__REALTIME_TIMESTAMP=str(int((now-1)*1e6)),_SYSTEMD_INVOCATION_ID='b'*32)]
    rows=rows[:1]  # Native grep selects only errors, across all invocations.
    monkeypatch.setattr(bridge,'journal_output',lambda args:'\n'.join(json.dumps(x) for x in rows))
    report=bridge.error_window(value)
    assert report['ok'] is False and report['observed_error_counts']['database_read_write_failure']==1
    assert 'MESSAGE' not in json.dumps(report)


def test_root_entrypoints_have_no_product_imports_or_runtime_code_exec():
    import ast
    for name in ('runtime_manager_bridge','bootstrap_runtime_manager'):
        tree=ast.parse((ROOT/'deploy'/(name+'.py')).read_text())
        for node in ast.walk(tree):
            if isinstance(node,ast.Import):
                assert all(not item.name.startswith('eimemory') for item in node.names)
            if isinstance(node,ast.ImportFrom):
                assert not str(node.module).startswith('eimemory')
    launcher=(ROOT/'deploy/runtime_manager_launch.py').read_text()
    assert "os.geteuid()==0" in launcher
    assert "'-I','-B'" in launcher


def test_actual_root_reader_retains_original_two_phase_budget(monkeypatch):
    from types import SimpleNamespace
    module=standalone('runtime_manager_bridge');bridge=module.Bridge(c)
    clock={'now':100.0};timeouts=[]
    monkeypatch.setattr(module.time,'monotonic',lambda:clock['now'])
    def run(args,**kwargs):
        timeouts.append(kwargs['timeout']);clock['now']+=7 if len(timeouts)==1 else 3
        return SimpleNamespace(returncode=0 if len(timeouts)==1 else 1,stdout='',stderr='')
    monkeypatch.setattr(module.subprocess,'run',run)
    assert bridge.journal_output(['/usr/bin/journalctl','--lines=2001','--grep=segfault'])==''
    assert timeouts==[12,5]


@pytest.mark.parametrize('returncode,stdout,stderr',[(1,'','denied'),(1,'{}',''),(0,'','partial'),(-15,'','')])
def test_actual_root_reader_does_not_turn_native_failure_into_empty_success(monkeypatch,returncode,stdout,stderr):
    from types import SimpleNamespace
    module=standalone('runtime_manager_bridge');bridge=module.Bridge(c)
    results=iter([SimpleNamespace(returncode=0,stdout='',stderr=''),SimpleNamespace(returncode=returncode,stdout=stdout,stderr=stderr)])
    monkeypatch.setattr(module.subprocess,'run',lambda *a,**k:next(results))
    with pytest.raises(RuntimeError):bridge.journal_output(['/usr/bin/journalctl','--lines=2001','--grep=segfault'])


def current_job_fixture(monkeypatch):
    module,bridge,value,_calls=bridge_fixture(monkeypatch)
    unit='eimemory-learn-watch.service';timer='eimemory-learn-watch.timer'
    value['units'][unit]['installed']=value['units'][timer]['installed']=True
    unitpath='/org/freedesktop/systemd1/unit/eimemory_2dlearn_2dwatch_2eservice'
    timerpath='/org/freedesktop/systemd1/unit/eimemory_2dlearn_2dwatch_2etimer'
    jobpath='/org/freedesktop/systemd1/job/1973236'
    start=3184557469608;stamp=3184557468623
    # Exact busctl type/data shapes observed in the temporary PID1 timer
    # fixture (not production): scalar data is scalar; struct data is a list.
    properties={
        (unitpath,'Job'):{'type':'(uo)','data':[1973236,jobpath]},
        (jobpath,'Id'):{'type':'u','data':1973236},
        (jobpath,'Unit'):{'type':'(so)','data':[unit,unitpath]},
        (jobpath,'JobType'):{'type':'s','data':'start'},
        (jobpath,'State'):{'type':'s','data':'running'},
        (jobpath,'ActivationDetails'):{'type':'a(ss)','data':[
            ['trigger_unit',timer],['trigger_timer_realtime_usec','1792413490034860'],
            ['trigger_timer_monotonic_usec',str(stamp)]]},
        (timerpath,'LastTriggerUSecMonotonic'):{'type':'t','data':stamp},
        (timerpath,'Unit'):{'type':'s','data':unit},
    }
    state={'InvocationID':'a'*32,'ControlGroup':'/system.slice/'+unit,
        'ExecMainStartTimestampMonotonic':str(start)}
    monkeypatch.setattr(bridge,'unit_bus_path',lambda name:unitpath if name==unit else timerpath)
    monkeypatch.setattr(bridge,'bus_property',lambda path,interface,name:deepcopy(properties[path,name]))
    monkeypatch.setattr(bridge,'unit_state',lambda *_:deepcopy(state))
    return module,bridge,value,unit,unitpath,jobpath,timerpath,properties,state


def test_actual_current_job_scalar_shapes_prove_timer_participation(monkeypatch):
    _m,bridge,value,unit,*_rest=current_job_fixture(monkeypatch)
    actual=bridge.activation_cause(value,unit,_rest[-1])
    assert actual['trigger_kind']=='scheduled_timer'
    assert actual['trigger_unit']=='eimemory-learn-watch.timer'
    assert actual['job_id']==1973236 and actual['job_state']=='running'
    assert len(actual['activation_details_digest'])==64


@pytest.mark.parametrize('change',[
    'no_job','old_unit_details_only','unsupported_job_details','not_running',
    'different_unit','stale_details','future_details','timer_moved','unregistered_timer',
    'duplicate_trigger','job_changed','invocation_changed',
])
def test_missing_manual_stale_or_changed_job_never_becomes_scheduled(monkeypatch,change):
    _m,bridge,value,unit,unitpath,jobpath,timerpath,props,state=current_job_fixture(monkeypatch)
    if change in {'no_job','old_unit_details_only'}:
        props[unitpath,'Job']={'type':'(uo)','data':[0,'/']}
        # Deliberately leave old populated activation details; never consulted.
    elif change=='unsupported_job_details':props[jobpath,'ActivationDetails']={'type':'a(ss)','data':[]}
    elif change=='not_running':props[jobpath,'State']={'type':'s','data':'waiting'}
    elif change=='different_unit':props[jobpath,'Unit']['data'][0]='eimemory-nightly.service'
    elif change=='stale_details':state['ExecMainStartTimestampMonotonic']=str(3184557468623+60000001)
    elif change=='future_details':state['ExecMainStartTimestampMonotonic']='1'
    elif change=='timer_moved':props[timerpath,'LastTriggerUSecMonotonic']['data']+=1
    elif change=='unregistered_timer':value['units']['eimemory-learn-watch.timer']['installed']=False
    elif change=='duplicate_trigger':props[jobpath,'ActivationDetails']['data'].append(['trigger_unit','eimemory-learn-watch.timer'])
    elif change=='job_changed':
        original=bridge.bus_property;reads=[]
        def changed(path,interface,name):
            result=original(path,interface,name)
            if name=='Job':
                reads.append(1)
                if len(reads)>1:return {'type':'(uo)','data':[0,'/']}
            return result
        monkeypatch.setattr(bridge,'bus_property',changed)
    elif change=='invocation_changed':monkeypatch.setattr(bridge,'unit_state',lambda *_:{**state,'InvocationID':'b'*32})
    assert bridge.activation_cause(value,unit,state)['trigger_kind']=='unknown'


def lifecycle_fixture(monkeypatch,tmp_path):
    from types import SimpleNamespace
    module,bridge,value,calls=bridge_fixture(monkeypatch)
    request=job();key=c.digest(request)
    record=dict(job_key=key,request=request,registry=value,unit=c.job_unit(key),
        phase='submitted',active_before=None,quiesced=False,resumed=False)
    records={key:record};stored={}
    monkeypatch.setattr(bridge,'record',lambda k:deepcopy(records[k]))
    monkeypatch.setattr(bridge,'save',lambda r,rec,exclusive=False:records.__setitem__(rec['job_key'],deepcopy(rec)))
    monkeypatch.setattr(bridge,'verify_job_peer',lambda *_:None)
    bridge.state=tmp_path
    releases=tmp_path/'releases';releases.mkdir()
    target=releases/request['candidate_commit'];target.mkdir()
    monkeypatch.setattr(module,'RELEASES',releases)
    monkeypatch.setattr(module,'CURRENT',SimpleNamespace(resolve=lambda **_:target))
    monkeypatch.setattr(module,'atomic_json',lambda path,data,**_:stored.__setitem__(str(path),deepcopy(data)))
    def read(path,**_):
        if str(path) in stored:return c.canonical(stored[str(path)])
        return Path(path).read_bytes()
    monkeypatch.setattr(module,'read_file',read)
    # Existence is real, bytes are read from the same private memory ledger.
    def persist_active(data):
        path=tmp_path/'active-transition.json';path.touch();stored[str(path)]=deepcopy(data)
    def state(reg,unit,props):
        return {p:('loaded' if p=='LoadState' else 'inactive') for p in props}
    monkeypatch.setattr(bridge,'unit_state',state)
    return module,bridge,value,request,key,records,stored,target,calls,persist_active


def test_quiesce_preserves_original_set_after_partial_stop_failure(monkeypatch,tmp_path):
    _m,b,r,_req,key,records,_stored,_target,calls,_persist=lifecycle_fixture(monkeypatch,tmp_path)
    units=['eimemory-learn-watch.service','eimemory-learn-watch.timer','eimemory-rpc.service']
    for unit in units:r['units'][unit]['installed']=True
    monkeypatch.setattr(b,'unit_state',lambda reg,unit,props:{p:('loaded' if p=='LoadState' else 'active') for p in props})
    def stop(reg,unit,*args):
        calls.append((unit,args))
        raise ValueError('manager_command_failed')
    monkeypatch.setattr(b,'systemctl',stop)
    with pytest.raises(ValueError,match='command_failed'):b.transition(r,key,'quiesce',42,1001)
    assert set(records[key]['active_before'])==set(units)
    assert records[key]['phase']=='quiesce_intent' and not records[key]['quiesced']
    assert calls[0][0].endswith('.timer')


def test_quiesce_rejects_third_release_before_any_stop(monkeypatch,tmp_path):
    from types import SimpleNamespace
    m,b,r,_req,key,_records,_stored,target,calls,_persist=lifecycle_fixture(monkeypatch,tmp_path)
    monkeypatch.setattr(m,'CURRENT',SimpleNamespace(resolve=lambda **_:target.parent/('c'*40)))
    with pytest.raises(ValueError,match='current_unbound'):b.transition(r,key,'quiesce',42,1001)
    assert calls==[]


@pytest.mark.parametrize('tamper',[False,True])
def test_rollback_inherits_only_exact_same_transaction_capture(monkeypatch,tmp_path,tamper):
    _m,b,r,req,key,records,_stored,_target,calls,persist=lifecycle_fixture(monkeypatch,tmp_path)
    prior=records[key];prior.update(active_before=['eimemory-rpc.service','eimemory-learn-watch.timer'],quiesced=True)
    rollback={**req,'operation':'rollback','target_commit':req['base_commit']}
    if tamper:rollback['policy_digest']='9'*64
    rollback_key=c.digest(rollback)
    records[rollback_key]={**deepcopy(prior),'job_key':rollback_key,'request':rollback,
        'unit':c.job_unit(rollback_key),'active_before':None,'quiesced':False}
    persist({'job_key':key,'complete':False})
    if tamper:
        with pytest.raises(ValueError,match='another_transition'):b.transition(r,rollback_key,'quiesce',42,1001)
        assert calls==[]
    else:
        actual=b.transition(r,rollback_key,'quiesce',42,1001)
        assert actual['active_before']==prior['active_before']
        assert records[key]['superseded_by']==rollback_key
        assert records[rollback_key]['inherited_capture_from']==key


def test_finalize_keeps_global_ownership_until_business_complete(monkeypatch,tmp_path):
    _m,b,r,_req,key,records,stored,target,calls,persist=lifecycle_fixture(monkeypatch,tmp_path)
    records[key].update(active_before=['eimemory-rpc.service','eimemory-learn-watch.timer'],
        quiesced=True,resume_requested=True)
    for relative in c.COMPATIBILITY_PATHS:
        path=target/relative;path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(b'actual reviewed bytes')
        r['compatibility_hashes'][relative]=sha256(path.read_bytes()).hexdigest()
    persist({'job_key':key,'complete':False})
    first=b.finalize(r,key,42,1001)
    assert first['resumed'] and first['phase']=='resumed_awaiting_business'
    assert stored[str(tmp_path/'active-transition.json')]['complete'] is False
    count=len(calls)
    assert b.finalize(r,key,42,1001)['idempotent'] and len(calls)==count
    assert b.transition(r,key,'resume',42,1001)['phase']=='resumed_awaiting_business'
    final=b.complete(r,key,42,1001)
    assert final['phase']=='completed' and final['operational_complete']
    assert stored[str(tmp_path/'active-transition.json')]['complete'] is True
    assert b.complete(r,key,42,1001)['idempotent'] and len(calls)==count


def test_legacy_hold_keeps_restore_set_for_reviewed_manual_upgrade(monkeypatch,tmp_path):
    _m,b,r,req,key,records,stored,_target,calls,persist=lifecycle_fixture(monkeypatch,tmp_path)
    records[key].update(active_before=['eimemory-rpc.service','eimemory-learn-watch.timer'],quiesced=True,resume_requested=True)
    persist({'job_key':key,'complete':False})
    held=b.finalize(r,key,42,1001)
    assert held['resumed'] is False and held['held_background']==['eimemory-learn-watch.timer']
    final=b.complete(r,key,42,1001)
    assert final['phase']=='completed_held' and final['operational_complete'] is False
    new_key='9'*64
    records[new_key]={**deepcopy(records[key]),'job_key':new_key,'unit':c.job_unit(new_key),
        'request':deepcopy(req),'active_before':None,'quiesced':False,'resumed':False,'finalized':False,'phase':'submitted'}
    with pytest.raises(ValueError,match='held_runtime_requires_manual'):b.transition(r,new_key,'quiesce',42,1001)
    records[new_key]['manual_release']=True
    actual=b.transition(r,new_key,'quiesce',42,1001)
    assert actual['active_before']==records[key]['active_before']
    assert records[new_key]['inherited_held_capture_from']==key


def test_complete_cannot_claim_business_before_resume(monkeypatch,tmp_path):
    _m,b,r,_req,key,_records,_stored,_target,calls,_persist=lifecycle_fixture(monkeypatch,tmp_path)
    with pytest.raises(ValueError,match='complete_before_resume'):b.complete(r,key,42,1001)
    assert calls==[]


def test_gc_unit_retains_durable_original_invocation(monkeypatch):
    _m,b,r,_calls=bridge_fixture(monkeypatch)
    record=dict(job_key='1'*64,unit=c.job_unit('1'*64),registry=r,phase='completed',
        original_invocation='a'*32,finalized=True,resumed=True)
    monkeypatch.setattr(b,'record',lambda *_:record)
    monkeypatch.setattr(b,'unit_state',lambda *_:{'InvocationID':'','ActiveState':'inactive','LoadState':'not-found'})
    actual=b.job_status(r,'1'*64)
    assert actual['original_invocation']=='a'*32 and actual['complete'] and actual['resumed']


@pytest.mark.parametrize('fault',[None,'payload_changed','wrong_digest','wrong_id'])
def test_activation_receipt_verifies_original_bytes_without_refresh(monkeypatch,tmp_path,fault):
    import hmac
    module,b,_r,_calls=bridge_fixture(monkeypatch)
    b.state=tmp_path
    payload=dict(schema='eimemory.runtime_activation_attestation.v1',trigger_kind='unknown',
        invocation_id='a'*32,scope=job()['scope'],observed_at='2026-10-19T12:00:00+00:00')
    secret=b'k'*32
    receipt={'payload':payload,'signature':hmac.new(secret,c.canonical(payload),'sha256').hexdigest()}
    if fault=='payload_changed':receipt['payload']['trigger_kind']='scheduled_timer'
    identifier=c.digest(payload);digest=c.digest(receipt)
    if fault=='wrong_digest':digest='0'*64
    if fault=='wrong_id':identifier='0'*64
    monkeypatch.setattr(module,'trusted_directory',lambda *_a,**_k:None)
    monkeypatch.setattr(module,'read_file',lambda path,**_:secret if Path(path).name=='activation-signing.key' else c.canonical(receipt))
    monkeypatch.setattr(module,'atomic_json',lambda *_a,**_k:pytest.fail('verify must not write or re-sign'))
    if fault:
        with pytest.raises(ValueError):b.verify_activation(identifier,digest)
    else:
        actual=b.verify_activation(identifier,digest)
        assert actual['trigger_kind']=='unknown' and actual['signature_verified']
        assert actual['reference']['receipt_id']==identifier


def test_only_reviewed_native_units_can_receive_activation_scope(monkeypatch,tmp_path):
    module,b,r,_calls=bridge_fixture(monkeypatch)
    b.proc=tmp_path
    process=tmp_path/'42';process.mkdir()
    (process/'status').write_text('Uid: 1001 1001 1001 1001\nNoNewPrivs: 1\n')
    (process/'stat').write_text('42 (source) '+' '.join(['0']*19+['7']+['0']*8))
    unit='eimemory-release-closure.service'
    (process/'cgroup').write_text('0::/system.slice/'+unit+'\n')
    r['unit_scopes']={}
    with pytest.raises(ValueError,match='scope_not_reviewed'):b.activation_attestation(r,42,1001)


def test_managed_issuer_is_disabled_by_default_and_has_no_request_paths(monkeypatch):
    from types import SimpleNamespace
    _m,b,r,_calls=bridge_fixture(monkeypatch)
    with pytest.raises(ValueError,match='not_enabled'):b.dispatch(wire(r,'issue_managed_incident_proof',source_record_id='rec1',source_digest='a'*64),pid=42,uid=1001)
    with pytest.raises(ValueError,match='shape'):b.dispatch(wire(r,'checkpoint_managed_incident_knowledge',scope=job()['scope']),pid=42,uid=1001)
    calls=[]
    authority=SimpleNamespace(issue=lambda *a,**k:calls.append(('issue',a,k)) or {'status':'unproven'},
        verify=lambda *a,**k:calls.append(('verify',a,k)) or {'status':'unproven'},
        checkpoint=lambda **k:calls.append(('checkpoint',(),k)) or {'ready':False})
    monkeypatch.setattr(b,'managed_authority',lambda _:authority)
    assert b.dispatch(wire(r,'issue_managed_incident_proof',source_record_id='rec1',source_digest='a'*64),pid=42,uid=1001)['reference']=={'status':'unproven'}
    assert calls[-1][2]['verified_activation_receipt']==b.verify_activation
    assert b.dispatch(wire(r,'verify_managed_incident_proof',receipt_id='b'*64,digest='c'*64),pid=42,uid=1001)['proof']['status']=='unproven'
    assert b.dispatch(wire(r,'checkpoint_managed_incident_knowledge'),pid=42,uid=1001)['checkpoint']['ready'] is False


def test_reviewed_manual_attempt_nonce_never_replays_unknown_job(monkeypatch,tmp_path):
    from types import SimpleNamespace
    m=standalone('bootstrap_runtime_manager');r=registry();calls=[]
    releases=tmp_path/'releases';releases.mkdir()
    monkeypatch.setattr(m,'STATE',tmp_path)
    monkeypatch.setattr(m,'RELEASES',releases)
    monkeypatch.setattr(m,'CURRENT',SimpleNamespace(resolve=lambda **_:releases/r['runtime_commit']))
    monkeypatch.setattr(m,'REGISTRY',tmp_path/'registry.json')
    (tmp_path/'registry.json').write_bytes(c.canonical(r));(tmp_path/'jobs').mkdir()
    monkeypatch.setattr(m,'load_contract',lambda:c)
    monkeypatch.setattr(m,'storage_clean',lambda:None)
    monkeypatch.setattr(m,'read',lambda path,**_:Path(path).read_bytes())
    def write(path,raw,**kwargs):
        path=Path(path)
        if kwargs.get('exclusive') and path.exists():raise FileExistsError(path)
        path.write_bytes(raw)
    monkeypatch.setattr(m,'write',write)
    actual={'state':'LoadState=not-found\nActiveState=inactive\nResult=success\nInvocationID=\n'}
    def call(args):
        calls.append(args)
        return actual['state'] if args[1]=='show' else ''
    monkeypatch.setattr(m,'call',call)
    args=SimpleNamespace(target_commit='b'*40,attempt_id='1'*32,prior_job_key=None)
    m.stage(args)
    first=next((tmp_path/'jobs').iterdir());first_bytes=first.read_bytes();firstkey=first.stem
    with pytest.raises(ValueError,match='prior_attempt_review_required'):m.stage(args)
    args.prior_job_key=firstkey;args.attempt_id='2'*32
    with pytest.raises(ValueError,match='active_or_unknown'):m.stage(args)
    assert len([x for x in calls if x[1]=='start'])==1
    actual['state']='LoadState=loaded\nActiveState=failed\nResult=exit-code\nInvocationID='+('a'*32)+'\n'
    m.stage(args)
    assert len(list((tmp_path/'jobs').iterdir()))==2 and first.read_bytes()==first_bytes
    new=next(json.loads(p.read_bytes()) for p in (tmp_path/'jobs').iterdir() if p!=first)
    assert new['approved_retry_of']==firstkey
    assert new['request']['reviewed_attempt_id']=='2'*32


@pytest.mark.parametrize('marker',['storage-release-transaction.json','.storage-release-transaction.json.clearing','.storage-release-transaction.json.recovery'])
def test_owner_restore_requires_no_storage_recovery_marker(monkeypatch,tmp_path,marker):
    m=standalone('bootstrap_runtime_manager');monkeypatch.setattr(m,'STORAGE_STATE',tmp_path)
    (tmp_path/marker).write_text('{}')
    with pytest.raises(ValueError,match='storage_recovery_not_clean'):m.storage_clean()


@pytest.mark.skipif(sys.platform=='win32' or os.geteuid()!=0,reason='native root POSIX ownership/mode restore fixture')
def test_root_owner_file_restore_preserves_exact_original_and_rejects_paths(tmp_path,monkeypatch):
    import os
    m=standalone('bootstrap_runtime_manager')
    # The real bootstrap forbids world-writable ancestors. A root-only /run
    # fixture exercises that boundary without relaxing it for pytest /tmp.
    import tempfile
    base=Path(tempfile.mkdtemp(prefix='eimemory-manager-root-fs-',dir='/run'))
    assert base.parent==Path('/run') and base.name.startswith('eimemory-manager-root-fs-')
    root=base/'root';units=base/'units';root.mkdir();units.mkdir()
    monkeypatch.setattr(m,'ROOT',root);monkeypatch.setattr(m,'SYSTEM_UNITS',units);monkeypatch.setattr(m,'REGISTRY',base/'registry.json')
    old=root/'runtime_manager_bridge.py';old.write_bytes(b'original root bytes');old.chmod(0o640)
    manifest={'runtime_manager_bridge.py':b'reviewed new bytes','templates/eimemory-rpc.service':b'new template'}
    snapshot=m.root_snapshot(manifest)
    old.write_bytes(b'replaced root bytes')
    new=units/'eimemory-rpc.service';new.write_bytes(b'new template');new.chmod(0o640)
    m.restore_root_files(snapshot)
    assert old.read_bytes()==b'original root bytes' and not new.exists()
    with pytest.raises(ValueError,match='path_invalid'):m.restore_root_files({'/root/arbitrary':None})
    import shutil
    assert base.resolve().parent==Path('/run') and base.name.startswith('eimemory-manager-root-fs-')
    shutil.rmtree(base)


@pytest.mark.skipif(sys.platform=='win32' or os.geteuid()!=0,reason='native root fixture for user-owned source and nofollow guards')
def test_user_owner_snapshot_rejects_unrelated_edit_and_restores_only_its_policy(tmp_path,monkeypatch):
    import os
    m=standalone('runtime_manager_owner_files')
    home=tmp_path/'h'/'config'/'systemd'/'user'
    home.mkdir(parents=True)
    # Pure codec fixture uses the current UID; actual UID1001 execution is also
    # checked by the independent native fixture, never by root product imports.
    monkeypatch.setattr(m.os,'geteuid',lambda:1001);monkeypatch.setattr(m.os,'getuid',lambda:1001)
    monkeypatch.setattr(m,'directories',lambda _:None)
    unit='eimemory-rpc.service'
    policy=home/(unit+'.d')/m.POLICY;policy.parent.mkdir();os.chown(policy.parent,1001,1001);policy.write_bytes(b'original')
    real_read=m.read
    def read(path):
        if not Path(path).exists():return None
        raw=Path(path).read_bytes()
        return {'kind':'file','raw_hex':raw.hex(),'sha256':sha256(raw).hexdigest(),'mode':0o600}
    monkeypatch.setattr(m,'read',read)
    original=m.snapshot([unit],home=home)
    policy.write_bytes(b'new root reviewed policy')
    assert m.restore(original,home=home)['ok'] and policy.read_bytes()==b'original'
    (home/unit).write_bytes(b'concurrent user config')
    with pytest.raises(ValueError,match='unrelated_configuration_changed'):m.restore(original,home=home)


@pytest.mark.skipif(sys.platform=='win32',reason='real bash managed same-current transition protocol')
@pytest.mark.parametrize('context,marker',[(False,False),(True,False),(True,True)])
def test_managed_already_current_uses_bound_protocol_without_rebuilding(tmp_path,context,marker):
    import subprocess
    text=(ROOT/'deploy/install_immutable_release.sh').read_text()
    start=text.index('_run_managed_already_current() {')
    end=text.index('# Threat boundary:',start)
    helpers=text[start:end]
    pending=tmp_path/'pending'
    if marker:pending.write_text('{}')
    commands=[]
    for function in ('_stop_storage_writers','_runtime_manager_phase','_verify_release_health','_record_deployment_receipt','_run_post_deploy_validation','_restart_storage_writers'):
        commands.append(function+'() { echo '+function+' "$@"; }')
    shell='set -e\n'+helpers+'\n'+'\n'.join(commands)+f"\nSTORAGE_TRANSACTION_MARKER='{pending}'\nSTORAGE_TRANSACTION_CLEARING='{tmp_path/'clearing'}'\nSTORAGE_TRANSACTION_RECOVERY='{tmp_path/'recovery'}'\n"+f"EIMEMORY_RUNTIME_TRANSITION_JOB={'a'*64 if context else ''}\nEIMEMORY_CODE_EVOLUTION_TRANSACTION_MODE=1\nRELEASE_DIR=/private/immutable\nCOMMIT={'b'*40}\n_run_managed_already_current\n_finish_managed_already_current\n"
    result=subprocess.run(['bash','-c',shell],capture_output=True,text=True)
    if marker:
        assert result.returncode==2 and not result.stdout
    elif not context:
        assert result.returncode==0 and not result.stdout
    else:
        assert result.returncode==0
        lines=result.stdout.splitlines()
        assert lines[:3]==['_stop_storage_writers','_runtime_manager_phase publish','_runtime_manager_phase activate']
        assert lines[-2:]==['_restart_storage_writers','managed_already_current=technical_verified_restore_deferred']
        assert '_record_deployment_receipt' in lines and '_run_post_deploy_validation' in lines
        assert not any('rebuild' in line or 'stage_built' in line for line in lines)


@pytest.mark.parametrize('fault',[None,'serving_changed','storage_pending','active_job_unknown','bad_history_digest'])
def test_restore_owner_requires_clean_original_release_and_stops_new_rpc_first(monkeypatch,tmp_path,fault):
    from types import SimpleNamespace
    m=standalone('bootstrap_runtime_manager');r=registry();calls=[]
    history=tmp_path/'bootstrap-20261019T120000Z-1';history.mkdir()
    releases=tmp_path/'releases';releases.mkdir()
    prior={unit:{'LoadState':'not-found','ActiveState':'inactive','UnitFileState':'not-found'} for unit in c.REGISTERED_UNITS}
    for unit in ('eimemory-rpc.service','eimemory-learn-watch.timer'):
        prior[unit]={'LoadState':'loaded','ActiveState':'active','UnitFileState':'enabled'}
    prior_system={unit:{'LoadState':'not-found','ActiveState':'inactive','UnitFileState':'not-found'} for unit in c.REGISTERED_UNITS|{'eimemory-runtime-manager.service'}}
    files={'schema':'eimemory.runtime_owner_user_files.v1','units':sorted(c.REGISTERED_UNITS),'files':{}}
    snapshot=dict(schema='eimemory.runtime_owner_snapshot.v1',commit='a'*40,
        root_files={},user_files=files,prior_user_state=prior,prior_system_state=prior_system)
    raw=c.canonical(snapshot);(history/'owner-snapshot.json').write_bytes(raw)
    registry_path=tmp_path/'registry.json';registry_path.write_bytes(c.canonical(r))
    monkeypatch.setattr(m,'STATE',tmp_path);monkeypatch.setattr(m,'REGISTRY',registry_path)
    monkeypatch.setattr(m,'RELEASES',releases)
    monkeypatch.setattr(m,'CURRENT',SimpleNamespace(resolve=lambda **_:releases/('b'*40 if fault=='serving_changed' else 'a'*40)))
    monkeypatch.setattr(m,'load_contract',lambda:c)
    monkeypatch.setattr(m,'directory',lambda path,**_:Path(path))
    monkeypatch.setattr(m.stat,'S_IMODE',lambda _:0o700)
    monkeypatch.setattr(m,'read',lambda path,**_:Path(path).read_bytes())
    monkeypatch.setattr(m,'write',lambda path,raw,**kw:Path(path).write_bytes(raw))
    account=SimpleNamespace(pw_name='darrow',pw_uid=1001,pw_gid=1001,pw_dir='/home/darrow')
    if sys.platform=='win32':monkeypatch.setitem(sys.modules,'pwd',SimpleNamespace(getpwnam=lambda _:account))
    else:
        import pwd
        monkeypatch.setattr(pwd,'getpwnam',lambda _:account)
    def storage():
        if fault=='storage_pending':raise ValueError('bootstrap_storage_recovery_not_clean')
    monkeypatch.setattr(m,'storage_clean',storage)
    monkeypatch.setattr(m,'owner_files',lambda account,operation,value:calls.append(('owner_files',operation)) or files)
    monkeypatch.setattr(m,'restore_root_files',lambda _:calls.append(('root_bytes','restore')))
    legacy_active=set();system_stopped=set()
    def call(args):
        calls.append(tuple(args))
        is_user='--user' in args
        if is_user:
            index=args.index('--user')+1;action=args[index];unit=args[index+1] if index+1<len(args) else ''
            if action=='start':legacy_active.add(unit)
            if action=='show':return 'ActiveState='+('active' if unit in legacy_active else 'inactive')+'\n'
            return ''
        if len(args)==2:return ''
        action=args[1];unit=args[2]
        if action=='stop':system_stopped.add(args[-1])
        if action=='show':
            if unit.startswith(c.JOB_UNIT_PREFIX):return 'LoadState=loaded\nActiveState=active\nResult=success\nExecMainStatus=0\n'
            if '--property=LoadState,ActiveState' in args:return 'LoadState=loaded\nActiveState='+('inactive' if unit in system_stopped else 'active')+'\n'
            return 'ActiveState=inactive\n'
        return ''
    monkeypatch.setattr(m,'call',call)
    def health(commit):
        assert 'eimemory-rpc.service' in legacy_active
        assert 'eimemory-rpc.service' in system_stopped
        calls.append(('health',commit));return {'ok':True,'commit':commit}
    monkeypatch.setattr(m,'healthy_original',health)
    args=SimpleNamespace(history_id=history.name,expected_history_sha256=sha256(raw).hexdigest(),
        expected_current_commit='a'*40,expected_registry_sha256=sha256(registry_path.read_bytes()).hexdigest(),abandon_failed_job_key=None)
    if fault=='bad_history_digest':args.expected_history_sha256='0'*64
    if fault=='active_job_unknown':
        key='f'*64;(tmp_path/'active-transition.json').write_bytes(c.canonical({'job_key':key,'complete':False}))
        args.abandon_failed_job_key=key
    if fault:
        with pytest.raises(ValueError):m.restore_owner(args)
        assert not legacy_active and not system_stopped
    else:
        m.restore_owner(args)
        assert legacy_active=={'eimemory-rpc.service','eimemory-learn-watch.timer'}
        assert (history/'restore-complete.json').exists()
        first_start=next(i for i,cmd in enumerate(calls) if '--user' in cmd and 'start' in cmd)
        assert any(cmd[:2]==('/usr/bin/systemctl','stop') and cmd[-1]=='eimemory-rpc.service' for cmd in calls[:first_start])



def validation_fixture(monkeypatch,tmp_path):
    from types import SimpleNamespace
    module,b,r,calls=bridge_fixture(monkeypatch)
    req=job();key=c.digest(req)
    r['runtime_commit']=req['target_commit']
    releases=tmp_path/'releases';releases.mkdir();controller=releases/req['controller_commit'];controller.mkdir()
    target=releases/req['target_commit'];target.mkdir()
    entry=controller/c.VALIDATION_ENTRY;entry.parent.mkdir();entry.write_bytes(b'fixed reviewed validation entry')
    r['validation_entry_digest']=sha256(entry.read_bytes()).hexdigest()
    parent=dict(job_key=key,request=req,registry=r,unit=c.job_unit(key),original_invocation='a'*32,
        phase='activate',quiesced=True,resumed=False)
    b.state=tmp_path;(tmp_path/'validations').mkdir();(tmp_path/'validation-results').mkdir()
    store={};artifact={'raw':None};state={'LoadState':'loaded','ActiveState':'activating','SubState':'start',
        'Result':'success','InvocationID':'b'*32,'ExecMainStatus':'0','MainPID':'123',
        'ControlGroup':'/system.slice/'+c.validation_unit(key,'receipt')}
    monkeypatch.setattr(module,'RELEASES',releases)
    monkeypatch.setattr(module,'CURRENT',SimpleNamespace(resolve=lambda **_:target))
    monkeypatch.setattr(module,'trusted_directory',lambda *_a,**_k:None)
    monkeypatch.setattr(b,'record',lambda _:deepcopy(parent))
    monkeypatch.setattr(b,'verify_job_peer',lambda *_:None)
    monkeypatch.setattr(b,'unit_state',lambda *_:deepcopy(state))
    monkeypatch.setattr(b,'validation_artifact',lambda *_:artifact['raw'])
    monkeypatch.setattr(module,'read_file',lambda path,**_:c.canonical(store[str(path)]) if str(path) in store else Path(path).read_bytes())
    def atomic(path,value,**kwargs):
        if kwargs.get('exclusive') and str(path) in store:raise FileExistsError(path)
        Path(path).touch();store[str(path)]=deepcopy(value)
    monkeypatch.setattr(module,'atomic_json',atomic)
    def copy(path,raw,gid,exclusive=False):
        if exclusive and str(path) in store:raise FileExistsError(path)
        Path(path).touch();store[str(path)]=c.strict_json(raw,max_bytes=9*1024*1024)
    monkeypatch.setattr(b,'write_root_bytes',copy)
    b.proc=tmp_path/'proc';process=b.proc/'42';process.mkdir(parents=True)
    (process/'status').write_text('Uid: 1001 1001 1001 1001\nNoNewPrivs: 1\nCapEff: 0000000000000000\n')
    (process/'cgroup').write_text('0::/system.slice/'+c.validation_unit(key,'receipt')+'\n')
    return module,b,r,req,key,parent,store,state,artifact,calls


def test_validation_submission_is_one_shot_and_readonly_status_never_requires_parent_cgroup(monkeypatch,tmp_path):
    _m,b,r,_req,key,_parent,_store,_state,_artifact,calls=validation_fixture(monkeypatch,tmp_path)
    first=b.validate_job(r,key,'receipt',42,1001)
    assert first['operation']=='receipt' and first['original_invocation']=='b'*32
    assert first['parent_original_invocation']=='a'*32
    assert first['artifact_basename']=='validation-receipt-result.json'
    assert len(calls)==1 and calls[0][1:3]==['start','--no-block']
    monkeypatch.setattr(b,'verify_job_peer',lambda *_:pytest.fail('existing validation is readback only'))
    assert b.validate_job(r,key,'receipt',43,1001)==first
    assert b.validation_status(r,key,'receipt')==first and len(calls)==1


def test_validation_ambiguous_dispatch_is_not_restarted(monkeypatch,tmp_path):
    _m,b,r,_req,key,_parent,store,_state,_artifact,_calls=validation_fixture(monkeypatch,tmp_path)
    dispatched=[]
    def start(*args):
        dispatched.append(args)
        assert store
        raise ValueError('manager_command_failed')
    monkeypatch.setattr(b,'systemctl',start)
    with pytest.raises(ValueError,match='command_failed'):b.validate_job(r,key,'receipt',42,1001)
    assert b.validate_job(r,key,'receipt',42,1001)['original_invocation']=='b'*32
    assert len(dispatched)==1


@pytest.mark.parametrize('fault',['business_before_resume','entry_tamper','different_current','receipt_before_quiesce','retired_parent','wrong_operation'])
def test_validation_cannot_choose_entry_scope_current_or_early_business(monkeypatch,tmp_path,fault):
    from types import SimpleNamespace
    m,b,r,_req,key,parent,_store,_state,_artifact,calls=validation_fixture(monkeypatch,tmp_path)
    operation='receipt'
    if fault=='business_before_resume':operation='post_resume'
    elif fault=='entry_tamper':r['validation_entry_digest']='0'*64
    elif fault=='different_current':monkeypatch.setattr(m,'CURRENT',SimpleNamespace(resolve=lambda **_:m.RELEASES/('c'*40)))
    elif fault=='receipt_before_quiesce':parent['quiesced']=False
    elif fault=='retired_parent':parent['finalized']=True
    elif fault=='wrong_operation':operation='shell'
    with pytest.raises(ValueError):b.validate_job(r,key,operation,42,1001)
    assert calls==[]


def test_business_validation_requires_resumed_phase_and_preserves_controller_serving_distinction(monkeypatch,tmp_path):
    _m,b,r,req,key,parent,_store,state,_artifact,_calls=validation_fixture(monkeypatch,tmp_path)
    parent.update(resumed=True,phase='resumed_awaiting_business')
    state['ControlGroup']='/system.slice/'+c.validation_unit(key,'post_resume')
    result=b.validate_job(r,key,'post_resume',42,1001)
    assert result['target_commit']==req['candidate_commit']
    assert result['controller_commit']==req['base_commit']
    assert result['phase']=='resumed_awaiting_business'


def test_terminal_validation_seals_original_artifact_and_survives_unit_gc(monkeypatch,tmp_path):
    _m,b,r,_req,key,_parent,_store,state,artifact,_calls=validation_fixture(monkeypatch,tmp_path)
    b.validate_job(r,key,'receipt',42,1001)
    artifact['raw']=c.canonical({'fixture':'actual original report bytes'})
    before=sha256(artifact['raw']).hexdigest()
    original=b.seal_validation(r,key,'receipt','b'*32,before,42,1001)
    assert original['sealed_artifact']['artifact_sha256']==before
    state.update(ActiveState='inactive',SubState='dead',MainPID='0')
    sealed=b.validation_status(r,key,'receipt')
    assert sealed['artifact_sha256']==sha256(artifact['raw']).hexdigest()
    assert sealed['terminal_state']['InvocationID']=='b'*32
    state.update(ControlGroup='')
    assert b.validation_status(r,key,'receipt')['terminal_state']==sealed['terminal_state']
    state.update(LoadState='not-found',InvocationID='')
    old=b.validation_status(r,key,'receipt')
    assert old['original_invocation']=='b'*32 and old['terminal_state']==sealed['terminal_state']
    artifact['raw']=c.canonical({'fixture':'replaced report'})
    assert b.validation_status(r,key,'receipt')['artifact_sha256']==before
    # The root-owned immutable copy remains the actual original bytes even if
    # another same-UID process replaces its own mutable output after sealing.


def test_native_success_without_result_cannot_be_sealed(monkeypatch,tmp_path):
    _m,b,r,_req,key,_parent,_store,state,_artifact,_calls=validation_fixture(monkeypatch,tmp_path)
    b.validate_job(r,key,'receipt',42,1001)
    state.update(ActiveState='inactive',SubState='dead',MainPID='0')
    with pytest.raises(ValueError,match='success_artifact_not_sealed'):b.validation_status(r,key,'receipt')


def test_validation_wire_operation_cannot_overwrite_validation_enum(monkeypatch):
    monkeypatch.setattr(client,'request',lambda operation,**fields:{'schema':c.RESPONSE_SCHEMA,'operation':operation,
        'registry_generation':'1'*64,'validation':{'operation':fields['validation_operation'],'job_key':fields['job_key']}})
    assert client.validate_deployment_job('a'*64,'receipt')['operation']=='receipt'
    assert client.deployment_validation_status('a'*64,'post_resume')['operation']=='post_resume'


def test_actual_existing_timestamp_pyc_is_ignored_with_explicit_empty_prefix(tmp_path):
    import py_compile
    import subprocess
    source=tmp_path/'cacheprobe.py';cache=tmp_path/'protected-empty';cache.mkdir()
    poisoned="value='poison'\n";reviewed="value='source'\n"
    assert len(poisoned)==len(reviewed)
    source.write_text(poisoned);os.utime(source,(1000000000,1000000000))
    py_compile.compile(str(source),doraise=True)
    source.write_text(reviewed);os.utime(source,(1000000000,1000000000))
    command="import sys;sys.path.insert(0,sys.argv[1]);import cacheprobe;print(cacheprobe.value)"
    def value(flags):
        return subprocess.check_output([sys.executable,'-I','-B',*flags,'-c',command,str(tmp_path)],text=True).strip()
    assert value([])=='poison'  # -B alone still executes existing bytecode.
    assert value(['-X','pycache_prefix='+str(cache)])=='source'
    assert not any(cache.iterdir()) and source.read_text()==reviewed


@pytest.mark.skipif(sys.platform=='win32' or os.geteuid()!=0,reason='actual protected root-only POSIX pycache fixture')
@pytest.mark.parametrize('fault',[None,'nonempty','writable','symlink'])
def test_fixed_empty_pycache_rejects_cached_or_untrusted_directory(monkeypatch,fault):
    import tempfile,shutil
    m=standalone('runtime_manager_launch')
    base=Path(tempfile.mkdtemp(prefix='eimemory-manager-cache-fs-',dir='/run'))
    assert base.resolve().parent==Path('/run')
    cache=base/'cache';cache.mkdir(mode=0o555)
    monkeypatch.setattr(m,'EMPTY_PYCACHE',cache)
    monkeypatch.setenv('PYTHONPYCACHEPREFIX','preexisting-fixture')
    monkeypatch.setenv('PYTHONDONTWRITEBYTECODE','1')
    if fault=='nonempty':(cache/'injected.pyc').write_bytes(b'untrusted cached instructions')
    elif fault=='writable':cache.chmod(0o777)
    elif fault=='symlink':cache.rmdir();cache.symlink_to(base,target_is_directory=True)
    try:
        if fault:
            with pytest.raises(ValueError):m.protected_cache()
        else:
            flags=m.protected_cache()
            assert flags==['-I','-B','-X','pycache_prefix='+str(cache)]
            assert os.environ['PYTHONPYCACHEPREFIX']==str(cache)
    finally:
        assert base.resolve().parent==Path('/run') and base.name.startswith('eimemory-manager-cache-fs-')
        shutil.rmtree(base)


def test_root_reviewed_templates_apply_explicit_pyc_flags_and_real_readonly_validator_mounts():
    builder=standalone('build_runtime_manager_bundle');files=builder.build(ROOT,'a'*40)
    for unit in ('eimemory-learn-watch.service','eimemory-nightly.service','eimemory-rpc.service'):
        source=files['templates/'+unit].decode()
        start=next(line for line in source.splitlines() if line.startswith('ExecStart='))
        assert '-I -B -X pycache_prefix=/var/lib/eimemory-manager/empty-pycache' in start
    for op,prefix in c.VALIDATION_PREFIXES.items():
        source=files['templates/'+prefix+'.service'].decode()
        assert 'ReadOnlyPaths=/opt/eimemory/releases /usr/libexec/eimemory-manager' in source
        assert 'runtime_manager_launch.py --validation %i '+op in source
        assert 'User=darrow' in source and 'PrivateUsers=self' in source and 'NoNewPrivileges=yes' in source
    assert 'ExecCondition=' not in files['validation-runtime-policy.conf'].decode()


@pytest.mark.parametrize('fault',['wrong_cgroup','wrong_invocation','wrong_digest','nonzero_caps','no_nnp'])
def test_validation_root_seal_requires_actual_child_identity_and_its_exact_bytes(monkeypatch,tmp_path,fault):
    _m,b,r,_req,key,_parent,_store,state,artifact,calls=validation_fixture(monkeypatch,tmp_path)
    b.validate_job(r,key,'receipt',42,1001)
    artifact['raw']=c.canonical({'fixture':'original child raw bytes'})
    digest=sha256(artifact['raw']).hexdigest();invocation='b'*32
    if fault=='wrong_cgroup':(b.proc/'42'/'cgroup').write_text('0::/system.slice/'+c.job_unit(key)+'\n')
    elif fault=='wrong_invocation':invocation='c'*32
    elif fault=='wrong_digest':digest='0'*64
    elif fault=='nonzero_caps':(b.proc/'42'/'status').write_text('Uid: 1001 1001 1001 1001\nNoNewPrivs: 1\nCapEff: 0000000000000001\n')
    elif fault=='no_nnp':(b.proc/'42'/'status').write_text('Uid: 1001 1001 1001 1001\nNoNewPrivs: 0\nCapEff: 0000000000000000\n')
    with pytest.raises(ValueError):b.seal_validation(r,key,'receipt',invocation,digest,42,1001)
    assert not (tmp_path/'validation-results'/(key+'-receipt.json')).exists()


def test_manual_resume_of_legacy_target_holds_background_and_keeps_original_set(monkeypatch,tmp_path):
    _m,b,r,_req,key,records,stored,_target,calls,persist=lifecycle_fixture(monkeypatch,tmp_path)
    original=['eimemory-rpc.service','eimemory-learn-watch.timer']
    records[key].update(manual_release=True,quiesced=True,active_before=original)
    result=b.transition(r,key,'resume',42,1001)
    assert result['phase']=='completed_held' and result['resumed'] is False
    assert result['held_background']==['eimemory-learn-watch.timer'] and calls==[]
    assert records[key]['active_before']==original and records[key]['finalized'] is True
    assert stored[str(tmp_path/'active-transition.json')]['held'] is True



def test_legacy_failed_prefix_is_disabled_without_stop_or_reset(monkeypatch):
    from types import SimpleNamespace
    m=standalone('bootstrap_runtime_manager');calls=[]
    failed='eimemory-nightly.service';active='eimemory-learn-watch.timer'
    prior={failed:{'LoadState':'loaded','ActiveState':'failed','Result':'exit-code','InvocationID':'a'*32,'ExecMainStatus':'1'},
        active:{'LoadState':'loaded','ActiveState':'active'}}
    def call(args):
        calls.append(args)
        if 'show' in args:
            if failed in args:return 'ActiveState=failed\nResult=exit-code\nInvocationID='+('a'*32)+'\nExecMainStatus=1\n'
            return 'ActiveState=inactive\n'
        return ''
    monkeypatch.setattr(m,'call',call)
    account=SimpleNamespace(pw_name='darrow',pw_uid=1001)
    captured=m.retire_legacy_user_owners(account,prior,set(prior))
    assert captured==[active]
    assert any('disable' in args and failed in args for args in calls)
    assert not any(('stop' in args or 'reset-failed' in args or '--now' in args) and failed in args for args in calls)
    assert next(args for args in calls if 'disable' in args)[-1]==active


def test_manager_reinstall_cannot_replace_live_root_authority_in_place(monkeypatch,tmp_path):
    from types import SimpleNamespace
    m=standalone('bootstrap_runtime_manager');repository=tmp_path/'repo';repository.mkdir()
    path=tmp_path/'registry.json';path.write_bytes(b'protected original root registry')
    monkeypatch.setattr(m,'REGISTRY',path);monkeypatch.setattr(m,'STATE',tmp_path)
    monkeypatch.setattr(m,'read',lambda p:Path(p).read_bytes())
    monkeypatch.setitem(sys.modules,'pwd',SimpleNamespace(getpwnam=lambda _:SimpleNamespace(pw_uid=1001,pw_gid=1001,pw_dir='/home/darrow')))
    monkeypatch.setattr(m,'write',lambda *_args,**_kwargs:pytest.fail('root source must not be replaced'))
    with pytest.raises(ValueError,match='existing_manager_upgrade_requires_reviewed_handoff'):
        m.install(SimpleNamespace(repository_root=str(repository),expected_registry_sha256=sha256(path.read_bytes()).hexdigest()))


@pytest.mark.skipif(sys.platform=='win32' or os.geteuid()!=0,
    reason='actual root registry parent ownership and permissions require POSIX root')
@pytest.mark.parametrize('parent_uid',[0,1001])
def test_actual_registry_rejects_runtime_parent_without_changing_configuration_owner(parent_uid):
    import tempfile,shutil
    base=Path(tempfile.mkdtemp(prefix='eimemory-manager-registry-parent-',dir='/run'))
    assert base.resolve().parent==Path('/run')
    root_parent=base/'protected-authority';root_parent.mkdir(mode=0o750)
    runtime_config=base/'runtime-configuration';runtime_config.mkdir(mode=0o750)
    os.chown(runtime_config,1001,1001)
    selected=root_parent if parent_uid==0 else runtime_config
    path=selected/'runtime-manager.v1.json'
    value=registry();path.write_bytes(c.canonical(value));path.chmod(0o640);os.chown(path,0,1001)
    try:
        if parent_uid==0:
            assert client.read_registry(path)==value
        else:
            with pytest.raises(client.RuntimeManagerError,match='parent_untrusted'):
                client.read_registry(path)
        # The security fix moves the trust anchor; it never takes over the
        # legitimate runtime configuration or its child policy write surface.
        assert runtime_config.stat().st_uid==1001 and runtime_config.stat().st_gid==1001
        assert c.REGISTRY_PATH=='/etc/eimemory-code-automation-authority/runtime-manager.v1.json'
    finally:
        assert base.resolve().parent==Path('/run') and base.name.startswith('eimemory-manager-registry-parent-')
        shutil.rmtree(base)

def fused_fixture(monkeypatch,tmp_path,*,initial=('active','active')):
    from types import SimpleNamespace
    m,b,r,calls=bridge_fixture(monkeypatch);b.state=tmp_path;b.proc=tmp_path/'proc'
    process=b.proc/'42';process.mkdir(parents=True)
    (process/'status').write_text('Uid: 1001 1001 1001 1001\nNoNewPrivs: 1\nCapEff: 0000000000000000\nCapPrm: 0000000000000000\nCapAmb: 0000000000000000\n')
    (process/'cgroup').write_text('0::/system.slice/'+c.FUSED_UNIT+'\n')
    states={u:dict(LoadState='loaded',ActiveState=v,Result='exit-code' if v=='failed' else 'success') for u,v in zip(c.FUSED_L1_UNITS,initial)}
    native=dict(LoadState='loaded',ActiveState='active',InvocationID='a'*32,MainPID='50',
        ControlGroup='/system.slice/'+c.FUSED_UNIT,KillMode='control-group',SendSIGKILL='yes')
    for unit in (c.FUSED_UNIT,*c.FUSED_L1_UNITS):r['units'][unit]['installed']=True
    releases=tmp_path/'releases';target=releases/r['runtime_commit'];target.mkdir(parents=True)
    monkeypatch.setattr(m,'CURRENT',SimpleNamespace(resolve=lambda **_:target));monkeypatch.setattr(m,'RELEASES',releases)
    monkeypatch.setattr(m,'trusted_directory',lambda *_args,**_kwargs:None)
    monkeypatch.setattr(m.os,'chown',lambda *_args:None,raising=False)
    monkeypatch.setattr(m,'read_file',lambda path,**_:Path(path).read_bytes())
    def save(path,value,**kwargs):
        path=Path(path)
        if kwargs.get('exclusive') and path.exists():raise FileExistsError(path)
        path.write_bytes(c.canonical(value))
    monkeypatch.setattr(m,'atomic_json',save)
    monkeypatch.setattr(b,'unit_state',lambda reg,unit,props:{p:(native if unit==c.FUSED_UNIT else states[unit])[p] for p in props})
    def command(reg,unit,*args):
        calls.append((unit,args))
        if args[0]=='stop':states[unit]['ActiveState']='inactive'
        elif args[0]=='start':states[unit]['ActiveState']='active';states[unit]['Result']='success'
    monkeypatch.setattr(b,'systemctl',command)
    return m,b,r,calls,states,native,process,target


@pytest.mark.parametrize('initial',[('active','active'),('inactive','failed'),('active','inactive'),('activating','reloading')])
def test_fused_context_pauses_only_fixed_active_units_and_restores_service_before_timer(monkeypatch,tmp_path,initial):
    _m,b,r,calls,states,_native,_process,_target=fused_fixture(monkeypatch,tmp_path,initial=initial)
    result=b.fused_closure(r,'begin',42,1001);key=result['context_key']
    active=[u for u,s in zip(c.FUSED_L1_UNITS,initial) if s in {'active','activating','reloading'}]
    assert result['active_before']==active
    assert [u for u,args in calls if args[0]=='stop']==active
    before=list(calls);assert b.fused_closure(r,'begin',42,1001)['idempotent'] is True
    assert calls==before
    assert b.fused_closure(r,'end',42,1001,key)['restored'] is True
    assert [u for u,args in calls if args[0]=='start']==list(reversed(active))
    assert states[c.FUSED_L1_UNITS[1]]['ActiveState']==('active' if c.FUSED_L1_UNITS[1] in active else initial[1])
    assert c.strict_json((tmp_path/'active-transition.json').read_bytes())['complete'] is True
    assert b.fused_record(key)['active_before']==active


@pytest.mark.parametrize('fault',['wrong_cgroup','wrong_invocation','no_nnp','caps','uid','third_release','foreign_transition','unknown_l1'])
def test_fused_admission_has_no_arbitrary_unit_or_identity_authority(monkeypatch,tmp_path,fault):
    from types import SimpleNamespace
    m,b,r,calls,states,native,process,target=fused_fixture(monkeypatch,tmp_path)
    uid=1001
    if fault=='wrong_cgroup':(process/'cgroup').write_text('0::/system.slice/eimemory-nightly.service\n')
    elif fault=='wrong_invocation':native['InvocationID']=''
    elif fault=='no_nnp':(process/'status').write_text((process/'status').read_text().replace('NoNewPrivs: 1','NoNewPrivs: 0'))
    elif fault=='caps':(process/'status').write_text((process/'status').read_text().replace('CapPrm: 0000000000000000','CapPrm: 0000000000000001'))
    elif fault=='uid':uid=65534
    elif fault=='third_release':monkeypatch.setattr(m,'CURRENT',SimpleNamespace(resolve=lambda **_:target.parent/('b'*40)))
    elif fault=='foreign_transition':(tmp_path/'active-transition.json').write_bytes(c.canonical({'job_key':'b'*64,'complete':False}))
    elif fault=='unknown_l1':states[c.FUSED_L1_UNITS[0]]['ActiveState']='deactivating'
    with pytest.raises(ValueError):b.fused_closure(r,'begin',42,uid)
    assert calls==[]


@pytest.mark.parametrize('failed_unit',c.FUSED_L1_UNITS)
def test_fused_partial_pause_failure_restores_original_set_without_resetting_prior_failed(monkeypatch,tmp_path,failed_unit):
    _m,b,r,calls,states,_native,_process,_target=fused_fixture(monkeypatch,tmp_path)
    original=b.systemctl
    def command(reg,unit,*args):
        original(reg,unit,*args)
        if unit==failed_unit and args[0]=='stop':raise ValueError('manager_command_failed')
    monkeypatch.setattr(b,'systemctl',command)
    with pytest.raises(ValueError,match='command_failed'):b.fused_closure(r,'begin',42,1001)
    assert all(states[u]['ActiveState']=='active' for u in c.FUSED_L1_UNITS)
    pointer=c.strict_json((tmp_path/'active-transition.json').read_bytes())
    assert pointer['complete'] is True and b.fused_record(pointer['job_key'])['pause_failed'] is True


@pytest.mark.parametrize('new_invocation',[False,True])
def test_native_stoppost_or_next_prestart_recovers_durable_context_after_bridge_restart(monkeypatch,tmp_path,new_invocation):
    _m,b,r,calls,states,native,_process,_target=fused_fixture(monkeypatch,tmp_path)
    original=b.fused_closure(r,'begin',42,1001);old=b.fused_record(original['context_key'])
    native.update(ActiveState='activating' if new_invocation else 'deactivating',MainPID='0')
    if new_invocation:native['InvocationID']='b'*32
    # The root bytes, not a process-local active-set cache, are sufficient.
    restored=b.fused_closure(r,'recover',42,1001)
    assert restored['restored'] is True and all(states[u]['ActiveState']=='active' for u in c.FUSED_L1_UNITS)
    assert b.fused_record(original['context_key'])['invocation']==old['invocation']
    assert b.fused_closure(r,'recover',42,1001)['phase']=='no_pending_context'


def test_fused_recovery_refuses_running_main_and_deploy_refuses_active_fused(monkeypatch,tmp_path):
    _m,b,r,_calls,_states,native,_process,_target=fused_fixture(monkeypatch,tmp_path)
    begun=b.fused_closure(r,'begin',42,1001)
    with pytest.raises(ValueError,match='requires_native_hook'):b.fused_closure(r,'recover',42,1001)
    native['InvocationID']='b'*32
    with pytest.raises(ValueError,match='original_invocation'):b.fused_closure(r,'end',42,1001,begun['context_key'])


def test_fused_template_identity_and_hooks_are_fixed_root_reviewed_commands():
    files=standalone('build_runtime_manager_bundle').build(ROOT,'a'*40)
    text=files['templates/'+c.FUSED_UNIT].decode()
    assert 'Environment=USER=darrow' in text and 'USER=%u' not in text
    assert 'KillMode=control-group' in text and 'SendSIGKILL=yes' in text
    assert text.count('runtime_manager_launch.py --recover-fused')==2


def test_fused_restore_failure_retains_capture_and_failed_invocation_until_native_recovery(monkeypatch,tmp_path):
    _m,b,r,calls,states,native,_process,_target=fused_fixture(monkeypatch,tmp_path)
    original=b.fused_closure(r,'begin',42,1001);real=b.systemctl
    def command(reg,unit,*args):
        if args[0]=='start' and unit.endswith('.timer'):raise ValueError('manager_start_failed')
        return real(reg,unit,*args)
    monkeypatch.setattr(b,'systemctl',command)
    with pytest.raises(ValueError,match='start_failed'):b.fused_closure(r,'end',42,1001,original['context_key'])
    assert b.fused_record(original['context_key'])['active_before']==list(c.FUSED_L1_UNITS)
    assert c.strict_json((tmp_path/'active-transition.json').read_bytes())['complete'] is False
    native.update(ActiveState='deactivating',MainPID='0');monkeypatch.setattr(b,'systemctl',real)
    assert b.fused_closure(r,'recover',42,1001)['restored'] is True


def test_deployment_quiesce_refuses_fused_context_without_loading_a_fake_job(monkeypatch,tmp_path):
    _m,b,r,_req,key,_records,_stored,_target,calls,persist=lifecycle_fixture(monkeypatch,tmp_path)
    persist({'context':'fused_closure','job_key':'f'*64,'complete':False})
    with pytest.raises(ValueError,match='another_transition_active'):b.transition(r,key,'quiesce',42,1001)
    assert calls==[]


@pytest.mark.parametrize('pointer',[{'context':'deployment','job_key':'a'*64,'complete':False},
    {'context':'fused_closure','job_key':'b'*64,'complete':True}])
def test_fused_stop_hook_does_not_reenter_deployment_lifecycle_lock(monkeypatch,pointer):
    m=standalone('runtime_manager_launch')
    monkeypatch.setattr(m,'protected_cache',lambda:[])
    monkeypatch.setattr(m.os,'getuid',lambda:1001,raising=False);monkeypatch.setattr(m.os,'geteuid',lambda:1001,raising=False)
    monkeypatch.setattr(m,'read_root',lambda path:registry() if Path(path)==m.REGISTRY else pointer)
    monkeypatch.setattr(m.Path,'exists',lambda _:True)
    monkeypatch.setattr(m.socket,'socket',lambda *_args:pytest.fail('must not call a locked bridge for a nonpending fused context'))
    m.recover_fused()


def test_product_templates_use_root_reviewed_source_launcher_even_for_old_first_bootstrap():
    files=standalone('build_runtime_manager_bundle').build(ROOT,'a'*40)
    for name in ('eimemory-rpc.service','eimemory-console.service','eimemory-nightly.service','eimemory-l1-extract.service','eimemory-vector-sync.service','eimemory-audit-verify.service'):
        text=files['templates/'+name].decode()
        assert '/usr/libexec/eimemory-manager/runtime_manager_launch.py --product-module eimemory.' in text
        assert ' -m eimemory.' not in text and '/opt/eimemory/current/deploy/runtime_manager_launch.py' not in text


@pytest.mark.parametrize('module',['os','eimemory.api.runtime','eimemory.cli.main;sh','../eimemory.cli.main'])
def test_source_launcher_rejects_unregistered_module_before_loading_product(monkeypatch,module):
    m=standalone('runtime_manager_launch')
    monkeypatch.setattr(m,'read_root',lambda *_args,**_kwargs:pytest.fail('unregistered modules never read authority/source'))
    with pytest.raises(ValueError,match='module_not_registered'):m.prepare_product_module(module)
