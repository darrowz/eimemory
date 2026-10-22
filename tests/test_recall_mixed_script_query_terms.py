"""Mixed CJK/ASCII query runs must reach the FTS candidate arm."""
from contextlib import closing

from eimemory.models.records import RecordEnvelope, ScopeRef
from eimemory.retrieval.contracts import CandidateRequest
from eimemory.retrieval.sqlite_source import SQLiteCandidateSource
from eimemory.storage.runtime_store import RuntimeStore

SCOPE = ScopeRef(tenant_id='audit', agent_id='agent', workspace_id='workspace', user_id='owner')


def _put(store, rid, text):
    record = RecordEnvelope.create(kind='memory', title=text[:24], summary=text,
        content={'text': text, 'memory_type': 'event'}, scope=SCOPE, source_id='hermes',
        source='test.l1', meta={'memory_type': 'event', 'force_capture': True})
    record.record_id = rid
    return store.append(record)


def test_mixed_script_term_offers_each_script_run(tmp_path):
    with closing(RuntimeStore(tmp_path)) as store:
        with store._lock:
            terms = store.sqlite._candidate_query_terms('清理下honrui')
            assert terms[0] == '清理下honrui'
            assert 'honrui' in terms and '清理下' in terms
            expression = store.sqlite._fts_query('清理下honrui')
            assert '"honrui"' in expression
            # Pure-script terms keep their previous expansion.
            assert store.sqlite._candidate_query_terms('alpha beta') == ('alpha', 'beta')
            assert store.sqlite._candidate_query_terms('大模型') == ('大模型', '大模', '模型')


def test_mixed_script_query_retrieves_ascii_entity_record(tmp_path):
    with closing(RuntimeStore(tmp_path)) as store:
        _put(store, 'mem_target', 'Cleaned caches on honrui and freed disk space')
        _put(store, 'mem_other', 'Unrelated note about weather in the park')
        source = SQLiteCandidateSource(store)
        batch = source.search(CandidateRequest.create(query='清理下honrui', scope=SCOPE,
                                                      source_ids=['hermes'], limit=8))
        assert 'mem_target' in [hit.ref.record_id for hit in batch.hits]
