import json

import pytest

from eimemory.api.runtime import Runtime
from eimemory.models.records import RecallBundle
from eimemory.retrieval.proactive import ProactiveRecallService
from eimemory.evaluation.query_input_vault import load_query_input
from eimemory.evaluation.production_query_dataset import (
    collect_pending_production_queries, pending_production_query_capture_validation_error,
)
from test_query_input_vault import BASE
from eimemory.adapters.runtime.channel import resolve_channel_scope


def test_stage_diagnostics_survive_raw_capture_without_text(tmp_path, monkeypatch):
    monkeypatch.setenv('EIMEMORY_CAPTURE_ORIGINAL_QUERY', '1')
    runtime = Runtime.create(root=tmp_path)
    try:
        service = ProactiveRecallService(runtime, control_percent=0, release_identity={
            'release_commit':'a'*40, 'release_version':'1.0',
            'deployment_receipt_id':'receipt', 'release_session_id':'release'})
        bundle = RecallBundle(items=[], rules=[], reflections=[], confidence=0, next_action_hint="", explanation={
            'retrieval_status':'unavailable',
            'engine_diagnostics': {'source_names':['PostgresVectorCandidateSource'],
                'candidate_count':22, 'elapsed_ms':9800, 'drops':{}, 'query':'SECRET'},
            'relevance_selector': {'candidate_count':21, 'selected_count':0,
                'status':'unavailable', 'elapsed_ms':7400,
                'dropped_reasons':{'evidence_score_gap':20},
                'caller_assistance':{'status':'unavailable', 'calls':1,
                    'reason':'caller_verification_failed', 'error_type':'GatewayCompletionError',
                    'error_reason':'timeout', 'text':'SECRET'},
                'scored':[{'text':'SECRET'}]},
        })
        monkeypatch.setattr(service, '_recall_with_timeout', lambda **_: bundle)
        result = service.decide(channel='codex', scope=BASE, source_ids=['codex'],
            session_id='session', query_id='turn', query='Review recall failure', task_type='code.task')
        stored = runtime.store.sqlite.load_proactive_decision(result['decision_id'])
        diagnostic = stored['retrieval_diagnostics']
        assert diagnostic['engine']['candidate_count'] == 22
        assert diagnostic['selector']['dropped_reasons'] == {'evidence_score_gap':20}
        assert diagnostic['assistance']['error_reason'] == 'timeout'
        assert 'SECRET' not in json.dumps(diagnostic)
        captured = load_query_input(runtime, decision_id=result['decision_id'],
            scope=resolve_channel_scope('codex', BASE), channel='codex', source_id='codex')
        assert captured['retrieval_diagnostics'] == diagnostic
    finally:
        runtime.close()


def test_exact_maintenance_projection_cannot_become_natural_gold(tmp_path):
    runtime = Runtime.create(root=tmp_path)
    try:
        service = ProactiveRecallService(runtime, control_percent=0, release_identity={
            'release_commit':'a'*40, 'release_version':'1.0',
            'deployment_receipt_id':'receipt', 'release_session_id':'release'})
        result = service.decide(channel='codex', scope=BASE, source_ids=['codex'],
            session_id='session', query_id='turn', query='Review recall failure',
            task_type='code.task', acceptance_generated=True, recall_bundle=RecallBundle(items=[], rules=[], reflections=[], confidence=0, next_action_hint=""))
        assert collect_pending_production_queries(runtime, scope=BASE)['pending_record_ids'] == []
        args = dict(scope=BASE, channel='codex', decision_id=result['decision_id'], include_maintenance=True)
        first = collect_pending_production_queries(runtime, **args)
        second = collect_pending_production_queries(runtime, **args)
        assert len(first['pending_record_ids']) == 1
        assert second['pending_record_ids'] == first['pending_record_ids']
        assert first['explicit']['status'] == 'not_requested'
        pending = runtime.store.get_by_id(first['pending_record_ids'][0])
        assert pending.content['acceptance_generated'] is True
        assert pending_production_query_capture_validation_error(runtime, pending,
            exact_scope=pending.scope, channel='codex') == 'maintenance_capture_not_natural'
        assert collect_pending_production_queries(runtime, **{**args, 'channel':'hermes'})['pending_record_ids'] == []
        with pytest.raises(ValueError, match='exact channel'):
            collect_pending_production_queries(runtime, scope=BASE, decision_id=result['decision_id'])
    finally:
        runtime.close()


