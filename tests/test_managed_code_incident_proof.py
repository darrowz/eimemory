"""Isolated authority fixtures. None of these receipts are production evidence."""
from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
import json
import sqlite3
import stat
import os
import py_compile
import subprocess
import sys
import shutil
from types import SimpleNamespace
from pathlib import Path

import pytest

from eimemory.contracts.managed_code_incident_proof import (
    DOMAIN, FIELDS, SOURCE_KEY, TRANSACTION_SCHEMA, digest, stable_defect_key,
    validate_snapshot, transaction_snapshot,
)
from eimemory.core import managed_incident_proof_authority as authority
from eimemory.governance.evolution.managed_code_incident_proof import collect_physical_facts
from eimemory.models.records import ScopeRef
from eimemory.storage.runtime_store import RuntimeStore
from eimemory.ops.release_closure_failure import record_release_closure_failure

SCOPE = dict(tenant_id='default',agent_id='managed-fixture',workspace_id='workspace',user_id='owner')
COMMIT = 'a'*40


def state():
    conn=sqlite3.connect(':memory:');conn.row_factory=sqlite3.Row
    conn.executescript('''CREATE TABLE authority_meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
        CREATE TABLE managed_knowledge(owner TEXT NOT NULL,defect_key TEXT NOT NULL,record_id TEXT NOT NULL,
        source_digest TEXT NOT NULL,source_time TEXT NOT NULL,sequence INTEGER,human_input INTEGER NOT NULL,
        PRIMARY KEY(owner,record_id,source_digest));''')
    return conn


def original_facts():
    key=stable_defect_key('release.closure_internal_failure',['typed_code'])
    facts=dict(scope=SCOPE,source_record_id='incident-original',source_digest='b'*64,
        defect_key=key,source_created_at='2026-10-19T10:00:00.123456+00:00',
        source_received_at='2026-10-19T10:00:00.123Z',
        history=[],after=0,through=1,high_water=1,complete=True,
        rows=[dict(table_name='records',operation='INSERT',record_kind='incident',source='eimemory.release_closure_failure',
            **SCOPE,sequence=1,record_id='incident-original',payload_digest='b'*64,
            source_time='2026-10-19T10:00:00.123456+00:00',incident_class='release.closure_internal_failure',
            codes_json='["typed_code"]',human_input=0)],
        observed_at='2026-10-19T10:00:01+00:00',activation_reference={
            'schema':'eimemory.runtime_activation_receipt_ref.v1','receipt_id':'1'*64,'digest':'2'*64},
        incident={'incident_id':'incident-original','incident_digest':'e'*64},
        global_facts={key:None for key in FIELDS})
    facts['first_source_evidence']={'schema':'managed_native_first_physical_source.v1',
        'activation_reference':facts['activation_reference'],'created_at':facts['source_created_at']}
    return facts


def activation(facts):
    return dict(schema='eimemory.runtime_activation_attestation.v1',signature_verified=True,
        reference=facts['activation_reference'],registry_generation='c'*64,scope=SCOPE,
        peer_pid=123,peer_uid=1001,manager='system',unit='eimemory-learn-watch.service',
        observed_at='2026-10-19T10:00:00+00:00',trigger_kind='scheduled_timer',job_state='running',job_id=42)


def test_real_root_registry_can_prove_only_registered_domain_and_remembers_cross_release():
    conn=state();facts=original_facts();manifest=dict(scopes=[SCOPE])
    authority._ingest(conn,facts,manifest)
    assert authority._known(conn,facts)==(False,False,1,'')
    assert authority._activation_facts(facts,activation(facts),{'generation':'c'*64,'runtime_uid':1001},{'pid':123,'uid':1001})==(False,'')
    # Release/policy/producer changes cannot make the same typed defect unknown.
    later=deepcopy(facts);later.update(source_record_id='incident-next-release',source_digest='d'*64,
        source_created_at='2026-10-20T10:00:00+00:00',after=1,through=2,high_water=2)
    later['rows'][0].update(sequence=2,record_id=later['source_record_id'],payload_digest=later['source_digest'],
        source_time=later['source_created_at'])
    authority._ingest(conn,later,manifest)
    assert authority._known(conn,later)==(True,False,2,'')
    # The original fact is still novel at its original actual receive boundary.
    assert authority._known(conn,facts)==(False,False,1,'')


@pytest.mark.parametrize('change,reason',[
    ({'trigger_kind':'unknown'},'activation_cause_unknown'),
    ({'signature_verified':False},'original_activation_unverified'),
    ({'reference':{'fixture':'different-inv'}},'original_activation_unverified'),
    ({'peer_pid':0},'original_activation_unverified'),
    ({'job_state':'done'},'activation_cause_unknown'),
    ({'job_id':0},'activation_cause_unknown'),
    ({'observed_at':'2026-10-19T12:00:00+00:00'},'activation_source_time_unbound'),
])
def test_unsigned_unknown_stale_or_wrong_peer_never_becomes_scheduled(change,reason):
    facts=original_facts();value={**activation(facts),**change}
    assert authority._activation_facts(facts,value,{'generation':'c'*64,'runtime_uid':1001},{'pid':123,'uid':1001})==(None,reason)


def test_human_knowledge_late_input_mutation_and_gap_never_prove_absence():
    facts=original_facts();conn=state();manifest=dict(scopes=[SCOPE]);authority._ingest(conn,facts,manifest)
    next_=deepcopy(facts);next_.update(after=1,through=2,high_water=2)
    next_['rows'][0].update(sequence=2,record_id='late-human',payload_digest='d'*64,human_input=1,
        source='eimemory.code_maintenance',source_time='2026-10-18T00:00:00+00:00')
    authority._ingest(conn,next_,manifest)
    assert authority._known(conn,facts)==(None,None,1,'late_prior_typed_knowledge')
    broken=deepcopy(next_);broken['rows'][0]['sequence']=3
    with pytest.raises(ValueError,match='prefix_incomplete'):authority._ingest(conn,broken,manifest)
    broken=deepcopy(next_);broken['rows'][0]['operation']='DELETE';authority._ingest(conn,broken,manifest)
    assert conn.execute("SELECT value FROM authority_meta WHERE key='uncertainty'").fetchone()[0]=='typed_ingress_mutation_or_unregistered'


def test_historical_typed_prehistory_and_cross_scope_isolation():
    facts=original_facts();conn=state();facts['history']=[dict(scope=SCOPE,record_id='original-known-request',
        defect_key=facts['defect_key'],source_digest='d'*64,source_time='2026-10-18T00:00:00+00:00',human_input=False)]
    authority._ingest(conn,facts,dict(scopes=[SCOPE]))
    assert authority._known(conn,facts)==(True,False,1,'')
    other={**facts,'scope':{**SCOPE,'user_id':'unrelated'}}
    assert authority._known(conn,other)==(None,None,None,'original_receive_sequence_unverified')


def test_manifest_extension_replays_original_receive_prefix_and_typed_history_without_erasing_positives():
    conn=state();original=original_facts();other_scope={**SCOPE,'user_id':'newly-registered-owner'}
    other_row={**original['rows'][0],**other_scope,'record_id':'old-other-scope-incident',
        'payload_digest':'d'*64,'source_time':'2026-10-18T00:00:00+00:00'}
    initial=deepcopy(original);initial.update(through=2,high_water=2)
    initial['rows']=[other_row,{**initial['rows'][0],'sequence':2}]
    manifest={'scopes':[SCOPE]}
    authority._ingest(conn,initial,manifest)
    saved=[tuple(row) for row in conn.execute('SELECT * FROM managed_knowledge')]
    assert len(saved)==1 and authority._known(conn,initial)==(False,False,2,'')
    assert authority._catchup_progress(conn,manifest)=={'epoch':0,'cursor':2,'complete':True,'high_water':2}
    expanded={'scopes':[SCOPE,other_scope],'previous_manifest_digest':authority.digest(manifest)}
    authority._prepare_manifest_catchup(conn,expanded)
    assert [tuple(row) for row in conn.execute('SELECT * FROM managed_knowledge')]==saved
    assert authority._catchup_progress(conn,expanded)=={'epoch':1,'cursor':0,'complete':False,'high_water':2}
    # The physical global sequence is replayed in bounded pages. A formerly
    # excluded owner is not treated as having no retained typed prehistory.
    first=deepcopy(original);first.update(through=1,high_water=3,complete=False,rows=[other_row],history=[{
        'scope':other_scope,'record_id':'retained-before-capture','defect_key':original['defect_key'],
        'source_digest':'e'*64,'source_time':'2026-10-17T00:00:00+00:00','human_input':False}])
    authority._ingest(conn,first,expanded)
    assert authority._catchup_progress(conn,expanded)=={'epoch':1,'cursor':1,'complete':False,'high_water':2}
    new_source={**other_row,'sequence':3,'record_id':'new-other-scope-incident','payload_digest':'f'*64,
        'source_time':original['source_created_at']}
    last=deepcopy(original);last.update(scope=other_scope,source_record_id=new_source['record_id'],
        source_digest=new_source['payload_digest'],after=1,through=3,high_water=3,
        rows=[initial['rows'][1],new_source],history=[])
    authority._ingest(conn,last,expanded)
    assert authority._catchup_progress(conn,expanded)=={'epoch':1,'cursor':3,'complete':True,'high_water':3}
    assert authority._known(conn,last)==(True,False,3,'')
    assert conn.execute('SELECT * FROM managed_knowledge WHERE record_id=?',
        (original['source_record_id'],)).fetchone() is not None
    assert authority._known(conn,initial)==(False,False,2,'')
    before=conn.total_changes
    authority._prepare_manifest_catchup(conn,expanded)
    assert conn.total_changes==before  # replay does not create a new generation
    with pytest.raises(ValueError,match='continuity_unverified'):
        authority._prepare_manifest_catchup(conn,{'scopes':[SCOPE,other_scope]})
    assert conn.total_changes==before
    with pytest.raises(ValueError,match='high_water_regressed'):
        authority._ingest(conn,original,expanded)


