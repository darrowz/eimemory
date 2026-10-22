#!/usr/bin/python3
"""Fixed root-reviewed compatibility writer, executed as runtime UID only."""
from __future__ import annotations
import os
import json
from hashlib import sha256
from pathlib import Path
import re
import stat
import subprocess
import sys
import importlib.util

USER_MODE = os.environ.get('EIMEMORY_CONTROL_PLANE', '') == 'user'
CONTROL_UID = os.geteuid() if USER_MODE else 0
if USER_MODE:
    import pwd
    home=Path(pwd.getpwuid(os.geteuid()).pw_dir)
    helper=home/'.local/lib/eimemory/control-plane/user_control_plane.py'
    info=helper.lstat()
    if helper.is_symlink() or info.st_uid!=CONTROL_UID or info.st_mode&0o022:
        raise ValueError('manager_user_helper_untrusted')
    specification=importlib.util.spec_from_file_location('eimemory_user_control_plane',helper)
    user_control=importlib.util.module_from_spec(specification)
    exec(compile(helper.read_bytes(),str(helper),'exec'),user_control.__dict__)
    USER_COORDINATES=user_control.coordinates()



def read_root(path):
    if USER_MODE:return json.loads(user_control.read_file(path))
    source=Path('/usr/libexec/eimemory-manager/runtime_manager_launch.py')
    for parent in (source.parent,*source.parent.parents):
        meta=parent.lstat()
        if not stat.S_ISDIR(meta.st_mode) or meta.st_uid!=0 or stat.S_IMODE(meta.st_mode)&0o022:
            raise ValueError('manager_user_policy_helper_parent_untrusted')
    meta=source.lstat()
    if not stat.S_ISREG(meta.st_mode) or meta.st_uid!=0 or meta.st_nlink!=1 or stat.S_IMODE(meta.st_mode)!=0o640:
        raise ValueError('manager_user_policy_helper_untrusted')
    spec=importlib.util.spec_from_file_location('root_reviewed_manager_launch',source)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    return module.read_root(path)


