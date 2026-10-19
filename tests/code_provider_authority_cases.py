"""Isolated code-only authority faults; no production delegation or L5 claim."""
from copy import deepcopy
from dataclasses import asdict
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace
import json
import os
import subprocess

import pytest

from eimemory.api.runtime import Runtime
from eimemory.models.records import ScopeRef
from eimemory.governance.evolution import code_evolution_provider_authority as authority
from eimemory.governance.evolution.code_evolution_transaction import CodeEvolutionTransactionManager
from eimemory.storage.code_evolution_store import CodeEvolutionStore, CodeEvolutionStoreError, CodeEvolutionConflict


@pytest.fixture
def provider_authority_fixture(tmp_path, monkeypatch):
    owner = ScopeRef('fixture-tenant','fixture-agent','fixture-base::channel::hermes','fixture-business-openid')
    provider = ScopeRef('fixture-tenant','fixture-agent','fixture-base','fixture-operator')
    monkeypatch.setenv('EIMEMORY_AGENT_ID',provider.agent_id)
    monkeypatch.setenv('EIMEMORY_WORKSPACE_ID',provider.workspace_id)
    monkeypatch.setenv('EIMEMORY_USER_ID',provider.user_id)
    monkeypatch.delenv('EIMEMORY_USER_ALIASES',raising=False)
    monkeypatch.delenv('EIMEMORY_FEISHU_OPEN_ID',raising=False)
    monkeypatch.delenv('FEISHU_OPEN_ID',raising=False)
    entry=dict(owner_scope=asdict(owner),provider_scope=asdict(provider),capability_id=authority.CAPABILITY_ID,
        capability_scope='global',operation=authority.OPERATION,profile_key='l5.default',profile_digest='6'*64)
    mapping=dict(schema=authority.DELEGATION_SCHEMA,enabled=True,delegations=[entry])
    delegation_path = tmp_path / 'active-delegation.json'
    delegation_path.write_text(json.dumps(mapping), encoding='utf-8')
    monkeypatch.setattr(authority, 'DELEGATION_PATH', delegation_path)
    monkeypatch.setattr(authority,'_secure_delegations',lambda:deepcopy(mapping))
    def profile(_self,key,*,runtime_scope,capability_scope,**_kwargs):
        scope=asdict(runtime_scope) if isinstance(runtime_scope,ScopeRef) else dict(runtime_scope)
        return dict(profile=dict(profile_key=key,profile_digest=entry['profile_digest']),
            requirements=[dict(capability_id=authority.CAPABILITY_ID)] if scope==asdict(provider) else [])
    monkeypatch.setattr('eimemory.capabilities.profiles.CapabilityProfiles.resolve',profile)
    runtime=Runtime.create(root=tmp_path/'isolated-runtime')
    fixture=SimpleNamespace(owner=owner,provider=provider,mapping=mapping,entry=entry,runtime=runtime,root=tmp_path)
    fixture.snapshot=authority.select_provider_authority(runtime,owner_scope=owner,profile_key='l5.default')
    try:yield fixture
    finally:runtime.close()


def _create(f,identity='isolated-code-authority'):
    value=dict(transaction_id=identity,idempotency_key=identity,scope=asdict(f.owner),
        incident={'incident_id':'fixture-incident'},repository={'root':str(f.root),'ref':'master'},
        origin='system_detector',detector='eimemory.release_closure_failure.v1',
        known_before_detection=None,prior_user_reported=None,manual_bootstrap=None,profile_key='l5.default',
        capability_authority=f.snapshot,source_evidence={'capability_authority':f.snapshot},
        payload={'capability_authority':f.snapshot,'profile_key':'l5.default'})
    return CodeEvolutionTransactionManager(f.runtime).create_detected(value)


