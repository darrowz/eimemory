"""Publication expectations derive from custody originals, never current scans."""
from copy import deepcopy
from hashlib import sha256
import io
import json
import os
from pathlib import Path
import tarfile
from types import SimpleNamespace

import pytest

from test_user_release_recovery import installer


def fixture():
    module=installer();home=Path('/operator');uid=1001
    authority=home/'.config/eimemory/control-plane';units=home/'.config/systemd/user'
    files={'runtime-policy.conf':b'[Service]\nEnvironment=COMMIT=@COMMIT@\n',
        'templates/eimemory-rpc.service':b'[Service]\nExecStart=/bin/true\n',
        'eimemory-runtime-manager.service':b'[Service]\nExecStart=/bin/controller\n',
        'helper.py':b'new helper'}
    registry={'runtime_commit':'b'*40,'controller_commit':'a'*40,'runtime_user':'operator','runtime_gid':1001,
        'template_hashes':{'eimemory-rpc.service':'0'*64},
        'units':{'eimemory-rpc.service':{'installed':True},'hermes-gateway.service':{'installed':True}}}
    originals={'authority/identity/policy.json':(b'{"schema":"identity_operator_policy.v2"}',0o640),
        'authority/foreign/retained.json':(b'original authority',0o600),
        'code/legacy.py':(b'original legacy helper',0o640),
        'user-units/eimemory-rpc.service.d/99-local.conf':(b'original custom policy',0o664),
        'user-units/hermes-gateway.service.d/99-business.conf':(b'native gateway business',0o664)}
    contract=SimpleNamespace(canonical=module.canonical)
    policy=SimpleNamespace(render_gateway_integration=lambda *a,**kw:b'gateway integration only')
    def archive(entries=None):
        output=io.BytesIO()
        with tarfile.open(fileobj=output,mode='w') as tar:
            for name,(raw,mode) in (originals if entries is None else entries).items():
                item=tarfile.TarInfo(name);item.size=len(raw);item.mode=mode;item.uid=uid
                tar.addfile(item,io.BytesIO(raw))
        return output.getvalue()
    def expected(*,entries=None,repairs=()):
        return module.published_installation_expectations(archive(entries),home,uid,files,registry,b'{}',b'{}',
            contract,policy,metadata_repairs=repairs)
    return SimpleNamespace(module=module,home=home,authority=authority,units=units,originals=originals,
        expected=expected,archive=archive,registry=registry,uid=uid)


def test_expected_publication_preserves_all_custody_bytes_and_excludes_native_gateway_policy():
    f=fixture();expected=f.expected()
    assert expected[str(f.authority/'foreign/retained.json')]==dict(sha256=sha256(b'original authority').hexdigest(),mode=0o600,uid=1001)
    assert expected[str(f.units/'eimemory-rpc.service.d/99-local.conf')]['mode']==0o664
    assert str(f.units/'hermes-gateway.service.d/99-business.conf') not in expected
    assert expected[str(f.units/'hermes-gateway.service.d/zzzzzz-eimemory-user-control-plane.conf')]['mode']==0o600
    assert expected[str(f.units/'eimemory-rpc.service')]['mode']==0o644


def test_only_exact_reviewed_metadata_change_can_tighten_original_ei_dropin():
    f=fixture();path=f.units/'eimemory-rpc.service.d/99-local.conf'
    repair=dict(path=str(path),sha256=sha256(b'original custom policy').hexdigest(),before_mode=0o664,after_mode=0o644,uid=1001)
    expected=f.expected(repairs=[repair]);assert expected[str(path)]['mode']==0o644
    assert expected[str(path)]['sha256']==repair['sha256']
    assert f.expected()[str(path)]['mode']==0o664


