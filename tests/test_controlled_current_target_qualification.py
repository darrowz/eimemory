"""Independent current-target qualification contracts, using native local stores.

No test changes query wording, limits, task context, ranking or expected IDs
after generation. The only model fixture echoes supplied chat assertions; it
never chooses a temporal winner. These synthetic records are not CPA receipts.
"""
from copy import deepcopy
from dataclasses import asdict
from datetime import datetime, timezone
from hashlib import sha256
from types import SimpleNamespace
import json
import socket

import pytest

from eimemory.api.runtime import Runtime
from eimemory.evaluation import controlled_memory_benchmark as benchmark
from eimemory.evaluation import controlled_production_acceptance as cpa
from eimemory.metadata import business_metadata
from eimemory.models.records import LinkRef, RecordEnvelope, ScopeRef


SCOPE = ScopeRef(tenant_id='qualification-independent', agent_id='wren',
                 workspace_id='dock::channel::hermes', user_id='fixture-owner')
SOURCES = ['hermes']


@pytest.fixture
def local(tmp_path, monkeypatch):
    from eimemory.llm.command_client import CommandLLMClient
    from eimemory.retrieval import postgres_cli
    monkeypatch.setenv('EIMEMORY_LIGHTWEIGHT_ADMISSION_ENABLED', '0')
    monkeypatch.setattr(postgres_cli, 'runtime_candidate_source_from_env', lambda store: None)
    monkeypatch.setattr(CommandLLMClient, 'complete', lambda *a, **kw: pytest.fail('No external model'))
    monkeypatch.setattr(socket, 'create_connection', lambda *a, **kw: pytest.fail('No network'))
    with Runtime.create(root=tmp_path/'runtime', profile='core') as runtime:
        yield runtime


def fact(runtime, index=0, *, orphan=False, updated='2020-01-02T00:00:00Z'):
    text = f'Calibration cabinet WRN-{index:03d} uses an amber inspection card.'
    record = RecordEnvelope.create(kind='memory', title=f'Calibration cabinet WRN-{index:03d}',
        summary=text, content={'text':text, 'memory_type':'fact'}, source='hermes.memory',
        source_id='hermes', scope=SCOPE,
        meta={'memory_type':'fact', 'force_capture':True, 'memory_layer':'l1',
              **({'capture_origin':'l1_extract'} if orphan else {})},
        links=[LinkRef('derived_from','memory',f'absent-source-{index}')] if orphan else [])
    record.time.created_at='2020-01-01T00:00:00Z'
    record.time.updated_at=updated
    return runtime.store.append(record)


def build(runtime, limit=3, cutoff=''):
    return benchmark.build_controlled_memory_benchmark(runtime, scope=SCOPE, source_ids=SOURCES,
                                                        limit=limit, stable_before=cutoff)


def run(runtime, dataset):
    return benchmark.run_controlled_memory_benchmark(runtime, dataset, scope=SCOPE, source_ids=SOURCES)


def selected(dataset):
    return {case['source_binding']['record_id'] for case in dataset['cases']}


def entry_policy(minimum=3, maximum=3):
    return (dict(scope=asdict(SCOPE),source_ids=SOURCES),
            dict(minimum_cases_per_scope=minimum,max_cases_per_scope=maximum))


def test_three_ordinary_targets_use_unchanged_default_retrieval(local, monkeypatch):
    targets=[fact(local,index) for index in range(3)]
    original=local.memory.recall
    calls=[]
    def observe(**kwargs):
        calls.append(deepcopy(kwargs))
        return original(**kwargs)
    monkeypatch.setattr(local.memory,'recall',observe)
    dataset=build(local)
    assert calls==[], 'Qualification must not select targets by trial retrieval'
    before=deepcopy(dataset)
    assert selected(dataset)=={record.record_id for record in targets}
    report=run(local,dataset)
    assert report['pass_count']==report['evaluated_case_count']==3,report
    assert report['passed_threshold'] is True
    assert dataset==before
    assert calls==[dict(query=case['query'],scope=asdict(SCOPE),
                       task_context=case['task_context'],limit=case['limit']) for case in dataset['cases']]
    assert all(call['limit']==5 and set(call['task_context'])=={
        'source_ids','target_source_id','exact_scope_only','runtime_channel'} for call in calls)


