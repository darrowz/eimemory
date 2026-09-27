"""Full-repository wiring checks. Not executable in the source-slice harness.

These require the real RuntimeStore schema, record normalization, MemoryAPI,
reader pool and projections. No remote services or production data are used.
"""
from __future__ import annotations
from dataclasses import asdict
import sqlite3
from types import SimpleNamespace

import pytest
from eimemory.api.memory import MemoryAPI
from eimemory.api.evolution import EvolutionAPI
from eimemory.models.records import RecordEnvelope, ScopeRef
from eimemory.models.memory_edges import MemoryEdge
from eimemory.storage.runtime_store import RuntimeStore
from eimemory.governance.learning import rule_evolution as gates

SCOPE={'tenant_id':'audit-isolated','agent_id':'audit-agent','workspace_id':'audit-workspace','user_id':'audit-user','preserve_scope':True}

def scope_ref(user_id='audit-user'):
    return ScopeRef(tenant_id='audit-isolated',agent_id='audit-agent',workspace_id='audit-workspace',user_id=user_id)

@pytest.fixture
def real_store(tmp_path):
    store=RuntimeStore(tmp_path)
    try: yield store
    finally: store.close()

def memory_record(scope=None):
    return RecordEnvelope.create(kind='memory',title='Audit durable setting',
        summary='The audit fixture uses a durable setting for transactional version replacement.',
        content={'text':'The audit fixture uses a durable setting for transactional version replacement.'},
        scope=scope or scope_ref(),source='audit.integration',
        meta={'semantic_key':'deep-audit-version-key','force_capture':True})

def lose_fast_lookup_once(monkeypatch,store,record_id):
    original=store.get_by_id; lost=False
    def lookup(rid,*args,**kwargs):
        nonlocal lost
        if rid==record_id and not lost:
            lost=True
            return None
        return original(rid,*args,**kwargs)
    monkeypatch.setattr(store,'get_by_id',lookup)
    return original

@pytest.mark.parametrize('memory_type',['episodic','durable_fact'])
def test_real_ingest_rechecks_conflicting_id_inside_transaction(real_store,monkeypatch,memory_type):
    api=MemoryAPI(real_store)
    args=dict(memory_type=memory_type,title='Audit conflict guard',scope=SCOPE,force_capture=True,
              source='audit.integration',record_id='mem_deep_audit_conflict')
    first=api.ingest(text='The authoritative fixture value is alpha.',**args)
    original=lose_fast_lookup_once(monkeypatch,real_store,first.record_id)
    with pytest.raises(ValueError,match='record_id conflict'):
        api.ingest(text='A competing fixture attempts to replace it with beta.',**args)
    assert original(first.record_id,scope=first.scope).summary==first.summary

@pytest.mark.parametrize('memory_type',['episodic','durable_fact'])
def test_real_ingest_same_digest_lost_fast_lookup_is_idempotent(real_store,monkeypatch,memory_type):
    api=MemoryAPI(real_store)
    args=dict(text='The deterministic fixture value remains alpha.',memory_type=memory_type,
              title='Audit retry guard',scope=SCOPE,force_capture=True,source='audit.integration',
              record_id='mem_deep_audit_retry')
    first=api.ingest(**args)
    before=(real_store.root/'records.jsonl').read_bytes()
    lose_fast_lookup_once(monkeypatch,real_store,first.record_id)
    retry=api.ingest(**args)
    assert retry.time.created_at==first.time.created_at
    assert (real_store.root/'records.jsonl').read_bytes()==before

@pytest.mark.slow
def test_real_schema_supersession_drains_all_pages(real_store):
    old=[]
    with real_store.locked() as sql:
        for _ in range(1005):
            item=memory_record(); sql.upsert(item,commit=False); old.append(item.record_id)
        sql.commit()
    new=memory_record()
    real_store.append_and_supersede(new,semantic_key='deep-audit-version-key')
    with real_store.locked() as sql:
        active=sql.execute('SELECT count(*) FROM records WHERE semantic_key=? AND status=?',
                           ('deep-audit-version-key','active')).fetchone()[0]
    assert active==1
    assert {link.target_id for link in new.links if link.relation=='supersedes'}==set(old)

def test_real_reader_pool_pins_snapshot(real_store):
    with real_store.locked() as sql:
        sql.execute('CREATE TABLE audit_snapshot_probe(value TEXT)')
        sql.execute("INSERT INTO audit_snapshot_probe VALUES('old')"); sql.commit()
    writer=sqlite3.connect(real_store.sqlite.path,timeout=5)
    def callback(sql):
        first=sql.execute('SELECT value FROM audit_snapshot_probe').fetchone()[0]
        writer.execute("UPDATE audit_snapshot_probe SET value='new'"); writer.commit()
        second=sql.execute('SELECT value FROM audit_snapshot_probe').fetchone()[0]
        return first,second
    try: assert real_store.read_consistent(callback)==('old','old')
    finally: writer.close()

