#!/usr/bin/env python3
"""Prepare a reviewable byte-pinned root-manager bundle; never installs units."""
from __future__ import annotations
import argparse
from hashlib import sha256
import json
from pathlib import Path, PurePosixPath
import re
import sys
import zipfile

SOURCE_ROOT=Path(__file__).resolve().parents[1]


def build(root,commit, *, account=None, _user_profile=False):
    if account is None:
        import pwd
        account = pwd.getpwuid(1001)
    import os
    if ((not _user_profile and (account.pw_uid != 1001 or account.pw_gid != 1001))
            or (_user_profile and (account.pw_uid<=0 or account.pw_uid!=os.geteuid() or account.pw_gid!=os.getegid()))):
        raise ValueError("manager_bundle_runtime_identity_invalid")
    if (re.fullmatch(r"[a-z_][a-z0-9_-]{0,31}", account.pw_name) is None
            or re.fullmatch(r"/[A-Za-z0-9_./-]+", account.pw_dir) is None
            or account.pw_dir.startswith("//")
            or PurePosixPath(account.pw_dir).as_posix() != account.pw_dir
            or '..' in PurePosixPath(account.pw_dir).parts):
        raise ValueError("manager_bundle_runtime_account_invalid")
    if re.fullmatch('[0-9a-f]{40}',commit) is None:raise ValueError('full_controller_commit_required')
    # Importing this pure contract here is as the runtime/operator user only.
    sys.path.insert(0,str(root))
    from eimemory.contracts.runtime_manager import PRODUCT_SERVICES,PRODUCT_TIMERS,PRODUCT_PATHS,COMPATIBILITY_PATHS,VALIDATION_PREFIXES,VALIDATION_RUNTIME_SECONDS
    files={}
    entry=root/'deploy/run_code_deployment_job.py'
    validation_entry=root/'deploy/run_code_deployment_validation.py'
    files['controller-entry.json']=json.dumps({'entry':'deploy/run_code_deployment_job.py','sha256':sha256(entry.read_bytes()).hexdigest() if entry.is_file() else None,
        'validation_entry':'deploy/run_code_deployment_validation.py','validation_sha256':sha256(validation_entry.read_bytes()).hexdigest() if validation_entry.is_file() else None},sort_keys=True,separators=(',',':')).encode()
    files['compatibility.json']=json.dumps({name:sha256((root/name).read_bytes()).hexdigest() for name in COMPATIBILITY_PATHS},sort_keys=True,separators=(',',':')).encode()
    files['runtime_manager_contract.py']=(root/'eimemory/contracts/runtime_manager.py').read_bytes()
    for name in ('runtime_manager_bridge.py','runtime_manager_launch.py','runtime_manager_settings.py','runtime_manager_user_policy.py','runtime_manager_owner_files.py','storage_release_transaction.py'):
        files[name]=(root/'deploy'/name).read_bytes()
    authority=root/'eimemory/core/managed_incident_proof_authority.py'
    if authority.is_file():
        files['managed_incident_proof_authority.py']=authority.read_bytes()
    for unit in (*PRODUCT_SERVICES,*PRODUCT_TIMERS,*PRODUCT_PATHS):
        source=root/'deploy/systemd'/unit
        if not source.exists():continue
        text=source.read_text(encoding='utf-8')
        text=text.replace('WantedBy=default.target','WantedBy=multi-user.target').replace('%h',account.pw_dir)
        text=re.sub(r'^Environment=USER=%u$', 'Environment=USER='+account.pw_name, text, flags=re.M)
        text=text.replace(account.pw_dir+'/.config/systemd/user/eimemory-l5-effect-review.sh','/opt/eimemory/current/deploy/systemd/eimemory-l5-effect-review.sh')
        if unit.endswith('.service'):
            # Explicit argv is necessary: -I ignores PYTHONPYCACHEPREFIX.
            cache=' -I -B -X pycache_prefix=/var/lib/eimemory-manager/empty-pycache'
            text=text.replace('/.venv/bin/eimemory ', '/.venv/bin/python'+cache+' -m eimemory.cli.main ')
            lines=[]
            for line in text.splitlines():
                if line.startswith('Exec') and '/.venv/bin/python ' in line and 'pycache_prefix=' not in line:
                    line=line.replace('/.venv/bin/python ', '/.venv/bin/python'+cache+' ')
                if line.startswith('Exec'):
                    line=re.sub(r'(/opt/eimemory/current/\.venv/bin/python -I -B -X pycache_prefix=/var/lib/eimemory-manager/empty-pycache) -m (eimemory(?:\.[A-Za-z_][A-Za-z_0-9]*)+)',
                        r'\1 /usr/libexec/eimemory-manager/runtime_manager_launch.py --product-module \2',line)
                if line.startswith('Environment=PYTHONPYCACHEPREFIX='):
                    line='Environment=PYTHONPYCACHEPREFIX=/var/lib/eimemory-manager/empty-pycache'
                lines.append(line)
            text='\n'.join(lines)+'\n'
            text+='\n[Service]\nUser=@RUNTIME_USER@\nGroup=@RUNTIME_GROUP@\nEnvironment=HOME=@RUNTIME_HOME@\nEnvironment=XDG_RUNTIME_DIR=/run/user/1001\nEnvironment=DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1001/bus\nPrivateUsers=self\nNoNewPrivileges=yes\nCapabilityBoundingSet=\nAmbientCapabilities=\n'
            if unit=='eimemory-l5-fused-closure.service':
                text+='KillMode=control-group\nSendSIGKILL=yes\n'
                hook='/usr/bin/python3'+cache+' /usr/libexec/eimemory-manager/runtime_manager_launch.py --recover-fused'
                text+='ExecStartPre='+hook+'\nExecStopPost='+hook+'\n'
        files['templates/'+unit]=text.encode()
    files['templates/eimemory-code-deployment-job@.service']=b"""[Unit]
Description=eimemory registered independent deployment job %i
After=network-online.target eimemory-runtime-manager.service
Requires=eimemory-runtime-manager.service
[Service]
Type=exec
RemainAfterExit=yes
User=@RUNTIME_USER@
Group=@RUNTIME_GROUP@
Environment=HOME=@RUNTIME_HOME@
Environment=XDG_RUNTIME_DIR=/run/user/1001
Environment=DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1001/bus
Environment=PYTHONDONTWRITEBYTECODE=1
Environment=EIMEMORY_ROOT=/var/lib/eimemory
Environment=EIMEMORY_CONFIG_DIR=/etc/eimemory
EnvironmentFile=-/etc/eimemory/governance.env
EnvironmentFile=-/etc/eimemory/identity.env
EnvironmentFile=-/etc/eimemory/runtime-authority.env
EnvironmentFile=-/etc/eimemory/postgres.env
EnvironmentFile=-/etc/eimemory/embedding.env
EnvironmentFile=-/etc/eimemory/recall.env
PrivateUsers=self
NoNewPrivileges=yes
CapabilityBoundingSet=
AmbientCapabilities=
PrivateTmp=yes
KillMode=control-group
TimeoutStartSec=7200
RuntimeMaxSec=14400
TimeoutStopSec=90
ExecStart=/usr/bin/python3 -I -B -X pycache_prefix=/var/lib/eimemory-manager/empty-pycache /usr/libexec/eimemory-manager/runtime_manager_launch.py %i
ReadOnlyPaths=/usr/libexec/eimemory-manager /etc/eimemory-code-automation-authority /etc/eimemory-controlled-production-acceptance
"""
    for operation,prefix in VALIDATION_PREFIXES.items():
        files['templates/'+prefix+'.service']=files['templates/eimemory-code-deployment-job@.service'].replace(
            b'Description=eimemory registered independent deployment job %i',
            ('Description=eimemory fixed readonly deployment validation '+operation+' %i').encode()).replace(
            b'runtime_manager_launch.py %i',('runtime_manager_launch.py --validation %i '+operation).encode()).replace(
            b'ReadOnlyPaths=/usr/libexec/eimemory-manager ',b'ReadOnlyPaths=/opt/eimemory/releases /usr/libexec/eimemory-manager ').replace(
            b'RuntimeMaxSec=14400',('RuntimeMaxSec='+str(VALIDATION_RUNTIME_SECONDS[operation])).encode())
    files['job-runtime-policy.conf']=b"""[Service]
User=@RUNTIME_USER@
Group=@RUNTIME_GROUP@
PrivateUsers=self
NoNewPrivileges=yes
CapabilityBoundingSet=
AmbientCapabilities=
ReadOnlyPaths=/opt/eimemory/releases/@COMMIT@ /etc/eimemory-code-automation-authority /etc/eimemory-controlled-production-acceptance
ExecStartPre=/usr/bin/python3 -I -B -X pycache_prefix=/var/lib/eimemory-manager/empty-pycache /usr/libexec/eimemory-manager/runtime_manager_launch.py --preflight %i
"""
    files['validation-runtime-policy.conf']=b"""[Service]
User=@RUNTIME_USER@
Group=@RUNTIME_GROUP@
PrivateUsers=self
NoNewPrivileges=yes
CapabilityBoundingSet=
AmbientCapabilities=
ReadOnlyPaths=/opt/eimemory/releases /usr/libexec/eimemory-manager /etc/eimemory-code-automation-authority /etc/eimemory-controlled-production-acceptance
"""
    files['runtime-policy.conf']=b"""# Root-reviewed runtime authority; atomic directory replacements remain visible.
[Service]
User=@RUNTIME_USER@
Group=@RUNTIME_GROUP@
Environment=HOME=@RUNTIME_HOME@
Environment=EIMEMORY_RUNTIME_COMMIT=@COMMIT@
Environment=EIMEMORY_RUNTIME_RELEASE_DIR=/opt/eimemory/current
Environment=EIMEMORY_RPC_URL=http://127.0.0.1:8091/
Environment=PYTHONDONTWRITEBYTECODE=1
EnvironmentFile=-/etc/eimemory/governance.env
EnvironmentFile=-/etc/eimemory/recall.env
EnvironmentFile=-/etc/eimemory/identity.env
EnvironmentFile=-/etc/eimemory/runtime-authority.env
EnvironmentFile=-/etc/eimemory/evidence-receipt.env
PrivateUsers=self
NoNewPrivileges=yes
CapabilityBoundingSet=
AmbientCapabilities=
ReadOnlyPaths=/opt/eimemory/releases/@COMMIT@ /etc/eimemory-code-automation-authority /etc/eimemory-controlled-production-acceptance
ExecCondition=/usr/bin/python3 -I -B -X pycache_prefix=/var/lib/eimemory-manager/empty-pycache /usr/libexec/eimemory-manager/storage_release_transaction.py guard --marker /var/lib/eimemory/state/storage-release-transaction.json
ExecStartPre=/opt/eimemory/releases/@COMMIT@/.venv/bin/python -I -B -X pycache_prefix=/var/lib/eimemory-manager/empty-pycache -c "import os,sys; sys.exit(0 if os.statvfs(sys.argv[1]).f_flag & os.ST_RDONLY else 'immutable_release_mount_not_readonly')" /opt/eimemory/releases/@COMMIT@
"""
    # The root daemon has no product Python imports. It is the sole privileged
    # unit; every product Exec* is subject to User=@RUNTIME_USER@ without +/! bypasses.
    files['eimemory-runtime-manager.service']=b"""[Unit]
Description=eimemory fixed-action host runtime manager
After=network.target
[Service]
Type=simple
User=root
Group=root
ExecStart=/usr/bin/python3 -I -B -X pycache_prefix=/var/lib/eimemory-manager/empty-pycache /usr/libexec/eimemory-manager/runtime_manager_bridge.py
RuntimeDirectory=eimemory-manager
RuntimeDirectoryMode=0750
UMask=0027
AmbientCapabilities=CAP_SETUID CAP_SETGID
NoNewPrivileges=yes
PrivateTmp=yes
ProtectHome=read-only
ReadWritePaths=@RUNTIME_HOME@/.config/systemd/user
RestrictAddressFamilies=AF_UNIX
Restart=on-failure
[Install]
WantedBy=multi-user.target
"""
    for name in tuple(files):
        if name.endswith(('.service', '.conf')):
            files[name] = files[name].replace(b'@RUNTIME_USER@', account.pw_name.encode()).replace(
                b'@RUNTIME_HOME@', account.pw_dir.encode()).replace(
                b'@RUNTIME_GROUP@', str(account.pw_gid).encode())
    manifest={'schema':'eimemory.runtime_manager_bundle.v1','controller_commit':commit,
        'files':{name:sha256(raw).hexdigest() for name,raw in sorted(files.items())}}
    files['manifest.json']=json.dumps(manifest,sort_keys=True,separators=(',',':')).encode()
    return files


