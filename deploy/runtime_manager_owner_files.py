#!/usr/bin/python3
"""Fixed user-owned unit snapshot/restore worker, always executed as UID1001.

Root supplies original reviewed bytes over stdin, never a destination path.
Untouched unit/config bytes must still match; only our own compatibility
policy is restored or removed. No root executes runtime-owned unit source.
"""
from __future__ import annotations
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import stat
import sys

def default_home():
    import pwd
    return Path(pwd.getpwuid(1001).pw_dir) / '.config/systemd/user'
POLICY='zzzz-eimemory-host-manager.conf'
MAX=4*1024*1024


def directories(home):
    uid=os.geteuid()
    if uid!=1001 or os.getuid()!=uid:raise ValueError('owner_files_runtime_uid_required')
    for path in (home,home.parent,home.parent.parent,home.parent.parent.parent):
        st=path.lstat()
        if not stat.S_ISDIR(st.st_mode) or st.st_uid!=uid or stat.S_IMODE(st.st_mode)&0o022:
            raise ValueError('owner_files_directory_untrusted')


def read(path):
    if path.is_symlink():
        if os.readlink(path)!='/dev/null':raise ValueError('owner_files_symlink_forbidden')
        return {'kind':'mask'}
    if not path.exists():return None
    fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_CLOEXEC)
    try:
        a=os.fstat(fd)
        if not stat.S_ISREG(a.st_mode) or a.st_uid!=1001 or a.st_nlink!=1 or a.st_size>65536:
            raise ValueError('owner_files_file_untrusted')
        raw=os.read(fd,65537);b=os.fstat(fd);named=path.lstat()
        key=lambda s:(s.st_dev,s.st_ino,s.st_size,s.st_mtime_ns,s.st_ctime_ns,s.st_mode,s.st_uid,s.st_nlink)
        if key(a)!=key(b) or key(a)!=key(named):raise ValueError('owner_files_file_changed')
        return {'kind':'file','raw_hex':raw.hex(),'sha256':sha256(raw).hexdigest(),'mode':stat.S_IMODE(a.st_mode)}
    finally:os.close(fd)


def snapshot(units,*,home=None):
    home = default_home() if home is None else home
    directories(home)
    if (not isinstance(units,list) or len(units)>100 or len(set(units))!=len(units)
            or any(not isinstance(u,str) or not re.fullmatch(r'(?:eimemory|hermes|openclaw)-[A-Za-z0-9_-]+\.(?:service|timer|path)',u) for u in units)):
        raise ValueError('owner_files_unit_inventory_invalid')
    files={}
    for unit in units:
        files[unit]=read(home/unit)
        directory=home/(unit+'.d')
        if directory.exists() or directory.is_symlink():
            st=directory.lstat()
            if not stat.S_ISDIR(st.st_mode) or st.st_uid!=1001 or stat.S_IMODE(st.st_mode)&0o022:raise ValueError('owner_files_dropin_directory_untrusted')
            names=sorted(p.name for p in directory.iterdir())
            if len(names)>100 or any(not re.fullmatch(r'[A-Za-z0-9_.-]+\.conf',n) for n in names):raise ValueError('owner_files_dropin_inventory_invalid')
            for name in names:files[unit+'.d/'+name]=read(directory/name)
        files.setdefault(unit+'.d/'+POLICY,None)
    return {'schema':'eimemory.runtime_owner_user_files.v1','units':units,'files':files}


def restore(value,*,home=None):
    home = default_home() if home is None else home
    if not isinstance(value,dict) or set(value)!={'schema','units','files'} or value['schema']!='eimemory.runtime_owner_user_files.v1':raise ValueError('owner_files_snapshot_invalid')
    current=snapshot(value['units'],home=home)
    original=value['files']
    if not isinstance(original,dict):raise ValueError('owner_files_snapshot_invalid')
    # Validate the complete inventory before any write. New unrelated user
    # configuration is never deleted or silently replaced by owner rollback.
    for name in set(current['files'])|set(original):
        if name.endswith('.d/'+POLICY):continue
        if current['files'].get(name)!=original.get(name):raise ValueError('owner_files_unrelated_configuration_changed')
    allowed={unit+'.d/'+POLICY for unit in value['units']}
    if not set(original)<=set(current['files'])|allowed:raise ValueError('owner_files_snapshot_path_invalid')
    for name in allowed:
        before=original.get(name);target=home/name
        if before is None:
            if target.exists() or target.is_symlink():
                read(target);target.unlink()
        else:
            if (not isinstance(before,dict) or set(before)!={'kind','raw_hex','sha256','mode'} or before['kind']!='file'
                    or type(before['mode']) is not int or before['mode']&0o022):raise ValueError('owner_files_policy_snapshot_invalid')
            raw=bytes.fromhex(before['raw_hex'])
            if len(raw)>65536 or sha256(raw).hexdigest()!=before['sha256']:raise ValueError('owner_files_policy_digest_invalid')
            if not target.parent.exists():target.parent.mkdir(mode=0o700)
            # Same UID only; parent symlink/ownership is rechecked by snapshot.
            snapshot(value['units'],home=home)
            temp=target.with_name('.owner-restore-'+str(os.getpid()))
            fd=os.open(temp,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,before['mode'])
            try:
                os.write(fd,raw);os.fchmod(fd,before['mode']);os.fsync(fd);os.replace(temp,target)
            finally:
                os.close(fd)
                if temp.exists():temp.unlink()
        if target.parent.exists():
            d=os.open(target.parent,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
            try:os.fsync(d)
            finally:os.close(d)
    if snapshot(value['units'],home=home)!=value:raise ValueError('owner_files_restore_readback_mismatch')
    return {'ok':True,'files_restored':len(allowed)}


def main():
    if sys.argv[1:] not in (['snapshot'],['restore']):raise ValueError('owner_files_fixed_operation_required')
    raw=sys.stdin.buffer.read(MAX+1)
    if len(raw)>MAX:raise ValueError('owner_files_input_limit')
    value=json.loads(raw)
    result=snapshot(value) if sys.argv[1]=='snapshot' else restore(value)
    print(json.dumps(result,sort_keys=True,separators=(',',':')))


if __name__=='__main__':main()
