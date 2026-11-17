#!/usr/bin/python3
"""Reviewed current-user enrollment; never manufactures historical custody.

Loaded from an explicitly SHA-reviewed user bundle by install_user_runtime_manager.
All writes use a private durable journal. The historical import entry is separate.
"""
from __future__ import annotations

from contextlib import ExitStack, nullcontext
from datetime import datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import shlex
import socket
import stat
import struct
import subprocess
import tempfile

NATIVE_PROPERTIES = ['Id','LoadState','ActiveState','SubState','UnitFileState','MainPID',
                     'ControlPID','ControlGroup','InvocationID','ExecStart','FragmentPath','DropInPaths']
SERVICE_PROPERTIES = frozenset({'MainPID','ControlPID','ControlGroup','ExecStart'})
SOURCE_ENV = ('EIMEMORY_CONTROL_PLANE','EIMEMORY_IDENTITY_AUTHORITY_DIR','EIMEMORY_SUBJECT_PROJECTION_AUTHORITY_DIR',
              'EIMEMORY_CONTROLLED_PRODUCTION_ACCEPTANCE_POLICY','EIMEMORY_TRUSTED_REPOSITORY_ROOT',
              'EIMEMORY_DEPLOYMENT_REPO_ROOT','EIMEMORY_TRUSTED_REMOTE','EIMEMORY_TRUSTED_BRANCH',
              'EIMEMORY_DEPLOYMENT_BRANCH','EIMEMORY_CONFIG_DIR','EIMEMORY_CONFIG_PATH',
              'EIMEMORY_SYSTEM_SETTINGS_PATH','EIMEMORY_ROOT','EIMEMORY_CODE_AUTOMATION_POLICY_JSON',
              'EIMEMORY_CODE_EVOLUTION_KILL_SWITCH')
SOURCE_PROBE = r'''
import json,sys,os
sys.path.insert(0,sys.argv[1])
from eimemory import identity_authority as api
from eimemory.config.trusted import trusted_repository_root
v=json.load(sys.stdin); out={'repository_root':str(trusted_repository_root()),'identity':{}}
for name,path in v['identity'].items():
    if path is None: continue
    with api.snapshot(path,blocking=False) as (a,fd):
        current,reference=api.user_policy_migration(a.policy,v['uid'])
        out['identity'][name]={'current_policy':current,'retained_digest':reference,
            'original_policy':a.policy,'events':len(a.events),
            'retained_policies':{d:p for d,p in a.policies.items() if d!=a.policy_digest},
            'journal_sha256':__import__('hashlib').sha256(api._read(fd,api.MAX_JOURNAL_BYTES)).hexdigest()}
print(json.dumps(out,sort_keys=True,separators=(',',':')))
'''
USER_PROBE = r'''
import json,sys
sys.path.insert(0,sys.argv[1])
from eimemory import identity_authority as api
v=json.load(sys.stdin);out={}
for name,path in v.items():
    # Publication uses the same pure renderer as migrate_user_policy. Verify
    # read-only: a selected user journal may be the source inode still held by
    # the operator's shared custody lock, so an exclusive migration would wait
    # for its own parent and can never succeed.
    with api.snapshot(path,blocking=False) as (a,fd):
        out[name]={'policy_digest':a.policy_digest,'events':len(a.events),
            'journal_sha256':__import__('hashlib').sha256(api._read(fd,api.MAX_JOURNAL_BYTES)).hexdigest()}
print(json.dumps(out,sort_keys=True,separators=(',',':')))
'''
FORWARD_PROBE = r'''
import json,sys,hashlib
sys.path.insert(0,sys.argv[1])
from eimemory import identity_authority as api
v=json.load(sys.stdin);out={}
for name,item in v.items():
    with api.snapshot(item['path'],blocking=False) as (a,fd):
        raw=api._read(fd,api.MAX_JOURNAL_BYTES);size=item['prefix_size']
        if len(raw)<size or hashlib.sha256(raw[:size]).hexdigest()!=item['prefix_sha256']:
            raise ValueError('current_enrollment_original_journal_prefix_changed')
        out[name]={'policy_digest':a.policy_digest,'events':len(a.events),'journal_sha256':hashlib.sha256(raw).hexdigest(),
            'original_prefix_preserved':True}
print(json.dumps(out,sort_keys=True,separators=(',',':')))
'''


def identity(info):
    return (info.st_dev,info.st_ino,info.st_uid,info.st_gid,info.st_mode,info.st_nlink,
            info.st_size,info.st_mtime_ns,info.st_ctime_ns)


def gateway_destination(path):
    path=Path(path)
    return (path.name=='zzzzzz-eimemory-user-control-plane.conf'
            and re.fullmatch(r'(?:hermes-gateway(?:-[A-Za-z0-9_.-]+)?|openclaw-gateway)\.service\.d',path.parent.name) is not None)


def record(path, *, owners, maximum=32*1024*1024):
    """Capture exact bytes and ownership without following any named component."""
    path=Path(path)
    for parent in path.parents:
        if not parent.exists():continue
        info=parent.lstat()
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid not in owners|{0}
                or info.st_mode&0o022 and not (info.st_uid==0 and info.st_mode&stat.S_ISVTX)
                    and not (gateway_destination(path) and parent==path.parent and info.st_uid in owners)):
            raise ValueError('current_enrollment_parent_untrusted')
    if not os.path.lexists(path):return None
    fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_CLOEXEC|os.O_NONBLOCK)
    try:
        info=os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid not in owners or info.st_nlink!=1
                or info.st_mode&0o022 or info.st_size>maximum):
            raise ValueError('current_enrollment_file_untrusted')
        parts=[];remaining=maximum+1
        while remaining:
            part=os.read(fd,min(65536,remaining))
            if not part:break
            parts.append(part);remaining-=len(part)
        raw=b''.join(parts)
        if len(raw)!=info.st_size or identity(info)!=identity(os.fstat(fd)) or identity(info)!=identity(path.lstat()):
            raise ValueError('current_enrollment_file_changed')
        return {'raw_hex':raw.hex(),'sha256':sha256(raw).hexdigest(),'uid':info.st_uid,
                'gid':info.st_gid,'mode':stat.S_IMODE(info.st_mode)}
    finally:os.close(fd)


def contents(value):
    if value is None:return None
    raw=bytes.fromhex(value['raw_hex'])
    if sha256(raw).hexdigest()!=value['sha256']:
        raise ValueError('current_enrollment_archive_changed')
    return raw


def probe_environment(uid,serving,*,user_mode=None):
    """A probe never inherits operator authority or import/config selectors."""
    import pwd
    owner=pwd.getpwuid(uid)
    environment={'PATH':'/usr/bin:/bin','LC_ALL':'C','HOME':owner.pw_dir,'USER':owner.pw_name,'LOGNAME':owner.pw_name,
        'XDG_RUNTIME_DIR':'/run/user/'+str(uid),'DBUS_SESSION_BUS_ADDRESS':'unix:path=/run/user/'+str(uid)+'/bus',
        **{key:value for key,value in serving.items() if key in SOURCE_ENV and value is not None}}
    if user_mode is not None:
        if user_mode:environment['EIMEMORY_CONTROL_PLANE']='user'
        else:environment.pop('EIMEMORY_CONTROL_PLANE',None)
    return environment


def desired(raw, uid, gid, mode=0o640):
    return {'raw_hex':raw.hex(),'sha256':sha256(raw).hexdigest(),'uid':uid,'gid':gid,'mode':mode}


def process_environment(op, native, uid):
    """Use the actual serving process, not the operator's inherited environment."""
    state=native['eimemory-rpc.service'];pid=int(state['MainPID'])
    process=Path('/proc')/str(pid)
    def proof():
        status={line.partition(':')[0]:line.partition(':')[2].split()
                for line in (process/'status').read_text().splitlines()}
        groups=(process/'cgroup').read_text().splitlines()
        ticks=(process/'stat').read_text().rsplit(')',1)[1].split()[19]
        if (process.stat().st_uid!=uid or status.get('Uid')!=[str(uid)]*4
                or status.get('NSpid',[None])[0]!=str(pid)
                or '0::'+state['ControlGroup'] not in groups or not ticks.isdigit() or int(ticks)<=0):
            raise ValueError('current_enrollment_rpc_process_untrusted')
        return (ticks,tuple(status['NSpid']),tuple(groups))
    before=proof()
    raw=(process/'environ').read_bytes()
    if len(raw)>1024*1024:raise ValueError('current_enrollment_rpc_environment_limit')
    value={}
    for entry in raw.decode().split('\0'):
        if not entry:continue
        key,separator,item=entry.partition('=')
        if not separator or key in value:raise ValueError('current_enrollment_rpc_environment_invalid')
        value[key]=item
    if value.get('EIMEMORY_CONTROL_PLANE') not in {None,'','user'} or before!=proof() or raw!=(process/'environ').read_bytes():
        raise ValueError('current_enrollment_rpc_process_changed')
    selected={key:value.get(key) for key in SOURCE_ENV}
    if any(item is not None and any(character in item for character in '\r\n\0') for item in selected.values()):
        raise ValueError('current_enrollment_rpc_coordinate_invalid')
    if op.native_state('eimemory-rpc.service',NATIVE_PROPERTIES,{ 'PATH':'/usr/bin:/bin','LC_ALL':'C',
            'XDG_RUNTIME_DIR':'/run/user/'+str(uid),'DBUS_SESSION_BUS_ADDRESS':'unix:path=/run/user/'+str(uid)+'/bus'})!=state:
        raise ValueError('current_enrollment_rpc_process_changed')
    return selected


def native_properties(unit,*,loaded):
    """Request only the properties implemented by this native unit interface."""
    if not isinstance(unit,str) or not unit.endswith(('.service','.timer','.path')):
        raise ValueError('current_enrollment_native_unit_type_invalid')
    if not unit.endswith('.service'):
        return [name for name in NATIVE_PROPERTIES if name not in SERVICE_PROPERTIES]
    return [name for name in NATIVE_PROPERTIES if loaded or name!='ExecStart']


def validate_native_snapshot_unit(unit,value,uid):
    """Absent service commands and non-service PIDs are not native evidence."""
    if (not isinstance(value,dict) or value.get('LoadState') not in {'loaded','not-found'}
            or set(value)!=set(native_properties(unit,loaded=value['LoadState']=='loaded'))
            or any(not isinstance(item,str) for item in value.values()) or value.get('Id')!=unit
            or value.get('ActiveState') not in {'active','inactive','failed'}):
        raise ValueError('current_enrollment_native_snapshot_invalid')
    if value['LoadState']=='not-found' and value['ActiveState'] not in {'inactive','failed'}:
        raise ValueError('current_enrollment_native_snapshot_invalid')
    if not unit.endswith('.service'):return value
    if any(re.fullmatch(r'0|[1-9][0-9]*',value[name]) is None for name in ('MainPID','ControlPID')):
        raise ValueError('current_enrollment_native_service_pid_invalid')
    prefix='/user.slice/user-'+str(uid)+'.slice/user@'+str(uid)+'.service/'
    group=value['ControlGroup']
    if group and group not in {prefix+part+'/'+unit for part in ('app.slice','background.slice','session.slice')}:
        raise ValueError('current_enrollment_native_service_cgroup_invalid')
    running=value['MainPID']!='0' or value['ControlPID']!='0'
    if (running and (not group or value['ActiveState']!='active'
            or re.fullmatch('[0-9a-f]{32}',value['InvocationID']) is None)
            or value['LoadState']=='not-found' and (running or group)):
        raise ValueError('current_enrollment_native_service_process_invalid')
    if value['LoadState']=='loaded' and not value['ExecStart'].strip():
        raise ValueError('current_enrollment_native_service_command_missing')
    return value


