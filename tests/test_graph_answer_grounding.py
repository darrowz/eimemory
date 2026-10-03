"""Graph reachability must pass the shared answer grounding contract."""
from types import SimpleNamespace

import pytest

from eimemory.models.records import RecordEnvelope, ScopeRef
from eimemory.retrieval.engine import GovernedRecallEngine
from eimemory.retrieval.sqlite_source import SQLiteCandidateSource
from eimemory.storage.runtime_store import RuntimeStore


@pytest.mark.parametrize('edge_type,confidence,query,expected', [
    ('entity', .9, 'Which dependency supports Atlas?', False),
    ('semantic', .9, 'Which dependency supports Atlas?', False),
    ('depends_on', .8, 'Which dependency supports Atlas?', True),
    ('depends_on', .49, 'Which dependency supports Atlas?', False),
    ('depends_on', .8, 'How much does Atlas cost?', False),
])
def test_graph_expansion_requires_answer_evidence(tmp_path, monkeypatch, edge_type, confidence, query, expected):
    monkeypatch.setenv('EIMEMORY_LIGHTWEIGHT_ADMISSION_ENABLED', '0')
    store = RuntimeStore(tmp_path)
    engine = GovernedRecallEngine(store=store, candidate_source=SQLiteCandidateSource(store))
    item = RecordEnvelope.create(kind='memory', title='Ceramic catalog',
        summary='Porcelain glaze inventory.', scope=ScopeRef())
    store.append(item)
    ref = engine._record_key(item)
    selected, _ = engine._select_post_fusion_items([item], query=query, limit=5,
        fusion_state={'evidence_by_ref': {ref: {'graph_path'}}, 'graph_expanded_refs': {ref}},
        component_hints_by_ref={}, graph_edge_refs=[SimpleNamespace(
            edge_type=edge_type, confidence=confidence, from_id='atlas', to_id=item.record_id)])
    assert bool(selected) is expected


def test_real_causal_graph_recall_preserves_related_answer(tmp_path, monkeypatch):
    from eimemory.api.runtime import Runtime
    from eimemory.models.memory_edges import MemoryEdge
    monkeypatch.setenv('EIMEMORY_POSTGRES_VECTOR_ENABLED', '0')
    monkeypatch.setenv('EIMEMORY_LIGHTWEIGHT_ADMISSION_ENABLED', '0')
    runtime = Runtime.create(root=tmp_path, profile='core')
    scope = ScopeRef(user_id='graph-positive')
    cause = RecordEnvelope.create(kind='memory', title='Ceramic catalog',
        summary='Porcelain glaze inventory.', scope=scope)
    symptom = RecordEnvelope.create(kind='memory', title='Atlas failure',
        summary='Atlas failure during startup.', scope=scope)
    try:
        runtime.store.append(cause)
        runtime.store.append(symptom)
        runtime.store.upsert_memory_edge(MemoryEdge.create(from_id=cause.record_id,
            to_id=symptom.record_id, edge_type='causal', confidence=.82,
            evidence_id=symptom.record_id, scope=scope, reason='explicit_causal_reference'))
        result = runtime.memory.recall(query='why Atlas failure',
            scope={'user_id': scope.user_id}, task_context={'exact_scope_only': True}, limit=5)
        assert cause.record_id in [item.record_id for item in result.items]
        assert result.explanation['graph_expanded'] >= 1
    finally:
        runtime.close()
