#!/usr/bin/python3
"""Fixed, root-owned launcher executed only as the registered runtime UID."""
from __future__ import annotations
import json
import os
from pathlib import Path
import re
import stat
import sys
import socket
import struct
from hashlib import sha256


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


EMPTY_PYCACHE=Path('/var/lib/eimemory-manager/empty-pycache')
REGISTRY=Path('/etc/eimemory-code-automation-authority/runtime-manager.v1.json')


def protected_cache():
    for parent in (EMPTY_PYCACHE,*EMPTY_PYCACHE.parents):
        st=parent.lstat()
        if not stat.S_ISDIR(st.st_mode) or st.st_uid!=0 or stat.S_IMODE(st.st_mode)&0o022:
            raise ValueError('manager_launcher_pycache_untrusted')
    if stat.S_IMODE(EMPTY_PYCACHE.stat().st_mode)!=0o555 or any(EMPTY_PYCACHE.iterdir()):
        raise ValueError('manager_launcher_pycache_not_empty')
    os.environ['PYTHONPYCACHEPREFIX']=str(EMPTY_PYCACHE)
    os.environ['PYTHONDONTWRITEBYTECODE']='1'
    return ['-I','-B','-X','pycache_prefix='+str(EMPTY_PYCACHE)]


def recover_fused():
    # Fixed native pre-start/post-stop hook. No product imports or caller unit,
    # command, scope or invocation arguments cross the root boundary.
    protected_cache();registry=read_root(REGISTRY)
    if os.getuid()!=registry['runtime_uid'] or os.geteuid()!=registry['runtime_uid'] or os.geteuid()==0:
        raise ValueError('manager_launcher_runtime_uid_required')
    active=Path('/var/lib/eimemory-manager/active-transition.json')
    if not active.exists() and not active.is_symlink():
        print(json.dumps({'restored':True,'phase':'no_pending_context'}));return
    context=read_root(active)
    if not isinstance(context,dict):raise ValueError('manager_launcher_context_invalid')
    if context.get('context')!='fused_closure' or context.get('complete') is True:
        # A deployment may synchronously stop this service while holding the
        # lifecycle lock. Its StopPost must not call back into that lock for an
        # unrelated context. Only the root-owned pending fused pointer needs
        # recovery; that pointer itself prevents deployment quiesce admission.
        print(json.dumps({'restored':True,'phase':'no_pending_context'}));return
    path=Path('/run/eimemory-manager/bridge.sock')
    for parent in (path.parent,*path.parent.parents):
        st=parent.lstat()
        if not stat.S_ISDIR(st.st_mode) or st.st_uid!=0 or st.st_mode&0o022:raise ValueError('manager_launcher_socket_parent_untrusted')
    st=path.lstat()
    if not stat.S_ISSOCK(st.st_mode) or st.st_uid!=0 or stat.S_IMODE(st.st_mode)!=0o660 or st.st_nlink!=1:
        raise ValueError('manager_launcher_socket_untrusted')
    value={'schema':'eimemory.runtime_manager_request.v1','operation':'recover_fused_closure','registry_generation':registry['generation']}
    raw=json.dumps(value,sort_keys=True,separators=(',',':')).encode()
    def receive(stream,length):
        result=b''
        while len(result)<length:
            chunk=stream.recv(length-len(result))
            if not chunk:raise ValueError('manager_launcher_response_truncated')
            result+=chunk
        return result
    with socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) as stream:
        stream.settimeout(610);stream.connect(str(path))
        if struct.unpack('3i',stream.getsockopt(socket.SOL_SOCKET,socket.SO_PEERCRED,12))[1]!=0:
            raise ValueError('manager_launcher_host_root_peer_required')
        stream.sendall(struct.pack('>I',len(raw))+raw)
        size=struct.unpack('>I',receive(stream,4))[0]
        if size>65536:raise ValueError('manager_launcher_response_too_large')
        result=json.loads(receive(stream,size))
    if (result.get('schema')!='eimemory.runtime_manager_response.v1' or result.get('operation')!=value['operation']
            or result.get('registry_generation')!=registry['generation'] or result.get('ok') is not True):
        raise ValueError('manager_launcher_fused_recovery_failed')
    print(json.dumps(result))


def validate(key,operation):
    if re.fullmatch('[0-9a-f]{64}',key) is None or operation not in {'receipt','post_resume'}:
        raise ValueError('manager_launcher_validation_identity_invalid')
    record=read_root('/var/lib/eimemory-manager/validations/'+key+'-'+operation+'.json')
    parent=read_root('/var/lib/eimemory-manager/jobs/'+key+'.json')
    registry=record['registry'];uid=registry['runtime_uid']
    if os.getuid()!=uid or os.geteuid()!=uid or uid==0:raise ValueError('manager_launcher_runtime_uid_required')
    if (record['job_key']!=key or record['operation']!=operation or record['parent_original_invocation']!=parent.get('original_invocation')
            or record['target_commit']!=parent['request']['target_commit'] or record['controller_commit']!=parent['request']['controller_commit']):
        raise ValueError('manager_launcher_validation_record_mismatch')
    releases=Path('/opt/eimemory/releases');target=releases/record['target_commit'];controller=releases/record['controller_commit']
    for path in (target,controller):
        if path.is_symlink() or not path.is_dir() or not os.statvfs(path).f_flag & os.ST_RDONLY:
            raise ValueError('manager_launcher_validation_source_not_readonly')
    entry=controller/'deploy/run_code_deployment_validation.py'
    if entry.is_symlink() or sha256(entry.read_bytes()).hexdigest()!=record['entry_digest']:
        raise ValueError('manager_launcher_validation_entry_changed')
    flags=protected_cache()
    os.environ['EIMEMORY_RUNTIME_TRANSITION_JOB']=key
    os.environ['EIMEMORY_RUNTIME_CONTROLLER_COMMIT']=record['controller_commit']
    os.environ['EIMEMORY_PARENT_DEPLOYMENT_INVOCATION']=record['parent_original_invocation']
    os.execv(str(controller/'.venv/bin/python'),[str(controller/'.venv/bin/python'),*flags,str(entry),'--job-key',key,'--operation',operation])


def launch(key,*,preflight=False):
    if re.fullmatch('[0-9a-f]{64}',key) is None:raise ValueError('manager_launcher_key_invalid')
    flags=protected_cache()
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
        [str(release/'.venv/bin/python'),*flags,str(release/'deploy/run_code_deployment_job.py'),'--job-key',key])


if __name__=='__main__':
    if len(sys.argv)==2 and sys.argv[1]=='--recover-fused':recover_fused()
    elif len(sys.argv)==2:launch(sys.argv[1])
    elif len(sys.argv)==3 and sys.argv[1]=='--preflight':launch(sys.argv[2],preflight=True)
    elif len(sys.argv)==4 and sys.argv[1]=='--validation':validate(sys.argv[2],sys.argv[3])
    else:raise SystemExit('exact_job_key_required')