@pytest.mark.parametrize('error', [
    ValueError('code_provider_delegation_parent_not_root_controlled'),
    ValueError('code_provider_delegation_contract_invalid'),
    PermissionError('unreadable authority'),
    OSError('authority unavailable'),
])
def test_exact_provider_assessment_does_not_read_cross_scope_authority(tmp_path, monkeypatch, error):
    path = tmp_path / 'delegations.json'
    path.write_text('{}', encoding='utf-8')
    monkeypatch.setattr(authority, 'DELEGATION_PATH', path)
    owner = ScopeRef('tenant', 'agent', 'workspace', 'operator')
    monkeypatch.setattr(authority, 'configured_provider_scope', lambda _owner: asdict(owner))
    calls = []
    def unavailable():
        calls.append('read')
        raise error
    monkeypatch.setattr(authority, '_secure_delegations', unavailable)
    # A same-owner native assessment needs no cross-scope grant. In a user
    # namespace, even valid host-root files may have an unmapped ownership ID.
    assert authority.delegated_provider_authority(SimpleNamespace(), owner_scope=owner,
        profile_key='l5.default') is None
    assert calls == []


@pytest.mark.parametrize('error', [
    ValueError('code_provider_delegation_parent_not_root_controlled'),
    PermissionError('unreadable authority'),
])
def test_cross_scope_assessment_still_requires_readable_protected_authority(tmp_path, monkeypatch, error):
    path = tmp_path / 'delegations.json'
    path.write_text('{}', encoding='utf-8')
    monkeypatch.setattr(authority, 'DELEGATION_PATH', path)
    owner = ScopeRef('tenant', 'agent', 'workspace::channel::hermes', 'business-owner')
    provider = ScopeRef('tenant', 'agent', 'workspace', 'operator')
    monkeypatch.setattr(authority, 'configured_provider_scope', lambda _owner: asdict(provider))
    def unavailable():
        raise error
    monkeypatch.setattr(authority, '_secure_delegations', unavailable)
    monkeypatch.setattr(authority, 'select_provider_authority',
        lambda *_a, **_k: pytest.fail('unverified delegation must not select a provider'))
    with pytest.raises(type(error), match=str(error)):
        authority.delegated_provider_authority(SimpleNamespace(), owner_scope=owner,
            profile_key='l5.default')


def test_absent_optional_delegation_preserves_unconfigured_legacy_reader(tmp_path, monkeypatch):
    monkeypatch.setattr(authority, 'DELEGATION_PATH', tmp_path / 'absent.json')
    monkeypatch.setattr(authority, 'configured_provider_scope',
        lambda *_a: pytest.fail('absent optional delegation must not require provider configuration'))
    monkeypatch.setattr(authority, '_secure_delegations',
        lambda: pytest.fail('absent optional delegation must not be read'))
    owner = ScopeRef('tenant', 'agent', 'workspace', 'operator')
    assert authority.delegated_provider_authority(SimpleNamespace(), owner_scope=owner,
        profile_key='l5.default') is None


def test_code_provider_delegation_never_canonicalizes_business_identity(provider_authority_fixture):
    f=provider_authority_fixture
    from eimemory.identity import canonical_hongtu_user_id
    from eimemory.governance.l5.l5_scope_authority import authorized_capability_scopes
    assert canonical_hongtu_user_id(f.owner.user_id)==f.owner.user_id
    assert f.provider not in authorized_capability_scopes(f.owner)
    assert f.snapshot['owner_scope']==asdict(f.owner) and f.snapshot['provider_scope']==asdict(f.provider)
    assert f.snapshot['authorization_basis']=='root_code_provider_delegation'
    assert authority.verify_provider_authority(f.runtime,f.snapshot,owner_scope=f.owner)==f.snapshot


