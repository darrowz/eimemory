#!/usr/bin/python3
"""Root-reviewed bootstrap for fixed templates, never a product root entrypoint.

Copy this script to a root-private directory after reviewing its SHA256. The
input bundle's complete raw SHA256 is a required reviewed operator argument.
No Runtime, database, plugin, or runtime-owned Python module is constructed.
"""
from __future__ import annotations
import argparse
from hashlib import sha256
import importlib.util
import io
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import time
import urllib.request
from urllib.error import HTTPError, URLError
import zipfile
from contextlib import contextmanager

ROOT=Path('/usr/libexec/eimemory-manager')
STATE=Path('/var/lib/eimemory-manager')
REGISTRY=Path('/etc/eimemory-code-automation-authority/runtime-manager.v1.json')
SYSTEM_UNITS=Path('/etc/systemd/system')
CURRENT=Path('/opt/eimemory/current')
RELEASES=Path('/opt/eimemory/releases')
STORAGE_STATE=Path('/var/lib/eimemory/state')


def directory(path,*,gid=0,mode=0o750):
    path=Path(path)
    if not path.exists():
        directory(path.parent,gid=0,mode=0o755)
        path.mkdir(mode=mode);os.chown(path,0,gid)
    for item in (path,*path.parents):
        s=item.lstat()
        if not stat.S_ISDIR(s.st_mode) or s.st_uid!=0 or stat.S_IMODE(s.st_mode)&0o022:
            raise ValueError('bootstrap_directory_untrusted')
    return path


def exact_directory(path,*,gid,mode):
    path=directory(path,gid=gid,mode=mode)
    metadata=path.lstat()
    if metadata.st_gid!=gid or stat.S_IMODE(metadata.st_mode)!=mode:
        raise ValueError('bootstrap_directory_identity_or_mode_invalid')
    return path


def read(path,*,limit=16*1024*1024,root=True):
    fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_CLOEXEC)
    try:
        a=os.fstat(fd)
        if not stat.S_ISREG(a.st_mode) or a.st_nlink!=1 or a.st_size>limit or (root and (a.st_uid!=0 or stat.S_IMODE(a.st_mode)&0o022)):
            raise ValueError('bootstrap_input_untrusted')
        raw=os.read(fd,limit+1);b=os.fstat(fd);c=Path(path).lstat()
        key=lambda s:(s.st_dev,s.st_ino,s.st_size,s.st_mtime_ns,s.st_ctime_ns,s.st_uid,s.st_gid,s.st_mode,s.st_nlink)
        if key(a)!=key(b) or key(a)!=key(c):raise ValueError('bootstrap_input_changed')
        return raw
    finally:os.close(fd)


def write(path,raw,*,gid,mode=0o640,exclusive=False):
    path=Path(path);directory(path.parent,gid=gid)
    temporary=path.with_name(path.name+'.tmp.'+str(os.getpid()))
    fd=os.open(temporary,os.O_CREAT|os.O_EXCL|os.O_WRONLY|os.O_NOFOLLOW,mode)
    try:
        os.fchown(fd,0,gid);os.fchmod(fd,mode)
        stream=os.fdopen(fd,'wb',closefd=False);stream.write(raw);stream.flush();os.fsync(fd);stream.close()
        if exclusive:os.link(temporary,path,follow_symlinks=False)
        else:
            if path.exists():read(path)
            os.replace(temporary,path)
        d=os.open(path.parent,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
        try:os.fsync(d)
        finally:os.close(d)
    finally:
        os.close(fd)
        if temporary.exists():temporary.unlink()


def call(args):
    done=subprocess.run(args,check=False,capture_output=True,text=True,timeout=300,
        env={'PATH':'/usr/sbin:/usr/bin:/sbin:/bin','LC_ALL':'C','TZ':'UTC'})
    if done.returncode:raise ValueError('bootstrap_systemctl_failed')
    return done.stdout


def user_command(account,*args):
    return ['/usr/sbin/runuser','-u',account.pw_name,'--','/usr/bin/env',
        'XDG_RUNTIME_DIR=/run/user/'+str(account.pw_uid),
        'DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/'+str(account.pw_uid)+'/bus',
        '/usr/bin/systemctl','--user',*args]


def bundle(raw,expected):
    if re.fullmatch('[0-9a-f]{64}',expected) is None or sha256(raw).hexdigest()!=expected:
        raise ValueError('bootstrap_reviewed_bundle_digest_mismatch')
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        names=archive.namelist()
        if len(names)!=len(set(names)) or len(names)>100 or any(n.startswith('/') or '..' in n.split('/') or '\\' in n for n in names):
            raise ValueError('bootstrap_bundle_names_invalid')
        if any(i.file_size>1048576 for i in archive.infolist()):raise ValueError('bootstrap_bundle_member_too_large')
        files={name:archive.read(name) for name in names}
    manifest=json.loads(files.pop('manifest.json'))
    if set(manifest)!={'schema','controller_commit','files'} or manifest['schema']!='eimemory.runtime_manager_bundle.v1' or re.fullmatch('[0-9a-f]{40}',manifest['controller_commit']) is None:
        raise ValueError('bootstrap_bundle_manifest_invalid')
    if set(files)!=set(manifest['files']) or any(sha256(raw).hexdigest()!=manifest['files'][name] for name,raw in files.items()):
        raise ValueError('bootstrap_bundle_member_digest_mismatch')
    if not {'runtime_manager_contract.py','runtime_manager_bridge.py','runtime_manager_launch.py','runtime-policy.conf','eimemory-runtime-manager.service'}<=set(files):
        raise ValueError('bootstrap_bundle_support_missing')
    return files,manifest


@contextmanager
def lifecycle_lock():
    import fcntl
    directory(STATE,gid=1001,mode=0o750)
    path=STATE/'lifecycle.lock'
    fd=os.open(path,os.O_RDWR|os.O_CREAT|os.O_NOFOLLOW|os.O_CLOEXEC,0o600)
    try:
        meta=os.fstat(fd);named=path.lstat()
        if (not stat.S_ISREG(meta.st_mode) or meta.st_uid!=0 or meta.st_nlink!=1
                or stat.S_IMODE(meta.st_mode)!=0o600 or (meta.st_dev,meta.st_ino)!=(named.st_dev,named.st_ino)):
            raise ValueError('bootstrap_lifecycle_lock_untrusted')
        deadline=time.monotonic()+5
        while True:
            try:fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB);break
            except BlockingIOError:
                if time.monotonic()>=deadline:raise ValueError('bootstrap_lifecycle_lock_timeout')
                time.sleep(0.025)
        yield
    finally:os.close(fd)


