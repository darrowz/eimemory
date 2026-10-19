#!/usr/bin/python3
"""Fixed root-reviewed compatibility writer, executed as runtime UID only."""
from __future__ import annotations
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import importlib.util


def read_root(path):
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
    registry=read_root('/etc/eimemory/runtime-manager.v1.json')
    uid=registry['runtime_uid']
    if os.getuid()!=uid or os.geteuid()!=uid or uid==0:raise ValueError('manager_user_policy_runtime_uid_required')
    root=Path('/usr/libexec/eimemory-manager')
    fd=os.open(root/'runtime-policy.conf',os.O_RDONLY|os.O_NOFOLLOW|os.O_CLOEXEC)
    try:
        st=os.fstat(fd)
        if st.st_uid!=0 or st.st_nlink!=1 or not stat.S_ISREG(st.st_mode) or stat.S_IMODE(st.st_mode)!=0o640:
            raise ValueError('manager_user_policy_root_template_invalid')
        raw=os.read(fd,65537)
        if len(raw)>65536:raise ValueError('manager_user_policy_template_limit')
    finally:os.close(fd)
    raw=raw.replace(b'@COMMIT@',commit.encode()).replace(b'User=darrow\n',b'').replace(b'Group=darrow\n',b'').replace(b'PrivateUsers=self',b'PrivateUsers=yes')
    raw=raw.replace(('/opt/eimemory/releases/'+commit+' ').encode(),b'/opt/eimemory/releases ')
    home=Path('/home/darrow/.config/systemd/user')
    for parent in (home,home.parent,home.parent.parent,Path('/home/darrow')):
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
        target=directory/'zzzz-eimemory-host-manager.conf'
        temporary=directory/('.eimemory-host-manager-'+str(os.getpid()))
        fd=os.open(temporary,os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW|os.O_WRONLY,0o600)
        try:
            os.write(fd,raw);os.fsync(fd)
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


if __name__=='__main__':
    if len(sys.argv)!=2:raise SystemExit('exact_commit_required')
    apply(sys.argv[1])
