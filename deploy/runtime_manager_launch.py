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
import importlib
import runpy
import ast
from hashlib import sha256


def read_root(path,*,raw=False):
    return_raw=raw
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
        if return_raw:return raw
        def pairs(items):
            value={}
            for key,item in items:
                if key in value:raise ValueError('manager_launcher_duplicate_json_key')
                value[key]=item
            return value
        return json.loads(raw,object_pairs_hook=pairs,parse_constant=lambda _:(_ for _ in ()).throw(ValueError('manager_launcher_nonfinite_json')))
    finally:os.close(fd)


EMPTY_PYCACHE=Path('/var/lib/eimemory-manager/empty-pycache')
REGISTRY=Path('/etc/eimemory-code-automation-authority/runtime-manager.v1.json')
CURRENT=Path('/opt/eimemory/current')
RELEASES=Path('/opt/eimemory/releases')
CGROUP=Path('/proc/self/cgroup')
PRODUCT_MODULES=frozenset({'eimemory.cli.main','eimemory.cli.l1_worker',
    'eimemory.governance.serve_console','eimemory.governance.safety.audit_verifier',
    'eimemory.retrieval.vector_sync_worker'})


def root_contract():
    path=Path(__file__).parent/'runtime_manager_contract.py'
    raw=read_root(path,raw=True)
    spec=importlib.util.spec_from_file_location('reviewed_launcher_contract',path)
    module=importlib.util.module_from_spec(spec);exec(compile(raw,str(path),'exec'),module.__dict__)
    return module


def source_alias_target(path):
    """Recognize only the repository's explicit, data-free relocation shim."""
    if path.is_symlink() or not path.is_file() or path.resolve(strict=True)!=path:
        return None
    try:
        tree=ast.parse(path.read_bytes())
        branch=tree.body[2]
        target=branch.body[0].names[0].name
        if re.fullmatch(r'eimemory(?:\.[A-Za-z_][A-Za-z_0-9]*)+',target) is None:return None
        expected=ast.parse('''"""Compatibility alias: this module moved to TARGET."""
import sys as _sys
if __name__ != "__main__":
    import TARGET as _target
    _sys.modules[__name__] = _target
else:
    import runpy as _runpy
    _runpy.run_module("TARGET", run_name="__main__", alter_sys=True)
'''.replace('TARGET',target))
        if ast.dump(tree,include_attributes=False)!=ast.dump(expected,include_attributes=False):return None
        return target
    except (OSError,SyntaxError,AttributeError,IndexError,TypeError,ValueError):
        return None


def assert_product_origins(release):
    package=release/'eimemory'
    for name,module in list(sys.modules.items()):
        if name!='eimemory' and not name.startswith('eimemory.'):continue
        if re.fullmatch(r'eimemory(?:\.[A-Za-z_][A-Za-z_0-9]*)*',name) is None:
            raise ValueError('manager_product_module_origin_invalid')
        if module is None:raise ValueError('manager_product_preloaded_module_invalid')
        canonical=getattr(getattr(module,'__spec__',None),'name',name)
        if not isinstance(canonical,str) or re.fullmatch(r'eimemory(?:\.[A-Za-z_][A-Za-z_0-9]*)*',canonical) is None:
            raise ValueError('manager_product_module_origin_invalid')
        if canonical!=name:
            alias=release/Path(*name.split('.'))
            alias=alias/'__init__.py' if alias.is_dir() else alias.with_suffix('.py')
            if (source_alias_target(alias)!=canonical or sys.modules.get(canonical) is not module):
                raise ValueError('manager_product_module_alias_invalid')
        relative=Path(*canonical.split('.'))
        path=release/relative;filename=getattr(module,'__file__',None)
        namespace=getattr(module,'__path__',None)
        if filename is None:
            if not path.is_dir() or namespace is None:raise ValueError('manager_product_module_origin_invalid')
        else:
            expected=path/'__init__.py' if namespace is not None else path.with_suffix('.py')
            if (Path(filename)!=expected or expected.is_symlink() or expected.resolve(strict=True)!=expected
                    or not expected.is_file()):
                raise ValueError('manager_product_module_origin_invalid')
        if namespace is not None and (list(namespace)!=[str(path)] or path.is_symlink() or path.resolve(strict=True)!=path):
            raise ValueError('manager_product_package_path_invalid')
    loaded=sys.modules.get('eimemory')
    if loaded is not None and (getattr(loaded,'__file__',None)!=str(package/'__init__.py')
            or list(getattr(loaded,'__path__',[]))!=[str(package)]):
        raise ValueError('manager_product_package_origin_invalid')


