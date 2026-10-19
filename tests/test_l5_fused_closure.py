from pathlib import Path
import importlib.util
import json
import os
import subprocess
import sys
from types import SimpleNamespace

import pytest

ROOT=Path(__file__).resolve().parents[1]


def test_fused_closure_script_keeps_memory_plane_before_l5_gates():
    text=(ROOT/'deploy/run_memory_l5_fused_closure.sh').read_text()
    assert text.index('stage=memory_plane_eval')<text.index('eval production-query collect')
    assert text.index('eval production-query status')<text.index('learn release-closure')
    assert 'install_immutable_release.sh' not in text and 'systemctl' not in text
    assert 'fused-begin' in text and 'fused-end' in text
    assert text.count('--scope-tenant "$SCOPE_TENANT"')==3
    assert '"$BIN" -m' not in text and '"$BIN" -c' not in text
    assert 'production_query_not_ready' in text


def _run_isolated(tmp_path,*,invalid='',begin_failure=False,end_failure=False,
                  eval_failure=False,invalid_key=False):
    repo=tmp_path/'repo';repo.mkdir()
    current=tmp_path/'current';binary=current/'.venv/bin/python';binary.parent.mkdir(parents=True)
    trace=tmp_path/'trace.jsonl';fake_dir=tmp_path/'bin';fake_dir.mkdir()
    systemctl=fake_dir/'systemctl';systemctl.write_text('#!/bin/sh\nexit 99\n');systemctl.chmod(0o755)
    binary.write_text('#!'+sys.executable+'\n'+'''import json,os,sys
args=sys.argv[1:]
def event(value):
    with open(os.environ['FUSED_TRACE'],'a') as f:f.write(json.dumps(value)+'\\n')
if any(x.endswith('runtime_manager_control.py') for x in args):
    action=next(x for x in args if x.startswith('fused-'));event(['manager',action])
    if action=='fused-scope':
        data=json.loads(os.environ['FUSED_SCOPE']);print('\\n'.join(data[k] for k in ('tenant_id','agent_id','workspace_id','user_id')))
    elif action=='fused-begin':
        if os.environ['FUSED_BEGIN_FAIL']=='1':raise SystemExit(3)
        print('bad-key' if os.environ['FUSED_BAD_KEY']=='1' else 'a'*64)
    elif action=='fused-end':
        if os.environ['FUSED_END_FAIL']=='1':raise SystemExit(5)
        print('{"restored":true}')
    raise SystemExit(0)
event(['python',*args])
if 'eimemory.cli.l1_worker' in args:
    event(['owner',*[os.environ.get('EIMEMORY_DEPLOY_SCOPE_'+k) for k in ('TENANT','AGENT','WORKSPACE','USER')]])
    raise SystemExit(8 if os.environ['FUSED_EVAL_FAIL']=='1' else 0)
print('1' if '-c' in args else '{"ok":true,"ready":true}')
''')
    binary.chmod(0o755);log=tmp_path/'closure.log'
    owner=dict(tenant_id='tenant-A',agent_id='agent-A',workspace_id='workspace::channel',user_id='ou_A')
    env={**os.environ,'PATH':str(fake_dir)+os.pathsep+os.environ.get('PATH','/usr/bin:/bin'),
        'EIMEMORY_TRUSTED_REPOSITORY_ROOT':'' if invalid=='repo' else str(repo),'EIMEMORY_DEPLOYMENT_REPO_ROOT':'',
        'EIMEMORY_DEPLOYMENT_CURRENT_LINK':str(current),'EIMEMORY_PRIOR_COMMIT':'bad' if invalid=='prior' else '1'*40,
        'EIMEMORY_L5_FUSED_LOG':str(log),'EIMEMORY_L5_QUERY_STATUS_JSON':str(tmp_path/'status.json'),
        'FUSED_TRACE':str(trace),'FUSED_SCOPE':json.dumps(owner),'FUSED_BEGIN_FAIL':str(int(begin_failure)),
        'FUSED_END_FAIL':str(int(end_failure)),'FUSED_EVAL_FAIL':str(int(eval_failure)),'FUSED_BAD_KEY':str(int(invalid_key))}
    result=subprocess.run(['bash',str(ROOT/'deploy/run_memory_l5_fused_closure.sh')],env=env,capture_output=True,text=True,encoding='utf-8',errors='replace',timeout=20)
    calls=[json.loads(line) for line in trace.read_text().splitlines()] if trace.exists() else []
    assert log.exists(),result.stderr
    return result,calls,log.read_text(),owner


