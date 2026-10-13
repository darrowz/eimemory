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
from eimemory.api.memory import MemoryAPI
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
    monkeypatch.setenv('EIMEMORY_CALLER_ASSISTED_RECALL_REQUIRED','0')
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
    engine=MemoryAPI(store).recall_engine
    yield NS(store=store,record=record,request=request,engine=engine,cid=cid,fragment=fragment,calls=calls)
    store.close()


def vars_scope(scope):
    return {k:getattr(scope,k) for k in cat.SCOPE_FIELDS}


def select(system,*,validate=lambda r:True,query=QUERY):
    return system.engine._select_post_fusion_items([system.record],query=query,limit=1,
        fusion_state={},component_hints_by_ref={},validate=validate)


def test_real_default_engine_wrapper_keeps_local_admission_unverified(system):
    system.engine._recall=lambda request:select(system)
    items,report=system.engine.recall(system.request)
    assert items==[system.record] and not system.calls
    assert report['status']=='evidence_found'
    assert 'caller_assistance' not in report and 'independent_scored' not in report
    bundle=RecallBundle(items=items,rules=[],reflections=[],confidence=.9,next_action_hint='',
                        explanation={'relevance_selector':report})
    compact=bundle.to_compact_dict(limit=1)
    assert compact['items'][0]['record_id']==system.record.record_id
    assert 'proofs' not in compact


def test_real_lightweight_fragment_route(system):
    gate=LightweightAdmission(LightweightConfig(enabled=True))
    with evidence_scope(system.store,system.request):
        items,report=gate.select([system.record],query=QUERY,limit=1,validate=lambda r:True,
            backend_available=True,hints_for=lambda r:{'dense_vector_score':.9,
                'fragment_policy':POLICY,'evidence_fragment_id':system.fragment['id']})
    assert items==[system.record] and not system.calls
    assert report['quality_evaluation']=='post_delivery'
    assert report['scored'][0]['fragment_id']==system.fragment['id']
    assert 'caller_assistance' not in report and 'independent_scored' not in report


def revoke_at_final_authority_read(system, monkeypatch):
    """Mutate canonical authority, not the optional semantic-review catalog."""
    reads=[]
    original=system.engine._hydrate_records_batch
    def hydrate(records, *, deadline_at):
        reads.append(1)
        if len(reads)==2:
            current=system.store.get_by_exact_ref(system.record.record_id,
                scope=system.record.scope,source_id=system.record.source_id)
            current.status='inactive'
            system.store.rewrite(current)
        return original(records,deadline_at=deadline_at)
    monkeypatch.setattr(system.engine,'_hydrate_records_batch',hydrate)
    return reads


def test_default_final_canonical_revoke_is_unavailable(system,monkeypatch):
    reads=revoke_at_final_authority_read(system,monkeypatch)
    with evidence_scope(system.store,system.request):
        items,report=select(system)
    assert not items and report['status']=='unavailable' and not system.calls
    assert len(reads)==2 and report['dropped_reasons']['authority_changed']==1


def test_lightweight_final_canonical_revoke_is_unavailable(system,monkeypatch):
    system.engine.relevance_admission=LightweightAdmission(LightweightConfig(enabled=True))
    reads=revoke_at_final_authority_read(system,monkeypatch)
    # Exact identity uses the real SQLite authority without a synthetic vector
    # backend. The engine's shared final authority fence still owns delivery.
    items,report=select(system,query=system.record.title)
    assert not items and report['status']=='unavailable' and not system.calls
    assert len(reads)==2 and report['dropped_reasons']['authority_changed']==1


def test_stale_catalog_only_affects_explicit_independent_verification(system):
    system.store.append(RecordEnvelope.create(kind='memory',title='另一条规范',scope=system.record.scope,
                                             summary='新材料需要进一步审核。',source_id='fixture'))
    with evidence_scope(system.store,system.request):
        items,report=select(system)
        assert items==[system.record] and not system.calls
        assert 'caller_assistance' not in report
        verified,review=assist.verify_candidates(query=QUERY,
            candidates=[(system.record,candidate_record_keyword_text(system.record,max_text_chars=16000))],limit=1)
    assert not verified and len(system.calls)==1 and review['status']=='no_evidence'
    assert review['local_evidence']['reason']=='contract_stale'


def test_explicit_shadow_verification_keeps_model_result(system,monkeypatch):
    monkeypatch.setenv('EIMEMORY_INDEPENDENT_EVIDENCE_MODE','shadow')
    with evidence_scope(system.store,system.request):
        items,report=select(system)
        assert items==[system.record] and not system.calls
        assert 'caller_assistance' not in report
        verified,review=assist.verify_candidates(query=QUERY,
            candidates=[(system.record,candidate_record_keyword_text(system.record,max_text_chars=16000))],limit=1)
    assert not verified and len(system.calls)==1
    assert review['local_evidence']['selection_agreement']=='different_records'


@pytest.mark.parametrize('verdict', ['supported','no_support','fabricated'])
def test_required_verifier_still_controls_governed_delivery(system,monkeypatch,verdict):
    monkeypatch.setenv('EIMEMORY_CALLER_ASSISTED_RECALL_REQUIRED','1')
    def complete(**kwargs):
        system.calls.append(kwargs)
        selected=[] if verdict=='no_support' else [{'id':'0','quote':
            system.record.summary if verdict=='supported' else '没有出现在原始记录中的伪造事实。'}]
        return NS(text=json.dumps({'selected':selected},ensure_ascii=False),model_id='gpt-5.6-luna')
    monkeypatch.setattr(assist,'configured_client',lambda:NS(timeout_seconds=9,complete=complete))
    with evidence_scope(system.store,system.request):
        items,report=select(system)
    assert len(system.calls)==1
    if verdict=='supported':
        assert items==[system.record] and report['status']=='evidence_found'
        assert report['caller_assistance']['proofs'][0]['record_id']==system.record.record_id
    else:
        assert not items
        assert report['status']==('no_evidence' if verdict=='no_support' else 'unavailable')