def native_snapshot(op, environment, contract, topology, home):
    names=sorted(contract.REGISTERED_UNITS|set(topology['retired_gateways']))
    states={}
    for name in names:
        load=op.native_state(name,['LoadState'],environment)['LoadState']
        state=op.native_state(name,native_properties(name,loaded=load=='loaded'),environment)
        if state['LoadState']!=load:raise ValueError('current_enrollment_native_load_changed')
        states[name]=validate_native_snapshot_unit(name,state,topology['runtime_uid'])
    prefix='/user.slice/user-'+str(topology['runtime_uid'])+'.slice/user@'+str(topology['runtime_uid'])+'.service/'
    for name in ('eimemory-rpc.service',topology['owner_unit']):
        item=states[name]
        if (item['Id']!=name or item['LoadState']!='loaded' or item['ActiveState']!='active'
                or re.fullmatch('[1-9][0-9]*',item['MainPID']) is None
                or re.fullmatch('[0-9a-f]{32}',item['InvocationID']) is None
                or item['ControlGroup'] not in {prefix+part+'/'+name for part in
                                              ('app.slice','background.slice','session.slice')}):
            raise ValueError('current_enrollment_native_owner_unverified')
    for name in topology['retired_gateways']:
        item=states[name]
        absent=item['LoadState']=='not-found' and item['UnitFileState'] in {'','not-found','disabled'}
        disabled=item['LoadState']=='loaded' and item['UnitFileState']=='disabled'
        if (not (absent or disabled) or item['ActiveState'] not in {'inactive','failed'}
                or item['MainPID']!='0' or item['ControlPID']!='0' or item['ControlGroup']):
            raise ValueError('current_enrollment_retired_gateway_live')
    inventory=subprocess.run(['/usr/bin/systemctl','--user','list-units','--all','--plain',
        '--no-legend','--output=json'],env=environment,text=True,capture_output=True,timeout=30)
    if inventory.returncode or len(inventory.stdout.encode())>2*1024*1024:
        raise ValueError('current_enrollment_native_inventory_unreadable')
    rows=op.strict(inventory.stdout)
    if not isinstance(rows,list) or len(rows)>8192:raise ValueError('current_enrollment_native_inventory_invalid')
    relevant=[]
    for row in rows:
        if not isinstance(row,dict) or not isinstance(row.get('unit'),str):
            raise ValueError('current_enrollment_native_inventory_invalid')
        name=row['unit']
        if name.startswith(('eimemory-','hermes-gateway','openclaw-gateway')):
            relevant.append({key:row.get(key) for key in ('unit','load','active','sub')})
            if name not in set(names)|{'eimemory-runtime-manager.service'}:
                if row.get('active') not in {'inactive','failed'}:
                    raise ValueError('current_enrollment_unknown_native_work')
                if contract.is_dynamic_unit(name):
                    child=op.native_state(name,['MainPID','ControlPID','ControlGroup','ActiveState'],environment)
                    if child['MainPID']!='0' or child['ControlPID']!='0' or child['ControlGroup']:
                        raise ValueError('current_enrollment_unknown_native_work')
    system_inventory={}
    for operation in ('list-units','list-unit-files'):
        result=subprocess.run(['/usr/bin/systemctl',operation,'--all','--plain','--no-legend','--output=json'],
            env=environment,text=True,capture_output=True,timeout=30)
        if result.returncode not in {0,1} or len(result.stdout.encode())>2*1024*1024:
            raise ValueError('current_enrollment_system_inventory_unreadable')
        values=op.strict(result.stdout)
        if not isinstance(values,list) or len(values)>8192:raise ValueError('current_enrollment_system_inventory_invalid')
        selected=[]
        for row in values:
            name=row.get('unit' if operation=='list-units' else 'unit_file') if isinstance(row,dict) else None
            if not isinstance(name,str):raise ValueError('current_enrollment_system_inventory_invalid')
            if name.startswith(('eimemory-','hermes-gateway','openclaw-gateway')):
                if (operation=='list-units' and row.get('active') not in {'inactive','failed'}
                        or operation=='list-unit-files' and row.get('state') not in {'disabled','static','masked','masked-runtime'}):
                    raise ValueError('current_enrollment_system_work_present')
                selected.append(row)
        system_inventory[operation]=sorted(selected,key=lambda row:row.get('unit',row.get('unit_file')))
    scanned=0
    for root in (home/'.config/systemd/user',home/'.local/share/systemd/user',
                 Path('/run/user')/str(topology['runtime_uid'])/'systemd/user'):
        if not root.exists():continue
        for path in root.rglob('*'):
            scanned+=1
            if scanned>8192:raise ValueError('current_enrollment_native_inventory_limit')
            if (re.fullmatch('hermes-gateway-.+\\.service',path.name)
                    and path.name not in topology['retired_gateways']):
                raise ValueError('current_enrollment_unknown_gateway')
            if path.is_symlink() and any(part.endswith(('.wants','.requires','.upholds'))
                    for part in path.relative_to(root).parts[:-1]):
                if path.name in topology['retired_gateways'] or path.resolve(strict=False).name in topology['retired_gateways']:
                    raise ValueError('current_enrollment_retired_gateway_autostart')
    # Native system manager must not concurrently own this control plane.
    result=subprocess.run(['/usr/bin/systemctl','show','eimemory-runtime-manager.service',
        '--property=LoadState,ActiveState,MainPID,ControlPID,ControlGroup','--no-pager'],
        env=environment,text=True,capture_output=True,timeout=30)
    if result.returncode not in {0,4}:raise ValueError('current_enrollment_system_manager_unreadable')
    system=dict(line.split('=',1) for line in result.stdout.splitlines())
    if system!={'LoadState':'not-found','ActiveState':'inactive','MainPID':'0','ControlPID':'0','ControlGroup':''}:
        raise ValueError('current_enrollment_system_controller_present')
    op.stopped_controller(environment)
    if op.native_state('eimemory-runtime-manager.service',['LoadState'],environment)['LoadState']!='not-found':
        raise ValueError('current_enrollment_existing_native_controller')
    return {'schema':'eimemory.current_user_native_snapshot.v1','units':states,
            'runtime_uid':topology['runtime_uid'],'runtime_gid':topology['runtime_gid'],
            'authority_environment':process_environment(op,states,topology['runtime_uid']),
            'system_controller':system,'native_inventory':sorted(relevant,key=lambda row:row['unit']),
            'system_inventory':system_inventory}


def authority_snapshot(op, native, release, uid, gid, authority):
    env=native['authority_environment']
    source_user=env['EIMEMORY_CONTROL_PLANE']=='user'
    identity_roots=({'identity':str(authority/'identity'),'subject-projection':str(authority/'subject-projection')}
        if source_user else {'identity':env['EIMEMORY_IDENTITY_AUTHORITY_DIR'] or '/etc/eimemory-identity-governance',
                            'subject-projection':env['EIMEMORY_SUBJECT_PROJECTION_AUTHORITY_DIR'] or '/etc/eimemory-subject-projection'})
    source_uid=uid if source_user else 0
    source={};publication={};active_roots={}
    def add(path,destination,*,required=False):
        path=Path(path)
        if not path.is_absolute() or path.resolve(strict=False)!=path:
            raise ValueError('current_enrollment_authority_coordinate_invalid')
        entry=record(path,owners={source_uid})
        if entry is None:
            if required:raise ValueError('current_enrollment_authority_original_missing')
            return
        source[str(path)]=entry
        if source_user and path==Path(destination):
            if entry['gid']!=gid:raise ValueError('current_enrollment_source_group_mismatch')
            publication[str(destination)]=dict(entry)
        else:publication[str(destination)]=desired(contents(entry),uid,gid)
    for name,root in identity_roots.items():
        path=Path(root)
        if not os.path.lexists(path):continue
        active_roots[name]=str(path)
        add(path/'policy.json',authority/name/'policy.json',required=True)
        add(path/'requests.jsonl',authority/name/'requests.jsonl',required=True)
        known={'policy.json','requests.jsonl'}|({'retained-policies'} if source_user else set())
        if set(item.name for item in path.iterdir())-known:
            raise ValueError('current_enrollment_unknown_identity_material')
    probe_env=probe_environment(uid,env)
    result=subprocess.run([str(release/'.venv/bin/python'),'-I','-B','-c',SOURCE_PROBE,str(release)],
        input=op.canonical({'identity':active_roots,'uid':uid}),env=probe_env,
        capture_output=True,timeout=60)
    if result.returncode:raise ValueError('current_enrollment_original_authority_invalid')
    verified=op.strict(result.stdout)
    for name,item in verified['identity'].items():
        original=source[str(Path(active_roots[name])/'requests.jsonl')]
        if original['sha256']!=item['journal_sha256']:
            raise ValueError('current_enrollment_authority_changed')
        publication[str(authority/name/'policy.json')]=desired(op.canonical(item['current_policy'])+b'\n',uid,gid)
        if item['retained_digest'] is not None:
            publication[str(authority/name/'retained-policies'/(item['retained_digest']+'.json'))]=desired(
                op.canonical(item['original_policy'])+b'\n',uid,gid)
        retained=Path(active_roots[name])/'retained-policies'
        expected={digest+'.json' for digest in item['retained_policies']}
        if retained.exists():
            if {path.name for path in retained.iterdir()}!=expected:
                raise ValueError('current_enrollment_unknown_retained_policy')
            for filename in sorted(expected):add(retained/filename,authority/name/'retained-policies'/filename,required=True)
        elif expected:raise ValueError('current_enrollment_retained_policy_missing')
    standing=authority/'standing' if source_user else Path('/etc/eimemory-code-automation-authority')
    if standing.exists():
        for path in sorted(standing.iterdir()):
            if path.name.startswith('scope-') or path.name=='standing-grant.revoked':
                if path.name!='standing-grant.revoked' and re.fullmatch(r'scope-[0-9a-f]{64}\.(?:v1\.json|revoked)',path.name) is None:
                    raise ValueError('current_enrollment_unknown_standing_material')
                add(path,authority/'standing'/path.name,required=True)
    add((authority if source_user else standing)/'code-provider-delegations.v1.json',authority/'code-provider-delegations.v1.json')
    cpa=env['EIMEMORY_CONTROLLED_PRODUCTION_ACCEPTANCE_POLICY'] or (
        str(authority/'controlled-production-acceptance/policy.json') if source_user else '/etc/eimemory-controlled-production-acceptance/policy.json')
    add(cpa,authority/'controlled-production-acceptance/policy.json')
    snapshot={'schema':'eimemory.current_user_authority_snapshot.v1','sources':source,
              'source_control_plane':'user' if source_user else 'root','source_uid':source_uid,
              'identity_validation':verified,'identity_roots':active_roots,'publication':publication}
    if len(op.canonical(snapshot))>64*1024*1024:
        raise ValueError('current_enrollment_authority_snapshot_too_large')
    # Preserve every original byte, journal hash and original requester UID. New
    # ownership changes only the serving coordinate and future operator policy.
    return snapshot,publication


