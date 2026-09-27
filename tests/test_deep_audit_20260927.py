"""Function-level regression contracts on real SQLite, with explicit test fixtures.

SqliteProbe is a minimal reproduction schema, NOT the production migrations or
recall index. RuntimeProbe replaces only record serialization, projection, and
bootstrap plumbing. The patched storage/governance methods themselves are used.
Run the separate integration suite against the full repository before release.
"""
from __future__ import annotations
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from hashlib import sha256
import inspect
import itertools
import json
from pathlib import Path
import sqlite3
import threading
from types import SimpleNamespace
import uuid

import pytest
from eimemory.models.records import ScopeRef, LinkRef
from eimemory.models.memory_edges import MemoryEdge, stable_memory_edge_id
from eimemory.storage.sqlite_store import SqliteRecordStore
from eimemory.storage.runtime_store import RuntimeStore
from eimemory.governance.learning import rule_evolution as gates
from eimemory.api import evolution as evolution_module

_COUNTER=itertools.count(1)
@dataclass
class RecordFixture:
    record_id: str=field(default_factory=lambda:'rec_'+uuid.uuid4().hex)
    kind: str='memory'
    status: str='active'
    title: str='fixture title'
    summary: str='fixture summary'
    detail: str=''
    content: dict=field(default_factory=dict)
    tags: list=field(default_factory=list)
    links: list=field(default_factory=list)
    evidence: list=field(default_factory=list)
    source: str='audit.fixture'
    scope: ScopeRef=field(default_factory=lambda:ScopeRef('tenant','agent','workspace','user'))
    time: dict=field(default_factory=lambda:{'created_at':f'{next(_COUNTER):012d}'})
    provenance: dict=field(default_factory=dict)
    meta: dict=field(default_factory=dict)
    source_id: str='default'
    def touch(self): self.time['updated_at']=f'{next(_COUNTER):012d}'
    def to_dict(self): return asdict(self)
    @classmethod
    def create(cls,**kw): return cls(**kw)
    @classmethod
    def from_dict(cls,d):
        d=dict(d); d['scope']=ScopeRef.from_dict(d['scope'])
        d['links']=[LinkRef(**link) for link in d.get('links',[])]
        return cls(**d)

SCHEMA='''
CREATE TABLE IF NOT EXISTS records(
 storage_key TEXT PRIMARY KEY,record_id TEXT NOT NULL,kind TEXT NOT NULL,status TEXT NOT NULL,
 source_id TEXT NOT NULL,tenant_id TEXT,agent_id TEXT,workspace_id TEXT,user_id TEXT,
 updated_at TEXT,meta_json TEXT,payload_json TEXT,payload_pointer_json TEXT DEFAULT '',payload_digest TEXT DEFAULT '');
CREATE TABLE IF NOT EXISTS outbox(id INTEGER PRIMARY KEY,stream TEXT,payload TEXT);
CREATE TABLE IF NOT EXISTS sentinel(value TEXT);
CREATE TABLE IF NOT EXISTS memory_edges(
 edge_id TEXT PRIMARY KEY,from_id TEXT,to_id TEXT,edge_type TEXT,confidence REAL,evidence_id TEXT,
 tenant_id TEXT,agent_id TEXT,workspace_id TEXT,user_id TEXT,reason TEXT,meta_json TEXT,created_at TEXT,updated_at TEXT);
'''

