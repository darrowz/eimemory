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


def is_gateway_unit(unit):
    """External business gateways; OpenClaw loop jobs are eimemory sidecars."""
    return isinstance(unit,str) and (unit in {'hermes-gateway.service','openclaw-gateway.service'}
        or re.fullmatch(r'hermes-gateway-[A-Za-z0-9_][A-Za-z0-9_.-]{0,63}\.service',unit) is not None
        or re.fullmatch(r'[A-Za-z0-9_][A-Za-z0-9_.@-]{0,63}-gateway\.service',unit) is not None)


def render_gateway_integration(unit,commit,*,authority=None):
    """Memory connection metadata only; native gateway execution stays owned."""
    if not is_gateway_unit(unit) or re.fullmatch('[0-9a-f]{40}',commit or '') is None:
        raise ValueError('manager_gateway_integration_identity_invalid')
    lines=['# eimemory gateway memory integration only; native process policy is unchanged.','[Service]',
        'Environment=EIMEMORY_ROOT=/var/lib/eimemory','Environment=EIMEMORY_CONFIG_DIR=/etc/eimemory',
        'Environment=EIMEMORY_RUNTIME_COMMIT='+commit,
        'Environment=EIMEMORY_RUNTIME_RELEASE_DIR=/opt/eimemory/current',
        'Environment=EIMEMORY_RPC_URL=http://127.0.0.1:8091/',
        'Environment=EIMEMORY_RPC_ENV_FILE=/etc/eimemory/rpc.env',
        'EnvironmentFile=-/etc/eimemory/rpc.env','EnvironmentFile=-/etc/eimemory/governance.env',
        'EnvironmentFile=-/etc/eimemory/recall.env','EnvironmentFile=-/etc/eimemory/identity.env',
        'EnvironmentFile=-/etc/eimemory/runtime-authority.env','EnvironmentFile=-/etc/eimemory/evidence-receipt.env']
    if unit.startswith('hermes-gateway'):
        lines+=['Environment=EIMEMORY_ATTESTATION_HOST_PROFILE=operator-separated-v1',
            'Environment=EIMEMORY_HERMES_ATTESTATION_TOKEN_FILE=/etc/eimemory/hermes-attestation.token',
            'Environment=EIMEMORY_ADAPTER_RECEIPT_HANDOFF_FILE=/var/lib/eimemory/state/adapter-receipt-handoff.sqlite3']
        if unit=='hermes-gateway.service':
            lines+=['Environment=EIMEMORY_HERMES_GATEWAY_PROCESS=1',
                'Environment=EIMEMORY_HERMES_CODE_IMPLEMENTATION_SOCKET=/var/lib/eimemory/run/hermes-code-implementation.v2.sock']
    elif unit=='openclaw-gateway.service':
        lines+=['Environment="EIMEMORY_HOOK_COMMAND=/opt/eimemory/current/.venv/bin/eimemory openclaw-hook"',
            'Environment="EIMEMORY_BRIDGE_COMMAND=/opt/eimemory/current/.venv/bin/eimemory ei-bridge feishu"',
            'Environment=EIMEMORY_ENABLE_PROMPT_INJECTION=1','Environment=EIMEMORY_ENABLE_PROMPT_BRIDGE=0']
    if authority is not None:
        authority=Path(authority)
        if (not authority.is_absolute() or '..' in authority.parts
                or re.fullmatch(r'/[A-Za-z0-9_./-]+',str(authority)) is None):
            raise ValueError('manager_gateway_integration_authority_invalid')
        lines+=['Environment=EIMEMORY_CONTROL_PLANE=user','EnvironmentFile=-'+str(authority/'user-runtime.env')]
    return ('\n'.join(lines)+'\n').encode()


def legacy_gateway_policy(raw,commit,account,unit,*,authority=None,user_mode=True):
    """Reconstruct old reviewed policy bytes for archival/CAS retirement only."""
    if not is_gateway_unit(unit) or re.fullmatch('[0-9a-f]{40}',commit or '') is None:
        raise ValueError('manager_gateway_integration_identity_invalid')
    raw=raw.replace(b'@COMMIT@',commit.encode()).replace(('User='+account.pw_name+'\n').encode(),b'')
    raw=raw.replace(('Group='+str(account.pw_gid)+'\n').encode(),b'').replace(b'PrivateUsers=self',b'PrivateUsers=yes')
    raw=raw.replace(('/opt/eimemory/releases/'+commit+' ').encode(),b'/opt/eimemory/releases ')
    if user_mode:
        if authority is None:raise ValueError('manager_gateway_legacy_authority_required')
        raw+=('EnvironmentFile='+str(Path(authority)/'user-runtime.env')+'\n').encode()
        if unit.startswith('hermes-gateway'):raw+=b'UMask=0077\n'
    return raw

