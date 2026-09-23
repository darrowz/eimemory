"""Contract tests with a real SQLite surrogate, not a full Runtime E2E suite."""
from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
from dataclasses import asdict, dataclass, field
import json
import math
import sqlite3
import threading
from time import perf_counter
from types import SimpleNamespace

import pytest

from eimemory.models.records import ScopeRef
from eimemory.contracts.recall_boundary import (
    finite_float, exact_ref, scope_tuple, authority_digest, bounded_deadline, bind_score_entries,
    normalize_retrieval_state, source_collection_incomplete, may_capture_recall_gap,
)
from eimemory.retrieval.authority_gate import enforce_selection_authority, authoritative_identity_exists
from eimemory.raw.boundary import (raw_request_boundary, raw_ref_allowed, capture_raw_snapshot,
                                  raw_remaining_seconds, invoke_raw_reranker, _CURRENT, RawRecallUnavailable)
from eimemory.storage.recall_deadline import recall_read_scope

@dataclass
class Doc:
    record_id: str='id'
    scope: ScopeRef=field(default_factory=lambda:ScopeRef(tenant_id='t',agent_id='a',workspace_id='w',user_id='u'))
    source_id: str='alpha'
    status: str='active'
    kind: str='memory'
    title: str='memo'
    detail: str='original evidence'
    aliases: list=field(default_factory=list)
    meta: dict=field(default_factory=dict)

class Store:
    """SQL reference table only; intentionally not eimemory.RuntimeStore."""
    def __init__(self):
        self._lock=threading.RLock()
        self.sqlite=SimpleNamespace(conn=sqlite3.connect(':memory:',check_same_thread=False))
        self.sqlite.conn.execute('CREATE TABLE records (rid,tenant,agent,workspace,user,source,payload,PRIMARY KEY(rid,tenant,agent,workspace,user,source))')
        self.reads=0;self.rows=[];self.last_lookup={}
    def put(self, doc):
        with self._lock:
            self.sqlite.conn.execute('INSERT OR REPLACE INTO records VALUES (?,?,?,?,?,?,?)',(*exact_ref(doc),json.dumps(asdict(doc))))
            self.sqlite.conn.commit()
    def _doc(self,payload):
        p=json.loads(payload);p['scope']=ScopeRef.from_dict(p['scope']);return Doc(**p)
    def get_by_exact_ref(self, rid, *, scope, source_id):
        with self._lock:
            row=self.sqlite.conn.execute('SELECT payload FROM records WHERE rid=? AND tenant=? AND agent=? AND workspace=? AND user=? AND source=?', (rid,*scope_tuple(scope),source_id)).fetchone()
        return None if row is None else self._doc(row[0])
    def batch(self, docs):
        if not docs:return {}
        keys=list(dict.fromkeys(exact_ref(d) for d in docs));self.reads+=1
        placeholders=','.join('(?,?,?,?,?,?)' for _ in keys)
        rows=self.sqlite.conn.execute('SELECT payload FROM records WHERE (rid,tenant,agent,workspace,user,source) IN ('+placeholders+')',[v for key in keys for v in key]).fetchall()
        return {exact_ref(d):d for d in (self._doc(row[0]) for row in rows)}
    def search_identity_candidates(self, **kwargs):
        self.last_lookup=kwargs;return deepcopy(self.rows)
    def close(self):self.sqlite.conn.close()

class Engine:
    def __init__(self, store):
        self.store=store
        self._callbacks=SimpleNamespace(_is_temporally_stale_memory=lambda r:bool(r.meta.get('stale')))
    _record_key=staticmethod(exact_ref)
    def _local_read_scope(self, deadline):
        return recall_read_scope(self.store,{'_recall_collection_deadline_monotonic':deadline})
    def _hydrate_records_batch(self, docs, *, deadline_at):
        with self._local_read_scope(deadline_at):return self.store.batch(docs)

@pytest.fixture
def store():
    s=Store();yield s;s.close()

def run_selector(store, docs, choose=None, **kwargs):
    def selector(self, items, **kw):
        if choose:return choose(self,items,kw)
        return items[:kw['limit']],{}
    return enforce_selection_authority(selector)(Engine(store),docs,limit=kwargs.pop('limit',20),**kwargs)

@pytest.mark.parametrize('value',[float('nan'),float('inf'),float('-inf'),True,False,{},[],None,'bad',10**500])
def test_invalid_score_is_finite_zero(value):
    assert finite_float(value)==0.0

