import pytest
from dataclasses import asdict
from eimemory.api.runtime import Runtime
from eimemory.models.records import RecordEnvelope, ScopeRef
from eimemory.scheduler.jobs import _production_recall_smoke_dataset


@pytest.mark.parametrize('content,meta,source', [
    ({'page_type': 'digest'}, {}, 'test'),
    ({'page_type': 'synthesis'}, {}, 'test'),
    ({}, {'page_type': 'digest'}, 'test'),
    ({}, {}, 'eimemory.knowledge.synthesis'),
])
def test_smoke_digest_target_has_explicit_view_without_changing_default(tmp_path, content, meta, source):
    runtime = Runtime.create(root=tmp_path)
    scope = ScopeRef(agent_id='hongtu', workspace_id='embodied', user_id='darrow')
    try:
        digest = runtime.store.append(RecordEnvelope.create(
            kind='knowledge_page', title='Research digest',
            summary='Recent papers: deterministic testing and reliable retrieval.',
            content=content, meta=meta, source=source, scope=scope))
        plain = runtime.store.append(RecordEnvelope.create(
            kind='memory', title='User preference', summary='Prefer concise answers with cited evidence.',
            meta={'memory_type':'preference'}, source='operator.preference', scope=scope))
        dataset = _production_recall_smoke_dataset(runtime, scope=asdict(scope))
        cases = {c['expected_record_ids'][0]:c for c in dataset['cases']}
        assert set(cases) == {digest.record_id, plain.record_id}
        assert digest.record_id in cases  # Do not remove the failing sample.
        context = cases[digest.record_id]['task_context']
        assert context.get('include_digest_pages') is True
        assert not cases[plain.record_id]['task_context'].get('include_digest_pages')
        assert runtime.memory._is_default_recall_suppressed_record(digest, {})
        assert not runtime.memory._is_default_recall_suppressed_record(digest, context)
        bundle = runtime.memory.recall(query=cases[digest.record_id]['query'],
            scope=asdict(scope), task_context=context, limit=5)
        assert digest.record_id in [item.record_id for item in bundle.items]
    finally:
        runtime.close()


def test_explicit_kind_keeps_knowledge_page_when_intent_would_suppress_it(tmp_path):
    runtime = Runtime.create(root=tmp_path)
    scope = ScopeRef(agent_id='hongtu', workspace_id='embodied', user_id='darrow')
    try:
        page = runtime.store.append(RecordEnvelope.create(
            kind='knowledge_page', title='Delivery note',
            summary='海报要求必须同时写清交付范围和验收标准。',
            content={'page_type': 'topic'},
            source='eimemory.knowledge.compiler', source_id='default', scope=scope))
        context = {
            'exact_scope_only': True,
            'source_ids': ['default'],
            'kinds': ['memory', 'multimodal_memory', 'knowledge_page', 'claim_card'],
        }
        blocked = runtime.memory._record_recall_filter_block_reason(page, {
            'suppressed_kinds': ['knowledge_page', 'news'],
            'intent_name': 'project_delivery',
        })
        kept = runtime.memory._record_recall_filter_block_reason(
            page,
            runtime.memory._recall_filters_from_task_context(context) | {
                'suppressed_kinds': ['knowledge_page', 'news'],
                'intent_name': 'project_delivery',
            },
        )
        bundle = runtime.memory.recall(
            query=page.summary[:160], scope=asdict(scope), task_context=context, limit=5)
        assert blocked == 'intent_kind:suppressed'
        assert kept == ''
        assert page.record_id in [item.record_id for item in bundle.items]
        preference = runtime.store.append(RecordEnvelope.create(
            kind='knowledge_page', title='Preference note',
            summary='用户偏好熟悉内容，也会犹豫是否探索新颖内容。',
            content={'page_type': 'topic'},
            source='eimemory.knowledge.compiler', source_id='default', scope=scope))
        preference_bundle = runtime.memory.recall(
            query=preference.summary[:160], scope=asdict(scope), task_context=context, limit=5)
        assert preference.record_id in [item.record_id for item in preference_bundle.items]
    finally:
        runtime.close()
