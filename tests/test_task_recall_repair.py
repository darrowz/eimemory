from dataclasses import asdict
from contextlib import closing
import json

import pytest

from eimemory.api.memory import MemoryAPI
from eimemory.models.records import RecordEnvelope, ScopeRef
from eimemory.recall import classify_recall_intent
from eimemory.storage.runtime_store import RuntimeStore


@pytest.mark.parametrize('query', [
    '最近已授权任务、进展、待验收', '召回修复任务做到哪了？',
    'What is the status of my authorized tasks?',
    '上次我授权了什么任务？', 'What did we agree to do last time?',
    '查看任务历史记录', 'Recall the history of my tasks',
])
def test_task_query_overrides_native_research_default(query):
    assert classify_recall_intent(query, {'task_type': 'research.task'}).name == 'task_recall'


@pytest.mark.parametrize('query', ['研究任务调度算法', '任务进度管理软件论文', '历史研究方法', 'Graphiti paper'])
def test_general_research_is_not_task_evidence_route(query):
    assert classify_recall_intent(query, {'task_type': 'research.task'}).name == 'research'


def task_record(text, *, memory_type='task_context', user='owner', source_id='allowed', source='user.capture'):
    return RecordEnvelope.create(kind='memory', title='召回修复任务', summary=text,
        content={'text': text, 'memory_type': memory_type},
        meta={'memory_type': memory_type}, source=source, source_id=source_id,
        scope=ScopeRef(agent_id='agent', workspace_id='workspace', user_id=user))


def test_task_context_is_recalled_without_opening_logs_or_other_partitions(tmp_path):
    with closing(RuntimeStore(tmp_path)) as store:
        good = task_record('召回修复任务已完成代码修改，进展：测试通过，目前待验收。')
        preference = task_record('授权后直接办任务，不要给选项。', memory_type='preference')
        forbidden = [
            task_record(good.summary, user='other'),
            task_record(good.summary, source_id='forbidden'),
            task_record(good.summary, memory_type='audit', source='command.audit'),
            task_record(good.summary, source='agent.diagnostic'),
        ]
        for item in [good, preference, *forbidden]:
            store.append(item)
        bundle = MemoryAPI(store).recall(query='召回修复任务进展，哪些待验收？',
            scope=asdict(good.scope), task_context={'task_type': 'research.task', 'source_ids': ['allowed']}, limit=8)
        assert [item.record_id for item in bundle.items] == [good.record_id]
        assert bundle.explanation['recall_intent']['name'] == 'task_recall'
        assert not bundle.explanation.get('raw_evidence')


def test_task_context_remains_hidden_for_regular_research(tmp_path):
    with closing(RuntimeStore(tmp_path)) as store:
        item = task_record('Graphiti paper benchmark task completed.')
        store.append(item)
        bundle = MemoryAPI(store).recall(query='Graphiti paper benchmark', scope=asdict(item.scope),
            task_context={'task_type': 'research.task'}, limit=8)
        assert not bundle.items


def test_task_route_does_not_append_unfiltered_episode_backrefs(tmp_path):
    with closing(RuntimeStore(tmp_path)) as store:
        good = task_record('召回修复任务已完成代码修改，进展：测试通过，目前待验收。')
        unrelated = task_record('不相关的诊断审计。', memory_type='audit', source='agent.diagnostic')
        unrelated.title = 'Hermes completed turn'
        good.evidence = [unrelated.record_id]
        store.append(unrelated)
        store.append(good)
        bundle = MemoryAPI(store).recall(query='召回修复任务进展，哪些待验收？',
            scope=asdict(good.scope), task_context={'task_type': 'research.task'}, limit=8)
        assert [item.record_id for item in bundle.items] == [good.record_id]
        assert not bundle.explanation['cascade_evidence']


@pytest.mark.parametrize('lightweight', [False, True])
def test_native_default_tool_delivers_task_history_through_loadout(tmp_path, lightweight):
    from eimemory.adapters.hermes.provider_core import HermesMemoryProviderCore
    from eimemory.adapters.eibrain.rpc import EIBrainRPCBridge
    from eimemory.adapters.runtime.channel import resolve_channel_scope
    from eimemory.api.runtime import Runtime
    with closing(Runtime.create(root=tmp_path)) as runtime:
        bridge = EIBrainRPCBridge(runtime)
        calls = []
        class LocalClient:
            def call_or_bypass(self, method, params):
                calls.append((method, params))
                return bridge.handle({'method': method, 'params': params})
        provider = HermesMemoryProviderCore(client=LocalClient())
        provider.initialize('task-recall-test', agent_workspace='embodied', user_id='darrow')
        scope = ScopeRef.from_dict(resolve_channel_scope('hermes', provider._common_params()['scope']))
        record = task_record('最近已授权的召回修复任务进展：代码已完成，目前待验收。',
                             memory_type='conversation', source_id='hermes')
        record.scope = scope
        record.title = 'Hermes completed turn'
        runtime.store.append(record)
        if lightweight:
            from test_recall_budget_reserve import fragment_source
            from eimemory.retrieval.lightweight_admission import LightweightAdmission, LightweightConfig
            runtime.memory.recall_engine.candidate_source = fragment_source(runtime.store)
            runtime.memory.recall_engine.relevance_admission = LightweightAdmission(LightweightConfig(enabled=True))
        direct = runtime.memory.recall(query='最近已授权任务、进展、待验收', scope=asdict(scope),
                                       task_context={'task_type': 'research.task'}, limit=8)
        assert [item.record_id for item in direct.items] == [record.record_id]
        output = json.loads(provider.handle_tool_call('eimemory_recall', {'query': '最近已授权任务、进展、待验收'}))
        assert calls[-1][1]['task_type'] == 'task.status'
        assert output['ok'], output
        result = output['result']
        assert [item['record_id'] for item in result['bundle']['items']] == [record.record_id]
        assert '待验收' in result['context']
        history = json.loads(provider.handle_tool_call('eimemory_search_l0', {'query': '上次我授权了什么任务？'}))
        assert history['ok'], history
        assert [item['record_id'] for item in history['result']['bundle']['items']] == [record.record_id]
        provider.shutdown()


@pytest.mark.parametrize('memory_type', ['operator_preference', 'instruction', 'persona', 'user_profile'])
def test_task_history_rejects_persona_variants(tmp_path, memory_type):
    with closing(RuntimeStore(tmp_path)) as store:
        preference = task_record('我们约定已授权任务直接执行，任务完成后再汇报，不要反复请求确认。',
                                 memory_type=memory_type)
        good = task_record('我们约定了召回修复任务，已授权先写复现测试并修复预算问题。')
        store.append(preference)
        store.append(good)
        bundle = MemoryAPI(store).recall(query='上次我授权了什么任务？', scope=asdict(good.scope), limit=8)
        assert [item.record_id for item in bundle.items] == [good.record_id]