class SqliteProbe(SqliteRecordStore):
    def __init__(self,path=':memory:'):
        self.conn=sqlite3.connect(str(path),timeout=10,check_same_thread=False)
        self.conn.row_factory=sqlite3.Row
        self.conn.execute('PRAGMA journal_mode=WAL')
        self.conn.executescript(SCHEMA)
    def assert_connection_lock_held(self):
        # Lock enforcement/production bootstrap is outside this fixture's scope.
        pass
    def execute(self,sql,parameters=(),/): return self.conn.execute(sql,parameters)
    def commit(self): self.conn.commit()
    def rollback(self): self.conn.rollback()
    @property
    def in_transaction(self): return self.conn.in_transaction
    def upsert(self,record,*,commit=True):
        key=self._storage_key(record)
        existing=self.conn.execute('SELECT source_id FROM records WHERE storage_key=?',(key,)).fetchone()
        if existing and existing[0]!=record.source_id:
            raise ValueError('source_id move requires an explicit mutation path')
        self.conn.execute('''INSERT INTO records(storage_key,record_id,kind,status,source_id,
        tenant_id,agent_id,workspace_id,user_id,updated_at,meta_json,payload_json)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(storage_key) DO UPDATE SET
        status=excluded.status,source_id=excluded.source_id,tenant_id=excluded.tenant_id,
        agent_id=excluded.agent_id,workspace_id=excluded.workspace_id,user_id=excluded.user_id,
        updated_at=excluded.updated_at,meta_json=excluded.meta_json,payload_json=excluded.payload_json''',
        (key,record.record_id,record.kind,record.status,record.source_id,
         record.scope.tenant_id,record.scope.agent_id,record.scope.workspace_id,record.scope.user_id,
         record.time.get('updated_at',record.time['created_at']),json.dumps(record.meta),json.dumps(record.to_dict())))
        if commit: self.conn.commit()
    def _record_from_storage_row(self,row,*,hydrate=True):
        try: return RecordFixture.from_dict(json.loads(row['payload_json']))
        except (ValueError,KeyError,TypeError): return None
    def _record_matches_projection_row(self,rec,row):
        return (rec.record_id==row['record_id'] and rec.kind==row['kind']
                and rec.status==row['status'] and rec.source_id==row['source_id']
                and all(getattr(rec.scope,n)==row[n] for n in ('tenant_id','agent_id','workspace_id','user_id')))
    def _apply_scope_filters(self,where,params,scope):
        # Reproduce ordinary read visibility; application-specific aliases are
        # deliberately not modeled by this minimal fixture.
        where.extend(['tenant_id=?','agent_id=?','workspace_id=?',"(user_id=? OR user_id='')"])
        params.extend([scope.tenant_id,scope.agent_id,scope.workspace_id,scope.user_id])
    def enqueue_export(self,*,stream,payload,commit=False):
        cursor=self.conn.execute('INSERT INTO outbox(stream,payload) VALUES(?,?)',(stream,json.dumps(payload)))
        if commit: self.conn.commit()
        return {'operation_id':str(cursor.lastrowid)}

class RuntimeProbe(RuntimeStore):
    def __init__(self,path=':memory:'):
        self.sqlite=SqliteProbe(path); self._lock=threading.RLock(); self.projected=[]
    def _enqueue_record_exports(self,record):
        return [self.sqlite.enqueue_export(stream='records',payload=record.to_dict())]
    def _auxiliary_entry(self,stream,payload,*,scope): return payload
    def _safe_post_commit_projection(self,exports,records):
        assert not self.sqlite.in_transaction
        self.projected.extend(records)
    def list_records(self,*,kinds=None,scope=None,limit=200,**kw):
        rows=self.sqlite.conn.execute('SELECT * FROM records ORDER BY updated_at DESC').fetchall()
        records=[self.sqlite._record_from_storage_row(row) for row in rows]
        return [r for r in records if r and (not kinds or r.kind in kinds) and (scope is None or
            (r.scope.tenant_id,r.scope.agent_id,r.scope.workspace_id)==(scope.tenant_id,scope.agent_id,scope.workspace_id)
            and r.scope.user_id in {scope.user_id,''})][:limit]
    @contextmanager
    def borrow_reader(self):
        with self._lock: yield SimpleNamespace(store=self.sqlite)
    def close(self): self.sqlite.conn.close()

@pytest.fixture
def store():
    obj=RuntimeProbe()
    try: yield obj
    finally: obj.close()

def record(**kw):
    kw.setdefault('meta',{'semantic_key':'key'})
    return RecordFixture(**kw)

def stored(store,rec):
    method=store.sqlite.get_by_id
    options={'exact_scope':True} if 'exact_scope' in inspect.signature(method).parameters else {}
    return method(rec.record_id,scope=rec.scope,**options)