def test_legacy_manifest_index_requires_bounded_catchup_and_corrupt_cursor_cannot_reset_it():
    conn=state();manifest={'scopes':[SCOPE]};token=authority.digest(manifest)
    conn.execute("INSERT INTO authority_meta VALUES('manifest',?)",(token,))
    conn.execute("INSERT INTO authority_meta VALUES('high_water','9')")
    assert not authority._catchup_progress(conn,manifest)['complete']
    authority._prepare_manifest_catchup(conn,manifest)
    assert authority._catchup_progress(conn,manifest)=={'epoch':0,'cursor':0,'complete':False,'high_water':9}
    conn.execute("UPDATE authority_meta SET value='different' WHERE key='catchup_manifest'")
    with pytest.raises(ValueError,match='catchup_manifest_mismatch'):
        authority._prepare_manifest_catchup(conn,manifest)
    conn.execute("UPDATE authority_meta SET value=? WHERE key='catchup_manifest'",(token,))
    conn.execute("UPDATE authority_meta SET value='10' WHERE key='catchup_cursor'")
    with pytest.raises(ValueError,match='catchup_state_invalid'):
        authority._catchup_progress(conn,manifest)
    conn.execute("UPDATE authority_meta SET value='0' WHERE key='catchup_cursor'")
    conn.execute("DELETE FROM authority_meta WHERE key='catchup_manifest'")
    with pytest.raises(ValueError,match='catchup_state_missing'):
        authority._prepare_manifest_catchup(conn,manifest)


def test_unclassified_manual_code_report_is_unknown_and_same_symptom_cannot_reset_novelty():
    facts=original_facts();conn=state()
    facts['history']=[dict(scope=SCOPE,record_id='unclassified-original-human-code-report',defect_key='*',
        source_digest='d'*64,source_time='2026-10-18T00:00:00+00:00',human_input=True)]
    authority._ingest(conn,facts,dict(scopes=[SCOPE]))
    assert authority._known(conn,facts)==(None,None,1,'unclassified_typed_prehistory')
    # No source field establishes that two same-symptom causes differ. A new
    # causal story/title/release is not an input to the protected key codec.
    assert stable_defect_key('release.closure_internal_failure',['typed_code'])==facts['defect_key']


def test_issuer_real_hmac_original_bytes_and_readback_not_only_a_flag(tmp_path,monkeypatch):
    """Actual issue/sign/immutable-file/SQLite logic; host FS boundary is separate."""
    conn=state();conn.executescript('''CREATE TABLE managed_receipts(source_record_id TEXT,source_digest TEXT,
        receipt_id TEXT,digest TEXT,PRIMARY KEY(source_record_id,source_digest));
        CREATE TABLE managed_manifest_history(digest TEXT PRIMARY KEY);''')
    facts=original_facts();manifest=dict(scopes=[SCOPE]);token=authority.digest(manifest)
    conn.execute('INSERT INTO managed_manifest_history VALUES(?)',(token,));conn.commit()
    @contextmanager
    def writable(_registry,current_manifest):
        try:
            authority._prepare_manifest_catchup(conn,current_manifest)
            conn.execute('INSERT OR IGNORE INTO managed_manifest_history VALUES(?)',(authority.digest(current_manifest),))
            yield conn;conn.commit()
        except BaseException:conn.rollback();raise
    @contextmanager
    def readonly(*_args):
        memory=sqlite3.connect(':memory:');memory.row_factory=sqlite3.Row
        conn.backup(memory)
        try:yield memory
        finally:memory.close()
    def worker(_manifest,_registry,_id,_digest,after,cursor_anchor=None):
        value=deepcopy(facts)
        value.update(cursor_anchor=cursor_anchor,cursor_anchor_verified=True)
        if after:
            value.update(after=1,through=1,high_water=1,rows=[])
        if not _id:
            value.update(scope=None,incident=None,source_record_id='',source_digest='')
        return value
    monkeypatch.setattr(authority,'RECEIPT_DIRECTORY',str(tmp_path))
    monkeypatch.setattr(authority,'_configuration',lambda _: (manifest,b'private-root-fixture-key-'*4))
    monkeypatch.setattr(authority,'_state',writable);monkeypatch.setattr(authority,'_readonly_state',readonly)
    monkeypatch.setattr(authority,'_worker',worker)
    monkeypatch.setattr(authority,'_runtime_producer_guard',lambda *_:{'isolated':'fixture'})
    # Windows exercises signing/state bytes, not POSIX authority path permissions.
    monkeypatch.setattr(authority,'_read',lambda path,**_:Path(path).read_bytes())
    monkeypatch.setattr(authority,'_fsync_directory',lambda _:None)
    for flag in ('O_NOFOLLOW','O_CLOEXEC'):
        if not hasattr(authority.os,flag):monkeypatch.setattr(authority.os,flag,0,raising=False)
    registry=dict(generation='c'*64,runtime_uid=1001)
    calls=[]
    def verify_original(rid,rdigest):
        calls.append((rid,rdigest));return activation(facts)
    reference=authority.issue(facts['source_record_id'],facts['source_digest'],
        verified_activation_receipt=verify_original,registry=registry,peer={'uid':1001,'pid':999})
    assert calls==[('1'*64,'2'*64)]
    original=(tmp_path/(reference['receipt_id']+'.json')).read_bytes()
    changes=conn.total_changes
    snapshot=authority.verify(reference['receipt_id'],reference['digest'],registry=registry)['snapshot']
    assert snapshot['status']=='proven' and snapshot['qualification_domain']==DOMAIN
    assert snapshot['global_facts']=={key:None for key in FIELDS}
    assert snapshot['managed_facts']=={key:False for key in FIELDS}
    validate_snapshot(snapshot)
    assert conn.total_changes==changes
    assert (tmp_path/(reference['receipt_id']+'.json')).read_bytes()==original
    # The transport alone is doubled; the endpoint re-reads the actual HMAC
    # receipt and original root SQLite index above. No boolean certifier stub.
    from types import ModuleType
    client=ModuleType('eimemory.core.runtime_manager')
    client.verify_managed_incident_proof=lambda rid,rdigest:authority.verify(rid,rdigest,registry=registry)
    monkeypatch.setitem(sys.modules,'eimemory.core.runtime_manager',client)
    from eimemory.governance.l5.l5_product_completion import build_product_completion
    transaction=dict(transaction_id='consumer-only-fixture',transaction_schema_version=TRANSACTION_SCHEMA,
        incident_id=snapshot['incident_id'],incident_digest=snapshot['incident_digest'],**SCOPE,
        origin='system_detector',detector='eimemory.release_closure_failure.v1',
        **snapshot['global_facts'],managed_incident_proof=snapshot,
        terminal_receipt_digest='9'*64,qualifying_terminal_outcome='succeeded_sedimented',
        evidence_verified=True,observation_valid=True)
    # Remaining gate inputs are explicit consumer fixtures, not actual 8h or
    # deployment evidence. This test makes no production completion claim.
    def completion(**changes):
        return build_product_completion(dict(ok=True,status='ready'),
            provider=dict(ready=True,catalog_ready=True,advertisement_fresh=True),
            transaction={**transaction,**changes},current_lineage=dict(ok=True,compatible=True))
    complete=completion()
    assert complete['schema_version']=='l5_readiness.v5' and complete['qualification_domain']==DOMAIN
    assert complete['product_l5_complete'] is True
    assert complete['global_provenance']=={key:None for key in FIELDS}
    assert transaction['known_before_detection'] is None
    assert not completion(terminal_receipt_digest='')['product_l5_complete']
    assert not completion(observation_valid=False)['product_l5_complete']
    assert not completion(evidence_verified=False)['product_l5_complete']
    assert not completion(qualifying_terminal_outcome='aborted_no_external_effect')['product_l5_complete']
    altered=deepcopy(snapshot);altered['managed_facts']['manual_bootstrap']=True
    altered['status']='adverse';altered['digest']=digest({k:v for k,v in altered.items() if k!='digest'})
    rejected=completion(managed_incident_proof=altered)
    assert rejected['product_l5_complete'] is False and 'managed_incident_proof_unverified' in rejected['gaps']
    legacy=dict(transaction);legacy.pop(SOURCE_KEY)
    old=build_product_completion(dict(ok=True,status='ready'),provider=dict(ready=True,catalog_ready=True,advertisement_fresh=True),
        transaction=legacy,current_lineage=dict(ok=True,compatible=True))
    assert old['schema_version']=='l5_readiness.v4' and old['product_l5_complete'] is False
    # A newly reviewed scope cannot inherit a completed old-domain cursor.
    # The original HMAC/clock/receipt stay authentic while qualification waits
    # for explicit durable maintenance, even if readback can inspect one page.
    manifest.update(scopes=[SCOPE,{**SCOPE,'user_id':'new-domain-owner'}],previous_manifest_digest=token)
    before_transition=conn.total_changes
    pending=authority.verify(reference['receipt_id'],reference['digest'],registry=registry)
    assert pending['snapshot']==snapshot and not pending['qualification_current']
    assert pending['current_managed_facts']['known_before_detection'] is None
    assert pending['reason']=='managed_history_manifest_catchup_incomplete'
    assert conn.total_changes==before_transition
    assert (tmp_path/(reference['receipt_id']+'.json')).read_bytes()==original
    assert not completion()['product_l5_complete']
    checkpoint=authority.checkpoint(registry=registry,peer={'uid':1001})
    assert checkpoint['status']=='ready' and checkpoint['catchup_epoch']==1
    assert checkpoint['catchup_complete'] is True and checkpoint['catchup_cursor']==1
    assert checkpoint['catchup_manifest_digest']==authority.digest(manifest)
    assert authority.verify(reference['receipt_id'],reference['digest'],registry=registry)['qualification_current']
    assert completion()['product_l5_complete'] is True
    assert (tmp_path/(reference['receipt_id']+'.json')).read_bytes()==original
    changes=conn.total_changes
    late=deepcopy(facts);late.update(after=1,through=2,high_water=2,history=[])
    late.update(cursor_anchor=authority._receive_anchor(conn),cursor_anchor_verified=True)
    late['rows'][0].update(sequence=2,record_id='late-original-report',payload_digest='d'*64,
        human_input=1,source='eimemory.code_maintenance',source_time='2026-10-18T00:00:00+00:00')
    monkeypatch.setattr(authority,'_worker',lambda *_:deepcopy(late))
    rechecked=authority.verify(reference['receipt_id'],reference['digest'],registry=registry)
    assert rechecked['snapshot']==snapshot and not rechecked['qualification_current']
    assert rechecked['current_managed_facts']['known_before_detection'] is None
    # Authenticated original source remains usable for an already-authorized
    # repair. Only current managed qualification is withdrawn by the late fact.
    from eimemory.governance.evolution.managed_code_incident_proof import verified_managed_snapshot
    assert verified_managed_snapshot(snapshot)==snapshot
    assert completion()['product_l5_complete'] is False
    assert conn.total_changes==changes and (tmp_path/(reference['receipt_id']+'.json')).read_bytes()==original
    # Replay retains the original signed clock/bytes and does not call a later cause.
    replay=authority.issue(facts['source_record_id'],facts['source_digest'],
        verified_activation_receipt=lambda *_:pytest.fail('must keep original receipt'),
        registry=registry,peer={'uid':1001,'pid':1000})
    assert replay==reference and (tmp_path/(reference['receipt_id']+'.json')).read_bytes()==original
    # A retained historical transaction can establish a prior positive after
    # this receipt was signed. Root positives must survive even a later gap.
    learned=worker(manifest,registry,facts['source_record_id'],facts['source_digest'],1,authority._receive_anchor(conn))
    learned['history']=[dict(scope=SCOPE,record_id='retained-prior-user-transaction',defect_key=facts['defect_key'],
        source_digest='f'*64,source_time='2026-10-18T00:00:00+00:00',human_input=True)]
    authority._ingest(conn,learned,manifest);conn.commit()
    assert authority._known(conn,facts)==(True,True,1,'')
    changes=conn.total_changes
    # A storage rollback can numerically reattain the old HWM with different
    # logical rows. Authenticity survives, but no negative current fact does.
    anchor=authority._receive_anchor(conn);saved_positives=[tuple(row) for row in conn.execute('SELECT * FROM managed_knowledge')]
    broken=dict(cursor_anchor=anchor,cursor_anchor_verified=False,high_water=99)
    monkeypatch.setattr(authority,'_worker',lambda *_:deepcopy(broken))
    gap=authority.verify(reference['receipt_id'],reference['digest'],registry=registry)
    assert gap['snapshot']==snapshot and not gap['qualification_current']
    assert gap['current_managed_facts']['known_before_detection'] is True
    assert gap['current_managed_facts']['prior_user_reported'] is True
    assert gap['reason']=='receive_prefix_anchor_changed'
    assert conn.total_changes==changes  # verifier remains genuinely read-only
    assert not completion()['product_l5_complete']
    diagnosed=authority.checkpoint(registry=registry,peer={'uid':1001})
    assert diagnosed['status']=='unproven' and diagnosed['physical_high_water']==99
    assert authority._receive_anchor(conn)==anchor
    assert [tuple(row) for row in conn.execute('SELECT * FROM managed_knowledge')]==saved_positives
    # Once persisted, neither restoring the original row nor later catching up
    # a numeric sequence can replace the old witness or clear the uncertainty.
    monkeypatch.setattr(authority,'_worker',lambda *_:pytest.fail('sticky chronology gap must not silently heal'))
    assert authority.checkpoint(registry=registry,peer={'uid':1001})['status']=='unproven'
    assert not authority.verify(reference['receipt_id'],reference['digest'],registry=registry)['qualification_current']
    assert authority._receive_anchor(conn)==anchor
    assert (tmp_path/(reference['receipt_id']+'.json')).read_bytes()==original
    packet=json.loads(original);packet['payload']['snapshot']['managed_facts']['manual_bootstrap']=True
    mutated=authority.canonical(packet);(tmp_path/(reference['receipt_id']+'.json')).write_bytes(mutated)
    from hashlib import sha256
    with pytest.raises(ValueError,match='signature_invalid'):
        authority.verify(reference['receipt_id'],sha256(mutated).hexdigest(),registry=registry)