def registry(op, contract, files, manifest, account, native, scopes, repository):
    entry=op.strict(files['controller-entry.json']);commit=manifest['controller_commit']
    value={'schema':contract.REGISTRY_SCHEMA,'generation':'0'*64,'runtime_user':account.pw_name,
        'runtime_uid':account.pw_uid,'runtime_gid':account.pw_gid,'controller_commit':commit,
        'runtime_commit':commit,'repository_root':repository,
        'units':{name:{'manager':'user','installed':state['LoadState']=='loaded'}
                 for name,state in native['units'].items()},
        'template_hashes':{name.removeprefix('templates/'):sha256(raw).hexdigest()
                           for name,raw in files.items() if name.startswith('templates/')},
        'compatibility_hashes':op.strict(files['compatibility.json']),'unit_scopes':scopes,
        'managed_incident_authority':{'enabled':False},'controller_entry_digest':entry['sha256'],
        'validation_entry_digest':entry['validation_sha256']}
    value['generation']=contract.digest({key:item for key,item in value.items() if key!='generation'})
    return contract.validate_registry(value)


def publication(op,files,value,settings,home,authority_files,source_environment=None,*,enrollment_id=None):
    """Render existing reviewed templates without executing native unit code."""
    uid=value['runtime_uid'];gid=value['runtime_gid'];commit=value['runtime_commit']
    authority=home/'.config/eimemory/control-plane';code=home/'.local/lib/eimemory/control-plane'
    units=home/'.config/systemd/user';result=dict(authority_files)
    def put(path,raw,mode=0o640):result[str(path)]=desired(raw,uid,gid,mode)
    for name,raw in files.items():
        if name!='eimemory-runtime-manager.service':put(code/name,raw)
    put(code/'runtime-settings.v1.json',settings)
    put(authority/'runtime-manager.v1.json',op.canonical(value))
    put(authority/'installation.v1.json',op.canonical({'schema':'eimemory.user_control_plane_installation.v1',
        'runtime_uid':uid,'runtime_gid':gid,'control_plane':'user'}))
    if enrollment_id is not None:
        put(authority/'current-user-enrollment.v1.json',op.canonical({'schema':'eimemory.current_user_enrollment_selector.v1',
            'enrollment_id':enrollment_id,'runtime_uid':uid,'runtime_gid':gid,'current_commit':commit}))
    runtime_env=('EIMEMORY_CONTROL_PLANE=user\nEIMEMORY_IDENTITY_AUTHORITY_DIR='+str(authority/'identity')+
        '\nEIMEMORY_SUBJECT_PROJECTION_AUTHORITY_DIR='+str(authority/'subject-projection')+
        '\nEIMEMORY_CONTROLLED_PRODUCTION_ACCEPTANCE_POLICY='+str(authority/'controlled-production-acceptance/policy.json')+'\n').encode()
    # Resolve trust using the actual serving process, then carry that same
    # anchor through the namespace switch. An operator's shell is not evidence.
    runtime_env+=('EIMEMORY_TRUSTED_REPOSITORY_ROOT='+value['repository_root']+'\n').encode()
    for name in SOURCE_ENV[4:]:
        item=(source_environment or {}).get(name)
        if item is not None and name!='EIMEMORY_TRUSTED_REPOSITORY_ROOT':
            quoted='"'+item.replace('\\','\\\\').replace('"','\\"')+'"'
            runtime_env+=(name+'='+quoted+'\n').encode()
    put(authority/'user-runtime.env',runtime_env)
    # Reuse the historical installer's exact renderer through an injected writer.
    op.install_user_units(files,units,uid,controller_commit=commit,runtime_commit=commit,
                          writer=lambda path,raw,owner,mode=0o640:put(path,raw,mode))
    policy=op.execute_reviewed_module('current_user_policy',code/'runtime_manager_user_policy.py',files['runtime_manager_user_policy.py'])
    raw=files['runtime-policy.conf'].replace(b'@COMMIT@',commit.encode())
    raw=raw.replace(('User='+value['runtime_user']+'\n').encode(),b'').replace(('Group='+str(gid)+'\n').encode(),b'')
    raw=raw.replace(b'PrivateUsers=self',b'PrivateUsers=yes').replace(('/opt/eimemory/releases/'+commit+' ').encode(),b'/opt/eimemory/releases ')
    for unit,item in value['units'].items():
        if not item['installed'] or not unit.endswith('.service'):continue
        if op.is_gateway_unit(unit):
            content=policy.render_gateway_integration(unit,commit,authority=authority)
        else:
            content=raw;template=files.get('templates/'+unit)
            if template is not None:
                reset='\n[Service]\n'+''.join(name+'=\n' for name in ('ExecStart','ExecStartPre','ExecStartPost','ExecStop','ExecStopPost','ExecCondition'))
                commands='\n'.join(line for line in template.decode().replace('\\\n',' ').splitlines()
                    if line.startswith(('ExecStart=','ExecStartPre=','ExecStartPost=','ExecStop=','ExecStopPost=','ExecCondition=')))
                content=reset.encode()+commands.encode()+b'\n'+raw
            content+=('EnvironmentFile='+str(authority/'user-runtime.env')+'\n').encode()
        put(units/(unit+'.d')/'zzzzzz-eimemory-user-control-plane.conf',content,0o600)
    barrier=home/'.local/state/eimemory/control-plane/controller-installation.json'
    guard=('[Unit]\nConditionPathExists=!'+str(barrier)+'\n').encode()
    for unit in set(value['template_hashes'])|{'eimemory-runtime-manager.service'}:
        if not op.is_gateway_unit(unit):put(units/(unit+'.d')/'05-eimemory-installation-guard.conf',guard,0o644)
    return result


def validate_empty_coordinates(op,home,authority_files,uid,authority_sources=None,*,contract=None,epoch=None):
    authority=home/'.config/eimemory/control-plane';state=home/'.local/state/eimemory/control-plane'
    code=home/'.local/lib/eimemory/control-plane'
    for path in (Path('/var/lib/eimemory-manager'),
                 Path('/etc/eimemory-code-automation-authority/runtime-manager.v1.json'),
                 Path('/etc/eimemory-code-automation-authority/managed-incident-proof.key'),
                 Path('/etc/eimemory-code-automation-authority/managed-code-incident-manifest.v1.json'),
                 home/'.config/systemd/user/eimemory-runtime-manager.service',
                 home/'.config/systemd/user/eimemory-runtime-manager.service.d',
                 authority/'runtime-manager.v1.json',authority/'installation.v1.json'):
        if os.path.lexists(path):raise ValueError('current_enrollment_existing_control_material')
    allowed={str(authority/name) for name in ('hermes-topology.v1.json','hermes-topology-intent.v1.json')}|set(authority_files)
    if authority.exists():
        for path in authority.rglob('*'):
            if path.is_dir() and not path.is_symlink():continue
            if str(path) not in allowed:raise ValueError('current_enrollment_unknown_existing_material')
            value=record(path,owners={uid})
            known=(authority_sources or {}).get(str(path),authority_files.get(str(path)))
            if str(path) in authority_files and contents(value)!=contents(known):
                raise ValueError('current_enrollment_existing_authority_conflict')
    for root in (state,code):
        if not root.exists():continue
        for path in root.rglob('*'):
            relative=path.relative_to(root)
            if (root==state and len(relative.parts)>=2 and relative.parts[0]=='current-enrollments'
                    and re.fullmatch('[0-9a-f]{32}',relative.parts[1])):
                if contract is None or epoch is None:raise ValueError('current_enrollment_existing_control_material')
                previous=root/'current-enrollments'/relative.parts[1]
                plan=op.strict(op.owned_bytes(previous/'plan.json',uid))
                epoch.validate_plan(plan,uid=uid,gid=os.getegid())
                _put,journal,_native=recovery_material(op,previous,plan,home,uid,os.getegid(),contract,epoch,terminal=True)
                if journal['phase']!='rolled_back':raise ValueError('current_enrollment_existing_control_material')
                validate_rollback_seal(op,previous,plan,uid,epoch)
                continue
            if (root==state and len(relative.parts)>=2 and relative.parts[0]=='current-enrollments'
                    and re.fullmatch(r'\.abandoned-[0-9a-f]{32}-[0-9]+',relative.parts[1])):
                abandoned=root/'current-enrollments'/relative.parts[1]
                validate_abandoned_stage(op,abandoned,uid)
                continue
            # Empty lock files are bookkeeping, not old custody. Any archive is
            # a consumed nonce and must be recovered explicitly, never erased.
            if root==state and relative.as_posix() in {'bridge.lock','lifecycle.lock'}:
                if contents(record(path,owners={uid}))==b'':continue
            if not path.is_dir() or path.is_symlink():
                raise ValueError('current_enrollment_existing_control_material')


