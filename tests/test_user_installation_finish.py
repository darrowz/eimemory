"""Publication expectations derive from custody originals, never current scans."""
from copy import deepcopy
from hashlib import sha256
import io
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