@pytest.mark.parametrize('damage',['revoke','profile','target','owner','digest','operation'])
def test_code_provider_snapshot_requires_live_exact_authority(provider_authority_fixture,monkeypatch,damage):
    f=provider_authority_fixture;value=deepcopy(f.snapshot)
    if damage=='revoke':f.mapping['delegations']=[]
    elif damage=='profile':f.entry['profile_digest']='7'*64
    elif damage=='target':monkeypatch.setenv('EIMEMORY_WORKSPACE_ID','other-product')
    elif damage=='owner':value['owner_scope']['user_id']='other-user'
    elif damage=='digest':value['digest']='f'*64
    else:value['operation']='other-operation'
    with pytest.raises((ValueError,RuntimeError)):
        authority.verify_provider_authority(f.runtime,value,owner_scope=f.owner)


def test_code_provider_legacy_resolution_remains_exact_owner(provider_authority_fixture,monkeypatch):
    f=provider_authority_fixture;calls=[]
    monkeypatch.setattr('eimemory.adapters.hermes.code_implementation.resolve_code_implementation_provider',
        lambda *_args,**kwargs:calls.append(kwargs) or {'ok':False,'provider_ready':False})
    authority.resolve_provider_authority(f.runtime,owner_scope=f.owner,checked_at='2026-10-19T00:00:00Z')
    assert calls[0]['runtime_scope']==asdict(f.owner)
    authority.resolve_provider_authority(f.runtime,owner_scope=f.owner,checked_at='2026-10-19T00:00:00Z',snapshot=f.snapshot)
    assert calls[1]['runtime_scope']==asdict(f.provider)


def test_code_provider_v3_sqlite_and_real_abort_receipt_preserve_same_snapshot(provider_authority_fixture):
    f=provider_authority_fixture;tx=_create(f);ledger=CodeEvolutionStore(f.runtime.store)
    assert tx['schema_version']=='code_evolution_transaction.v3'
    assert tx['source_evidence']['capability_authority']==tx['payload']['capability_authority']==f.snapshot
    assert authority.transaction_provider_authority(ledger.get_transaction(tx['transaction_id']))==f.snapshot
    manager=CodeEvolutionTransactionManager(f.runtime)
    manager.effect_disabled(tx['transaction_id'],step='isolated-no-effects')
    terminal=ledger.get_terminal_receipt(tx['transaction_id'])
    assert terminal['payload']['capability_authority']==f.snapshot
    assert terminal['outcome']=='aborted_no_external_effect'
    assert ledger.get_transaction(tx['transaction_id'])['manual_bootstrap'] is None


@pytest.mark.parametrize('damage',['source','payload','proposal','source_mirror','changed_source_mirror','all_provider','all_provenance'])
def test_code_provider_v3_rejects_missing_typed_projection(provider_authority_fixture,damage):
    f=provider_authority_fixture;tx=_create(f);ledger=CodeEvolutionStore(f.runtime.store)
    conn=f.runtime.store.sqlite.conn
    row=conn.execute('SELECT payload_json,source_evidence_json FROM code_evolution_transactions WHERE transaction_id=?',(tx['transaction_id'],)).fetchone()
    payload=json.loads(row['payload_json']);source=json.loads(row['source_evidence_json'])
    if damage in {'source','all_provider'}:source.pop('capability_authority')
    if damage in {'payload','all_provider'}:payload.pop('capability_authority')
    if damage in {'proposal','all_provider'}:payload['payload'].pop('capability_authority')
    if damage=='source_mirror':payload['source_evidence'].pop('capability_authority')
    if damage=='changed_source_mirror':payload['source_evidence']['capability_authority']['digest']='f'*64
    if damage=='all_provenance':
        source.pop('incident_provenance');payload['source_evidence'].pop('incident_provenance')
    if damage=='all_provider':payload['source_evidence'].pop('capability_authority')
    conn.execute('UPDATE code_evolution_transactions SET payload_json=?,source_evidence_json=? WHERE transaction_id=?',
        (json.dumps(payload),json.dumps(source),tx['transaction_id']));conn.commit()
    with pytest.raises(CodeEvolutionStoreError):ledger.get_transaction(tx['transaction_id'])