def prepare(args,op,files,manifest,account,helper,contract,topology_contract,epoch_contract):
    home=Path(account.pw_dir);authority=home/'.config/eimemory/control-plane'
    if args.enrollment_id is None or re.fullmatch('[0-9a-f]{32}',args.enrollment_id) is None:
        raise ValueError('current_enrollment_nonce_invalid')
    release=Path('/opt/eimemory/releases')/manifest['controller_commit']
    if Path('/opt/eimemory/current').resolve(strict=True)!=release:
        raise ValueError('current_enrollment_current_release_mismatch')
    # Verify every operator/controller source in addition to compatibility pins.
    sources={'current_user_enrollment_contract.py':'eimemory/contracts/current_user_enrollment.py',
             'hermes_topology_contract.py':'eimemory/contracts/hermes_topology.py',
             'runtime_manager_current_user.py':'deploy/runtime_manager_current_user.py',
             'runtime_manager_bridge.py':'deploy/runtime_manager_bridge.py'}
    for name,path in sources.items():op.reviewed(release/path,sha256(files[name]).hexdigest(),4*1024*1024)
    for path,digest in op.strict(files['compatibility.json']).items():op.reviewed(release/path,digest,4*1024*1024)
    raw=op.reviewed(args.logical_topology,args.expected_logical_topology_sha256,1024*1024)
    logical=topology_contract.validate_topology(op.strict(raw))
    if (raw!=topology_contract.canonical_bytes(logical) or logical['runtime_uid']!=account.pw_uid
            or logical['runtime_gid']!=account.pw_gid or len(logical['profiles'])!=3
            or any(item.get('operator_scope',{}).get('user_id',account.pw_name)!=account.pw_name
                   for item in logical['profiles'])):
        raise ValueError('current_enrollment_topology_identity_invalid')
    if op.owned_bytes(authority/'hermes-topology.v1.json',account.pw_uid)!=raw:
        raise ValueError('current_enrollment_protected_topology_changed')
    prior_intent=op.owned_bytes(authority/'hermes-topology-intent.v1.json',account.pw_uid)
    original=op.strict(prior_intent)
    if original.get('schema')!='eimemory.hermes_topology_intent.v1':
        raise ValueError('current_enrollment_previous_current_epoch_preserved')
    topology_contract.validate_operator_topology_intent(original,main=logical['owner_unit'],
        retired=logical['retired_gateways'],uid=account.pw_uid,gid=account.pw_gid)
    if original['topology_sha256']!=sha256(raw).hexdigest():raise ValueError('current_enrollment_protected_intent_changed')
    environment={'PATH':'/usr/bin:/bin','LC_ALL':'C','XDG_RUNTIME_DIR':'/run/user/'+str(account.pw_uid),
        'DBUS_SESSION_BUS_ADDRESS':'unix:path=/run/user/'+str(account.pw_uid)+'/bus'}
    native=native_snapshot(op,environment,contract,logical,home)
    native['protected_intent_sha256']=sha256(prior_intent).hexdigest()
    authorities,authority_files=authority_snapshot(op,native,release,account.pw_uid,account.pw_gid,authority)
    validate_empty_coordinates(op,home,authority_files,account.pw_uid,authorities['sources'],contract=contract,epoch=epoch_contract)
    repository=authorities['identity_validation']['repository_root']
    if repository!=args.repository_root or str(Path(repository).resolve(strict=True))!=repository:
        raise ValueError('current_enrollment_repository_mismatch')
    scopes_raw=op.reviewed(args.unit_scopes,args.expected_unit_scopes_sha256,65536)
    scopes=op.strict(scopes_raw)
    settings=op.reviewed(args.runtime_settings,args.expected_runtime_settings_sha256,65536)
    settings_module=op.execute_reviewed_module('current_settings',release/'deploy/runtime_manager_settings.py',files['runtime_manager_settings.py'])
    settings_module.validate(op.strict(settings))
    value=registry(op,contract,files,manifest,account,native,scopes,repository)
    if set(scopes)!={name for name,item in value['units'].items()
                       if item['installed'] and name in contract.PRODUCT_SERVICES}:
        raise ValueError('current_enrollment_installed_scopes_required')
    if any(scope['user_id']!=account.pw_name for scope in scopes.values()):
        raise ValueError('current_enrollment_scope_user_mismatch')
    planned=publication(op,files,value,settings,home,authority_files,native['authority_environment'],enrollment_id=args.enrollment_id)
    before={name:record(name,owners={account.pw_uid}) for name in planned}
    before[str(authority/'hermes-topology-intent.v1.json')]=record(authority/'hermes-topology-intent.v1.json',owners={account.pw_uid})
    before[str(home/'.local/state/eimemory/control-plane/activation-signing.key')]=None
    native['publication_before_sha256']=contract.digest(before)
    if max(len(op.canonical(planned)),len(op.canonical(before)))>64*1024*1024:
        raise ValueError('current_enrollment_publication_snapshot_too_large')
    intent={'schema':epoch_contract.INTENT_SCHEMA,'enrollment_id':args.enrollment_id,
        'runtime_uid':account.pw_uid,'runtime_gid':account.pw_gid,'current_commit':manifest['controller_commit'],
        'bundle_sha256':args.expected_bundle_sha256,'registry_sha256':contract.digest(value),
        'runtime_settings_sha256':sha256(settings).hexdigest(),'unit_scopes_sha256':sha256(scopes_raw).hexdigest(),
        'native_snapshot_sha256':contract.digest(native),'authority_snapshot_sha256':contract.digest(authorities),
        'topology_sha256':sha256(raw).hexdigest(),'authorization':'explicit_user_operator',
        'historical_custody_recovered':False,'autonomous_repair_credit':False}
    epoch_contract.validate_intent(intent,uid=account.pw_uid,gid=account.pw_gid)
    return {'intent':intent,'registry':value,'native':native,'authority':authorities,'topology_raw':raw,
        'prior_intent_raw':prior_intent,'environment':environment,'release':release,
        'publication':planned,'identity_roots':
        {name:str(authority/name) for name in authorities['identity_roots']}}


class Publication:
    """Compare-and-swap transaction; an interrupted write is rollbackable."""
    def __init__(self,op,archive,uid,gid,publication,*,journal=None,gateway_writer=None):
        self.op=op;self.archive=archive;self.uid=uid;self.gid=gid;self.publication=publication
        self.gateway_writer=gateway_writer
        self.journal=journal or {'schema':'eimemory.current_user_publication_journal.v1','phase':'prepared',
            'before':{path:record(path,owners={uid}) for path in publication},'attempted':[],'created_directories':[]}

    def save(self):self.op.write(self.archive/'journal.json',self.op.canonical(self.journal),self.uid,0o600)

    def parent(self,path):
        missing=[];parent=path.parent
        while not parent.exists():missing.append(parent);parent=parent.parent
        # The intent is durable before a directory creation, including crashes.
        for target in reversed(missing):
            if str(target) not in self.journal['created_directories']:
                self.journal['created_directories'].append(str(target));self.save()
            target.mkdir(mode=0o750)
            self.op.sync_parent(target)

    def apply(self,path):
        path=str(path);before=self.journal['before'][path];after=self.publication[path]
        if record(path,owners={self.uid})!=before:raise ValueError('current_enrollment_publication_cas_failed')
        # A selected user journal is also our locked source inode. Replacing
        # identical bytes would detach queued legitimate writers from the
        # named append-only journal and silently lose their future events.
        if before==after:return
        if path not in self.journal['attempted']:self.journal['attempted'].append(path)
        self.journal['phase']='publishing';self.save()
        self.parent(Path(path))
        self.write(path,after)
        if record(path,owners={self.uid})!=after:raise ValueError('current_enrollment_publication_readback_failed')

    def write(self,path,item):
        if gateway_destination(path):
            if self.gateway_writer is None:raise ValueError('current_enrollment_gateway_writer_required')
            target=Path(path)
            existing=(contents(record(target,owners={self.uid})),target.lstat()) if os.path.lexists(target) else None
            self.gateway_writer(target,contents(item),self.uid,item['mode'],expected_existing=existing)
        else:self.op.write(path,contents(item),self.uid,item['mode'])

    def directory(self,path,mode=0o750):
        path=Path(path);self.parent(path)
        if not path.exists():
            self.journal['created_directories'].append(str(path));self.save()
            path.mkdir(mode=mode);path.chmod(mode);self.op.sync_parent(path)
        info=path.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid!=self.uid or stat.S_IMODE(info.st_mode)!=mode:
            raise ValueError('current_enrollment_directory_changed')

    def verify(self):
        if any(record(path,owners={self.uid})!=item for path,item in self.publication.items()):
            raise ValueError('current_enrollment_publication_changed')

    def rollback(self):
        self.journal['phase']='rolling_back';self.save()
        for path in reversed(self.journal['attempted']):
            before=self.journal['before'][path];after=self.publication[path];now=record(path,owners={self.uid})
            if now==before:continue
            if now!=after:raise ValueError('current_enrollment_rollback_foreign_change_preserved')
            if before is None:Path(path).unlink();self.op.sync_parent(path)
            else:self.write(path,before)
        for name in reversed(self.journal['created_directories']):
            path=Path(name)
            if path.exists():
                info=path.lstat()
                if not stat.S_ISDIR(info.st_mode) or info.st_uid!=self.uid:raise ValueError('current_enrollment_rollback_directory_changed')
                try:path.rmdir();self.op.sync_parent(path)
                except OSError:
                    if any(path.iterdir()):raise ValueError('current_enrollment_rollback_directory_not_empty') from None
                    raise
        self.journal['phase']='rolled_back';self.save()


def systemctl(environment,*arguments):
    result=subprocess.run(['/usr/bin/systemctl','--user',*arguments],env=environment,
                          capture_output=True,text=True,timeout=90)
    if result.returncode:raise ValueError('current_enrollment_native_operation_failed')


def barrier_bytes(op,plan):
    return op.canonical({'schema':'eimemory.current_user_enrollment_barrier.v1',
        'enrollment_id':plan['enrollment_id'],'plan_sha256':sha256(op.canonical(plan)).hexdigest()})


def verify_barrier(op,path,expected,uid,*,missing_ok=False):
    actual=record(path,owners={uid})
    if actual is None:
        if missing_ok:return
        raise ValueError('current_enrollment_barrier_missing')
    if contents(actual)!=expected or actual['mode']!=0o600:
        raise ValueError('current_enrollment_foreign_barrier_preserved')


def ensure_barrier(op,path,expected,uid):
    if os.path.lexists(path):verify_barrier(op,path,expected,uid);return
    descriptor=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW|os.O_CLOEXEC,0o600)
    try:
        os.fchmod(descriptor,0o600)
        with os.fdopen(descriptor,'wb',closefd=False) as stream:
            stream.write(expected);stream.flush();os.fsync(descriptor)
    finally:os.close(descriptor)
    op.sync_parent(path);verify_barrier(op,path,expected,uid)


def remove_barrier(op,path,expected,uid,*,missing_ok=False):
    verify_barrier(op,path,expected,uid,missing_ok=missing_ok)
    if os.path.lexists(path):path.unlink();op.sync_parent(path)


def quiesce(op,environment,native,units):
    names=[name for name in units if native['units'][name]['LoadState']=='loaded']
    # Stop triggers before their services; no unit is enabled/disabled by enroll.
    for suffix in (('.timer','.path'),('.service',)):
        batch=[name for name in names if name.endswith(suffix)]
        if batch:systemctl(environment,'stop',*batch)
    for name in names:
        properties=(['ActiveState','MainPID','ControlPID','ControlGroup','UnitFileState'] if name.endswith('.service')
                    else ['ActiveState','UnitFileState'])
        item=op.native_state(name,properties,environment)
        if (item['ActiveState'] not in {'inactive','failed'} or name.endswith('.service')
                and (item['MainPID']!='0' or item['ControlPID']!='0' or item['ControlGroup'])):
            raise ValueError('current_enrollment_native_writer_still_present')
        if item['UnitFileState']!=native['units'][name]['UnitFileState']:
            raise ValueError('current_enrollment_native_enablement_changed')


def restore(op,environment,native,units):
    for suffix in (('.service',),('.timer','.path')):
        batch=[name for name in units if name.endswith(suffix) and native['units'][name]['ActiveState']=='active'
               and op.native_state(name,['ActiveState'],environment)['ActiveState']!='active']
        if batch:systemctl(environment,'start',*batch)
    for name in units:
        item=op.native_state(name,['ActiveState','UnitFileState'],environment);original=native['units'][name]
        if item['UnitFileState']!=original['UnitFileState']:
            raise ValueError('current_enrollment_enabled_state_changed')
        if original['ActiveState']=='active' and item['ActiveState']!='active':
            raise ValueError('current_enrollment_native_resume_failed')