def append_with_ingest_contract(store,rec,*,supersede=False):
    """Model the API's already-completed fast lookup and atomic recheck handoff.

    The old API does not pass existing_match; feature detection permits this
    same regression to show the old overwrite rather than an argument error.
    The full-API integration tests separately verify real MemoryAPI wiring.
    """
    method=store.append_and_supersede if supersede else store.append
    options={}
    if 'existing_match' in inspect.signature(method).parameters:
        options['existing_match']=lambda old: old.scope==rec.scope and old.source_id==rec.source_id and old.meta.get('request_digest')==rec.meta.get('request_digest')
    if supersede: options['semantic_key']='key'
    return method(rec,**options)

def run_gate(rule,feedback,replay,*,scope=None,rate=1.0,roi=0.0,minimum=0.0):
    kwargs=dict(rules=[rule],feedback_records=[feedback],replay_results=[replay],min_roi=minimum,roi_summary={'roi_signal':roi})
    if 'scope' in inspect.signature(gates._promotion_candidates).parameters:
        kwargs['scope']=scope or rule.scope
    return gates._promotion_candidates(**kwargs)

def evidence():
    rule=record(kind='rule',status='accepted')
    fb=record(kind='feedback',scope=rule.scope,meta={'decision':'accept','target_ref':{'record_id':rule.record_id}})
    rp=record(kind='replay_result',scope=rule.scope,source='test.behavioral_verifier',
              meta={'verdict':'pass','pass_rate':1.0,'sample_size':2,'target_rule_id':rule.record_id})
    return rule,fb,rp

@pytest.mark.parametrize('name',['tenant_id','agent_id','workspace_id','user_id'])
def test_scope_rejects_reserved_separator(name):
    with pytest.raises(ValueError): ScopeRef(**{name:'a\x1fb'})

@pytest.mark.parametrize('name',['tenant_id','agent_id','workspace_id','user_id','record_id'])
def test_storage_keys_reject_separator_even_without_scope_constructor(store,name):
    values=dict(tenant_id='t',agent_id='a',workspace_id='w',user_id='u',record_id='rec_x')
    values[name]='a\x1fb'
    with pytest.raises(ValueError): store.sqlite._storage_key_from_values(**values)

def test_safe_legacy_storage_key_bytes_preserved(store):
    actual=store.sqlite._storage_key_from_values(tenant_id='',agent_id='agent',workspace_id='工作区|x',user_id='',record_id='rec_1')
    assert actual=='\x1f'.join(['default','agent','工作区|x','','rec_1'])

@pytest.mark.parametrize('field_name',['from_id','to_id','evidence_id'])
def test_edge_id_components_cannot_alias(field_name):
    values=dict(scope=ScopeRef('t','a','w','u'),from_id='rec_from',to_id='rec_to',edge_type='temporal',evidence_id='rec_evidence')
    values[field_name]='x\x1fy'
    with pytest.raises(ValueError): stable_memory_edge_id(**values)

def test_safe_edge_id_bytes_preserved():
    args=dict(scope=ScopeRef('t','a','w','u'),from_id='rec_a',to_id='rec_b',edge_type='temporal',evidence_id='rec_e')
    raw='\x1f'.join(['t','a','w','u','temporal','rec_a','rec_b','rec_e'])
    assert stable_memory_edge_id(**args)=='edge_'+sha256(raw.encode()).hexdigest()[:24]

def seed_versions(store,count,**kw):
    old=[]
    for _ in range(count):
        rec=record(**kw); store.sqlite.upsert(rec,commit=False); old.append(rec)
    store.sqlite.commit(); return old

def test_supersede_drains_more_than_one_thousand_versions(store):
    old=seed_versions(store,1005); new=record()
    store.append_and_supersede(new,semantic_key='key')
    assert all(stored(store,r).status=='superseded' for r in old)
    assert len(stored(store,new).links)==1005
    snapshots=[json.loads(row[0]) for row in store.sqlite.conn.execute("SELECT payload FROM outbox WHERE stream='records'")]
    assert [p for p in snapshots if p['record_id']==new.record_id][-1]['links']==new.to_dict()['links']