def prepare_product_module(module):
    if module not in PRODUCT_MODULES:raise ValueError('manager_product_module_not_registered')
    # Only reviewed root-owned launcher bytes run here; no root process imports
    # a runtime package. The actual current link is authoritative for serving
    # even during publish -> activate, when the registry still names its prior.
    read_root(__file__,raw=True);registry=root_contract().validate_registry(read_root(REGISTRY))
    if os.getuid()!=registry['runtime_uid'] or os.geteuid()!=registry['runtime_uid'] or os.geteuid()==0:
        raise ValueError('manager_launcher_runtime_uid_required')
    flags=protected_cache()
    if not sys.flags.isolated or not sys.dont_write_bytecode or sys._xoptions.get('pycache_prefix')!=str(EMPTY_PYCACHE):
        raise ValueError('manager_product_interpreter_flags_required')
    release=CURRENT.resolve(strict=True)
    if release.parent!=RELEASES or re.fullmatch('[0-9a-f]{40}',release.name) is None or release.is_symlink():
        raise ValueError('manager_product_current_release_invalid')
    if not os.statvfs(release).f_flag & os.ST_RDONLY:raise ValueError('manager_product_source_not_readonly')
    prefix=release/'.venv'
    if Path(sys.prefix).resolve(strict=True)!=prefix or Path(sys.executable) not in {
            prefix/'bin/python',CURRENT/'.venv/bin/python'}:
        raise ValueError('manager_product_target_interpreter_required')
    package=release/'eimemory'
    if package.is_symlink() or not package.is_dir() or not (package/'__init__.py').is_file():
        raise ValueError('manager_product_source_package_invalid')
    assert_product_origins(release)  # A .pth-preloaded site-packages copy fails.
    sys.path.insert(0,str(release));importlib.invalidate_caches()
    importlib.import_module('eimemory')
    assert_product_origins(release)
    spec=importlib.util.find_spec(module)
    expected=release/Path(*module.split('.')).with_suffix('.py')
    if spec is None or spec.origin!=str(expected) or expected.is_symlink() or expected.resolve(strict=True)!=expected:
        raise ValueError('manager_product_entry_origin_invalid')
    assert_product_origins(release)
    return release


def apply_runtime_settings(module,args):
    path=Path(__file__).parent/'runtime-settings.v1.json'
    if not path.exists() and not path.is_symlink():return args
    value=read_root(path)
    helper_path=path.parent/'runtime_manager_settings.py'
    raw=read_root(helper_path,raw=True)
    spec=importlib.util.spec_from_file_location('reviewed_launcher_settings',helper_path)
    helper=importlib.util.module_from_spec(spec)
    exec(compile(raw,str(helper_path),'exec'),helper.__dict__)
    helper.validate(value)
    units={part for line in CGROUP.read_text().splitlines()
        for part in line.split(':',2)[-1].split('/') if part in value['units']}
    if len(units)!=1:raise ValueError('manager_settings_native_unit_ambiguous')
    return helper.apply(value,units.pop(),module,args)


