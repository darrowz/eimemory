"""Native SQLite controls remain complete in the presence of unrelated data."""
from contextlib import closing
from copy import deepcopy
from dataclasses import asdict, replace
import json
from types import SimpleNamespace

import pytest

from eimemory.governance.evolution import system_code_repair as router
from eimemory.governance.l5.l5_reader import _code_evolution_evidence, _quality_repair_transaction
from eimemory.governance.learning.quality_gap_intake import QUALITY_GAP_SOURCE
from eimemory.governance.release.closure_verdict import report_digest
from eimemory.models.records import RecordEnvelope, ScopeRef, TimeRef
from eimemory.ops.runtime_identity_drift import detect_runtime_identity_drift, inspect_live_runtime_identity
from eimemory.storage.code_evolution_store import CodeEvolutionStore, CodeEvolutionStoreError, digest_json
from eimemory.storage.jsonl import canonical_payload_json, payload_digest
from eimemory.storage.runtime_store import RuntimeStore

SCOPE=ScopeRef(agent_id='hongrui',workspace_id='embodied',user_id='darrow')
COMMIT='a'*40
EARLY='2026-10-01T00:00:00+00:00'
LATE='2026-10-02T00:00:00+00:00'


def quality_pair(key='recall', *, scope=SCOPE):
    semantic='quality-gap:'+key
    common={'schema':'eimemory.quality_gap.v1','semantic_key':semantic,'target_capability':'memory.recall'}
    gap=RecordEnvelope.create(kind='reflection',title='Quality gap',source=QUALITY_GAP_SOURCE,scope=scope,
        status='active',content=dict(common),meta={**common,'report_type':'quality_gap'})
    gap.record_id='gap-'+key;gap.time=TimeRef(EARLY,EARLY,EARLY)
    resolution=RecordEnvelope.create(kind='reflection',title='Quality resolution',source=QUALITY_GAP_SOURCE,scope=scope,
        status='resolved',content={**common,'resolves_gap_id':gap.record_id,
            'resolution':{'status':'passed','report_digest':'d'*64,'observed_at':LATE}},
        meta={**common,'report_type':'quality_gap_resolution','resolves_gap_id':gap.record_id,
            'report_digest':'d'*64,'resolved_at':LATE,'release_commit':COMMIT,'deployment_receipt_id':'receipt'})
    resolution.record_id='resolution-'+key;resolution.time=TimeRef(LATE,LATE,LATE)
    return gap,resolution


def bulk_records(store, template, count, *, prefix='noise'):
    """Insert complete physical envelopes without unrelated FTS/export work."""
    store.append(template)
    with store._lock:
        original=dict(store.sqlite.conn.execute('SELECT * FROM records WHERE storage_key=?',
            (store.sqlite._storage_key(template),)).fetchone())
        columns=list(original);rows=[]
        for index in range(count):
            item=replace(template,record_id=f'{prefix}-{index:06}',
                time=TimeRef('2099-01-01T00:00:00+00:00','2099-01-01T00:00:00+00:00','2099-01-01T00:00:00+00:00'))
            payload=item.to_dict();row=dict(original)
            row.update(record_id=item.record_id,storage_key=store.sqlite._storage_key(item),
                payload_json=canonical_payload_json(payload),payload_digest=payload_digest(payload),
                created_at=item.time.created_at,updated_at=item.time.updated_at,occurred_at=item.time.occurred_at)
            rows.append(tuple(row[column] for column in columns))
        store.sqlite.conn.executemany('INSERT INTO records ('+','.join(columns)+') VALUES ('+
            ','.join('?' for _ in columns)+')',rows)
        store.sqlite.conn.commit()


def page(store, **kwargs):
    return store.list_records_by_sources_exact_scope(scope=SCOPE,sources=[QUALITY_GAP_SOURCE],kinds=['reflection'],**kwargs)


def test_52_quality_rows_are_not_hidden_by_7600_unrelated_reflections(tmp_path):
    with closing(RuntimeStore(tmp_path)) as store:
        bulk_records(store,RecordEnvelope.create(kind='reflection',title='Unrelated monitor',source='eimemory.scheduler.noise',scope=SCOPE),7600)
        for index in range(26):
            for item in quality_pair(f'{index:02}'):store.append(item)
        assert all(item.source!=QUALITY_GAP_SOURCE for item in store.list_records(kinds=['reflection'],scope=SCOPE,limit=501))
        records=page(store)
        assert len(records)==52 and all(item.scope==SCOPE and item.source==QUALITY_GAP_SOURCE for item in records)
        transaction=_quality_repair_transaction(SimpleNamespace(store=store),runtime_scope=SCOPE)
        assert transaction['quality_resolution_id']=='resolution-25' and transaction['quality_page_complete'] is True
        assert transaction['evidence_verified'] is False
        assert transaction['evidence_error']=='quality_repair_provenance_unproven'
        for field in ('known_before_detection','prior_user_reported','manual_bootstrap','observation_valid'):
            assert transaction[field] is None