def load_contract():
    path=ROOT/'runtime_manager_contract.py';raw=read(path)
    spec=importlib.util.spec_from_file_location('reviewed_runtime_contract',path)
    c=importlib.util.module_from_spec(spec);exec(compile(raw,str(path),'exec'),c.__dict__)
    return c


def owner_files(account,operation,value):
    # Fixed reviewed helper, product source is never imported in root.
    path=ROOT/'runtime_manager_owner_files.py';read(path)
    done=subprocess.run(['/usr/sbin/runuser','-u',account.pw_name,'--','/usr/bin/python3','-I','-B','-X',
        'pycache_prefix=/var/lib/eimemory-manager/empty-pycache',str(path),operation],
        input=json.dumps(value,sort_keys=True,separators=(',',':')),capture_output=True,text=True,timeout=30,
        env={'PATH':'/usr/bin:/bin','LC_ALL':'C','PYTHONDONTWRITEBYTECODE':'1'})
    if done.returncode or done.stderr or len(done.stdout.encode())>4*1024*1024:raise ValueError('bootstrap_user_owner_files_failed')
    return json.loads(done.stdout)


def storage_clean():
    for name in ('storage-release-transaction.json','.storage-release-transaction.json.clearing','.storage-release-transaction.json.recovery'):
        path=STORAGE_STATE/name
        if path.exists() or path.is_symlink():raise ValueError('bootstrap_storage_recovery_not_clean')


def healthy_original(commit):
    # Public /health deliberately omits deploy identity. Read one fixed local
    # credential as data only; do not source it, export it, archive it or follow
    # an HTTP redirect carrying that credential.
    lines=read('/etc/eimemory/rpc.env',limit=65536,root=False).decode('utf-8').splitlines()
    tokens=[line.strip().partition('=')[2].strip() for line in lines
        if line.strip().startswith('EIMEMORY_RPC_AUTH_TOKEN=')]
    if len(tokens)!=1 or not tokens[0] or len(tokens[0])>4096 or any(c in tokens[0] for c in '\r\n\0'):
        raise ValueError('bootstrap_original_health_auth_unavailable')
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self,*_args,**_kwargs):return None
    opener=urllib.request.build_opener(urllib.request.ProxyHandler({}),NoRedirect())
    request=urllib.request.Request('http://127.0.0.1:8091/health',headers={'Authorization':'Bearer '+tokens[0]})
    release=RELEASES/commit
    deadline=time.monotonic()+120;last_error=None
    while time.monotonic()<deadline:
        try:
            with opener.open(request,timeout=min(10,max(0.1,deadline-time.monotonic()))) as response:
                raw=response.read(1048577)
        except HTTPError as exc:
            if exc.code<500:raise ValueError('bootstrap_original_health_http_rejected') from exc
            last_error=exc
        except (URLError,OSError,TimeoutError) as exc:
            last_error=exc
        else:
            if len(raw)>1048576:raise ValueError('bootstrap_health_output_limit')
            health=json.loads(raw)
            # An observed wrong identity is rejected immediately. Only genuine
            # readiness/transport startup delay is polled; no owner is restarted.
            if (not isinstance(health,dict) or health.get('commit')!=commit
                    or health.get('service')!='eimemory-rpc' or health.get('configured_runtime_commit')!=commit
                    or health.get('import_root')!=str(release/'eimemory')
                    or not isinstance(health.get('paths'),dict) or health['paths'].get('release')!=str(release)
                    or not isinstance(health.get('checks'),dict)
                    or not {'process','store','runtime_identity','ready'}<=set(health['checks'])):
                raise ValueError('bootstrap_original_health_identity_failed')
            if health.get('ok') is True and all(v is True for v in health['checks'].values()):
                return {'commit':commit,'ok':True,'checks':health['checks']}
            last_error=ValueError('bootstrap_original_health_not_ready')
        remaining=deadline-time.monotonic()
        if remaining>0:time.sleep(min(1,remaining))
    raise ValueError('bootstrap_original_health_readiness_timeout') from last_error


def root_snapshot(files):
    paths={REGISTRY,SYSTEM_UNITS/'eimemory-runtime-manager.service'}
    for name in files:
        if name=='manifest.json':continue
        paths.add(ROOT/name)
        if name.startswith('templates/'):
            unit=name.split('/')[-1];paths.add(SYSTEM_UNITS/unit)
            if unit.endswith('.service'):paths.add(SYSTEM_UNITS/(unit+'.d')/'90-eimemory-runtime.conf')
    result={}
    for path in sorted(paths):
        if path.exists() or path.is_symlink():
            raw=read(path);st=path.stat()
            result[str(path)]={'raw_hex':raw.hex(),'sha256':sha256(raw).hexdigest(),'mode':stat.S_IMODE(st.st_mode),'gid':st.st_gid}
        else:result[str(path)]=None
    return result