def proof_fixture():
    material=dict(schema='managed_code_incident_proof.v1',qualification_domain=DOMAIN,
        reference={'schema':'managed_code_incident_proof_ref.v1','receipt_id':'1'*64,'digest':'2'*64},scope=SCOPE,
        source_record_id='incident-original',source_digest='b'*64,incident_id='incident-original',incident_digest='e'*64,
        defect_key=stable_defect_key('release.closure_internal_failure',['typed_code']),
        defect_key_semantics='conservative_native_symptom_signature.v1',
        activation_reference={'schema':'eimemory.runtime_activation_receipt_ref.v1','receipt_id':'3'*64,'digest':'4'*64},
        manifest_digest='f'*64,history_watermark=1,source_sequence=1,
        managed_facts={key:False for key in FIELDS},global_facts={key:None for key in FIELDS},
        external_knowledge=None,external_user_reports=None,status='proven',reason='')
    return {**material,'digest':digest(material)}


@pytest.mark.parametrize('adverse',FIELDS)
def test_managed_root_readback_cannot_erase_original_global_adverse_fact(monkeypatch,adverse):
    """Contradictory authority boundary fixture, not an actual issuer claim."""
    from types import ModuleType
    from eimemory.governance.evolution.managed_code_incident_proof import qualification_evidence
    from eimemory.governance.evolution.code_evolution_transaction import qualification_report
    from eimemory.governance.l5.l5_product_completion import build_product_completion
    proof=proof_fixture();proof['global_facts'][adverse]=True
    proof['digest']=digest({key:value for key,value in proof.items() if key!='digest'})
    client=ModuleType('eimemory.core.runtime_manager')
    client.verify_managed_incident_proof=lambda *_:dict(schema='managed_code_incident_proof_verification.v1',
        snapshot=proof,qualification_current=True,current_managed_facts={key:False for key in FIELDS},
        current_watermark=1,reason='')
    monkeypatch.setitem(sys.modules,'eimemory.core.runtime_manager',client)
    transaction=dict(transaction_id='contradictory-boundary-fixture',transaction_schema_version=TRANSACTION_SCHEMA,
        incident_id=proof['incident_id'],incident_digest=proof['incident_digest'],**SCOPE,
        origin='system_detector',detector='eimemory.release_closure_failure.v1',**proof['global_facts'],
        managed_incident_proof=proof,terminal_receipt_digest='9'*64,
        qualifying_terminal_outcome='succeeded_sedimented',evidence_verified=True,observation_valid=True)
    checked=qualification_evidence(transaction)
    assert checked['facts'][adverse] is True and checked['global_facts'][adverse] is True
    lineage=dict(ok=True,compatible=True)
    assert not qualification_report(transaction,current_lineage=lineage)['qualifies_for_product_completion']
    complete=build_product_completion(dict(ok=True,status='ready'),
        provider=dict(ready=True,catalog_ready=True,advertisement_fresh=True),transaction=transaction,current_lineage=lineage)
    assert complete['global_provenance'][adverse] is True
    assert not complete['product_l5_complete'] and not complete['code_evolution']['transaction_verified']


def managed_payload():
    proof=proof_fixture()
    return dict(transaction_id='managed-sqlite',idempotency_key='managed-sqlite',scope=SCOPE,
        origin='system_detector',detector='eimemory.release_closure_failure.v1',
        **{key:None for key in FIELDS},
        incident=dict(incident_id=proof['incident_id'],incident_digest=proof['incident_digest'],
            incident_class='release.closure_internal_failure',diagnostic_codes=['typed_code']),
        repository=dict(repository_root='/fixture/repository',repository_ref='master',base_commit=COMMIT),
        managed_incident_proof=proof,source_evidence={SOURCE_KEY:proof},payload={SOURCE_KEY:proof})