@pytest.mark.parametrize('foreign',[replace(SCOPE,user_id=''),replace(SCOPE,agent_id='hongtu'),
    replace(SCOPE,user_id='other'),replace(SCOPE,workspace_id='other'),replace(SCOPE,tenant_id='other')])
def test_exact_owner_never_borrows_shared_or_registry_quality(tmp_path,foreign):
    with closing(RuntimeStore(tmp_path)) as store:
        for item in quality_pair(scope=foreign):store.append(item)
        assert page(store)==[]
        assert _quality_repair_transaction(SimpleNamespace(store=store),runtime_scope=SCOPE) is None


@pytest.mark.parametrize('defect',['source','kind','status','scope','record_id','source_id','digest','empty_digest','missing_status','pointer'])
def test_typed_source_page_does_not_drop_corrupt_hydration_or_physical_mismatch(tmp_path,defect):
    with closing(RuntimeStore(tmp_path)) as store:
        item=quality_pair()[0];store.append(item)
        with store._lock:
            payload=item.to_dict();updates={}
            if defect in ('source','kind','status','record_id','source_id'):payload[defect]='different'
            if defect=='scope':payload['scope']['user_id']='other'
            if defect=='missing_status':payload.pop('status')
            updates.update(payload_json=canonical_payload_json(payload),payload_digest=payload_digest(payload))
            if defect=='digest':updates['payload_digest']='0'*64
            if defect=='empty_digest':updates['payload_digest']=''
            if defect=='pointer':updates['payload_pointer_json']='not-json'
            store.sqlite.conn.execute('UPDATE records SET '+','.join(key+'=?' for key in updates)+' WHERE record_id=?',
                (*updates.values(),item.record_id));store.sqlite.conn.commit()
        with pytest.raises(RuntimeError):page(store)
        result=_quality_repair_transaction(SimpleNamespace(store=store),runtime_scope=SCOPE)
        assert result['evidence_verified'] is False and result['quality_page_complete'] is False


def test_typed_page_hydrates_complete_archived_payload_without_compact_projection_credit(tmp_path):
    with closing(RuntimeStore(tmp_path)) as store:
        item=quality_pair()[0];item.kind='capability_score';item.content['long_body']='private fixture '*2000
        store.append(item)
        assert store.sqlite.conn.execute('SELECT payload_pointer_json FROM records WHERE record_id=?',(item.record_id,)).fetchone()[0]
        result=store.list_records_by_sources_exact_scope(scope=SCOPE,sources=[QUALITY_GAP_SOURCE],kinds=['capability_score'])
        assert result[0].content['long_body']==item.content['long_body']


def test_exact_record_id_filters_before_limit_without_hiding_damage_in_full_page(tmp_path):
    with closing(RuntimeStore(tmp_path)) as store:
        gap,resolution=quality_pair()
        store.append(gap);store.append(resolution)
        with store._lock:
            store.sqlite.conn.execute("UPDATE records SET payload_digest='' WHERE record_id=?",(resolution.record_id,))
            store.sqlite.conn.commit()
        assert [item.record_id for item in page(store,record_ids=[gap.record_id],limit=1)]==[gap.record_id]
        assert page(store,record_ids=['absent'],limit=2)==[]
        with pytest.raises(RuntimeError):page(store)


@pytest.mark.parametrize('limit',[0,502,True,'501'])
def test_typed_page_invalid_bounds_cannot_broaden_or_truncate_silently(tmp_path,limit):
    with closing(RuntimeStore(tmp_path)) as store:
        with pytest.raises(ValueError):page(store,limit=limit)


def test_quality_source_sentinel_is_explicitly_unknown(tmp_path):
    with closing(RuntimeStore(tmp_path)) as store:
        bulk_records(store,quality_pair()[0],501,prefix='quality')
        result=_quality_repair_transaction(SimpleNamespace(store=store),runtime_scope=SCOPE)
        assert result=={'evidence_verified':False,'quality_page_complete':False,
                        'evidence_error':'quality_repair_source_page_incomplete'}