def apply(commit):
    if re.fullmatch('[0-9a-f]{40}',commit) is None:raise ValueError('manager_user_policy_commit_invalid')
    registry=read_root(USER_COORDINATES['registry'] if USER_MODE else '/etc/eimemory-code-automation-authority/runtime-manager.v1.json')
    uid=registry['runtime_uid']
    if os.getuid()!=uid or os.geteuid()!=uid or uid==0:raise ValueError('manager_user_policy_runtime_uid_required')
    root=USER_COORDINATES['code'] if USER_MODE else Path('/usr/libexec/eimemory-manager')
    fd=os.open(root/'runtime-policy.conf',os.O_RDONLY|os.O_NOFOLLOW|os.O_CLOEXEC)
    try:
        st=os.fstat(fd)
        if st.st_uid!=CONTROL_UID or st.st_nlink!=1 or not stat.S_ISREG(st.st_mode) or stat.S_IMODE(st.st_mode)!=0o640:
            raise ValueError('manager_user_policy_root_template_invalid')
        raw=os.read(fd,65537)
        if len(raw)>65536:raise ValueError('manager_user_policy_template_limit')
    finally:os.close(fd)
    import pwd
    account = pwd.getpwuid(uid)
    if account.pw_name != registry['runtime_user'] or account.pw_gid != registry['runtime_gid']:
        raise ValueError('manager_user_policy_runtime_account_mismatch')
    raw=raw.replace(b'@COMMIT@',commit.encode()).replace(('User='+account.pw_name+'\n').encode(),b'').replace(('Group='+str(account.pw_gid)+'\n').encode(),b'').replace(b'PrivateUsers=self',b'PrivateUsers=yes')
    raw=raw.replace(('/opt/eimemory/releases/'+commit+' ').encode(),b'/opt/eimemory/releases ')
    if USER_MODE:
        for name in ('.hermes','.cache','.codex','.openclaw'):
            product_home=Path(account.pw_dir)/name
            st=product_home.lstat()
            if not stat.S_ISDIR(st.st_mode) or st.st_uid!=uid or st.st_mode&0o022:
                raise ValueError('manager_user_policy_product_home_untrusted')
    home=Path(account.pw_dir)/'.config/systemd/user'
    for parent in (home,home.parent,home.parent.parent,Path(account.pw_dir)):
        st=parent.lstat()
        if not stat.S_ISDIR(st.st_mode) or st.st_uid!=uid or stat.S_IMODE(st.st_mode)&0o022:
            raise ValueError('manager_user_policy_directory_untrusted')
    for unit,item in registry['units'].items():
        if item['manager']!='user' or not item['installed'] or not unit.endswith('.service'):continue
        directory=home/(unit+'.d')
        if not directory.exists():directory.mkdir(mode=0o700)
        st=directory.lstat()
        if not stat.S_ISDIR(st.st_mode) or st.st_uid!=uid or stat.S_IMODE(st.st_mode)&0o022:
            raise ValueError('manager_user_policy_dropin_untrusted')
        unit_raw=raw
        if USER_MODE:
            if unit=='hermes-gateway.service' or unit.startswith('hermes-gateway-'):
                policies={'93-eimemory-handoff.conf':'hermes-handoff-permissions.conf',
                          '94-eimemory-hermes-profile-boundary.conf':'hermes-profile-boundary.conf'}
                if unit=='hermes-gateway.service':
                    policies['91-eimemory-hermes.conf']='hermes-gateway-eimemory.conf'
                for destination,source in policies.items():
                    content=user_control.read_file(root/'hermes-policy'/source)
                    content=content.replace(b'@EIMEMORY_HERMES_BOUNDARY_PYTHON@',
                        str(Path(account.pw_dir)/'.hermes/hermes-agent/venv/bin/python').encode())
                    content=content.replace(b'@EIMEMORY_HERMES_BOUNDARY_HELPER@',
                        str(root/'ensure_hermes_profile_boundary.py').encode())
                    content=content.replace(b'@EIMEMORY_HERMES_BOUNDARY_HOST_ROOT@',
                        str(Path(account.pw_dir)/'.hermes/hermes-agent').encode())
                    write_user_metadata(directory/destination,content,uid,0o644)
                if unit=='hermes-gateway.service':
                    write_user_metadata(home/'hermes-gateway-eimemory.sh',
                        user_control.read_file(root/'hermes-policy/hermes-gateway-eimemory.sh'),uid,0o755)
            template=root/'templates'/unit
            if template.is_file():
                original=user_control.read_file(template)
                if sha256(original).hexdigest()!=registry['template_hashes'].get(unit):
                    raise ValueError('manager_user_policy_template_digest_mismatch')
                resets='\n[Service]\n'+''.join(name+'=\n' for name in
                    ('ExecStart','ExecStartPre','ExecStartPost','ExecStop','ExecStopPost','ExecCondition'))
                commands='\n'.join(line for line in original.decode().replace('\\\n',' ').splitlines()
                                   if line.startswith(('ExecStart=','ExecStartPre=','ExecStartPost=',
                                                       'ExecStop=','ExecStopPost=','ExecCondition=')))
                unit_raw=resets.encode()+commands.encode()+b'\n'+raw
            unit_raw+=('EnvironmentFile='+str(USER_COORDINATES['authority']/'user-runtime.env')+'\n').encode()
        target=directory/('zzzzzz-eimemory-user-control-plane.conf' if USER_MODE else 'zzzz-eimemory-host-manager.conf')
        temporary=directory/('.eimemory-host-manager-'+str(os.getpid()))
        fd=os.open(temporary,os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW|os.O_WRONLY,0o600)
        try:
            os.write(fd,unit_raw);os.fsync(fd)
            if target.exists():
                old=target.lstat()
                if not stat.S_ISREG(old.st_mode) or old.st_uid!=uid or old.st_nlink!=1:
                    raise ValueError('manager_user_policy_target_untrusted')
            os.replace(temporary,target)
            d=os.open(directory,os.O_RDONLY|os.O_NOFOLLOW|os.O_DIRECTORY)
            try:os.fsync(d)
            finally:os.close(d)
        finally:
            os.close(fd)
            if temporary.exists():temporary.unlink()
    subprocess.run(['/usr/bin/systemctl','--user','daemon-reload'],check=True,
        env={'PATH':'/usr/bin:/bin','XDG_RUNTIME_DIR':'/run/user/'+str(uid),
            'DBUS_SESSION_BUS_ADDRESS':'unix:path=/run/user/'+str(uid)+'/bus'})


def write_user_metadata(target, raw, uid, mode):
    """Publish reviewed controller bytes without exposing writable native units."""
    if os.path.lexists(target):
        info=target.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_uid!=uid or info.st_nlink!=1:
            raise ValueError('manager_user_policy_metadata_untrusted')
    temporary=target.with_name('.eimemory-metadata-'+str(os.getpid()))
    fd=os.open(temporary,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,mode)
    try:
        os.fchmod(fd,mode)
        with os.fdopen(fd,'wb',closefd=False) as output:
            output.write(raw);output.flush();os.fsync(fd)
        os.replace(temporary,target)
        parent=os.open(target.parent,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
        try:os.fsync(parent)
        finally:os.close(parent)
    finally:
        os.close(fd)
        if temporary.exists():temporary.unlink()


if __name__=='__main__':
    if len(sys.argv)!=2:raise SystemExit('exact_commit_required')
    apply(sys.argv[1])