def test_real_sqlite_four_projection_terminal_binding_and_no_global_upgrade(physical):
    from eimemory.storage.code_evolution_store import CodeEvolutionStore,CodeEvolutionStoreError
    ledger=CodeEvolutionStore(physical.store);payload=managed_payload();row=ledger.create_transaction(payload)
    assert row['schema_version']==TRANSACTION_SCHEMA
    assert transaction_snapshot(row)==proof_fixture()
    assert all(row[key] is None for key in FIELDS)
    raw=physical.store.sqlite.conn.execute('SELECT known_before_detection,prior_user_reported,manual_bootstrap FROM code_evolution_transactions').fetchone()
    assert tuple(raw)==(1,1,1)
    terminal=ledger.add_terminal_receipt(row['transaction_id'],dict(outcome='aborted_no_external_effect',evidence_digest='c'*64),terminal_state='ABORTED_NO_EXTERNAL_EFFECT')
    assert json.loads(terminal['payload_json'])[SOURCE_KEY]==proof_fixture()
    assert ledger.get_terminal_receipt(row['transaction_id'])['payload'][SOURCE_KEY]==proof_fixture()
    # Even a recomputed ordinary outer digest cannot remove the root snapshot.
    # Validate the actual read projection independently of immutable SQL guards.
    changed=deepcopy(row);changed['source_evidence'].pop(SOURCE_KEY)
    with pytest.raises(ValueError,match='proof_mismatch'):transaction_snapshot(changed)


@pytest.mark.parametrize('path',[
    'eimemory/contracts/managed_code_incident_proof.py','eimemory/core/managed_incident_proof_authority.py',
    'eimemory/governance/evolution/managed_code_incident_proof.py','eimemory/ops/prepare_managed_incident_authority.py',
    'eimemory/ops/runtime_identity_drift.py','eimemory/ops/release_closure_failure.py',
    'eimemory/governance/l5/l5_product_completion.py','eimemory/governance/l5_product_completion.py',
    'eimemory/governance/l5/native_assessment.py'])
def test_issuer_and_original_source_authority_are_deny_self(path):
    from eimemory.governance.evolution.code_evolution_path_policy import path_denied_by_self
    assert path_denied_by_self(path)


@pytest.mark.parametrize('path',authority.RUNTIME_PRODUCER_FILES)
def test_runtime_native_producer_compatibility_pins_are_deny_self(path):
    from eimemory.governance.evolution.code_evolution_path_policy import path_denied_by_self
    assert path_denied_by_self(path)


def test_runtime_allowed_business_changes_keep_producer_contract_but_native_changes_fail(tmp_path,monkeypatch):
    """Actual private files; POSIX descriptor boundary is covered separately."""
    controller=tmp_path/'controller';candidate=tmp_path/('d'*40)
    pins={name:digest(name) for name in authority.RUNTIME_PRODUCER_FILES}
    # Raw identity equality is checked by the same helper even when product
    # business/aggregation code legitimately differs between these releases.
    expected={}
    for directory in (controller,candidate):
        for name in pins:
            path=directory/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_text('# protected '+name)
            expected[name]=authority.sha256(path.read_bytes()).hexdigest()
        path=directory/'eimemory/governance/l5/l5_reader.py';path.parent.mkdir(parents=True,exist_ok=True)
        path.write_text('business_version = '+repr(directory.name))
    manifest=dict(source_directory=str(controller),producer_files=expected,release_commit=COMMIT)
    registry=dict(runtime_commit='d'*40,controller_commit=COMMIT,runtime_uid=1001,runtime_gid=1001)
    monkeypatch.setattr(authority,'RELEASES_DIRECTORY',str(tmp_path))
    def read(path,**_):
        path=Path(path);return path.read_bytes(),authority._identity(path.stat())
    monkeypatch.setattr(authority,'_read',read)
    before=authority._runtime_producer_guard(manifest,registry)
    assert len(before)==len(authority.RUNTIME_PRODUCER_FILES)
    path=candidate/'eimemory/ops/release_closure_failure.py';original=path.read_bytes()
    path.write_bytes(original+b'\n# modified actual producer')
    with pytest.raises(ValueError,match='runtime_producer_changed'):
        authority._runtime_producer_guard(manifest,registry)
    path.write_bytes(original)
    assert authority._runtime_producer_guard(manifest,registry)!=before
    injected=candidate/'eimemory/core/__init__.py';injected.write_text('# injected import hook')
    with pytest.raises(ValueError,match='runtime_namespace_injected'):
        authority._runtime_producer_guard(manifest,registry)
    injected.unlink()
    shadow=candidate/'eimemory/ops/release_closure_failure/__init__.py';shadow.parent.mkdir()
    shadow.write_text('# new package shadows a pinned module on Python import')
    with pytest.raises(ValueError,match='runtime_import_shadowed'):
        authority._runtime_producer_guard(manifest,registry)
    shadow.unlink();shadow.parent.rmdir()
    # It cannot borrow the frozen controller when actual candidate is missing.
    registry['runtime_commit']='e'*40
    with pytest.raises(FileNotFoundError):authority._runtime_producer_guard(manifest,registry)


@pytest.mark.parametrize('attack',['module_package','namespace_module'])
def test_actual_python_protected_import_replacement_is_rejected(tmp_path,monkeypatch,attack):
    """Actual isolated interpreter imports, no authority/Runtime/DB involved."""
    controller=tmp_path/'controller';candidate=tmp_path/('d'*40)
    expected={}
    for directory in (controller,candidate):
        for name in authority.RUNTIME_PRODUCER_FILES:
            path=directory/name;path.parent.mkdir(parents=True,exist_ok=True)
            raw=b'VALUE = "protected"\n' if name=='eimemory/core/clock.py' else b'# protected fixture\n'
            path.write_bytes(raw);expected[name]=authority.sha256(raw).hexdigest()
    manifest=dict(source_directory=str(controller),producer_files=expected,release_commit=COMMIT)
    registry=dict(runtime_commit='d'*40,controller_commit=COMMIT,runtime_uid=1001,runtime_gid=1001)
    monkeypatch.setattr(authority,'RELEASES_DIRECTORY',str(tmp_path))
    monkeypatch.setattr(authority,'_read',lambda path,**_: (Path(path).read_bytes(),authority._identity(Path(path).stat())))
    assert authority._runtime_producer_guard(manifest,registry)
    snippet='import sys;sys.path.insert(0,sys.argv[1]);from eimemory.core import clock;print(clock.VALUE)'
    argv=[sys.executable,'-I','-S','-B','-c',snippet,str(candidate)]
    assert subprocess.check_output(argv).strip()==b'protected'
    if attack=='module_package':
        path=candidate/'eimemory/core/clock/__init__.py';path.parent.mkdir()
        path.write_text('VALUE = "injected"\n')
        reason='runtime_import_shadowed'
    else:
        redirected=candidate/'injected';redirected.mkdir()
        (redirected/'clock.py').write_text('VALUE = "injected"\n')
        (candidate/'eimemory/core.py').write_text('__path__ = '+repr([str(redirected)])+'\n')
        reason='runtime_namespace_shadowed'
    assert subprocess.check_output(argv).strip()==b'injected'
    with pytest.raises(ValueError,match=reason):authority._runtime_producer_guard(manifest,registry)


def test_all_protected_namespace_import_prefixes_reject_new_module_files(tmp_path,monkeypatch):
    source=Path(__file__).resolve().parents[1]
    expected={name:authority.sha256((source/name).read_bytes()).hexdigest() for name in authority.RUNTIME_PRODUCER_FILES}
    candidate=tmp_path/('d'*40)
    for name in expected:
        path=candidate/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes((source/name).read_bytes())
    manifest=dict(source_directory=str(source),producer_files=expected,release_commit=COMMIT)
    registry=dict(runtime_commit='d'*40,controller_commit=COMMIT,runtime_uid=1001,runtime_gid=1001)
    monkeypatch.setattr(authority,'RELEASES_DIRECTORY',str(tmp_path))
    monkeypatch.setattr(authority,'_read',lambda path,**_: (Path(path).read_bytes(),authority._identity(Path(path).stat())))
    prefixes={parent.as_posix() for name in authority.RUNTIME_PRODUCER_FILES for parent in Path(name).parents
        if str(parent)!='.' and (parent/'__init__.py').as_posix() not in expected}
    assert {'eimemory/core','eimemory/models','eimemory/storage'}<=prefixes
    for prefix in prefixes:
        injected=candidate/(prefix+'.py');injected.write_text('# replaces a reviewed namespace import\n')
        with pytest.raises(ValueError,match='runtime_namespace_shadowed'):
            authority._runtime_producer_guard(manifest,registry)
        injected.unlink()
    assert authority._runtime_producer_guard(manifest,registry)