@pytest.mark.parametrize('owner',[None,{}, {**asdict(SCOPE),'user_id':1}, {**asdict(SCOPE),'tenant_id':''}])
def test_exact_record_owner_is_not_inferred_or_normalized(tmp_path,owner):
    with closing(RuntimeStore(tmp_path)) as store:
        with pytest.raises(ValueError):
            store.list_records_by_sources_exact_scope(scope=owner,sources=[QUALITY_GAP_SOURCE],kinds=['reflection'])


@pytest.mark.parametrize('defect',['missing_parent','archived_resolution','reopened','cycle','competing_tip'])
def test_narrow_quality_page_preserves_tip_and_lifecycle_gates(tmp_path,defect):
    with closing(RuntimeStore(tmp_path)) as store:
        gap,resolution=quality_pair()
        if defect!='missing_parent':store.append(gap)
        if defect=='archived_resolution':resolution.status='archived'
        if defect=='cycle':gap.content['supersedes_gap_id']=resolution.record_id;gap.meta['supersedes_gap_id']=resolution.record_id;store.rewrite(gap)
        store.append(resolution)
        if defect=='reopened':
            new=deepcopy(gap);new.record_id='reopen';new.content['supersedes_gap_id']=resolution.record_id
            new.meta['supersedes_gap_id']=resolution.record_id;new.time=TimeRef(LATE,LATE,LATE);store.append(new)
        if defect=='competing_tip':
            other=deepcopy(resolution);other.record_id='second-resolution'
            other.meta['report_digest']='e'*64;other.content['resolution']['report_digest']='e'*64;store.append(other)
        assert _quality_repair_transaction(SimpleNamespace(store=store),runtime_scope=SCOPE) is None


def current_incident(*,commit=COMMIT,scope=SCOPE,status='active'):
    report=detect_runtime_identity_drift(expected_commit=commit,
        unit_environments={'fixture.service':'EIMEMORY_RUNTIME_COMMIT='+('b'*40)},detected_at=EARLY,scope=scope)
    incident=report['incident']
    record=RecordEnvelope.create(kind='incident',title=incident['title'],source='eimemory.runtime_identity_drift',
        scope=scope,status=status,content={**incident,'detector_report':report},
        provenance={'origin':'system_detector','detector':'eimemory.runtime_identity_drift.v1',
                    'known_before_detection':False,'prior_user_reported':False},
        meta={'observation_valid':True,'incident_digest':incident['incident_digest']})
    record.record_id=incident['incident_id']
    return record


def inspect(runtime,*,observed='b'*40,stamp=EARLY):
    return inspect_live_runtime_identity(runtime,scope=SCOPE,detected_at=stamp,
        runner=lambda unit:'EIMEMORY_RUNTIME_COMMIT='+observed if unit=='eimemory-rpc.service' else None)


def router_runtime(store,monkeypatch):
    monkeypatch.setattr(router,'_repository_identity',lambda _:dict(ok=True,base_commit=COMMIT))
    monkeypatch.setattr('eimemory.governance.release.evidence_contract.current_release_identity',
                        lambda *_:SimpleNamespace(commit=COMMIT))
    return SimpleNamespace(store=store)


def test_6074_generic_timer_incidents_cannot_hide_current_eligible_source(tmp_path,monkeypatch):
    with closing(RuntimeStore(tmp_path)) as store:
        runtime=router_runtime(store,monkeypatch)
        current=inspect(runtime)
        bulk_records(store,RecordEnvelope.create(kind='incident',title='Unrelated timer',source='eimemory.timer_monitor',scope=SCOPE),6074)
        reads=[]
        monkeypatch.setattr(router,'_automation_policy_identity',lambda:(reads.append(True) or ('','','')))
        result=router.process_system_code_incidents(runtime,scope=SCOPE,repo_root=tmp_path)
        assert result['reason']=='automation_policy_identity_unavailable'
        assert result['pending_incident_record_ids']==[current['incident_record_id']] and reads==[True]


@pytest.mark.parametrize('case',['empty','old','archived','other_owner','shared_owner','untrusted_diagnosis'])
def test_no_current_trusted_incident_is_idle_before_policy_access(tmp_path,monkeypatch,case):
    with closing(RuntimeStore(tmp_path)) as store:
        runtime=router_runtime(store,monkeypatch)
        if case!='empty':
            item=current_incident(commit='c'*40 if case=='old' else COMMIT,
                scope=replace(SCOPE,user_id='other') if case=='other_owner' else replace(SCOPE,user_id='') if case=='shared_owner' else SCOPE,
                status='archived' if case=='archived' else 'active')
            if case=='untrusted_diagnosis':item.content['detector_report']['status']='diagnosis_required';item.provenance={}
            store.append(item)
        monkeypatch.setattr(router,'_automation_policy_identity',lambda:pytest.fail('idle owner consulted policy'))
        assert router.process_system_code_incidents(runtime,scope=SCOPE,repo_root=tmp_path)=={'ok':True,'status':'idle','processed':[]}


