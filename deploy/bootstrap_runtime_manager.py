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
import zipfile

ROOT=Path('/usr/libexec/eimemory-manager')
STATE=Path('/var/lib/eimemory-manager')
REGISTRY=Path('/etc/eimemory/runtime-manager.v1.json')


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


def install(args):
    import pwd
    account=pwd.getpwnam('darrow')
    repository=Path(args.repository_root)
    if repository.resolve(strict=True)!=repository or not repository.is_dir():raise ValueError('bootstrap_repository_coordinate_invalid')
    if account.pw_uid!=1001 or account.pw_gid!=1001 or account.pw_dir!='/home/darrow':raise ValueError('bootstrap_actual_runtime_identity_mismatch')
    files,manifest=bundle(read(args.bundle),args.expected_bundle_sha256)
    commit=manifest['controller_commit']
    if Path('/opt/eimemory/current').resolve(strict=True)!=Path('/opt/eimemory/releases')/commit:
        raise ValueError('bootstrap_controller_must_be_current')
    directory(ROOT,gid=account.pw_gid);directory(STATE,gid=account.pw_gid);directory(STATE/'jobs',gid=account.pw_gid)
    lock=os.open(STATE/'bootstrap.lock',os.O_WRONLY|os.O_CREAT|os.O_NOFOLLOW,0o600)
    import fcntl
    fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    history=directory(STATE/('bootstrap-'+time.strftime('%Y%m%dT%H%M%SZ')+'-'+str(os.getpid())),gid=0,mode=0o700)
    # Original root bytes are exclusive history, including a failed migration.
    for path in [REGISTRY,*[Path('/etc/systemd/system')/n.split('/')[-1] for n in files if n.startswith('templates/')]]:
        if path.exists():write(history/(path.name+'.original'),read(path),gid=0,mode=0o600,exclusive=True)
    for name,raw in files.items():
        if name=='eimemory-runtime-manager.service':continue
        write(ROOT/name,raw,gid=account.pw_gid)
    spec=importlib.util.spec_from_file_location('reviewed_runtime_contract',ROOT/'runtime_manager_contract.py')
    c=importlib.util.module_from_spec(spec);spec.loader.exec_module(c)
    units={}
    prior={}
    for unit in sorted(c.REGISTERED_UNITS):
        command=user_command(account,'show',unit,'--property=ActiveState,LoadState,UnitFileState')
        values=dict(line.split('=',1) for line in call(command).splitlines() if '=' in line)
        if not {'ActiveState','LoadState','UnitFileState'}<=set(values):raise ValueError('bootstrap_legacy_state_incomplete')
        prior[unit]=values
        units[unit]={'manager':'user' if unit in c.USER_COMPAT_UNITS else 'system',
            'installed':values['LoadState']=='loaded' if unit in c.USER_COMPAT_UNITS else 'templates/'+unit in files}
    write(history/'prior-user-state.json',c.canonical(prior),gid=0,mode=0o600,exclusive=True)
    # Stop/disable the old product owner before publishing any system owner.
    # No parallel RPC owner is ever started as part of a migration.
    active=[]
    for unit in sorted(set(c.REGISTERED_UNITS)-set(c.USER_COMPAT_UNITS)):
        if prior[unit]['LoadState']=='loaded':
            if prior[unit]['ActiveState'] in {'active','activating','reloading','deactivating'}:active.append(unit)
            call(user_command(account,'disable','--now',unit))
            state=dict(line.split('=',1) for line in call(user_command(account,'show',unit,'--property=ActiveState')).splitlines() if '=' in line)
            if state.get('ActiveState') not in {'inactive','failed'}:raise ValueError('bootstrap_legacy_owner_still_active')
    write(history/'active-before.json',c.canonical(active),gid=0,mode=0o600,exclusive=True)
    for name,raw in files.items():
        if not name.startswith('templates/'):continue
        unit=name.split('/')[-1]
        if unit!='eimemory-code-deployment-job@.service' and unit not in c.REGISTERED_UNITS:raise ValueError('bootstrap_unit_not_registered')
        # Every product command must remain non-root, including ExecStartPre.
        if unit.endswith('.service'):
            text=raw.decode()
            if 'User=darrow' not in text or 'PrivateUsers=self' not in text or 'NoNewPrivileges=yes' not in text:
                raise ValueError('bootstrap_product_identity_missing')
            if re.search(r'^Exec[^=]*=[+!]',text,re.M):raise ValueError('bootstrap_product_privilege_prefix_forbidden')
        write(Path('/etc/systemd/system')/unit,raw,gid=account.pw_gid)
        if unit.endswith('.service'):
            policy=files['job-runtime-policy.conf' if unit=='eimemory-code-deployment-job@.service' else 'runtime-policy.conf'].replace(b'@COMMIT@',commit.encode())
            # The independent installer needs candidate writes. Protect only
            # its registered controller; consumers protect the entire tree.
            if unit in {'eimemory-rpc.service','eimemory-console.service'}:
                policy=policy.replace(('/opt/eimemory/releases/'+commit+' ').encode(),b'/opt/eimemory/releases ')
            write(Path('/etc/systemd/system')/(unit+'.d')/'90-eimemory-runtime.conf',policy,gid=account.pw_gid)
    registry={'schema':c.REGISTRY_SCHEMA,'runtime_user':'darrow','runtime_uid':account.pw_uid,'runtime_gid':account.pw_gid,
        'controller_commit':commit,'runtime_commit':commit,'repository_root':str(repository),'units':units,
        'template_hashes':{name.split('/')[-1]:sha256(raw).hexdigest() for name,raw in files.items() if name.startswith('templates/')},
        'compatibility_hashes':json.loads(files['compatibility.json'])}
    registry['generation']=c.digest(registry);c.validate_registry(registry)
    write(REGISTRY,c.canonical(registry),gid=account.pw_gid)
    write('/etc/systemd/system/eimemory-runtime-manager.service',files['eimemory-runtime-manager.service'],gid=0)
    call(['/usr/bin/systemctl','daemon-reload'])
    call(['/usr/bin/systemctl','enable','--now','eimemory-runtime-manager.service'])
    # Preserve the old active set for the first registered release transition.
    # Do not execute old automatic effects before the new source is installed.
    write(STATE/'migration-prior-active.json',c.canonical({'active_before':active,'restored':False}),gid=account.pw_gid,exclusive=True)
    if 'eimemory-rpc.service' in active:
        call(['/usr/bin/systemctl','start','eimemory-rpc.service'])
    write(history/'complete.json',c.canonical({'registry_generation':registry['generation'],'controller_commit':commit,'old_active_retained':active,'automatic_jobs_paused_until_release_transition':True}),gid=0,mode=0o600,exclusive=True)
    print(json.dumps({'ok':True,'registry_generation':registry['generation'],'history':str(history)}))