def test_codex_host_marks_deliberate_maintenance_from_process_env(monkeypatch):
    from test_codex_adapter import FakeClient
    from eimemory.adapters.codex.hook import CodexHookAdapter
    monkeypatch.setenv('EIMEMORY_ACCEPTANCE_GENERATED', '1')
    client = FakeClient()
    adapter = CodexHookAdapter(client=client, scope=BASE)
    adapter.handle('UserPromptSubmit', {'session_id':'s', 'turn_id':'t', 'prompt':'Review recall failure'})
    assert client.calls[0][1]['acceptance_generated'] is True


def test_proactive_injects_selected_deep_evidence_and_rehydrates_it(tmp_path, monkeypatch):
    from eimemory.models.records import RecordEnvelope, ScopeRef
    from eimemory.retrieval.evidence_fragments import evidence_fragments
    from eimemory.retrieval.postgres_vector import candidate_record_keyword_text
    runtime = Runtime.create(root=tmp_path)
    try:
        record = RecordEnvelope.create(kind='memory', title='Previous release review',
            content={'text': ('Unrelated earlier discussion.\n' * 80) +
                'Deployment must use the immutable installer and preserve automatic rollback.'},
            source='codex.memory', source_id='codex',
            scope=ScopeRef.from_dict(resolve_channel_scope('codex', BASE)), meta={'force_capture':True})
        runtime.store.append(record)
        fragment = next(f for f in evidence_fragments(candidate_record_keyword_text(record, max_text_chars=16000))
                        if 'immutable installer' in f['text'])
        bundle = RecallBundle(items=[record], rules=[], reflections=[], confidence=.95, next_action_hint='',
            explanation={'retrieval_status':'evidence_found','relevance_selector':{'scored':[
                {'record_id':record.record_id,'source_id':'codex','fragment_id':fragment['id'],
                 'projection_text_chars':16000}]}})
        service = ProactiveRecallService(runtime, control_percent=0, release_identity={
            'release_commit':'a'*40, 'release_version':'1.0',
            'deployment_receipt_id':'receipt', 'release_session_id':'release'})
        monkeypatch.setattr(service, '_recall_with_timeout', lambda **_: bundle)
        args = dict(channel='codex', scope=BASE, source_ids=['codex'], session_id='s', query_id='t',
            query='Recall the previous requirements for safely deploying the repair', task_type='code.task')
        result = service.decide(**args)
        assert 'immutable installer' in result['context']
        assert result['context'] == service.decide(**args)['context']
        assert result['items'][0]['text'] == fragment['text'].strip()
        with pytest.raises(ValueError, match='identity conflict'):
            service.decide(**{**args,'acceptance_generated':True})
    finally:
        runtime.close()


def test_outer_recall_failure_keeps_raw_query_and_safe_failure_stage(tmp_path, monkeypatch):
    monkeypatch.setenv('EIMEMORY_CAPTURE_ORIGINAL_QUERY', '1')
    runtime = Runtime.create(root=tmp_path)
    try:
        service = ProactiveRecallService(runtime, control_percent=0, release_identity={
            'release_commit':'a'*40, 'release_version':'1.0',
            'deployment_receipt_id':'receipt', 'release_session_id':'release'})
        def failed(**_):
            raise TimeoutError('proactive recall timed out')
        monkeypatch.setattr(service, '_recall_with_timeout', failed)
        result = service.decide(channel='codex', scope=BASE, source_ids=['codex'],
            session_id='s', query_id='t', query='Review previous release requirements')
        assert result['decision_id']
        captured = load_query_input(runtime, decision_id=result['decision_id'],
            scope=resolve_channel_scope('codex', BASE), channel='codex', source_id='codex')
        assert captured['retrieval_status'] == 'unavailable'
        assert captured['retrieval_diagnostics']['engine']['reason'] == 'recall_deadline_exceeded'
        assert result['context'] == ''
    finally:
        runtime.close()


def test_legacy_capture_provenance_is_unknown_not_relabelled_natural(tmp_path):
    from test_query_input_vault import seed
    runtime = Runtime.create(root=tmp_path)
    seed(runtime)
    runtime.store.sqlite.conn.execute('ALTER TABLE proactive_decisions DROP COLUMN acceptance_generated')
    runtime.store.sqlite.conn.commit()
    runtime.close()
    runtime = Runtime.create(root=tmp_path)
    try:
        assert runtime.store.sqlite.load_proactive_decision('decision')['acceptance_generated'] is None
        assert collect_pending_production_queries(runtime, scope=BASE)['pending_record_ids'] == []
    finally:
        runtime.close()