@pytest.mark.parametrize('value',[0,1,-2,0.25,'0.7'])
def test_finite_scores_preserved(value):assert finite_float(value)==float(value)

@pytest.mark.parametrize('value',[float('nan'),float('inf'),float('-inf'),0,None,5000])
def test_deadline_has_three_second_ceiling(value):assert bounded_deadline(value,started=10)==13

def test_expired_deadline_is_not_reset():assert bounded_deadline(9,started=10)==9

def test_earlier_deadline_preserved():assert bounded_deadline(11,started=10)==11

@pytest.mark.parametrize('source_a,source_b',[('alpha','beta'),('beta','alpha')])
def test_scores_join_full_source_partition(source_a,source_b):
    a,b=Doc(source_id=source_a),Doc(source_id=source_b)
    rows=[{**asdict(a),'final_score':0.1},{**asdict(b),'final_score':0.9}]
    scores=bind_score_entries([a,b],rows)
    assert scores[exact_ref(a)]['final_score']==0.1
    assert scores[exact_ref(b)]['final_score']==0.9

@pytest.mark.parametrize('field',['tenant_id','agent_id','workspace_id','user_id'])
def test_scores_join_all_scope_fields(field):
    a,b=Doc(),Doc();setattr(b.scope,field,'other')
    scores=bind_score_entries([a,b],[{**asdict(a),'final_score':1},{**asdict(b),'final_score':2}])
    assert scores[exact_ref(a)]['final_score']==1 and scores[exact_ref(b)]['final_score']==2

def test_ambiguous_legacy_scores_do_not_cross_bind():
    assert bind_score_entries([Doc(),Doc(source_id='beta')],[{'record_id':'id','final_score':1}])=={}

def test_unique_legacy_score_is_compatible():
    a=Doc();assert bind_score_entries([a],[{'record_id':'id','final_score':1}])[exact_ref(a)]['final_score']==1

@pytest.mark.parametrize('row',[{'record_id':'id','source_id':'alpha','final_score':1},{'record_id':'id','scope':{},'final_score':1},{'record_id':'id','scope':None,'final_score':1}])
def test_partial_explicit_identity_never_legacy_fallback(row):assert bind_score_entries([Doc()],[row])=={}

def test_duplicate_exact_score_is_ambiguous():
    a=Doc();row={**asdict(a),'final_score':1};assert bind_score_entries([a],[row,row])=={}

def test_selector_happy_path_two_batch_reads(store):
    docs=[Doc(record_id=str(i)) for i in range(40)]
    for doc in docs:store.put(doc)
    chosen,state=run_selector(store,docs,limit=40)
    assert len(chosen)==40 and state['status']=='evidence_found' and store.reads==2

@pytest.mark.parametrize('status',['inactive','removed','candidate','superseded','rejected'])
def test_selector_rejects_nonactive_input_even_when_callback_true(store,status):
    doc=Doc(status=status);store.put(doc)
    chosen,state=run_selector(store,[doc],validate=lambda _:True)
    assert chosen==[] and state['status']=='unavailable'

def test_selector_cannot_ignore_scope_authorization(store):
    doc=Doc();store.put(doc)
    chosen,state=run_selector(store,[doc],validate=lambda _:False)
    assert not chosen and state['status']=='unavailable'

@pytest.mark.parametrize('mutation',['status','content','delete','source'])
def test_fresh_read_after_verifier_detects_change(store,mutation):
    doc=Doc();store.put(doc)
    def choose(self,items,kw):
        changed=deepcopy(doc)
        if mutation=='status':changed.status='inactive';store.put(changed)
        elif mutation=='content':changed.detail='changed after validation';store.put(changed)
        else:
            store.sqlite.conn.execute('DELETE FROM records');store.sqlite.conn.commit()
            if mutation=='source':changed.source_id='beta';store.put(changed)
        return items,{'status':'evidence_found'}
    chosen,state=run_selector(store,[doc],choose,validate=lambda _:True)
    assert chosen==[] and state['status']=='unavailable'

def test_mutable_alias_cannot_change_before_digest(store):
    doc=Doc();store.put(doc)
    def choose(self,items,kw):
        items[0].detail='changed in place';store.put(items[0]);return items,{}
    chosen,state=run_selector(store,[doc],choose,validate=lambda _:True)
    assert not chosen