def diagnosis_incident(*,commit=COMMIT,scope=SCOPE,status='active'):
    identity={'schema':'release_closure_incident_identity.v2','detector':'eimemory.release_closure_failure.v1',
              'status':'diagnosis_required','scope':asdict(scope),'incident_class':'release.closure_report_failure',
              'release_commit':commit,'report_digest':'f'*64}
    digest=report_digest(identity)
    incident={'incident_id':'diagnosis-'+digest[:24],'incident_digest':digest,
        'incident_class':'release.closure_report_failure','title':'Bounded diagnosis',
        'summary':'Protected current report requires diagnosis','diagnostic_codes':['unknown_boundary'],
        'acceptance_requirements':['bounded_diagnosis']}
    detector={'schema':'release_closure_failure.v2','origin':'system_detector','detector':'eimemory.release_closure_failure.v1',
        'manual_bootstrap':False,'observation_valid':True,'release_commit':commit,'status':'diagnosis_required',
        'repair_eligible':False,'identity':identity,'report_digest':'f'*64,'incident':incident}
    return RecordEnvelope.create(kind='incident',title='Bounded diagnosis',source='eimemory.release_closure_failure',
        scope=scope,status=status,content={**incident,'detector_report':detector},
        provenance={'origin':'system_detector','detector':'eimemory.release_closure_failure.v1',
                    'known_before_detection':False,'prior_user_reported':False},
        meta={'observation_valid':True,'incident_digest':digest})


@pytest.mark.parametrize('case',['current','old','archived','wrong_scope','wrong_detector','tampered_identity'])
def test_diagnosis_requires_exact_current_native_authority_before_policy(tmp_path,monkeypatch,case):
    with closing(RuntimeStore(tmp_path)) as store:
        runtime=router_runtime(store,monkeypatch)
        item=diagnosis_incident(commit='c'*40 if case=='old' else COMMIT,
            scope=replace(SCOPE,user_id='other') if case=='wrong_scope' else SCOPE,
            status='archived' if case=='archived' else 'active')
        if case=='wrong_detector':item.provenance['detector']='untrusted.detector'
        if case=='tampered_identity':item.content['detector_report']['identity']['report_digest']='e'*64
        store.append(item)
        monkeypatch.setattr(router,'_automation_policy_identity',lambda:pytest.fail('diagnosis or historical input read policy'))
        result=router.process_system_code_incidents(runtime,scope=SCOPE,repo_root=tmp_path)
        assert result['status']==('diagnosis_required' if case=='current' else 'idle')
        assert result['ok'] is (case!='current')


def test_allowed_incident_source_sentinel_blocks_before_policy(tmp_path,monkeypatch):
    with closing(RuntimeStore(tmp_path)) as store:
        runtime=router_runtime(store,monkeypatch)
        bulk_records(store,current_incident(),501,prefix='incident')
        monkeypatch.setattr(router,'_automation_policy_identity',lambda:pytest.fail('incomplete incident page consulted policy'))
        result=router.process_system_code_incidents(runtime,scope=SCOPE,repo_root=tmp_path)
        assert result['status']=='blocked' and result['reason']=='incident_source_page_incomplete'
        assert result['source_page_complete'] is False


@pytest.mark.parametrize('change',['healthy','changed','missing_pointer','corrupt_pointer'])
def test_runtime_routing_uses_current_native_observation_not_old_active_incident(tmp_path,monkeypatch,change):
    from eimemory.ops.runtime_identity_drift import runtime_identity_current_record_id
    with closing(RuntimeStore(tmp_path)) as store:
        runtime=router_runtime(store,monkeypatch)
        first=inspect(runtime)
        raw=store.sqlite.conn.execute('SELECT payload_json FROM records WHERE record_id=?',
            (first['incident_record_id'],)).fetchone()[0]
        second=None
        if change in ('healthy','changed'):
            second=inspect(runtime,observed=COMMIT if change=='healthy' else 'c'*40,stamp=LATE)
        else:
            pointer=runtime_identity_current_record_id(SCOPE,COMMIT)
            with store._lock:
                if change=='missing_pointer':store.sqlite.conn.execute('DELETE FROM records WHERE record_id=?',(pointer,))
                else:store.sqlite.conn.execute("UPDATE records SET source='foreign' WHERE record_id=?",(pointer,))
                store.sqlite.conn.commit()
        reads=[]
        monkeypatch.setattr(router,'_automation_policy_identity',lambda:(reads.append(True) or ('','','')))
        result=router.process_system_code_incidents(runtime,scope=SCOPE,repo_root=tmp_path)
        assert store.sqlite.conn.execute('SELECT payload_json FROM records WHERE record_id=?',
            (first['incident_record_id'],)).fetchone()[0]==raw
        if change=='healthy':assert result=={'ok':True,'status':'idle','processed':[]} and not reads
        elif change=='changed':
            assert result['reason']=='automation_policy_identity_unavailable'
            assert result['pending_incident_record_ids']==[second['incident_record_id']] and reads==[True]
        else:assert result['reason']=='runtime_identity_current_observation_unverified' and not reads