def test_orphaned_l1_is_skipped_before_any_retrieval(local,monkeypatch):
    denied=fact(local,41,orphan=True)
    monkeypatch.setattr(local.memory,'recall',lambda **kw:pytest.fail('Inapplicable target must not retrieve'))
    dataset=build(local)
    assert denied.record_id not in selected(dataset)
    assert dataset['dataset_generation']['skipped_reason_counts']=={'source_current_evidence_inapplicable':1}
    report=run(local,dataset)
    assert report['passed_threshold'] is False
    assert report.get('evaluated_case_count',0)==report['sample_count']==0


def test_skipped_newer_targets_do_not_consume_the_positive_case_budget(local,monkeypatch):
    targets=[fact(local,index) for index in range(3)]
    for index in range(9):fact(local,100+index,orphan=True,updated='2021-01-01T00:00:00Z')
    monkeypatch.setattr(local.memory,'recall',lambda **kw:pytest.fail('Generation must remain retrieval-free'))
    dataset=build(local,limit=3)
    assert selected(dataset)=={record.record_id for record in targets}
    generation=dataset['dataset_generation']
    assert generation['scanned_count']==12
    assert generation['skipped_reason_counts']=={'source_current_evidence_inapplicable':9}
    assert generation['scan_truncated'] is False


def test_native_scan_cap_stays_bounded_and_cannot_certify_less_than_minimum(local,monkeypatch):
    for index in range(3):fact(local,index)
    for index in range(65):fact(local,200+index,orphan=True,updated='2021-01-01T00:00:00Z')
    native_reader=local.store.list_controlled_memory_candidates
    reads=[]
    def observed(**kwargs):
        reads.append(deepcopy(kwargs))
        return native_reader(**kwargs)
    monkeypatch.setattr(local.store,'list_controlled_memory_candidates',observed)
    monkeypatch.setattr(local.memory,'recall',lambda **kw:pytest.fail('Scan exhaustion must not run retrieval'))
    dataset=build(local,limit=3)
    assert dataset['cases']==[]
    assert dataset['dataset_generation']['scanned_count']==64
    assert dataset['dataset_generation']['scan_truncated'] is True
    assert reads[0]['limit']==65
    entry,policy=entry_policy()
    with pytest.raises(cpa.ControlledAcceptanceError,match='insufficient_existing_records'):
        cpa._evaluate_agent(local,entry,policy,'2025-01-01T00:00:00Z')


def test_two_valid_targets_never_satisfy_three_case_cpa_minimum(local,monkeypatch):
    for index in range(2):fact(local,index)
    fact(local,33,orphan=True,updated='2021-01-01T00:00:00Z')
    assert len(build(local)['cases'])==2
    monkeypatch.setattr(local.memory,'recall',lambda **kw:pytest.fail('Minimum check precedes retrieval'))
    entry,policy=entry_policy()
    with pytest.raises(cpa.ControlledAcceptanceError,match='insufficient_existing_records'):
        cpa._evaluate_agent(local,entry,policy,'2025-01-01T00:00:00Z')


@pytest.mark.parametrize('fail_on',[1,2],ids=['before-any-target','after-one-valid-target'])
def test_unknown_authority_read_is_not_a_skip_or_partial_positive_set(local,monkeypatch,fail_on):
    for index in range(3):fact(local,index)
    monkeypatch.setattr(local.memory,'recall',lambda **kw:pytest.fail('Unknown authority must not retrieve'))
    native=local.store.read_consistent
    reads=[]
    def bounded(*args,**kwargs):
        reads.append(True)
        if len(reads)==fail_on:raise TimeoutError('synthetic read deadline')
        return native(*args,**kwargs)
    monkeypatch.setattr(local.store,'read_consistent',bounded)
    dataset=build(local)
    assert dataset['cases']==[]
    assert dataset['dataset_generation']['ok'] is False
    assert dataset['dataset_generation']['reason']=='source_current_authority_unavailable'
    assert run(local,dataset)['passed_threshold'] is False
    assert len(reads)>=fail_on


