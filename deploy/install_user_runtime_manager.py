#!/usr/bin/env python3
"""Install a byte-reviewed user controller without privilege escalation.

This prepares the controller and user unit definitions. It never stops a system
service, changes the current release, creates an incident or accepts a release.
"""
from __future__ import annotations

import argparse
from hashlib import sha256
import importlib.util
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import subprocess
import sys
import tempfile
import zipfile
from contextlib import contextmanager
from functools import wraps


@contextmanager
def user_operator_profile():
    """Select this operation's profile without changing a caller's environment."""
    name='EIMEMORY_CONTROL_PLANE';original=os.environ.get(name)
    os.environ[name]='user'
    try:yield
    finally:
        if original is None:os.environ.pop(name,None)
        else:os.environ[name]=original


def scoped_user_operator(function):
    @wraps(function)
    def operation(*args,**kwargs):
        with user_operator_profile():return function(*args,**kwargs)
    return operation


@contextmanager
def operator_lock(helper,path):
    """Use the shared lock, including recovery from pre-lock controller releases."""
    if hasattr(helper,'verified_lock'):
        with helper.verified_lock(path) as descriptor:yield descriptor
        return
    # The reviewed operator entry must also recover controllers installed before
    # verified_lock existed. Never import unreviewed replacement helper code.
    import fcntl
    uid=os.geteuid();path=Path(path)
    if uid<=0 or os.getuid()!=uid:raise ValueError('user_control_plane_lock_untrusted')
    helper.trusted_directory(path.parent)
    descriptor=os.open(path,os.O_RDWR|os.O_CREAT|os.O_NOFOLLOW|os.O_CLOEXEC,0o600)
    try:
        info=os.fstat(descriptor);named=path.lstat()
        identity=lambda item:(item.st_dev,item.st_ino,item.st_uid,item.st_gid,item.st_mode,item.st_nlink)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid!=uid or info.st_nlink!=1
                or stat.S_IMODE(info.st_mode)!=0o600 or identity(info)!=identity(named)):
            raise ValueError('user_control_plane_lock_untrusted')
        try:fcntl.flock(descriptor,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError as exc:raise ValueError('user_control_plane_lock_busy') from exc
        if identity(info)!=identity(path.lstat()):raise ValueError('user_control_plane_lock_changed')
        yield descriptor
        if identity(info)!=identity(path.lstat()):raise ValueError('user_control_plane_lock_changed')
    finally:os.close(descriptor)


def native_state(unit,properties,environment):
    result=subprocess.run(['/usr/bin/systemctl','--user','show',unit,
        '--property='+','.join(properties),'--no-pager'],env=environment,
        text=True,capture_output=True,timeout=30)
    if result.returncode not in {0,4}:raise ValueError('user_operator_native_read_failed')
    fields={}
    for line in result.stdout.splitlines():
        key,separator,value=line.partition('=')
        if not separator or key in fields:raise ValueError('user_operator_native_state_invalid')
        fields[key]=value
    if set(fields)!=set(properties):raise ValueError('user_operator_native_state_incomplete')
    return fields


def stopped_controller(environment):
    fields=native_state('eimemory-runtime-manager.service',
        ['ActiveState','MainPID','ControlPID','ControlGroup'],environment)
    if (fields['ActiveState'] not in {'inactive','failed'} or fields['MainPID']!='0'
            or fields['ControlPID']!='0' or fields['ControlGroup']!=''):
        raise ValueError('user_install_controller_must_be_stopped')


def sync_parent(path):
    descriptor=os.open(Path(path).parent,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
    try:os.fsync(descriptor)
    finally:os.close(descriptor)


def verify_controller_sources(registry,commit):
    for name,expected in registry['compatibility_hashes'].items():
        source=Path('/opt/eimemory/releases')/commit/name
        if source.is_symlink() or source.resolve(strict=True)!=source:
            raise ValueError('user_install_release_controller_mismatch')
        reviewed(source,expected,4*1024*1024)


def owned_bytes(path,uid,maximum=16*1024*1024):
    """Read an owned regular name/descriptor once without following links."""
    path=Path(path);fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_CLOEXEC|os.O_NONBLOCK)
    try:
        before=os.fstat(fd)
        identity=lambda item:(item.st_dev,item.st_ino,item.st_uid,item.st_gid,item.st_mode,
                              item.st_nlink,item.st_size,item.st_mtime_ns,item.st_ctime_ns)
        if (not stat.S_ISREG(before.st_mode) or before.st_uid!=uid or before.st_nlink!=1
                or before.st_mode&0o022 or before.st_size>maximum):
            raise ValueError('user_operator_file_untrusted')
        raw=os.read(fd,maximum+1)
        if (len(raw)!=before.st_size or identity(before)!=identity(os.fstat(fd))
                or identity(before)!=identity(path.lstat())):
            raise ValueError('user_operator_file_changed')
        return raw
    finally:os.close(fd)


def installation_inventory(home,registry,uid,helper=None):
    """Seal published control bytes, including the generated effective policy."""
    home=Path(home);code=home/'.local/lib/eimemory/control-plane'
    authority=home/'.config/eimemory/control-plane';units=home/'.config/systemd/user'
    paths=set()
    for root in (code,authority):
        if helper is not None:helper.trusted_directory(root)
        if root.is_symlink():raise ValueError('user_install_published_symlink')
        for path in root.rglob('*'):
            if path.is_symlink():raise ValueError('user_install_published_symlink')
            if path.is_file():paths.add(path)
    native=set(registry['template_hashes'])|{name for name,item in registry['units'].items() if item['installed']}
    native.add('eimemory-runtime-manager.service')
    for unit in native:
        fragment=units/unit
        if os.path.lexists(fragment):paths.add(fragment)
        dropins=units/(unit+'.d')
        if dropins.exists():
            if helper is not None:helper.trusted_directory(dropins)
            if dropins.is_symlink():raise ValueError('user_install_published_symlink')
            for path in dropins.rglob('*'):
                if path.is_symlink():raise ValueError('user_install_published_symlink')
                if path.is_file():paths.add(path)
        if unit in registry['units'] and registry['units'][unit]['installed'] and unit.endswith('.service'):
            policy=dropins/'zzzzzz-eimemory-user-control-plane.conf'
            if not policy.is_file():raise ValueError('user_install_effective_policy_missing')
    wrapper=units/'hermes-gateway-eimemory.sh'
    if os.path.lexists(wrapper):paths.add(wrapper)
    result={}
    for path in sorted(paths):
        raw=(helper.read_file(path,maximum=16*1024*1024,modes=(0o600,0o640,0o644,0o755))
            if helper is not None else owned_bytes(path,uid))
        info=path.lstat()
        result[str(path)]={'sha256':sha256(raw).hexdigest(),'mode':stat.S_IMODE(info.st_mode),'uid':info.st_uid}
    return result


def strict(raw):
    def pairs(items):
        value={}
        for key,item in items:
            if key in value:raise ValueError('user_install_duplicate_json_key')
            value[key]=item
        return value
    return json.loads(raw,object_pairs_hook=pairs,
        parse_constant=lambda _:(_ for _ in ()).throw(ValueError('user_install_nonfinite_json')))


def canonical(value):
    return json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=False,allow_nan=False).encode()


def execute_reviewed_module(name,path,raw):
    # A dotted module name such as "helper.py" creates a package context and
    # defeats the controller contract's stdlib-only standalone import path.
    specification=importlib.util.spec_from_file_location('user_reviewed_'+Path(name).stem,path)
    module=importlib.util.module_from_spec(specification)
    exec(compile(raw,str(path),'exec'),module.__dict__)
    return module


def reviewed(path,expected,maximum=16*1024*1024):
    path=Path(path)
    if re.fullmatch('[0-9a-f]{64}',expected or '') is None:
        raise ValueError('user_install_review_digest_required')
    named_before=path.lstat()
    if not stat.S_ISREG(named_before.st_mode):raise ValueError('user_install_reviewed_input_unsafe')
    fd=os.open(path,os.O_RDONLY|getattr(os,'O_NOFOLLOW',0)|getattr(os,'O_CLOEXEC',0)|getattr(os,'O_NONBLOCK',0))
    try:
        before=os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink!=1 or before.st_size>maximum:
            raise ValueError('user_install_reviewed_input_unsafe')
        raw=os.read(fd,maximum+1)
        identity=lambda s:(s.st_dev,s.st_ino,s.st_mode,s.st_uid,s.st_nlink,s.st_size,s.st_mtime_ns,s.st_ctime_ns)
        # Windows handle ctime and directory-entry ctime use different native
        # clocks. Compare each clock against its own initial observation while
        # still binding the handle and name by inode, mode, owner, size and mtime.
        named_after=path.lstat()
        same_name=(identity(before)==identity(named_after) if os.name!='nt' else
                   identity(before)[:-1]==identity(named_after)[:-1])
        if (len(raw)!=before.st_size or identity(before)!=identity(os.fstat(fd))
                or identity(named_before)!=identity(named_after) or not same_name
                or sha256(raw).hexdigest()!=expected):
            raise ValueError('user_install_reviewed_input_changed')
        return raw
    finally:os.close(fd)