def verify_controller(op,environment,value,socket_path,code):
    """Verify the actual native unit, kernel peer and bounded Bridge response."""
    state=op.native_state('eimemory-runtime-manager.service',
        ['Id','LoadState','ActiveState','MainPID','ControlPID','ControlGroup','InvocationID','ExecStart',
         'NoNewPrivileges','PrivateUsers','FragmentPath'],environment)
    uid=value['runtime_uid'];prefix='/user.slice/user-'+str(uid)+'.slice/user@'+str(uid)+'.service/'
    expected_argv=['/usr/bin/python3','-I','-B','-X','pycache_prefix='+str(code/'empty-pycache'),str(code/'runtime_manager_bridge.py')]
    commands=re.findall(r'\{ path=([^;]+) ; argv\[\]=(.*?) ; ignore_errors=(?:yes|no) ;',state['ExecStart'])
    native_argv=(shlex.split(commands[0][1]) if len(commands)==1 else [])
    if (state['Id']!='eimemory-runtime-manager.service' or state['LoadState']!='loaded'
            or state['ActiveState']!='active' or re.fullmatch('[1-9][0-9]*',state['MainPID']) is None
            or state['ControlPID']!='0' or re.fullmatch('[0-9a-f]{32}',state['InvocationID']) is None
            or state['NoNewPrivileges']!='yes' or state['PrivateUsers'] not in {'no','false'}
            or state['ControlGroup'] not in {prefix+part+'/eimemory-runtime-manager.service'
                                            for part in ('app.slice','background.slice','session.slice')}
            or len(commands)!=1 or commands[0][0].strip()!='/usr/bin/python3' or native_argv!=expected_argv
            or state['FragmentPath']!=str(code.parents[3]/'.config/systemd/user/eimemory-runtime-manager.service')):
        raise ValueError('current_enrollment_controller_native_unverified')
    process=Path('/proc')/state['MainPID'];status={line.partition(':')[0]:line.partition(':')[2].split()
                                                  for line in (process/'status').read_text().splitlines()}
    if (status.get('Uid')!=[str(uid)]*4 or status.get('Gid')!=[str(value['runtime_gid'])]*4
            or status.get('NoNewPrivs')!=['1']
            or any(status.get(key)!=['0000000000000000'] for key in ('CapEff','CapPrm','CapAmb'))
            or '0::'+state['ControlGroup'] not in (process/'cgroup').read_text().splitlines()
            or (process/'uid_map').read_text().split()!=['0','0',str(2**32-1)]):
        raise ValueError('current_enrollment_controller_process_unverified')
    if (process/'cmdline').read_bytes()!=b'\0'.join(item.encode() for item in expected_argv)+b'\0':
        raise ValueError('current_enrollment_controller_argv_unverified')
    parent=socket_path.parent.lstat();info=socket_path.lstat()
    if (not stat.S_ISDIR(parent.st_mode) or parent.st_uid!=uid or stat.S_IMODE(parent.st_mode)!=0o700
            or not stat.S_ISSOCK(info.st_mode) or info.st_uid!=uid or info.st_gid!=value['runtime_gid']
            or stat.S_IMODE(info.st_mode)!=0o660 or info.st_nlink!=1):
        raise ValueError('current_enrollment_controller_socket_untrusted')
    request={'schema':'eimemory.runtime_manager_request.v1','operation':'unit_state',
             'registry_generation':value['generation'],'unit':'eimemory-rpc.service','properties':['LoadState']}
    with socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) as stream:
        stream.settimeout(15);stream.connect(str(socket_path))
        pid,peer_uid,_gid=struct.unpack('3i',stream.getsockopt(socket.SOL_SOCKET,socket.SO_PEERCRED,12))
        if pid!=int(state['MainPID']) or peer_uid!=uid:raise ValueError('current_enrollment_controller_peer_invalid')
        raw=op.canonical(request);stream.sendall(struct.pack('>I',len(raw))+raw)
        def receive(size):
            raw=b''
            while len(raw)<size:
                part=stream.recv(size-len(raw))
                if not part:raise ValueError('current_enrollment_controller_response_truncated')
                raw+=part
            return raw
        size=struct.unpack('>I',receive(4))[0]
        if not 0<size<=65536:raise ValueError('current_enrollment_controller_response_limit')
        response=op.strict(receive(size))
    if (response.get('schema')!='eimemory.runtime_manager_response.v1' or response.get('ok') is not True
            or response.get('operation')!='unit_state' or response.get('registry_generation')!=value['generation']
            or response.get('state')!={'LoadState':'loaded'}):
        raise ValueError('current_enrollment_controller_response_invalid')
    if op.native_state('eimemory-runtime-manager.service',list(state),environment)!=state:
        raise ValueError('current_enrollment_controller_changed_during_verification')
    return {'schema':'eimemory.current_user_controller_verification.v1','native':state,
            'peer_pid':pid,'peer_uid':peer_uid,'registry_generation':value['generation']}


def verify_authority(op,data):
    result=subprocess.run([str(data['release']/'.venv/bin/python'),'-I','-B','-c',USER_PROBE,str(data['release'])],
        input=op.canonical(data['identity_roots']),env=probe_environment(data['registry']['runtime_uid'],
            data['native']['authority_environment'],user_mode=True),
        capture_output=True,timeout=60)
    if result.returncode:raise ValueError('current_enrollment_migrated_authority_invalid')
    current=op.strict(result.stdout)
    for name,original in data['authority']['identity_validation']['identity'].items():
        if (current.get(name,{}).get('journal_sha256')!=original['journal_sha256']
                or current[name].get('events')!=original['events']
                or current[name].get('policy_digest')!=sha256(op.canonical(original['current_policy'])).hexdigest()):
            raise ValueError('current_enrollment_original_identity_history_changed')


def run(args,op,files,manifest):
    import pwd
    if os.name!='posix' or os.getuid()!=os.geteuid() or os.geteuid()<=0:
        raise ValueError('current_enrollment_native_user_required')
    account=pwd.getpwuid(os.geteuid());uid=account.pw_uid;gid=account.pw_gid;home=Path(account.pw_dir)
    if gid!=os.getgid() or gid!=os.getegid():raise ValueError('current_enrollment_primary_group_required')
    # Only compile authenticated bundle members, with an adjacent helper for the
    # standalone registry contract. No caller-selected Python import directory.
    with tempfile.TemporaryDirectory(prefix='.eimemory-current-user-review-',dir=home) as staging:
        stage=Path(staging)
        for name in ('user_control_plane.py','runtime_manager_contract.py','hermes_topology_contract.py',
                     'current_user_enrollment_contract.py'):
            op.write(stage/name,files[name],uid)
        with op.user_operator_profile():
            helper=op.execute_reviewed_module('fresh_helper',stage/'user_control_plane.py',files['user_control_plane.py'])
            contract=op.execute_reviewed_module('fresh_registry',stage/'runtime_manager_contract.py',files['runtime_manager_contract.py'])
        topology=op.execute_reviewed_module('fresh_topology',stage/'hermes_topology_contract.py',files['hermes_topology_contract.py'])
        epoch=op.execute_reviewed_module('fresh_epoch',stage/'current_user_enrollment_contract.py',files['current_user_enrollment_contract.py'])
        if args.operation=='recover-current-user':return recover(args,op,files,manifest,account,helper,contract,epoch)
        if args.operation=='finish-current-user':return finish_pending(args,op,files,manifest,account,helper,contract,epoch)
        if args.operation=='initialize-current-user' and re.fullmatch('[0-9a-f]{32}',args.enrollment_id or ''):
            previous=home/'.local/state/eimemory/control-plane/current-enrollments'/args.enrollment_id
            if os.path.lexists(previous):
                return retry_completed(args,op,files,manifest,account,helper,contract,epoch,previous)
            if any(previous.parent.glob('.preparing-'+args.enrollment_id+'-*')):
                raise ValueError('current_enrollment_unpublished_stage_preserved')
            if any(previous.parent.glob('.abandoned-'+args.enrollment_id+'-*')):
                raise ValueError('current_enrollment_nonce_consumed')
        data=prepare(args,op,files,manifest,account,helper,contract,topology,epoch)
        if args.operation=='inspect-current-user':
            # Operator review material only; creates no control directory/key.
            op.archive_write(args.output,op.canonical(data['intent']),uid)
            report={'schema':'eimemory.current_user_enrollment_review.v1','operator_intent':data['intent'],
                'registry':data['registry'],'source_control_plane':data['authority']['source_control_plane'],
                'authority_sources':{name:{key:item[key] for key in ('sha256','uid','gid','mode')}
                    for name,item in data['authority']['sources'].items()},
                'publication':{name:{key:item[key] for key in ('sha256','uid','gid','mode')}
                    for name,item in data['publication'].items()},'native_snapshot':data['native'],
                'release_accepted':False,'historical_custody_recovered':False,'autonomous_repair_credit':False}
            op.archive_write(args.output.with_name(args.output.name+'.review.json'),op.canonical(report),uid)
            return {'ready_for_review':True,'operator_intent_sha256':contract.digest(data['intent']),
                    'historical_custody_recovered':False,'release_accepted':False}
        expected=op.reviewed(args.operator_intent,args.expected_operator_intent_sha256,65536)
        if expected!=op.canonical(data['intent']):raise ValueError('current_enrollment_operator_review_stale')
        state=home/'.local/state/eimemory/control-plane';authority=home/'.config/eimemory/control-plane'
        code=home/'.local/lib/eimemory/control-plane';environment=data['environment']
        # No production key is created until complete preflight and both locks.
        op.directory(state,uid)
        with op.operator_lock(helper,state/'lifecycle.lock'),ExitStack() as bridge_lock,ExitStack() as sources:
            bridge_lock.enter_context(op.operator_lock(helper,state/'bridge.lock'))
            archive=state/'current-enrollments'/args.enrollment_id
            if os.path.lexists(archive):raise ValueError('current_enrollment_nonce_consumed')
            for path in data['authority']['sources']:
                if Path(path).name=='requests.jsonl':
                    import fcntl
                    fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_CLOEXEC)
                    sources.callback(os.close,fd);fcntl.flock(fd,fcntl.LOCK_SH|fcntl.LOCK_NB)
            fresh=prepare(args,op,files,manifest,account,helper,contract,topology,epoch)
            if fresh['intent']!=data['intent']:raise ValueError('current_enrollment_preflight_changed')
            final_archive=archive
            op.directory(archive.parent,uid,0o700)
            archive=archive.parent/('.preparing-'+args.enrollment_id+'-'+str(os.getpid()))
            archive.mkdir(mode=0o700);archive.chmod(0o700);op.sync_parent(archive)
            key=os.urandom(32)
            put=data['publication'];put[str(state/'activation-signing.key')]=desired(key,uid,gid,0o600)
            policy=op.execute_reviewed_module('fresh_gateway_writer',code/'runtime_manager_user_policy.py',files['runtime_manager_user_policy.py'])
            transaction=Publication(op,archive,uid,gid,put,gateway_writer=policy.write_user_metadata)
            before=transaction.journal['before']
            plan={'schema':epoch.PLAN_SCHEMA,**{name:data['intent'][name] for name in
                ('enrollment_id','runtime_uid','runtime_gid','current_commit','bundle_sha256','registry_sha256',
                 'native_snapshot_sha256','authority_snapshot_sha256','topology_sha256','authorization',
                 'historical_custody_recovered','autonomous_repair_credit')},
                'operator_intent_sha256':contract.digest(data['intent']),'activation_key_sha256':sha256(key).hexdigest(),
                'before_inventory_sha256':contract.digest(before),'publication_sha256':'0'*64}
            # The topology intent binds the plan's immutable material; avoid a
            # circular plan/publication hash by publishing its exact bytes as a
            # separately signed receipt binding after the plan is fixed.
            plan['publication_sha256']=contract.digest(put)
            topology_intent={'schema':epoch.TOPOLOGY_SCHEMA,'intent_id':args.enrollment_id,
                'runtime_uid':uid,'runtime_gid':gid,'mode':'single_native_gateway','main_gateway':topology.MAIN_GATEWAY,
                'retired_gateways':topology.validate_topology(op.strict(data['topology_raw']))['retired_gateways'],
                'source':{'enrollment_id':args.enrollment_id,'plan_sha256':contract.digest(plan),
                    'registry_sha256':plan['registry_sha256'],'base_commit':plan['current_commit']},
                'topology_sha256':plan['topology_sha256'],'native_snapshot_sha256':plan['native_snapshot_sha256'],
                'authorization':'explicit_user_operator','autonomous_repair_credit':False}
            topology_raw=op.canonical(topology_intent)
            # This separate, fixed CAS target is covered by signed completion;
            # do not recompute the publication digest and create a cycle.
            intent_path=str(authority/'hermes-topology-intent.v1.json')
            put[intent_path]=desired(topology_raw,uid,gid)
            before[intent_path]=record(intent_path,owners={uid})
            plan['before_inventory_sha256']=contract.digest(before)
            if plan['before_inventory_sha256']!=data['native']['publication_before_sha256']:
                raise ValueError('current_enrollment_reviewed_destinations_changed')
            # before inventory includes topology target from the outset.
            topology_intent['source']['plan_sha256']=contract.digest(plan);topology_raw=op.canonical(topology_intent)
            put[intent_path]=desired(topology_raw,uid,gid)
            epoch.validate_plan(plan,uid=uid,gid=gid)
            payload={'schema':epoch.RECEIPT_SCHEMA,'enrollment_id':args.enrollment_id,'runtime_uid':uid,'runtime_gid':gid,
                'origin':'current_native_user_operator','created_at':datetime.now(timezone.utc).isoformat(),
                'plan_sha256':contract.digest(plan),'topology_intent_sha256':sha256(topology_raw).hexdigest(),
                'publication_sha256':plan['publication_sha256'],'authorization':'explicit_user_operator',
                'historical_custody_recovered':False,'release_accepted':False,'autonomous_repair_credit':False}
            receipt=epoch.sign_completion(payload,key)
            for name,raw in [('operator-intent.json',expected),('plan.json',op.canonical(plan)),
                ('registry.initial.json',op.canonical(data['registry'])),('native-snapshot.json',op.canonical(data['native'])),
                ('authority-snapshot.json',op.canonical(data['authority'])),('topology.original',data['topology_raw']),
                ('topology-intent.original',data['prior_intent_raw']),('topology-intent.current.json',topology_raw),
                ('activation-key.original',key),('publication.json',op.canonical(put))]:op.archive_write(archive/name,raw,uid)
            transaction.save()
            barrier=state/'controller-installation.json'
            expected_barrier=barrier_bytes(op,plan)
            op.archive_write(archive/'barrier.json',expected_barrier,uid)
            # A complete private WAL is published atomically before the first
            # production barrier/unit/key/registry mutation. A crash before
            # this rename leaves only a discardable unpublished private stage.
            op.sync_parent(archive/'plan.json')
            if os.path.lexists(final_archive):raise ValueError('current_enrollment_nonce_consumed')
            archive.rename(final_archive);op.sync_parent(final_archive)
            archive=final_archive;transaction.archive=archive
            ensure_barrier(op,barrier,expected_barrier,uid)
            units=[name for name,item in data['registry']['units'].items() if item['installed']]
            try:
                quiesce(op,environment,data['native'],units)
                # Authority was validated under original coordinates before any
                # selector exists; now retain exact source bytes through cutover.
                for name,item in data['authority']['sources'].items():
                    if record(name,owners={data['authority']['source_uid']})!=item:raise ValueError('current_enrollment_authority_changed')
                guards=[name for name in put if name.endswith('/05-eimemory-installation-guard.conf')]
                for name in guards:transaction.apply(name)
                systemctl(environment,'daemon-reload')
                for name in sorted(set(put)-set(guards),key=lambda path:(path.endswith('/runtime-manager.v1.json'),path)):
                    transaction.apply(name)
                transaction.directory(code/'empty-pycache',0o555)
                for leaf in ('jobs','validations','validation-results','activation-receipts','fused-closures','managed-incident-receipts'):
                    op.directory(state/leaf,uid,0o700 if leaf=='managed-incident-receipts' else 0o750)
                socket_path=Path('/run/user')/str(uid)/'eimemory-manager/bridge.sock'
                op.directory(socket_path.parent,uid,0o700)
                verify_authority(op,data)
                transaction.verify()
                op.archive_write(archive/'complete.json',op.canonical(receipt),uid)
                remove_barrier(op,barrier,expected_barrier,uid)
                systemctl(environment,'daemon-reload')
                # Release bridge.lock for the real controller; keep lifecycle
                # serialization. Existing helper locks are non-reentrant, so
                # startup is completed by an independently locked finalizer.
                transaction.journal['phase']='published';transaction.save()
            except BaseException:
                verify_barrier(op,barrier,expected_barrier,uid,missing_ok=True)
                transaction.rollback();systemctl(environment,'daemon-reload')
                remove_barrier(op,barrier,expected_barrier,uid,missing_ok=True)
                restore(op,environment,data['native'],units)
                seal_rollback(op,archive,plan,uid,epoch)
                raise
            bridge_lock.close()
            return finish(op,data,transaction,archive,helper,contract,uid,gid,home,lock_held=True)


