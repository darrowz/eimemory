#!/usr/bin/env python3
"""Runtime-user CLI for exact registry reads and contextual release transitions."""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import sys

# This CLI is exclusively executed as the runtime user, never by root.
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from eimemory.core.runtime_manager import read_registry, unit_state, request, transition


def fused_scope():
    from eimemory.identity import HONGTU_AGENT_ID,HONGTU_WORKSPACE_ID,default_operator_user_id
    from eimemory.contracts.runtime_manager import validate_scope
    return validate_scope({
        'tenant_id':os.environ.get('EIMEMORY_DEPLOY_SCOPE_TENANT') or os.environ.get('EIMEMORY_TENANT_ID') or 'default',
        'agent_id':os.environ.get('EIMEMORY_DEPLOY_SCOPE_AGENT') or os.environ.get('EIMEMORY_AGENT_ID') or HONGTU_AGENT_ID,
        'workspace_id':os.environ.get('EIMEMORY_DEPLOY_SCOPE_WORKSPACE') or os.environ.get('EIMEMORY_WORKSPACE_ID') or HONGTU_WORKSPACE_ID,
        'user_id':os.environ.get('EIMEMORY_DEPLOY_SCOPE_USER') or os.environ.get('EIMEMORY_USER_ID') or default_operator_user_id()})


def systemctl(args):
    args=list(args)
    props=[];units=[];value=False;quiet=False;verb=None;i=0
    while i<len(args):
        item=args[i]
        if item in {'--value'}:value=True
        elif item in {'--quiet','-q'}:quiet=True
        elif item in {'--no-page','--no-pager'}:pass
        elif item in {'-p','--property'}:
            i+=1;props+=args[i].split(',')
        elif item.startswith('--property='):props+=item.partition('=')[2].split(',')
        elif item.startswith('-'):raise ValueError('manager_control_option_forbidden')
        elif verb is None:verb=item
        else:units.append(item)
        i+=1
    if verb=='list-units':
        registry=read_registry()
        for unit,item in registry['units'].items():
            if item['installed'] and unit.endswith('.service'):print(unit)
        return 0
    if verb not in {'show','is-active','is-enabled','cat'} or len(units)!=1:
        raise ValueError('manager_control_mutation_requires_transition')
    unit=units[0]
    if verb in {'is-active','is-enabled'}:
        key='ActiveState' if verb=='is-active' else 'UnitFileState'
        result=unit_state(unit,(key,))[key]
        if not quiet:print(result)
        return 0 if result in ({'active','reloading','activating'} if key=='ActiveState' else {'enabled','enabled-runtime','static','indirect','generated'}) else 3
    if verb=='cat':
        state=unit_state(unit,('LoadState','FragmentPath'))
        if state['LoadState']!='loaded':return 1
        print('# registered root-managed unit '+unit);return 0
    props=props or ['ActiveState','SubState','Result','InvocationID','MainPID','ExecMainStatus']
    if 'Environment' in props:
        if any(p not in {'Environment','LoadState'} for p in props):raise ValueError('manager_environment_query_forbidden')
        commit=request('runtime_identity',unit=unit)['runtime_commit']
        data={'LoadState':'loaded','Environment':'EIMEMORY_RUNTIME_COMMIT='+commit}
    else:data=unit_state(unit,tuple(props))
    for prop in props:print(data[prop] if value else prop+'='+data[prop])
    return 0


def main(argv=None):
    argv=sys.argv[1:] if argv is None else argv
    if hasattr(os,'geteuid') and os.geteuid()==0:raise ValueError('manager_control_runtime_uid_required')
    if not argv:raise ValueError('manager_control_action_required')
    if argv[0]=='systemctl':return systemctl(argv[1:])
    if argv[0]=='inventory':
        print(json.dumps(read_registry()));return 0
    if argv[0]=='transition' and len(argv)==2:
        key=os.environ.get('EIMEMORY_RUNTIME_TRANSITION_JOB','')
        print(json.dumps(transition(key,argv[1])));return 0
    if argv[0]=='probe' and len(argv)==1:
        print(json.dumps(request('readonly_probe',job_key=os.environ.get('EIMEMORY_RUNTIME_TRANSITION_JOB',''))));return 0
    if argv[0]=='single-owner' and len(argv)==1:
        print(json.dumps(request('single_rpc_owner')));return 0
    if argv==['hermes-adapter-timeout']:
        from eimemory.ops.controlled_stability import _host_process_observation
        state=unit_state('hermes-gateway.service',('Id','MainPID'))
        observation=_host_process_observation(state)
        timeout=float(observation['environment'].get('EIMEMORY_ADAPTER_TIMEOUT_SECONDS',''))
        if not 0.1<=timeout<=30.0:raise ValueError('manager_hermes_adapter_timeout_invalid')
        print(f'{timeout:g}');return 0
    if argv==['fused-scope']:
        scope=fused_scope()
        for name in ('tenant_id','agent_id','workspace_id','user_id'):print(scope[name])
        return 0
    if argv==['fused-begin']:
        from eimemory.core.runtime_manager import begin_fused_closure
        result=begin_fused_closure()
        if result.get('phase')!='paused':raise ValueError('manager_fused_pause_not_confirmed')
        key=result.get('context_key','')
        if len(key)!=64 or any(c not in '0123456789abcdef' for c in key):raise ValueError('manager_fused_context_invalid')
        print(key);return 0
    if len(argv)==2 and argv[0]=='fused-end':
        from eimemory.core.runtime_manager import end_fused_closure
        result=end_fused_closure(argv[1])
        if result.get('restored') is not True:raise ValueError('manager_fused_restore_not_confirmed')
        print(json.dumps(result));return 0
    raise ValueError('manager_control_action_forbidden')


if __name__=='__main__':
    try:raise SystemExit(main())
    except (ValueError,RuntimeError) as exc:
        print(type(exc).__name__+': '+str(exc),file=sys.stderr);raise SystemExit(2)