def test_shared_versions_do_not_starve_private_supersession(store):
    own=seed_versions(store,1)[0]
    shared_scope=ScopeRef('tenant','agent','workspace','')
    shared=seed_versions(store,1005,scope=shared_scope)
    new=record(); store.append_and_supersede(new,semantic_key='key')
    assert stored(store,own).status=='superseded'
    assert all(stored(store,r).status=='active' for r in shared)

def test_other_source_is_unchanged(store):
    old=seed_versions(store,1,source_id='another')[0]
    store.append_and_supersede(record(),semantic_key='key')
    assert stored(store,old).status=='active'

def test_candidate_query_unavailable_rolls_back(store,monkeypatch):
    old=seed_versions(store,1)[0]; new=record()
    monkeypatch.setattr(store.sqlite,'list_records_by_meta_value',lambda **kw:None)
    with pytest.raises(RuntimeError): store.append_and_supersede(new,semantic_key='key')
    assert stored(store,new) is None and stored(store,old).status=='active'
    assert store.sqlite.conn.execute('SELECT count(*) FROM outbox').fetchone()[0]==0

def test_unreadable_exact_candidate_is_not_silently_ignored(store):
    old=seed_versions(store,1)[0]; new=record()
    store.sqlite.conn.execute("UPDATE records SET payload_json='broken' WHERE record_id=?",(old.record_id,)); store.sqlite.commit()
    with pytest.raises((RuntimeError,ValueError)): store.append_and_supersede(new,semantic_key='key')
    assert stored(store,new) is None

def test_inactive_new_record_does_not_supersede(store):
    old=seed_versions(store,1)[0]
    store.append_and_supersede(record(status='rejected'),semantic_key='key')
    assert stored(store,old).status=='active'

def test_second_page_error_rolls_back_first_page(store,monkeypatch):
    old=seed_versions(store,1005); new=record(); original=store.sqlite.upsert; changed=[]
    def failing(rec,*,commit=True):
        if rec.status=='superseded':
            changed.append(rec.record_id)
            if len(changed)==1001: raise OSError('second page failure')
        return original(rec,commit=commit)
    monkeypatch.setattr(store.sqlite,'upsert',failing)
    with pytest.raises(OSError): store.append_and_supersede(new,semantic_key='key')
    assert stored(store,new) is None
    assert all(stored(store,r).status=='active' for r in old)
    assert not store.sqlite.in_transaction

@pytest.mark.parametrize('supersede',[False,True])
def test_explicit_id_conflicting_retry_cannot_overwrite(store,supersede):
    first=record(meta={'semantic_key':'key','request_digest':'first'})
    other=record(record_id=first.record_id,summary='different request',meta={'semantic_key':'key','request_digest':'other'})
    append_with_ingest_contract(store,first,supersede=supersede)
    with pytest.raises(ValueError): append_with_ingest_contract(store,other,supersede=supersede)
    assert stored(store,first).summary==first.summary

@pytest.mark.parametrize('supersede',[False,True])
def test_same_digest_retry_returns_stored_record_without_duplicate_export(store,supersede):
    first=record(meta={'semantic_key':'key','request_digest':'same'})
    retry=RecordFixture.from_dict(first.to_dict()); retry.time={'created_at':'999999999999'}
    append_with_ingest_contract(store,first,supersede=supersede)
    result=append_with_ingest_contract(store,retry,supersede=supersede)
    assert result.time==first.time
    assert store.sqlite.conn.execute("SELECT count(*) FROM outbox WHERE stream='records'").fetchone()[0]==1

@pytest.mark.parametrize('same_digest',[False,True])
def test_two_connections_contending_for_explicit_id(tmp_path,same_digest):
    a=RuntimeProbe(tmp_path/'race.sqlite'); b=RuntimeProbe(tmp_path/'race.sqlite')
    barrier=threading.Barrier(2); results=[]
    r1=record(meta={'request_digest':'one'}); r2=record(record_id=r1.record_id,meta={'request_digest':'one' if same_digest else 'two'})
    def worker(store,rec):
        try:
            # Both API fast lookups observe no row before either append.
            assert stored(store,rec) is None
            barrier.wait(timeout=5)
            results.append(('ok',append_with_ingest_contract(store,rec).meta['request_digest']))
        except Exception as exc: results.append((type(exc).__name__,str(exc)))
    threads=[threading.Thread(target=worker,args=args) for args in [(a,r1),(b,r2)]]
    try:
        for t in threads: t.start()
        for t in threads: t.join(timeout=12)
        assert not any(t.is_alive() for t in threads)
        statuses=sorted(item[0] for item in results)
        assert statuses==(['ok','ok'] if same_digest else ['ValueError','ok'])
        assert a.sqlite.conn.execute("SELECT count(*) FROM outbox WHERE stream='records'").fetchone()[0]==1
    finally: a.close(); b.close()

