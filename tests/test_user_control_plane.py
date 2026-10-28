"""Rootless authority keeps descriptor, namespace and provenance boundaries."""
from hashlib import sha256
import importlib.util
import json
import os
from pathlib import Path
import stat
from types import SimpleNamespace
import zipfile

import pytest

from eimemory.contracts import user_control_plane as authority

ROOT=Path(__file__).resolve().parents[1]
POSIX=pytest.mark.skipif(os.name!='posix',reason='native owner and descriptor contract')


def standalone(name):
    path=ROOT/'deploy'/(name+'.py')
    spec=importlib.util.spec_from_file_location('test_user_'+name,path)
    module=importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_invalid_mode_never_selects_a_fallback(monkeypatch):
    monkeypatch.setenv(authority.MODE_ENV,'arbitrary')
    with pytest.raises(ValueError,match='mode_invalid'):authority.user_mode()


@POSIX
def test_coordinates_ignore_caller_home_and_runtime_directory(monkeypatch):
    import pwd
    account=pwd.getpwuid(os.geteuid())
    if account.pw_uid==0:pytest.skip('requires unprivileged operator')
    monkeypatch.setenv('HOME','/attacker/home')
    monkeypatch.setenv('XDG_RUNTIME_DIR','/attacker/runtime')
    value=authority.coordinates()
    assert value['authority']==Path(account.pw_dir)/'.config/eimemory/control-plane'
    assert value['socket']==Path('/run/user')/str(account.pw_uid)/'eimemory-manager/bridge.sock'


@POSIX
def test_authority_reader_uses_user_owned_exact_bytes(tmp_path):
    if os.geteuid()==0:pytest.skip('requires unprivileged operator')
    target=tmp_path/'policy.json';target.write_bytes(b'{"enabled":true}');target.chmod(0o600)
    assert authority.read_file(target)==b'{"enabled":true}'


@POSIX
@pytest.mark.parametrize('fault',['symlink','ancestor_symlink','hardlink','writable','fifo','oversized','foreign'])
def test_authority_reader_rejects_unsafe_inputs(tmp_path,monkeypatch,fault):
    if os.geteuid()==0:pytest.skip('requires unprivileged operator')
    parent=tmp_path/'sealed';parent.mkdir(mode=0o700)
    target=parent/'policy.json';target.write_bytes(b'{}');target.chmod(0o600)
    if fault=='symlink':
        real=parent/'real';target.rename(real);target.symlink_to(real)
    elif fault=='ancestor_symlink':
        alias=tmp_path/'alias';alias.symlink_to(parent,target_is_directory=True);target=alias/target.name
    elif fault=='hardlink':os.link(target,parent/'alias')
    elif fault=='writable':target.chmod(0o666)
    elif fault=='fifo':target.unlink();os.mkfifo(target,0o600)
    elif fault=='oversized':target.write_bytes(b'x'*65537)
    elif fault=='foreign':
        current=authority.account()
        monkeypatch.setattr(authority,'account',lambda:SimpleNamespace(pw_uid=current.pw_uid+1,pw_dir=current.pw_dir))
    with pytest.raises((ValueError,OSError)):authority.read_file(target)


@POSIX
def test_user_bundle_keeps_bus_and_signing_material_out_of_products(tmp_path):
    if os.geteuid()==0:pytest.skip('requires unprivileged operator')
    import pwd
    module=standalone('build_runtime_manager_bundle')
    native=pwd.getpwuid(os.geteuid())
    home=tmp_path/'operator';home.mkdir(mode=0o750)
    account=SimpleNamespace(pw_uid=native.pw_uid,pw_gid=native.pw_gid,pw_name=native.pw_name,pw_dir=str(home))
    files=module.build_user(ROOT,'a'*40,account=account)
    manifest=json.loads(files['manifest.json'])
    assert manifest['schema']=='eimemory.user_runtime_manager_bundle.v1'
    assert manifest['files']=={name:sha256(raw).hexdigest() for name,raw in files.items() if name!='manifest.json'}
    assert {name for name in files if name.startswith('hermes-policy/')}=={'hermes-policy/hermes-gateway-eimemory.conf'}
    assert 'ensure_hermes_profile_boundary.py' not in files
    for name,raw in files.items():
        # These are controller inputs, not installed unit/drop-in policies.
        if name.startswith('hermes-policy/'):
            assert raw==(ROOT/'deploy/systemd'/name.removeprefix('hermes-policy/')).read_bytes()
            continue
        if not name.endswith(('.service','.conf')):continue
        text=raw.decode()
        assert not any(line.startswith(('User=','Group=')) for line in text.splitlines())
        assert '/usr/libexec/eimemory-manager' not in text
        assert '/var/lib/eimemory-manager' not in text
        assert 'Environment=EIMEMORY_CONTROL_PLANE=user' in text
        assert 'AmbientCapabilities=\n' in text
        if name=='eimemory-runtime-manager.service':
            assert 'CapabilityBoundingSet=' not in text
            assert 'PrivateTmp=no\n' in text and 'PrivateUsers=no\n' in text and 'PrivatePIDs=no\n' in text
            assert 'NoNewPrivileges=yes\n' in text and 'RestrictAddressFamilies=AF_UNIX\n' in text
            assert not any(line.startswith(('ReadOnlyPaths=','ReadWritePaths=',
                'InaccessiblePaths=','ProtectSystem=','ProtectHome=')) for line in text.splitlines())
            assert 'RuntimeDirectory=eimemory-manager\n' in text
            assert 'RuntimeDirectoryMode=0700\n' in text
            assert 'RuntimeDirectoryPreserve=yes\n' in text
        else:
            assert 'CapabilityBoundingSet=\n' in text
            assert 'Wants=eimemory-runtime-manager.service\n' in text
            assert 'After=eimemory-runtime-manager.service\n' in text
            assert 'PrivatePIDs=yes' in text and 'PrivateUsers=yes' in text
            inaccessible=next(line for line in text.splitlines() if line.startswith('InaccessiblePaths='))
            assert 'UMask=0027' in text
            readonly=[path for line in text.splitlines() if line.startswith('ReadOnlyPaths=')
                      for path in line.partition('=')[2].split()]
            assert any(Path(account.pw_dir).is_relative_to(Path(path)) for path in readonly)
            assert 'ReadWritePaths=-'+account.pw_dir+'/.hermes ' in text
            assert '/bus' in inaccessible and '/systemd/private' in inaccessible
            assert 'managed-incident-proof.key' in inaccessible and 'activation-signing.key' in inaccessible
            handoff='ReadWritePaths=-'+str(home/'.local/state/eimemory/hermes-handoff')+'\n'
            assert (handoff in text)==(name in {'templates/eimemory-code-deployment-job@.service','job-runtime-policy.conf'})