@pytest.mark.parametrize('invalid',['repo','prior'])
def test_invalid_fused_configuration_never_acquires_context(tmp_path,invalid):
    result,calls,log,_=_run_isolated(tmp_path,invalid=invalid)
    assert result.returncode==2 and calls==[] and 'error=' in log


@pytest.mark.parametrize('eval_failure',[False,True])
@pytest.mark.parametrize('end_failure',[False,True])
def test_fused_always_restores_bound_context_and_preserves_original_failure(tmp_path,eval_failure,end_failure):
    result,calls,log,_=_run_isolated(tmp_path,eval_failure=eval_failure,end_failure=end_failure)
    assert result.returncode==(8 if eval_failure else 2 if end_failure else 0),log
    assert calls[0]==['manager','fused-scope'] and calls[1]==['manager','fused-begin']
    assert calls[-1]==['manager','fused-end']
    assert sum(call==['manager','fused-begin'] for call in calls)==1
    if end_failure:assert 'error=l1_managed_restore_failed' in log


@pytest.mark.parametrize('fault',['begin_failure','invalid_key'])
def test_unconfirmed_pause_never_runs_eval(tmp_path,fault):
    result,calls,log,_=_run_isolated(tmp_path,**{fault:True})
    assert result.returncode==2 and not any(call[0]=='python' for call in calls)
    assert ['manager','fused-end'] not in calls
    # Fixed native ExecStopPost owns recovery even if the begin reply was lost.
    assert 'error=l1_managed_' in log


def test_nondefault_tenant_and_all_four_coords_reach_real_cli_argv_and_l1_env(tmp_path):
    result,calls,log,owner=_run_isolated(tmp_path)
    assert result.returncode==0,log
    assert ['owner',*[owner[k] for k in ('tenant_id','agent_id','workspace_id','user_id')]] in calls
    commands=[call for call in calls if call[0]=='python' and 'eimemory.cli.main' in call and '-c' not in call]
    assert len(commands)==3
    for call in commands:
        for flag,name in [('tenant','tenant_id'),('agent','agent_id'),('workspace','workspace_id'),('user','user_id')]:
            assert call[call.index('--scope-'+flag)+1]==owner[name]


def test_actual_l1_eval_dispatch_preserves_explicit_nondefault_four_scope(monkeypatch):
    from eimemory.cli import l1_worker as worker
    owner=dict(tenant_id='tenant-B',agent_id='agent-B',workspace_id='workspace-B::channel',user_id='ou_B');seen=[]
    for key,value in owner.items():monkeypatch.setenv('EIMEMORY_DEPLOY_SCOPE_'+key.removesuffix('_id').upper(),value)
    monkeypatch.setenv('EIMEMORY_ROOT','isolated-no-store-created');monkeypatch.setenv('EIMEMORY_L1_WORKER_ACTION','eval')
    fake=SimpleNamespace(close=lambda:seen.append('closed'))
    monkeypatch.setattr(worker,'Runtime',SimpleNamespace(create=lambda **_:fake))
    monkeypatch.setattr(worker,'AgentRuntimeMemoryService',lambda rt:rt)
    monkeypatch.setattr(worker,'evaluate_plane',lambda service,*,scope:seen.append(scope) or {'ok':True})
    assert worker.main()==0 and seen==[owner,'closed']


def test_fused_scope_reads_configured_identity_and_explicit_deploy_overrides(monkeypatch):
    from eimemory import identity
    spec=importlib.util.spec_from_file_location('fused_control',ROOT/'deploy/runtime_manager_control.py')
    control=importlib.util.module_from_spec(spec);spec.loader.exec_module(control)
    for key in ('TENANT','AGENT','WORKSPACE','USER'):
        monkeypatch.delenv('EIMEMORY_DEPLOY_SCOPE_'+key,raising=False);monkeypatch.delenv('EIMEMORY_'+key+'_ID',raising=False)
    monkeypatch.setattr(identity,'HONGTU_AGENT_ID','configured-agent');monkeypatch.setattr(identity,'HONGTU_WORKSPACE_ID','configured-workspace')
    monkeypatch.setattr(identity,'default_operator_user_id',lambda:'configured-user')
    assert control.fused_scope()==dict(tenant_id='default',agent_id='configured-agent',workspace_id='configured-workspace',user_id='configured-user')
    monkeypatch.setenv('EIMEMORY_AGENT_ID','generic-agent');monkeypatch.setenv('EIMEMORY_DEPLOY_SCOPE_AGENT','exact-agent')
    monkeypatch.setenv('EIMEMORY_DEPLOY_SCOPE_TENANT','tenant-C')
    assert control.fused_scope()['agent_id']=='exact-agent' and control.fused_scope()['tenant_id']=='tenant-C'
