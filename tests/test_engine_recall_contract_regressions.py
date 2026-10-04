"""Isolated expected contracts against d55587b6; no remote dependencies."""
from copy import deepcopy
import threading
from time import perf_counter
import pytest
from eimemory.models.records import RecordEnvelope, ScopeRef, TimeRef
from eimemory.recall.indexing import build_recall_index_document, clear_recall_index_document_cache
from eimemory.recall.dedupe import memory_content_key, preference_paraphrase_key
from eimemory.recall.lexical import analyze_lexical_signal
from eimemory.api.memory import MemoryAPI
from eimemory.retrieval.contracts import CandidateBatch, CandidateRequest
from eimemory.retrieval.engine import GovernedRecallEngine
from eimemory.storage.runtime_store import RuntimeStore

STAMP = '2026-10-02T12:00:00Z'

def record(**changes):
    values = dict(record_id='memory_audit', kind='memory', status='active',
        title='synthetic alpha', summary='', detail='', content={'text': 'synthetic alpha'},
        tags=[], links=[], evidence=[], source='synthetic', scope=ScopeRef(tenant_id='audit'),
        time=TimeRef(STAMP, STAMP, STAMP), provenance={}, meta={}, source_id='synthetic_a')
    values.update(changes)
    return RecordEnvelope(**values)

@pytest.fixture(autouse=True)
def isolate(monkeypatch):
    for key in list(__import__('os').environ):
        if key.startswith('EIMEMORY_'):
            monkeypatch.delenv(key)
    # Disallow all network calls in the evidence process.
    def no_network(*args, **kwargs):
        raise AssertionError('network is forbidden by audit')
    monkeypatch.setattr('socket.socket.connect', no_network)
    monkeypatch.setattr('socket.create_connection', no_network)
    clear_recall_index_document_cache()
    yield
    clear_recall_index_document_cache()

@pytest.mark.parametrize('change', [
    {'scope': ScopeRef(tenant_id='other_audit')},
    {'source_id': 'synthetic_b', 'source': 'diagnostic.traceback'},
    {'source': 'diagnostic.traceback', 'content': {'text': 'corrected beta'}},
], ids=['scope_partition', 'source_partition', 'same_second_update'])
def test_index_cache_matches_uncached_authority(change):
    first = record()
    build_recall_index_document(first)
    second = deepcopy(first)
    for field, value in change.items():
        setattr(second, field, value)
    assert build_recall_index_document(second).to_dict() == build_recall_index_document(second, use_cache=False).to_dict()


def test_same_second_cache_does_not_change_pollution_gate():
    api = object.__new__(MemoryAPI)
    api.source_registry = None
    clean = record(content={'text': '默认采用简短摘要', 'memory_type': 'preference'})
    polluted = deepcopy(clean)
    polluted.source = 'diagnostic.traceback'
    build_recall_index_document(clean)
    warm_reason = api._online_recall_pollution_reason(polluted)
    clear_recall_index_document_cache()
    cold_reason = api._online_recall_pollution_reason(polluted)
    assert warm_reason == cold_reason


def test_default_preference_keeps_opposite_step_orders():
    left = record(record_id='memory_order_a', title='打印机卡纸处理', content={
        'memory_type': 'preference', 'text': '默认先断开电源，再取出残纸。'})
    right = record(record_id='memory_order_b', title='打印机卡纸处理', content={
        'memory_type': 'preference', 'text': '默认先取出残纸，再断开电源。'})
    assert memory_content_key(left) != memory_content_key(right)
    left_key, right_key = preference_paraphrase_key(left), preference_paraphrase_key(right)
    assert left_key is None or right_key is None or left_key != right_key
    assert len(MemoryAPI._dedupe_records([left, right])) == 2
    assert len(MemoryAPI._dedupe_records([left, right])) == 2


def test_mixed_default_clause_keeps_steps():
    left = record(record_id='memory_order_a', title='处理偏好', content={
        'memory_type': 'preference', 'text': '默认提供摘要；先断开电源，再取出残纸。'})
    right = record(record_id='memory_order_b', title='处理偏好', content={
        'memory_type': 'preference', 'text': '默认提供摘要；先取出残纸，再断开电源。'})
    left_key, right_key = preference_paraphrase_key(left), preference_paraphrase_key(right)
    assert left_key is None or right_key is None or left_key != right_key
    assert len(MemoryAPI._dedupe_records([left, right])) == 2


