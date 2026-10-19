#!/usr/bin/python3
"""Fixed, root-owned launcher executed only as the registered runtime UID."""
from __future__ import annotations
import json
import os
from pathlib import Path
import re
import stat
import sys


def read_root(path):
    path=Path(path)
    for parent in (path.parent,*path.parent.parents):
        st=parent.lstat()
        if not stat.S_ISDIR(st.st_mode) or st.st_uid!=0 or stat.S_IMODE(st.st_mode)&0o022:
            raise ValueError('manager_launcher_parent_untrusted')
    fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_CLOEXEC)
    try:
        a=os.fstat(fd)
        if a.st_uid!=0 or a.st_nlink!=1 or stat.S_IMODE(a.st_mode)!=0o640 or not stat.S_ISREG(a.st_mode) or a.st_size>65536:
            raise ValueError('manager_launcher_record_untrusted')
        raw=os.read(fd,65537);b=os.fstat(fd);c=path.lstat()
        coordinates=lambda s:(s.st_dev,s.st_ino,s.st_size,s.st_mtime_ns,s.st_ctime_ns,s.st_mode,s.st_uid,s.st_gid,s.st_nlink)
        if coordinates(a)!=coordinates(b) or coordinates(a)!=coordinates(c):raise ValueError('manager_launcher_record_changed')
        return json.loads(raw)
    finally:os.close(fd)


def launch(key,*,preflight=False):
    if re.fullmatch('[0-9a-f]{64}',key) is None:raise ValueError('manager_launcher_key_invalid')
    record=read_root('/var/lib/eimemory-manager/jobs/'+key+'.json')
    registry=record['registry']
    if os.getuid()!=registry['runtime_uid'] or os.geteuid()!=registry['runtime_uid'] or os.geteuid()==0:
        raise ValueError('manager_launcher_runtime_uid_required')
    request=record['request'];commit=request['controller_commit']
    if re.fullmatch('[0-9a-f]{40}',commit) is None or record['job_key']!=key:raise ValueError('manager_launcher_record_identity_invalid')
    release=Path('/opt/eimemory/releases')/commit
    if release.is_symlink() or not release.is_dir():raise ValueError('manager_launcher_controller_missing')
    if not os.statvfs(release).f_flag & os.ST_RDONLY:raise ValueError('manager_launcher_controller_not_readonly')
    if preflight:return
    os.environ['EIMEMORY_RUNTIME_TRANSITION_JOB']=key
    os.environ['EIMEMORY_RUNTIME_CONTROLLER_COMMIT']=commit
    os.environ['PYTHONDONTWRITEBYTECODE']='1'
    if record.get('manual_release') is True:
        os.environ['EIMEMORY_CODE_EVOLUTION_TRANSACTION_MODE']='0'
        repository=Path(registry['repository_root'])
        if repository.resolve(strict=True)!=repository:raise ValueError('manager_launcher_repository_changed')
        os.environ['REPO_DIR']=str(repository)
        os.environ['EIMEMORY_TRUSTED_REPOSITORY_ROOT']=str(repository)
        worker=repository/'deploy/eimemory-deploy-worker'
        os.execv(str(worker),[str(worker),'deploy',request['target_commit']])
    os.execv(str(release/'.venv/bin/python'),
        [str(release/'.venv/bin/python'),'-I','-B',str(release/'deploy/run_code_deployment_job.py'),'--job-key',key])


if __name__=='__main__':
    if len(sys.argv)==2:launch(sys.argv[1])
    elif len(sys.argv)==3 and sys.argv[1]=='--preflight':launch(sys.argv[2],preflight=True)
    else:raise SystemExit('exact_job_key_required')