@pytest.mark.parametrize('identity',['legacy','invalid_v2','native_v2'])
def test_unknown_runtime_lifecycle_requires_valid_current_v2_identity(tmp_path,monkeypatch,identity):
    with closing(RuntimeStore(tmp_path)) as store:
        runtime=router_runtime(store,monkeypatch)
        item=current_incident(status='pending')
        if identity=='legacy':
            report=detect_runtime_identity_drift(expected_commit=COMMIT,
                unit_environments={'fixture.service':'EIMEMORY_RUNTIME_COMMIT='+('b'*40)},detected_at=EARLY)
            item.content={**report['incident'],'detector_report':report}
            item.record_id=report['incident']['incident_id'];item.meta['incident_digest']=report['incident']['incident_digest']
        if identity=='invalid_v2':item.content['detector_report']['identity_digest']='0'*64
        store.append(item)
        monkeypatch.setattr(router,'_automation_policy_identity',lambda:pytest.fail('unknown/legacy incident consulted policy'))
        result=router.process_system_code_incidents(runtime,scope=SCOPE,repo_root=tmp_path)
        assert result['status']==('blocked' if identity=='native_v2' else 'idle')
        if identity=='native_v2':assert result['reason']=='current_incident_lifecycle_unverified'


def transaction_payload(name,*,scope=SCOPE,root='/repo',terminal=True):
    return {'transaction_id':name,'idempotency_key':name,'scope':asdict(scope),
        'incident':{'incident_id':'incident-'+name},'repository':{'root':root,'ref':'master'},
        'terminal':terminal,'current_state':'SUCCEEDED_SEDIMENTED' if terminal else 'OBSERVING'}


def evidence(runtime,monkeypatch,root='/repo'):
    monkeypatch.setattr('eimemory.adapters.hermes.code_implementation.resolve_code_implementation_provider',lambda *a,**k:{})
    runtime.code_evolution_current_lineage={'ok':True,'compatible':True}
    return _code_evolution_evidence(runtime,runtime_scope=SCOPE,capability_scope='global',checked_at='',repo_root=root,catalog=None)[1]


def test_quality_uses_report_owner_even_with_explicit_different_registry_scope(tmp_path,monkeypatch):
    with closing(RuntimeStore(tmp_path)) as store:
        registry=replace(SCOPE,agent_id='hongtu')
        for item in quality_pair('logical'):store.append(item)
        for item in quality_pair('registry',scope=registry):store.append(item)
        runtime=SimpleNamespace(store=store,code_evolution_current_lineage={'ok':True,'compatible':True})
        monkeypatch.setattr('eimemory.adapters.hermes.code_implementation.resolve_code_implementation_provider',lambda *a,**k:{})
        _,transaction,_=_code_evolution_evidence(runtime,runtime_scope=registry,evidence_scope=SCOPE,
            capability_scope='global',checked_at='',repo_root='/repo',catalog=None)
        assert transaction['quality_resolution_id']=='resolution-logical'
        assert transaction['evidence_verified'] is False and transaction['known_before_detection'] is None


@pytest.mark.parametrize('with_owner',[False,True])
def test_report_owner_ledger_is_separate_from_provider_selector_and_foreign_user(tmp_path,monkeypatch,with_owner):
    with closing(RuntimeStore(tmp_path)) as store:
        owner=replace(SCOPE,user_id='Hermes')
        ledger=CodeEvolutionStore(store)
        ledger.create_transaction(transaction_payload('selector-terminal'))
        ledger.create_transaction(transaction_payload('foreign-terminal',scope=replace(SCOPE,user_id='other')))
        if with_owner:ledger.create_transaction(transaction_payload('report-owner-active',scope=owner,terminal=False))
        seen=[]
        monkeypatch.setattr('eimemory.adapters.hermes.code_implementation.resolve_code_implementation_provider',
            lambda *a,**k:(seen.append(k['runtime_scope']) or {'provider_ready':True}))
        runtime=SimpleNamespace(store=store,code_evolution_current_lineage={'ok':True,'compatible':True})
        _,transaction,_=_code_evolution_evidence(runtime,runtime_scope=SCOPE,evidence_scope=owner,
            capability_scope='global',checked_at='',repo_root='/repo',catalog=None)
        assert seen==[asdict(SCOPE)]
        if with_owner:assert transaction['transaction_id']=='report-owner-active' and transaction['nonterminal'] is True
        else:assert not transaction.get('transaction_id') and transaction['qualifying_terminal_outcome'] is None