def test_decimal_version_is_not_a_major_version_match():
    signal = analyze_lexical_signal('module v1.2', 'module v1.3 has 2 errors')
    assert signal.version_hits == ()
    assert not GovernedRecallEngine._keyword_exact_match('module v1.2', record(title='module v1.3 has 2 errors'))


def test_decimal_version_reports_full_matched_version():
    signal = analyze_lexical_signal('module v1.2', 'module v1.2')
    assert signal.version_hits == ('v1.2',)


class EmptySource:
    name = 'synthetic'
    def search(self, request):
        return CandidateBatch()

@pytest.mark.parametrize('task_type', ['', 'synthetic_task'])
def test_engine_preflight_policy_reads_obey_deadline(tmp_path, task_type):
    store = RuntimeStore(tmp_path / 'synthetic_store')
    api = MemoryAPI(store, candidate_source=EmptySource())
    locked = threading.Event()
    release = threading.Event()
    def hold():
        with store._lock:
            locked.set()
            release.wait(.35)
    thread = threading.Thread(target=hold, daemon=True)
    thread.start()
    assert locked.wait(1)
    started = perf_counter()
    try:
        api.recall(query='synthetic alpha', scope={'tenant_id': 'audit'}, limit=1,
            task_context={'scope_strategy': 'exact', 'kinds': ['memory'], 'task_type': task_type,
                '_recall_deadline_monotonic': started + .025})
        elapsed = perf_counter() - started
    finally:
        release.set()
        thread.join(1)
        store.close()
    assert elapsed < .20, f'25ms budget exceeded by unguarded policy lock: {elapsed:.3f}s'

@pytest.mark.parametrize('stale_kind', ['deleted', 'title_changed', 'inactive', 'rejected',
    'superseded', 'expired', 'digest_mismatch', 'source_not_allowed', 'current'])
def test_canonical_stale_identity_does_not_suppress_legacy_fallback(tmp_path, monkeypatch, stale_kind):
    from eimemory.retrieval.contracts import CandidateHit, CandidateRef, ExactScope
    import eimemory.retrieval.engine as engine_mod
    canonical_scope = ScopeRef(tenant_id='audit', user_id='canonical')
    legacy_scope = ScopeRef(tenant_id='audit', user_id='legacy')
    monkeypatch.setattr(engine_mod, 'is_hongtuish_scope', lambda *_a, **_kw: True)
    monkeypatch.setattr(engine_mod, 'hongtu_scope', lambda *_a, **_kw: {
        'tenant_id': 'audit', 'agent_id': '', 'workspace_id': '', 'user_id': 'canonical'})
    monkeypatch.setattr(engine_mod, 'hongtu_query_scopes_with_aliases', lambda *_a, **_kw: [canonical_scope, legacy_scope])
    store = RuntimeStore(tmp_path / 'synthetic_store')
    fallback = record(record_id='memory_legacy', title='synthetic alpha', scope=legacy_scope)
    store.append(fallback)
    if stale_kind != 'deleted':
        canonical = record(record_id='memory_canonical', scope=canonical_scope)
        if stale_kind == 'title_changed':
            canonical.title, canonical.content = 'unrelated beta', {'text': 'unrelated beta'}
        elif stale_kind == 'inactive':
            canonical.status = 'inactive'
        elif stale_kind == 'rejected':
            canonical.meta = {'quality': {'capture_decision': 'reject'}}
        elif stale_kind == 'superseded':
            canonical.meta = {'superseded_by': 'memory_new'}
        elif stale_kind == 'expired':
            canonical.meta = {'valid_until': '2000-01-01T00:00:00Z'}
        store.append(canonical)
    class Source:
        name = 'synthetic'
        requests = []
        def search(self, request):
            self.requests.append(request.scope)
            rid = 'memory_canonical' if request.scope == ExactScope.from_scope(canonical_scope) else fallback.record_id
            is_canonical = request.scope == ExactScope.from_scope(canonical_scope)
            source_id = 'synthetic_blocked' if is_canonical and stale_kind == 'source_not_allowed' else 'synthetic_a'
            hints = {'identity_indexed': True}
            if is_canonical and stale_kind == 'digest_mismatch':
                hints['_candidate_projection_digest'] = '0' * 64
                hints['_candidate_authoritative_updated_at'] = STAMP
            return CandidateBatch(hits=(CandidateHit(
                ref=CandidateRef(rid, request.scope, source_id), source_rank=1, source_score=1,
                evidence_hints=('exact_title',), component_hints=hints),))
    source = Source()
    api = MemoryAPI(store, candidate_source=source)
    try:
        bundle = api.recall(query='synthetic alpha', scope={'tenant_id': 'audit', 'user_id': 'legacy'},
            task_context={'scope_strategy': 'canonical_first', 'kinds': ['memory'],
                'source_ids': ['synthetic_a']}, limit=1)
        if stale_kind == 'current':
            assert ExactScope.from_scope(legacy_scope) not in source.requests
            assert [item.record_id for item in bundle.items] == ['memory_canonical']
        else:
            assert ExactScope.from_scope(legacy_scope) in source.requests
            assert fallback.record_id in [item.record_id for item in bundle.items]
    finally:
        store.close()