def test_code_provider_cas_cannot_replace_or_remove_snapshot(provider_authority_fixture):
    f=provider_authority_fixture;tx=_create(f);ledger=CodeEvolutionStore(f.runtime.store)
    payload=deepcopy(tx['payload']);payload.pop('capability_authority')
    with pytest.raises(CodeEvolutionConflict):
            ledger.cas_transition(tx['transaction_id'],expected_state=tx['current_state'],
            expected_state_version=tx['state_version'],target_state='DIAGNOSED',updates={'payload_json':payload})
    assert authority.transaction_provider_authority(ledger.get_transaction(tx['transaction_id']))==f.snapshot


def test_code_provider_terminal_snapshot_cannot_be_laundered_with_new_outer_hash(provider_authority_fixture):
    f=provider_authority_fixture;tx=_create(f);ledger=CodeEvolutionStore(f.runtime.store)
    CodeEvolutionTransactionManager(f.runtime).effect_disabled(tx['transaction_id'],step='isolated-no-effects')
    terminal=ledger.get_terminal_receipt(tx['transaction_id']);payload=deepcopy(terminal['payload'])
    payload.pop('capability_authority');digest=authority._digest(payload)
    conn=f.runtime.store.sqlite.conn
    import sqlite3
    with pytest.raises(sqlite3.IntegrityError,match='append-only'):
        conn.execute('UPDATE code_evolution_terminal_receipts SET payload_json=?,receipt_digest=? WHERE transaction_id=?',
            (json.dumps(payload),digest,tx['transaction_id']))
    conn.rollback()
    # Fault injection into this disposable isolated database only. Normal
    # SQLite itself has already refused the mutation above.
    conn.execute('DROP TRIGGER trg_code_evolution_terminal_receipts_no_update')
    conn.execute('UPDATE code_evolution_terminal_receipts SET payload_json=?,receipt_digest=? WHERE transaction_id=?',
        (json.dumps(payload),digest,tx['transaction_id']))
    conn.commit()
    with pytest.raises(CodeEvolutionStoreError,match='provider authority'):ledger.get_terminal_receipt(tx['transaction_id'])


@pytest.mark.parametrize('delegated',[False,True])
def test_code_provider_historical_ads_use_only_pinned_or_legacy_scope(provider_authority_fixture,delegated):
    f=provider_authority_fixture
    from eimemory.governance.l5.l5_reader import _historical_advertisement_evidence_error
    tx=_create(f) if delegated else {**asdict(f.owner), 'source_evidence':{},'payload':{}}
    tx.update(advertisement_id='fixture-ad',advertisement_digest='a'*64)
    calls=[]
    capabilities=SimpleNamespace(advertisement_context=lambda *_args,**kwargs:calls.append(kwargs) or None)
    result=_historical_advertisement_evidence_error(capabilities,transaction_row=tx,runtime_scope=f.provider,
        capability_scope='global',runtime=f.runtime)
    assert result=='terminal_advertisement_unavailable'
    assert calls[0]['runtime_scope']==asdict(f.provider if delegated else f.owner)