def bundle_files(path,expected,uid,gid):
    bundle=reviewed(path,expected,64*1024*1024)
    with zipfile.ZipFile(io.BytesIO(bundle)) as archive:
        entries=archive.infolist()
        if len(entries)>128 or len({x.filename for x in entries})!=len(entries):
            raise ValueError('user_install_bundle_inventory_invalid')
        files={}
        for entry in entries:
            name=entry.filename;parts=PurePosixPath(name).parts
            if (not parts or name.startswith('/') or '..' in parts or '\\' in name
                    or any(ord(c)<32 for c in name) or entry.is_dir()
                    or entry.file_size>8*1024*1024 or stat.S_ISLNK(entry.external_attr>>16)):
                raise ValueError('user_install_bundle_member_unsafe')
            files[name]=archive.read(entry)
    manifest=strict(files.pop('manifest.json'))
    if (set(manifest)!={'schema','controller_commit','runtime_uid','runtime_gid','files'}
            or manifest['schema']!='eimemory.user_runtime_manager_bundle.v1'
            or manifest['runtime_uid']!=uid or manifest['runtime_gid']!=gid
            or manifest['files']!={name:sha256(raw).hexdigest() for name,raw in sorted(files.items())}
            or re.fullmatch('[0-9a-f]{40}',manifest['controller_commit']) is None):
        raise ValueError('user_install_bundle_identity_invalid')
    return files,manifest


def directory(path,uid,mode=0o750):
    path=Path(path)
    if not path.is_absolute() or '..' in path.parts:raise ValueError('user_install_directory_invalid')
    missing=[];parent=path
    while not parent.exists():missing.append(parent);parent=parent.parent
    for item in reversed(missing):item.mkdir(mode=mode)
    for item in (path,*path.parents):
        info=item.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid not in {0,uid} or info.st_mode&0o022:
            raise ValueError('user_install_directory_untrusted')
    if path.lstat().st_uid!=uid:raise ValueError('user_install_directory_owner_invalid')
    path.chmod(mode)