def test_same_second_update_persists_current_recall_index_traits(tmp_path):
    store = RuntimeStore(tmp_path / 'synthetic_store')
    clean = record(content={'text': 'synthetic alpha', 'memory_type': 'preference'})
    polluted = deepcopy(clean)
    polluted.source = 'diagnostic.traceback'
    try:
        store.append(clean)
        store.append(polluted)
        row = store.sqlite.conn.execute(
            'SELECT source, lane, visibility, source_class FROM recall_index WHERE record_id=?',
            (clean.record_id,)).fetchone()
        assert row['source'] == 'diagnostic.traceback'
        assert row['source_class'] == 'diagnostic'
        assert row['lane'] == 'operational'
        assert row['visibility'] == 'evidence_only'
    finally:
        store.close()


def test_keyword_exact_does_not_match_wrong_decimal_version():
    assert not GovernedRecallEngine._keyword_exact_match(
        'module v1.2', record(title='module v1.3 has 2 errors'))


def test_mixed_default_step_orders_both_survive_api_dedupe():
    left = record(record_id='memory_order_a', title='处理偏好', content={
        'memory_type': 'preference', 'text': '默认提供摘要；先断开电源，再取出残纸。'})
    right = record(record_id='memory_order_b', title='处理偏好', content={
        'memory_type': 'preference', 'text': '默认提供摘要；先取出残纸，再断开电源。'})
    assert len(MemoryAPI._dedupe_records([left, right])) == 2


def test_cached_document_scope_is_not_poisoned_by_consumer_mutation():
    current = record()
    document = build_recall_index_document(current)
    document.scope['tenant_id'] = 'consumer_mutation'
    assert build_recall_index_document(current).scope['tenant_id'] == 'audit'

@pytest.mark.parametrize('field,value', [
    ('meta', {'memory_type': 'conversation'}),
    ('meta', {'quality': {'salience_score': .95}}),
    ('provenance', {'capture_origin': 'turn_sync'}),
    ('tags', ['new_anchor']),
    ('title', 'updated title'),
])
def test_cache_tracks_all_projection_input_values(field, value):
    current = record()
    build_recall_index_document(current)
    setattr(current, field, value)
    assert build_recall_index_document(current).to_dict() == build_recall_index_document(current, use_cache=False).to_dict()


def test_cache_hit_scope_is_also_isolated():
    current = record()
    build_recall_index_document(current)
    hit = build_recall_index_document(current)
    hit.scope['tenant_id'] = 'modified_hit'
    assert build_recall_index_document(current).scope['tenant_id'] == 'audit'


def test_cache_unsupported_values_bypass_without_repr_identity():
    from eimemory.recall.indexing import recall_index_document_compute_count
    current = record(content={'text': ('not', 'a', 'json', 'list')})
    assert build_recall_index_document(current).body_text == "('not', 'a', 'json', 'list')"
    build_recall_index_document(current)
    assert recall_index_document_compute_count() == 2


def test_cache_projection_and_key_use_the_same_copied_input(monkeypatch):
    from eimemory.recall import indexing
    current = record()
    real_copy = indexing.deepcopy
    def copy_then_mutate(value):
        copied = real_copy(value)
        value.source = 'diagnostic.traceback'
        return copied
    monkeypatch.setattr(indexing, 'deepcopy', copy_then_mutate)
    first = build_recall_index_document(current)
    assert first.source_class == 'default'
    assert build_recall_index_document(current).source_class == 'diagnostic'