def test_code_provider_bridge_manager_and_terminal_bind_one_snapshot(provider_authority_fixture,monkeypatch):
    f=provider_authority_fixture
    from test_code_evolution_bridge_v2 import _TestProvider
    from eimemory.adapters.hermes import code_implementation as module
    from eimemory.governance.evolution.code_evolution_bridge import propose_code_patch_v2
    from eimemory.governance.evolution.code_evolution_test_plans import protected_test_plan_digest
    repo=f.root/'isolated-repo';relative='eimemory/governance/l5/l5_reader.py'
    source=repo/relative;source.parent.mkdir(parents=True);source.write_bytes(b'VALUE = 1\n')
    for args in [('init','--quiet'),('config','core.autocrlf','false'),('remote','add','origin','https://example.com/isolated.git'),
                 ('add','.'),('-c','user.name=Fixture','-c','user.email=fixture@example.com','commit','--quiet','-m','Fixture')]:
        subprocess.run(['git',*args],cwd=repo,check=True,capture_output=True)
    base=subprocess.check_output(['git','rev-parse','HEAD'],cwd=repo,text=True).strip()
    tree=sha256(module.canonical_json([{'path':relative,'sha256':sha256(source.read_bytes()).hexdigest()}]).encode()).hexdigest()
    calls=[];provider=_TestProvider()
    monkeypatch.setattr(module,'CodeImplementationSocketClient',_TestProvider)
    def resolve(_runtime,**kwargs):
        calls.append(kwargs)
        return dict(ok=True,provider_ready=True,provider=provider,implementation_digest=module.IMPLEMENTATION_DIGEST,
            provider_instance_id=module.PROVIDER_INSTANCE_ID,advertisement_id='isolated-ad',advertisement_digest='c'*64,
            catalog_case_id='hongtu_code_implementation_v2',catalog_snapshot_digest='d'*64)
    monkeypatch.setattr(module,'resolve_code_implementation_provider',resolve)
    report=propose_code_patch_v2(f.runtime,transaction_id='isolated-bridge-delegation',request_id='isolated-request',nonce='isolated-nonce',
        incident=dict(incident_id='isolated-incident',incident_digest='a'*64,
            incident_class='l5.product_completion_semantic_misreport',title='Isolated contract',summary='No production proof',
            diagnostic_codes=['isolated'],acceptance_requirements=['proposal_only']),
        scope=asdict(f.owner),repo_root=repo,base_commit=base,base_tree_digest=tree,allowed_files=[relative],
        test_plan_id='l5.product-completion-reporting.v1',test_plan_digest=protected_test_plan_digest('l5.product-completion-reporting.v1'),
        bounds=dict(maximum_files=1,maximum_bytes_per_file=48*1024,maximum_total_bytes=96*1024,maximum_changed_lines=400),
        origin='system_detector',detector='eimemory.release_closure_failure.v1',known_before_detection=None,
        prior_user_reported=None,manual_bootstrap=None,capability_authority=f.snapshot)
    assert report['ok'] is True,report
    assert report['capability_authority']==f.snapshot and calls[0]['runtime_scope']==asdict(f.provider)
    result=CodeEvolutionTransactionManager(f.runtime).submit_proposal(report,scope=asdict(f.owner),effects_enabled=False)
    tx=result['transaction'];assert {key:tx[key] for key in asdict(f.owner)}==asdict(f.owner)
    assert authority.transaction_provider_authority(tx)==f.snapshot and tx['manual_bootstrap'] is None
    terminal=CodeEvolutionStore(f.runtime.store).get_terminal_receipt(tx['transaction_id'])
    assert terminal['payload']['capability_authority']==f.snapshot
    assert terminal['outcome']=='aborted_no_external_effect'


def test_code_provider_live_authority_revocation_never_changes_immutable_source(provider_authority_fixture):
    f=provider_authority_fixture;tx=_create(f);ledger=CodeEvolutionStore(f.runtime.store)
    from eimemory.governance.evolution.code_evolution_effects import _live_provider_authority_error
    before=deepcopy(ledger.get_transaction(tx['transaction_id']))
    f.mapping['delegations']=[]
    assert _live_provider_authority_error(f.runtime,tx,{})=='code_evolution_provider_authority_unverified'
    assert ledger.get_transaction(tx['transaction_id'])==before