def test_user_bundle_reader_authenticates_the_exact_archive(tmp_path):
    module=standalone('install_user_runtime_manager')
    files={'runtime_manager_contract.py':b'reviewed'}
    manifest={'schema':'eimemory.user_runtime_manager_bundle.v1','controller_commit':'a'*40,
        'runtime_uid':1001,'runtime_gid':1001,'files':{name:sha256(raw).hexdigest() for name,raw in files.items()}}
    output=tmp_path/'bundle.zip'
    with zipfile.ZipFile(output,'w') as archive:
        for name,raw in files.items():archive.writestr(name,raw)
        archive.writestr('manifest.json',json.dumps(manifest))
    actual,checked=module.bundle_files(output,sha256(output.read_bytes()).hexdigest(),1001,1001)
    assert actual==files and checked==manifest
    with pytest.raises(ValueError,match='changed'):
        module.bundle_files(output,'f'*64,1001,1001)


@pytest.mark.parametrize('fault',['traversal','symlink','duplicate','foreign_uid','unbound_file'])
def test_user_bundle_rejects_unreviewed_or_unsafe_members(tmp_path,fault):
    module=standalone('install_user_runtime_manager')
    files={'reviewed.py':b'code'}
    manifest={'schema':'eimemory.user_runtime_manager_bundle.v1','controller_commit':'a'*40,
        'runtime_uid':1002 if fault=='foreign_uid' else 1001,'runtime_gid':1001,
        'files':{name:sha256(raw).hexdigest() for name,raw in files.items()}}
    output=tmp_path/'bad.zip'
    with zipfile.ZipFile(output,'w') as archive:
        for name,raw in files.items():archive.writestr(name,raw)
        archive.writestr('manifest.json',json.dumps(manifest))
        if fault=='traversal':archive.writestr('../outside',b'bad')
        elif fault=='unbound_file':archive.writestr('extra.py',b'bad')
        elif fault=='duplicate':archive.writestr('reviewed.py',b'bad')
        elif fault=='symlink':
            info=zipfile.ZipInfo('link.py');info.external_attr=(stat.S_IFLNK|0o777)<<16
            archive.writestr(info,b'outside')
    with pytest.raises(ValueError):module.bundle_files(output,sha256(output.read_bytes()).hexdigest(),1001,1001)


def test_user_authority_files_cannot_be_self_evolved():
    from eimemory.governance.evolution.code_evolution_path_policy import path_denied_by_self
    for name in ('eimemory/contracts/user_control_plane.py','deploy/install_user_runtime_manager.py',
                 'deploy/prepare_user_release.py'):
        assert path_denied_by_self(name)


@pytest.mark.parametrize('mapping,expected', [('1001 1001 1\n',True),
    ('0 0 4294967295\n',False),('1001 2001 1\n',False),('invalid',False)])
def test_unmapped_system_ancestors_need_exact_native_user_mapping(monkeypatch,mapping,expected):
    monkeypatch.setattr(authority,'account',lambda:SimpleNamespace(pw_dir='/home/operator',pw_uid=1001))
    def read(path,*a,**kw):
        return '65534\n' if str(path).endswith('overflowuid') else mapping
    monkeypatch.setattr(authority.Path,'read_text',read)
    metadata=SimpleNamespace(st_uid=65534)
    assert authority._system_owner(metadata,Path('/home'),1001) is expected
    assert not authority._system_owner(metadata,Path('/home/foreign/authority'),1001)