@pytest.mark.parametrize('query,text,versions', [
    ('module v1.2.3', 'module v1.2.3', ('v1.2.3',)),
    ('module v1.2.3', 'module v1.2.4 has 3 errors', ()),
    ('module v1.2', 'module v1.2.3', ()),
    ('module v1.2', 'module v1.2beta', ()),
    ('v2', 'MIPROv2', ()),
    ('v2.1', 'MIPROv2.1', ()),
])
def test_lexical_version_tokens_remain_atomic(query, text, versions):
    assert analyze_lexical_signal(query, text).version_hits == versions


def test_lexical_version_repair_preserves_other_ascii_cleanup():
    from eimemory.recall import lexical
    assert lexical._clean_text('ABC12-3 alpha_beta alpha-v2.1') == 'abc12 3 alpha_beta alpha v2 1'
    assert lexical._TOKEN_RE.findall('v1.2 v1.2.3 MIPROv2') == ['v1.2', 'v1.2.3', 'MIPROv2']


def test_cache_custom_deepcopy_cannot_change_uncached_semantics():
    from eimemory.recall.indexing import recall_index_document_compute_count
    class UnsupportedText:
        copies = 0
        def __str__(self):
            return 'original unsupported text'
        def __deepcopy__(self, memo):
            self.copies += 1
            return 'changed by custom deepcopy'
    text = UnsupportedText()
    current = record(content={'text': text})
    assert build_recall_index_document(current).body_text == 'original unsupported text'
    assert build_recall_index_document(current).body_text == 'original unsupported text'
    assert text.copies == 0
    assert recall_index_document_compute_count() == 2


def test_cache_retains_bounded_lru_and_repeat_hits(monkeypatch):
    from eimemory.recall import indexing
    monkeypatch.setattr(indexing, '_RECALL_DOC_CACHE_MAXSIZE', 2)
    a, b, c = [record(record_id=f'memory_lru_{key}') for key in 'abc']
    for item in (a, b, a, c, a):
        build_recall_index_document(item)
    assert len(indexing._RECALL_DOC_CACHE) == 2
    assert indexing.recall_index_document_compute_count() == 3
    build_recall_index_document(b)
    assert len(indexing._RECALL_DOC_CACHE) == 2
    assert indexing.recall_index_document_compute_count() == 4


def test_policy_deadline_failure_is_unavailable_and_restores_store(tmp_path, monkeypatch):
    store = RuntimeStore(tmp_path / 'synthetic_store')
    api = MemoryAPI(store, candidate_source=EmptySource())
    def no_policy(*_args, **_kwargs):
        pytest.fail('expired preflight must not enter policy read')
    monkeypatch.setattr(store, 'search_policy', no_policy)
    try:
        bundle = api.recall(query='synthetic alpha', scope={'tenant_id': 'audit'},
            task_context={'_recall_deadline_monotonic': perf_counter() - 1}, limit=1)
        assert bundle.explanation['retrieval_status'] == 'unavailable'
        assert bundle.explanation['relevance_selector']['collection_complete'] is False
        assert not bundle.reflections
        assert store.sqlite.conn.execute('SELECT 1').fetchone()[0] == 1
    finally:
        store.close()


def test_engine_does_not_hold_store_lock_while_calling_candidate_source(tmp_path):
    store = RuntimeStore(tmp_path / 'synthetic_store')
    class Source:
        name = 'synthetic'
        calls = 0
        def search(self, request):
            assert not store._lock._is_owned()
            self.calls += 1
            return CandidateBatch()
    source = Source()
    api = MemoryAPI(store, candidate_source=source)
    try:
        api.recall(query='synthetic alpha', scope={'tenant_id': 'audit'},
            task_context={'scope_strategy': 'exact', 'task_type': 'synthetic_task'}, limit=1)
        assert source.calls == 1
    finally:
        store.close()

@pytest.mark.parametrize('query,text,versions', [
    ('升级到v1.2版本', '升级到v1.2版本', ('v1.2',)),
    ('升级到v1.2版本', '升级到v1.3版本另有2个修复', ()),
    ('version v1.2.', 'version v1.2.', ('v1.2',)),
    ('version v1.2.', 'version v1.3. has 2 errors', ()),
    ('版本v1.2.3。', '版本v1.2.3。', ('v1.2.3',)),
    ('版本v1.2.3。', '版本v1.2.4。另有3个修复', ()),
])
def test_dotted_versions_support_natural_sentence_boundaries(query, text, versions):
    assert analyze_lexical_signal(query, text).version_hits == versions
    assert GovernedRecallEngine._keyword_exact_match(query, record(title=text)) == bool(versions)