@pytest.mark.parametrize('fault',['none','runtime','scope','receipt','revoked'])
def test_code_provider_terminal_reader_revalidates_pinned_authority(provider_authority_fixture,fault):
    f=provider_authority_fixture
    from eimemory.adapters.hermes import code_implementation as provider
    from eimemory.governance.l5.l5_reader import _qualifying_ledger_evidence_error
    tx=_create(f)
    tx.update(current_state='SUCCEEDED_SEDIMENTED',terminal_receipt_digest='e'*64,
        capability_id=provider.CAPABILITY_ID,revision_id=provider.REVISION_ID,binding_id=provider.BINDING_ID,
        provider_kind=provider.PROVIDER_KIND,provider_instance_id=provider.PROVIDER_INSTANCE_ID,
        implementation_digest='a'*64,advertisement_id='fixture-ad',advertisement_digest='b'*64)
    receipt=dict(transaction_id=tx['transaction_id'],receipt_digest='e'*64,outcome='succeeded_sedimented',
        payload=dict(capability_authority=f.snapshot))
    runtime=None if fault=='runtime' else f.runtime
    scope=f.owner if fault=='scope' else f.provider
    if fault=='receipt':receipt['payload'].pop('capability_authority')
    if fault=='revoked':f.mapping['delegations']=[]
    calls=[]
    capabilities=SimpleNamespace(advertisement_context=lambda *_args,**kwargs:calls.append(kwargs) or None)
    error=_qualifying_ledger_evidence_error(SimpleNamespace(),capabilities=capabilities,transaction_row=tx,
        terminal_receipt=receipt,current_provider=dict(implementation_digest='a'*64),runtime_scope=scope,
        capability_scope='global',runtime=runtime)
    expected=dict(none='terminal_advertisement_unavailable',runtime='terminal_provider_authority_unavailable',
        scope='terminal_provider_authority_scope_mismatch',receipt='terminal_provider_authority_receipt_mismatch',
        revoked='terminal_provider_authority_unverified')
    assert error==expected[fault]
    # This deliberately stops at the missing immutable advertisement. It does
    # not manufacture a qualifying terminal or an L5 completion result.
    assert calls==([] if fault!='none' else [dict(runtime_scope=asdict(f.provider),capability_scope='global')])


@pytest.mark.parametrize('revoked',[False,True])
def test_code_provider_observation_keeps_owner_and_selects_pinned_profile(provider_authority_fixture,monkeypatch,revoked):
    f=provider_authority_fixture
    from eimemory.governance.evolution import code_evolution_effects as effects
    from eimemory.contracts.release_identity import ReleaseIdentity
    from test_code_evolution_effects import _pending_observation_report
    tx=_create(f);tx.update(current_state='OBSERVING',base_commit='a'*40,candidate_commit='b'*40,deployed_commit='b'*40)
    report=_pending_observation_report();report['profile_key']='l5.default'
    report['transaction_evidence'].update({key:tx[key] for key in ('transaction_id','profile_key','current_state','base_commit','candidate_commit','deployed_commit')})
    calls=[];provider_calls=[];receipt_scopes=[]
    monkeypatch.setattr('eimemory.governance.release.evidence_contract.current_release_identity',lambda *_args:ReleaseIdentity('b'*40,'fixture','receipt','session'))
    monkeypatch.setattr('eimemory.governance.l5.l5_reader.build_l5_effective_report',lambda *_args,**kwargs:calls.append(kwargs) or report)
    monkeypatch.setattr('eimemory.adapters.hermes.code_implementation.resolve_code_implementation_provider',
        lambda *_args,**kwargs:provider_calls.append(kwargs) or {})
    monkeypatch.setattr('eimemory.governance.release.deployment_receipt.inspect_immutable_deployment',
        lambda *_args,**kwargs:receipt_scopes.append(kwargs['scope']) or dict(ok=True,receipt_digest='c'*64))
    if revoked:f.mapping['delegations']=[]
    sample=effects.sample_code_evolution_observation(f.runtime,transaction=tx,observed_at='2026-10-19T00:00:00Z')
    if revoked:
        assert sample['ok'] is False and sample['reason']=='observation_authority_unavailable:ValueError'
        assert not calls and not provider_calls and not receipt_scopes
    else:
        assert calls[0]['scope']==asdict(f.owner) and calls[0]['runtime_scope']==asdict(f.provider)
        assert calls[0]['profile_key']==f.snapshot['profile_key']
        assert provider_calls[0]['runtime_scope']==asdict(f.provider) and receipt_scopes==[asdict(f.owner)]
        # The isolated provider is deliberately unready and source novelty is
        # unknown, so selecting a real nonempty Profile cannot certify repair.
        assert sample['health_ok'] is False


