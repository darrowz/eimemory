"""Fixed, synthetic requests exercise routing, evidence and durable evaluation together."""
from copy import deepcopy
from dataclasses import asdict
import json

import pytest

from eimemory.api.memory import MemoryAPI
from eimemory.api.runtime import Runtime
from eimemory.evaluation.controlled_memory_benchmark import (
    _case, build_controlled_memory_benchmark, run_controlled_memory_benchmark,
)
from eimemory.governance.learning.supervisor import persist_supervisor_summary, supervisor_summary
from eimemory.models.records import RecordEnvelope, ScopeRef
from eimemory.recall import classify_recall_intent
from eimemory.recall.preference import preference_recall_request, supports_preference_request
from eimemory.scheduler.jobs import _memory_eval_report_record
from eimemory.scheduler.result_contract import nightly_result_diagnostics

SCOPE=ScopeRef('synthetic','unified-contract','isolated::channel::hermes','owner')
CONTEXT={'source_ids':['hermes'],'target_source_id':'hermes','exact_scope_only':True}

@pytest.fixture(autouse=True)
def no_models(monkeypatch):
    monkeypatch.setattr('eimemory.llm.command_client.CommandLLMClient.complete',
                        lambda *a,**kw: pytest.fail('Model prohibited'))

def memory(runtime,title,text=None,*,memory_type='durable_fact',scope=SCOPE):
    return runtime.store.append(RecordEnvelope.create(kind='memory',title=title,summary=text or title,
        content={'text':text or title,'memory_type':memory_type},
        meta={'memory_type':memory_type,'memory_layer':'l1','force_capture':True},
        scope=scope,source='synthetic.memory',source_id='hermes'))

@pytest.mark.parametrize('query',[
    'Orchid preference cache layout', 'Orchid偏好索引布局',
    '用户偏好分析表使用绿色索引', '用户沟通风格偏好诊断失败记录',
    'Recall preference filtering failed for the Orchid archive',
    'The communication style parser stores tokens',
])
def test_descriptive_keywords_are_not_preference_requests(query):
    api=MemoryAPI.__new__(MemoryAPI)
    intent=classify_recall_intent(query,CONTEXT)
    assert preference_recall_request(query,CONTEXT) is None
    assert not api._is_preference_query(query,CONTEXT,recall_intent=intent)

def test_goal_marker_is_not_a_request_but_explicit_context_and_markers_are():
    api=MemoryAPI.__new__(MemoryAPI);query='Orchid cache layout'
    context={'goal':'compare preference cache implementations'}
    assert not api._is_preference_query(query,context,recall_intent=classify_recall_intent(query,context))
    explicit={'intent':'operator_preference'}
    assert api._is_preference_query(query,explicit,recall_intent=classify_recall_intent(query,explicit))
    assert api._is_preference_query(query,{'preference_query_markers':['Orchid']},
                                  recall_intent=classify_recall_intent(query))

@pytest.mark.parametrize('query',['我喜欢什么','我的偏好','What are my preferences',
    '鸿哥 沟通风格','operator preference','communication style','reply style'])
def test_real_requests_keep_preference_filter(query):
    api=MemoryAPI.__new__(MemoryAPI)
    assert api._is_preference_query(query,CONTEXT,recall_intent=classify_recall_intent(query,CONTEXT))

