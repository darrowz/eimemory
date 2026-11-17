"""Selected registered knowledge must still be authorized at final delivery."""
from copy import deepcopy
from dataclasses import asdict
import json
from time import perf_counter

import pytest

from eimemory.adapters.runtime.service import AgentRuntimeMemoryService
from eimemory.api.runtime import Runtime
from eimemory.knowledge.evidence_gate import current_evidence_reasons
from eimemory.models.records import RecallBundle, RecordEnvelope, ScopeRef
from eimemory.recall.loadout import render_loadout
from eimemory.recall.raw_citation import RawCitationLane
from eimemory.storage.runtime_store import RuntimeStore


SCOPE = ScopeRef('source-final', 'reader', 'isolated', 'owner')
SOURCE = {'source_id': 'orchid-docs', 'source_kind': 'url', 'title': 'Orchid Guide',
          'uri': 'https://docs.example.test/orchid', 'metadata': {
              'connector_id': 'fixture.local', 'knowledge_source_kind': 'official_docs', 'trust': 1.0}}
TEXT = 'The Orchid relay stores completed batches in the amber spool.'


@pytest.fixture
def material(tmp_path):
    with Runtime(RuntimeStore(tmp_path), profile='core') as runtime:
        runtime.sources.add_source(SOURCE)
        report = runtime.ingest_knowledge_source(
            {'source_id': SOURCE['source_id'], 'source_kind': 'official_docs',
             'title': SOURCE['title'], 'uri': SOURCE['uri'], 'text': TEXT},
            scope=asdict(SCOPE), connector_id='fixture.local', persist=True)
        record = runtime.store.get_by_exact_ref(report['persisted_record_ids'][0],
            scope=SCOPE, source_id='default')
        assert record is not None and record.kind == 'knowledge_unit'
        assert record.source_id != record.content['source_id'] == SOURCE['source_id']
        kept, drops = runtime.memory._apply_online_recall_pollution_gate([record], allow_operational_recall=True)
        assert kept == [record] and not drops
        yield runtime, record


def selected(record):
    # Every prebuilt body-bearing lane must disappear on final rejection.
    return RecallBundle([record], [], [], 0.9, TEXT,
        explanation={'retrieval_status': 'evidence_found', 'diagnostic_excerpt': TEXT},
        raw_citation=RawCitationLane(evidence=[{'text': TEXT}]))


def final(runtime, record, *, deadline=None):
    bundle = selected(record)
    result = runtime.memory._final_current_evidence_admission(bundle,
        deadline_at=perf_counter() + 2 if deadline is None else deadline)
    return bundle, result


def assert_withheld(result):
    assert not result.items and not result.rules and not result.reflections
    assert not result.next_action_hint and result.raw_citation is None
    assert result.explanation['retrieval_status'] == 'unavailable'
    assert TEXT not in json.dumps(result.to_dict())
    compact = AgentRuntimeMemoryService._assemble_recall_bundle(result, limit=8)
    assert compact['retrieval_status'] == 'unavailable'
    assert TEXT not in render_loadout(compact, max_chars=7200)


@pytest.mark.parametrize('change', ['disable', 'delete', 'trust', 'uri', 'connector', 'kind'])
def test_final_delivery_rechecks_existing_registry_binding(material, change):
    runtime, record = material
    original, initial = final(runtime, record)
    assert initial is original
    source = deepcopy(SOURCE)
    if change == 'delete':
        runtime.sources.path.unlink()
    else:
        if change == 'disable': source['enabled'] = False
        elif change == 'trust': source['metadata']['trust'] = 0.55
        elif change == 'uri': source['uri'] = 'https://docs.example.test/other'
        elif change == 'connector': source['metadata']['connector_id'] = 'different.connector'
        elif change == 'kind': source['metadata']['knowledge_source_kind'] = 'blog'
        runtime.sources.add_source(source)
    old, result = final(runtime, record)
    assert_withheld(result)
    assert old.items == [record] and old.next_action_hint == TEXT
    assert old.raw_citation is not None  # no mutation of caller-owned input


@pytest.mark.parametrize('change', ['none', 'unrelated', 'display_title'])
def test_authorized_selected_source_is_not_rejected_by_unrelated_changes(material, change):
    runtime, record = material
    if change == 'unrelated':
        runtime.sources.add_source({**SOURCE, 'source_id': 'unrelated', 'enabled': False})
    elif change == 'display_title':
        runtime.sources.add_source({**SOURCE, 'title': 'A different display label'})
    original, result = final(runtime, record)
    assert result is original and result.items == [record]