def test_append_does_not_commit_callers_transaction(store):
    store.sqlite.execute('BEGIN'); store.sqlite.execute("INSERT INTO sentinel VALUES('pending')")
    with pytest.raises(RuntimeError): store.append(record())
    assert store.sqlite.in_transaction
    store.sqlite.rollback()
    assert store.sqlite.conn.execute('SELECT count(*) FROM sentinel').fetchone()[0]==0

def test_nested_supersede_does_not_rollback_callers_transaction(store):
    store.sqlite.execute('BEGIN'); store.sqlite.execute("INSERT INTO sentinel VALUES('pending')")
    with pytest.raises(Exception): store.append_and_supersede(record(),semantic_key='key')
    assert store.sqlite.in_transaction
    assert store.sqlite.conn.execute('SELECT count(*) FROM sentinel').fetchone()[0]==1
    store.sqlite.rollback()

def edge(**kw):
    defaults=dict(scope=ScopeRef('tenant','agent','workspace','user'),from_id='rec_a',to_id='rec_b',edge_type='temporal',confidence=0.4,evidence_id='rec_e')
    defaults.update(kw); return MemoryEdge.create(**defaults)

@pytest.mark.parametrize('changed',['scope','from_id','to_id','edge_type','evidence_id'])
def test_forged_edge_id_cannot_rebind_or_modify_another_identity(store,changed):
    original=edge(); store.sqlite.upsert_memory_edges([original])
    attack=MemoryEdge.from_dict(original.to_dict()); attack.reason='forged'
    setattr(attack,changed,ScopeRef('other','agent','workspace','user') if changed=='scope' else ('causal' if changed=='edge_type' else 'rec_other'))
    with pytest.raises(ValueError): store.sqlite.upsert_memory_edges([attack])
    assert store.sqlite.conn.execute('SELECT reason FROM memory_edges WHERE edge_id=?',(original.edge_id,)).fetchone()[0]==original.reason

def test_edge_metadata_update_same_identity_is_allowed(store):
    original=edge(); store.sqlite.upsert_memory_edges([original])
    original.reason='legitimate update'; original.confidence=0.7
    store.sqlite.upsert_memory_edges([original])
    assert store.sqlite.conn.execute('SELECT reason,confidence FROM memory_edges').fetchone()[:]==('legitimate update',0.7)

def test_mixed_edge_batch_conflict_is_atomic(store):
    original=edge(); store.sqlite.upsert_memory_edges([original])
    unrelated=edge(to_id='rec_c'); attack=MemoryEdge.from_dict(original.to_dict()); attack.scope=ScopeRef('other','agent','workspace','user')
    with pytest.raises(ValueError): store.sqlite.upsert_memory_edges([unrelated,attack])
    assert store.sqlite.conn.execute('SELECT count(*) FROM memory_edges').fetchone()[0]==1

def test_edge_conflict_preserves_outer_transaction(store):
    original=edge(); store.sqlite.upsert_memory_edges([original])
    store.sqlite.execute('BEGIN'); store.sqlite.execute("INSERT INTO sentinel VALUES('pending')")
    attack=MemoryEdge.from_dict(original.to_dict()); attack.to_id='rec_wrong'
    with pytest.raises(ValueError): store.sqlite.upsert_memory_edges([attack],commit=False)
    assert store.sqlite.in_transaction
    assert store.sqlite.conn.execute('SELECT count(*) FROM sentinel').fetchone()[0]==1
    store.sqlite.rollback()