def restore_root_files(snapshot):
    if not isinstance(snapshot,dict) or len(snapshot)>250:raise ValueError('bootstrap_root_snapshot_invalid')
    # All coordinates come from our original root-only history. Validate all
    # names and original bytes before stopping an owner or replacing a file.
    for name,value in snapshot.items():
        path=Path(name)
        if ('..' in path.parts or not path.is_absolute() or
                not (path==REGISTRY or path.is_relative_to(ROOT) or path.is_relative_to(SYSTEM_UNITS))):
            raise ValueError('bootstrap_root_snapshot_path_invalid')
        if value is not None:
            if not isinstance(value,dict) or set(value)!={'raw_hex','sha256','mode','gid'}:raise ValueError('bootstrap_root_snapshot_invalid')
            raw=bytes.fromhex(value['raw_hex'])
            if (sha256(raw).hexdigest()!=value['sha256'] or type(value['mode']) is not int or value['mode']&0o022
                    or type(value['gid']) is not int or value['gid']<0):raise ValueError('bootstrap_root_snapshot_digest_invalid')
    for name,value in snapshot.items():
        path=Path(name)
        if value is None:
            if path.exists() or path.is_symlink():read(path);path.unlink()
        else:write(path,bytes.fromhex(value['raw_hex']),gid=value['gid'],mode=value['mode'])
        if path.parent.exists():
            directory_fd=os.open(path.parent,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
            try:os.fsync(directory_fd)
            finally:os.close(directory_fd)


def exited_job_state(c,key,record):
    properties=('LoadState','ActiveState','SubState','Result','InvocationID','ExecMainStatus',
        'MainPID','ControlGroup','ExecMainExitTimestampMonotonic')
    state=dict(line.split('=',1) for line in call(['/usr/bin/systemctl','show',c.job_unit(key),
        '--property='+','.join(properties)]).splitlines() if '=' in line)
    if set(state)!=set(properties):raise ValueError('bootstrap_job_native_state_incomplete')
    original=record.get('original_invocation');actual=state['InvocationID']
    if actual and original not in {None,actual}:raise ValueError('bootstrap_job_invocation_changed')
    mono=state['ExecMainExitTimestampMonotonic']
    genuine_exit=(state['LoadState']=='loaded' and actual and mono.isdigit() and int(mono)>0
        and state['MainPID']=='0'
        and ((state['ActiveState']=='active' and state['SubState']=='exited' and state['Result']=='success' and state['ExecMainStatus']=='0')
            or (state['ActiveState'] in {'inactive','failed'} and state['Result'] in {'exit-code','signal','timeout','core-dump','watchdog'})))
    if genuine_exit:
        c.hex_value(actual,32,'bootstrap_job_invocation_invalid')
        old=record.get('terminal_state')
        if old and any(old.get(name)!=state[name] for name in ('InvocationID','Result','ExecMainStatus','ExecMainExitTimestampMonotonic')):
            raise ValueError('bootstrap_job_terminal_state_changed')
        record['original_invocation']=actual
        if old is None:record['terminal_state']=state
        write(STATE/'jobs'/(key+'.json'),c.canonical(record),gid=record['registry']['runtime_gid'])
    ended=(genuine_exit or (state['ActiveState'] in {'inactive','failed'} and state['MainPID'] in {'0',''}
        and state['LoadState'] in {'loaded','not-found'} and record.get('terminal_state') is not None))
    return state,bool(ended)


def restore_owner(args):
    import pwd
    account=pwd.getpwnam('darrow');c=load_contract()
    if re.fullmatch(r'bootstrap-[0-9]{8}T[0-9]{6}Z-[0-9]+',args.history_id) is None:raise ValueError('bootstrap_history_id_invalid')
    history=STATE/args.history_id
    if not history.is_dir() or history.is_symlink() or stat.S_IMODE(history.stat().st_mode)!=0o700:raise ValueError('bootstrap_history_not_protected')
    directory(history,gid=0,mode=0o700)
    raw=read(history/'owner-snapshot.json')
    if sha256(raw).hexdigest()!=args.expected_history_sha256:raise ValueError('bootstrap_history_review_digest_mismatch')
    original=c.strict_json(raw,max_bytes=16*1024*1024)
    if set(original)!={'schema','commit','root_files','user_files','prior_user_state','prior_system_state'} or original['schema']!='eimemory.runtime_owner_snapshot.v1':raise ValueError('bootstrap_owner_snapshot_invalid')
    if CURRENT.resolve(strict=True)!=RELEASES/original['commit'] or args.expected_current_commit!=original['commit']:
        raise ValueError('bootstrap_owner_restore_serving_release_changed')
    current=read(REGISTRY) if REGISTRY.exists() else b''
    if sha256(current).hexdigest()!=args.expected_registry_sha256:raise ValueError('bootstrap_owner_restore_registry_cas_mismatch')
    storage_clean()
    # A running or ambiguous effect cannot be invalidated by changing owners.
    active=STATE/'active-transition.json'
    abandoned=None
    if active.exists():
        transition=c.strict_json(read(active))
        if transition.get('complete') is not True:
            key=transition.get('job_key');c.hex_value(key,64,'bootstrap_active_job_invalid')
            if args.abandon_failed_job_key!=key:raise ValueError('bootstrap_active_job_requires_review')
            record_path=STATE/'jobs'/(key+'.json')
            if not record_path.exists():raise ValueError('bootstrap_active_job_record_missing')
            record=c.strict_json(read(record_path))
            state,ended=exited_job_state(c,key,record)
            held=record.get('phase') in {'held_incompatible_runtime','completed_held'}
            if not ended or (state.get('Result')=='success' and not held):
                raise ValueError('bootstrap_active_or_unknown_job_cannot_be_abandoned')
            abandoned=key
    jobs=STATE/'jobs'
    if jobs.exists():
        records=sorted(jobs.glob('*.json'))
        if len(records)>1000:raise ValueError('bootstrap_owner_restore_job_inventory_limit')
        for path in records:
            key=path.stem;c.hex_value(key,64,'bootstrap_job_record_name_invalid')
            record=c.strict_json(read(path))
            if record.get('job_key')!=key:raise ValueError('bootstrap_job_record_binding_invalid')
            status,ended=exited_job_state(c,key,record)
            if not ended:
                raise ValueError('bootstrap_owner_restore_job_still_running_or_unknown')
            if (record.get('phase')=='submitted' and record.get('original_invocation') is None
                    and args.abandon_failed_job_key!=key):raise ValueError('bootstrap_owner_restore_ambiguous_submission')
            for operation in c.VALIDATION_PREFIXES:
                path=STATE/'validations'/(key+'-'+operation+'.json')
                if not path.exists():continue
                child=c.strict_json(read(path));unit=c.validation_unit(key,operation)
                if child.get('unit')!=unit or child.get('job_key')!=key:raise ValueError('bootstrap_validation_binding_invalid')
                native=dict(line.split('=',1) for line in call(['/usr/bin/systemctl','show',unit,
                    '--property=LoadState,ActiveState,InvocationID,MainPID,ControlGroup']).splitlines() if '=' in line)
                if native.get('InvocationID') and native['InvocationID']!=child.get('original_invocation'):
                    raise ValueError('bootstrap_validation_invocation_unknown')
                if native.get('ActiveState') in {'active','activating','reloading','deactivating'}:
                    retained=(native.get('MainPID')=='0' and child.get('terminal_state') is not None)
                    if not retained and args.abandon_failed_job_key!=key:
                        raise ValueError('bootstrap_validation_still_running')
                    if (not native.get('InvocationID') or native.get('ControlGroup')!='/system.slice/'+unit):
                        raise ValueError('bootstrap_validation_cgroup_unknown')
                    call(['/usr/bin/systemctl','stop',unit])
                    after=dict(line.split('=',1) for line in call(['/usr/bin/systemctl','show',unit,
                        '--property=ActiveState,InvocationID,MainPID']).splitlines() if '=' in line)
                    if after.get('ActiveState') not in {'inactive','failed'} or after.get('MainPID') not in {'0',''} or after.get('InvocationID') not in {'',child['original_invocation']}:
                        raise ValueError('bootstrap_validation_not_fenced')
    prior=original['prior_user_state'];prior_system=original['prior_system_state']
    if set(prior_system)!=c.REGISTERED_UNITS|{'eimemory-runtime-manager.service'}:raise ValueError('bootstrap_prior_system_inventory_invalid')
    if set(prior)!=c.REGISTERED_UNITS:raise ValueError('bootstrap_prior_owner_inventory_invalid')
    # Validate user files without modifying anything. snapshot equality ignores
    # only the one policy file this reviewed manager itself can replace.
    now=owner_files(account,'snapshot',original['user_files']['units'])
    for name in set(original['user_files']['files'])|set(now['files']):
        if not name.endswith('.d/zzzz-eimemory-host-manager.conf') and now['files'].get(name)!=original['user_files']['files'].get(name):
            raise ValueError('bootstrap_old_owner_configuration_changed')
    write(history/'restore-intent.json',c.canonical({'expected_current_commit':original['commit'],'registry_sha256':sha256(current).hexdigest(),'abandoned_failed_job':abandoned}),gid=0,mode=0o600,exclusive=True)
    # Stop every new product owner; do not start legacy RPC until PID1 RPC has
    # been physically observed inactive. The bridge itself stops after callers.
    for unit in sorted(set(c.REGISTERED_UNITS)-set(c.USER_COMPAT_UNITS),key=lambda u:(u=='eimemory-rpc.service',u)):
        state=dict(line.split('=',1) for line in call(['/usr/bin/systemctl','show',unit,'--property=LoadState,ActiveState']).splitlines() if '=' in line)
        if state.get('LoadState')=='not-found':continue
        if state.get('LoadState')!='loaded':raise ValueError('bootstrap_new_owner_state_unknown')
        call(['/usr/bin/systemctl','disable',unit])
        if state['ActiveState'] in {'active','activating','reloading','deactivating'}:
            call(['/usr/bin/systemctl','stop',unit])
        after=dict(line.split('=',1) for line in call(['/usr/bin/systemctl','show',unit,'--property=ActiveState']).splitlines() if '=' in line)
        if after.get('ActiveState') not in {'inactive','failed'}:raise ValueError('bootstrap_new_owner_still_active')
    manager_state=dict(line.split('=',1) for line in call(['/usr/bin/systemctl','show','eimemory-runtime-manager.service',
        '--property=LoadState,ActiveState']).splitlines() if '=' in line)
    if manager_state.get('LoadState')=='loaded':
        call(['/usr/bin/systemctl','disable','eimemory-runtime-manager.service'])
        if manager_state.get('ActiveState') in {'active','activating','reloading','deactivating'}:
            call(['/usr/bin/systemctl','stop','eimemory-runtime-manager.service'])
        after=dict(line.split('=',1) for line in call(['/usr/bin/systemctl','show','eimemory-runtime-manager.service',
            '--property=ActiveState']).splitlines() if '=' in line)
        if after.get('ActiveState') not in {'inactive','failed'}:raise ValueError('bootstrap_new_manager_still_active')
    elif manager_state.get('LoadState')!='not-found':raise ValueError('bootstrap_new_manager_state_unknown')
    for unit in sorted(c.USER_COMPAT_UNITS):
        if prior[unit]['LoadState']=='loaded':call(user_command(account,'stop',unit))
    owner_files(account,'restore',original['user_files'])
    restore_root_files(original['root_files'])
    call(['/usr/bin/systemctl','daemon-reload']);call(user_command(account,'daemon-reload'))
    # Restore only exact original enablement and activity. No wildcard service
    # action and no root execution of a user-owned program occurs here.
    for unit,state in sorted(prior.items()):
        enabled=state['UnitFileState']
        if enabled in {'enabled','enabled-runtime'}:call(user_command(account,'enable',*(['--runtime'] if enabled=='enabled-runtime' else []),unit))
        elif enabled in {'disabled','masked','not-found','static','indirect','generated','alias',''}:pass
        else:raise ValueError('bootstrap_prior_enablement_unknown')
    active_states={'active','activating','reloading','deactivating'}
    user_rpc=prior['eimemory-rpc.service']['ActiveState'] in active_states
    system_rpc=prior_system['eimemory-rpc.service']['ActiveState'] in active_states
    if user_rpc==system_rpc:raise ValueError('bootstrap_original_rpc_owner_not_unique')
    for unit,state in sorted(prior_system.items()):
        if state['UnitFileState'] in {'enabled','enabled-runtime'}:
            call(['/usr/bin/systemctl','enable',*(['--runtime'] if state['UnitFileState']=='enabled-runtime' else []),unit])
    manager='user' if user_rpc else 'system'
    rpc_start=user_command(account,'start','eimemory-rpc.service') if user_rpc else ['/usr/bin/systemctl','start','eimemory-rpc.service']
    call(rpc_start)
    health=healthy_original(original['commit'])
    storage_clean()
    if CURRENT.resolve(strict=True)!=RELEASES/original['commit']:raise ValueError('bootstrap_owner_restore_current_changed')
    active_before=[]
    for namespace,inventory in [('user',prior),('system',prior_system)]:
        for unit,state in sorted(inventory.items()):
            if state['ActiveState'] in active_states:
                if unit!='eimemory-rpc.service':
                    call(user_command(account,'start',unit) if namespace=='user' else ['/usr/bin/systemctl','start',unit])
                active_before.append({'manager':namespace,'unit':unit})
    opposite=['/usr/bin/systemctl','show','eimemory-rpc.service','--property=ActiveState'] if user_rpc else user_command(account,'show','eimemory-rpc.service','--property=ActiveState')
    other=dict(line.split('=',1) for line in call(opposite).splitlines() if '=' in line)
    if other.get('ActiveState') not in {'inactive','failed'}:raise ValueError('bootstrap_rpc_dual_owner')
    for item in active_before:
        query=user_command(account,'show',item['unit'],'--property=ActiveState') if item['manager']=='user' else ['/usr/bin/systemctl','show',item['unit'],'--property=ActiveState']
        state=dict(line.split('=',1) for line in call(query).splitlines() if '=' in line)
        if state.get('ActiveState') not in {'active','activating'}:raise ValueError('bootstrap_legacy_active_set_not_restored')
    migration=STATE/'migration-prior-active.json'
    if migration.exists():
        capture=c.strict_json(read(migration))
        if capture.get('history_id')!=args.history_id:raise ValueError('bootstrap_restore_migration_history_mismatch')
        capture['restored']=True;capture['owner_restore_history']=args.history_id
        write(migration,c.canonical(capture),gid=account.pw_gid)
    write(history/'restore-complete.json',c.canonical({'ok':True,'manager':manager,'commit':original['commit'],'active_before':active_before,'health':health}),gid=0,mode=0o600,exclusive=True)
    print(json.dumps({'ok':True,'manager':manager,'history_id':args.history_id,'commit':original['commit']}))


def configure_managed_authority(args,registry,history,c,account):
    authority_manifest=getattr(args,'managed_incident_manifest',None)
    authority_digest=getattr(args,'expected_managed_incident_manifest_sha256',None)
    if (authority_manifest is None)!=(authority_digest is None):raise ValueError('bootstrap_managed_manifest_review_required')
    if authority_manifest is not None:
        if not (ROOT/'managed_incident_proof_authority.py').is_file():raise ValueError('bootstrap_managed_authority_module_missing')
        manifest_raw=read(authority_manifest,limit=1048576)
        if sha256(manifest_raw).hexdigest()!=authority_digest:raise ValueError('bootstrap_managed_manifest_digest_mismatch')
        manifest_value=c.strict_json(manifest_raw,max_bytes=1048576)
        if not isinstance(manifest_value,dict) or manifest_value.get('schema')!='managed_code_incident_manifest.v1' or manifest_value.get('enabled') is not True:
            raise ValueError('bootstrap_managed_manifest_invalid')
        authority_directory=Path('/etc/eimemory-code-automation-authority')
        directory(authority_directory,gid=account.pw_gid,mode=0o750)
        installed=authority_directory/'managed-code-incident-manifest.v1.json'
        # Changing issuer knowledge boundaries is a separate explicit review.
        # Preserve all original bytes in this migration history first.
        if installed.exists():write(history/'managed-manifest.original',read(installed),gid=0,mode=0o600,exclusive=True)
        write(installed,manifest_raw,gid=account.pw_gid)
        secret=authority_directory/'managed-incident-proof.key'
        if not secret.exists():write(secret,os.urandom(32),gid=0,mode=0o600,exclusive=True)
        elif len(read(secret))<32 or stat.S_IMODE(secret.stat().st_mode)!=0o600:raise ValueError('bootstrap_managed_key_invalid')
        receipts=STATE/'managed-incident-receipts';directory(receipts,gid=0,mode=0o700)
        if stat.S_IMODE(receipts.stat().st_mode)!=0o700:raise ValueError('bootstrap_managed_receipt_directory_invalid')
        empty=receipts/'empty-pycache';directory(empty,gid=0,mode=0o700)
        if stat.S_IMODE(empty.stat().st_mode)!=0o700 or any(empty.iterdir()):raise ValueError('bootstrap_managed_empty_pycache_invalid')
        registry['managed_incident_authority']={'enabled':True,
            'database_path':'/var/lib/eimemory/state/eimemory.sqlite','manifest_path':str(installed),
            'manifest_sha256':authority_digest,'receipt_directory':str(receipts),'key_path':str(secret)}
        spec=importlib.util.spec_from_file_location('reviewed_managed_configuration',ROOT/'managed_incident_proof_authority.py')
        read(ROOT/'managed_incident_proof_authority.py')
        authority=importlib.util.module_from_spec(spec);exec(compile(read(ROOT/'managed_incident_proof_authority.py'),str(ROOT/'managed_incident_proof_authority.py'),'exec'),authority.__dict__)
        authority.validate_configuration(registry=registry)
    return registry



def retire_legacy_user_owners(account,prior,units):
    active=[]
    # Timers first prevent a new old-owner invocation between the snapshot and
    # stopping services. Failed/inactive services are never stopped/reset.
    for unit in sorted(units,key=lambda name:(not name.endswith(('.timer','.path')),name)):
        before=prior[unit]
        if before['LoadState']!='loaded':continue
        running=before['ActiveState'] in {'active','activating','reloading','deactivating'}
        if running:active.append(unit)
        call(user_command(account,'disable',unit))
        if running:call(user_command(account,'stop',unit))
        after=dict(line.split('=',1) for line in call(user_command(account,'show',unit,
            '--property=ActiveState,Result,InvocationID,ExecMainStatus')).splitlines() if '=' in line)
        if after.get('ActiveState') not in {'inactive','failed'}:raise ValueError('bootstrap_legacy_owner_still_active')
        if before['ActiveState']=='failed':
            if after['ActiveState']!='failed' or any(before.get(key) not in {None,''} and after.get(key)!=before[key]
                    for key in ('Result','InvocationID','ExecMainStatus')):
                raise ValueError('bootstrap_legacy_failed_prefix_changed')
    return active


def install(args):
    import pwd
    account=pwd.getpwnam('darrow')
    repository=Path(args.repository_root)
    if repository.resolve(strict=True)!=repository or not repository.is_dir():raise ValueError('bootstrap_repository_coordinate_invalid')
    if account.pw_uid!=1001 or account.pw_gid!=1001 or account.pw_dir!='/home/darrow':raise ValueError('bootstrap_actual_runtime_identity_mismatch')
    if REGISTRY.exists():
        original=read(REGISTRY)
        if args.expected_registry_sha256 is None or sha256(original).hexdigest()!=args.expected_registry_sha256:
            raise ValueError('bootstrap_existing_registry_cas_required')
        active=STATE/'active-transition.json'
        if active.exists() and json.loads(read(active)).get('complete') is not True:
            raise ValueError('bootstrap_transition_still_active')
        # Replacing a running daemon's reviewed modules does not update its
        # loaded authority. A manager upgrade needs its own reviewed handoff;
        # this first-owner migration entry must not silently perform one.
        raise ValueError('bootstrap_existing_manager_upgrade_requires_reviewed_handoff')
    elif args.expected_registry_sha256 is not None:
        raise ValueError('bootstrap_expected_registry_missing')
    cache=STATE/'empty-pycache'
    if not cache.exists():directory(cache,gid=0,mode=0o555)
    if cache.is_symlink() or cache.stat().st_uid!=0 or stat.S_IMODE(cache.stat().st_mode)!=0o555 or any(cache.iterdir()):
        raise ValueError('bootstrap_manager_empty_pycache_invalid')
    migration=STATE/'migration-prior-active.json'
    if migration.exists() and json.loads(read(migration)).get('restored') is not True:
        raise ValueError('bootstrap_previous_migration_capture_unrestored')
    files,manifest=bundle(read(args.bundle),args.expected_bundle_sha256)
    commit=manifest['controller_commit']
    scope_raw=read(args.unit_scope_manifest)
    if sha256(scope_raw).hexdigest()!=args.expected_unit_scope_sha256:raise ValueError('bootstrap_unit_scope_manifest_digest_mismatch')
    scopes=json.loads(scope_raw)
    if set(scopes)!={'schema','units'} or scopes['schema']!='eimemory.runtime_unit_scopes.v1':raise ValueError('bootstrap_unit_scope_manifest_invalid')
    if CURRENT.resolve(strict=True)!=RELEASES/commit:
        raise ValueError('bootstrap_controller_must_be_current')
    for path in (ROOT,STATE,STATE/'jobs',STATE/'validations',STATE/'validation-results'):
        exact_directory(path,gid=account.pw_gid,mode=0o750)
    lock=os.open(STATE/'bootstrap.lock',os.O_WRONLY|os.O_CREAT|os.O_NOFOLLOW,0o600)
    import fcntl
    fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    history=directory(STATE/('bootstrap-'+time.strftime('%Y%m%dT%H%M%SZ')+'-'+str(os.getpid())),gid=0,mode=0o700)
    # Original root bytes are exclusive history, including a failed migration.
    for path in [REGISTRY,*[SYSTEM_UNITS/n.split('/')[-1] for n in files if n.startswith('templates/')]]:
        if path.exists():write(history/(path.name+'.original'),read(path),gid=0,mode=0o600,exclusive=True)
    if migration.exists():write(history/'migration-prior-active.original',read(migration),gid=0,mode=0o600,exclusive=True)
    before_root=root_snapshot(files)
    for name,raw in files.items():
        if name=='eimemory-runtime-manager.service':continue
        write(ROOT/name,raw,gid=account.pw_gid)
    spec=importlib.util.spec_from_file_location('reviewed_runtime_contract',ROOT/'runtime_manager_contract.py')
    c=importlib.util.module_from_spec(spec);exec(compile(read(ROOT/'runtime_manager_contract.py'),str(ROOT/'runtime_manager_contract.py'),'exec'),c.__dict__)
    if not isinstance(scopes['units'],dict) or not set(scopes['units'])<=set(c.PRODUCT_SERVICES):raise ValueError('bootstrap_unit_scope_manifest_invalid')
    for scope in scopes['units'].values():c.validate_scope(scope)
    units={}
    prior={}
    for unit in sorted(c.REGISTERED_UNITS):
        command=user_command(account,'show',unit,'--property=ActiveState,LoadState,UnitFileState,Result,InvocationID,ExecMainStatus')
        values=dict(line.split('=',1) for line in call(command).splitlines() if '=' in line)
        if not {'ActiveState','LoadState','UnitFileState'}<=set(values):raise ValueError('bootstrap_legacy_state_incomplete')
        prior[unit]=values
        units[unit]={'manager':'user' if unit in c.USER_COMPAT_UNITS else 'system',
            'installed':values['LoadState']=='loaded' if unit in c.USER_COMPAT_UNITS else 'templates/'+unit in files}
    write(history/'prior-user-state.json',c.canonical(prior),gid=0,mode=0o600,exclusive=True)
    prior_system={}
    for unit in sorted(c.REGISTERED_UNITS|{'eimemory-runtime-manager.service'}):
        state=dict(line.split('=',1) for line in call(['/usr/bin/systemctl','show',unit,'--property=LoadState,ActiveState,UnitFileState']).splitlines() if '=' in line)
        if not {'LoadState','ActiveState','UnitFileState'}<=set(state):raise ValueError('bootstrap_prior_system_state_unknown')
        prior_system[unit]=state
    active_states={'active','activating','reloading','deactivating'}
    if (prior_system['eimemory-rpc.service']['ActiveState'] in active_states
            and prior['eimemory-rpc.service']['ActiveState'] in active_states):raise ValueError('bootstrap_preexisting_dual_rpc_owner')
    user_original=owner_files(account,'snapshot',sorted(c.REGISTERED_UNITS))
    owner_original={'schema':'eimemory.runtime_owner_snapshot.v1','commit':commit,'root_files':before_root,
        'user_files':user_original,'prior_user_state':prior,'prior_system_state':prior_system}
    write(history/'owner-snapshot.json',c.canonical(owner_original),gid=0,mode=0o600,exclusive=True)
    storage_clean()
    authority_preview=configure_managed_authority(args,{'managed_incident_authority':{'enabled':False},'runtime_uid':account.pw_uid,'runtime_gid':account.pw_gid,'runtime_user':'darrow','runtime_commit':commit,'controller_commit':commit},history,c,account)
    # Stop/disable the old product owner before publishing any system owner.
    # No parallel RPC owner is ever started as part of a migration.
    active=retire_legacy_user_owners(account,prior,set(c.REGISTERED_UNITS)-set(c.USER_COMPAT_UNITS))
    write(history/'active-before.json',c.canonical(active),gid=0,mode=0o600,exclusive=True)
    for name,raw in files.items():
        if not name.startswith('templates/'):continue
        unit=name.split('/')[-1]
        if unit not in {'eimemory-code-deployment-job@.service',*(prefix+'.service' for prefix in c.VALIDATION_PREFIXES.values())} and unit not in c.REGISTERED_UNITS:raise ValueError('bootstrap_unit_not_registered')
        # Every product command must remain non-root, including ExecStartPre.
        if unit.endswith('.service'):
            text=raw.decode()
            if 'User=darrow' not in text or 'PrivateUsers=self' not in text or 'NoNewPrivileges=yes' not in text:
                raise ValueError('bootstrap_product_identity_missing')
            if re.search(r'^Exec[^=]*=[+!]',text,re.M):raise ValueError('bootstrap_product_privilege_prefix_forbidden')
        write(SYSTEM_UNITS/unit,raw,gid=account.pw_gid)
        if unit.endswith('.service'):
            policy=files['job-runtime-policy.conf' if unit=='eimemory-code-deployment-job@.service' else 'validation-runtime-policy.conf' if unit.startswith('eimemory-code-validation-') else 'runtime-policy.conf'].replace(b'@COMMIT@',commit.encode())
            # The independent installer needs candidate writes. Protect only
            # its registered controller; consumers protect the entire tree.
            if unit in {'eimemory-rpc.service','eimemory-console.service'}:
                policy=policy.replace(('/opt/eimemory/releases/'+commit+' ').encode(),b'/opt/eimemory/releases ')
            write(SYSTEM_UNITS/(unit+'.d')/'90-eimemory-runtime.conf',policy,gid=account.pw_gid)
    registry={'schema':c.REGISTRY_SCHEMA,'runtime_user':'darrow','runtime_uid':account.pw_uid,'runtime_gid':account.pw_gid,
        'controller_commit':commit,'runtime_commit':commit,'repository_root':str(repository),'units':units,
        'template_hashes':{name.split('/')[-1]:sha256(raw).hexdigest() for name,raw in files.items() if name.startswith('templates/')},
        'compatibility_hashes':json.loads(files['compatibility.json']),
        'controller_entry_digest':json.loads(files['controller-entry.json'])['sha256'],
        'validation_entry_digest':json.loads(files['controller-entry.json'])['validation_sha256'],
        'unit_scopes':scopes['units'],'managed_incident_authority':{'enabled':False}}
    registry['managed_incident_authority']=authority_preview['managed_incident_authority']
    registry['generation']=c.digest(registry);c.validate_registry(registry)
    directory(STATE/'activation-receipts',gid=account.pw_gid)
    key=STATE/'activation-signing.key'
    if not key.exists():write(key,os.urandom(32),gid=0,mode=0o600,exclusive=True)
    else:
        secret=read(key)
        if len(secret)!=32 or stat.S_IMODE(key.stat().st_mode)!=0o600:raise ValueError('bootstrap_activation_key_invalid')
    write(REGISTRY,c.canonical(registry),gid=account.pw_gid)
    write('/etc/systemd/system/eimemory-runtime-manager.service',files['eimemory-runtime-manager.service'],gid=0)
    call(['/usr/bin/systemctl','daemon-reload'])
    call(['/usr/bin/systemctl','enable','--now','eimemory-runtime-manager.service'])
    # Preserve the old active set for the first registered release transition.
    # Do not execute old automatic effects before the new source is installed.
    write(STATE/'migration-prior-active.json',c.canonical({'active_before':active,'restored':False,'history_id':history.name}),gid=account.pw_gid,exclusive=not migration.exists())
    if 'eimemory-rpc.service' in active:
        call(['/usr/bin/systemctl','start','eimemory-rpc.service'])
    write(history/'complete.json',c.canonical({'registry_generation':registry['generation'],'controller_commit':commit,'old_active_retained':active,'automatic_jobs_paused_until_release_transition':True}),gid=0,mode=0o600,exclusive=True)
    print(json.dumps({'ok':True,'registry_generation':registry['generation'],'history':str(history),'owner_snapshot_sha256':sha256(c.canonical(owner_original)).hexdigest()}))


def enable_managed(args):
    import pwd
    account=pwd.getpwnam('darrow');c=load_contract()
    old=read(REGISTRY)
    if sha256(old).hexdigest()!=args.expected_registry_sha256:raise ValueError('bootstrap_managed_registry_cas_mismatch')
    registry=c.validate_registry(c.strict_json(old))
    if CURRENT.resolve(strict=True)!=RELEASES/registry['runtime_commit']:raise ValueError('bootstrap_current_registry_mismatch')
    history=directory(STATE/('authority-'+time.strftime('%Y%m%dT%H%M%SZ')+'-'+str(os.getpid())),gid=0,mode=0o700)
    write(history/'registry.original',old,gid=0,mode=0o600,exclusive=True)
    configured=configure_managed_authority(args,dict(registry),history,c,account)
    configured['generation']=c.digest({k:v for k,v in configured.items() if k!='generation'})
    c.validate_registry(configured)
    if read(REGISTRY)!=old or CURRENT.resolve(strict=True)!=RELEASES/registry['runtime_commit']:
        raise ValueError('bootstrap_managed_authority_current_changed')
    write(REGISTRY,c.canonical(configured),gid=account.pw_gid)
    write(history/'complete.json',c.canonical({'registry_generation':configured['generation'],'manifest_sha256':args.expected_managed_incident_manifest_sha256}),gid=0,mode=0o600,exclusive=True)
    print(json.dumps({'ok':True,'registry_generation':configured['generation'],'history':str(history)}))


def stage(args):
    # Every reviewed attempt is independently recorded. An ambiguous old start
    # cannot be replayed under a fresh nonce or silently overwritten.
    if re.fullmatch('[0-9a-f]{40}',args.target_commit) is None:raise ValueError('bootstrap_full_target_required')
    if re.fullmatch('[0-9a-f]{32}',args.attempt_id) is None:raise ValueError('bootstrap_reviewed_attempt_id_required')
    c=load_contract();registry=c.validate_registry(c.strict_json(read(REGISTRY)))
    scope_path=Path(args.scope_manifest)
    directory(scope_path.parent)
    scope_raw=read(scope_path,limit=65536)
    if stat.S_IMODE(scope_path.stat().st_mode)!=0o640 or sha256(scope_raw).hexdigest()!=args.expected_scope_sha256:
        raise ValueError('bootstrap_manual_scope_review_digest_mismatch')
    scope_manifest=c.strict_json(scope_raw)
    if not isinstance(scope_manifest,dict) or set(scope_manifest)!={'schema','scope'} or scope_manifest['schema']!='eimemory.manual_release_scope.v1':
        raise ValueError('bootstrap_manual_scope_manifest_invalid')
    scope=c.validate_scope(scope_manifest['scope'])
    if CURRENT.resolve(strict=True)!=RELEASES/registry['runtime_commit']:raise ValueError('bootstrap_current_registry_mismatch')
    if args.target_commit==registry['runtime_commit']:raise ValueError('bootstrap_target_already_current')
    storage_clean()
    pair={'base_commit':registry['runtime_commit'],'candidate_commit':args.target_commit,'controller_commit':registry['controller_commit'],
        'validation_controller_commit':args.target_commit,'scope':scope}
    pointer=STATE/('manual-release-'+c.digest(pair)+'.json')
    approved_retry=None
    if pointer.exists():
        previous=c.strict_json(read(pointer))
        key=previous.get('job_key');c.hex_value(key,64,'bootstrap_prior_job_key_invalid')
        if args.prior_job_key!=key:raise ValueError('bootstrap_prior_attempt_review_required')
        record=c.strict_json(read(STATE/'jobs'/(key+'.json')))
        if record.get('manual_release') is not True or any(record['request'].get(k)!=v for k,v in pair.items()):raise ValueError('bootstrap_prior_attempt_binding_invalid')
        state=dict(line.split('=',1) for line in call(['/usr/bin/systemctl','show',c.job_unit(key),'--property=LoadState,ActiveState,Result,InvocationID']).splitlines() if '=' in line)
        if (state.get('LoadState')!='loaded' or state.get('ActiveState') not in {'inactive','failed'}
                or state.get('Result') in {None,'','success'} or not state.get('InvocationID')
                or record.get('original_invocation') not in {None,state['InvocationID']}):
            raise ValueError('bootstrap_prior_attempt_active_or_unknown')
        if previous.get('attempt_id')==args.attempt_id:raise ValueError('bootstrap_attempt_nonce_already_used')
        approved_retry=key
    elif args.prior_job_key is not None:raise ValueError('bootstrap_prior_attempt_missing')
    active=STATE/'active-transition.json'
    if active.exists():
        transition=c.strict_json(read(active))
        if transition.get('complete') is not True and transition.get('job_key')!=approved_retry:
            raise ValueError('bootstrap_another_transition_active')
    request={'schema':'eimemory.manual_release_permit.v2','operation':'deploy',**pair,
        'target_commit':args.target_commit,'reviewed_attempt_id':args.attempt_id}
    key=c.digest(request)
    c.validate_manual_permit(request,key)
    record={'job_key':key,'request':request,'registry':registry,'manual_release':True,
        'phase':'submitted','unit':c.job_unit(key),'active_before':None,'quiesced':False,'resumed':False,
        'approved_retry_of':approved_retry}
    write(STATE/'jobs'/(key+'.json'),c.canonical(record),gid=registry['runtime_gid'],exclusive=True)
    write(pointer,c.canonical({'job_key':key,'attempt_id':args.attempt_id}),gid=registry['runtime_gid'])
    call(['/usr/bin/systemctl','start','--no-block',record['unit']])
    print(json.dumps({'ok':True,'job_key':key,'unit':record['unit'],'state':'submitted','approved_retry_of':approved_retry}))


def main():
    if os.geteuid()!=0:raise SystemExit('host_root_required')
    # Even the bootstrap must be copied to a reviewed root-private location.
    directory(Path(__file__).resolve().parent)
    read(__file__)
    p=argparse.ArgumentParser();sub=p.add_subparsers(dest='operation',required=True)
    q=sub.add_parser('install');q.add_argument('--bundle',type=Path,required=True);q.add_argument('--expected-bundle-sha256',required=True);q.add_argument('--repository-root',type=Path,required=True);q.add_argument('--unit-scope-manifest',type=Path,required=True);q.add_argument('--expected-unit-scope-sha256',required=True);q.add_argument('--expected-registry-sha256');q.add_argument('--managed-incident-manifest',type=Path);q.add_argument('--expected-managed-incident-manifest-sha256')
    q=sub.add_parser('enable-managed-authority');q.add_argument('--managed-incident-manifest',type=Path,required=True);q.add_argument('--expected-managed-incident-manifest-sha256',required=True);q.add_argument('--expected-registry-sha256',required=True)
    q=sub.add_parser('stage-release');q.add_argument('--target-commit',required=True);q.add_argument('--attempt-id',required=True);q.add_argument('--prior-job-key');q.add_argument('--scope-manifest',type=Path,required=True);q.add_argument('--expected-scope-sha256',required=True)
    q=sub.add_parser('restore-owner');q.add_argument('--history-id',required=True);q.add_argument('--expected-history-sha256',required=True);q.add_argument('--expected-current-commit',required=True);q.add_argument('--expected-registry-sha256',required=True);q.add_argument('--abandon-failed-job-key')
    a=p.parse_args()
    with lifecycle_lock():
        if a.operation=='install':install(a)
        elif a.operation=='stage-release':stage(a)
        elif a.operation=='enable-managed-authority':enable_managed(a)
        else:restore_owner(a)


if __name__=='__main__':main()