@pytest.mark.parametrize('error', [OSError, ValueError])
def test_final_registry_read_failure_is_unavailable(material, monkeypatch, error):
    runtime, record = material
    def unavailable():
        raise error('fixture registry authority unavailable')
    monkeypatch.setattr(runtime.sources, 'list_sources', unavailable)
    _, result = final(runtime, record)
    assert_withheld(result)
    assert result.explanation['current_evidence']['blocked_count'] == 1


@pytest.mark.parametrize('when', ['before', 'during_registry_read'])
def test_final_registry_check_cannot_extend_the_existing_deadline(material, monkeypatch, when):
    import eimemory.knowledge.current_evidence as module
    runtime, record = material
    deadline = perf_counter() + 2
    if when == 'before':
        deadline = perf_counter() - 1
    else:
        original = runtime.sources.list_sources
        def late():
            sources = original()
            monkeypatch.setattr(module, 'perf_counter', lambda: deadline + 1)
            return sources
        monkeypatch.setattr(runtime.sources, 'list_sources', late)
    _, result = final(runtime, record, deadline=deadline)
    assert_withheld(result)


def test_ordinary_durable_memory_does_not_acquire_registry_dependency(material, monkeypatch):
    runtime, _ = material
    record = runtime.store.append(RecordEnvelope.create(kind='memory', title='Operator preference',
        summary='Prefer concise summaries.', source='fixture.manual', scope=SCOPE))
    monkeypatch.setattr(runtime.sources, 'list_sources', lambda: pytest.fail('ordinary memory read registry'))
    original, result = final(runtime, record)
    assert result is original and result.items == [record]


def test_non_recall_evidence_consumers_keep_the_existing_opt_in_boundary(material):
    runtime, record = material
    runtime.sources.add_source({**SOURCE, 'enabled': False})
    assert current_evidence_reasons([record], store=runtime.store) == ['']


@pytest.mark.parametrize('revoked', [False, True])
def test_native_persisted_brief_rechecks_its_registered_parent(material, revoked):
    runtime, record = material
    # Ordinary knowledge units do not contribute to a brief unless they belong
    # to a supported body section. Use the native followup producer contract.
    followup = runtime.ingest_knowledge_source(
        {'source_id': SOURCE['source_id'], 'source_kind': 'official_docs',
         'title': SOURCE['title'], 'uri': SOURCE['uri'], 'text': 'Follow up: audit ' + TEXT},
        scope=asdict(SCOPE), connector_id='fixture.local', persist=True)
    report = runtime.build_daily_brief(scope=asdict(SCOPE), date=record.time.occurred_at[:10], persist=True)
    assert report['persisted'] is True
    assert report['followups']
    assert set(followup['persisted_record_ids']) & {ref['record_id'] for ref in report['source_record_refs']}
    brief = runtime.store.get_by_exact_ref(report['persisted_record_id'], scope=SCOPE, source_id='default')
    original, before = final(runtime, brief)
    assert before is original
    if revoked:
        runtime.sources.add_source({**SOURCE, 'enabled': False})
    original, after = final(runtime, brief)
    if revoked:
        assert_withheld(after)
        assert brief.record_id not in render_loadout(
            AgentRuntimeMemoryService._assemble_recall_bundle(after, limit=8), max_chars=7200)
    else:
        assert after is original and after.items == [brief]


def test_revocation_after_real_selection_is_enforced_in_adapter_context(material, monkeypatch):
    runtime, record = material
    service = AgentRuntimeMemoryService(runtime)
    context = {'kinds': ['knowledge_unit'], 'exact_scope_only': True,
        'allowed_recall_lanes': ['external_knowledge'], 'include_operational_recall': True,
        'rerank_with_llm': False, 'enable_llm_rerank': False}
    def recall():
        return service.prefetch(channel='openclaw', scope=asdict(SCOPE), query=TEXT, limit=8, task_context=context)
    initial = recall()
    assert initial['bundle']['retrieval_status'] == 'evidence_found'
    initial_ids = {item['record_id'] for item in initial['bundle']['items']}
    assert initial_ids and TEXT in initial['context']
    assert any(record_id in initial['context'] for record_id in initial_ids)
    selector = runtime.memory.recall_engine._select_post_fusion_items
    selected_ids = []
    def withdraw(items, **kwargs):
        selected_items, report = selector(items, **kwargs)
        selected_ids.extend(item.record_id for item in selected_items)
        runtime.sources.add_source({**SOURCE, 'enabled': False})
        return selected_items, report
    monkeypatch.setattr(runtime.memory.recall_engine, '_select_post_fusion_items', withdraw)
    result = recall()
    assert initial_ids & set(selected_ids)
    assert result['bundle']['retrieval_status'] == 'unavailable'
    assert not result['bundle']['items']
    assert TEXT not in json.dumps(result)
    assert all(record_id not in result['context'] for record_id in initial_ids)