def test_real_append_preserves_outer_transaction(real_store):
    with real_store.locked() as sql:
        sql.execute('CREATE TABLE audit_outer_probe(value TEXT)'); sql.commit()
        sql.execute('BEGIN'); sql.execute("INSERT INTO audit_outer_probe VALUES('pending')")
        try:
            with pytest.raises(RuntimeError,match='own_transaction'):
                real_store.append(memory_record())
            assert sql.in_transaction
            assert sql.execute('SELECT count(*) FROM audit_outer_probe').fetchone()[0]==1
        finally: sql.rollback()

def test_real_edge_identity_conflict_is_rejected(real_store):
    edge=MemoryEdge.create(scope=scope_ref(),from_id='mem_a',to_id='mem_b',edge_type='temporal',confidence=0.3)
    with real_store.locked() as sql: sql.upsert_memory_edges([edge])
    forged=MemoryEdge.from_dict(edge.to_dict()); forged.scope=scope_ref(user_id='other-user'); forged.reason='forged'
    with real_store.locked() as sql:
        with pytest.raises(ValueError,match='immutable_identity'): sql.upsert_memory_edges([forged])
        assert sql.execute('SELECT reason FROM memory_edges WHERE edge_id=?',(edge.edge_id,)).fetchone()[0]==edge.reason

def test_real_distinct_structured_reflections_both_persist(real_store):
    def observation(n):
        return RecordEnvelope.create(kind='reflection',title='Repeated metric',summary='Same display text',
            content={'payload':{'metric':n}},scope=scope_ref(),source='audit.integration')
    first=real_store.append(observation(1)); second=real_store.append(observation(2))
    assert first.record_id!=second.record_id
    assert real_store.get_by_id(second.record_id,scope=second.scope).content['payload']['metric']==2

def test_real_shared_reflection_does_not_replace_private_write(real_store):
    shared=RecordEnvelope.create(kind='reflection',title='Scoped reflection',summary='Same text',scope=scope_ref(''),source='audit.integration')
    private=RecordEnvelope.create(kind='reflection',title='Scoped reflection',summary='Same text',scope=scope_ref(),source='audit.integration')
    real_store.append(shared); result=real_store.append(private)
    assert result.scope==private.scope and result.record_id==private.record_id

def test_real_replay_rule_remains_diagnostic(real_store):
    evolution=EvolutionAPI(real_store)
    rule=evolution.store_rule(title='Candidate',summary='A candidate not installed for execution.',task_type='audit',
        retrieval_policy={'route_hint':'unexercised'},scope=asdict(scope_ref()),status='accepted')
    report=evolution.replay_rule(record_id=rule.record_id,dataset=[])
    assert report.meta['verdict']=='diagnostic_only'
    assert report.meta['promotion_eligible'] is False
    assert not gates._is_actual_replay_result(report)

def test_real_user_loop_does_not_offer_shared_rule_promotion(real_store,monkeypatch):
    evolution=EvolutionAPI(real_store); shared=scope_ref('')
    rule=evolution.store_rule(title='Shared accepted rule',summary='A shared rule requires shared-scope authority.',
        task_type='audit',retrieval_policy={},scope=asdict(shared),status='accepted')
    evolution.feedback(target_ref={'kind':'rule','record_id':rule.record_id},decision='accept',
        reason='Test fixture feedback',reviewed_by='test-fixture',scope=asdict(shared))
    replay=RecordEnvelope.create(kind='replay_result',title='Verifier fixture',scope=shared,
        source='test.behavioral_verifier',meta={'target_rule_id':rule.record_id,'verdict':'pass','pass_rate':1.0,'sample_size':1})
    real_store.append(replay)
    monkeypatch.setattr(gates,'_build_roi_summary',lambda *args,**kwargs:{'roi_signal':1.0})
    result=gates.run_rule_evolution_loop(SimpleNamespace(store=real_store,evolution=evolution),asdict(scope_ref()),apply=False)
    assert result['promoted_count']==0


def test_real_ingest_private_identity_does_not_fall_back_to_shared_record(real_store):
    api=MemoryAPI(real_store)
    args=dict(text='This isolated fixture has the same identifier in two independent scopes.',memory_type='episodic',
              title='Exact ingest identity',force_capture=True,source='audit.integration',record_id='mem_exact_ingest_scope')
    shared=api.ingest(scope={**SCOPE,'user_id':''},**args)
    private=api.ingest(scope=SCOPE,**args)
    assert private.scope.user_id=='audit-user' and shared.scope.user_id==''
    assert real_store.get_by_id(private.record_id,scope=private.scope,exact_scope=True).scope==private.scope
    assert real_store.get_by_id(shared.record_id,scope=shared.scope,exact_scope=True).scope==shared.scope


def test_real_scoped_promotion_with_reused_record_id(real_store):
    own=RecordEnvelope.create(kind='rule',title='Scoped rule',scope=scope_ref(),status='accepted')
    other=RecordEnvelope.create(kind='rule',title='Other owner rule',scope=scope_ref('another-user'),status='accepted')
    other.record_id=own.record_id
    real_store.append(own); real_store.append(other)
    EvolutionAPI(real_store).promote_rule(record_id=own.record_id,scope=own.scope,promoter='audit.integration')
    assert real_store.get_by_id(own.record_id,scope=own.scope,exact_scope=True).status=='active'
    assert real_store.get_by_id(other.record_id,scope=other.scope,exact_scope=True).status=='accepted'