def test_configuration_binds_frozen_controller_not_serving_candidate(tmp_path,monkeypatch):
    source=Path(__file__).resolve().parents[1]
    manifest=dict(schema='managed_code_incident_manifest.v1',qualification_domain=DOMAIN,
        defect_key_semantics=authority.KEY_SEMANTICS,enabled=True,scopes=[SCOPE],source_directory=str(source),
        producer_files={name:'1'*64 for name in authority.RUNTIME_PRODUCER_FILES},release_commit=COMMIT,
        ingress_sources=list(authority.INGRESS),historical_domain='retained_typed_history_and_complete_registered_ingress',
        schema_digest='2'*64,previous_manifest_digest='')
    raw=authority.canonical(manifest)
    registry=dict(runtime_commit='d'*40,controller_commit=COMMIT,managed_incident_authority=dict(enabled=True,
        database_path=authority.DATABASE_PATH,manifest_path=authority.MANIFEST_PATH,
        manifest_sha256=authority.sha256(raw).hexdigest(),receipt_directory=authority.RECEIPT_DIRECTORY,key_path=authority.KEY_PATH))
    monkeypatch.setattr(authority.os,'geteuid',lambda:0,raising=False)
    monkeypatch.setattr(authority,'_read',lambda path,**_:raw if str(path)==authority.MANIFEST_PATH else b'k'*32)
    assert authority._configuration(registry)[0]==manifest
    with pytest.raises(ValueError,match='manifest_invalid'):
        authority._configuration({**registry,'controller_commit':'e'*40})


def test_runtime_activation_cannot_borrow_frozen_controller_or_different_serving_identity():
    facts=original_facts();registry=dict(generation='c'*64,runtime_uid=1001,runtime_commit='d'*40,controller_commit=COMMIT)
    actual={**activation(facts),'runtime_commit':'d'*40,'controller_commit':COMMIT}
    assert authority._activation_facts(facts,actual,registry,{'uid':1001})==(False,'')
    for changed in ({'runtime_commit':COMMIT},{'controller_commit':'e'*40}):
        assert authority._activation_facts(facts,{**actual,**changed},registry,{'uid':1001})==(None,'original_activation_release_unverified')


def test_source_inventory_excludes_unsearched_venv_but_detects_package_injection(tmp_path):
    (tmp_path/'package').mkdir();(tmp_path/'package'/'__init__.py').write_bytes(b'')
    site=tmp_path/'.venv'/'lib'/'python3'/'site-packages';site.mkdir(parents=True)
    (site/'unrelated.py').write_text('raise RuntimeError("not imported")')
    if os.name=='posix':os.symlink('lib',tmp_path/'.venv'/'lib64',target_is_directory=True)
    cache=tmp_path/'package'/'__pycache__';cache.mkdir();(cache/'unsearched.py').write_text('unsearched')
    assert authority.python_source_inventory(tmp_path)=={'package/__init__.py'}
    (tmp_path/'package'/'injected.py').write_text('unsafe = True')
    assert authority.python_source_inventory(tmp_path)=={'package/__init__.py','package/injected.py'}


def test_actual_interpreter_existing_pyc_cannot_override_pinned_source_with_empty_prefix(tmp_path):
    source=tmp_path/'source';source.mkdir();path=source/'victim.py'
    original=b'VALUE = "source"\n';forged=b'VALUE = "forged"\n';assert len(original)==len(forged)
    path.write_bytes(original);metadata=path.stat();path.write_bytes(forged)
    os.utime(path,ns=(metadata.st_atime_ns,metadata.st_mtime_ns))
    # The child uses -I, so write the attack cache where that child looks,
    # independently of pytest's private PYTHONPYCACHEPREFIX.
    bytecode=source/'__pycache__'/('victim.'+sys.implementation.cache_tag+'.pyc')
    py_compile.compile(str(path),cfile=str(bytecode),doraise=True)
    path.write_bytes(original);os.utime(path,ns=(metadata.st_atime_ns,metadata.st_mtime_ns))
    snippet='import sys;sys.path.insert(0,sys.argv[1]);import victim;print(victim.VALUE)'
    base=[sys.executable,'-I','-S','-B']
    # -B alone does read a matching already-existing timestamp .pyc.
    assert subprocess.check_output([*base,'-c',snippet,str(source)]).strip()==b'forged'
    prefix=tmp_path/'empty-root-owned-cache';prefix.mkdir()
    assert subprocess.check_output([*base,'-X','pycache_prefix='+str(prefix),'-c',snippet,str(source)]).strip()==b'source'
    assert list(prefix.iterdir())==[] and path.read_bytes()==original


@pytest.mark.skipif(os.name!='posix',reason='real POSIX descriptor/no-follow/identity boundary')
def test_posix_empty_source_and_changed_then_restored_identity_are_strict(
        tmp_path, monkeypatch, trusted_dataset_path_ancestors):
    # Model only immutable external ancestors for this descriptor-level source
    # identity test. The actual producer directory/files and their mutation
    # clocks remain untouched; root/native permission tests use real metadata.
    external={(p.stat().st_dev,p.stat().st_ino) for p in tmp_path.parents}
    def external_metadata(meta):
        if stat.S_ISDIR(meta.st_mode) and (meta.st_dev,meta.st_ino) in external:
            values=list(meta)
            if meta.st_uid not in {0,os.geteuid()}:values[4]=0
            if not meta.st_mode & stat.S_ISVTX:
                values[0]=int(meta.st_mode)&~(stat.S_IWGRP|stat.S_IWOTH)
            return os.stat_result(values)
        return meta
    native_os=authority.os
    monkeypatch.setattr(authority,'os',SimpleNamespace(**{
        **vars(native_os),
        'fstat':lambda fd:external_metadata(native_os.fstat(fd)),
        'stat':lambda *args,**kwargs:external_metadata(native_os.stat(*args,**kwargs)),
    }))
    path=tmp_path/'__init__.py';path.write_bytes(b'')
    owners=(0,os.geteuid())
    assert authority._read(path,owners=owners,allow_empty=True)==b''
    with pytest.raises(ValueError,match='file_untrusted'):authority._read(path,owners=owners)
    path.write_bytes(b'value = 1\n')
    manifest=dict(source_directory=str(tmp_path),scopes=[SCOPE],schema_digest='a'*64,
        producer_files={'__init__.py':authority.sha256(path.read_bytes()).hexdigest()})
    registry=dict(runtime_user='darrow',runtime_uid=os.geteuid(),runtime_gid=os.getegid())
    before=authority._producer_guard(manifest,registry)
    if os.geteuid()==0:
        # Same native layout as an unrelated root job adding a /var/tmp entry.
        # Root-only parent namespace stays fixed; source file is unchanged.
        parent=tmp_path.parent
        sibling=parent/('unrelated-private-state-'+tmp_path.name);sibling.mkdir()
        assert before==authority._producer_guard(manifest,registry)
        sibling.rmdir()
        import pwd
        account=pwd.getpwnam('darrow');mutable=tmp_path.parent/(tmp_path.name+'-mutable');mutable.mkdir()
        source=mutable/'producer.py';source.write_bytes(b'same source')
        os.chown(mutable,account.pw_uid,account.pw_gid)
        mutable_manifest={**manifest,'source_directory':str(mutable),
            'producer_files':{'producer.py':authority.sha256(source.read_bytes()).hexdigest()}}
        mutable_registry={**registry,'runtime_uid':account.pw_uid,'runtime_gid':account.pw_gid}
        original_parent=authority._producer_guard(mutable_manifest,mutable_registry)
        moved=tmp_path.parent/(tmp_path.name+'-moved');mutable.rename(moved);moved.rename(mutable)
        assert original_parent!=authority._producer_guard(mutable_manifest,mutable_registry)
    path.write_bytes(b'value = 2\n');path.write_bytes(b'value = 1\n')
    after=authority._producer_guard(manifest,registry)
    assert before!=after
    monkeypatch.setattr(authority,'_empty_pycache',lambda:str(tmp_path/'empty'))
    def attack(*_args,**_kwargs):
        saved=path.read_bytes();path.write_bytes(b'bad = True\n');path.write_bytes(saved)
        return SimpleNamespace(returncode=0,stdout=b'{}',stderr=b'')
    monkeypatch.setattr(authority.subprocess,'run',attack)
    with pytest.raises(ValueError,match='producer_identity_changed'):
        authority._worker(manifest,registry,'','',0)


def test_default_second_precision_source_without_first_binding_is_unproven():
    from eimemory.models.records import RecordEnvelope
    envelope=RecordEnvelope.create(kind='incident',title='default precision original',scope=ScopeRef.from_dict(SCOPE))
    facts=original_facts();facts['source_created_at']=envelope.time.created_at
    facts.pop('first_source_evidence')
    value=activation(facts);value['observed_at']=envelope.time.created_at.replace('+00:00','.123456+00:00')
    assert authority._activation_facts(facts,value,{'generation':'c'*64,'runtime_uid':1001},{'uid':1001})==(None,'original_first_physical_source_unproven')


def test_no_incident_checkpoint_accumulates_real_receive_rows_without_root_receipt(physical):
    conn=physical.store.sqlite.conn
    schema={row['name']:row['sql'] for row in conn.execute("SELECT name,sql FROM sqlite_master WHERE name LIKE 'trg_code_incident_coverage_%' OR name LIKE 'idx_incident_coverage_%' ORDER BY name")}
    facts=collect_physical_facts(physical.store.sqlite.path,manifest=dict(scopes=[SCOPE],schema_digest=digest(schema)),
        source_record_id='',source_digest='')
    assert facts['incident'] is None and facts['scope'] is None and facts['complete'] is True
    root_index=state();authority._ingest(root_index,facts,dict(scopes=[SCOPE]))
    assert root_index.execute("SELECT value FROM authority_meta WHERE key='high_water'").fetchone()[0]==str(facts['high_water'])
    assert root_index.execute('SELECT count(*) FROM managed_knowledge').fetchone()[0]==0