def stage(args):
    # This root-only explicit release permit is a reviewed manual deployment,
    # separate from automatic code-evolution authorization/transaction facts.
    if re.fullmatch('[0-9a-f]{40}',args.target_commit) is None:raise ValueError('bootstrap_full_target_required')
    spec=importlib.util.spec_from_file_location('reviewed_runtime_contract',ROOT/'runtime_manager_contract.py')
    read(ROOT/'runtime_manager_contract.py');c=importlib.util.module_from_spec(spec);spec.loader.exec_module(c)
    registry=c.validate_registry(c.strict_json(read(REGISTRY)))
    if Path('/opt/eimemory/current').resolve(strict=True)!=Path('/opt/eimemory/releases')/registry['runtime_commit']:
        raise ValueError('bootstrap_current_registry_mismatch')
    request={'schema':'eimemory.manual_release_permit.v1','operation':'deploy','controller_commit':registry['controller_commit'],
        'base_commit':registry['runtime_commit'],'candidate_commit':args.target_commit,'target_commit':args.target_commit}
    key=c.digest(request)
    record={'job_key':key,'request':request,'registry':registry,'manual_release':True,
        'phase':'submitted','unit':c.job_unit(key),'active_before':None,'quiesced':False,'resumed':False}
    write(STATE/'jobs'/(key+'.json'),c.canonical(record),gid=registry['runtime_gid'],exclusive=True)
    call(['/usr/bin/systemctl','start','--no-block',record['unit']])
    print(json.dumps({'ok':True,'job_key':key,'unit':record['unit'],'state':'submitted'}))


def main():
    if os.geteuid()!=0:raise SystemExit('host_root_required')
    # Even the bootstrap must be copied to a reviewed root-private location.
    directory(Path(__file__).resolve().parent)
    read(__file__)
    p=argparse.ArgumentParser();sub=p.add_subparsers(dest='operation',required=True)
    q=sub.add_parser('install');q.add_argument('--bundle',type=Path,required=True);q.add_argument('--expected-bundle-sha256',required=True);q.add_argument('--repository-root',type=Path,required=True)
    q=sub.add_parser('stage-release');q.add_argument('--target-commit',required=True)
    a=p.parse_args();install(a) if a.operation=='install' else stage(a)


if __name__=='__main__':main()