def finish(op,data,transaction,archive,helper,contract,uid,gid,home,*,lock_held=False):
    state=home/'.local/state/eimemory/control-plane';environment=data['environment']
    with (nullcontext() if lock_held else op.operator_lock(helper,state/'lifecycle.lock')):
        try:
            transaction.verify()
            systemctl(environment,'start','eimemory-runtime-manager.service')
            proof=verify_controller(op,environment,data['registry'],Path('/run/user')/str(uid)/'eimemory-manager/bridge.sock',
                                    home/'.local/lib/eimemory/control-plane')
            # Bridge must independently validate the current birth receipt and
            # support native manual-release restoration, before writers resume.
            with op.user_operator_profile():
                bridge_module=op.execute_reviewed_module('fresh_bridge',home/'.local/lib/eimemory/control-plane/runtime_manager_bridge.py',
                    contents(transaction.publication[str(home/'.local/lib/eimemory/control-plane/runtime_manager_bridge.py')]))
            bridge=bridge_module.Bridge(contract,registry=home/'.config/eimemory/control-plane/runtime-manager.v1.json',state=state)
            bridge.registered_topology(data['registry'])
            units=[name for name,item in data['registry']['units'].items() if item['installed']]
            restore(op,environment,data['native'],units)
            context=bridge.registered_topology(data['registry'])[0]
            bridge.native_topology_snapshot(data['registry'],context['main_gateway'],context['retired_gateways'])
            transaction.verify()
            op.archive_write(archive/'controller-verification.json',op.canonical(proof),uid)
            transaction.journal['phase']='completed';transaction.save()
            return {'complete':True,'enrollment_id':archive.name,'origin':'current_native_user_operator',
                'registry_generation':data['registry']['generation'],'historical_custody_recovered':False,
                'release_accepted':False,'autonomous_repair_credit':False,'managed_incident_authority_enabled':False}
        except BaseException:
            systemctl(environment,'stop','eimemory-runtime-manager.service')
            with op.operator_lock(helper,state/'bridge.lock'):
                barrier=state/'controller-installation.json'
                plan=op.strict(op.owned_bytes(archive/'plan.json',uid))
                expected_barrier=barrier_bytes(op,plan)
                ensure_barrier(op,barrier,expected_barrier,uid)
                units=[name for name,item in data['registry']['units'].items() if item['installed']]
                quiesce(op,environment,data['native'],units)
                transaction.rollback();systemctl(environment,'daemon-reload')
                remove_barrier(op,barrier,expected_barrier,uid)
                restore(op,environment,data['native'],[name for name,item in data['registry']['units'].items() if item['installed']])
                seal_rollback(op,archive,plan,uid,None)
            raise


def recovery_material(op,archive,plan,home,uid,gid,contract,epoch,*,terminal=False):
    """Validate every rollback destination before any native or file mutation."""
    def raw(name,maximum=64*1024*1024):return op.owned_bytes(archive/name,uid,maximum)
    put=op.strict(raw('publication.json'));journal=op.strict(raw('journal.json'))
    initial=contract.validate_registry(op.strict(raw('registry.initial.json')))
    if (journal.get('schema')!='eimemory.current_user_publication_journal.v1'
            or set(journal)!={'schema','phase','before','attempted','created_directories'}
            or journal.get('phase') not in ({'completed','rolled_back'} if terminal else
                                          {'prepared','publishing','published','rolling_back','rolled_back'})
            or not isinstance(put,dict) or not isinstance(journal['before'],dict)
            or set(put)!=set(journal['before'])
            or contract.digest(journal['before'])!=plan['before_inventory_sha256']
            or contract.digest(initial)!=plan['registry_sha256']
            or initial['runtime_uid']!=uid or initial['runtime_gid']!=gid
            or initial['managed_incident_authority']!={'enabled':False}):
        raise ValueError('current_enrollment_recovery_archive_changed')
    if (not isinstance(journal['attempted'],list) or len(journal['attempted'])!=len(set(journal['attempted']))
            or not set(journal['attempted'])<=set(put)
            or not isinstance(journal['created_directories'],list)
            or len(journal['created_directories'])!=len(set(journal['created_directories']))):
        raise ValueError('current_enrollment_recovery_journal_invalid')
    allowed_directories=set()
    for name,item in put.items():
        path=Path(name)
        if (not path.is_absolute() or '..' in path.parts or str(path)!=name
                or not (op.publication_scope(path,home,initial)
                        or path==home/'.local/state/eimemory/control-plane/activation-signing.key')):
            raise ValueError('current_enrollment_recovery_destination_invalid')
        allowed_directories.update(str(parent) for parent in path.parents if parent.is_relative_to(home) and parent!=home)
        for value in (item,journal['before'][name]):
            if value is None:continue
            if (not isinstance(value,dict) or set(value)!={'raw_hex','sha256','uid','gid','mode'}
                    or value['uid']!=uid or value['gid']!=gid or type(value['mode']) is not int
                    or value['mode'] not in {0o600,0o640,0o644}):
                raise ValueError('current_enrollment_recovery_file_invalid')
            contents(value)
    allowed_directories.add(str(home/'.local/lib/eimemory/control-plane/empty-pycache'))
    if not set(journal['created_directories'])<=allowed_directories:
        raise ValueError('current_enrollment_recovery_directory_invalid')
    intent_path=str(home/'.config/eimemory/control-plane/hermes-topology-intent.v1.json')
    material={name:item for name,item in put.items() if name!=intent_path}
    if contract.digest(material)!=plan['publication_sha256']:
        raise ValueError('current_enrollment_recovery_archive_changed')
    operator=op.strict(raw('operator-intent.json'));epoch.validate_intent(operator,uid=uid,gid=gid)
    key=raw('activation-key.original',4096)
    logical_raw=raw('topology.original');topology_raw=raw('topology-intent.current.json')
    native_raw=raw('native-snapshot.json');authority_raw=raw('authority-snapshot.json')
    epoch.validate_materials(key,plan=plan,intent=operator,registry=initial,
        topology_raw=logical_raw,native_raw=native_raw,authority_raw=authority_raw)
    intent=op.strict(topology_raw)
    if (contents(put[intent_path])!=topology_raw or intent.get('schema')!=epoch.TOPOLOGY_SCHEMA
            or intent.get('intent_id')!=archive.name or intent.get('source')!={
                'enrollment_id':archive.name,'plan_sha256':contract.digest(plan),
                'registry_sha256':plan['registry_sha256'],'base_commit':plan['current_commit']}):
        raise ValueError('current_enrollment_recovery_topology_invalid')
    native=op.strict(native_raw)
    if set(native.get('units',{}))!=set(initial['units']):
        raise ValueError('current_enrollment_recovery_snapshot_invalid')
    for name,value in native['units'].items():validate_native_snapshot_unit(name,value,uid)
    return put,journal,native


