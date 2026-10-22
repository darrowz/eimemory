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
    directory(path.parent,uid,0o750)
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


def install(args):
    if os.name!='posix' or os.geteuid()<=0 or os.getuid()!=os.geteuid():
        raise ValueError('user_install_nonroot_identity_required')
    import pwd
    account=pwd.getpwuid(os.geteuid());uid=account.pw_uid;home=Path(account.pw_dir)
    if account.pw_gid!=os.getegid():raise ValueError('user_install_primary_group_required')
    environment={**os.environ,'XDG_RUNTIME_DIR':'/run/user/'+str(uid),
        'DBUS_SESSION_BUS_ADDRESS':'unix:path=/run/user/'+str(uid)+'/bus'}
    native=subprocess.run(['/usr/bin/systemctl','--user','show','eimemory-runtime-manager.service',
        '--property=ActiveState','--value'],env=environment,capture_output=True,text=True,timeout=20)
    if native.returncode or native.stdout.strip() not in {'inactive','failed'}:
        raise ValueError('user_install_controller_must_be_stopped')
    files,manifest=bundle_files(args.bundle,args.expected_bundle_sha256,uid,account.pw_gid)
    seed=strict(reviewed(args.registry_seed,args.expected_registry_seed_sha256,65536))
    settings=reviewed(args.runtime_settings,args.expected_runtime_settings_sha256,65536)
    managed_raw=reviewed(args.managed_manifest,args.expected_managed_manifest_sha256,1048576)
    managed=strict(managed_raw)
    commit=manifest['controller_commit'];runtime=seed['runtime_commit']
    prior_path=home/'.config/eimemory/control-plane/managed-code-incident-manifest.v1.json'
    prior=strict(reviewed(prior_path,seed['managed_incident_authority']['manifest_sha256'],1048576))
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
    directory(socket,uid,0o700)
    # Reinstallation needs an exact current-generation CAS and no live effect.
    registry_path=authority/'runtime-manager.v1.json'
    if registry_path.exists():
        if not args.expected_existing_registry_sha256:
            raise ValueError('user_install_existing_registry_cas_required')
        reviewed(registry_path,args.expected_existing_registry_sha256)
        transition=state/'active-transition.json'
        if transition.exists() and strict(transition.read_bytes()).get('complete') is not True:
            raise ValueError('user_install_active_transition_preserved')
    elif args.expected_existing_registry_sha256:
        raise ValueError('user_install_existing_registry_missing')
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
    os.environ['EIMEMORY_CONTROL_PLANE']='user'
    with tempfile.TemporaryDirectory(prefix='.user-controller-review-',dir=code.parent) as staging:
        staged=Path(staging)
        for name in ('runtime_manager_contract.py','user_control_plane.py','runtime_manager_settings.py'):
            write(staged/name,files[name],uid)
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
    # Backups share the key/ledger's inaccessible mount boundary in products.
    import tarfile
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
    print(json.dumps({'ok':True,'control_plane':'user','runtime_uid':uid,
        'controller_commit':commit,'runtime_commit':runtime,'registry_generation':registry['generation'],
        'serving_link_changed':False,'services_started':False,'release_accepted':False}))




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
    os.environ['EIMEMORY_CONTROL_PLANE']='user'
    def load(name,source):
        expected=declared['compatibility_hashes'][source]
        data=reviewed(code/name,expected,1024*1024)
        spec=importlib.util.spec_from_file_location('user_stage_'+name.removesuffix('.py'),code/name)
        module=importlib.util.module_from_spec(spec)
        exec(compile(data,str(code/name),'exec'),module.__dict__)
        return module
    helper=load('user_control_plane.py','eimemory/contracts/user_control_plane.py')
    for path in (authority,state,code):helper.trusted_directory(path)
    contract=load('runtime_manager_contract.py','eimemory/contracts/runtime_manager.py')
    registry=contract.validate_registry(contract.strict_json(raw))
    if (registry['runtime_uid']!=uid or registry['runtime_gid']!=account.pw_gid
            or registry['runtime_user']!=account.pw_name
            or registry['controller_commit']!=args.target_commit
            or registry['runtime_commit']==args.target_commit):
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
                 '.storage-release-transaction.json.recovery'):
        if os.path.lexists(Path('/var/lib/eimemory/state')/name):
            raise ValueError('user_stage_storage_recovery_not_clean')
    environment={'PATH':'/usr/bin:/bin','LC_ALL':'C','EIMEMORY_CONTROL_PLANE':'user',
        'XDG_RUNTIME_DIR':'/run/user/'+str(uid),
        'DBUS_SESSION_BUS_ADDRESS':'unix:path=/run/user/'+str(uid)+'/bus'}
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
        active=state/'active-transition.json'
        if os.path.lexists(active) and contract.strict_json(helper.read_file(active)).get('complete') is not True:
            raise ValueError('user_stage_existing_transition_preserved')
        pair={'base_commit':registry['runtime_commit'],'candidate_commit':args.target_commit,
            'controller_commit':registry['controller_commit'],'validation_controller_commit':args.target_commit,
            'scope':scope}
        pointer=state/('manual-release-'+contract.digest(pair)+'.json')
        if os.path.lexists(pointer):raise ValueError('user_stage_existing_attempt_preserved')
        request={'schema':'eimemory.manual_release_permit.v2','operation':'deploy',**pair,
            'target_commit':args.target_commit,'reviewed_attempt_id':args.attempt_id}
        key=contract.digest(request);contract.validate_manual_permit(request,key)
        record={'job_key':key,'request':request,'registry':registry,'manual_release':True,
            'phase':'submitted','unit':contract.job_unit(key),'active_before':None,
            'quiesced':False,'resumed':False,'approved_retry_of':None}
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
        exclusive(pointer,{'job_key':key,'attempt_id':args.attempt_id})
    finally:os.close(lock)
    # A start error leaves both durable records intact: unknown starts must
    # never be resubmitted under another nonce without reviewed recovery.
    subprocess.run(['/usr/bin/systemctl','--user','start','--no-block',record['unit']],
        env=environment,capture_output=True,text=True,timeout=30,check=True)
    print(json.dumps({'ok':True,'job_key':key,'unit':record['unit'],'state':'submitted',
        'release_accepted':False,'autonomous_repair_credit':False}))


def main():
    if sys.argv[1:2]==['stage-release']:
        parser=argparse.ArgumentParser(description=stage_release.__doc__)
        parser.add_argument('operation',choices=['stage-release'])
        parser.add_argument('--target-commit',required=True)
        parser.add_argument('--attempt-id',required=True)
        parser.add_argument('--expected-registry-sha256',required=True)
        parser.add_argument('--scope-manifest',type=Path,required=True)
        parser.add_argument('--expected-scope-sha256',required=True)
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