@pytest.mark.parametrize('source',['eimemory.ops.timer_monitor','evolution.observe','unknown.code-source'])
def test_unregistered_retained_history_accumulates_without_certifying_novelty(physical,source):
    from eimemory.models.records import RecordEnvelope
    old=RecordEnvelope.create(kind='incident',source=source,title='retained unclassified incident',
        scope=ScopeRef.from_dict(SCOPE),content={'incident_class':'release.closure_internal_failure',
            'diagnostic_codes':['typed_code']})
    physical.store.append(old)
    original=old.to_dict()
    conn=physical.store.sqlite.conn
    schema={row['name']:row['sql'] for row in conn.execute("SELECT name,sql FROM sqlite_master WHERE name LIKE 'trg_code_incident_coverage_%' OR name LIKE 'idx_incident_coverage_%' ORDER BY name")}
    before=conn.total_changes
    facts=collect_physical_facts(physical.store.sqlite.path,
        manifest=dict(scopes=[SCOPE],schema_digest=digest(schema)),source_record_id='',source_digest='')
    assert facts['complete'] is True and conn.total_changes==before
    retained=next(item for item in facts['history'] if item['record_id']==old.record_id)
    assert retained['defect_key']=='*' and retained['source_digest']==digest(original)
    assert physical.store.get_by_id(old.record_id).to_dict()==original
    index=state();authority._ingest(index,facts,dict(scopes=[SCOPE]))
    assert index.execute("SELECT defect_key FROM managed_knowledge WHERE record_id=?",(old.record_id,)).fetchone()[0]=='*'
    assert int(index.execute("SELECT value FROM authority_meta WHERE key='high_water'").fetchone()[0])==facts['through']
    current=original_facts()
    current.update(source_record_id='later-native-source',source_created_at='2099-01-01T00:00:00+00:00')
    # The preserved unknown history never becomes evidence of no prior report.
    assert authority._known(index,current)[:2]==(None,None)


@pytest.mark.skipif(os.name!='posix' or not hasattr(os,'geteuid') or os.geteuid()!=0,
                    reason='actual root issuer and unprivileged UID worker require native root fixture')
def test_native_root_private_key_actual_uid_worker_and_receipt_readback(tmp_path,physical,monkeypatch):
    """Full isolated FS/UID/SQLite/HMAC path. Timer receipt is a labeled fixture.

    Only the worker's fixed database literal is relocated in a private root-owned
    script, and AST-equivalence checks bind all remaining code. No host authority,
    service, main repository, runtime DB or production incident is touched.
    """
    import ast
    import pwd
    import time
    from eimemory.governance.evolution import managed_code_incident_proof as managed
    account=pwd.getpwnam('darrow')
    # Traversal for the real UID worker, confined to this disposable pytest tree.
    for path in (tmp_path,*tmp_path.parents):
        if path.stat().st_mode&0o111==0o111:break
        path.chmod((path.stat().st_mode&0o777)|0o111)
    fixture_source=tmp_path/'fixture-source';fixture_source.mkdir(mode=0o755)
    repository=Path(__file__).resolve().parents[1]
    for relative in authority.python_source_inventory(repository):
        if not relative.startswith('eimemory/'):continue
        destination=fixture_source/relative;destination.parent.mkdir(parents=True,exist_ok=True)
        destination.write_bytes((repository/relative).read_bytes());destination.chmod(0o644)
    site=fixture_source/'.venv'/'lib'/'site-packages';site.mkdir(parents=True)
    (site/'unrelated.py').write_text('raise AssertionError("must not execute candidate venv")')
    os.symlink('lib',fixture_source/'.venv'/'lib64',target_is_directory=True)
    control=tmp_path/'root-authority';control.mkdir(mode=0o700)
    receipts=control/'receipts';receipts.mkdir(mode=0o700)
    cache=receipts/'empty-pycache';cache.mkdir(mode=0o700)
    secret=control/'key';secret.write_bytes(b'isolated-root-private-signing-key-'*4);secret.chmod(0o600)
    monkeypatch.setattr(authority,'DATABASE_PATH',str(physical.store.sqlite.path))
    monkeypatch.setattr(authority,'MANIFEST_PATH',str(control/'manifest.json'))
    monkeypatch.setattr(authority,'KEY_PATH',str(secret));monkeypatch.setattr(authority,'RECEIPT_DIRECTORY',str(receipts))
    monkeypatch.setattr(authority,'PYCACHE_DIRECTORY',str(cache))
    native_source=Path(authority.__file__).read_text(encoding='utf8')
    relocated=native_source.replace("DATABASE_PATH = '/var/lib/eimemory/state/eimemory.sqlite'",
        'DATABASE_PATH = '+repr(str(physical.store.sqlite.path)),1)
    assert native_source!=relocated
    def without_db(source):
        tree=ast.parse(source)
        return ast.dump(ast.Module(body=[item for item in tree.body if not (
            isinstance(item,ast.Assign) and any(isinstance(target,ast.Name) and target.id=='DATABASE_PATH' for target in item.targets))],type_ignores=[]),include_attributes=False)
    assert without_db(native_source)==without_db(relocated)
    worker=tmp_path/'root-reviewed-worker.py';worker.write_text(relocated,encoding='utf8');worker.chmod(0o644)
    monkeypatch.setattr(authority,'__file__',str(worker))
    activation_reference={'schema':'eimemory.runtime_activation_receipt_ref.v1','receipt_id':'1'*64,'digest':'2'*64}
    issued_at=datetime.now(timezone.utc).isoformat()
    monkeypatch.setattr(managed,'native_activation_reference',lambda:deepcopy(activation_reference))
    actual=record_release_closure_failure(physical,scope=ScopeRef.from_dict(SCOPE),
        closure_report=dict(report_type='l5_release_closure',ok=False,closure_complete=False,data_accumulating=False,
            blocked_stage='release_lineage',blocked_reason='release_lineage_not_compatible',
            deployment=dict(commit=COMMIT,version='1.14.156'),release_lineage=dict(ok=True,compatible=False,unknown_production_paths=[])),
        detected_at=datetime.now(timezone.utc).isoformat())
    record=physical.store.get_by_id(actual['incident_record_id'],scope=ScopeRef.from_dict(SCOPE))
    # The isolated source is genuinely persisted with native precise first INSERT.
    from eimemory.ops.prepare_managed_incident_authority import prepare_manifest
    manifest=prepare_manifest(database_path=physical.store.sqlite.path,source_directory=fixture_source,
        scopes=[SCOPE],release_commit=COMMIT)
    manifest_path=Path(authority.MANIFEST_PATH);manifest_path.write_bytes(authority.canonical(manifest));manifest_path.chmod(0o640)
    registry=dict(generation='c'*64,runtime_user='darrow',runtime_uid=account.pw_uid,runtime_gid=account.pw_gid,runtime_commit=COMMIT,
        managed_incident_authority=dict(enabled=True,database_path=str(physical.store.sqlite.path),manifest_path=str(manifest_path),
            manifest_sha256=authority.sha256(manifest_path.read_bytes()).hexdigest(),receipt_directory=str(receipts),key_path=str(secret)))
    for path in [physical.store.root,*physical.store.root.rglob('*')]:
        if path.is_symlink():raise AssertionError('fixture runtime has a symlink')
        os.chown(path,account.pw_uid,account.pw_gid)
    preflight=authority.validate_configuration(registry=registry)
    assert preflight['key_material_returned'] is False
    denied=subprocess.run(['/usr/sbin/runuser','-u','darrow','--','/usr/bin/python3','-I','-S','-B','-c',
        'import pathlib,sys;pathlib.Path(sys.argv[1]).read_bytes()',str(secret)],capture_output=True)
    assert denied.returncode!=0
    activation_receipt=dict(schema='eimemory.runtime_activation_attestation.v1',signature_verified=True,
        reference=activation_reference,registry_generation=registry['generation'],scope=SCOPE,
        peer_pid=os.getpid(),peer_uid=account.pw_uid,manager='system',unit='eimemory-learn-watch.service',
        observed_at=issued_at,trigger_kind='scheduled_timer',job_state='running',job_id=42,
        runtime_commit=COMMIT,controller_commit=COMMIT)
    reference=authority.issue(record.record_id,digest(record.to_dict()),
        verified_activation_receipt=lambda *_:deepcopy(activation_receipt),registry=registry,peer={'uid':account.pw_uid,'pid':123})
    original=(receipts/(reference['receipt_id']+'.json')).read_bytes()
    state_before=(receipts/'knowledge.sqlite').read_bytes()
    proof=authority.verify(reference['receipt_id'],reference['digest'],registry=registry)['snapshot']
    assert proof['status']=='proven' and proof['global_facts']=={key:None for key in FIELDS}
    assert proof['managed_facts']=={key:False for key in FIELDS}
    assert (receipts/'knowledge.sqlite').read_bytes()==state_before
    assert (receipts/(reference['receipt_id']+'.json')).read_bytes()==original
    assert list(cache.iterdir())==[]
    # Actual serving157 may change an allowed aggregation file; the reviewed
    # reader/controller156 and original signed proof remain the authority.
    releases=tmp_path/'releases';releases.mkdir();candidate_commit='d'*40
    candidate=releases/candidate_commit;shutil.copytree(fixture_source,candidate,symlinks=True)
    (candidate/'eimemory/governance/l5/l5_reader.py').write_text('# allowed fixture aggregation repair\n')
    monkeypatch.setattr(authority,'RELEASES_DIRECTORY',str(releases))
    serving={**registry,'controller_commit':COMMIT,'runtime_commit':candidate_commit}
    original_readback=authority.verify(reference['receipt_id'],reference['digest'],registry=serving)
    assert original_readback['snapshot']==proof and original_readback['qualification_current']
    assert (receipts/(reference['receipt_id']+'.json')).read_bytes()==original
    checkpoint=authority.checkpoint(registry=serving,peer={'uid':account.pw_uid})
    assert checkpoint['status']=='ready' and checkpoint['incident_created'] is False
    next_issue_time=datetime.now(timezone.utc).isoformat()
    next_report=record_release_closure_failure(physical,scope=ScopeRef.from_dict(SCOPE),
        closure_report=dict(report_type='l5_release_closure',ok=False,closure_complete=False,data_accumulating=False,
            blocked_stage='release_lineage',blocked_reason='release_lineage_not_compatible',
            deployment=dict(commit=candidate_commit,version='1.14.157'),
            release_lineage=dict(ok=True,compatible=False,unknown_production_paths=[])),
        detected_at=datetime.now(timezone.utc).isoformat())
    next_record=physical.store.get_by_id(next_report['incident_record_id'],scope=ScopeRef.from_dict(SCOPE))
    next_activation={**activation_receipt,'observed_at':next_issue_time,'runtime_commit':candidate_commit,'controller_commit':COMMIT}
    # Same symptom remains known; a compatible candidate does not reset novelty.
    second=authority.issue(next_record.record_id,digest(next_record.to_dict()),
        verified_activation_receipt=lambda *_:deepcopy(next_activation),registry=serving,peer={'uid':account.pw_uid,'pid':123})
    second_proof=authority.verify(second['receipt_id'],second['digest'],registry=serving)['snapshot']
    assert second_proof['status']=='adverse' and second_proof['managed_facts']['known_before_detection'] is True
    (candidate/'eimemory/ops/release_closure_failure.py').write_text('# unauthorized producer change\n')
    with pytest.raises(ValueError,match='runtime_producer_changed'):
        authority.checkpoint(registry=serving,peer={'uid':account.pw_uid})
    # Runtime incompatibility blocks new issuance, not historical authenticity.
    assert authority.verify(reference['receipt_id'],reference['digest'],registry=serving)['snapshot']==proof