def test_complete_target_qualification_does_not_certify_incomplete_retrieval(local,monkeypatch):
    for index in range(3):fact(local,index)
    dataset=build(local)
    native=local.memory.recall
    def incomplete(**kwargs):
        bundle=native(**kwargs)
        assert bundle.items, 'Keep real successful retrieval; fault only its completeness evidence'
        bundle.explanation['relevance_selector']['collection_complete']=False
        return bundle
    monkeypatch.setattr(local.memory,'recall',incomplete)
    report=run(local,dataset)
    assert report['passed_threshold'] is False
    assert report['pass_count']==0 and report['fail_count']==3
    assert {sample['failure_reason'] for sample in report['samples']}=={'controlled_retrieval_incomplete'}


def test_historical_exact_grant_does_not_replace_a_now_inapplicable_target(local,monkeypatch):
    original=fact(local,72)
    granted_case=build(local,limit=1)['cases'][0]
    # Preserve the exact grant; a later source withdrawal cannot authorize one
    # of the many other old records, even when all are otherwise eligible.
    original.status='removed'
    local.store.rewrite(original)
    for index in range(3):fact(local,80+index)
    entry,policy=entry_policy(minimum=1,maximum=1)
    policy.update(purpose='historical_business_qualification',business_sources=[dict(
        source_binding=granted_case['source_binding'],source_version=['f'*64,'e'*64,'old-physical'],
        origin_source=original.source)])
    before=deepcopy(policy)
    calls=[]
    monkeypatch.setattr(local.memory,'recall',lambda **kw:calls.append(kw))
    with pytest.raises(cpa.ControlledAcceptanceError):
        cpa._evaluate_agent(local,entry,policy,'2025-01-01T00:00:00Z')
    assert calls==[] and policy==before
    assert len(build(local)['cases'])==3, 'Qualified alternatives exist but are outside the exact grant'


def test_historical_grant_rechecks_parent_without_replacing_unchanged_child(local,monkeypatch):
    parent=RecordEnvelope.create(kind='memory',title='Original WRN source turn',
        content={'text':'The WRN cabinet uses amber cards.'},scope=SCOPE,source='hermes.gateway',source_id='hermes',
        meta={'capture_origin':'turn_sync','memory_layer':'l0','force_capture':True})
    parent.time.created_at=parent.time.updated_at='2020-01-01T00:00:00Z'
    parent=local.store.append(parent)
    child=fact(local,93,orphan=True)
    child.links=[LinkRef('derived_from','memory',parent.record_id)]
    child.meta={**business_metadata(child.meta),'authoritative':True,'runtime_channel':'hermes',
        'source_event_id':'independent-import-93',
        'native_import':dict(schema='hermes_native_memory_import.v1',file='/original-business/MEMORY.md',
                            line=94,file_digest='d'*64,
                            text_digest=sha256(child.content['text'].strip().encode()).hexdigest())}
    child=local.store.rewrite(child)
    dataset=build(local,limit=1)
    assert selected(dataset)=={child.record_id}
    grant=deepcopy(dataset['cases'][0]['source_binding'])
    cutoff=datetime.now(timezone.utc).isoformat()
    version=cpa._physical(local,grant,SCOPE,tuple(SOURCES),cutoff)
    entry,policy=entry_policy(minimum=1,maximum=1)
    policy.update(purpose='historical_business_qualification',business_sources=[dict(
        source_binding=grant,source_version=version,origin_source=child.source)])
    from eimemory.evaluation.historical_business_qualification import business_provenance
    assert business_provenance(local,grant,version,SCOPE,tuple(SOURCES),cutoff,policy)['origin_source']==child.source
    parent.status='removed'
    local.store.rewrite(parent)
    for index in range(3):fact(local,300+index)
    before=deepcopy(policy)
    assert benchmark._binding(local.store.get_by_exact_ref(child.record_id,scope=SCOPE,source_id='hermes'))==grant
    monkeypatch.setattr(local.memory,'recall',lambda **kw:pytest.fail('Historical target is inapplicable'))
    with pytest.raises(cpa.ControlledAcceptanceError,match='controlled_acceptance_source_target_inapplicable'):
        cpa._evaluate_agent(local,entry,policy,cutoff)
    assert policy==before
    assert child.record_id not in selected(build(local))


