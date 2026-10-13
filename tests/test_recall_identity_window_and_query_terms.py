"""Native bounded identity and long-question candidate regressions."""
from contextlib import closing
from dataclasses import asdict, replace

import pytest

from eimemory.api.memory import MemoryAPI
from eimemory.adapters.create_safety_gate import gate_host_create
from eimemory.models.records import RecordEnvelope, ScopeRef
from eimemory.retrieval.authority_gate import authoritative_identity_exists
from eimemory.retrieval.contracts import CandidateRequest
from eimemory.retrieval.sqlite_source import SQLiteCandidateSource
from eimemory.storage.runtime_store import RuntimeStore


SCOPE = ScopeRef(tenant_id='audit', agent_id='agent', workspace_id='workspace', user_id='owner')
TITLE = 'Synthetic account allocation'
PREFIX = 'Please retrieve previous stored project account configuration details regarding documented historical ownership'


def put(store, rid, *, title=TITLE, text=TITLE, stale=False, source='hermes', scope=SCOPE, newer=False):
    record = RecordEnvelope.create(kind='memory', title=title, summary=text,
        content={'text': text, 'memory_type': 'fact'}, scope=scope, source_id=source,
        source='test.l1', meta={'memory_type': 'fact', 'force_capture': True,
                              **({'valid_until': '2000-01-01T00:00:00Z'} if stale else {})})
    record.record_id = rid
    record.time.updated_at = record.time.created_at = record.time.occurred_at = (
        '2026-02-01T00:00:00Z' if newer else '2026-01-01T00:00:00Z')
    return store.append(record)


@pytest.mark.parametrize('count,valid,expected', [
    (1, {0}, True), (8, {0}, True), (8, set(), False),
    (9, {0, 8}, False), (9, {0}, None), (80, {0, 79}, None),
    (80, {79}, None),
])
def test_identity_uniqueness_requires_a_complete_lookup(tmp_path, count, valid, expected):
    with closing(RuntimeStore(tmp_path)) as store:
        for index in range(count):
            put(store, f'mem_{index:03d}', stale=index not in valid)
        # Foreign identities cannot create ambiguity in the authorized partition.
        put(store, 'mem_foreign_source', source='codex')
        put(store, 'mem_foreign_user', scope=replace(SCOPE, user_id='other'))
        memory = MemoryAPI(store)
        request = CandidateRequest.create(query=TITLE, scope=SCOPE, kinds=['memory'],
            source_ids=['hermes'], target_source_id='hermes')
        assert authoritative_identity_exists(memory.recall_engine, query=TITLE, request=request,
            target_source_id='hermes') is expected


def test_truncated_identity_cannot_publish_unique_exists_or_authorize_force(tmp_path):
    with closing(RuntimeStore(tmp_path)) as store:
        for index in range(80):
            put(store, f'mem_{index:03d}', stale=index not in {0, 79})
        bundle = MemoryAPI(store).recall(query=TITLE, scope=asdict(SCOPE), limit=1,
            task_context={'source_ids': ['hermes'], 'target_source_id': 'hermes',
                          'exact_scope_only': True})
        assert [item.record_id for item in bundle.items] == ['mem_000']
        fusion = bundle.explanation['fusion']
        assert fusion['create_safety'] == 'unavailable'
        assert fusion['ambiguity_reasons'] == ['identity_lookup_unavailable']
        decision = gate_host_create(None, text=TITLE, scope=asdict(SCOPE), force=True,
                                    fusion_hint=bundle.explanation)
        assert not decision['allow']
        assert decision['reason'] == 'create_safety_unavailable'


@pytest.mark.parametrize('force_anchor', [False, True])
def test_long_question_collects_tail_evidence_without_expanding_partition(tmp_path, force_anchor):
    with closing(RuntimeStore(tmp_path)) as store:
        put(store, 'mem_target', title='Falconquasar database owner',
            text='Falconquasar database owner is owneralpha', newer=True)
        for index in range(45):
            put(store, f'mem_noise_{index:03d}', title=f'Historical project reference {index}', text=PREFIX)
        put(store, 'mem_foreign_source', title='Falconquasar database owner', source='codex', newer=True)
        put(store, 'mem_foreign_user', title='Falconquasar database owner',
            scope=replace(SCOPE, user_id='other'), newer=True)
        query = PREFIX + ' Falconquasar'
        request = CandidateRequest.create(query=query, scope=SCOPE, kinds=['memory'],
            source_ids=['hermes'], limit=14, budget=48,
            recall_filters={'_force_anchor_fallback': force_anchor})
        batch = SQLiteCandidateSource(store).search(request)
        with store._lock:
            rows,diagnostics=store.sqlite._candidate_rows(query=query,kinds=['memory'],scope=SCOPE,
                limit=14,recall_filters={'_exact_scope':True,'_source_ids':['hermes'],
                    'candidate_limit':48,'_force_anchor_fallback':force_anchor})
        # Candidate inclusion is distinct from admission. The full original
        # question still determines scoring and may reject a weak tail-only hit.
        assert 'mem_target' in {row['record_id'] for row in rows}
        assert not {'mem_foreign_source','mem_foreign_user'} & {row['record_id'] for row in rows}
        assert not {'mem_foreign_source', 'mem_foreign_user'} & {hit.ref.record_id for hit in batch.hits}
        assert batch.diagnostic_dict()['candidate_count'] <= 48
        assert len(batch.hits) <= 14
        # A term chosen for search is not an exact-title identity certificate.
        assert not any('exact_title' in hit.evidence_hints for hit in batch.hits)


def test_query_term_caps_cover_suffix_and_preserve_short_expressions(tmp_path):
    with closing(RuntimeStore(tmp_path)) as store:
        with store._lock:
            sqlite = store.sqlite
            assert sqlite._fts_query('alpha beta') == '"alpha" OR "beta"'
            for query in (PREFIX + ' Falconquasar', '长期项目配置说明和历史账户规范' * 15 + ' 蓝鹭星'):
                terms = sqlite._candidate_query_terms(query)
                anchors = sqlite._bounded_query_terms(terms, 8)
                assert len(anchors) <= 8 and anchors[-1] == terms[-1]
                expression = sqlite._fts_query(query)
                assert expression.count(' OR ') <= 11
                assert ('falconquasar' if 'Falconquasar' in query else '蓝鹭星') in expression
