"""Sanitized pre-call record structure controls, not production history replay."""
from contextlib import closing
from dataclasses import asdict, replace

import pytest

from eimemory.api.memory import MemoryAPI
from eimemory.models.records import RecordEnvelope, ScopeRef
from eimemory.recall import classify_recall_intent
from eimemory.storage.runtime_store import RuntimeStore


# Frozen from an authorized active conversation created AND last updated before
# B1. Keep one factual clause; replace path/project, counts and scope identifiers.
# No original query, record ID, personal identity or production DB is retained.
TEXT = ('已完成的证据：压缩包内若干个清单文件哈希匹配；补丁校验器确认本地仓库处于指定基线、'
        '工作区原本无改动，随后将若干个文件应用到 Atlas项目，并逐文件校验了应用后的字节')


@pytest.mark.parametrize('query', [
    'Atlas项目现在进展如何？', 'Atlas项目现在有哪些卡点需要修复？',
    'Atlas项目任务进展有哪些待验收？',
])
def test_project_status_is_task_route_not_operational_permission(query):
    assert classify_recall_intent(query, {'task_type': 'research.task'}).name == 'task_recall'
    assert not MemoryAPI._allows_operational_recall(query, {})


@pytest.mark.parametrize('query', [
    '研究任务调度算法', '如何改善Atlas项目管理软件的进度展示？',
    'Atlas项目进展管理软件论文', '如何修复Atlas项目的卡点？',
])
def test_project_research_neighbors_stay_research(query):
    assert classify_recall_intent(query, {'task_type': 'research.task'}).name == 'research'


def test_frozen_status_natural_and_keyword_controls(tmp_path):
    with closing(RuntimeStore(tmp_path)) as store:
        good = RecordEnvelope.create(kind='memory', title='Hermes completed turn', summary=TEXT,
            content={'text': TEXT, 'memory_type': 'conversation'}, meta={'memory_type': 'conversation'},
            source='hermes.memory', source_id='hermes',
            scope=ScopeRef(agent_id='agent', workspace_id='workspace::channel::hermes', user_id='owner'))
        store.append(good)
        for index, changes in enumerate([
            {'scope': replace(good.scope, user_id='other')},
            {'source_id': 'forbidden'}, {'status': 'revoked'},
            {'content': {'text': TEXT, 'memory_type': 'audit'}, 'meta': {'memory_type': 'audit'}},
        ]):
            store.append(replace(good, record_id=f'mem_negative_{index}', **changes))
        for query in ['Atlas项目任务进展有哪些待验收？', 'Atlas项目现在进展如何？']:
            bundle = MemoryAPI(store).recall(query=query, scope=asdict(good.scope),
                task_context={'task_type': 'research.task', 'source_ids': ['hermes']}, limit=8)
            assert [r.record_id for r in bundle.items] == [good.record_id]
        research = MemoryAPI(store).recall(query='如何改善Atlas项目管理软件的进度展示？',
            scope=asdict(good.scope), task_context={'task_type': 'research.task', 'source_ids': ['hermes']})
        assert not research.items