@pytest.mark.parametrize('memory_type',['durable_fact','preference'])
def test_generated_query_canonical_context_and_known_item_survive_receipts(tmp_path,memory_type):
    with Runtime.create(root=tmp_path) as runtime:
        target=memory(runtime,'Orchid preference cache layout',memory_type=memory_type)
        dataset=build_controlled_memory_benchmark(runtime,scope=SCOPE,source_ids=['hermes'])
        before=deepcopy(dataset);case=dataset['cases'][0]
        assert case==_case(target) and case['query']==target.title
        assert case['task_context']=={**CONTEXT,'runtime_channel':'hermes'}
        report=run_controlled_memory_benchmark(runtime,dataset,scope=SCOPE,source_ids=['hermes'])
        assert dataset==before
        assert report['sample_count']==report['pass_count']==1 and report['fail_count']==0
        sample=report['samples'][0]
        assert sample['expected_rank']==1
        assert sample['returned_refs'][0]['record_id']==target.record_id
        assert report['threshold']==1.0 and report['memory_benchmark_accepted'] is False
        report['dataset_source']='controlled_records'
        receipt=_memory_eval_report_record(report,scope=SCOPE);runtime.store.append(receipt)
        diagnostic=nightly_result_diagnostics({'memory_eval_ci':report},[])
        summary=supervisor_summary(command='nightly',ok=True,duration_ms=1,memory_peak=0)
        summary['nightly_diagnostics']=diagnostic
        saved=persist_supervisor_summary(runtime,scope=SCOPE,summary=summary)
    with Runtime.create(root=tmp_path) as runtime:
        assert runtime.store.get_by_exact_ref(receipt.record_id,scope=SCOPE,source_id=receipt.source_id).content['report']==report
        assert runtime.store.get_by_exact_ref(saved.record_id,scope=SCOPE,source_id=saved.source_id).content['nightly_diagnostics']==diagnostic
    assert target.title not in json.dumps(report)
    assert target.title not in json.dumps(diagnostic)

def test_typed_attribute_no_answer_scope_and_diagnostic_safety_share_contract(tmp_path):
    query='我的回复长度是什么'
    with Runtime.create(root=tmp_path) as runtime:
        language=memory(runtime,'Response language','用户希望用英文回复。',memory_type='preference')
        diagnostic=memory(runtime,query,'问题：recall 回复长度诊断失败。',memory_type='preference')
        foreign=memory(runtime,'Response length','用户希望回复简短。',memory_type='preference',
                       scope=ScopeRef('synthetic','unified-contract','foreign','owner'))
        request=preference_recall_request(query)
        assert request.attribute=='length' and not supports_preference_request(request,language)
        assert not runtime.memory._is_preference_recall_candidate(diagnostic,query)
        assert runtime.memory.recall(query=query,scope=asdict(SCOPE),task_context=CONTEXT,limit=5).items==[]
        positive=memory(runtime,'Response length','用户希望回复简短。',memory_type='preference')
        bundle=runtime.memory.recall(query=query,scope=asdict(SCOPE),task_context=CONTEXT,limit=5)
        assert positive.record_id in [item.record_id for item in bundle.items]
        assert not {foreign.record_id,diagnostic.record_id,language.record_id}&{item.record_id for item in bundle.items}

def test_generated_unsupported_attribute_case_remains_failed_and_not_replaced(tmp_path):
    with Runtime.create(root=tmp_path) as runtime:
        memory(runtime,'我的回复长度是什么','用户希望用英文回复。',memory_type='preference')
        dataset=build_controlled_memory_benchmark(runtime,scope=SCOPE,source_ids=['hermes']);before=deepcopy(dataset)
        report=run_controlled_memory_benchmark(runtime,dataset,scope=SCOPE,source_ids=['hermes'])
        assert dataset==before
        assert report['sample_count']==report['fail_count']==1 and report['pass_count']==0
        assert report['samples'][0]['failure_reason']=='known_item_missing'
        assert report['passed_threshold'] is report['memory_benchmark_accepted'] is False

def test_context_mutation_cannot_dispatch_or_certify_generated_case(tmp_path,monkeypatch):
    with Runtime.create(root=tmp_path) as runtime:
        memory(runtime,'Orchid archive layout')
        dataset=build_controlled_memory_benchmark(runtime,scope=SCOPE,source_ids=['hermes'])
        dataset['cases'][0]['task_context']['goal']='preference'
        monkeypatch.setattr(runtime.memory,'recall',lambda **kw:pytest.fail('Noncanonical case dispatched'))
        report=run_controlled_memory_benchmark(runtime,dataset,scope=SCOPE,source_ids=['hermes'])
        assert report['fail_count']==1 and report['passed_threshold'] is False
        assert report['samples'][0]['failure_reason']=='source_case_binding_changed'