def test_edge_commit_false_leaves_write_uncommitted(tmp_path):
    store=RuntimeProbe(tmp_path/'edge.sqlite'); other=sqlite3.connect(tmp_path/'edge.sqlite')
    try:
        store.sqlite.upsert_memory_edges([edge()],commit=False)
        assert store.sqlite.in_transaction
        assert other.execute('SELECT count(*) FROM memory_edges').fetchone()[0]==0
        store.sqlite.commit()
        assert other.execute('SELECT count(*) FROM memory_edges').fetchone()[0]==1
    finally: other.close(); store.close()

def test_empty_edge_batch_does_not_start_transaction(store):
    assert store.sqlite.upsert_memory_edges([],commit=False)==[]
    assert not store.sqlite.in_transaction

def test_read_consistent_pins_wal_snapshot(tmp_path):
    store=RuntimeProbe(tmp_path/'snapshot.sqlite'); writer=sqlite3.connect(tmp_path/'snapshot.sqlite')
    store.sqlite.execute("INSERT INTO sentinel VALUES('old')"); store.sqlite.commit()
    def callback(sqlite):
        first=sqlite.execute('SELECT value FROM sentinel').fetchone()[0]
        writer.execute("UPDATE sentinel SET value='new'"); writer.commit()
        second=sqlite.execute('SELECT value FROM sentinel').fetchone()[0]
        return first,second
    try:
        assert store.read_consistent(callback)==('old','old')
        assert not store.sqlite.in_transaction
        assert store.sqlite.execute('SELECT value FROM sentinel').fetchone()[0]=='new'
    finally: writer.close(); store.close()

def test_read_failure_cleans_owned_transaction(store):
    def callback(sqlite):
        sqlite.execute('SELECT * FROM sentinel').fetchall()
        raise OSError('read failed')
    with pytest.raises(OSError): store.read_consistent(callback)
    assert not store.sqlite.in_transaction

def test_read_preserves_callers_transaction(store):
    store.sqlite.execute('BEGIN'); store.sqlite.execute("INSERT INTO sentinel VALUES('pending')")
    assert store.read_consistent(lambda s:s.execute('SELECT value FROM sentinel').fetchone()[0])=='pending'
    assert store.sqlite.in_transaction
    store.sqlite.rollback()

def test_read_callback_cannot_leave_accidental_write_pending(store):
    store.read_consistent(lambda s:s.execute("INSERT INTO sentinel VALUES('wrong')"))
    assert not store.sqlite.in_transaction
    assert store.sqlite.execute('SELECT count(*) FROM sentinel').fetchone()[0]==0

def test_shared_reflection_is_not_private_dedup_target(store):
    shared=record(kind='reflection',scope=ScopeRef('tenant','agent','workspace',''))
    store.sqlite.upsert(shared)
    private=record(kind='reflection')
    assert store._existing_reflection_duplicate(private) is None

def test_distinct_structured_reflections_are_not_dropped(store):
    first=record(kind='reflection',content={'payload':{'count':1}}); store.sqlite.upsert(first)
    newer=record(kind='reflection',content={'payload':{'count':2}})
    assert store._existing_reflection_duplicate(newer) is None

def test_identical_structured_reflection_key_order_dedups(store):
    first=record(kind='reflection',content={'report':{'a':1,'b':2}}); store.sqlite.upsert(first)
    again=record(kind='reflection',content={'report':{'b':2,'a':1}})
    duplicate=store._existing_reflection_duplicate(again)
    assert duplicate is not None and duplicate.record_id==first.record_id

def test_identical_exact_scope_reflection_still_dedups(store):
    first=record(kind='reflection'); store.sqlite.upsert(first)
    assert store._existing_reflection_duplicate(record(kind='reflection')).record_id==first.record_id

@pytest.mark.parametrize('bad',[float('nan'),float('inf'),float('-inf'),'NaN','Infinity',1.01,True,None,-0.1])
def test_invalid_pass_rates_never_authorize_promotion(bad):
    rule,fb,rp=evidence(); rp.meta['pass_rate']=bad
    assert run_gate(rule,fb,rp)==[]

@pytest.mark.parametrize('bad',[float('nan'),float('inf'),True,None])
def test_invalid_roi_never_authorizes_promotion(bad):
    assert run_gate(*evidence(),roi=bad)==[]