def write(path,raw,uid,mode=0o640):
    path=Path(path)
    directory(path.parent,uid,stat.S_IMODE(path.parent.lstat().st_mode) if path.parent.exists() else 0o750)
    if path.exists() or path.is_symlink():
        before=path.lstat()
        if not stat.S_ISREG(before.st_mode) or before.st_uid!=uid or before.st_nlink!=1:
            raise ValueError('user_install_destination_untrusted')
    temporary=path.with_name('.'+path.name+'.install-'+str(os.getpid()))
    fd=os.open(temporary,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,mode)
    try:
        os.fchmod(fd,mode)
        with os.fdopen(fd,'wb',closefd=False) as stream:
            stream.write(raw);stream.flush();os.fsync(fd)
        os.replace(temporary,path)
        parent=os.open(path.parent,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
        try:os.fsync(parent)
        finally:os.close(parent)
    finally:
        os.close(fd)
        temporary.unlink(missing_ok=True)


def archive_write(path,raw,uid,mode=0o600):
    """Custody originals are immutable, including a resumed operator recovery."""
    if os.path.lexists(path):
        if owned_bytes(path,uid)!=raw:raise ValueError('user_recovery_archive_changed')
        return
    write(path,raw,uid,mode)


def retain_manifest_history(state, raw, generation, uid):
    """Archive reviewed manifest bytes before replacement, including skipped releases."""
    if re.fullmatch('[0-9a-f]{64}',generation or '') is None:
        raise ValueError('user_install_prior_generation_invalid')
    from datetime import datetime,timezone
    name='authority-'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')+'-'+str(os.getpid())
    history=Path(state)/name
    history.mkdir(mode=0o750)  # Existing histories are never overwritten.
    write(history/'managed-manifest.snapshot',raw,uid,0o600)
    write(history/'complete.json',canonical({'registry_generation':generation,
        'manifest_sha256':sha256(raw).hexdigest()}),uid,0o600)
    parent=os.open(history.parent,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
    try:os.fsync(parent)
    finally:os.close(parent)
    return history


@scoped_user_operator
def install(args):
    if os.name!='posix' or os.geteuid()<=0 or os.getuid()!=os.geteuid():
        raise ValueError('user_install_nonroot_identity_required')
    import pwd
    account=pwd.getpwuid(os.geteuid());uid=account.pw_uid;home=Path(account.pw_dir)
    if account.pw_gid!=os.getegid():raise ValueError('user_install_primary_group_required')
    if os.path.lexists(home/'.local/state/eimemory/control-plane/controller-installation.json'):
        raise ValueError('user_install_incomplete_installation_preserved')
    environment={**os.environ,'XDG_RUNTIME_DIR':'/run/user/'+str(uid),
        'DBUS_SESSION_BUS_ADDRESS':'unix:path=/run/user/'+str(uid)+'/bus'}
    stopped_controller(environment)
    files,manifest=bundle_files(args.bundle,args.expected_bundle_sha256,uid,account.pw_gid)
    seed=strict(reviewed(args.registry_seed,args.expected_registry_seed_sha256,65536))
    settings=reviewed(args.runtime_settings,args.expected_runtime_settings_sha256,65536)
    managed_raw=reviewed(args.managed_manifest,args.expected_managed_manifest_sha256,1048576)
    managed=strict(managed_raw)
    commit=manifest['controller_commit'];runtime=seed['runtime_commit']
    prior_path=home/'.config/eimemory/control-plane/managed-code-incident-manifest.v1.json'
    prior_raw=reviewed(prior_path,seed['managed_incident_authority']['manifest_sha256'],1048576)
    prior=strict(prior_raw)
    if (seed['runtime_uid']!=uid or seed['runtime_gid']!=account.pw_gid
            or seed['runtime_user']!=account.pw_name
            or Path('/opt/eimemory/current').resolve(strict=True)!=Path('/opt/eimemory/releases')/runtime
            or managed['release_commit']!=commit
            or managed['previous_manifest_digest']!=sha256(canonical(prior)).hexdigest()
            or managed.get('scopes')!=prior.get('scopes')):
        raise ValueError('user_install_original_release_binding_invalid')
    authority=home/'.config/eimemory/control-plane';state=home/'.local/state/eimemory/control-plane'
    code=home/'.local/lib/eimemory/control-plane';socket=Path('/run/user')/str(uid)/'eimemory-manager'
    for path in (authority,state,code,home/'.config/systemd/user',home/'.local/share/systemd/user',
                 *(state/name for name in ('jobs','validations','validation-results','activation-receipts','fused-closures'))):
        directory(path,uid)
    for name in ('.hermes','.cache','.codex','.openclaw'):
        product_home=home/name
        mode=stat.S_IMODE(product_home.lstat().st_mode) if product_home.exists() else 0o750
        directory(product_home,uid,mode)
    directory(home/'.local/state/hermes',uid,0o700)
    directory(socket,uid,0o700)
    # Reinstallation needs an exact current-generation CAS and no live effect.
    registry_path=authority/'runtime-manager.v1.json'
    original_registry=None
    if registry_path.exists():
        if not args.expected_existing_registry_sha256:
            raise ValueError('user_install_existing_registry_cas_required')
        original_registry=reviewed(registry_path,args.expected_existing_registry_sha256)
        transition=state/'active-transition.json'
        if transition.exists() and strict(transition.read_bytes()).get('complete') is not True:
            raise ValueError('user_install_active_transition_preserved')
    elif args.expected_existing_registry_sha256:
        raise ValueError('user_install_existing_registry_missing')
    transition=state/'active-transition.json'
    original_transition=owned_bytes(transition,uid,65536) if os.path.lexists(transition) else None
    if original_transition is not None:
        meta=transition.lstat()
        original_transition=reviewed(transition,sha256(original_transition).hexdigest(),65536)
        if meta.st_uid!=uid or meta.st_nlink!=1 or stat.S_IMODE(meta.st_mode)!=0o640:
            raise ValueError('user_install_transition_untrusted')
    # Preserve independently imported original knowledge and signing material.
    # No fresh empty ledger or regenerated key may replace migration evidence.
    for key in (authority/'managed-incident-proof.key',state/'activation-signing.key'):
        info=key.lstat()
        if (not stat.S_ISREG(info.st_mode) or info.st_uid!=uid or info.st_nlink!=1
                or stat.S_IMODE(info.st_mode)!=0o600 or not 32<=info.st_size<=4096):
            raise ValueError('user_install_original_signing_material_required')
        expected=(args.expected_activation_key_sha256 if key.name=='activation-signing.key'
                  else args.expected_managed_key_sha256)
        raw=reviewed(key,expected,4096)
        if key.name=='activation-signing.key' and len(raw)!=32:
            raise ValueError('user_install_original_activation_key_size_invalid')
    knowledge_directory=state/'managed-incident-receipts'
    if knowledge_directory.is_symlink() or not knowledge_directory.is_dir():
        raise ValueError('user_install_original_knowledge_required')
    directory(knowledge_directory,uid,0o700)
    # Bind the original complete control ledger, not an empty replacement.
    reviewed(knowledge_directory/'knowledge.sqlite',args.expected_knowledge_sha256,256*1024*1024)
    import sqlite3
    for suffix in ('-wal','-journal'):
        sidecar=knowledge_directory/('knowledge.sqlite'+suffix)
        if sidecar.is_symlink() or sidecar.exists() and sidecar.stat().st_size:
            raise ValueError('user_install_original_knowledge_not_checkpointed')
    connection=sqlite3.connect((knowledge_directory/'knowledge.sqlite').as_uri()+'?mode=ro&immutable=1',uri=True)
    try:
        if connection.execute('PRAGMA quick_check').fetchone()!=('ok',):
            raise ValueError('user_install_original_knowledge_corrupt')
        for table in ('authority_meta','managed_knowledge','managed_receipts','managed_manifest_history'):
            if connection.execute('SELECT 1 FROM sqlite_master WHERE type=? AND name=?',('table',table)).fetchone() is None:
                raise ValueError('user_install_original_knowledge_schema_missing')
        if connection.execute('SELECT COUNT(*) FROM managed_knowledge').fetchone()[0]<=0:
            raise ValueError('user_install_original_knowledge_empty')
    finally:connection.close()
    with tempfile.TemporaryDirectory(prefix='.user-controller-review-',dir=code.parent) as staging:
        staged=Path(staging)
        for name,raw in files.items():write(staged/name,raw,uid)
        helper=execute_reviewed_module('user_control_plane.py',staged/'user_control_plane.py',files['user_control_plane.py'])
        if not hasattr(helper,'verified_lock'):raise ValueError('user_install_shared_lock_contract_required')
        specification=importlib.util.spec_from_file_location('user_install_contract',staged/'runtime_manager_contract.py')
        contract=importlib.util.module_from_spec(specification)
        exec(compile(files['runtime_manager_contract.py'],str(staged/'runtime_manager_contract.py'),'exec'),contract.__dict__)
        settings_spec=importlib.util.spec_from_file_location('user_install_settings',staged/'runtime_manager_settings.py')
        settings_module=importlib.util.module_from_spec(settings_spec)
        exec(compile(files['runtime_manager_settings.py'],str(staged/'runtime_manager_settings.py'),'exec'),settings_module.__dict__)
        settings_module.validate(strict(settings))
    entry=strict(files['controller-entry.json'])
    registry={**seed,'controller_commit':commit,'runtime_commit':runtime,
        'units':{name:{**value,'manager':'user'} for name,value in seed['units'].items()},
        'template_hashes':{name.removeprefix('templates/'):sha256(raw).hexdigest()
            for name,raw in files.items() if name.startswith('templates/')},
        'compatibility_hashes':strict(files['compatibility.json']),
        'controller_entry_digest':entry['sha256'],'validation_entry_digest':entry['validation_sha256'],
        'managed_incident_authority':{'enabled':True,'database_path':'/var/lib/eimemory/state/eimemory.sqlite',
            'manifest_path':str(authority/'managed-code-incident-manifest.v1.json'),
            'manifest_sha256':sha256(managed_raw).hexdigest(),
            'receipt_directory':str(state/'managed-incident-receipts'),
            'key_path':str(authority/'managed-incident-proof.key')}}
    registry['generation']=contract.digest({k:v for k,v in registry.items() if k!='generation'})
    contract.validate_registry(registry)
    for name,expected in registry['compatibility_hashes'].items():
        source=Path('/opt/eimemory/releases')/commit/name
        if source.is_symlink() or sha256(source.read_bytes()).hexdigest()!=expected:
            raise ValueError('user_install_release_controller_mismatch')
    # All reviewed inputs and release pins have passed before publishing code.
    # bridge.lock excludes a running/new controller; lifecycle.lock excludes
    # operator staging/recovery as well as a concurrent native transaction.
    with operator_lock(helper,state/'bridge.lock'),operator_lock(helper,state/'lifecycle.lock'):
        stopped_controller(environment)
        verify_controller_sources(registry,commit)
        if Path('/opt/eimemory/current').resolve(strict=True)!=Path('/opt/eimemory/releases')/runtime:
            raise ValueError('user_install_current_changed')
        if original_registry is None:
            if os.path.lexists(registry_path):raise ValueError('user_install_registry_changed')
        elif reviewed(registry_path,args.expected_existing_registry_sha256)!=original_registry:
            raise ValueError('user_install_registry_changed')
        actual_transition=transition.read_bytes() if os.path.lexists(transition) else None
        if actual_transition!=original_transition:raise ValueError('user_install_transition_changed')
        marker=state/'controller-installation.json'
        if os.path.lexists(marker):raise ValueError('user_install_incomplete_installation_preserved')
        _publish_installation(args,account,files,manifest,seed,settings,managed_raw,prior_raw,
            registry,contract,helper,environment,original_registry,original_transition)


def _publish_installation(args,account,files,manifest,seed,settings,managed_raw,prior_raw,
                          registry,contract,helper,environment,original_registry,original_transition):
    """Publish only inside both locks; any exception leaves a durable fence."""
    uid=account.pw_uid;home=Path(account.pw_dir)
    authority=home/'.config/eimemory/control-plane';state=home/'.local/state/eimemory/control-plane'
    code=home/'.local/lib/eimemory/control-plane';registry_path=authority/'runtime-manager.v1.json'
    commit=manifest['controller_commit'];runtime=seed['runtime_commit']
    # Backups share the key/ledger's inaccessible mount boundary in products.
    import tarfile
    knowledge_directory=state/'managed-incident-receipts'
    backup_directory=knowledge_directory/'migration-backups'
    directory(backup_directory,uid,0o700)
    backup=backup_directory/(commit+'-'+str(os.getpid())+'.tar')
    descriptor=os.open(backup,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
    with os.fdopen(descriptor,'wb') as output:
        with tarfile.open(fileobj=output,mode='w') as archive:
            for label,source in (('authority',authority),('code',code),('user-units',home/'.config/systemd/user')):
                archive.add(source,arcname=label,recursive=True)
        output.flush();os.fsync(output.fileno())
    backup_parent=os.open(backup_directory,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
    try:os.fsync(backup_parent)
    finally:os.close(backup_parent)
    marker=state/'controller-installation.json'
    intent={'schema':'eimemory.user_controller_installation.v1','controller_commit':commit,
        'runtime_commit':runtime,'bundle_sha256':args.expected_bundle_sha256,
        'registry_before_sha256':sha256(original_registry).hexdigest() if original_registry is not None else None,
        'transition_before_sha256':sha256(original_transition).hexdigest() if original_transition is not None else None,
        'registry_after_sha256':sha256(contract.canonical(registry)).hexdigest(),
        'backup_basename':backup.name,'backup_sha256':sha256(backup.read_bytes()).hexdigest(),
        'phase':'publication_pending','release_accepted':False,'autonomous_repair_credit':False}
    intent_raw=canonical(intent)
    write(marker,intent_raw,uid,0o600)
    # PID1 evaluates this guard even for an old controller lacking the new
    # startup guard. Conditions also fence timers/path/service dependency starts.
    guard=('[Unit]\nConditionPathExists=!'+str(marker)+'\n').encode()
    guarded=set(name.removeprefix('templates/') for name in files if name.startswith('templates/'))
    guarded.update(name for name,item in registry['units'].items() if item['installed'])
    guarded.add('eimemory-runtime-manager.service')
    unit_home=home/'.config/systemd/user'
    for unit in sorted(guarded):
        write(unit_home/(unit+'.d')/'05-eimemory-installation-guard.conf',guard,uid,0o644)
    subprocess.run(['/usr/bin/systemctl','--user','daemon-reload'],env=environment,check=True,timeout=30)
    stopped_controller(environment)
    verify_controller_sources(registry,commit)
    if Path('/opt/eimemory/current').resolve(strict=True)!=Path('/opt/eimemory/releases')/runtime:
        raise ValueError('user_install_current_changed')
    if original_registry is not None and reviewed(registry_path,args.expected_existing_registry_sha256)!=original_registry:
        raise ValueError('user_install_registry_changed')
    if (transition_raw:=((state/'active-transition.json').read_bytes()
            if os.path.lexists(state/'active-transition.json') else None))!=original_transition:
        raise ValueError('user_install_transition_changed')
    retain_manifest_history(state,prior_raw,seed['generation'],uid)
    for name,raw in files.items():
        if name!='eimemory-runtime-manager.service':write(code/name,raw,uid)
    cache=code/'empty-pycache'
    directory(cache,uid,0o555)
    if any(cache.iterdir()):raise ValueError('user_install_pycache_not_empty')
    write(code/'runtime-settings.v1.json',settings,uid)
    write(authority/'user-runtime.env',
        ('EIMEMORY_CONTROL_PLANE=user\nEIMEMORY_IDENTITY_AUTHORITY_DIR='+str(authority/'identity')+'\n'
         'EIMEMORY_SUBJECT_PROJECTION_AUTHORITY_DIR='+str(authority/'subject-projection')+'\n').encode(),uid)
    write(authority/'managed-code-incident-manifest.v1.json',managed_raw,uid)
    write(registry_path,contract.canonical(registry),uid)
    write(authority/'installation.v1.json',canonical({'schema':'eimemory.user_control_plane_installation.v1',
        'runtime_uid':uid,'runtime_gid':account.pw_gid,'control_plane':'user'}),uid)
    unit_home=home/'.config/systemd/user'
    install_user_units(files,unit_home,uid,controller_commit=commit,runtime_commit=runtime)
    for unit,item in registry['units'].items():
        if item['installed'] and unit.endswith('.service'):
            directory(unit_home/(unit+'.d'),uid)
    # Historical grants retain their original requester IDs and journal hashes.
    # Only future operator requests move to the user identity.
    release=Path('/opt/eimemory/releases')/commit
    migration=subprocess.run([str(release/'.venv/bin/python'),'-I','-B','-X','pycache_prefix='+str(cache),
        '-c',('import sys;sys.path.insert(0,sys.argv[1]);from pathlib import Path;'
              'from eimemory.identity_authority import migrate_user_policy,coordinates;'
              'print(migrate_user_policy());p=coordinates()["authority"]/"subject-projection";'
              'print(migrate_user_policy(p)) if (p/"policy.json").exists() else None'),str(release)],
        env={**os.environ,'XDG_RUNTIME_DIR':'/run/user/'+str(uid),
             'DBUS_SESSION_BUS_ADDRESS':'unix:path=/run/user/'+str(uid)+'/bus'},timeout=60,check=False)
    if migration.returncode:raise ValueError('user_install_identity_history_migration_failed')
    # Apply the exact reviewed user policy; no product or model is executed.
    result=subprocess.run(['/usr/bin/python3','-I','-B',str(code/'runtime_manager_user_policy.py'),runtime],
        env={**os.environ,'XDG_RUNTIME_DIR':'/run/user/'+str(uid),
             'DBUS_SESSION_BUS_ADDRESS':'unix:path=/run/user/'+str(uid)+'/bus'},timeout=60,check=False)
    if result.returncode:raise ValueError('user_install_policy_failed')
    # Read back the full published generation before lifting the durable fence.
    for name,raw in files.items():
        if name!='eimemory-runtime-manager.service':reviewed(code/name,sha256(raw).hexdigest())
    reviewed(code/'runtime-settings.v1.json',sha256(settings).hexdigest())
    reviewed(authority/'managed-code-incident-manifest.v1.json',sha256(managed_raw).hexdigest())
    reviewed(registry_path,sha256(contract.canonical(registry)).hexdigest())
    reviewed(unit_home/'eimemory-runtime-manager.service',sha256(files['eimemory-runtime-manager.service']).hexdigest())
    for name,raw in files.items():
        if name.startswith('templates/'):
            reviewed(unit_home/name.removeprefix('templates/'),sha256(raw).hexdigest())
    for path,digest in ((authority/'managed-incident-proof.key',args.expected_managed_key_sha256),
                        (state/'activation-signing.key',args.expected_activation_key_sha256),
                        (knowledge_directory/'knowledge.sqlite',args.expected_knowledge_sha256)):
        reviewed(path,digest,256*1024*1024)
    for unit in guarded:
        reviewed(unit_home/(unit+'.d')/'05-eimemory-installation-guard.conf',sha256(guard).hexdigest())
    stopped_controller(environment)
    verify_controller_sources(registry,commit)
    if Path('/opt/eimemory/current').resolve(strict=True)!=Path('/opt/eimemory/releases')/runtime:
        raise ValueError('user_install_current_changed')
    if ((state/'active-transition.json').read_bytes()
            if os.path.lexists(state/'active-transition.json') else None)!=original_transition:
        raise ValueError('user_install_transition_changed')
    reviewed(marker,sha256(intent_raw).hexdigest(),65536)
    seal={'schema':'eimemory.user_controller_installation_completion.v1',
        'installation_sha256':sha256(intent_raw).hexdigest(),'registry_sha256':sha256(contract.canonical(registry)).hexdigest(),
        'controller_commit':commit,'runtime_commit':runtime,'release_accepted':False,'autonomous_repair_credit':False,
        'published_files':installation_inventory(home,registry,uid,helper)}
    write(backup.with_suffix('.complete.json'),canonical(seal),uid,0o600)
    marker.unlink();sync_parent(marker)
    print(json.dumps({'ok':True,'control_plane':'user','runtime_uid':uid,
        'controller_commit':commit,'runtime_commit':runtime,'registry_generation':registry['generation'],
        'serving_link_changed':False,'services_started':False,'release_accepted':False}))


@scoped_user_operator
def recover_completed_installation(args):
    """Complete only an already sealed installation; partial installs stay fenced."""
    import pwd
    import time
    if os.name!='posix' or os.geteuid()<=0 or os.getuid()!=os.geteuid():
        raise ValueError('user_install_recovery_nonroot_identity_required')
    account=pwd.getpwuid(os.geteuid());uid=account.pw_uid;home=Path(account.pw_dir)
    if account.pw_gid!=os.getegid():raise ValueError('user_install_recovery_primary_group_required')
    state=home/'.local/state/eimemory/control-plane';code=home/'.local/lib/eimemory/control-plane'
    authority=home/'.config/eimemory/control-plane';marker=state/'controller-installation.json'
    registry_path=authority/'runtime-manager.v1.json'
    marker_raw=reviewed(marker,args.expected_marker_sha256,65536);intent=strict(marker_raw)
    registry_raw=reviewed(registry_path,args.expected_registry_sha256,65536);declared=strict(registry_raw)
    if (intent.get('schema')!='eimemory.user_controller_installation.v1'
            or intent.get('phase')!='publication_pending' or intent.get('release_accepted') is not False
            or intent.get('autonomous_repair_credit') is not False
            or intent.get('registry_after_sha256')!=sha256(registry_raw).hexdigest()):
        raise ValueError('user_install_recovery_intent_unverified')
    def load(name,source):
        raw=reviewed(code/name,declared['compatibility_hashes'][source],1024*1024)
        return execute_reviewed_module(name,code/name,raw)
    helper=load('user_control_plane.py','eimemory/contracts/user_control_plane.py')
    contract=load('runtime_manager_contract.py','eimemory/contracts/runtime_manager.py')
    registry=contract.validate_registry(declared)
    if (registry['runtime_uid']!=uid or registry['runtime_gid']!=account.pw_gid
            or registry['runtime_user']!=account.pw_name or registry['controller_commit']!=intent['controller_commit']
            or registry['runtime_commit']!=intent['runtime_commit']):
        raise ValueError('user_install_recovery_identity_invalid')
    for path in (authority,state,code):helper.trusted_directory(path)
    environment={'PATH':'/usr/bin:/bin','LC_ALL':'C','XDG_RUNTIME_DIR':'/run/user/'+str(uid),
        'DBUS_SESSION_BUS_ADDRESS':'unix:path=/run/user/'+str(uid)+'/bus'}
    with operator_lock(helper,state/'bridge.lock'),operator_lock(helper,state/'lifecycle.lock'):
        stopped_controller(environment)
        if (owned_bytes(marker,uid,65536)!=marker_raw or owned_bytes(registry_path,uid,65536)!=registry_raw
                or Path('/opt/eimemory/current').resolve(strict=True)!=Path('/opt/eimemory/releases')/registry['runtime_commit']):
            raise ValueError('user_install_recovery_authority_changed')
        basename=intent.get('backup_basename','')
        if re.fullmatch(re.escape(registry['controller_commit'])+r'-[0-9]+\.tar',basename) is None:
            raise ValueError('user_install_recovery_backup_name_invalid')
        backup=state/'managed-incident-receipts/migration-backups'/basename
        if sha256(owned_bytes(backup,uid,256*1024*1024)).hexdigest()!=intent['backup_sha256']:
            raise ValueError('user_install_recovery_backup_changed')
        seal_path=backup.with_suffix('.complete.json')
        seal_raw=reviewed(seal_path,args.expected_completion_sha256,4*1024*1024);seal=strict(seal_raw)
        expected={'schema':'eimemory.user_controller_installation_completion.v1',
            'installation_sha256':sha256(marker_raw).hexdigest(),'registry_sha256':sha256(registry_raw).hexdigest(),
            'controller_commit':registry['controller_commit'],'runtime_commit':registry['runtime_commit'],
            'release_accepted':False,'autonomous_repair_credit':False}
        if (not isinstance(seal,dict) or set(seal)!=set(expected)|{'published_files'}
                or any(seal.get(key)!=value for key,value in expected.items())
                or not isinstance(seal['published_files'],dict)
                or installation_inventory(home,registry,uid,helper)!=seal['published_files']):
            raise ValueError('user_install_recovery_completion_unverified')
        verify_controller_sources(registry,registry['controller_commit'])
        cache=code/'empty-pycache'
        helper.trusted_directory(cache)
        if stat.S_IMODE(cache.stat().st_mode)!=0o555 or any(cache.iterdir()):
            raise ValueError('user_install_recovery_pycache_untrusted')
        transition=state/'active-transition.json'
        actual=sha256(owned_bytes(transition,uid,65536)).hexdigest() if os.path.lexists(transition) else None
        if actual!=intent['transition_before_sha256']:
            raise ValueError('user_install_recovery_transition_changed')
        # Keep the completion-window evidence in the private custody archive.
        archived=backup.with_suffix('.recovered-'+str(time.time_ns())+'.json')
        write(archived,canonical({'schema':'eimemory.user_installation_marker_recovery.v1',
            'marker_hex':marker_raw.hex(),'completion_sha256':sha256(seal_raw).hexdigest(),
            'release_accepted':False,'autonomous_repair_credit':False}),uid,0o600)
        stopped_controller(environment)
        if (owned_bytes(marker,uid,65536)!=marker_raw or owned_bytes(registry_path,uid,65536)!=registry_raw
                or installation_inventory(home,registry,uid,helper)!=seal['published_files']):
            raise ValueError('user_install_recovery_authority_changed')
        marker.unlink();sync_parent(marker)
        print(json.dumps({'ok':True,'installation_recovered':True,'controller_commit':registry['controller_commit'],
            'services_started':False,'release_accepted':False,'autonomous_repair_credit':False}))




def install_user_units(files,unit_home,uid,*,controller_commit,runtime_commit):
    """Install initial job/validation mounts before the first job can start."""
    for name,raw in files.items():
        if name.startswith('templates/'):
            unit=name.removeprefix('templates/')
            write(unit_home/unit,raw,uid,0o644)
            destination=unit_home/(unit+'.d')
            directory(destination,uid)
            if unit.endswith('.service'):
                if unit=='eimemory-code-deployment-job@.service':
                    policy_name='job-runtime-policy.conf';target=controller_commit
                elif unit.startswith('eimemory-code-validation-'):
                    policy_name='validation-runtime-policy.conf';target=controller_commit
                else:
                    policy_name='runtime-policy.conf';target=runtime_commit
                policy=files[policy_name].replace(b'@COMMIT@',target.encode())
                write(destination/'90-eimemory-runtime.conf',policy,uid)
        elif name=='eimemory-runtime-manager.service':
            write(unit_home/name,raw,uid,0o644)


def failed_attempt_native(contract,record,environment):
    """Prove a failed native main/pre-start exit; never infer an unknown start."""
    key=record.get('job_key');contract.validate_manual_permit(record.get('request'),key)
    if record.get('manual_release') is not True or record.get('unit')!=contract.job_unit(key):
        raise ValueError('user_recovery_manual_attempt_invalid')
    properties=[*contract.NATIVE_EXIT_PROPERTIES,'ControlPID','ExecMainStartTimestampMonotonic']
    observed=native_state(record['unit'],properties,environment)
    native={name:observed[name] for name in contract.NATIVE_EXIT_PROPERTIES}
    actual=native['InvocationID'];original=record.get('original_invocation')
    if (observed['ControlPID']!='0' or native['MainPID']!='0' or native['ControlGroup']!=''
            or actual and original not in {None,actual}):
        raise ValueError('user_recovery_attempt_active_or_unknown')
    pinned=record.get('terminal_state')
    if pinned is not None:
        if (pinned.get('Result') not in {'exit-code','signal','timeout','core-dump','watchdog'}
                or not contract.native_terminal_readback(pinned,native,original,record['unit'])):
            raise ValueError('user_recovery_terminal_state_changed')
        return pinned
    if (contract.native_terminal_exit(native,original,record['unit'])
            and native['Result'] in {'exit-code','signal','timeout','core-dump','watchdog'}):
        return native
    prestart=(record.get('phase')=='submitted' and record.get('quiesced') is False
        and record.get('active_before') is None and native['LoadState']=='loaded'
        and native['ActiveState']=='failed' and native['SubState']=='failed'
        and native['Result']=='exit-code' and native['ExecMainStatus']=='0'
        and native['ExecMainExitTimestampMonotonic']=='0'
        and observed['ExecMainStartTimestampMonotonic']=='0')
    if prestart:
        contract.hex_value(actual,32,'user_recovery_prestart_invocation_invalid')
        old=record.get('prestart_failure')
        if old is not None and old!=observed:raise ValueError('user_recovery_prestart_evidence_changed')
        return observed
    raise ValueError('user_recovery_attempt_active_or_unknown')


def revalidation_parent(contract,registry,record,scope,environment):
    request=record.get('request',{})
    if (record.get('manual_release') is not True or record.get('resumed') is not True
            or record.get('phase') not in {'resumed_awaiting_business','completed'}
            or request.get('target_commit')!=registry['runtime_commit'] or request.get('scope')!=scope):
        raise ValueError('user_stage_revalidation_parent_not_eligible')
    failed_attempt_native(contract,record,environment)
    release=Path('/opt/eimemory/releases')/registry['runtime_commit']
    raw=reviewed(release/'deploy/install_immutable_release.sh',
        registry['compatibility_hashes']['deploy/install_immutable_release.sh'],4*1024*1024)
    if b'EIMEMORY_MANAGED_CURRENT_REVALIDATION_CONTRACT=independent-post-resume.v1' not in raw.splitlines():
        raise ValueError('user_stage_revalidation_contract_unavailable')


def capture_reference(helper,state,contract,registry,scope):
    path=state/'manual-recovery-capture.json'
    if not os.path.lexists(path):return None
    raw=helper.read_file(path);value=contract.strict_json(raw)
    fields={'schema','source_job_key','source_job_sha256','source_invocation_id',
        'source_registry_generation','base_commit','scope','active_before','enabled_before',
        'source_native_exit','history_basename'}
    if (not isinstance(value,dict) or set(value)!=fields
            or value['schema']!='eimemory.manual_release_recovery_capture.v1'
            or value['base_commit']!=registry['runtime_commit'] or value['scope']!=scope
            or contract.canonical(value)!=raw):
        raise ValueError('user_stage_recovery_capture_invalid')
    return {'basename':path.name,'sha256':sha256(raw).hexdigest()}


@scoped_user_operator
def stage_release(args):
    """Record one reviewed operator attempt and start its fixed user-owned job.

    This is a manual deployment permit, never an autonomous repair receipt.
    Existing/ambiguous attempts and transitions are preserved, never retried.
    """
    if os.name!='posix' or os.geteuid()<=0 or os.getuid()!=os.geteuid():
        raise ValueError('user_stage_nonroot_identity_required')
    import pwd
    import fcntl
    account=pwd.getpwuid(os.geteuid());uid=account.pw_uid
    if account.pw_gid!=os.getegid():raise ValueError('user_stage_primary_group_required')
    if re.fullmatch('[0-9a-f]{40}',args.target_commit) is None:
        raise ValueError('user_stage_full_target_required')
    if re.fullmatch('[0-9a-f]{32}',args.attempt_id) is None:
        raise ValueError('user_stage_reviewed_attempt_required')
    home=Path(account.pw_dir)
    authority=home/'.config/eimemory/control-plane';state=home/'.local/state/eimemory/control-plane'
    code=home/'.local/lib/eimemory/control-plane'
    registry_path=authority/'runtime-manager.v1.json'
    raw=reviewed(registry_path,args.expected_registry_sha256,65536)
    declared=strict(raw)
    def load(name,source):
        expected=declared['compatibility_hashes'][source]
        data=reviewed(code/name,expected,1024*1024)
        return execute_reviewed_module(name,code/name,data)
    helper=load('user_control_plane.py','eimemory/contracts/user_control_plane.py')
    for path in (authority,state,code):helper.trusted_directory(path)
    contract=load('runtime_manager_contract.py','eimemory/contracts/runtime_manager.py')
    registry=contract.validate_registry(contract.strict_json(raw))
    if (registry['runtime_uid']!=uid or registry['runtime_gid']!=account.pw_gid
            or registry['runtime_user']!=account.pw_name
            or registry['controller_commit']!=args.target_commit):
        raise ValueError('user_stage_controller_target_binding_invalid')
    scope_raw=reviewed(args.scope_manifest,args.expected_scope_sha256,65536)
    scope_manifest=contract.strict_json(scope_raw)
    if (not isinstance(scope_manifest,dict) or set(scope_manifest)!={'schema','scope'}
            or scope_manifest['schema']!='eimemory.manual_release_scope.v1'):
        raise ValueError('user_stage_scope_manifest_invalid')
    scope=contract.validate_scope(scope_manifest['scope'])
    releases=Path('/opt/eimemory/releases');current=Path('/opt/eimemory/current')
    if current.resolve(strict=True)!=releases/registry['runtime_commit']:
        raise ValueError('user_stage_current_registry_mismatch')
    reviewed(releases/args.target_commit/contract.CONTROLLER_ENTRY,registry['controller_entry_digest'],65536)
    reviewed(releases/args.target_commit/contract.VALIDATION_ENTRY,registry['validation_entry_digest'],65536)
    for name in ('storage-release-transaction.json','.storage-release-transaction.json.clearing',
                 '.storage-release-transaction.json.recovery','.storage-restore-journal.json'):
        if os.path.lexists(Path('/var/lib/eimemory/state')/name):
            raise ValueError('user_stage_storage_recovery_not_clean')
    environment={'PATH':'/usr/bin:/bin','LC_ALL':'C','EIMEMORY_CONTROL_PLANE':'user',
        'XDG_RUNTIME_DIR':'/run/user/'+str(uid),
        'DBUS_SESSION_BUS_ADDRESS':'unix:path=/run/user/'+str(uid)+'/bus'}
    revalidation=getattr(args,'revalidate_current_after',None)
    if args.target_commit==registry['runtime_commit']:
        if not revalidation:raise ValueError('user_stage_target_already_current')
    elif revalidation:raise ValueError('user_stage_revalidation_target_not_current')
    # A user permit cannot retire a system owner or silently leave a timer
    # able to relaunch it. These are read-only native observations.
    for unit in (*contract.PRODUCT_SERVICES,*contract.PRODUCT_TIMERS,*contract.PRODUCT_PATHS,
                 'eimemory-runtime-manager.service'):
        result=subprocess.run(['/usr/bin/systemctl','show',unit,
            '--property=LoadState,ActiveState,MainPID,UnitFileState'],env=environment,
            capture_output=True,text=True,timeout=20,check=False)
        fields={}
        for row in result.stdout.splitlines():
            key,separator,value=row.partition('=')
            if not separator or key in fields:raise ValueError('user_stage_legacy_state_invalid')
            fields[key]=value
        allowed={'LoadState','ActiveState','MainPID','UnitFileState'}
        loaded=fields.get('LoadState')=='loaded'
        file_states={'','disabled','masked','not-found'}
        if unit.endswith('.service') and unit!='eimemory-runtime-manager.service':file_states.add('static')
        if (result.returncode not in {0,4} or not set(fields)<=allowed
                or fields.get('LoadState') not in {'loaded','not-found'}
                or fields.get('ActiveState') not in {'inactive','failed'}
                or (loaded and unit.endswith('.service') and fields.get('MainPID')!='0')
                or fields.get('MainPID','0') not in {'','0'}
                or (loaded and 'UnitFileState' not in fields)
                or fields.get('UnitFileState','') not in file_states):
            raise ValueError('user_stage_legacy_system_owner_not_retired')
    cache=code/'empty-pycache'
    subprocess.run([sys.executable,'-I','-B','-X','pycache_prefix='+str(cache),
        str(releases/args.target_commit/'deploy/prepare_user_release.py'),'verify',
        '--repository-root',registry['repository_root'],'--commit',args.target_commit,
        '--releases-root',str(releases)],env=environment,timeout=300,check=True)
    lock_path=state/'lifecycle.lock'
    lock=os.open(lock_path,os.O_RDWR|os.O_CREAT|os.O_NOFOLLOW|os.O_CLOEXEC,0o600)
    try:
        info=os.fstat(lock);named=lock_path.lstat()
        if (not stat.S_ISREG(info.st_mode) or info.st_uid!=uid or info.st_nlink!=1
                or stat.S_IMODE(info.st_mode)!=0o600
                or (info.st_dev,info.st_ino)!=(named.st_dev,named.st_ino)):
            raise ValueError('user_stage_lifecycle_lock_untrusted')
        try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError as exc:raise ValueError('user_stage_lifecycle_busy') from exc
        if reviewed(registry_path,args.expected_registry_sha256,65536)!=raw:
            raise ValueError('user_stage_registry_changed')
        if current.resolve(strict=True)!=releases/registry['runtime_commit']:
            raise ValueError('user_stage_current_changed')
        if os.path.lexists(state/'controller-installation.json'):
            raise ValueError('user_stage_controller_installation_pending')
        pair={'base_commit':registry['runtime_commit'],'candidate_commit':args.target_commit,
            'controller_commit':registry['controller_commit'],'validation_controller_commit':args.target_commit,
            'scope':scope}
        pointer=state/('manual-release-'+contract.digest(pair)+'.json')
        approved_retry=None;pointer_raw=None;previous_raw=None
        prior_key=getattr(args,'prior_job_key',None)
        if os.path.lexists(pointer):
            if not prior_key:raise ValueError('user_stage_existing_attempt_preserved')
            pointer_raw=helper.read_file(pointer);previous=contract.strict_json(pointer_raw)
            contract.hex_value(prior_key,64,'user_stage_prior_job_key_invalid')
            if set(previous)!={'job_key','attempt_id'} or previous['job_key']!=prior_key:
                raise ValueError('user_stage_prior_attempt_review_required')
            previous_raw=helper.read_file(state/'jobs'/(prior_key+'.json'))
            prior=contract.strict_json(previous_raw)
            if (prior.get('job_key')!=prior_key
                    or any(prior['request'].get(name)!=value for name,value in pair.items())
                    or previous['attempt_id']==args.attempt_id):
                raise ValueError('user_stage_prior_attempt_binding_invalid')
            failed_attempt_native(contract,prior,environment)
            approved_retry=prior_key
        elif prior_key:raise ValueError('user_stage_prior_attempt_missing')
        active=state/'active-transition.json'
        if os.path.lexists(active):
            active_value=contract.strict_json(helper.read_file(active))
            if active_value.get('complete') is not True and active_value.get('job_key')!=approved_retry:
                raise ValueError('user_stage_existing_transition_preserved')
        if revalidation:
            contract.hex_value(revalidation,64,'user_stage_revalidation_job_key_invalid')
            original=contract.strict_json(helper.read_file(state/'jobs'/(revalidation+'.json')))
            revalidation_parent(contract,registry,original,scope,environment)
            healthy_runtime(uid,registry['runtime_commit'])
        recovery=capture_reference(helper,state,contract,registry,scope)
        request={'schema':'eimemory.manual_release_permit.v2','operation':'deploy',**pair,
            'target_commit':args.target_commit,'reviewed_attempt_id':args.attempt_id}
        key=contract.digest(request);contract.validate_manual_permit(request,key)
        record={'job_key':key,'request':request,'registry':registry,'manual_release':True,
            'phase':'submitted','unit':contract.job_unit(key),'active_before':None,
            'quiesced':False,'resumed':False,'approved_retry_of':approved_retry,
            **({'revalidation_of':revalidation} if revalidation else {}),
            **({'recovery_capture':recovery} if recovery else {})}
        jobs=state/'jobs';helper.trusted_directory(jobs)
        destination=jobs/(key+'.json')
        def exclusive(path,value):
            fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW|os.O_CLOEXEC,0o640)
            with os.fdopen(fd,'wb') as output:
                os.fchmod(output.fileno(),0o640)
                output.write(contract.canonical(value));output.flush();os.fsync(output.fileno())
            parent=os.open(path.parent,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
            try:os.fsync(parent)
            finally:os.close(parent)
        exclusive(destination,record)
        if approved_retry:
            # Preserve the exact old pointer/record, including an authentic
            # pre-start failure, before atomically publishing a reviewed retry.
            archive=state/'managed-incident-receipts'/('manual-retry-'+args.attempt_id)
            if os.path.lexists(archive):raise ValueError('user_stage_retry_history_exists')
            directory(archive,uid,0o700)
            write(archive/'pointer.original',pointer_raw,uid,0o600)
            write(archive/'job.original',previous_raw,uid,0o600)
            if helper.read_file(pointer)!=pointer_raw or helper.read_file(state/'jobs'/(prior_key+'.json'))!=previous_raw:
                raise ValueError('user_stage_prior_attempt_changed')
            write(pointer,contract.canonical({'job_key':key,'attempt_id':args.attempt_id}),uid)
        else:exclusive(pointer,{'job_key':key,'attempt_id':args.attempt_id})
    finally:os.close(lock)
    # A start error leaves both durable records intact: unknown starts must
    # never be resubmitted under another nonce without reviewed recovery.
    subprocess.run(['/usr/bin/systemctl','--user','start','--no-block',record['unit']],
        env=environment,capture_output=True,text=True,timeout=30,check=True)
    print(json.dumps({'ok':True,'job_key':key,'unit':record['unit'],'state':'submitted',
        'release_accepted':False,'autonomous_repair_credit':False}))


def validate_failed_manual_recovery(contract, registry, record, transition, native, current):
    """A failed manual attempt can be abandoned, never relabeled successful."""
    key=record.get('job_key')
    contract.hex_value(key,64,'user_recovery_job_key_invalid')
    pending=transition=={'complete':False,'job_key':key}
    completed=(transition.get('complete') is True and transition.get('job_key')==key
        and transition.get('held') is False and record.get('phase')=='completed'
        and record.get('finalized') is True and record.get('resumed') is True)
    if (not (pending or completed)
            or record.get('manual_release') is not True
            or record.get('quiesced') is not True
            or record.get('phase') not in {'publish','activate','rollback','resume_deferred_until_terminal',
                                         'resumed_awaiting_business','completed'}
            or record.get('unit')!=contract.job_unit(key)):
        raise ValueError('user_recovery_failed_manual_binding_invalid')
    contract.validate_manual_permit(record['request'],key)
    request=record['request'];old=record['registry']
    base=(current==request['base_commit'] and old==registry
        and record.get('resumed') is not True and record.get('finalized') is not True)
    candidate=(current==request['candidate_commit'] and record.get('resumed') is True
        and record.get('phase') in {'resumed_awaiting_business','completed'}
        and registry.get('controller_commit')==request['validation_controller_commit']
        and {k:v for k,v in old.items() if k not in {'generation','runtime_commit','controller_commit'}}
            =={k:v for k,v in registry.items() if k not in {'generation','runtime_commit','controller_commit'}})
    if current!=registry['runtime_commit'] or not (base or candidate):
        raise ValueError('user_recovery_prior_runtime_changed')
    active=record.get('active_before')
    if (not isinstance(active,list) or len(active)!=len(set(active))
            or not set(active)<=set(registry['units'])):
        raise ValueError('user_recovery_original_capture_invalid')
    if (not contract.native_terminal_exit(native,record.get('original_invocation'),record['unit'])
            or native['Result'] not in {'exit-code','signal','timeout','core-dump','watchdog'}
            or native['ControlGroup']!=''):
        raise ValueError('user_recovery_native_failure_unproven')


def healthy_runtime(uid,commit):
    import urllib.request
    import shlex
    token_path=Path('/etc/eimemory/rpc.env');token_meta=token_path.lstat()
    if (not stat.S_ISREG(token_meta.st_mode) or token_meta.st_uid!=uid
            or token_meta.st_mode&0o022 or token_meta.st_nlink!=1):
        raise ValueError('user_recovery_rpc_config_untrusted')
    assignments=shlex.split(token_path.read_text())
    if len(assignments)!=1 or not assignments[0].startswith('EIMEMORY_RPC_AUTH_TOKEN='):
        raise ValueError('user_recovery_rpc_config_invalid')
    request=urllib.request.Request('http://127.0.0.1:8091/health',
        headers={'Authorization':'Bearer '+assignments[0].partition('=')[2]})
    with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(request,timeout=20) as response:
        health=json.load(response)
    if (health.get('ok') is not True or health.get('commit')!=commit
            or health.get('configured_runtime_commit')!=commit
            or any(health.get('checks',{}).get(name) is not True
                   for name in ('process','store','runtime_identity','ready'))):
        raise ValueError('user_recovery_prior_health_unverified')


def recovery_capture(contract,record,job_raw,native,current,history_name,enabled_before):
    active=record['active_before']
    if (not isinstance(enabled_before,dict) or not set(enabled_before)<=set(record['registry']['units'])
            or any(not isinstance(value,str) or value not in {'enabled','enabled-runtime','disabled','static','indirect','generated','not-found',
                                 'masked','masked-runtime','linked','linked-runtime','alias','transient',''}
                   for value in enabled_before.values())):
        raise ValueError('user_recovery_enabled_capture_invalid')
    return {'schema':'eimemory.manual_release_recovery_capture.v1','source_job_key':record['job_key'],
        'source_job_sha256':sha256(job_raw).hexdigest(),'source_invocation_id':native['InvocationID'],
        'source_registry_generation':record['registry']['generation'],'base_commit':current,
        'scope':record['request']['scope'],'active_before':list(active),
        'enabled_before':dict(enabled_before),'source_native_exit':native,'history_basename':history_name}


def migration_enabled_capture(contract,record,user_audit,host_audit):
    """Map the original system products and user compatibility units once."""
    system={};users={}
    if host_audit is not None:
        if (host_audit.get('schema')!='manual_user_owner_custody.v1'
                or host_audit.get('manual_bootstrap') is not True
                or host_audit.get('autonomous_credit') is not False
                or host_audit.get('current_commit')!=record['request']['base_commit']
                or not isinstance(host_audit.get('system_units'),dict)):
            raise ValueError('user_recovery_system_migration_audit_invalid')
        system=host_audit['system_units']
    if user_audit is not None:
        if (user_audit.get('schema')!='manual_user_owner_before.v1'
                or not isinstance(user_audit.get('states'),dict)):
            raise ValueError('user_recovery_user_migration_audit_invalid')
        users=user_audit['states']
    compatibility=contract.compatibility_units(record['registry']['units'])
    result={}
    for unit in record['active_before']:
        state=(users.get(unit) if unit in compatibility or unit not in system else system.get(unit))
        if isinstance(state,dict) and 'UnitFileState' in state:
            if state.get('LoadState')!='loaded':raise ValueError('user_recovery_migration_unit_unloaded')
            result[unit]=state['UnitFileState']
    return result


def reconcile_consumed_capture(helper,contract,state,record,history,uid):
    """Retire only this failed consumer's already applied capture, never another."""
    fixed=state/'manual-recovery-capture.json'
    if not os.path.lexists(fixed):return
    raw=helper.read_file(fixed);capture=contract.strict_json(raw)
    applied=record.get('recovery_capture_applied')
    expected={'schema':'eimemory.manual_recovery_capture_consumption.v1','sha256':sha256(raw).hexdigest(),
        'job_key':record['job_key'],'invocation_id':record['original_invocation'],
        'source_job_key':capture.get('source_job_key'),'history_basename':capture.get('history_basename')}
    if (applied!=expected or re.fullmatch(r'manual-abandon-[0-9]+',capture.get('history_basename','')) is None
            or not isinstance(capture.get('active_before'),list)
            or not set(capture['active_before'])<=set(record['active_before'])
            or not isinstance(capture.get('enabled_before'),dict)
            or any(record.get('enabled_before',{}).get(unit)!=value
                   for unit,value in capture['enabled_before'].items())):
        raise ValueError('user_recovery_existing_capture_preserved')
    source=state/'managed-incident-receipts'/capture['history_basename']
    if helper.read_file(source/'recovery-capture.json')!=raw:
        raise ValueError('user_recovery_consumed_capture_changed')
    original_raw=helper.read_file(source/'job.original');original=contract.strict_json(original_raw)
    original_native=contract.strict_json(helper.read_file(source/'native-exit.json'))
    contract.validate_manual_permit(original.get('request'),capture.get('source_job_key'))
    if (capture.get('schema')!='eimemory.manual_release_recovery_capture.v1'
            or sha256(original_raw).hexdigest()!=capture.get('source_job_sha256')
            or original.get('job_key')!=capture.get('source_job_key')
            or original.get('manual_release') is not True
            or capture.get('scope')!=record['request']['scope']
            or capture.get('base_commit')!=record['request']['base_commit']
            or capture.get('active_before')!=original.get('active_before')
            or capture.get('source_native_exit')!=original_native
            or capture.get('source_invocation_id')!=original_native.get('InvocationID')
            or capture.get('source_registry_generation')!=original['registry']['generation']
            or not contract.native_terminal_exit(original_native,original.get('original_invocation'),original['unit'])
            or original_native.get('Result') not in {'exit-code','signal','timeout','core-dump','watchdog'}):
        raise ValueError('user_recovery_consumed_capture_binding_invalid')
    consumption=source/'capture-consumption.json'
    if os.path.lexists(consumption):
        ledger=helper.read_file(consumption)
        if contract.strict_json(ledger)!=expected or contract.canonical(expected)!=ledger:
            raise ValueError('user_recovery_foreign_capture_consumer')
    else:
        # record.save was durable before the crash. Complete its exact binding
        # as an explicit operator reconciliation, with a distinct audit event.
        write(source/'capture-consumption-operator-recovery.json',canonical({
            'schema':'eimemory.manual_capture_consumption_operator_recovery.v1',
            'job_key':record['job_key'],'job_sha256':sha256(contract.canonical(record)).hexdigest(),
            'consumption':expected,'autonomous_repair_credit':False}),uid,0o600)
        write(consumption,canonical(expected),uid)
        ledger=canonical(expected)
    archive_write(history/'previous-consumed-capture.json',raw,uid)
    archive_write(history/'previous-capture-consumption.json',ledger,uid)
    if helper.read_file(fixed)!=raw:raise ValueError('user_recovery_consumed_capture_changed')


def pin_failed_terminal(helper,contract,state,record,job_raw,native,history,uid,environment):
    """Pin a real failed native exit once, keeping the untouched pre-pin bytes."""
    if record.get('terminal_state') is not None:return record,job_raw
    if (not contract.native_terminal_exit(native,record.get('original_invocation'),record['unit'])
            or native['Result'] not in {'exit-code','signal','timeout','core-dump','watchdog'}
            or native['ControlGroup']!=''):
        raise ValueError('user_recovery_terminal_pin_unproven')
    if failed_attempt_native(contract,record,environment)!=native:
        raise ValueError('user_recovery_terminal_pin_changed')
    archive_write(history/'job.pre-terminal-pin.original',job_raw,uid)
    event={'schema':'eimemory.manual_failed_terminal_pin.v1','job_key':record['job_key'],
        'invocation_id':native['InvocationID'],'original_job_sha256':sha256(job_raw).hexdigest(),
        'history_basename':history.name,'autonomous_repair_credit':False}
    archive_write(history/'terminal-pin.json',canonical({'pin':event,'native_exit':native}),uid)
    path=state/'jobs'/(record['job_key']+'.json')
    if helper.read_file(path)!=job_raw:raise ValueError('user_recovery_terminal_pin_changed')
    pinned={**record,'original_invocation':native['InvocationID'],'terminal_state':dict(native),
        'operator_terminal_pin':event}
    pinned_raw=contract.canonical(pinned)
    write(path,pinned_raw,uid)
    return pinned,pinned_raw


@scoped_user_operator
def abandon_failed_manual(args):
    """Operator recovery after verified storage rollback; preserves failed proof."""
    import fcntl
    import pwd
    import urllib.request
    import shlex
    import time
    if os.getuid()!=os.geteuid() or os.geteuid()<=0:
        raise ValueError('user_recovery_nonroot_identity_required')
    uid=os.geteuid();home=Path(pwd.getpwuid(uid).pw_dir)
    authority=home/'.config/eimemory/control-plane';state=home/'.local/state/eimemory/control-plane'
    code=home/'.local/lib/eimemory/control-plane'
    raw=reviewed(authority/'runtime-manager.v1.json',args.expected_registry_sha256,65536)
    registry=strict(raw)
    def load(name,source):
        data=reviewed(code/name,registry['compatibility_hashes'][source],1024*1024)
        return execute_reviewed_module(name,code/name,data)
    helper=load('user_control_plane.py','eimemory/contracts/user_control_plane.py')
    contract=load('runtime_manager_contract.py','eimemory/contracts/runtime_manager.py')
    registry=contract.validate_registry(registry)
    for directory_path in (authority,state,code):helper.trusted_directory(directory_path)
    environment={'PATH':'/usr/bin:/bin','LC_ALL':'C','XDG_RUNTIME_DIR':'/run/user/'+str(uid),
        'DBUS_SESSION_BUS_ADDRESS':'unix:path=/run/user/'+str(uid)+'/bus'}
    def show(unit,properties):
        return native_state(unit,list(properties),environment)
    stopped_controller(environment)
    with operator_lock(helper,state/'bridge.lock'),operator_lock(helper,state/'lifecycle.lock'):
        stopped_controller(environment)
        if os.path.lexists(state/'controller-installation.json'):
            raise ValueError('user_recovery_controller_installation_pending')
        transition_raw=reviewed(state/'active-transition.json',args.expected_transition_sha256,65536)
        transition=strict(transition_raw)
        contract.hex_value(args.job_key,64,'user_recovery_job_key_invalid')
        job_raw=helper.read_file(state/'jobs'/(args.job_key+'.json'))
        record=contract.strict_json(job_raw)
        native=failed_attempt_native(contract,record,environment)
        current=Path('/opt/eimemory/current').resolve(strict=True)
        if current.parent!=Path('/opt/eimemory/releases'):
            raise ValueError('user_recovery_current_path_invalid')
        validate_failed_manual_recovery(contract,registry,record,transition,native,current.name)
        storage=Path('/var/lib/eimemory/state')
        for name in ('storage-release-transaction.json','.storage-release-transaction.json.clearing',
                     '.storage-release-transaction.json.recovery','.storage-restore-journal.json'):
            if os.path.lexists(storage/name):raise ValueError('user_recovery_storage_not_clean')
        # No dynamic child may survive its failed parent, including validation
        # processes which still have a write-capable SQLite connection.
        for operation in contract.VALIDATION_PREFIXES:
            instance=contract.validation_unit(args.job_key,operation)
            child=show(instance,['MainPID','ControlPID','ActiveState','ControlGroup'])
            if (child['MainPID']!='0' or child['ControlPID']!='0'
                    or child['ActiveState'] not in {'inactive','failed'} or child['ControlGroup']!=''):
                raise ValueError('user_recovery_child_still_present')
        rpc=show('eimemory-rpc.service',['ActiveState','MainPID'])
        if rpc['ActiveState']!='active' or not rpc['MainPID'].isdigit() or int(rpc['MainPID'])<=0:
            raise ValueError('user_recovery_rpc_not_active')
        healthy_runtime(uid,current.name)
        if (reviewed(authority/'runtime-manager.v1.json',args.expected_registry_sha256,65536)!=raw
                or reviewed(state/'active-transition.json',args.expected_transition_sha256,65536)!=transition_raw
                or helper.read_file(state/'jobs'/(args.job_key+'.json'))!=job_raw):
            raise ValueError('user_recovery_authority_changed')
        capture_path=state/'manual-recovery-capture.json'
        previous_capture=helper.read_file(capture_path) if os.path.lexists(capture_path) else None
        pending=False;history=None
        if previous_capture is not None:
            prior_capture=contract.strict_json(previous_capture)
            if (prior_capture.get('source_job_key')==args.job_key
                    and prior_capture.get('source_job_sha256')==sha256(job_raw).hexdigest()
                    and re.fullmatch(r'manual-abandon-[0-9]+',prior_capture.get('history_basename',''))):
                history=state/'managed-incident-receipts'/prior_capture['history_basename']
                helper.trusted_directory(history,private=True)
                expected_capture=recovery_capture(contract,record,job_raw,native,current.name,
                    history.name,prior_capture.get('enabled_before'))
                if canonical(expected_capture)!=previous_capture:
                    raise ValueError('user_recovery_pending_capture_changed')
                pending=True
        if history is None:
            history=state/'managed-incident-receipts'/('manual-abandon-'+str(time.time_ns()))
            directory(history,uid,0o700)
        if not pending:
            record,job_raw=pin_failed_terminal(helper,contract,state,record,job_raw,native,history,uid,environment)
        for name,data in (('registry.original',raw),('job.original',job_raw),('transition.original',transition_raw),
                          ('native-exit.json',canonical(native))):archive_write(history/name,data,uid)
        enabled_before=record.get('enabled_before',{})
        enabled_sources={'source':'original_job','job_sha256':sha256(job_raw).hexdigest()}
        if not enabled_before:
            # The first migration predates enabled_before capture. Its private
            # pre-migration native audit remains the original source, including
            # enabled gateways temporarily disabled by the migration operator.
            originals={};audits=[]
            for kind,audit,basename in (
                    ('user_units',state/'managed-incident-receipts/user-native-before.initial.json',
                     'enabled-before-user-audit.original'),
                    ('system_units',state/'managed-incident-receipts/migration-import-20261022/host-custody.json',
                     'enabled-before-system-audit.original')):
                if audit.exists():
                    audit_raw=helper.read_file(audit);originals[kind]=contract.strict_json(audit_raw)
                    archive_write(history/basename,audit_raw,uid)
                    audits.append({'kind':kind,'basename':basename,'sha256':sha256(audit_raw).hexdigest()})
            enabled_before=migration_enabled_capture(contract,record,originals.get('user_units'),originals.get('system_units'))
            enabled_sources=({'source':'original_migration_native_audits','audits':audits}
                if audits else {'source':'unavailable','claimed_enabled_units':[]})
        archive_write(history/'enabled-before-sources.json',canonical(enabled_sources),uid)
        capture=recovery_capture(contract,record,job_raw,native,current.name,history.name,enabled_before)
        capture_raw=canonical(capture)
        if previous_capture is not None and not pending:
            reconcile_consumed_capture(helper,contract,state,record,history,uid)
        elif pending and capture_raw!=previous_capture:
            raise ValueError('user_recovery_pending_capture_changed')
        archive_write(history/'recovery-capture.json',capture_raw,uid)
        if previous_capture is not None and helper.read_file(capture_path)!=previous_capture:
            raise ValueError('user_recovery_capture_changed')
        write(capture_path,capture_raw,uid)
        result={'complete':True,'job_key':args.job_key,'outcome':'operator_abandoned_failed_manual_release',
            'recovery_history':history.name,'failed_deployment_preserved':True,'release_accepted':False,
            'autonomous_repair_credit':False,'runtime_commit':current.name,
            'recovery_capture_sha256':sha256(capture_raw).hexdigest()}
        archive_write(history/'complete.json',canonical(result),uid)
        if (helper.read_file(state/'jobs'/(args.job_key+'.json'))!=job_raw
                or failed_attempt_native(contract,record,environment)!=native
                or reviewed(authority/'runtime-manager.v1.json',args.expected_registry_sha256,65536)!=raw
                or reviewed(state/'active-transition.json',args.expected_transition_sha256,65536)!=transition_raw
                or Path('/opt/eimemory/current').resolve(strict=True)!=current):
            raise ValueError('user_recovery_authority_changed')
        write(state/'active-transition.json',canonical(result),uid)
        print(json.dumps(result))


def main():
    if sys.argv[1:2]==['recover-completed-installation']:
        parser=argparse.ArgumentParser(description=recover_completed_installation.__doc__)
        parser.add_argument('operation',choices=['recover-completed-installation'])
        parser.add_argument('--expected-marker-sha256',required=True)
        parser.add_argument('--expected-completion-sha256',required=True)
        parser.add_argument('--expected-registry-sha256',required=True)
        recover_completed_installation(parser.parse_args())
        return
    if sys.argv[1:2]==['abandon-failed-manual']:
        parser=argparse.ArgumentParser(description=abandon_failed_manual.__doc__)
        parser.add_argument('operation',choices=['abandon-failed-manual'])
        parser.add_argument('--job-key',required=True)
        parser.add_argument('--expected-registry-sha256',required=True)
        parser.add_argument('--expected-transition-sha256',required=True)
        abandon_failed_manual(parser.parse_args())
        return
    if sys.argv[1:2]==['stage-release']:
        parser=argparse.ArgumentParser(description=stage_release.__doc__)
        parser.add_argument('operation',choices=['stage-release'])
        parser.add_argument('--target-commit',required=True)
        parser.add_argument('--attempt-id',required=True)
        parser.add_argument('--expected-registry-sha256',required=True)
        parser.add_argument('--scope-manifest',type=Path,required=True)
        parser.add_argument('--expected-scope-sha256',required=True)
        parser.add_argument('--prior-job-key')
        parser.add_argument('--revalidate-current-after')
        stage_release(parser.parse_args())
        return
    parser=argparse.ArgumentParser(description=__doc__)
    for name in ('bundle','registry-seed','runtime-settings','managed-manifest'):
        parser.add_argument('--'+name,type=Path,required=True)
        parser.add_argument('--expected-'+name+'-sha256',required=True)
    parser.add_argument('--expected-existing-registry-sha256',default='')
    parser.add_argument('--expected-knowledge-sha256',required=True)
    parser.add_argument('--expected-managed-key-sha256',required=True)
    parser.add_argument('--expected-activation-key-sha256',required=True)
    install(parser.parse_args())


if __name__=='__main__':main()