@pytest.fixture
def physical(tmp_path,monkeypatch):
    monkeypatch.setenv('EIMEMORY_EVIDENCE_RECEIPT_HMAC_KEY','managed-fixture-only-'+'f'*64)
    store=RuntimeStore(tmp_path/'runtime')
    try:yield SimpleNamespace(store=store)
    finally:store.close()


def test_real_native_source_and_sqlite_receive_prefix_are_read_only(physical):
    report=dict(report_type='l5_release_closure',ok=False,closure_complete=False,data_accumulating=False,
        blocked_stage='release_lineage',blocked_reason='release_lineage_not_compatible',
        deployment=dict(commit=COMMIT,version='1.14.156'),
        release_lineage=dict(ok=True,compatible=False,unknown_production_paths=[]))
    actual=record_release_closure_failure(physical,scope=ScopeRef.from_dict(SCOPE),closure_report=report,
        detected_at=datetime.now(timezone.utc).isoformat())
    record=physical.store.get_by_id(actual['incident_record_id'],scope=ScopeRef.from_dict(SCOPE))
    conn=physical.store.sqlite.conn
    schema={row['name']:row['sql'] for row in conn.execute("SELECT name,sql FROM sqlite_master WHERE name LIKE 'trg_code_incident_coverage_%' OR name LIKE 'idx_incident_coverage_%' ORDER BY name")}
    before=conn.total_changes
    facts=collect_physical_facts(physical.store.sqlite.path,manifest=dict(scopes=[SCOPE],schema_digest=digest(schema)),
        source_record_id=record.record_id,source_digest=digest(record.to_dict()))
    assert facts['complete'] is True and facts['global_facts']=={key:None for key in FIELDS}
    assert facts['source_digest']==digest(record.to_dict()) and conn.total_changes==before
    registry=state();authority._ingest(registry,facts,dict(scopes=[SCOPE]))
    known,reported,sequence,reason=authority._known(registry,facts)
    assert (known,reported,reason)==(False,False,'') and sequence>0


def _physical_prefix_fixture(physical):
    report=dict(report_type='l5_release_closure',ok=False,closure_complete=False,data_accumulating=False,
        blocked_stage='release_lineage',blocked_reason='release_lineage_not_compatible',
        deployment=dict(commit=COMMIT,version='1.14.156'),
        release_lineage=dict(ok=True,compatible=False,unknown_production_paths=[]))
    actual=record_release_closure_failure(physical,scope=ScopeRef.from_dict(SCOPE),closure_report=report,
        detected_at=datetime.now(timezone.utc).isoformat())
    record=physical.store.get_by_id(actual['incident_record_id'],scope=ScopeRef.from_dict(SCOPE))
    conn=physical.store.sqlite.conn
    schema={row['name']:row['sql'] for row in conn.execute("SELECT name,sql FROM sqlite_master WHERE name LIKE 'trg_code_incident_coverage_%' OR name LIKE 'idx_incident_coverage_%' ORDER BY name")}
    manifest=dict(scopes=[SCOPE],schema_digest=digest(schema))
    facts=collect_physical_facts(physical.store.sqlite.path,manifest=manifest,
        source_record_id=record.record_id,source_digest=digest(record.to_dict()))
    root=state();authority._ingest(root,facts,manifest)
    return manifest,facts,root,authority._receive_anchor(root)


def test_receive_anchor_is_same_original_logical_row_after_signed_cold_move(physical):
    from eimemory.models.records import RecordEnvelope
    from eimemory.storage.migrations.code_incident_coverage import JOURNAL
    from eimemory.storage.code_incident_coverage_store import CodeIncidentCoverageStore
    from eimemory.storage.code_incident_coverage_archive import TABLES
    from eimemory.governance.learning.effect_policy import seal,verify
    manifest,original,root,anchor=_physical_prefix_fixture(physical)
    for index in range(3):
        physical.store.append(RecordEnvelope.create(kind='reflection',source='managed.anchor.fixture',
            scope=ScopeRef.from_dict(SCOPE),title=f'ordinary subsequent write {index}'))
    result=CodeIncidentCoverageStore(physical.store).maintain_archive(
        seal=seal,verify=verify,keep={table:1 for table in TABLES})
    assert result['ok']
    assert physical.store.sqlite.conn.execute(f'SELECT 1 FROM {JOURNAL} WHERE sequence=?',
        (anchor['sequence'],)).fetchone() is None
    before=physical.store.sqlite.conn.total_changes
    later=collect_physical_facts(physical.store.sqlite.path,manifest=manifest,
        source_record_id=original['source_record_id'],source_digest=original['source_digest'],
        after=anchor['sequence'],cursor_anchor=anchor)
    assert later['cursor_anchor_verified'] is True and later['cursor_anchor']==anchor
    assert later['complete'] and physical.store.sqlite.conn.total_changes==before
    authority._ingest(root,later,manifest)
    assert authority._known(root,original)==(False,False,original['through'],'')
    assert authority._receive_anchor(root)['sequence']==later['through']>anchor['sequence']


