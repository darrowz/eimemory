from contextlib import closing
from dataclasses import asdict, replace

import pytest

from eimemory.api.memory import MemoryAPI
from eimemory.models.records import RecordEnvelope, ScopeRef
from eimemory.retrieval.contracts import CandidateBatch, CandidateHit, CandidateRef, ExactScope
from eimemory.retrieval.engine import GovernedRecallEngine
from eimemory.retrieval.lightweight_admission import LightweightAdmission, LightweightConfig
from eimemory.storage.runtime_store import RuntimeStore


def test_sqlite_stops_scoring_at_collection_deadline(tmp_path, monkeypatch):
    from eimemory.storage import sqlite_store
    clock = [1.0]
    # No production-time sleep: model the expensive lexical stage only.
    monkeypatch.setattr(sqlite_store, 'perf_counter', lambda: clock[0], raising=False)
    original = sqlite_store.analyze_lexical_signal
    calls = []
    def timed_lexical(*args, **kwargs):
        calls.append(1)
        clock[0] += .3
        return original(*args, **kwargs)
    scope = ScopeRef(agent_id='agent', workspace_id='workspace', user_id='owner')
    with closing(RuntimeStore(tmp_path)) as store:
        for index in range(8):
            store.append(RecordEnvelope.create(kind='memory', title=f'alpha item {index}',
                summary='alpha distinct facts', content={'text': f'alpha fact {index}'}, scope=scope))
        monkeypatch.setattr(sqlite_store, 'analyze_lexical_signal', timed_lexical)
        items, report = store.search_with_diagnostics(query='alpha', kinds=['memory'], scope=scope, limit=8,
            recall_filters={'_exact_scope': True, '_recall_collection_deadline_monotonic': 1.5})
        assert len(calls) == 2
        assert len(items) == 2
        assert report['blocked_counts']['candidate_scoring_timeout'] == 6


@pytest.mark.parametrize('mode', ['', 'structured', 'fast'])
def test_collection_cutoff_keeps_time_to_validate_collected_identity(tmp_path, monkeypatch, mode):
    clock = [1.0]
    monkeypatch.setattr('eimemory.retrieval.engine.perf_counter', lambda: clock[0])
    monkeypatch.setattr('eimemory.retrieval.lightweight_admission.perf_counter', lambda: clock[0])
    scope = ScopeRef(agent_id='agent', workspace_id='workspace', user_id='owner')
    alternate = replace(scope, user_id='alias')
    monkeypatch.setattr('eimemory.retrieval.engine.hongtu_query_scopes_with_aliases', lambda *a, **k: [scope, alternate])
    monkeypatch.setattr('eimemory.retrieval.engine.hongtu_query_scopes', lambda scope: [scope])
    with closing(RuntimeStore(tmp_path)) as store:
        item = store.append(RecordEnvelope.create(kind='memory', title='specific durable fact',
            summary='specific durable fact', content={'text': 'specific durable fact'}, scope=scope))

        class SlowSource:
            name = 'bounded_test_source'
            calls = 0

            def search(self, request):
                self.calls += 1
                clock[0] += 2.4
                return CandidateBatch(hits=(CandidateHit(
                    CandidateRef(item.record_id, ExactScope.from_scope(scope), item.source_id),
                    1, 1.0, evidence_hints=('exact_title',)),))

        source = SlowSource()
        engine = GovernedRecallEngine(store=store, candidate_source=source)
        engine.relevance_admission = LightweightAdmission(LightweightConfig(enabled=True))
        memory = MemoryAPI(store, recall_engine=engine)
        monkeypatch.setattr(memory, '_memory_usage_adjustments',
            lambda *a, **k: pytest.fail('optional feedback used validation reserve'))
        bundle = memory.recall(query=item.title, scope=asdict(scope), limit=2,
            task_context={'recall_mode': mode})
        assert source.calls == 1
        assert [result.record_id for result in bundle.items] == [item.record_id]
        assert bundle.explanation['engine_diagnostics']['drops']['recall_budget_exhausted'] >= 1