@pytest.mark.parametrize('revoke_at',['never','build','archive'])
def test_code_provider_native_assessment_rechecks_authority_and_preserves_owner(provider_authority_fixture,monkeypatch,revoke_at):
    f=provider_authority_fixture
    from eimemory.governance.l5 import native_assessment as native
    from eimemory.contracts.release_identity import ReleaseIdentity
    from test_native_l5_assessment_maintenance import envelope
    value=envelope();value['scope']=asdict(f.owner)
    calls=[]
    def build(**kwargs):
        calls.append(kwargs)
        if revoke_at=='build':f.mapping['delegations']=[]
        return deepcopy(value)
    runtime=SimpleNamespace(store=f.runtime.store,build_l5_readiness_report=build)
    monkeypatch.setattr(native,'current_release_identity',lambda *_args:ReleaseIdentity('b'*40,'fixture','receipt','session'))
    monkeypatch.setattr('eimemory.governance.release.evidence_contract.current_release_identity',lambda *_args:ReleaseIdentity('b'*40,'fixture','receipt','session'))
    append=native.append_learning_record_once
    def archive(*args,**kwargs):
        record=append(*args,**kwargs)
        if revoke_at=='archive':f.mapping['delegations']=[]
        return record
    monkeypatch.setattr(native,'append_learning_record_once',archive)
    result=native.run_native_l5_assessment(runtime,scope=asdict(f.owner))
    assert calls[0]['scope']==asdict(f.owner) and calls[0]['runtime_scope']==f.provider
    assert result['product_l5_complete'] is False and result['ok'] is False
    if revoke_at=='never':
        assert result['execution_ok'] is True and result['archive_verified'] is True
        saved=f.runtime.store.get_by_id(result['record_id'],scope=f.owner,exact_scope=True)
        assert saved.scope==f.owner and saved.content['envelope']['capability_authority']==f.snapshot
        assert saved.content['selector_scope']==asdict(f.provider)
    else:
        assert result['execution_ok'] is False and result['archive_verified'] is False


@pytest.mark.skipif(os.name!='posix' or not hasattr(os,'geteuid') or os.geteuid()!=0,
    reason='native root-owned delegation filesystem boundary')
@pytest.mark.parametrize('damage',['valid','writable','link','hardlink','foreign_owner'])
def test_code_provider_root_mapping_filesystem_boundary(tmp_path,monkeypatch,damage):
    path=tmp_path/'root-authority'/'code-provider-delegations.v1.json';path.parent.mkdir(mode=0o750)
    owner=dict(tenant_id='t',agent_id='a',workspace_id='b::channel::hermes',user_id='logical')
    provider={**owner,'workspace_id':'b','user_id':'operator'}
    mapping=dict(schema=authority.DELEGATION_SCHEMA,enabled=True,delegations=[dict(owner_scope=owner,
        provider_scope=provider,capability_id=authority.CAPABILITY_ID,capability_scope='global',operation=authority.OPERATION,
        profile_key='l5.default',profile_digest='6'*64)])
    path.write_text(json.dumps(mapping));path.chmod(0o640);monkeypatch.setattr(authority,'DELEGATION_PATH',path)
    if damage=='writable':path.chmod(0o660)
    elif damage=='link':
        original=path.with_name('original.json');path.rename(original);path.symlink_to(original)
    elif damage=='hardlink':os.link(path,path.with_name('second.json'))
    elif damage=='foreign_owner':os.chown(path,65534,65534)
    if damage=='valid':assert authority._secure_delegations()==mapping
    else:
        with pytest.raises(ValueError):authority._secure_delegations()


__all__=['provider_authority_fixture']+[name for name in globals() if name.startswith('test_')]