@POSIX
@pytest.mark.parametrize('damage',['none','archive_scope','archive_digest','journal_requester'])
@pytest.mark.parametrize('kind',['identity','subject-projection'])
def test_user_identity_migration_preserves_original_grants_and_rejects_tampering(tmp_path,monkeypatch,damage,kind):
    if os.geteuid()==0:pytest.skip('requires unprivileged operator')
    from eimemory import identity_authority as identity
    control=tmp_path/'control';control.mkdir(mode=0o750)
    directory=control/kind;directory.mkdir(mode=0o750)
    monkeypatch.setattr(identity,'user_mode',lambda:True)
    monkeypatch.setattr(identity,'coordinates',lambda:{'authority':control})
    scope=dict(tenant_id='t',agent_id='a',workspace_id='w',user_id='u')
    legacy=dict(schema='identity_operator_policy.v1',requester_uid=0,owner_scope=scope,source_ids=['source'])
    manifest=dict(owner_scope=scope,source_id='source',items=[])
    event=dict(operation='authorize',event_id='retained-original',requester_uid=0,
        previous_hash='',policy_digest=identity.digest(legacy),manifest=manifest,
        manifest_digest=identity.digest(manifest),request_text='Original historical test authorization',
        requested_at='2026-10-01T00:00:00+00:00')
    event['event_hash']=identity.digest(event)
    policy=directory/'policy.json';policy.write_text(identity.canonical(legacy));policy.chmod(0o640)
    journal=directory/'requests.jsonl';journal.write_text(identity.canonical(event)+'\n');journal.chmod(0o640)
    original=journal.read_bytes()
    result=identity.migrate_user_policy(directory)
    assert result['historical_requester_ids_rewritten'] is False
    assert journal.read_bytes()==original
    archive=directory/'retained-policies'/(identity.digest(legacy)+'.json')
    if damage=='archive_scope':
        changed={**legacy,'owner_scope':{**scope,'user_id':'other'}}
        archive.write_text(identity.canonical(changed))
    elif damage=='archive_digest':archive.write_text('{}')
    elif damage=='journal_requester':
        changed={**event,'requester_uid':os.geteuid()+1};changed.pop('event_hash')
        changed['event_hash']=identity.digest(changed)
        journal.write_text(identity.canonical(changed)+'\n')
    if damage!='none':
        with pytest.raises(identity.IdentityAuthorityError):
            with identity.snapshot(directory):pass
        return
    with identity.snapshot(directory) as (snapshot,_):
        assert snapshot.active(event['event_id'])==event
        assert snapshot.policy['requester_uid']==os.geteuid()
    revoked=identity.revoke(directory,grant_id=event['event_id'],reason='Explicit user operator test revocation')
    with identity.snapshot(directory) as (snapshot,_):
        assert snapshot.active(event['event_id']) is None
        assert snapshot.events[-1]['requester_uid']==os.geteuid()
        assert snapshot.events[-1]['event_id']==revoked['revocation_id']


@pytest.mark.parametrize('damage', ['missing_inventory', 'changed_console'])
def test_prepared_release_rejects_unbound_console_before_execution(tmp_path, monkeypatch, damage):
    module=standalone('prepare_user_release')
    commit='a'*40
    release=tmp_path/commit
    binary=release/'.venv/bin'
    binary.mkdir(parents=True)
    names=('eimemory','eimemory-qmd','pip','pip3')
    for name in names:(binary/name).write_bytes(b'reviewed-console')
    record={'schema':'eimemory.prepared_user_release.v1','commit':commit,
        'repository_root':str(tmp_path),'release_dir':str(release),
        'source_tree':'reviewed-tree','activated':False,'release_accepted':False,
        'console_script_hashes':{name:sha256(b'reviewed-console').hexdigest() for name in names}}
    if damage=='missing_inventory':record['console_script_hashes']={}
    else:(binary/'eimemory').write_bytes(b'unreviewed-console')
    monkeypatch.setattr(module,'coordinates',lambda:(tmp_path,tmp_path/'cache'))
    monkeypatch.setattr(module,'git',lambda *a:'reviewed-tree')
    executed=[]
    monkeypatch.setattr(module,'run',lambda argv,**kw:executed.append(argv))
    with pytest.raises(ValueError,match='console_script'):
        module.verify(tmp_path,commit,tmp_path,record)
    assert not any('--help' in argv for argv in executed)


