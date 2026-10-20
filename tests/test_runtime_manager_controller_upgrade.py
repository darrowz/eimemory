"""Controller-only upgrades never restart a business service or reset knowledge."""
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace
import sys

import pytest

from eimemory.contracts import runtime_manager as c
from test_runtime_manager_handoff import module


@pytest.mark.parametrize('fault',['none','link','scope','commit','path','digest'])
def test_upgrade_manifest_requires_exact_current_source_and_prior_link(tmp_path,monkeypatch,fault):
    m=module();commit='b'*40
    prior={'scopes':[{'tenant_id':'t','agent_id':'a','workspace_id':'w','user_id':'u'}]}
    candidate={'release_commit':commit,'source_directory':str(tmp_path/commit),
        'previous_manifest_digest':c.digest(prior),'scopes':prior['scopes'],'enabled':True}
    if fault=='link':candidate['previous_manifest_digest']='0'*64
    if fault=='scope':candidate['scopes']=[]
    if fault=='commit':candidate['release_commit']='a'*40
    if fault=='path':candidate['source_directory']='/unreviewed/checkout'
    raw=c.canonical(candidate);path=tmp_path/'manifest';path.write_bytes(raw)
    args=SimpleNamespace(managed_incident_manifest=path,
        expected_managed_incident_manifest_sha256='0'*64 if fault=='digest' else sha256(raw).hexdigest())
    monkeypatch.setattr(m,'load_contract',lambda:c);monkeypatch.setattr(m,'RELEASES',tmp_path)
    monkeypatch.setattr(m,'read',lambda p,**_:raw if p==path else c.canonical(prior))
    files={'managed_incident_proof_authority.py':b'reviewed source'}
    if fault=='none':assert m.configure_upgrade_inputs(args,files,commit)==sha256(raw).hexdigest()
    else:
        with pytest.raises(ValueError):m.configure_upgrade_inputs(args,files,commit)


@pytest.mark.parametrize('fault',['none','registry','release','dual_owner','template','configure','not_stopped'])
def test_controller_upgrade_preserves_business_owner_and_rolls_back_fixed_code(tmp_path,monkeypatch,fault):
    m=module();commit='b'*40
    source=tmp_path/'releases'/commit;source.mkdir(parents=True)
    state=tmp_path/'state';state.mkdir();root=tmp_path/'root';root.mkdir()
    units=tmp_path/'units';units.mkdir();(units/'eimemory-runtime-manager.service').write_bytes(b'fixed unit')
    path=tmp_path/'registry'
    registry={'runtime_commit':commit,'controller_commit':'a'*40,'generation':'d'*64,
        'template_hashes':{'eimemory-rpc.service':sha256(b'fixed RPC template').hexdigest()},
        'managed_incident_authority':{'enabled':True}}
    original=c.canonical(registry);path.write_bytes(original)
    files={'templates/eimemory-rpc.service':b'fixed RPC template',
        'eimemory-runtime-manager.service':b'fixed unit','runtime_manager_bridge.py':b'new fixed bridge',
        'compatibility.json':b'{}','controller-entry.json':b'{"sha256":null,"validation_sha256":null}'}
    if fault=='template':files['templates/eimemory-rpc.service']=b'different unit'
    args=SimpleNamespace(expected_registry_sha256='0'*64 if fault=='registry' else sha256(original).hexdigest(),
        bundle=tmp_path/'bundle',expected_bundle_sha256='e'*64,
        expected_current_commit='c'*40 if fault=='release' else commit)
    monkeypatch.setattr(m,'STATE',state);monkeypatch.setattr(m,'ROOT',root)
    monkeypatch.setattr(m,'SYSTEM_UNITS',units);monkeypatch.setattr(m,'REGISTRY',path)
    monkeypatch.setattr(m,'CURRENT',source);monkeypatch.setattr(m,'RELEASES',source.parent)
    contract=SimpleNamespace(canonical=c.canonical,strict_json=c.strict_json,digest=c.digest,validate_registry=lambda x:x)
    monkeypatch.setattr(m,'load_contract',lambda:contract)
    monkeypatch.setitem(sys.modules,'pwd',SimpleNamespace(getpwnam=lambda _:SimpleNamespace(pw_gid=1001)))
    old_manifest=b'{"original":true}';manifest_path=Path('/etc/eimemory-code-automation-authority/managed-code-incident-manifest.v1.json')
    monkeypatch.setattr(m,'read',lambda p,**_:old_manifest if Path(p)==manifest_path else b'' if p==args.bundle else Path(p).read_bytes())
    monkeypatch.setattr(m,'bundle',lambda *_:(files,{'controller_commit':commit}))
    monkeypatch.setattr(m,'configure_upgrade_inputs',lambda *_:'e'*64)
    monkeypatch.setattr(m,'storage_clean',lambda:None)
    monkeypatch.setattr(m,'_handoff_jobs',lambda *_args,**kwargs:{'abandoned':None})
    monkeypatch.setattr(m,'healthy_original',lambda _:None)
    snapshot={str(path):{'raw_hex':original.hex()}}
    monkeypatch.setattr(m,'root_snapshot',lambda _:snapshot)
    monkeypatch.setattr(m,'directory',lambda p,**_:Path(p).mkdir(parents=True,exist_ok=True) or Path(p))
    actions=[];stopped={'value':False}
    def call(argv):
        assert argv[-1]=='eimemory-runtime-manager.service'
        actions.append(argv[1]);stopped['value']=argv[1]=='stop'
    monkeypatch.setattr(m,'call',call)
    def native(_a,manager,unit,*_):
        if unit=='eimemory-rpc.service':
            return dict(ActiveState='active' if manager=='system' or fault=='dual_owner' else 'inactive')
        return dict(ActiveState='inactive' if stopped['value'] and fault!='not_stopped' else 'active',
            MainPID='0' if stopped['value'] and fault!='not_stopped' else '123')
    monkeypatch.setattr(m,'_handoff_unit_state',native)
    writes=[]
    def write(p,raw,**_):
        writes.append(str(p))
        if Path(p)!=manifest_path:Path(p).parent.mkdir(parents=True,exist_ok=True);Path(p).write_bytes(raw)
    monkeypatch.setattr(m,'write',write)
    restored=[]
    def restore(value):restored.append(value);path.write_bytes(original)
    monkeypatch.setattr(m,'restore_root_files',restore)
    def configure(_args,value,*_):
        if fault=='configure':raise ValueError('invalid_producer_guard')
        value['managed_incident_authority']={'enabled':True,'reviewed':'new'}
    monkeypatch.setattr(m,'configure_managed_authority',configure)
    if fault=='none':
        m.upgrade_controller(args)
        assert c.strict_json(path.read_bytes())['controller_commit']==commit
        assert actions==['stop','start'] and not restored
    else:
        with pytest.raises(ValueError):m.upgrade_controller(args)
        assert path.read_bytes()==original
        if fault=='configure':assert restored==[snapshot] and actions==['stop','stop','start']
        if fault in {'registry','release','dual_owner','template'}:assert actions==[] and writes==[]
        if fault=='not_stopped':assert not restored and not any(str(root) in p for p in writes)