@pytest.mark.parametrize('bad',[float('nan'),float('-inf'),True])
def test_invalid_roi_threshold_never_authorizes_promotion(bad):
    assert run_gate(*evidence(),minimum=bad)==[]

@pytest.mark.parametrize('bad',[0,-1,True,'1',None])
def test_replay_without_positive_integer_sample_count_is_not_evidence(bad):
    rule,fb,rp=evidence(); rp.meta['sample_size']=bad
    assert run_gate(rule,fb,rp)==[]

def test_valid_exact_scope_replay_still_authorizes_candidate():
    rule,fb,rp=evidence(); rp.meta['pass_rate']=0.8
    assert run_gate(rule,fb,rp)==[rule]

@pytest.mark.parametrize('which',['feedback','replay'])
def test_other_scope_evidence_cannot_promote_same_named_rule(which):
    rule,fb,rp=evidence(); foreign=ScopeRef('tenant','agent','workspace','')
    (fb if which=='feedback' else rp).scope=foreign
    assert run_gate(rule,fb,rp)==[]

def test_user_loop_cannot_promote_shared_rule():
    rule,fb,rp=evidence(); shared=ScopeRef('tenant','agent','workspace','')
    rule.scope=fb.scope=rp.scope=shared
    assert run_gate(rule,fb,rp,scope=ScopeRef('tenant','agent','workspace','user'))==[]

def test_historical_baseline_probe_never_authorizes_promotion():
    rule,fb,rp=evidence(); rp.source='evolution.replay'
    assert run_gate(rule,fb,rp)==[]

def test_historical_text_lint_stays_blocked():
    rule,fb,rp=evidence(); rp.meta['replay_source']='outcome_trace_suggested_replay'
    assert run_gate(rule,fb,rp)==[]

def test_new_baseline_probe_is_diagnostic_not_behavioral(monkeypatch):
    rule=record(kind='rule',content={'retrieval_policy':{'never_used':'true'},'response_policy':{'never_used':'true'}})
    observed=[]
    class FakeMemoryAPI:
        def __init__(self,store): pass
        def recall(self,**kw): observed.append(kw); return SimpleNamespace(items=[record(title='hit')])
    monkeypatch.setattr(evolution_module,'MemoryAPI',FakeMemoryAPI)
    monkeypatch.setattr(evolution_module,'RecordEnvelope',RecordFixture)
    api=object.__new__(evolution_module.EvolutionAPI)
    api.store=SimpleNamespace(get_by_id=lambda rid:rule,append=lambda r:r)
    result=api.replay_rule(record_id=rule.record_id,dataset=[{'query':'q','expect_any_title':['hit']}])
    assert observed and result.meta.get('pass_rate')==1.0
    assert result.meta.get('verdict')=='diagnostic_only'
    assert result.meta.get('promotion_eligible') is False
    assert not gates._is_actual_replay_result(result)

@pytest.mark.parametrize('bad',[float('nan'),float('inf'),True,1.5,None])
def test_invalid_numeric_replay_does_not_inflate_pass_counter(bad):
    _,_,rp=evidence(); rp.meta['pass_rate']=bad
    assert not gates._replay_result_counts_as_pass(rp)


def exact_lookup(sqlite,record_id,scope=None):
    method=sqlite.get_by_id
    options={'exact_scope':True} if 'exact_scope' in inspect.signature(method).parameters else {}
    return method(record_id,scope=scope,**options)


def test_exact_lookup_does_not_select_newer_shared_record(store):
    own=record(record_id='rec_same'); shared=record(record_id='rec_same',scope=ScopeRef('tenant','agent','workspace',''))
    store.sqlite.upsert(own); store.sqlite.upsert(shared)
    assert exact_lookup(store.sqlite,own.record_id,own.scope).scope==own.scope


def test_ordinary_lookup_retains_shared_read_visibility(store):
    shared=record(scope=ScopeRef('tenant','agent','workspace',''))
    store.sqlite.upsert(shared)
    assert store.sqlite.get_by_id(shared.record_id,scope=ScopeRef('tenant','agent','workspace','user')).scope==shared.scope


