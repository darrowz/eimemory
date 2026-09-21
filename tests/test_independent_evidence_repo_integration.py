"""Run in the COMPLETE pinned eimemory checkout after applying the full patch.

Unlike isolated source-subset checks, these tests import the real RuntimeStore,
RecordEnvelope, governed selector, fragment projector, compact serializer and
lightweight admission. No provider/network calls or production corpus are used.
"""
import json
from time import time
from types import SimpleNamespace as NS

import pytest

from eimemory.models.records import RecordEnvelope, ScopeRef, RecallBundle
from eimemory.storage.runtime_store import RuntimeStore
from eimemory.storage import independent_evidence as cat
from eimemory.retrieval import caller_assistance as assist
from eimemory.retrieval.independent_evidence import evidence_scope
from eimemory.retrieval.engine import GovernedRecallEngine
from eimemory.retrieval.evidence_fragments import evidence_fragments, POLICY
from eimemory.retrieval.postgres_vector import candidate_record_keyword_text
from eimemory.retrieval.lightweight_admission import LightweightAdmission, LightweightConfig

QUERY='材料链接应该怎么处理？'


@pytest.fixture
def system(tmp_path,monkeypatch):
    monkeypatch.setenv('EIMEMORY_INDEPENDENT_EVIDENCE_MODE','enforce')
    monkeypatch.setenv('EIMEMORY_CALLER_ASSISTED_RECALL_ENABLED','1')
    monkeypatch.setenv('EIMEMORY_RECALL_GATEWAY_POOL','0')
    monkeypatch.setenv('EIMEMORY_RECALL_GATEWAY_PREWARM','0')
    monkeypatch.setenv('EIMEMORY_RECALL_ATTRIBUTE_PRECHECK','0')
    monkeypatch.setenv('EIMEMORY_RECALL_EXPECTED_MODEL','gpt-5.6-luna')
    store=RuntimeStore(tmp_path)
    scope=ScopeRef(tenant_id='fixture',agent_id='agent',workspace_id='workspace',user_id='operator')
    record=RecordEnvelope.create(kind='memory',title='材料链接处理规范',scope=scope,
        summary='材料链接处理时阅读完整原文并保留出处。',content={},source_id='fixture',
        meta={'memory_type':'instruction'})
    store.append(record)
    text=candidate_record_keyword_text(record,max_text_chars=16000)
    fragment=next(f for f in evidence_fragments(text) if '材料链接处理时' in f['text'])
    request=NS(query=QUERY,scope=scope,source_ids=('fixture',),task_context_dict=lambda:{})
    with cat.connect(store,writable=True) as conn:
        cat.install(conn)
        packet=cat.prepare(conn,{'scope':vars_scope(scope),'source_id':'fixture','record_id':record.record_id,
            'intent':'procedure','subject':'材料链接','attribute':'','aliases':[],
            'fragment_id':fragment['id'],'expires_at':int(time())+3600})
        cid=cat.approve(conn,packet,expected_digest=cat.digest(packet),reviewer='fixture-reviewer',
            review_receipt='a'*64,attestations={k:True for k in cat.ATTESTATIONS})
    calls=[]
    def complete(**kwargs):
        calls.append(kwargs)
        return NS(text='{"selected":[]}',model_id='gpt-5.6-luna')
    monkeypatch.setattr(assist,'configured_client',lambda:NS(timeout_seconds=9,complete=complete))
    engine=GovernedRecallEngine.__new__(GovernedRecallEngine)
    engine.store=store
    engine.relevance_admission=None
    yield NS(store=store,record=record,request=request,engine=engine,cid=cid,fragment=fragment,calls=calls)
    store.sqlite.conn.close()


def vars_scope(scope):
    return {k:getattr(scope,k) for k in cat.SCOPE_FIELDS}


def select(system,*,validate=lambda r:True):
    return system.engine._select_post_fusion_items([system.record],query=QUERY,limit=1,
        fusion_state={},component_hints_by_ref={},validate=validate)


def test_real_default_engine_wrapper_and_compact_fragment(system):
    system.engine._recall=lambda request:select(system)
    items,report=system.engine.recall(system.request)
    assert items==[system.record] and not system.calls
    assert report['scored'][0]['fragment_id']==system.fragment['id']
    bundle=RecallBundle(items=items,rules=[],reflections=[],confidence=.9,next_action_hint='',
                        explanation={'relevance_selector':report})
    compact=bundle.to_compact_dict(limit=1)
    assert compact['items'][0]['evidence_excerpt']==system.fragment['text']
    assert compact['items'][0]['record_id']==system.record.record_id


def test_real_lightweight_fragment_route(system):
    gate=LightweightAdmission(LightweightConfig(enabled=True))
    with evidence_scope(system.store,system.request):
        items,report=gate.select([system.record],query=QUERY,limit=1,validate=lambda r:True,
            backend_available=True,hints_for=lambda r:{'dense_vector_score':.9,
                'fragment_policy':POLICY,'evidence_fragment_id':system.fragment['id']})
    assert items==[system.record] and not system.calls
    assert report['caller_assistance']['reason']=='reviewed_original_evidence'


def test_default_final_revoke_during_authority_check_is_unavailable(system):
    checks=[0]
    def validate(record):
        checks[0]+=1
        if checks[0]==2:
            with cat.connect(system.store,writable=True) as conn:
                cat.revoke(conn,system.cid,reviewer='fixture-reviewer')
        return True
    with evidence_scope(system.store,system.request):
        items,report=select(system,validate=validate)
    assert not items and report['status']=='unavailable' and not system.calls


def test_lightweight_final_revoke_during_authority_check_is_unavailable(system):
    checks=[0]
    def validate(record):
        checks[0]+=1
        if checks[0]==2:
            with cat.connect(system.store,writable=True) as conn:
                cat.revoke(conn,system.cid,reviewer='fixture-reviewer')
        return True
    with evidence_scope(system.store,system.request):
        items,report=LightweightAdmission(LightweightConfig(enabled=True)).select([system.record],
            query=QUERY,limit=1,validate=validate,backend_available=True,
            hints_for=lambda r:{'dense_vector_score':.9,'fragment_policy':POLICY,
                                'evidence_fragment_id':system.fragment['id']})
    assert not items and report['status']=='unavailable' and not system.calls


def test_default_stale_partition_falls_back_not_false_empty(system):
    system.store.append(RecordEnvelope.create(kind='memory',title='另一条规范',scope=system.record.scope,
                                             summary='新材料需要进一步审核。',source_id='fixture'))
    with evidence_scope(system.store,system.request):
        items,report=select(system)
    assert not items and len(system.calls)==1 and report['status']=='no_evidence'
    assert report['caller_assistance']['local_evidence']['reason']=='contract_stale'


def test_shadow_default_remains_original_model_result(system,monkeypatch):
    monkeypatch.setenv('EIMEMORY_INDEPENDENT_EVIDENCE_MODE','shadow')
    with evidence_scope(system.store,system.request):
        items,report=select(system)
    assert not items and len(system.calls)==1
    assert report['caller_assistance']['local_evidence']['selection_agreement']=='different_records'