def retry_completed(args,op,files,manifest,account,helper,contract,epoch,archive):
    """A repeated nonce validates its existing epoch and never creates a key."""
    uid=account.pw_uid;home=Path(account.pw_dir);state=home/'.local/state/eimemory/control-plane'
    helper.trusted_directory(archive,private=True)
    expected=op.reviewed(args.operator_intent,args.expected_operator_intent_sha256,65536)
    if op.owned_bytes(archive/'operator-intent.json',uid)!=expected:
        raise ValueError('current_enrollment_nonce_operator_mismatch')
    plan=op.strict(op.owned_bytes(archive/'plan.json',uid));epoch.validate_plan(plan,uid=uid,gid=account.pw_gid)
    if plan['bundle_sha256']!=args.expected_bundle_sha256 or plan['current_commit']!=manifest['controller_commit']:
        raise ValueError('current_enrollment_nonce_bundle_mismatch')
    with op.operator_lock(helper,state/'lifecycle.lock'):
        phase=op.strict(op.owned_bytes(archive/'journal.json',uid)).get('phase')
        if phase!='completed':raise ValueError('current_enrollment_nonce_consumed')
        put,journal,_native=recovery_material(op,archive,plan,home,uid,account.pw_gid,contract,epoch,terminal=True)
        if journal['phase']!='completed':raise ValueError('current_enrollment_nonce_consumed')
        if os.path.lexists(state/'controller-installation.json'):raise ValueError('current_enrollment_incomplete_barrier_preserved')
        # Runtime authority journals may legally grow after enrollment. Verify
        # every immutable coordinate, and authenticate birth/current topology
        # independently; never overwrite those live journals to match a birth.
        immutable={name:item for name,item in put.items() if name.startswith(str(home/'.local/lib/eimemory/control-plane')+'/')
            or name.startswith(str(home/'.config/systemd/user')+'/') or name in {
                str(home/'.config/eimemory/control-plane/runtime-manager.v1.json'),
                str(home/'.config/eimemory/control-plane/installation.v1.json'),
                str(home/'.config/eimemory/control-plane/user-runtime.env'),
                str(home/'.config/eimemory/control-plane/hermes-topology-intent.v1.json'),
                str(state/'activation-signing.key')}}
        Publication(op,archive,uid,account.pw_gid,immutable).verify()
        initial=contract.validate_registry(op.strict(op.owned_bytes(archive/'registry.initial.json',uid)))
        environment={'PATH':'/usr/bin:/bin','LC_ALL':'C','XDG_RUNTIME_DIR':'/run/user/'+str(uid),
                     'DBUS_SESSION_BUS_ADDRESS':'unix:path=/run/user/'+str(uid)+'/bus'}
        verify_controller(op,environment,initial,Path('/run/user')/str(uid)/'eimemory-manager/bridge.sock',
                          home/'.local/lib/eimemory/control-plane')
        with op.user_operator_profile():
            module=op.execute_reviewed_module('fresh_bridge',home/'.local/lib/eimemory/control-plane/runtime_manager_bridge.py',
                files['runtime_manager_bridge.py'])
        bridge=module.Bridge(contract,registry=home/'.config/eimemory/control-plane/runtime-manager.v1.json',state=state)
        intent=bridge.registered_topology(initial)[0]
        bridge.native_topology_snapshot(initial,intent['main_gateway'],intent['retired_gateways'])
        return {'complete':True,'already_enrolled':True,'enrollment_id':archive.name,
            'origin':'current_native_user_operator','registry_generation':initial['generation'],
            'historical_custody_recovered':False,'release_accepted':False,'autonomous_repair_credit':False,
            'managed_incident_authority_enabled':False}


def rollback_payload(plan):
    return {'schema':'eimemory.current_user_rollback_receipt.v1','enrollment_id':plan['enrollment_id'],
        'runtime_uid':plan['runtime_uid'],'runtime_gid':plan['runtime_gid'],'plan_sha256':sha256(
            json.dumps(plan,sort_keys=True,separators=(',',':'),ensure_ascii=False,allow_nan=False).encode()).hexdigest(),
        'origin':'current_native_user_operator','outcome':'rolled_back','historical_custody_recovered':False,
        'release_accepted':False,'autonomous_repair_credit':False}


def seal_rollback(op,archive,plan,uid,epoch):
    import hmac
    key=op.owned_bytes(archive/'activation-key.original',uid,4096)
    if len(key)!=32 or sha256(key).hexdigest()!=plan['activation_key_sha256']:
        raise ValueError('current_enrollment_rollback_key_changed')
    payload=rollback_payload(plan)
    # Same domain as the current-epoch receipt, with a distinct typed schema.
    signature=hmac.new(key,b'eimemory.current_user_enrollment.v1\x00'+op.canonical(payload),'sha256').hexdigest()
    op.archive_write(archive/'rollback-complete.json',op.canonical({'payload':payload,'signature':signature}),uid)


def validate_abandoned_stage(op,archive,uid):
    info=archive.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid!=uid or stat.S_IMODE(info.st_mode)!=0o700:
        raise ValueError('current_enrollment_abandoned_stage_invalid')
    value=op.strict(op.owned_bytes(archive/'abandoned.json',uid))
    match=re.fullmatch(r'\.abandoned-([0-9a-f]{32})-[0-9]+',archive.name)
    if (value.get('schema')!='eimemory.current_user_unpublished_abandonment.v1'
            or value.get('outcome')!='unpublished_stage_abandoned'
            or match is None or value.get('enrollment_id')!=match[1]
            or value.get('authorization')!='explicit_user_operator'
            or value.get('historical_custody_recovered') is not False
            or value.get('release_accepted') is not False or value.get('autonomous_repair_credit') is not False):
        raise ValueError('current_enrollment_abandoned_stage_invalid')
    for path in archive.iterdir():
        item=record(path,owners={uid})
        if item is None or item['mode']!=0o600:raise ValueError('current_enrollment_abandoned_stage_invalid')


def recover_unpublished(args,op,account,helper,state):
    uid=account.pw_uid;home=Path(account.pw_dir)
    expected=getattr(args,'expected_operator_intent_sha256',None)
    if expected is None or re.fullmatch('[0-9a-f]{64}',expected) is None:
        raise ValueError('current_enrollment_unpublished_operator_review_required')
    paths=list((state/'current-enrollments').glob('.preparing-'+args.enrollment_id+'-*'))
    if not paths:
        abandoned=list((state/'current-enrollments').glob('.abandoned-'+args.enrollment_id+'-*'))
        if len(abandoned)==1:
            validate_abandoned_stage(op,abandoned[0],uid)
            result=op.strict(op.owned_bytes(abandoned[0]/'abandoned.json',uid))
            if result.get('operator_intent_sha256')!=expected:raise ValueError('current_enrollment_nonce_operator_mismatch')
            return {**result,'already_abandoned':True}
    if len(paths)!=1:raise ValueError('current_enrollment_unpublished_stage_ambiguous')
    with op.operator_lock(helper,state/'lifecycle.lock'),op.operator_lock(helper,state/'bridge.lock'):
        archive=paths[0];helper.trusted_directory(archive,private=True)
        for path in (state/'controller-installation.json',state/'activation-signing.key',
            home/'.config/eimemory/control-plane/runtime-manager.v1.json',home/'.config/eimemory/control-plane/installation.v1.json'):
            if os.path.lexists(path):raise ValueError('current_enrollment_unpublished_effect_preserved')
        op.stopped_controller({'PATH':'/usr/bin:/bin','XDG_RUNTIME_DIR':'/run/user/'+str(uid),
            'DBUS_SESSION_BUS_ADDRESS':'unix:path=/run/user/'+str(uid)+'/bus'})
        allowed={'operator-intent.json','plan.json','registry.initial.json','native-snapshot.json','authority-snapshot.json',
            'topology.original','topology-intent.original','topology-intent.current.json','activation-key.original',
            'publication.json','journal.json','barrier.json'}
        for path in archive.iterdir():
            if path.name not in allowed:raise ValueError('current_enrollment_unpublished_unknown_material')
            if record(path,owners={uid})['mode']!=0o600:raise ValueError('current_enrollment_unpublished_material_untrusted')
        intent=archive/'operator-intent.json'
        if intent.exists() and sha256(op.owned_bytes(intent,uid)).hexdigest()!=expected:
            raise ValueError('current_enrollment_nonce_operator_mismatch')
        result={'schema':'eimemory.current_user_unpublished_abandonment.v1','enrollment_id':args.enrollment_id,
            'operator_intent_sha256':expected,'authorization':'explicit_user_operator','outcome':'unpublished_stage_abandoned',
            'historical_custody_recovered':False,'release_accepted':False,'autonomous_repair_credit':False}
        op.archive_write(archive/'abandoned.json',op.canonical(result),uid)
        destination=archive.with_name(archive.name.replace('.preparing-','.abandoned-',1))
        if os.path.lexists(destination):raise ValueError('current_enrollment_abandoned_stage_collision')
        archive.rename(destination);op.sync_parent(destination)
        return result


def validate_rollback_seal(op,archive,plan,uid,epoch):
    import hmac
    key=op.owned_bytes(archive/'activation-key.original',uid,4096)
    if len(key)!=32 or sha256(key).hexdigest()!=plan['activation_key_sha256']:
        raise ValueError('current_enrollment_rollback_key_changed')
    value=op.strict(op.owned_bytes(archive/'rollback-complete.json',uid))
    expected=epoch.sign_completion(rollback_payload(plan),key)
    if (value.get('payload')!=expected['payload'] or not isinstance(value.get('signature'),str)
            or not hmac.compare_digest(value['signature'],expected['signature'])):
        raise ValueError('current_enrollment_rollback_receipt_changed')
    return True


