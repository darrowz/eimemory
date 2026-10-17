"""Additional fixed controls kept separate from the RED reproduction set."""
from dataclasses import asdict
from types import SimpleNamespace

import pytest

from eimemory.api.runtime import Runtime
from eimemory.contracts.release_identity import ReleaseIdentity
from eimemory.governance.learning.supervisor import (
    build_supervisor_contract, persist_nightly_command_outcome, persist_supervisor_summary, supervisor_summary,
)
from eimemory.identity_ops import repair_hongtu_identity
from eimemory.models.records import LinkRef
from eimemory.recall import classify_recall_intent
from eimemory.recall.preference import preference_recall_request, supports_preference_request
from eimemory.retrieval.preference_diagnostics import safe_preference_route
from test_unified_identity_conflict_contract import CURRENT, LEGACY, entity, rows, store
from test_unified_preference_contract import CONTEXT, SCOPE, memory, no_models

@pytest.mark.parametrize('query,text',[
    ('What is my response format','I prefer replies in bullet points.'),
    ('我的回复语气是什么','用户希望回复礼貌。'),
])
def test_distinct_real_preference_properties_keep_positive_delivery(tmp_path,query,text):
    with Runtime.create(root=tmp_path) as runtime:
        target=memory(runtime,'Linden response preference',text,memory_type='preference')
        assert supports_preference_request(preference_recall_request(query),target)
        bundle=runtime.memory.recall(query=query,scope=asdict(SCOPE),task_context=CONTEXT,limit=5)
        assert [item.record_id for item in bundle.items]==[target.record_id]

def test_summary_is_authoritative_body_while_title_cannot_manufacture_attribute(tmp_path):
    with Runtime.create(root=tmp_path) as runtime:
        target=memory(runtime,'Response format: bullet points','No response format has been recorded.',memory_type='preference')
        request=preference_recall_request('What is my response format')
        assert not supports_preference_request(request,target)
        target.summary='I prefer replies in bullet points.'
        target.content={'memory_type':'preference'};target.detail=''
        stored=runtime.store.append(target)
        before=stored.to_dict()
        assert supports_preference_request(request,stored)
        bundle=runtime.memory.recall(query='What is my response format',scope=asdict(SCOPE),task_context=CONTEXT,limit=5)
        assert [item.record_id for item in bundle.items]==[stored.record_id]
        assert runtime.store.get_by_exact_ref(stored.record_id,scope=SCOPE,source_id='hermes').summary==before['summary']

def test_topic_keyword_in_an_independent_fact_does_not_activate_preference_view(tmp_path):
    with Runtime.create(root=tmp_path) as runtime:
        query='Linden user preference table stores timestamps'
        target=memory(runtime,query)
        assert not runtime.memory._is_preference_query(query,CONTEXT,recall_intent=classify_recall_intent(query,CONTEXT))
        bundle=runtime.memory.recall(query=query,scope=asdict(SCOPE),task_context=CONTEXT,limit=5)
        assert [item.record_id for item in bundle.items]==[target.record_id]

def test_explicit_context_has_a_durable_fixed_route_without_weakening_candidate_safety(tmp_path):
    with Runtime.create(root=tmp_path) as runtime:
        target=memory(runtime,'Linden cache layout')
        bundle=runtime.memory.recall(query=target.title,scope=asdict(SCOPE),
                                    task_context={**CONTEXT,'intent':'operator.preference'},limit=5)
        assert bundle.items==[]
        route=safe_preference_route(bundle.explanation['preference_route'])
        assert route['entry']=='explicit_context' and route['filter_applied'] is True
        assert safe_preference_route(route)==route

@pytest.mark.parametrize('difference',['detail','meta','evidence','links'])
def test_equal_summary_never_proves_target_payload_equivalence(store,difference):
    source=store.append(entity(LEGACY));target=entity(CURRENT)
    if difference=='detail':target.detail='Different authoritative detail'
    elif difference=='meta':target.meta['authority']='independent'
    elif difference=='evidence':target.evidence=['psrc_independent']
    else:target.links=[LinkRef(relation='mentioned_in',target_kind='paper_source',target_id='psrc_independent')]
    assert source.summary==target.summary
    store.append(target);before=rows(store)
    report=repair_hongtu_identity(SimpleNamespace(store=store),scope=CURRENT,apply=True)
    assert report['ok'] is False and report['outcome_counts']=={'target_conflict':1}
    assert rows(store)==before

@pytest.mark.parametrize('conflict',[False,True])
def test_real_repair_failure_keeps_nonzero_durable_command_and_healthy_scheduler(store,monkeypatch,conflict):
    monkeypatch.setattr('eimemory.governance.release.evidence_contract.current_release_identity',
                        lambda *a,**kw:ReleaseIdentity('b'*40,'1.0.0','receipt-synthetic','session-synthetic'))
    runtime=SimpleNamespace(store=store)
    store.append(entity(LEGACY))
    if conflict:store.append(entity(CURRENT))
    else:
        with store._lock:
            store.sqlite.execute('UPDATE records SET payload_digest=?',('0'*64,));store.sqlite.commit()
    repair=repair_hongtu_identity(runtime,scope=CURRENT,apply=True)
    assert repair['ok'] is False
    summary=supervisor_summary(command='nightly',ok=True,duration_ms=1,memory_peak=0,
                               run_id='run-synthetic',started_at='2026-01-01T00:00:00Z')
    summary['nightly_diagnostics']={'execution_ok':True,'failed_steps':[]}
    saved=persist_supervisor_summary(runtime,scope=CURRENT,summary=summary)
    bound={**saved.content,'record_id':saved.record_id}
    ending=persist_nightly_command_outcome(runtime,scope=CURRENT,supervisor=bound,identity_repair=repair)
    assert ending['exit_code']==1 and ending['first_failed_phase']=='identity_repair'
    assert ending['scheduler_execution_ok'] is True and ending['identity_repair_ok'] is False
    contract=build_supervisor_contract(runtime,scope=CURRENT)
    assert contract['scheduler_status']=='healthy' and contract['status']=='degraded'