USER_MODE = os.environ.get('EIMEMORY_CONTROL_PLANE', '') == 'user'
CONTROL_UID = os.geteuid() if USER_MODE and hasattr(os,'geteuid') else 0


def load_user_control():
    """Pure render/retirement helpers can be imported before installation."""
    global user_control,USER_COORDINATES
    if 'user_control' in globals():return
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
    if USER_MODE:
        load_user_control()
        return json.loads(user_control.read_file(path))
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
    if USER_MODE:load_user_control()
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
    home=Path(account.pw_dir)/'.config/systemd/user'
    for unit,item in registry['units'].items():
        if item['manager']!='user' or not item['installed'] or not unit.endswith('.service'):continue
        directory=home/(unit+'.d')
        if is_gateway_unit(unit):
            # Native gateway directories may intentionally be collaborative.
            # Only our integration file is private; never chmod native paths.
            for parent in (home,home.parent,home.parent.parent,Path(account.pw_dir)):
                st=parent.lstat()
                if not stat.S_ISDIR(st.st_mode) or st.st_uid!=uid:
                    raise ValueError('manager_gateway_directory_untrusted')
            if not os.path.lexists(directory):directory.mkdir(mode=0o700)
            st=directory.lstat()
            if not stat.S_ISDIR(st.st_mode) or st.st_uid!=uid:
                raise ValueError('manager_gateway_directory_untrusted')
            authority=USER_COORDINATES['authority'] if USER_MODE else None
            target=directory/('zzzzzz-eimemory-user-control-plane.conf' if USER_MODE else 'zzzz-eimemory-host-manager.conf')
            content=render_gateway_integration(unit,commit,authority=authority)
            expected_existing=None
            if os.path.lexists(target):
                info=target.lstat()
                if (not stat.S_ISREG(info.st_mode) or info.st_uid!=uid or info.st_nlink!=1
                        or info.st_mode&0o022 or info.st_size>65536):raise ValueError('manager_gateway_integration_target_untrusted')
                existing=target.read_bytes()
                expected_previous=render_gateway_integration(unit,registry['runtime_commit'],authority=authority)
                if existing not in {content,expected_previous}:
                    raise ValueError('manager_gateway_integration_customized_preserved')
                expected_existing=(existing,info)
            write_user_metadata(target,content,uid,0o600,expected_existing=expected_existing)
            continue
        for parent in (home,home.parent,home.parent.parent,Path(account.pw_dir)):
            st=parent.lstat()
            if not stat.S_ISDIR(st.st_mode) or st.st_uid!=uid or stat.S_IMODE(st.st_mode)&0o022:
                raise ValueError('manager_user_policy_directory_untrusted')
        if not directory.exists():directory.mkdir(mode=0o700)
        st=directory.lstat()
        if not stat.S_ISDIR(st.st_mode) or st.st_uid!=uid or stat.S_IMODE(st.st_mode)&0o022:
            raise ValueError('manager_user_policy_dropin_untrusted')
        unit_raw=raw
        if USER_MODE:
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


def write_user_metadata(target, raw, uid, mode, *, expected_existing):
    """Publish only the identified integration file, retaining native policy."""
    identity=lambda info:(info.st_dev,info.st_ino,info.st_uid,info.st_gid,
        info.st_mode,info.st_nlink,info.st_size,info.st_mtime_ns,info.st_ctime_ns)
    directory=target.parent.lstat()
    directory_identity=lambda info:(info.st_dev,info.st_ino,info.st_uid,info.st_gid,info.st_mode)
    def check_expected():
        if directory_identity(target.parent.lstat())!=directory_identity(directory):
            raise ValueError('manager_gateway_integration_directory_changed')
        if expected_existing is None:
            if os.path.lexists(target):raise ValueError('manager_gateway_integration_changed')
            return
        expected,info=expected_existing
        descriptor=os.open(target,os.O_RDONLY|os.O_NOFOLLOW|os.O_CLOEXEC|os.O_NONBLOCK)
        try:
            current=os.fstat(descriptor)
            if (not stat.S_ISREG(current.st_mode) or current.st_uid!=uid or current.st_nlink!=1
                    or identity(current)!=identity(info) or current.st_size>65536
                    or os.read(descriptor,65537)!=expected
                    or identity(os.fstat(descriptor))!=identity(info)
                    or identity(target.lstat())!=identity(info)):
                raise ValueError('manager_gateway_integration_changed')
        finally:os.close(descriptor)
    check_expected()
    temporary=target.with_name('.eimemory-metadata-'+str(os.getpid()))
    fd=os.open(temporary,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,mode)
    try:
        os.fchmod(fd,mode)
        with os.fdopen(fd,'wb',closefd=False) as output:
            output.write(raw);output.flush();os.fsync(fd)
        check_expected()
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
