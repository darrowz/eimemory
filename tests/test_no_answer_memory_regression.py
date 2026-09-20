from eimemory.api.runtime import Runtime
from eimemory.models.records import RecordEnvelope, ScopeRef


def test_unrelated_user_question_does_not_recall_link_preference(tmp_path):
    runtime = Runtime.create(root=tmp_path / 'runtime')
    scope = ScopeRef(agent_id='default', workspace_id='hermes::channel::hermes', user_id='test-user')
    try:
        runtime.store.append(RecordEnvelope.create(
            kind='memory', title='微信抓取与链接默认评估',
            summary='微信公众号文章用移动微信 UA（MicroMessenger）curl 可读；不要因验证页失败就让用户贴正文。鸿哥发 URL 默认要测试评估，不是只摘要。',
            scope=scope, source='hermes.memory', source_id='hermes',
            meta={'memory_type': 'convention'},
        ))
        bundle = runtime.memory.recall(query='用户的火星基地门禁口令是什么', scope={'agent_id':scope.agent_id, 'workspace_id':scope.workspace_id, 'user_id':scope.user_id}, limit=5)
        assert not bundle.items
        positive = runtime.memory.recall(query='微信公众号链接默认要评估还是摘要', scope={'agent_id':scope.agent_id, 'workspace_id':scope.workspace_id, 'user_id':scope.user_id}, limit=5)
        assert len(positive.items) == 1
    finally:
        runtime.close()