def test_exact_lookup_never_uses_shared_fallback(store):
    shared=record(scope=ScopeRef('tenant','agent','workspace',''))
    store.sqlite.upsert(shared)
    assert exact_lookup(store.sqlite,shared.record_id,ScopeRef('tenant','agent','workspace','user')) is None


def test_exact_lookup_requires_an_explicit_scope(store):
    with pytest.raises(ValueError): exact_lookup(store.sqlite,'rec_missing')


def test_exact_lookup_rejects_corrupt_existing_payload(store):
    old=record(); store.sqlite.upsert(old)
    store.sqlite.execute("UPDATE records SET payload_json='broken' WHERE record_id=?",(old.record_id,)); store.sqlite.commit()
    with pytest.raises(RuntimeError): exact_lookup(store.sqlite,old.record_id,old.scope)


@pytest.mark.parametrize('supersede',[False,True])
def test_insert_once_cannot_return_another_scope(store,supersede):
    fields=dict(record_id='rec_same',source='adapter.memory',meta={'authoritative':True,'idempotency_key':'adapter.example'})
    shared=record(scope=ScopeRef('tenant','agent','workspace',''),**fields); own=record(**fields)
    store.sqlite.upsert(shared)
    method=store.append_and_supersede if supersede else store.append
    inserted=method(own)
    assert inserted.scope==own.scope
    assert store.sqlite.conn.execute('SELECT count(*) FROM records').fetchone()[0]==2


def test_scoped_promotion_writes_only_the_selected_namespace(store):
    own=record(record_id='rec_same_rule',kind='rule',status='accepted')
    unrelated=record(record_id=own.record_id,kind='rule',status='accepted',scope=ScopeRef('other','agent','workspace','user'))
    store.sqlite.upsert(own); store.sqlite.upsert(unrelated)
    api=object.__new__(evolution_module.EvolutionAPI); api.store=store
    options={'scope':own.scope} if 'scope' in inspect.signature(api.promote_rule).parameters else {}
    api.promote_rule(record_id=own.record_id,promoter='test',**options)
    rows=dict(store.sqlite.execute('SELECT tenant_id,status FROM records').fetchall())
    assert rows=={'tenant':'active','other':'accepted'}


def test_scoped_promotion_does_not_promote_shared_fallback(store):
    shared=record(kind='rule',status='accepted',scope=ScopeRef('tenant','agent','workspace',''))
    store.sqlite.upsert(shared)
    api=object.__new__(evolution_module.EvolutionAPI); api.store=store
    options={'scope':ScopeRef('tenant','agent','workspace','user')} if 'scope' in inspect.signature(api.promote_rule).parameters else {}
    with pytest.raises(ValueError): api.promote_rule(record_id=shared.record_id,promoter='test',**options)
    assert store.sqlite.execute('SELECT status FROM records').fetchone()[0]=='accepted'


def test_supersede_oversized_history_rolls_back_instead_of_partial_success(store):
    seed_versions(store,10001); new=record()
    with pytest.raises(RuntimeError,match='supersede_requires_offline_repair'):
        store.append_and_supersede(new,semantic_key='key')
    assert store.sqlite.execute("SELECT count(*) FROM records WHERE status='active'").fetchone()[0]==10001
    assert exact_lookup(store.sqlite,new.record_id,new.scope) is None
    assert store.sqlite.execute('SELECT count(*) FROM outbox').fetchone()[0]==0


@pytest.mark.parametrize('supersede',[False,True])
def test_cancelled_append_rolls_back_only_its_own_transaction(store,monkeypatch,supersede):
    new=record(); original=store.sqlite.upsert
    def interrupted(rec,*,commit=True):
        original(rec,commit=commit)
        raise KeyboardInterrupt('controlled test cancellation')
    monkeypatch.setattr(store.sqlite,'upsert',interrupted)
    method=store.append_and_supersede if supersede else store.append
    with pytest.raises(KeyboardInterrupt): method(new)
    assert not store.sqlite.in_transaction
    assert stored(store,new) is None
    assert store.sqlite.execute('SELECT count(*) FROM outbox').fetchone()[0]==0
