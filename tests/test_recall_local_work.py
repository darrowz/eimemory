from contextlib import closing

from eimemory.models.records import RecordEnvelope, ScopeRef
from eimemory.storage.runtime_store import RuntimeStore


def test_scoring_parses_each_candidate_payload_once(tmp_path, monkeypatch):
    with closing(RuntimeStore(tmp_path)) as store:
        scope = ScopeRef(user_id='owner')
        for index in range(6):
            store.append(RecordEnvelope.create(kind='memory', title=f'alpha {index}',
                content={'text': f'alpha beta {index}'}, scope=scope))
        calls = []
        original = store.sqlite._payload_dict_from_json
        def counted(payload):
            calls.append(payload)
            return original(payload)
        monkeypatch.setattr(store.sqlite, '_payload_dict_from_json', counted)
        items, report = store.search_with_diagnostics(query='alpha', kinds=['memory'], scope=scope,
            limit=6, recall_filters={'_exact_scope': True})
        assert len(items) == 6
        assert len(calls) == report['candidate_count']


def test_chinese_context_expansion_does_not_rescan_each_repeated_match(monkeypatch):
    from eimemory.recall import lexical
    original = lexical._is_chinese
    calls = []
    def counted(text):
        calls.append(1)
        return original(text)
    monkeypatch.setattr(lexical, '_is_chinese', counted)
    run = '任务进度' * 80
    assert lexical._expand_chinese_context(run, ['任务', '进度']) == [run]
    assert len(calls) <= len(run) * 3


def test_chinese_context_expansion_preserves_phrase_first_dedup_order():
    from eimemory.recall.lexical import _expand_chinese_context
    assert _expand_chinese_context('甲乙丙 abc 丁乙丙 甲乙丙 戊己', ['乙丙', '甲乙', '戊己']) == [
        '甲乙丙', '丁乙丙', '戊己']


def test_projection_cache_ignores_deadline_but_keeps_authority_and_filters():
    from eimemory.retrieval.contracts import CandidateRequest
    from eimemory.retrieval.postgres_vector import PostgresVectorCandidateSource, PostgresVectorConfig
    source = object.__new__(PostgresVectorCandidateSource)
    source.config = PostgresVectorConfig()
    def key(deadline, source_ids=('allowed',), revision='1'):
        request = CandidateRequest.create(query='alpha', scope=ScopeRef(user_id='owner'),
            source_ids=source_ids, recall_filters={'_recall_collection_deadline_monotonic': deadline})
        return source._cache_key(request, watermark='wm', authority_cursor=('', ''), authority_revision=revision)
    assert key(1.0) == key(2.0)
    assert key(1.0) != key(1.0, source_ids=('other',))
    assert key(1.0) != key(1.0, revision='2')
