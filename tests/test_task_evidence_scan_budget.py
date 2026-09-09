from eimemory.recall import task_queries
from eimemory.models.records import RecordEnvelope, ScopeRef


def test_task_summary_short_circuits_large_transcript(monkeypatch):
    record = RecordEnvelope.create(scope=ScopeRef(agent_id='test',workspace_id='test',user_id='test'), kind='memory', title='任务', summary='已完成',
        detail='unrelated transcript ' * 100000,
        content={'memory_type': 'conversation'}, meta={'memory_type': 'conversation'})
    original = task_queries.supports_task_evidence
    seen=[]
    def observed(mode, text):
        seen.append(len(text))
        return original(mode,text)
    monkeypatch.setattr(task_queries,'supports_task_evidence',observed)
    assert task_queries.is_task_evidence(record,'status')
    assert seen == [2, 3]


def test_task_state_in_detail_still_found():
    record = RecordEnvelope.create(scope=ScopeRef(agent_id='test',workspace_id='test',user_id='test'), kind='memory', title='任务', summary='相关记录',
        detail='召回修复已提交，等待验收。',
        content={'memory_type': 'conversation'}, meta={'memory_type': 'conversation'})
    assert task_queries.is_task_evidence(record,'status')
