"""Long queries: supplementary bm25 collection fills unused FTS budget only."""
from eimemory.models.records import RecordEnvelope, ScopeRef
from eimemory.storage.runtime_store import RuntimeStore

SCOPE = ScopeRef(agent_id="main", workspace_id="w")
FILLER = "今天下午我们讨论了很多别的事情包括天气午饭会议室预订以及周末安排还有一些零散的琐事"


def _store(tmp_path):
    store = RuntimeStore(root=tmp_path)
    target = RecordEnvelope.create(kind="memory", title="部署验收流程偏好",
                                   summary="明确授权提交部署验收后再执行发布与回滚检查", scope=SCOPE)
    other = RecordEnvelope.create(kind="memory", title="家里猫粮购买", summary="每月补充一次猫粮和猫砂", scope=SCOPE)
    store.append(target)
    store.append(other)
    return store, target


def test_short_query_has_no_supplement(tmp_path):
    store, _ = _store(tmp_path)
    assert store.sqlite._long_query_fts_query("部署验收") == ""


def test_long_query_expression_is_wider_but_bounded(tmp_path):
    store, _ = _store(tmp_path)
    query = FILLER + "请按明确授权提交部署验收的流程执行" + FILLER
    wide = store.sqlite._long_query_fts_query(query)
    primary = store.sqlite._fts_query(query)
    assert wide and wide.count(" OR ") + 1 > primary.count(" OR ") + 1
    assert wide.count(" OR ") + 1 <= 64


def test_long_query_supplement_brings_target_into_pool(tmp_path):
    store, target = _store(tmp_path)
    query = FILLER + "请按明确授权提交部署验收的流程执行" + FILLER
    rows, report = store.sqlite._candidate_rows(query=query, kinds=None, scope=SCOPE, limit=5,
                                                recall_filters={"candidate_limit": 40})
    ids = {str(row["record_id"]) for row in rows}
    assert target.record_id in ids
    assert report.get("long_query_supplement", 0) >= 0