@pytest.mark.parametrize('change',['gateway','authority','hash','uid','before','after','duplicate'])
def test_metadata_repair_never_expands_scope_or_weakens_permissions(change):
    f=fixture();repair=dict(path=str(f.units/'eimemory-rpc.service.d/99-local.conf'),
        sha256=sha256(b'original custom policy').hexdigest(),before_mode=0o664,after_mode=0o644,uid=1001)
    if change=='gateway':repair['path']=str(f.units/'hermes-gateway.service.d/99-business.conf')
    elif change=='authority':repair['path']=str(f.authority/'foreign/retained.json')
    elif change=='hash':repair['sha256']='0'*64
    elif change=='uid':repair['uid']=0
    elif change=='before':repair['before_mode']=0o600
    elif change=='after':repair['after_mode']=0o666
    repairs=[repair,deepcopy(repair)] if change=='duplicate' else [repair]
    with pytest.raises(ValueError,match='metadata_repair_invalid'):f.expected(repairs=repairs)


@pytest.mark.parametrize('alias',['authority/./x','authority//x','authority/../x','/authority/x'])
def test_before_tar_aliases_cannot_overwrite_another_expected_file(alias):
    f=fixture()
    with pytest.raises(ValueError,match='backup_unsafe'):
        f.expected(entries={alias:(b'unsafe alias',0o640)})


def test_different_custom_or_foreign_authority_bytes_change_the_complete_expected_graph():
    f=fixture();original=f.expected()
    for name in ('authority/foreign/retained.json','user-units/eimemory-rpc.service.d/99-local.conf'):
        modified=deepcopy(f.originals);modified[name]=(b'different bytes',modified[name][1])
        assert f.expected(entries=modified)!=original
    modified=deepcopy(f.originals);modified['authority/extra.json']=(b'new file',0o640)
    assert set(f.expected(entries=modified))!=set(original)
    modified=deepcopy(f.originals);del modified['authority/foreign/retained.json']
    assert set(f.expected(entries=modified))!=set(original)


@pytest.mark.skipif(os.name!='posix',reason='native interrupted publication protocol')
@pytest.mark.parametrize('fault',['none','upgrade','claimed_old_pin','forged_old_registry',
    'missing_old_registry','missing_registry_before_field','missing_prior_manifest',
    'missing_history','changed_history','changed_history_receipt','history_race'])