def test_exact_owner_repository_transactions_survive_foreign_and_other_repo_flood(tmp_path,monkeypatch):
    with closing(RuntimeStore(tmp_path)) as store:
        ledger=CodeEvolutionStore(store)
        for index in range(120):
            ledger.create_transaction(transaction_payload('foreign-'+str(index),scope=replace(SCOPE,user_id='other')))
            ledger.create_transaction(transaction_payload('other-repo-'+str(index),root='/other'))
        ledger.create_transaction({**transaction_payload('own-active',terminal=False),'updated_at':EARLY})
        assert all(row['transaction_id']!='own-active' for row in ledger.list_transactions(limit=100))
        assert [row['transaction_id'] for row in ledger.list_transactions_exact_scope(scope=SCOPE,repository_root='/repo')]==['own-active']
        result=evidence(SimpleNamespace(store=store),monkeypatch)
        assert result['transaction_id']=='own-active' and result['nonterminal'] is True
        assert result['ledger_page_complete'] is True


@pytest.mark.parametrize('defect',['sentinel','corrupt_payload','wrong_payload_owner','normalized_payload_owner','wrong_payload_repo'])
def test_incomplete_or_corrupt_ledger_never_falls_back_to_quality(tmp_path,monkeypatch,defect):
    with closing(RuntimeStore(tmp_path)) as store:
        ledger=CodeEvolutionStore(store)
        ledger.create_transaction(transaction_payload('existing',terminal=defect=='sentinel'))
        if defect=='sentinel':
            for index in range(500):ledger.create_transaction(transaction_payload('many-'+str(index)))
        else:
            with store._lock:
                payload=transaction_payload('existing')
                if defect=='corrupt_payload':raw='not json'
                else:
                    if defect=='wrong_payload_owner':payload['scope']['user_id']='other'
                    if defect=='normalized_payload_owner':payload['scope']['user_id']=' '+SCOPE.user_id+' '
                    if defect=='wrong_payload_repo':payload['repository']['root']='/other'
                    raw=json.dumps(payload)
                store.sqlite.conn.execute('UPDATE code_evolution_transactions SET payload_json=? WHERE transaction_id=?',(raw,'existing'))
                store.sqlite.conn.commit()
        monkeypatch.setattr('eimemory.governance.l5.l5_reader._quality_repair_transaction',lambda *a,**k:pytest.fail('unknown ledger fell back to quality'))
        result=evidence(SimpleNamespace(store=store),monkeypatch)
        assert result['ledger_page_complete'] is False and result['evidence_verified'] is False
        assert result['evidence_error']==('code_evolution_source_page_incomplete' if defect=='sentinel' else 'code_evolution_source_page_unavailable')