@POSIX
@pytest.mark.parametrize('damage', ['missing_key', 'wrong_key', 'empty_ledger', 'pending_wal', 'broken_manifest_chain'])
def test_user_install_preserves_missing_or_unverified_original_authority(tmp_path, monkeypatch, damage):
    if os.geteuid()==0:pytest.skip('requires unprivileged operator')
    import pwd
    import sqlite3
    module=standalone('install_user_runtime_manager')
    native=pwd.getpwuid(os.geteuid())
    home=tmp_path/'operator'
    authority=home/'.config/eimemory/control-plane'
    state=home/'.local/state/eimemory/control-plane'
    knowledge=state/'managed-incident-receipts'
    authority.mkdir(parents=True);knowledge.mkdir(parents=True)
    key=authority/'managed-incident-proof.key'
    activation=state/'activation-signing.key'
    for path in (key,activation):path.write_bytes(b'0123456789abcdef0123456789abcdef');path.chmod(0o600)
    assert len(activation.read_bytes())==32
    database=knowledge/'knowledge.sqlite'
    with sqlite3.connect(database) as connection:
        for table in ('authority_meta','managed_knowledge','managed_receipts','managed_manifest_history'):
            connection.execute('CREATE TABLE '+table+'(value TEXT)')
        if damage!='empty_ledger':connection.execute('INSERT INTO managed_knowledge VALUES (?)',('retained-test-fact',))
    database.chmod(0o600)
    if damage=='missing_key':key.unlink()
    if damage=='pending_wal':database.with_name('knowledge.sqlite-wal').write_bytes(b'uncheckpointed-test-data')
    account=SimpleNamespace(pw_uid=native.pw_uid,pw_gid=native.pw_gid,pw_name=native.pw_name,pw_dir=str(home))
    monkeypatch.setattr(pwd,'getpwuid',lambda _:account)
    monkeypatch.setattr(module.subprocess,'run',lambda *a,**kw:SimpleNamespace(returncode=0,
        stdout='ActiveState=inactive\nMainPID=0\nControlPID=0\nControlGroup=\n'))
    # Replace only native directory placement/current serving-link observations;
    # original-key and ledger authentication and all publication code stay real.
    monkeypatch.setattr(module,'directory',lambda *a,**kw:None)
    real_resolve=Path.resolve
    monkeypatch.setattr(module.Path,'resolve',lambda path,*a,**kw:
        Path('/opt/eimemory/releases')/('b'*40) if str(path)=='/opt/eimemory/current' else real_resolve(path,*a,**kw))
    prior={'schema':'managed_code_incident_manifest.v1','release_commit':'b'*40,'scopes':[]}
    prior_path=authority/'managed-code-incident-manifest.v1.json'
    prior_path.write_text(json.dumps(prior,indent=2)+'\n');prior_path.chmod(0o640)
    raw_digest=sha256(prior_path.read_bytes()).hexdigest()
    canonical_digest=sha256(module.canonical(prior)).hexdigest()
    assert raw_digest!=canonical_digest
    seed={'runtime_commit':'b'*40,'runtime_uid':native.pw_uid,'runtime_gid':native.pw_gid,
        'runtime_user':native.pw_name,'managed_incident_authority':{'manifest_sha256':raw_digest}}
    managed={'release_commit':'a'*40,'scopes':[],
        'previous_manifest_digest':raw_digest if damage=='broken_manifest_chain' else canonical_digest}
    seed_path=tmp_path/'seed.json';seed_path.write_text(json.dumps(seed))
    managed_path=tmp_path/'manifest.json';managed_path.write_text(json.dumps(managed))
    settings=tmp_path/'settings.json';settings.write_text('{}')
    monkeypatch.setattr(module,'bundle_files',lambda *a:({}, {'controller_commit':'a'*40}))
    args=SimpleNamespace(bundle=tmp_path/'bundle',expected_bundle_sha256='d'*64,
        registry_seed=seed_path,expected_registry_seed_sha256=sha256(seed_path.read_bytes()).hexdigest(),
        managed_manifest=managed_path,expected_managed_manifest_sha256=sha256(managed_path.read_bytes()).hexdigest(),
        runtime_settings=settings,expected_runtime_settings_sha256=sha256(settings.read_bytes()).hexdigest(),
        expected_existing_registry_sha256='',expected_knowledge_sha256=sha256(database.read_bytes()).hexdigest(),
        expected_managed_key_sha256='f'*64 if damage=='wrong_key' else sha256(b'0123456789abcdef0123456789abcdef').hexdigest(),
        expected_activation_key_sha256=sha256(activation.read_bytes()).hexdigest())
    before=database.read_bytes()
    if damage=='missing_key':
        with pytest.raises(FileNotFoundError):module.install(args)
    else:
        errors={'wrong_key':'reviewed_input_changed','empty_ledger':'original_knowledge_empty',
            'pending_wal':'original_knowledge_not_checkpointed','broken_manifest_chain':'original_release_binding_invalid'}
        with pytest.raises(ValueError,match=errors[damage]):module.install(args)
    assert database.read_bytes()==before
    assert not (authority/'installation.v1.json').exists()
    assert not (authority/'runtime-manager.v1.json').exists()
    assert not (home/'.local/lib/eimemory/control-plane').exists()


