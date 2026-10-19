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
        managed_incident_authority={'enabled':False},controller_entry_digest=None)
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
    root=tmp_path/'root';units=tmp_path/'units';root.mkdir();units.mkdir()
    monkeypatch.setattr(m,'ROOT',root);monkeypatch.setattr(m,'SYSTEM_UNITS',units);monkeypatch.setattr(m,'REGISTRY',tmp_path/'registry.json')
    old=root/'runtime_manager_bridge.py';old.write_bytes(b'original root bytes');old.chmod(0o640)
    manifest={'runtime_manager_bridge.py':b'reviewed new bytes','templates/eimemory-rpc.service':b'new template'}
    snapshot=m.root_snapshot(manifest)
    old.write_bytes(b'replaced root bytes')
    new=units/'eimemory-rpc.service';new.write_bytes(b'new template');new.chmod(0o640)
    m.restore_root_files(snapshot)
    assert old.read_bytes()==b'original root bytes' and not new.exists()
    with pytest.raises(ValueError,match='path_invalid'):m.restore_root_files({'/root/arbitrary':None})


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
        if action=='disable':system_stopped.add(args[-1])
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
        assert any(cmd[:3]==('/usr/bin/systemctl','disable','--now') and cmd[-1]=='eimemory-rpc.service' for cmd in calls[:first_start])