@pytest.mark.parametrize('later_rows',[0,2])
def test_restored_sqlite_cannot_requalify_by_numerically_reattaining_old_watermark(physical,tmp_path,later_rows):
    from eimemory.models.records import RecordEnvelope
    from eimemory.storage.migrations.code_incident_coverage import JOURNAL
    # Preserve a real pre-detection SQLite snapshot, then observe the original
    # first INSERT into the live fixture before restoring that older snapshot.
    restored_root=tmp_path/'restored-runtime';(restored_root/'state').mkdir(parents=True)
    target=restored_root/'state/eimemory.sqlite'
    with sqlite3.connect(target) as backup:
        physical.store.sqlite.conn.backup(backup)
    manifest,original,root,anchor=_physical_prefix_fixture(physical)
    restored=RuntimeStore(restored_root)
    try:
        rewound=collect_physical_facts(restored.sqlite.path,manifest=manifest,
            source_record_id=original['source_record_id'],source_digest=original['source_digest'],
            after=anchor['sequence'],cursor_anchor=anchor)
        assert rewound['cursor_anchor_verified'] is False and rewound['high_water']<anchor['sequence']
        target_sequence=anchor['sequence']+later_rows
        previous_sequence=restored.sqlite.conn.execute(
            f'SELECT coalesce(max(sequence),0) FROM {JOURNAL}').fetchone()[0]
        replacements=target_sequence-previous_sequence
        assert 0<replacements<=100, 'isolated fixture replacement prefix must be bounded'
        for index in range(replacements):
            # These are distinct real writes, not retries of the same
            # reflection that RuntimeStore correctly deduplicates.
            replacement=RecordEnvelope.create(kind='reflection',source='managed.restore.fixture',
                scope=ScopeRef.from_dict(SCOPE),
                title=f'actual replacement after old SQLite snapshot {index}',
                content={'fixture_replacement_index':index,'target_sequence':target_sequence})
            appended=restored.append(replacement)
            assert appended.record_id==replacement.record_id
            next_sequence=restored.sqlite.conn.execute(
                f'SELECT coalesce(max(sequence),0) FROM {JOURNAL}').fetchone()[0]
            assert next_sequence==previous_sequence+1, 'each physical INSERT must advance the original journal'
            previous_sequence=next_sequence
        assert previous_sequence==target_sequence
        assert restored.sqlite.conn.execute('SELECT 1 FROM records WHERE record_id=?',
            (original['source_record_id'],)).fetchone() is None
        changes=restored.sqlite.conn.total_changes
        facts=collect_physical_facts(restored.sqlite.path,manifest=manifest,
            source_record_id=original['source_record_id'],source_digest=original['source_digest'],
            after=anchor['sequence'],cursor_anchor=anchor)
        assert facts['cursor_anchor_verified'] is False and facts['cursor_anchor']==anchor
        assert facts['high_water']==anchor['sequence']+later_rows
        assert facts['incident'] is None and facts['scope'] is None
        assert restored.sqlite.conn.total_changes==changes
    finally:
        restored.close()
    assert authority._receive_anchor(root)==anchor
    # Deleting a root witness is also not a legitimate legacy-reset route.
    root.execute("DELETE FROM authority_meta WHERE key='receive_anchor'")
    with pytest.raises(ValueError,match='receive_anchor_unverified'):
        authority._ingest(root,original,manifest)
    assert authority._anchor_gap(root,None,anchor['sequence'])=='receive_prefix_anchor_unavailable'
    assert not authority._catchup_progress(root,manifest)['complete']


@pytest.mark.parametrize('fault', ['', 'missing', 'digest', 'too_many'])
def test_skipped_reviewed_manifests_preserve_knowledge_and_anchor(monkeypatch, fault):
    conn=state();manifest={'scopes':[SCOPE]};facts=original_facts()
    authority._ingest(conn,facts,manifest)
    conn.execute('CREATE TABLE managed_manifest_history(digest TEXT PRIMARY KEY)')
    before_knowledge=[tuple(row) for row in conn.execute('SELECT * FROM managed_knowledge')]
    before_meta=dict(conn.execute('SELECT key,value FROM authority_meta'))
    archive={};current=manifest
    for index in range(130 if fault=='too_many' else 3):
        following={'scopes':[SCOPE],'previous_manifest_digest':authority.digest(current),'release_index':index}
        archive[authority.digest(current)]=current
        current=following
    if fault=='missing':archive.clear()
    elif fault=='digest':archive[current['previous_manifest_digest']]={'tampered':True}
    monkeypatch.setattr(authority,'_reviewed_manifest_archive',lambda:archive)
    if fault:
        with pytest.raises(ValueError,match='continuity_unverified'):authority._prepare_manifest_catchup(conn,current)
        assert dict(conn.execute('SELECT key,value FROM authority_meta'))==before_meta
    else:
        authority._prepare_manifest_catchup(conn,current)
        assert authority._catchup_progress(conn,current)=={'epoch':3,'cursor':0,'complete':False,'high_water':1}
        after_meta=dict(conn.execute('SELECT key,value FROM authority_meta'))
        assert after_meta['receive_anchor']==before_meta['receive_anchor']
        assert after_meta.get('uncertainty')==before_meta.get('uncertainty')
    assert [tuple(row) for row in conn.execute('SELECT * FROM managed_knowledge')]==before_knowledge


def test_manifest_archive_requires_completed_root_history_and_protected_reads(tmp_path,monkeypatch):
    completed=tmp_path/'controller-20261021T060000Z-123'
    incomplete=tmp_path/'controller-20261021T060001Z-124'
    unrelated=tmp_path/'config-repairs'
    for p in (completed,incomplete,unrelated):p.mkdir()
    manifest={'schema':'managed_code_incident_manifest.v1','qualification_domain':authority.DOMAIN,
        'defect_key_semantics':authority.KEY_SEMANTICS,'enabled':True,'previous_manifest_digest':'a'*64}
    (completed/'complete.json').write_text(json.dumps({'ok':True,'controller_commit':'a'*40,
        'registry_generation':'b'*64,'manifest_sha256':'c'*64}),encoding='utf-8')
    for p in (completed,incomplete,unrelated):
        (p/'managed-manifest.snapshot').write_text(json.dumps(manifest),encoding='utf-8')
    monkeypatch.setattr(authority,'CONTROLLER_HISTORY_DIRECTORY',str(tmp_path))
    reads=[]
    def protected_read(path,**kwargs):
        reads.append((Path(path),kwargs));return Path(path).read_bytes()
    monkeypatch.setattr(authority,'_read',protected_read)
    assert authority._reviewed_manifest_archive()=={authority.digest(manifest):manifest}
    assert all(kwargs.get('mode')==0o600 for _,kwargs in reads)
    assert not any(path.parent==unrelated for path,_ in reads)
    assert not any(path.name=='managed-manifest.snapshot' and path.parent==incomplete for path,_ in reads)
@pytest.mark.parametrize('error,preserved',[
    ('managed_authority_lock_timeout',True),('managed_authority_manifest_continuity_unverified',True),
    ('manager_transport_unavailable',True),('manager_peer_uid_denied',True),
    ('manager_error /PRIVATE/PATH?token=SECRET',False),('PRIVATE_SECRET',False),
    ('managed_authority_'+('x'*97),False)])
def test_checkpoint_failure_keeps_only_fixed_control_plane_reason_codes(monkeypatch,error,preserved):
    from eimemory.core import runtime_manager
    from eimemory.governance.evolution.managed_code_incident_proof import checkpoint_managed_ingress
    def fail():raise runtime_manager.RuntimeManagerError(error)
    monkeypatch.setattr(runtime_manager,'checkpoint_managed_incident_knowledge',fail)
    result=checkpoint_managed_ingress()
    assert result['status']=='unproven' and result['qualifies_for_product_completion'] is False
    assert result['reason']=='managed_ingress_checkpoint_unavailable'
    assert result.get('error_code')==(error if preserved else None)
    assert 'PRIVATE' not in json.dumps(result) and 'SECRET' not in json.dumps(result)


def test_checkpoint_non_manager_exception_cannot_supply_a_manager_code(monkeypatch):
    from eimemory.core import runtime_manager
    from eimemory.governance.evolution.managed_code_incident_proof import checkpoint_managed_ingress
    def fail():raise ValueError('managed_authority_lock_timeout')
    monkeypatch.setattr(runtime_manager,'checkpoint_managed_incident_knowledge',fail)
    result=checkpoint_managed_ingress()
    assert result['error_type']=='ValueError' and 'error_code' not in result

def test_managed_worker_continues_small_cold_frames_and_retains_prior_uncertainty(physical,monkeypatch):
    from eimemory.models.records import RecordEnvelope
    from eimemory.storage import code_incident_coverage_archive as archive_module
    from eimemory.storage.code_incident_coverage_archive import TABLES
    from eimemory.storage.code_incident_coverage_store import CodeIncidentCoverageStore
    from eimemory.storage.migrations.code_incident_coverage import JOURNAL
    from eimemory.governance.learning.effect_policy import seal,verify
    manifest,original,root,anchor=_physical_prefix_fixture(physical)
    for index in range(8):
        physical.store.append(RecordEnvelope.create(kind='reflection',source='ordinary.fixture',
            scope=ScopeRef.from_dict(SCOPE),title=f'original retained metadata {index}'))
    conn=physical.store.sqlite.conn
    before=[dict(row) for row in conn.execute(f'SELECT * FROM {JOURNAL} ORDER BY sequence')]
    with monkeypatch.context() as patch:
        patch.setattr(archive_module,'MAX_ROWS',1)
        CodeIncidentCoverageStore(physical.store).maintain_archive(seal=seal,verify=verify,
            keep={table:1 for table in TABLES},batches=64)
    monkeypatch.setattr(archive_module,'MAX_READ_SEGMENTS',2)
    root.execute("INSERT OR REPLACE INTO authority_meta VALUES('uncertainty','typed_ingress_mutation_or_unregistered')")
    after=original['through'];seen=list(original['rows']);pages=0
    while True:
        facts=collect_physical_facts(physical.store.sqlite.path,manifest=manifest,
            source_record_id='',source_digest='',after=after)
        assert facts['cursor_anchor_verified'] and 0<facts['through']-after<=5000
        seen.extend(facts['rows']);pages+=1
        # This fixture authority is isolated memory, never a formal ledger.
        authority._ingest(root,facts,manifest)
        assert root.execute("SELECT value FROM authority_meta WHERE key='uncertainty'").fetchone()[0]=='typed_ingress_mutation_or_unregistered'
        if facts['complete']:break
        assert facts['through']<facts['high_water']
        after=facts['through']
    assert pages>1 and seen==before
    assert archive_module.MAX_READ_SEGMENTS==2