def build_user(root, commit, *, account=None):
    """Render a separate user profile; never soften the legacy profile in place."""
    import os
    if account is None:
        import pwd
        account=pwd.getpwuid(os.geteuid())
    if account.pw_uid<=0 or account.pw_uid!=os.geteuid():
        raise ValueError('manager_user_bundle_nonroot_identity_required')
    files=build(root,commit,account=account,_user_profile=True)
    home=Path(account.pw_dir)
    import stat
    home_seal=home
    for path in (home,*home.parents):
        info=path.lstat()
        if info.st_uid!=account.pw_uid:
            if path==home:raise ValueError('manager_user_home_owner_invalid')
            break
        if not stat.S_ISDIR(info.st_mode) or info.st_mode&0o022:
            raise ValueError('manager_user_home_ancestor_untrusted')
        home_seal=path
    authority=home/'.config/eimemory/control-plane'
    state=home/'.local/state/eimemory/control-plane'
    code=home/'.local/lib/eimemory/control-plane'
    runtime=Path('/run/user')/str(account.pw_uid)
    files['user_control_plane.py']=(root/'eimemory/contracts/user_control_plane.py').read_bytes()
    rewrites=(('/var/lib/eimemory-manager/empty-pycache',str(code/'empty-pycache')),
        ('/run/user/1001',str(runtime)),
        ('/usr/libexec/eimemory-manager',str(code)),
        ('/etc/eimemory-controlled-production-acceptance',str(authority/'controlled-production-acceptance')),
        ('/etc/eimemory-code-automation-authority',str(authority)),
        ('/var/lib/eimemory-manager',str(state)))
    isolation=('\n[Unit]\nWants=eimemory-runtime-manager.service\n'
        'After=eimemory-runtime-manager.service\n[Service]\nEnvironment=EIMEMORY_CONTROL_PLANE=user\n'
        'PrivateUsers=yes\nPrivatePIDs=yes\nNoNewPrivileges=yes\nUMask=0027\n'
        'CapabilityBoundingSet=\nAmbientCapabilities=\n'
        f'ReadOnlyPaths={home_seal} {code} {authority} {state} {home}/.config/systemd {home}/.local/share/systemd {runtime}/systemd {runtime}/eimemory-manager\n'
        f'ReadWritePaths=-{home}/.hermes -{home}/.cache -{home}/.codex -{home}/.openclaw\n'
        f'InaccessiblePaths={authority}/managed-incident-proof.key {state}/activation-signing.key {state}/managed-incident-receipts {runtime}/bus {runtime}/systemd/private\n')
    for name in tuple(files):
        if not name.endswith(('.service','.conf','.timer','.path')):
            continue
        text=files[name].decode()
        for old,new in rewrites:
            text=text.replace(old,new)
        lines=[]
        for line in text.splitlines():
            if line.startswith(('User=','Group=','AmbientCapabilities=','RuntimeDirectory=',
                                'RuntimeDirectoryMode=','ProtectHome=','ReadWritePaths=')):
                continue
            line=line.replace('WantedBy=multi-user.target','WantedBy=default.target')
            line=line.replace('PrivateUsers=self','PrivateUsers=yes')
            lines.append(line)
        text='\n'.join(lines)+'\n'
        if name=='eimemory-runtime-manager.service':
            # The controller alone sees host native PIDs and the user bus.
            text+='\n[Service]\nEnvironment=EIMEMORY_CONTROL_PLANE=user\nCapabilityBoundingSet=\nAmbientCapabilities=\n'
            text+='RuntimeDirectory=eimemory-manager\nRuntimeDirectoryMode=0700\nRuntimeDirectoryPreserve=yes\n'
            text+='Environment=XDG_RUNTIME_DIR='+str(runtime)+'\n'
            text+='Environment=DBUS_SESSION_BUS_ADDRESS=unix:path='+str(runtime/'bus')+'\n'
        elif name.endswith(('.service','.conf')):
            text+=isolation
        files[name]=text.encode()
    manifest={'schema':'eimemory.user_runtime_manager_bundle.v1','controller_commit':commit,
        'runtime_uid':account.pw_uid,'runtime_gid':account.pw_gid,
        'files':{name:sha256(raw).hexdigest() for name,raw in sorted(files.items()) if name!='manifest.json'}}
    files['manifest.json']=json.dumps(manifest,sort_keys=True,separators=(',',':')).encode()
    return files


def main():
    import os
    if hasattr(os,'geteuid') and os.geteuid()==0:
        raise ValueError('manager_bundle_build_requires_unprivileged_operator')
    p=argparse.ArgumentParser();p.add_argument('--controller-commit',required=True);p.add_argument('--output',type=Path,required=True)
    p.add_argument('--user',action='store_true')
    a=p.parse_args()
    if a.user:files=build_user(SOURCE_ROOT,a.controller_commit)
    else:files=build(SOURCE_ROOT,a.controller_commit)
    with zipfile.ZipFile(a.output,'x',compression=zipfile.ZIP_DEFLATED) as archive:
        for name,raw in sorted(files.items()):archive.writestr(name,raw)
    print(json.dumps({'bundle':str(a.output),'sha256':sha256(a.output.read_bytes()).hexdigest(),'files':len(files)}))


if __name__=='__main__':main()