def test_finish_initial_publication_preserves_imported_authority_without_inventing_old_registry(tmp_path,monkeypatch,fault):
    import pwd
    module=installer();uid=os.geteuid();gid=os.getegid()
    if uid==0:pytest.skip('unprivileged operator required')
    home=tmp_path/'operator';authority=home/'.config/eimemory/control-plane'
    state=home/'.local/state/eimemory/control-plane';code=home/'.local/lib/eimemory/control-plane'
    units=home/'.config/systemd/user';knowledge=state/'managed-incident-receipts'
    for path in (authority,code,units,knowledge,authority/'identity'):path.mkdir(parents=True,exist_ok=True)
    real_path=Path;account=pwd.getpwuid(uid)
    current=tmp_path/'releases'/('b'*40);current.mkdir(parents=True)
    paths={'/opt/eimemory/current':current,'/opt/eimemory/releases':current.parent}
    monkeypatch.setattr(module,'Path',lambda value:paths.get(str(value),real_path(value)))
    def fixture_directory(path,_uid,mode=0o750):path.mkdir(parents=True,exist_ok=True);path.chmod(mode)
    monkeypatch.setattr(module,'directory',fixture_directory)
    monkeypatch.setattr(module,'stopped_controller',lambda _:None)
    monkeypatch.setattr(module,'verify_controller_sources',lambda *_:None)
    prior=module.canonical({'release_commit':'c'*40,'scopes':[{'user_id':'original-business-user'}]})
    managed=module.canonical({'release_commit':'a'*40,'previous_manifest_digest':sha256(prior).hexdigest(),
        'scopes':json.loads(prior)['scopes']})
    originals={authority/'managed-incident-proof.key':b'm'*32,state/'activation-signing.key':b'a'*32,
        knowledge/'knowledge.sqlite':b'original-nonempty-ledger',authority/'managed-code-incident-manifest.v1.json':prior,
        authority/'identity/policy.json':b'{"schema":"identity_operator_policy.v2"}'}
    if fault in {'upgrade','missing_old_registry'}:originals[authority/'runtime-manager.v1.json']=b'original-registry'
    for path,raw in originals.items():path.write_bytes(raw);path.chmod(0o600 if path.name.endswith('.key') else 0o640)
    files={'eimemory-runtime-manager.service':b'[Service]\nExecStart=/bin/true\n',
        'user_control_plane.py':b'# reviewed helper\n','runtime_manager_contract.py':b'# reviewed contract\n',
        'runtime_manager_user_policy.py':b'# reviewed policy\n','runtime_manager_settings.py':b'# reviewed settings\n',
        'templates/eimemory-rpc.service':b'[Service]\nExecStart=/bin/true\n',
        'runtime-policy.conf':b'[Service]\nEnvironment=TARGET=@COMMIT@\n',
        'controller-entry.json':module.canonical({'sha256':'1'*64,'validation_sha256':'2'*64})}
    files['compatibility.json']=module.canonical({source:sha256(files[name]).hexdigest() for name,source in
        [('user_control_plane.py','eimemory/contracts/user_control_plane.py'),
         ('runtime_manager_contract.py','eimemory/contracts/runtime_manager.py')]})
    reviewed_contract=SimpleNamespace(canonical=module.canonical,validate_registry=lambda value:value,
        digest=lambda value:sha256(module.canonical(value)).hexdigest())
    seed={'runtime_commit':'b'*40,'generation':'f'*64,'runtime_uid':uid,'runtime_gid':gid,'runtime_user':account.pw_name,
        'units':{'eimemory-rpc.service':{'installed':True}},
        'managed_incident_authority':{'manifest_sha256':sha256(prior).hexdigest()}}
    manifest={'controller_commit':'a'*40};settings=b'{}'
    registry=module.published_registry(seed,files,manifest,managed,home,uid,gid,reviewed_contract)
    args=SimpleNamespace(expected_bundle_sha256='e'*64,expected_existing_registry_sha256=
        sha256(b'original-registry').hexdigest() if fault in {'upgrade','missing_old_registry'} else '',
        expected_managed_key_sha256=sha256(b'm'*32).hexdigest(),expected_activation_key_sha256=sha256(b'a'*32).hexdigest(),
        expected_knowledge_sha256=sha256(b'original-nonempty-ledger').hexdigest())
    def run(argv,**kwargs):
        if argv[0]=='/usr/bin/python3':
            raw=files['runtime-policy.conf'].replace(b'@COMMIT@',b'b'*40)
            resets='\n[Service]\n'+''.join(name+'=\n' for name in
                ('ExecStart','ExecStartPre','ExecStartPost','ExecStop','ExecStopPost','ExecCondition'))
            content=resets.encode()+b'ExecStart=/bin/true\n'+raw+('EnvironmentFile='+str(authority/'user-runtime.env')+'\n').encode()
            module.write(units/'eimemory-rpc.service.d/zzzzzz-eimemory-user-control-plane.conf',content,uid,0o600)
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(module.subprocess,'run',run)
    actual_inventory=module.installation_inventory
    monkeypatch.setattr(module,'installation_inventory',lambda *_a,**_k:(_ for _ in ()).throw(OSError('crash before completion seal')))
    with pytest.raises(OSError,match='crash before completion seal'):
        module._publish_installation(args,SimpleNamespace(pw_uid=uid,pw_gid=gid,pw_dir=str(home)),files,manifest,seed,
            settings,managed,prior,registry,reviewed_contract,None,{},originals.get(authority/'runtime-manager.v1.json'),None)
    monkeypatch.setattr(module,'installation_inventory',actual_inventory)
    monkeypatch.setattr(pwd,'getpwuid',lambda _:SimpleNamespace(pw_uid=uid,pw_gid=gid,pw_name=account.pw_name,pw_dir=str(home)))
    monkeypatch.setattr(module.subprocess,'run',lambda *_a,**_kw:pytest.fail('finishing publication replayed an operation'))
    helper=SimpleNamespace(trusted_directory=lambda *_a,**_k:None,
        read_file=lambda path,**kwargs:module.owned_bytes(path,uid,kwargs.get('maximum',65536)))
    modules={'user_control_plane.py':helper,'runtime_manager_contract.py':reviewed_contract,
        'runtime_manager_user_policy.py':SimpleNamespace(),
        'runtime_manager_settings.py':SimpleNamespace(validate=lambda value:None)}
    monkeypatch.setattr(module,'execute_reviewed_module',lambda name,*_a:modules[name])
    monkeypatch.setattr(module,'bundle_files',lambda *_a:(files,manifest))
    for key,raw in [('registry_seed',module.canonical(seed)),('runtime_settings',settings),('managed_manifest',managed)]:
        path=tmp_path/key;path.write_bytes(raw);setattr(args,key,path);setattr(args,'expected_'+key+'_sha256',sha256(raw).hexdigest())
    args.bundle=tmp_path/'reviewed-bundle'
    marker=state/'controller-installation.json';intent=json.loads(marker.read_bytes())
    backup=knowledge/'migration-backups'/intent['backup_basename']
    if fault in {'forged_old_registry','missing_old_registry','missing_prior_manifest'}:
        with tarfile.open(fileobj=io.BytesIO(backup.read_bytes()),mode='r:') as archive:
            entries=[(item,archive.extractfile(item).read() if item.isfile() else None) for item in archive]
        output=io.BytesIO()
        with tarfile.open(fileobj=output,mode='w') as archive:
            for item,raw in entries:
                if fault=='missing_old_registry' and item.name=='authority/runtime-manager.v1.json':continue
                if fault=='missing_prior_manifest' and item.name=='authority/managed-code-incident-manifest.v1.json':continue
                archive.addfile(item,io.BytesIO(raw) if raw is not None else None)
            if fault=='forged_old_registry':
                item=tarfile.TarInfo('authority/runtime-manager.v1.json');item.uid=uid;item.mode=0o640
                raw=b'forged-old-registry';item.size=len(raw);archive.addfile(item,io.BytesIO(raw))
        backup.write_bytes(output.getvalue());intent['backup_sha256']=sha256(output.getvalue()).hexdigest()
    if fault=='claimed_old_pin':args.expected_existing_registry_sha256='d'*64
    if fault=='missing_registry_before_field':del intent['registry_before_sha256']
    retained=list(state.glob('authority-*/managed-manifest.snapshot'))
    assert len(retained)==1
    if fault=='missing_history':retained[0].unlink()
    if fault=='changed_history':retained[0].write_bytes(b'changed original history')
    if fault=='changed_history_receipt':retained[0].with_name('complete.json').write_bytes(b'{}')
    if fault=='history_race':
        real_archive=module.archive_write
        def archive_then_change(path,raw,owner,mode=0o600):
            result=real_archive(path,raw,owner,mode)
            if '.published-finish-' in path.name:retained[0].write_bytes(b'changed during sealing')
            return result
        monkeypatch.setattr(module,'archive_write',archive_then_change)
    marker.write_bytes(module.canonical(intent))
    args.expected_marker_sha256=sha256(marker.read_bytes()).hexdigest()
    args.expected_registry_sha256=sha256((authority/'runtime-manager.v1.json').read_bytes()).hexdigest()
    if fault in {'none','upgrade'}:
        module.finish_published_installation(args)
        assert not marker.exists()
        seal=json.loads(backup.with_suffix('.complete.json').read_bytes())
        assert seal['schema']=='eimemory.user_controller_installation_completion.v2'
        assert seal['release_accepted'] is False and seal['autonomous_repair_credit'] is False
        assert seal['retained_manifest_history']['manifest_sha256']==sha256(prior).hexdigest()
        assert len(list(backup.parent.glob('*.published-finish-*.json')))==1
        assert any(path.read_bytes()==prior for path in state.glob('authority-*/managed-manifest.snapshot'))
        assert (state/'activation-signing.key').read_bytes()==b'a'*32
    else:
        error='user_install_manifest_history_unverified' if fault in {
            'missing_history','changed_history','changed_history_receipt','history_race'} else 'user_install_finish_original_binding_invalid'
        with pytest.raises(ValueError,match=error):module.finish_published_installation(args)
        assert marker.exists() and not backup.with_suffix('.complete.json').exists()