@POSIX
@pytest.mark.parametrize('damage', ['none','writable_root','root_mapped','privileges','capabilities','wide_mapping'])
def test_nested_namespace_system_ancestor_needs_kernel_isolation(monkeypatch, damage):
    monkeypatch.setattr(authority,'account',lambda:SimpleNamespace(pw_dir='/home/operator',pw_uid=1001))
    def read(path,*a,**kw):
        if str(path).endswith('overflowuid'):return '65534'
        if str(path).endswith('uid_map'):return '1001 0 2' if damage=='wide_mapping' else '1001 0 1'
        return ('NoNewPrivs: '+('0' if damage=='privileges' else '1')+'\n'
                +'CapEff: '+('1' if damage=='capabilities' else '0')+'\nCapPrm: 0\nCapAmb: 0\n')
    monkeypatch.setattr(authority.Path,'read_text',read)
    native=authority.os
    monkeypatch.setattr(authority,'os',SimpleNamespace(**{**vars(native),
        'stat':lambda _:SimpleNamespace(st_uid=1001 if damage=='root_mapped' else 65534),
        'statvfs':lambda _:SimpleNamespace(f_flag=0 if damage=='writable_root' else native.ST_RDONLY)}))
    assert authority._system_owner(SimpleNamespace(st_uid=65534),Path('/'),1001) is (damage=='none')
    assert not authority._system_owner(SimpleNamespace(st_uid=65534),Path('/foreign/authority'),1001)


@POSIX
@pytest.mark.parametrize('damage',['root','bad_target','bad_attempt'])
def test_user_stage_rejects_invalid_identity_before_reading_control_state(monkeypatch, damage):
    module=standalone('install_user_runtime_manager')
    if damage=='root':
        monkeypatch.setattr(module.os,'geteuid',lambda:0)
        monkeypatch.setattr(module.os,'getuid',lambda:0)
    elif os.geteuid()==0:pytest.skip('requires unprivileged operator')
    args=SimpleNamespace(target_commit='invalid' if damage=='bad_target' else 'a'*40,
        attempt_id='invalid' if damage=='bad_attempt' else 'b'*32)
    monkeypatch.setattr(module,'reviewed',lambda *a,**kw:pytest.fail('invalid request read controller state'))
    with pytest.raises(ValueError,match={'root':'nonroot_identity','bad_target':'full_target','bad_attempt':'reviewed_attempt'}[damage]):
        module.stage_release(args)