def test_new_injected_reference_is_rejected(store):
    doc,other=Doc(),Doc(record_id='injected');store.put(doc);store.put(other)
    chosen,state=run_selector(store,[doc],lambda *_:([other],{}),validate=lambda _:True)
    assert not chosen and state['status']=='unavailable'

def test_pool_sibling_cannot_lend_its_identity(store):
    doc=Doc();store.put(doc);sibling=Doc(record_id='sibling')
    fusion={'pool_members':{exact_ref(doc):[doc,sibling]}}
    def choose(self,items,kw):
        assert kw['fusion_state']['pool_members']=={exact_ref(doc):[doc]}
        return items,{}
    chosen,_=run_selector(store,[doc],choose,fusion_state=fusion)
    assert chosen==[doc] and fusion['pool_members'][exact_ref(doc)]==[doc,sibling]

def test_no_lock_is_held_during_external_verification(store):
    doc=Doc();store.put(doc);finished=threading.Event()
    def choose(self,items,kw):
        def writer():
            changed=deepcopy(doc);changed.status='inactive';store.put(changed);finished.set()
        thread=threading.Thread(target=writer);thread.start();thread.join(2)
        assert finished.is_set()
        return items,{}
    chosen,_=run_selector(store,[doc],choose)
    assert not chosen

def test_final_read_exception_is_unavailable(store,monkeypatch):
    doc=Doc();store.put(doc)
    def choose(self,items,kw):
        monkeypatch.setattr(store,'batch',lambda *_:(_ for _ in ()).throw(sqlite3.OperationalError('test')))
        return items,{}
    chosen,state=run_selector(store,[doc],choose,validate=lambda _:True)
    assert not chosen and state['status']=='unavailable'

def test_expired_selection_does_not_call_verifier(store):
    called=[];chosen,state=run_selector(store,[Doc()],lambda *_:called.append(1),deadline_at=perf_counter()-1)
    assert not chosen and not called and state['status']=='unavailable'

def test_no_evidence_is_distinct_from_failure(store):
    chosen,state=run_selector(store,[])
    assert not chosen and state['status']=='no_evidence' and state['collection_complete'] is True

def test_partial_valid_results_are_degraded(store):
    a,b=Doc(),Doc(record_id='bad');store.put(a);store.put(b)
    chosen,state=run_selector(store,[a,b],validate=lambda doc:doc.record_id!='bad')
    assert chosen==[a] and state['status']=='degraded'

def request(scope=None,sources=('alpha',),kinds=()):
    scope=scope or Doc().scope
    return SimpleNamespace(scope=SimpleNamespace(to_scope_ref=lambda:scope),source_ids=sources,kinds=kinds)

def identity_row(doc):return {**asdict(doc),'evidence':['exact_title']}

@pytest.mark.parametrize('status',['active','candidate','draft','rejected','inactive'])
def test_identity_requires_active_exact_record(store,status):
    doc=Doc(status=status);store.put(doc);store.rows=[identity_row(doc)]
    assert authoritative_identity_exists(Engine(store),query='memo',request=request(),target_source_id='alpha') is (status=='active')
    assert store.last_lookup['recall_filters']['_exact_scope'] is True

def test_identity_never_calls_bare_get_by_id(store):
    doc=Doc(source_id='beta');store.put(doc);store.rows=[identity_row(Doc())]
    store.get_by_id=lambda *_args,**_kw:pytest.fail('bare ID lookup must not be used')
    assert authoritative_identity_exists(Engine(store),query='memo',request=request(),target_source_id='alpha') is False

@pytest.mark.parametrize('field',['tenant_id','agent_id','workspace_id','user_id'])
def test_identity_rejects_other_scope(store,field):
    doc=Doc();setattr(doc.scope,field,'other');store.put(doc);store.rows=[identity_row(doc)]
    assert authoritative_identity_exists(Engine(store),query='memo',request=request(),target_source_id='alpha') is False

def test_identity_source_outside_allowlist_is_unavailable(store):
    assert authoritative_identity_exists(Engine(store),query='memo',request=request(sources=()),target_source_id='alpha') is None

def test_identity_ambiguous_stays_nonunique(store):
    a,b=Doc(),Doc(record_id='other');store.put(a);store.put(b);store.rows=[identity_row(a),identity_row(b)]
    assert authoritative_identity_exists(Engine(store),query='memo',request=request(),target_source_id='alpha') is False