def test_legacy_exact_derived_parent_remains_eligible_without_new_binding_field(local):
    parent=RecordEnvelope.create(kind='memory',title='Original cabinet inspection',
        content={'text':'Cabinet WRN-511 uses an amber inspection card.'},scope=SCOPE,
        source='hermes.gateway',source_id='hermes',
        meta={'capture_origin':'turn_sync','memory_layer':'l0','force_capture':True})
    parent.time.created_at=parent.time.updated_at='2020-01-01T00:00:00Z'
    parent=local.store.append(parent)
    child=fact(local,511,orphan=True)
    child.links=[LinkRef('derived_from','memory',parent.record_id)]
    child=local.store.rewrite(child)
    assert 'source_event_binding' not in business_metadata(child.meta)
    dataset=build(local,limit=3)
    assert selected(dataset)=={child.record_id}
    result=run(local,dataset)
    assert result['passed_threshold'] is True,result


class AssertionEcho:
    def complete(self,**kwargs):
        prompt=kwargs['user_prompt']
        if prompt.startswith('{'):
            parsed=json.loads(prompt)
            payload=[dict(record_id=item['record_id'],action='store',target_ids=[]) for item in parsed['new_memories']]
        else:
            assertion=prompt.split('\nUSER:\n',1)[1].split('\n\nASSISTANT:\n',1)[0]
            payload=[dict(content=assertion,type='fact',priority=90)]
        return SimpleNamespace(text=json.dumps(payload,ensure_ascii=False))


@pytest.fixture
def chat(local,monkeypatch):
    from eimemory.adapters.runtime import service as service_module
    original=service_module.extract_l1_from_l0_record
    def extraction(*args,**kwargs):
        kwargs.update(llm=AssertionEcho(),use_llm=True,fallback_heuristic=False)
        return original(*args,**kwargs)
    monkeypatch.setattr(service_module,'extract_l1_from_l0_record',extraction)
    service=service_module.AgentRuntimeMemoryService(local)
    turns=[]
    def say(text):
        turns.append(text)
        result=service.sync_turn(channel='hermes',scope=asdict(SCOPE),session_id='qualification-source',
            turn_id=str(len(turns)),user_text=text,assistant_text='Recorded.')
        assert result['ok'] is True
        if result.get('l1_queued'):assert service.drain_l1_queue(limit=1)==1
        return result['record']['record_id']
    return say


def test_valid_current_temporal_target_is_not_rejected_as_historical(local,chat):
    chat('自2020-01-01起，雾鹭设备归属部门是北方校验组。')
    dataset=build(local,limit=10,cutoff=datetime.now(timezone.utc).isoformat())
    assert len(dataset['cases'])==1,dataset['dataset_generation']
    case=dataset['cases'][0]
    record=local.store.get_by_exact_ref(case['source_binding']['record_id'],scope=SCOPE,source_id='hermes')
    assert business_metadata(record.meta).get('living_memory_v1',{}).get('temporal')
    assert '北方校验组' in record.summary
    assert run(local,dataset)['passed_threshold'] is True


def test_current_temporal_replacement_keeps_successor_and_excludes_old_target(local,chat):
    chat('自2020-01-01起，溪塔设备归属部门是旧校验组。')
    old=build(local,limit=10)
    assert len(old['cases'])==1
    old_ids=selected(old)
    chat('自2021-01-01起，溪塔设备归属部门改为新校验组。')
    current=build(local,limit=10)
    assert len(current['cases'])==1,current['dataset_generation']
    assert selected(current).isdisjoint(old_ids)
    sample=run(local,old)
    assert sample['passed_threshold'] is False
    assert sample['samples'][0]['evaluable'] is False
    assert run(local,current)['passed_threshold'] is True


def test_future_temporal_target_is_not_selected_using_creation_cutoff_as_now(local,chat):
    chat('自2097-01-01起，舟羽设备归属部门是未来校验组。')
    dataset=build(local,limit=10,cutoff='2098-01-01T00:00:00Z')
    assert dataset['cases']==[]
    reasons=dataset['dataset_generation']['skipped_reason_counts']
    assert sum(reasons.values())>=1
    assert set(reasons)<={'source_temporal_inapplicable','source_record_not_yet_valid'}