@POSIX
@pytest.mark.parametrize('damage',['none','legacy_active','active_transition','existing_attempt','start_error','scope_digest','current_changed'])
def test_user_stage_binds_one_manual_job_and_preserves_ambiguous_attempts(tmp_path,monkeypatch,damage):
    if os.geteuid()==0:pytest.skip('requires unprivileged operator')
    import pwd
    import subprocess
    from test_runtime_manager import registry as registry_fixture
    from eimemory.contracts import runtime_manager as contract
    module=standalone('install_user_runtime_manager')
    native=pwd.getpwuid(os.geteuid());home=tmp_path/'operator'
    authority=home/'.config/eimemory/control-plane';state=home/'.local/state/eimemory/control-plane'
    code=home/'.local/lib/eimemory/control-plane'
    for path in (authority,state/'jobs',code):path.mkdir(parents=True,mode=0o750)
    account=SimpleNamespace(pw_uid=native.pw_uid,pw_gid=native.pw_gid,pw_name=native.pw_name,pw_dir=str(home))
    monkeypatch.setattr(pwd,'getpwuid',lambda _:account)
    monkeypatch.setenv('EIMEMORY_CONTROL_PLANE','user')
    # Model an operator outside the NNP product namespace. Real descriptor,
    # ownership, directory and digest checks are retained in the fixture copy.
    helper=(ROOT/'eimemory/contracts/user_control_plane.py').read_bytes()+b'\n# Test-only trusted operator context.\ndef user_mode(): return False\n'
    control=(ROOT/'eimemory/contracts/runtime_manager.py').read_bytes()
    for name,raw in (('user_control_plane.py',helper),('runtime_manager_contract.py',control)):
        path=code/name;path.write_bytes(raw);path.chmod(0o640)
    releases=tmp_path/'releases';target=releases/('a'*40);base=releases/('b'*40)
    (target/'deploy').mkdir(parents=True);base.mkdir()
    for name in ('run_code_deployment_job.py','run_code_deployment_validation.py'):
        (target/'deploy'/name).write_bytes(b'reviewed-test-entry')
    current=tmp_path/'current';current.symlink_to(target if damage=='current_changed' else base,target_is_directory=True)
    storage=tmp_path/'storage';storage.mkdir()
    real_path=module.Path
    mapping={'/opt/eimemory/releases':releases,'/opt/eimemory/current':current,'/var/lib/eimemory/state':storage}
    monkeypatch.setattr(module,'Path',lambda value: mapping.get(str(value),real_path(value)))
    value=registry_fixture()
    value.update(runtime_uid=native.pw_uid,runtime_gid=native.pw_gid,runtime_user=native.pw_name,runtime_commit='b'*40)
    value['units']={name:{**item,'manager':'user'} for name,item in value['units'].items()}
    value['compatibility_hashes']['eimemory/contracts/user_control_plane.py']=sha256(helper).hexdigest()
    value['compatibility_hashes']['eimemory/contracts/runtime_manager.py']=sha256(control).hexdigest()
    value['controller_entry_digest']=value['validation_entry_digest']=sha256(b'reviewed-test-entry').hexdigest()
    value['generation']=contract.digest({key:item for key,item in value.items() if key!='generation'})
    registry_path=authority/'runtime-manager.v1.json';registry_path.write_bytes(contract.canonical(value));registry_path.chmod(0o640)
    scope=dict(tenant_id='fixture',agent_id='fixture',workspace_id='fixture',user_id='fixture')
    scope_path=authority/'scope.json';scope_path.write_text(json.dumps({'schema':'eimemory.manual_release_scope.v1','scope':scope}));scope_path.chmod(0o640)
    pair=dict(base_commit='b'*40,candidate_commit='a'*40,controller_commit='a'*40,validation_controller_commit='a'*40,scope=scope)
    pointer=state/('manual-release-'+contract.digest(pair)+'.json')
    active=state/'active-transition.json'
    if damage=='existing_attempt':pointer.write_bytes(b'{"original_attempt":"preserved"}');pointer.chmod(0o640)
    if damage=='active_transition':active.write_bytes(b'{"complete":false,"job_key":"prior"}');active.chmod(0o640)
    preserved=pointer.read_bytes() if pointer.exists() else None
    calls=[]
    def run(argv,**kw):
        calls.append(argv)
        if argv[:2]==['/usr/bin/systemctl','show']:
            running=damage=='legacy_active'
            text=('LoadState=loaded\nActiveState='+('active' if running else 'inactive')
                +'\nMainPID='+('123' if running else '0')+'\nUnitFileState='+('enabled' if running else 'disabled')+'\n')
            return SimpleNamespace(returncode=0,stdout=text)
        if argv[:3]==['/usr/bin/systemctl','--user','start'] and damage=='start_error':
            raise subprocess.CalledProcessError(1,argv)
        return SimpleNamespace(returncode=0,stdout='')
    monkeypatch.setattr(module.subprocess,'run',run)
    args=SimpleNamespace(target_commit='a'*40,attempt_id='c'*32,
        expected_registry_sha256=sha256(registry_path.read_bytes()).hexdigest(),scope_manifest=scope_path,
        expected_scope_sha256='f'*64 if damage=='scope_digest' else sha256(scope_path.read_bytes()).hexdigest())
    if damage=='none':module.stage_release(args)
    else:
        expected=subprocess.CalledProcessError if damage=='start_error' else ValueError
        with pytest.raises(expected):module.stage_release(args)
    records=list((state/'jobs').glob('*.json'))
    starts=[call for call in calls if call[:3]==['/usr/bin/systemctl','--user','start']]
    if damage in {'none','start_error'}:
        assert len(records)==1 and len(starts)==1
        record=json.loads(records[0].read_bytes())
        contract.validate_manual_permit(record['request'],record['job_key'])
        assert record['manual_release'] is True and record['phase']=='submitted'
        assert record['request']['scope']==scope and record['registry']==value
        assert starts[0]==['/usr/bin/systemctl','--user','start','--no-block',record['unit']]
        original=records[0].read_bytes()
        with pytest.raises(ValueError,match='existing_attempt_preserved'):module.stage_release(args)
        assert records[0].read_bytes()==original
        assert len([call for call in calls if call[:3]==['/usr/bin/systemctl','--user','start']])==1
    else:
        assert records==[] and starts==[]
        if preserved is not None:assert pointer.read_bytes()==preserved
        if damage=='active_transition':assert active.read_bytes()==b'{"complete":false,"job_key":"prior"}'


def test_user_first_job_mount_uses_controller_release_before_first_transition(monkeypatch):
    module=standalone('install_user_runtime_manager')
    home=Path('/reviewed-user-units');written={}
    monkeypatch.setattr(module,'directory',lambda *a,**kw:None)
    monkeypatch.setattr(module,'write',lambda path,raw,*a,**kw:written.update({path:raw}))
    files={'templates/eimemory-code-deployment-job@.service':b'job-entry',
        'templates/eimemory-code-validation-receipt@.service':b'validation-entry',
        'templates/eimemory-rpc.service':b'rpc-entry',
        'job-runtime-policy.conf':b'ReadOnlyPaths=/opt/eimemory/releases/@COMMIT@',
        'validation-runtime-policy.conf':b'ReadOnlyPaths=/opt/eimemory/releases',
        'runtime-policy.conf':b'Environment=EIMEMORY_RUNTIME_COMMIT=@COMMIT@'}
    module.install_user_units(files,home,1001,controller_commit='a'*40,runtime_commit='b'*40)
    assert written[home/'eimemory-code-deployment-job@.service.d/90-eimemory-runtime.conf']==b'ReadOnlyPaths=/opt/eimemory/releases/'+b'a'*40
    assert written[home/'eimemory-code-validation-receipt@.service.d/90-eimemory-runtime.conf']==b'ReadOnlyPaths=/opt/eimemory/releases'
    assert written[home/'eimemory-rpc.service.d/90-eimemory-runtime.conf']==b'Environment=EIMEMORY_RUNTIME_COMMIT='+b'b'*40
    assert all(b'@COMMIT@' not in raw for raw in written.values())