@pytest.mark.parametrize('meta',[{'stale':True},{'quality':{'capture_decision':'reject'}}])
def test_identity_excludes_stale_or_rejected_capture(store,meta):
    doc=Doc(meta=meta);store.put(doc);store.rows=[identity_row(doc)]
    assert authoritative_identity_exists(Engine(store),query='memo',request=request(),target_source_id='alpha') is False

@pytest.mark.parametrize('status',['unavailable','ambiguous','degraded','evidence_found'])
def test_failed_or_ambiguous_empty_cannot_capture_gap(status):
    state={'status':status,'collection_complete':True}
    assert not may_capture_recall_gap(state,selected_count=0,now=1,deadline_at=2)

def test_only_complete_absence_can_capture_gap():
    state=normalize_retrieval_state({},selected_count=0)
    assert may_capture_recall_gap(state,selected_count=0,now=1,deadline_at=2)
    assert not may_capture_recall_gap(state,selected_count=0,now=2,deadline_at=2)

@pytest.mark.parametrize('report',[{'retrieval_mode':'deadline_exhausted'},{'drops':{'recall_budget_exhausted':1}}, {'status':'unavailable'},{'error':'read_failure'}])
def test_provider_incompleteness_is_not_absence(report):
    assert source_collection_incomplete([report])
    state=normalize_retrieval_state({},selected_count=0,incomplete=True)
    assert state['status']=='unavailable' and not state['collection_complete']

@pytest.mark.parametrize('exact',[True,False])
def test_raw_exact_scope_filters_before_text_consumer(store,exact):
    own,shared=Doc(),Doc(scope=ScopeRef(tenant_id='t',agent_id='a',workspace_id='w',user_id=''))
    store.put(own);store.put(shared);sent=[]
    @raw_request_boundary
    def search(_store,**_kw):
        docs=[d for d in (own,shared) if raw_ref_allowed(d)]
        for d in docs:capture_raw_snapshot(d)
        sent.extend(exact_ref(d) for d in docs)
        return [{'record':asdict(d)} for d in docs]
    out=search(store,scope=own.scope,task_context={'exact_scope_only':exact},source_ids=('alpha',))
    assert len(sent)==len(out)==(1 if exact else 2)
    assert _CURRENT.get() is None

def test_raw_revocation_during_rerank_is_dropped(store):
    doc=Doc();store.put(doc)
    @raw_request_boundary
    def search(_store,**_kw):
        capture_raw_snapshot(doc)
        changed=deepcopy(doc);changed.status='inactive';store.put(changed)
        return [{'record':asdict(doc)}]
    with pytest.raises(RawRecallUnavailable):
        search(store,scope=doc.scope,task_context={},source_ids=('alpha',))

def test_raw_forged_text_is_rebuilt_from_authority(store):
    doc=Doc();store.put(doc)
    @raw_request_boundary
    def search(_store,**_kw):
        capture_raw_snapshot(doc)
        return [{'record':{**asdict(doc),'detail':'FORGED'}}]
    out=search(store,scope=doc.scope,task_context={},source_ids=('alpha',))
    assert out[0]['record'].get('detail','original evidence')!='FORGED'

def test_raw_context_resets_after_exception(store):
    @raw_request_boundary
    def search(*_args,**_kw):raise RuntimeError('fault')
    with pytest.raises(RuntimeError):search(store,scope=Doc().scope,task_context={},source_ids=('alpha',))
    assert _CURRENT.get() is None

def test_raw_nested_request_cannot_broaden_scope(store):
    doc=Doc()
    @raw_request_boundary
    def inner(*_args,**_kw):
        assert not raw_ref_allowed(Doc(source_id='beta'));return []
    @raw_request_boundary
    def outer(*_args,**_kw):
        return inner(store,scope=doc.scope,task_context={},source_ids=None)
    assert outer(store,scope=doc.scope,task_context={'exact_scope_only':True},source_ids=('alpha',))==[]

@pytest.mark.parametrize('seconds',[1,8,20,float('inf')])
def test_raw_timeout_cannot_exceed_three_seconds(seconds):assert 0 < raw_remaining_seconds(seconds,{}) <= 3

def test_raw_expired_deadline_prevents_external_call():
    assert raw_remaining_seconds(8,{'_recall_deadline_monotonic':perf_counter()-1})==0

@pytest.mark.parametrize('legacy',[True,False])
def test_callback_signature_selected_before_invocation(legacy):
    calls=[]
    if legacy:
        def fn(items,*,query,candidate_count):calls.append(query);return items
    else:
        def fn(items,**kwargs):calls.append(kwargs['query']);return items
    assert invoke_raw_reranker(fn,[1],query='q',task_context={},limit=1,candidate_count=1)==[1]
    assert calls==['q']