def consumed_repair(store,runtime,monkeypatch,root,*,defect=''):
    detected=inspect(runtime)
    incident=detected['incident']
    policy_digest='f'*64
    transaction_id=router._stable_id('system-repair',incident['incident_digest'],COMMIT,policy_digest)
    monkeypatch.setattr(router,'_automation_policy_identity',lambda:(incident['incident_digest'],policy_digest,'l5.default'))
    monkeypatch.setattr('eimemory.governance.evolution.code_evolution_bridge.propose_code_patch_v2',
        lambda *a,**k:pytest.fail('consumed grant called the provider again'))
    monkeypatch.setattr('eimemory.capabilities.profiles.CapabilityProfiles.resolve',
        lambda *a,**k:pytest.fail('existing transaction restarted profile resolution'))
    payload={'transaction_id':transaction_id,'idempotency_key':transaction_id,'scope':asdict(SCOPE),
        'origin':'system_detector','detector':'eimemory.runtime_identity_drift.v1','incident':incident,
        'repository':{'repository_root':str(root.resolve()),'repository_ref':'master','repository_remote':'origin',
                      'base_commit':COMMIT,'base_tree_digest':'1'*64},
        'provider':{'capability_id':'code.implementation','revision_id':'code.implementation:v2','binding_id':'native-binding',
                    'provider_kind':'hermes','provider_instance_id':'native-provider','implementation_digest':'2'*64},
        'profile_key':'l5.default','current_state':'OBSERVING','terminal':False,
        'proposal_digest':'3'*64,'patch_digest':'4'*64,'candidate_tree_digest':'',
        'advertisement_id':'advertisement','advertisement_digest':'6'*64,
        'catalog_case_id':'case','catalog_snapshot_digest':'7'*64}
    if defect=='foreign_owner':payload['scope']['user_id']='other'
    if defect=='other_repo':payload['repository']['repository_root']=str(root/'other')
    if defect=='other_base':payload['repository']['base_commit']='c'*40
    if defect=='other_incident':payload['incident']={**incident,'incident_id':'other','incident_digest':'e'*64}
    if defect=='other_class':payload['incident']={**incident,'incident_class':'other.class'}
    if defect=='other_detector':payload['detector']='other.detector'
    if defect=='other_profile':payload['profile_key']='other.profile'
    if defect=='other_transaction':payload['transaction_id']=payload['idempotency_key']='different-transaction'
    if defect=='unknown_state':payload['current_state']='UNKNOWN'
    original=deepcopy(payload)
    original['advertisement']={key:payload[key] for key in ('advertisement_id','advertisement_digest')}
    original['catalog']={key:payload[key] for key in ('catalog_case_id','catalog_snapshot_digest')}
    payload['payload']=original
    ledger=CodeEvolutionStore(store)
    transaction=ledger.create_transaction(payload)
    transaction=ledger.cas_transition(transaction['transaction_id'],expected_state=transaction['current_state'],
        expected_state_version=transaction['state_version'],target_state=transaction['current_state'],
        updates={'candidate_tree_digest':'5'*64})
    policy={'schema_version':'code_automation_policy.v2','ok':True,'status':'enabled','policy_digest':policy_digest,
        'effects':{'commit':True,'push':True,'deployment':True,'rollback':True,'sedimentation':True},
        'deployment':{'observation_seconds':28800},'capability':{'profile_key':'l5.default'}}
    material={'schema':'code_automation_authorization.v2','transaction_id':transaction['transaction_id'],
        'policy_digest':policy_digest,'incident_digest':transaction['incident_digest'],
        **{key:transaction[key] for key in ('proposal_digest','patch_digest','candidate_tree_digest')},
        'provider':{key:transaction[key] for key in ('capability_id','revision_id','binding_id','provider_kind','provider_instance_id','implementation_digest')},
        'advertisement':{key:transaction[key] for key in ('advertisement_id','advertisement_digest')},
        'catalog':{key:transaction[key] for key in ('catalog_case_id','catalog_snapshot_digest')},
        'repository':{key:transaction[key] for key in ('repository_root','repository_remote','repository_ref','base_commit','base_tree_digest')},
        'verification_receipt_digests':['8'*64],'effects':policy['effects'],'deployment':policy['deployment']}
    if defect=='wrong_material_provider':material['provider']['implementation_digest']='0'*64
    if defect=='wrong_material_repository':material['repository']['base_tree_digest']='0'*64
    if defect=='wrong_material_candidate':material['candidate_tree_digest']='0'*64
    if defect=='wrong_material_incident':material['incident_digest']='0'*64
    if defect=='legacy_material':material['schema']='code_automation_authorization.v1'
    ledger.consume_policy(transaction_id=transaction['transaction_id'],policy_digest=policy_digest,
        authorization_receipt_digest=digest_json(material),
        payload={'authorization_material':material,'authorized_policy':policy})
    transaction=ledger.get_transaction(transaction['transaction_id'])
    if defect=='terminal':
        ledger.add_terminal_receipt(transaction['transaction_id'],
            {'outcome':'aborted_no_external_effect','evidence_digest':'9'*64},
            expected_state=transaction['current_state'],expected_state_version=transaction['state_version'],
            terminal_state='ABORTED_NO_EXTERNAL_EFFECT')
    if defect in ('corrupt_json','corrupt_digest','missing_transaction','wrong_payload_owner','wrong_policy_snapshot'):
        native_get=CodeEvolutionStore.get_policy_consumption
        native_transaction=CodeEvolutionStore.get_transaction
        if defect=='missing_transaction':
            monkeypatch.setattr(CodeEvolutionStore,'get_transaction',lambda *a,**k:None)
        elif defect=='wrong_payload_owner':
            def changed_transaction(self,key):
                row=deepcopy(native_transaction(self,key));row['payload']['scope']['user_id']='other';return row
            monkeypatch.setattr(CodeEvolutionStore,'get_transaction',changed_transaction)
        else:
            def changed_consumption(self,key):
                row=dict(native_get(self,key))
                if defect=='corrupt_json':row['payload_json']='not json'
                elif defect=='corrupt_digest':row['authorization_receipt_digest']='0'*64
                else:
                    value=json.loads(row['payload_json']);value['authorized_policy']['effects']['push']=False
                    row['payload_json']=json.dumps(value)
                return row
            monkeypatch.setattr(CodeEvolutionStore,'get_policy_consumption',changed_consumption)
    return ledger,transaction['transaction_id'],policy_digest