def finish_pending(args,op,files,manifest,account,helper,contract,epoch):
    """Complete a published epoch forward, preserving all subsequent state."""
    uid=account.pw_uid;gid=account.pw_gid;home=Path(account.pw_dir)
    epoch.hexadecimal(args.enrollment_id,32)
    state=home/'.local/state/eimemory/control-plane';code=home/'.local/lib/eimemory/control-plane'
    authority=home/'.config/eimemory/control-plane';archive=state/'current-enrollments'/args.enrollment_id
    helper.trusted_directory(archive,private=True)
    plan=op.strict(op.reviewed(archive/'plan.json',args.expected_plan_sha256,65536))
    epoch.validate_plan(plan,uid=uid,gid=gid)
    if plan['bundle_sha256']!=args.expected_bundle_sha256:raise ValueError('current_enrollment_recovery_bundle_mismatch')
    environment={'PATH':'/usr/bin:/bin','LC_ALL':'C','XDG_RUNTIME_DIR':'/run/user/'+str(uid),
                 'DBUS_SESSION_BUS_ADDRESS':'unix:path=/run/user/'+str(uid)+'/bus'}
    with op.operator_lock(helper,state/'lifecycle.lock'):
        current_phase=op.strict(op.owned_bytes(archive/'journal.json',uid)).get('phase')
        if current_phase not in {'published','completed'}:raise ValueError('current_enrollment_forward_publication_required')
        put,journal,native=recovery_material(op,archive,plan,home,uid,gid,contract,epoch,terminal=current_phase=='completed')
        if os.path.lexists(state/'controller-installation.json'):
            raise ValueError('current_enrollment_forward_barrier_preserved')
        initial=contract.validate_registry(op.strict(op.owned_bytes(archive/'registry.initial.json',uid)))
        current=contract.validate_registry(op.strict(op.owned_bytes(authority/'runtime-manager.v1.json',uid)))
        if any(current[field]!=initial[field] for field in set(initial)-{'generation','runtime_commit','controller_commit'}):
            raise ValueError('current_enrollment_forward_authority_changed')
        # Stable controller/selector material is immutable. Business authority
        # and runtime policy may have legitimately advanced after publication.
        fixed={name:item for name,item in put.items() if name.startswith(str(code)+'/') or name in {
            str(authority/'installation.v1.json'),str(authority/'hermes-topology-intent.v1.json'),str(state/'activation-signing.key'),
            str(home/'.config/systemd/user/eimemory-runtime-manager.service')}}
        Publication(op,archive,uid,gid,fixed).verify()
        proof=verify_controller(op,environment,current,Path('/run/user')/str(uid)/'eimemory-manager/bridge.sock',code)
        with op.user_operator_profile():
            module=op.execute_reviewed_module('fresh_bridge',code/'runtime_manager_bridge.py',files['runtime_manager_bridge.py'])
        bridge=module.Bridge(contract,registry=authority/'runtime-manager.v1.json',state=state)
        intent=bridge.registered_topology(current)[0]
        if current_phase=='completed':
            return {'complete':True,'already_enrolled':True,'enrollment_id':archive.name,'completed_forward':False,
                'historical_custody_recovered':False,'release_accepted':False,'autonomous_repair_credit':False}
        if current!=initial:raise ValueError('current_enrollment_forward_registry_changed')
        jobs=[]
        for path in sorted((state/'jobs').glob('*.json')):
            if len(jobs)>=128:raise ValueError('current_enrollment_forward_job_inventory_limit')
            jobs.append(bridge.record(contract.hex_value(path.stem,64,'current_enrollment_forward_job_invalid')))
        # The Bridge forbids all job submission/registry publication until
        # birth completes. Any such file now is foreign/ambiguous, preserved
        # for review rather than interpreted as a legitimate evolution chain.
        if jobs:raise ValueError('current_enrollment_forward_job_state_preserved')
        pointer=state/'active-transition.json'
        if os.path.lexists(pointer):
            value=op.strict(op.owned_bytes(pointer,uid))
            raise ValueError('current_enrollment_forward_transition_unbound')
        preserved_receipts=[]
        for path in sorted((state/'activation-receipts').glob('*.json')):
            raw=op.owned_bytes(path,uid)
            bridge.verify_activation(path.stem,sha256(raw).hexdigest());preserved_receipts.append(path.stem)
            if len(preserved_receipts)>4096:raise ValueError('current_enrollment_forward_receipt_inventory_limit')
        original=op.strict(op.owned_bytes(archive/'authority-snapshot.json',uid,64*1024*1024))
        roots={}
        for name in ('identity','subject-projection'):
            path=authority/name
            if not path.exists():
                if name in original['identity_roots']:raise ValueError('current_enrollment_forward_identity_missing')
                continue
            item=original['publication'].get(str(path/'requests.jsonl'))
            prefix=contents(item) if item is not None else b''
            roots[name]={'path':str(path),'prefix_size':len(prefix),'prefix_sha256':sha256(prefix).hexdigest()}
        result=subprocess.run([str(Path('/opt/eimemory/releases')/current['runtime_commit']/'.venv/bin/python'),'-I','-B','-c',
            FORWARD_PROBE,str(Path('/opt/eimemory/releases')/current['runtime_commit'])],input=op.canonical(roots),
            env=probe_environment(uid,native['authority_environment'],user_mode=True),capture_output=True,timeout=60)
        if result.returncode:raise ValueError('current_enrollment_forward_identity_invalid')
        identities=op.strict(result.stdout)
        restore(op,environment,native,[name for name,item in initial['units'].items() if item['installed']])
        bridge.native_topology_snapshot(current,intent['main_gateway'],intent['retired_gateways'])
        observation={'schema':'eimemory.current_user_forward_observation.v1','plan_sha256':args.expected_plan_sha256,
            'controller':proof,'registry_generation':current['generation'],
            'preserved_activation_receipts':preserved_receipts,'identity_observation':identities,
            'existing_job_keys':[],'runtime_resume_deferred_to_registered_job':False,
            'release_accepted':False,'historical_custody_recovered':False,'autonomous_repair_credit':False}
        if current_phase=='published':
            op.archive_write(archive/('forward-observation-'+contract.digest(observation)+'.json'),op.canonical(observation),uid)
            journal['phase']='completed';op.write(archive/'journal.json',op.canonical(journal),uid,0o600)
        return {'complete':True,'already_enrolled':current_phase=='completed','enrollment_id':archive.name,
            'completed_forward':True,'runtime_resume_deferred_to_registered_job':False,
            'preserved_activation_receipts':len(preserved_receipts),'historical_custody_recovered':False,
            'release_accepted':False,'autonomous_repair_credit':False}


def recover(args,op,files,manifest,account,helper,contract,epoch):
    """Roll back only an authenticated incomplete current enrollment."""
    uid=account.pw_uid;home=Path(account.pw_dir);state=home/'.local/state/eimemory/control-plane'
    epoch.hexadecimal(args.enrollment_id,32)
    archive=state/'current-enrollments'/args.enrollment_id
    if not os.path.lexists(archive):return recover_unpublished(args,op,account,helper,state)
    helper.trusted_directory(archive,private=True)
    if not getattr(args,'expected_plan_sha256',None):raise ValueError('current_enrollment_published_plan_review_required')
    plan_raw=op.reviewed(archive/'plan.json',args.expected_plan_sha256,65536);plan=op.strict(plan_raw)
    epoch.validate_plan(plan,uid=uid,gid=account.pw_gid)
    if plan['bundle_sha256']!=args.expected_bundle_sha256:raise ValueError('current_enrollment_recovery_bundle_mismatch')
    environment={'PATH':'/usr/bin:/bin','LC_ALL':'C','XDG_RUNTIME_DIR':'/run/user/'+str(uid),
                 'DBUS_SESSION_BUS_ADDRESS':'unix:path=/run/user/'+str(uid)+'/bus'}
    with op.operator_lock(helper,state/'lifecycle.lock'):
        # Authenticate the pending epoch before considering any service action.
        # An unrelated or already advanced controller must remain untouched.
        put,journal,native=recovery_material(op,archive,plan,home,uid,account.pw_gid,contract,epoch)
        barrier=state/'controller-installation.json';expected_barrier=barrier_bytes(op,plan)
        verify_barrier(op,barrier,expected_barrier,uid,missing_ok=True)
        initial=contract.validate_registry(op.strict(op.owned_bytes(archive/'registry.initial.json',uid)))
        if journal['phase']=='rolled_back' and os.path.lexists(archive/'rollback-complete.json'):
            validate_rollback_seal(op,archive,plan,uid,epoch)
            for name,before in journal['before'].items():
                if record(name,owners={uid})!=before:raise ValueError('current_enrollment_rollback_evolved')
            op.stopped_controller(environment)
            for name,original in native['units'].items():
                current=op.native_state(name,['ActiveState','UnitFileState'],environment)
                if current['UnitFileState']!=original['UnitFileState'] or original['ActiveState']=='active' and current['ActiveState']!='active':
                    raise ValueError('current_enrollment_rollback_native_changed')
            return {'rolled_back':True,'already_rolled_back':True,'enrollment_id':archive.name,
                    'historical_custody_recovered':False,'release_accepted':False,'autonomous_repair_credit':False}
        try:op.stopped_controller(environment)
        except ValueError as error:
            if str(error)!='user_install_controller_must_be_stopped':raise
            if journal['phase']!='published':raise ValueError('current_enrollment_recovery_foreign_controller') from error
            policy=op.execute_reviewed_module('recover_live_gateway_writer',home/'.local/lib/eimemory/control-plane/runtime_manager_user_policy.py',files['runtime_manager_user_policy.py'])
            Publication(op,archive,uid,account.pw_gid,put,journal=journal,gateway_writer=policy.write_user_metadata).verify()
            if any((state/'jobs').glob('*.json')) or os.path.lexists(state/'active-transition.json'):
                raise ValueError('current_enrollment_recovery_new_work_preserved')
            proof=verify_controller(op,environment,initial,Path('/run/user')/str(uid)/'eimemory-manager/bridge.sock',
                                    home/'.local/lib/eimemory/control-plane')
            op.archive_write(archive/'controller-before-recovery.json',op.canonical(proof),uid)
            systemctl(environment,'stop','eimemory-runtime-manager.service')
            op.stopped_controller(environment)
        with op.operator_lock(helper,state/'bridge.lock'):
            verify_barrier(op,barrier,expected_barrier,uid,missing_ok=True)
            policy=op.execute_reviewed_module('recover_gateway_writer',home/'.local/lib/eimemory/control-plane/runtime_manager_user_policy.py',files['runtime_manager_user_policy.py'])
            transaction=Publication(op,archive,uid,account.pw_gid,put,journal=journal,gateway_writer=policy.write_user_metadata)
            units=[name for name,item in native['units'].items() if item['LoadState']=='loaded']
            ensure_barrier(op,barrier,expected_barrier,uid)
            quiesce(op,environment,native,units)
            transaction.rollback();systemctl(environment,'daemon-reload')
            remove_barrier(op,barrier,expected_barrier,uid)
            restore(op,environment,native,units)
            seal_rollback(op,archive,plan,uid,epoch)
            return {'rolled_back':True,'enrollment_id':archive.name,'historical_custody_recovered':False,
                    'release_accepted':False,'autonomous_repair_credit':False}