@POSIX
def test_prepared_release_directory_mode_survives_service_umask(tmp_path, monkeypatch):
    module=standalone('prepare_user_release')
    # The harness uses a shared /tmp; model protected host ancestors while
    # retaining actual directory creation, descriptor chmod and leaf checks.
    ancestors=set(tmp_path.parents)
    original_lstat=Path.lstat
    def trusted_harness_ancestor(path):
        info=original_lstat(path)
        if path in ancestors:
            return SimpleNamespace(st_mode=info.st_mode&~0o022,st_uid=0)
        return info
    monkeypatch.setattr(Path,'lstat',trusted_harness_ancestor)
    cache=tmp_path/'operator'/'empty-pycache'
    previous=os.umask(0o027)
    try:
        module.private_directory(cache,0o555)
        assert cache.stat().st_mode&0o777==0o555
        assert not any(cache.iterdir())
        cache.chmod(0o550)
        module.private_directory(cache,0o555)
        assert cache.stat().st_mode&0o777==0o550
    finally:
        os.umask(previous)
        cache.chmod(0o750)


@pytest.mark.parametrize('configured',[None,'empty','fixed','identity','legacy','outside'])
def test_user_subject_projection_uses_separate_fixed_authority(tmp_path,monkeypatch,configured):
    from eimemory import identity_authority as identity,subject_projection as projection
    root=tmp_path/'control'
    monkeypatch.setattr(identity,'user_mode',lambda:True)
    monkeypatch.setattr(identity,'coordinates',lambda:{'authority':root})
    monkeypatch.delenv('EIMEMORY_SUBJECT_PROJECTION_AUTHORITY_DIR',raising=False)
    fixed=root/'subject-projection'
    value={'empty':'','fixed':fixed,'identity':root/'identity','legacy':Path('/etc/eimemory-subject-projection'),'outside':tmp_path/'other'}.get(configured)
    if configured in {None,'empty','fixed'}:
        assert projection.projection_authority_directory(value)==fixed
        assert identity.authority_directory(fixed)==fixed
        assert identity.authority_directory()==root/'identity'
    else:
        with pytest.raises(identity.IdentityAuthorityError,match='user_authority_path_fixed'):
            projection.projection_authority_directory(value)


def test_user_subject_projection_ignores_legacy_environment_path(tmp_path,monkeypatch):
    from eimemory import identity_authority as identity,subject_projection as projection
    root=tmp_path/'control'
    monkeypatch.setattr(identity,'user_mode',lambda:True)
    monkeypatch.setattr(identity,'coordinates',lambda:{'authority':root})
    monkeypatch.setenv('EIMEMORY_SUBJECT_PROJECTION_AUTHORITY_DIR','/etc/eimemory-subject-projection')
    assert projection.projection_authority_directory()==root/'subject-projection'


@POSIX
def test_user_manifest_archive_preserves_skipped_release_chain(tmp_path,monkeypatch):
    from eimemory.core import managed_incident_proof_authority as issuer
    module=standalone('install_user_runtime_manager')
    state=tmp_path/'state';state.mkdir()
    def fixture_directory(path,uid,mode=0o750):
        path=Path(path);path.mkdir(parents=True,exist_ok=True);path.chmod(mode)
    monkeypatch.setattr(module,'directory',fixture_directory)
    base={'schema':'managed_code_incident_manifest.v1','qualification_domain':issuer.DOMAIN,
          'enabled':True,'defect_key_semantics':issuer.KEY_SEMANTICS,'previous_manifest_digest':''}
    prior={**base,'previous_manifest_digest':issuer.digest(base),'release_commit':'a'*40}
    raw=(json.dumps(prior,indent=2)+'\n').encode()
    history=module.retain_manifest_history(state,raw,'b'*64,os.geteuid())
    assert (history/'managed-manifest.snapshot').read_bytes()==raw
    assert (history/'complete.json').stat().st_mode&0o777==0o600
    complete=json.loads((history/'complete.json').read_bytes())
    assert complete=={'registry_generation':'b'*64,'manifest_sha256':sha256(raw).hexdigest()}
    monkeypatch.setattr(issuer,'CONTROLLER_HISTORY_DIRECTORY',str(state))
    monkeypatch.setattr(issuer,'_read',lambda path,**kw:Path(path).read_bytes())
    current={**base,'previous_manifest_digest':issuer.digest(prior),'release_commit':'c'*40}
    archive=issuer._reviewed_manifest_archive()
    assert issuer._reviewed_manifest_chain(current,issuer.digest(base),archive)==[prior,current]
    with pytest.raises(ValueError,match='prior_generation_invalid'):
        module.retain_manifest_history(state,raw,'invalid',os.geteuid())
    assert (history/'managed-manifest.snapshot').read_bytes()==raw