def test_consumed_exact_active_repair_is_readonly_handoff_to_recovery(tmp_path,monkeypatch):
    with closing(RuntimeStore(tmp_path)) as store:
        runtime=router_runtime(store,monkeypatch)
        ledger,transaction_id,policy_digest=consumed_repair(store,runtime,monkeypatch,tmp_path)
        before=ledger.get_transaction(transaction_id)
        assert before['payload']['candidate_tree_digest']=='' and before['candidate_tree_digest']=='5'*64
        consumption=ledger.get_policy_consumption(policy_digest)
        records=store.sqlite.conn.execute('SELECT COUNT(*) FROM records').fetchone()[0]
        for _ in range(2):
            report=router.process_system_code_incidents(runtime,scope=SCOPE,repo_root=tmp_path)
            assert report['ok'] is True and report['status']=='existing' and report['repair_complete'] is False
            assert len(report['processed'])==1
            item=report['processed'][0]
            assert item['transaction_id']==transaction_id and item['status']=='OBSERVING'
            assert item['idempotent'] is True and item['recovery_required'] is True and item['repair_complete'] is False
        assert ledger.get_transaction(transaction_id)==before
        assert ledger.get_policy_consumption(policy_digest)==consumption
        assert store.sqlite.conn.execute('SELECT COUNT(*) FROM records').fetchone()[0]==records


@pytest.mark.parametrize('defect',['foreign_owner','other_repo','other_base','other_incident','other_class','other_detector',
    'other_profile','other_transaction','unknown_state','terminal','wrong_material_provider','wrong_material_repository',
    'wrong_material_candidate','wrong_material_incident','legacy_material','corrupt_json','corrupt_digest',
    'missing_transaction','wrong_payload_owner','wrong_policy_snapshot'])
def test_consumed_grant_mismatch_never_reauthorizes_or_hands_off(tmp_path,monkeypatch,defect):
    with closing(RuntimeStore(tmp_path)) as store:
        runtime=router_runtime(store,monkeypatch)
        consumed_repair(store,runtime,monkeypatch,tmp_path,defect=defect)
        report=router.process_system_code_incidents(runtime,scope=SCOPE,repo_root=tmp_path)
        assert report['ok'] is False and report['status']=='blocked'
        assert report['reason']=='automation_policy_already_consumed' and report['processed']==[]


@pytest.mark.parametrize('layer',['outer','proposal'])
@pytest.mark.parametrize('coordinate',[
    ('provider','capability_id'),('provider','revision_id'),('provider','binding_id'),
    ('provider','provider_kind'),('provider','provider_instance_id'),('provider','implementation_digest'),
    ('repository','repository_root'),('repository','repository_ref'),('repository','repository_remote'),
    ('repository','base_commit'),('repository','base_tree_digest'),
    ('incident','incident_id'),('incident','incident_digest'),('incident','incident_class'),
    ('proposal_digest',),('patch_digest',),('advertisement','advertisement_id'),
    ('advertisement','advertisement_digest'),('catalog','catalog_case_id'),('catalog','catalog_snapshot_digest')])
def test_consumed_authorization_does_not_mask_damaged_original_source_projection(tmp_path,monkeypatch,layer,coordinate):
    with closing(RuntimeStore(tmp_path)) as store:
        runtime=router_runtime(store,monkeypatch)
        ledger,transaction_id,_=consumed_repair(store,runtime,monkeypatch,tmp_path)
        payload=ledger.get_transaction(transaction_id)['payload']
        original=payload if layer=='outer' else payload['payload']
        if layer=='outer' and coordinate[0] in ('advertisement','catalog'):
            original[coordinate[1]]='damaged'
        else:
            for key in coordinate[:-1]:original=original[key]
            original[coordinate[-1]]='damaged'
        with store._lock:
            store.sqlite.conn.execute('UPDATE code_evolution_transactions SET payload_json=? WHERE transaction_id=?',
                (json.dumps(payload),transaction_id))
            store.sqlite.conn.commit()
        report=router.process_system_code_incidents(runtime,scope=SCOPE,repo_root=tmp_path)
        assert report['ok'] is False and report['reason']=='automation_policy_already_consumed'
        assert report['processed']==[]
