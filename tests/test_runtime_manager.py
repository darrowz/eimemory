from __future__ import annotations

from copy import deepcopy
from hashlib import sha256
import importlib.util
import io
import json
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
        template_hashes={},compatibility_hashes={name:'0'*64 for name in c.COMPATIBILITY_PATHS})
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
    key='1'*64;record={'unit':c.job_unit(key),'registry':value}
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