def test_internal_typeerror_is_never_retried():
    calls=[]
    def fn(items,**kw):calls.append(1);raise TypeError('internal bug')
    with pytest.raises(TypeError):invoke_raw_reranker(fn,[],query='q',task_context={},limit=1,candidate_count=0)
    assert calls==[1]


def test_auxiliary_rule_revocation_is_checked_separately(store):
    from eimemory.retrieval.authority_gate import revalidate_auxiliary_outputs
    doc=Doc(kind='rule');store.put(doc)
    snapshots={exact_ref(doc):authority_digest(doc)}
    changed=deepcopy(doc);changed.status='inactive';store.put(changed)
    result,count=revalidate_auxiliary_outputs(Engine(store),[doc],snapshots=snapshots,
        authorized=lambda _:True,deadline_at=perf_counter()+3)
    assert result==[] and count==1


def test_cached_precheck_does_not_send_revoked_text_to_verifier(store):
    doc=Doc();store.put(Doc(status='inactive'));sent=[]
    chosen,state=run_selector(store,[doc],lambda *_:sent.append(1),validate=lambda _:True)
    assert not chosen and not sent and state['status']=='unavailable'


def test_preserve_existing_incomplete_status():
    state=normalize_retrieval_state({'status':'no_evidence','collection_complete':False},selected_count=0)
    assert state['status']=='unavailable' and state['collection_complete'] is False


def test_raw_explicit_quality_rejection_is_never_sent():
    doc=Doc(meta={'quality':{'capture_decision':'reject'}})
    assert not raw_ref_allowed(doc)


@pytest.mark.parametrize('a,b,expected',[
    ((),None,None), (('memory',),None,('memory',)), ((),['rule'],('rule',)),
    (('memory','rule'),['rule'],('rule',)), (('memory',),['rule'],()),
    (('memory','memory'),['memory'],('memory',)),
])
def test_explicit_kind_filters_can_only_narrow(a,b,expected):
    from eimemory.contracts.recall_boundary import intersect_requested_kinds
    assert intersect_requested_kinds(a,b)==expected


def test_task_recall_cannot_send_unadmitted_raw_context(store):
    called=[]
    @raw_request_boundary
    def search(*_a,**_kw):called.append(1);return []
    assert search(store,scope=Doc().scope,task_context={'_task_recall_mode':'tasks'},source_ids=('alpha',))==[]
    assert not called


def test_explicit_raw_kind_filter_precedes_text_consumer(store):
    doc=Doc(kind='raw_chunk');seen=[]
    @raw_request_boundary
    def search(*_a,**_kw):
        if raw_ref_allowed(doc):seen.append(doc.detail)
        return []
    assert search(store,scope=doc.scope,task_context={'kinds':['memory']},source_ids=('alpha',))==[]
    assert not seen


def test_selector_exception_is_unavailable_not_absence(store):
    doc=Doc();store.put(doc)
    def choose(*_args):raise RuntimeError('test failure')
    out,state=run_selector(store,[doc],choose)
    assert out==[] and state['status']=='unavailable' and not state['collection_complete']


def test_cancellation_is_not_swallowed(store):
    doc=Doc();store.put(doc)
    def choose(*_args):raise KeyboardInterrupt()
    with pytest.raises(KeyboardInterrupt):run_selector(store,[doc],choose)


def test_selector_generator_is_consumed_with_limit(store):
    import itertools
    doc=Doc();store.put(doc)
    out,state=run_selector(store,[doc],lambda *_:(itertools.repeat(doc),{}),limit=2)
    assert out==[doc] and state['selected_count']==1


def test_selected_count_and_absence_status_cannot_contradict():
    assert normalize_retrieval_state({'status':'no_evidence'},selected_count=1)['status']=='evidence_found'


def test_raw_failure_signal_survives_list_api(store,monkeypatch):
    from collections import Counter
    import eimemory.raw.retrieval as module
    from eimemory.raw.boundary import guarded_raw_search
    def failure(*_args,**_kw):raise RuntimeError('private transport text')
    monkeypatch.setattr(module,'search_raw_chunks',failure,raising=False)
    diagnostics=Counter()
    assert guarded_raw_search(store,diagnostics=diagnostics,query='q')==[]
    assert diagnostics=={'raw_collection_unavailable':1}
