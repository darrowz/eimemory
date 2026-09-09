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


def test_lexical_matching_does_not_materialize_unrelated_record_bigrams(monkeypatch):
    from eimemory.recall import lexical
    extracted = []
    original = lexical._extract_terms
    def counted(text):
        extracted.append(text)
        return original(text)
    monkeypatch.setattr(lexical, '_extract_terms', counted)
    query = '任务进展 v2'
    record = '其他信息' * 1000 + ' 任务进展已完成 v2'
    signal = lexical.analyze_lexical_signal(query, record)
    assert '任务进展' in signal.exact_phrase_hits
    assert 'v2' in signal.version_hits
    assert max(map(len, extracted)) <= len(query)


def test_character_ngrams_avoid_python_character_normalization():
    from eimemory.storage.sqlite_store import SqliteRecordStore
    class Text(str):
        visits = 0
        def lower(self):
            return self
        def __iter__(self):
            for character in super().__iter__():
                self.visits += 1
                yield character
    text = Text('甲乙\t丙丁\u3000戊己\n庚辛')
    store = object.__new__(SqliteRecordStore)
    assert store._char_ngrams(text) == {'甲乙丙', '乙丙丁', '丙丁戊', '丁戊己', '戊己庚', '己庚辛'}
    assert text.visits == 0


def test_sqlite_hint_construction_does_not_freeze_twice(tmp_path, monkeypatch):
    from eimemory.retrieval import sqlite_source
    from eimemory.retrieval.contracts import CandidateRequest
    calls = []
    from eimemory.retrieval.contracts import freeze_value
    original = freeze_value
    def counted(value):
        calls.append(1)
        return original(value)
    monkeypatch.setattr(sqlite_source, 'freeze_value', counted, raising=False)
    with closing(RuntimeStore(tmp_path)) as store:
        scope = ScopeRef(user_id='owner')
        store.append(RecordEnvelope.create(kind='memory', title='alpha detail',
            summary='alpha records the verified deployment result and beta acceptance details.',
            content={'text': 'alpha records the verified deployment result and beta acceptance details.'},
            meta={'quality': {'capture_decision': 'accept', 'salience_score': .8}}, scope=scope))
        batch = sqlite_source.SQLiteCandidateSource(store).search(
            CandidateRequest.create(query='alpha', scope=scope, kinds=['memory'], limit=2))
        assert batch.hits
        assert 'final_score' in batch.hits[0].component_dict()
        assert calls == []