def product_module(module,args):
    if len(args)>128 or sum(len(v) for v in args)>65536 or any('\0' in v for v in args):
        raise ValueError('manager_product_argv_invalid')
    release=prepare_product_module(module)
    args=exec_runtime_settings(module,args)
    sys.argv=[module,*args]
    try:
        runpy.run_module(module,run_name='__main__',alter_sys=True)
    except BaseException as error:
        try:assert_product_origins(release)
        except (ValueError,OSError) as origin_error:
            if isinstance(error,SystemExit) and error.code in (None,0):raise origin_error from error
            if hasattr(error,'add_note'):error.add_note('Product origin recheck also failed: '+type(origin_error).__name__)
        raise
    else:
        assert_product_origins(release)


def exec_runtime_settings(module,args):
    """Make the reviewed UID overlay part of the actual exec environment."""
    path=Path(__file__).parent/'runtime-settings.v1.json'
    if not path.exists() and not path.is_symlink():return args
    token=sha256(read_root(path,raw=True)+read_root(__file__,raw=True)).hexdigest()
    marker='EIMEMORY_RUNTIME_SETTINGS_APPLIED'
    prior=os.environ.get(marker)
    if prior is not None:
        if prior!=token:raise ValueError('manager_settings_exec_identity_changed')
        return args
    args=apply_runtime_settings(module,args)
    environment={**os.environ,marker:token}
    os.execve(sys.executable,[sys.executable,*protected_cache(),str(Path(__file__).resolve()),
        '--product-module',module,*args],environment)
    raise ValueError('manager_settings_exec_returned')


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
    protected_cache();registry=root_contract().validate_registry(read_root(REGISTRY))
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
    if re.fullmatch('[0-9a-f]{64}',key) is None or operation not in {'receipt','pre_observation','post_resume'}:
        raise ValueError('manager_launcher_validation_identity_invalid')
    record=read_root('/var/lib/eimemory-manager/validations/'+key+'-'+operation+'.json')
    parent=read_root('/var/lib/eimemory-manager/jobs/'+key+'.json')
    c=root_contract();registry=c.validate_registry(record['registry']);uid=registry['runtime_uid']
    request=parent['request'];manual=parent.get('manual_release') is True
    if manual:c.validate_manual_permit(request,key)
    else:c.validate_job(request,key)
    controller=request.get('validation_controller_commit',request['controller_commit'])
    if os.getuid()!=uid or os.geteuid()!=uid or uid==0:raise ValueError('manager_launcher_runtime_uid_required')
    if (record['job_key']!=key or record['operation']!=operation or record['parent_original_invocation']!=parent.get('original_invocation')
            or record.get('manual_release') is not manual or record['launch_controller_commit']!=request['controller_commit']
            or record['target_commit'] not in ({request['base_commit'],request['candidate_commit']} if manual else {request['target_commit']})
            or record['controller_commit']!=controller):
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
    c=root_contract();c.validate_registry(registry)
    if record.get('manual_release') is True:c.validate_manual_permit(request,key)
    else:c.validate_job(request,key)
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
        for coordinate,suffix in (('tenant_id','TENANT'),('agent_id','AGENT'),('workspace_id','WORKSPACE'),('user_id','USER')):
            os.environ['EIMEMORY_DEPLOY_SCOPE_'+suffix]=request['scope'][coordinate]
        worker=repository/'deploy/eimemory-deploy-worker'
        os.execv(str(worker),[str(worker),'deploy',request['target_commit']])
    os.execv(str(release/'.venv/bin/python'),
        [str(release/'.venv/bin/python'),*flags,str(release/'deploy/run_code_deployment_job.py'),'--job-key',key])


if __name__=='__main__':
    if len(sys.argv)>=3 and sys.argv[1]=='--product-module':product_module(sys.argv[2],sys.argv[3:])
    elif len(sys.argv)==2 and sys.argv[1]=='--recover-fused':recover_fused()
    elif len(sys.argv)==2:launch(sys.argv[1])
    elif len(sys.argv)==3 and sys.argv[1]=='--preflight':launch(sys.argv[2],preflight=True)
    elif len(sys.argv)==4 and sys.argv[1]=='--validation':validate(sys.argv[2],sys.argv[3])
    else:raise SystemExit('exact_job_key_required')