def test_user_release_stage_matches_existing_contract_and_preserves_collision(tmp_path,monkeypatch):
    module=standalone('prepare_user_release')
    cleanup=standalone('clean_release_bytecode')
    monkeypatch.setattr(module.secrets,'token_hex',lambda size:'a'*(size*2))
    commit='b'*40
    stage=module.allocate_stage(tmp_path,commit)
    assert cleanup.resolve_release_paths(stage,tmp_path,allow_stage=True)==(stage.resolve(),tmp_path.resolve())
    sentinel=stage/'retained';sentinel.write_bytes(b'original attempt')
    with pytest.raises(FileExistsError):module.allocate_stage(tmp_path,commit)
    assert sentinel.read_bytes()==b'original attempt'

@pytest.mark.parametrize('damage',[None,'deferred_rollback','automatic','resumed','wrong_current','missing_exit','live','wrong_invocation','success','changed_transition'])
def test_failed_manual_recovery_preserves_native_failure_requirement(damage):
    from eimemory.contracts import runtime_manager as contract
    module=standalone('install_user_runtime_manager')
    current='b'*40
    registry={'runtime_commit':current,'units':{'eimemory-rpc.service':{}}}
    request={'schema':'eimemory.manual_release_permit.v2','operation':'deploy',
        'base_commit':current,'candidate_commit':'a'*40,'target_commit':'a'*40,
        'controller_commit':'a'*40,'validation_controller_commit':'a'*40,
        'reviewed_attempt_id':'c'*32,'scope':dict(tenant_id='fixture',agent_id='fixture',workspace_id='fixture',user_id='fixture')}
    key=contract.digest(request)
    record={'job_key':key,'request':request,'registry':registry,'manual_release':True,
        'quiesced':True,'resumed':False,'phase':'publish','unit':contract.job_unit(key),
        'active_before':['eimemory-rpc.service'],'original_invocation':'d'*32}
    transition={'complete':False,'job_key':key}
    native=dict(LoadState='loaded',ActiveState='failed',SubState='failed',Result='exit-code',
        InvocationID='d'*32,MainPID='0',ExecMainStatus='1',ExecMainExitTimestampMonotonic='123',ControlGroup='')
    if damage=='deferred_rollback':record['phase']='resume_deferred_until_terminal'
    if damage=='automatic':record['manual_release']=False
    if damage=='resumed':record['resumed']=True
    if damage=='wrong_current':current='a'*40
    if damage=='missing_exit':native['ExecMainExitTimestampMonotonic']='0'
    if damage=='live':native['MainPID']='123'
    if damage=='wrong_invocation':native['InvocationID']='e'*32
    if damage=='success':native.update(ActiveState='active',SubState='exited',Result='success',ExecMainStatus='0')
    if damage=='changed_transition':transition['job_key']='e'*64
    import copy
    original=copy.deepcopy((registry,record,transition,native))
    if damage not in {None,'deferred_rollback'}:
        with pytest.raises(ValueError):module.validate_failed_manual_recovery(contract,registry,record,transition,native,current)
    else:module.validate_failed_manual_recovery(contract,registry,record,transition,native,current)
    assert (registry,record,transition,native)==original
@POSIX
@pytest.mark.parametrize('damage',[None,'stale_start','wrong_pid','single_namespace','wrong_host','symlink'])
def test_gateway_private_pid_requires_same_native_process_start(tmp_path,damage):
    import sys
    module=(ROOT/'deploy/install_immutable_release.sh').read_text()
    marker='import os,sys,json,stat\n'
    script=marker+module.split(marker,1)[1].split("\n' \"$main_pid\" \"$gateway_pid\" \"$pid_file\"",1)[0]
    script=script.replace('from pathlib import Path\n','')
    process=tmp_path/'123';process.mkdir()
    pids='123 1'
    if damage=='single_namespace':pids='123'
    if damage=='wrong_host':pids='124 1'
    (process/'status').write_text('NSpid:\t'+pids+'\n')
    (process/'stat').write_text('123 (hermes gateway) S '+'0 '*18+'12345 0\n')
    record={'pid':2 if damage=='wrong_pid' else 1,'start_time':12344 if damage=='stale_start' else 12345}
    pid=tmp_path/'gateway.pid';pid.write_text(json.dumps(record))
    if damage=='symlink':
        link=tmp_path/'linked.pid';link.symlink_to(pid);pid=link
    fake_path=lambda value: tmp_path if str(value)=='/proc' else Path(value)
    original=sys.argv
    try:
        sys.argv=['pid-check','123','1',str(pid)]
        with pytest.raises((SystemExit,OSError)) as caught:
            exec(compile(script,'reviewed-gateway-pid-check','exec'),{'Path':fake_path})
    finally:sys.argv=original
    if damage=='symlink':assert isinstance(caught.value,OSError)
    else:assert caught.value.code==(0 if damage is None else 1)
def test_reviewed_operator_module_has_no_product_package_dependency(tmp_path):
    module=standalone('install_user_runtime_manager')
    source=tmp_path/'runtime_manager_contract.py'
    raw=b'assert not __package__, "standalone helper became a product package"\nvalue=42\n'
    source.write_bytes(raw)
    loaded=module.execute_reviewed_module(source.name,source,raw)
    assert loaded.value==42
    assert loaded.__package__==''
