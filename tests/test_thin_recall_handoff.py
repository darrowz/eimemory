"""Real persisted delivery audit -> observation gap -> learning goal, offline only."""
from hashlib import sha256
import json

import pytest

from eimemory.api.runtime import Runtime
from eimemory.governance.curiosity import generate_learning_goals
from eimemory.governance.self_model import build_self_model
from eimemory.scheduler.jobs import _run_quality_gap_intake
from eimemory.models.records import RecordEnvelope, ScopeRef
from test_explicit_recall_capture import _verified_release_fixture
from test_quality_gap_intake import SCOPE, _register_memory_recall


def seed(runtime, *, defect=None, decision_id='delivery', query='Where is the secret archive?', release=None):
    if release is None:
        _, release = _verified_release_fixture(runtime, SCOPE)
    record = RecordEnvelope.create(kind='memory', title='Archive', summary='private answer',
        source='hermes.memory', source_id='archive', scope=ScopeRef.from_dict(SCOPE))
    runtime.store.append(record)
    payload = dict(decision_id=decision_id, channel='hermes', scope=SCOPE,
        source_key=sha256(b'archive').hexdigest(), source_ids=['archive'],
        session_id='session', turn_id=decision_id, query_id=decision_id,
        query_digest=sha256(query.encode()).hexdigest(), task_type='memory.recall',
        policy_version='test', release_identity=release, release_bound=True,
        control_cohort=False, acceptance_generated=False)
    items = [dict(citation=f'M{i}', record_id=record.record_id, source_id='archive',
        confidence=0.9, order=i, state='injected', render_digest='d' * 64) for i in range(2)]
    if defect == 'unverified':
        payload['release_identity'] = {**release, 'deployment_receipt_id': 'missing'}
    elif defect == 'model_only':
        for item in items:
            item['state'] = 'volunteered'
        payload['retrieval_diagnostics'] = {'severe_failure': True, 'verified': True}
    elif defect == 'empty':
        payload['query_digest'] = sha256(b'').hexdigest()
    elif defect == 'no_result':
        items = []
    elif defect == 'cross_scope':
        payload['scope'] = {**SCOPE, 'user_id': 'other'}
    elif defect == 'cross_source':
        items[1]['source_id'] = 'other'
    elif defect == 'record_scope':
        record.scope = ScopeRef.from_dict({**SCOPE, 'user_id': 'other'})
        for item in items:
            item['record_id'] = 'foreign-record'
        record.record_id = 'foreign-record'
        runtime.store.append(record)
    elif defect == 'maintenance':
        payload['acceptance_generated'] = True
    elif defect == 'unbound':
        payload['release_bound'] = False
    elif defect == 'unique':
        items = items[:1]
    runtime.store.record_proactive_decision(payload, items, [])
    return record, release


def test_delivery_failure_to_nonpromoting_learning_goal(tmp_path):
    runtime = Runtime.create(root=tmp_path)
    try:
        _register_memory_recall(runtime)
        record, release = seed(runtime)
        before = record.to_dict()
        status = runtime.evolution.memory_quality_report(scope=SCOPE)['recall_relevance_evolution']
        first = _run_quality_gap_intake(runtime, scope=SCOPE, reports={})
        assert first['created_count'] == 1
        assert status['status'] == 'observation_only'
        gap_id = first['created_record_ids'][0]
        gap = runtime.store.get_by_id(gap_id, scope=SCOPE)
        assert gap.scope == ScopeRef.from_dict(SCOPE)
        assert gap.source_id == 'archive'
        assert gap.content['source_report']['observation']['decision_id'] == 'delivery'
        assert gap.content['source_report']['observation']['release_identity'] == release
        assert gap.content['blocking_metrics']['duplicate_delivered_record']['actual'] == 1
        seed(runtime, decision_id='retry', release=release)
        repeated = _run_quality_gap_intake(runtime, scope=SCOPE, reports={})
        assert repeated['created_count'] == 0
        model = build_self_model(runtime, scope=SCOPE, persist=False)
        goals = generate_learning_goals(model, [], goal_registry={}, thoughts=[], max_goals=20)
        assert any(gap_id in goal.get('source_record_ids', []) for goal in goals)
        assert first['mutation_boundary']['observation_records_only'] is True
        assert runtime.store.get_by_id(record.record_id).to_dict() == before
        assert not runtime.store.list_records(kinds=['capability_hypothesis'], scope=SCOPE, limit=50)
        assert 'secret archive' not in json.dumps(first)
        assert 'private answer' not in json.dumps(gap.to_dict())
        seed(runtime, decision_id='different-query', query='Where is another archive?', release=release)
        assert _run_quality_gap_intake(runtime, scope=SCOPE, reports={})['created_count'] == 1
        _, next_release = _verified_release_fixture(runtime, SCOPE)
        seed(runtime, decision_id='next-release', release=next_release)
        assert _run_quality_gap_intake(runtime, scope=SCOPE, reports={})['created_count'] == 1
    finally:
        runtime.close()


@pytest.mark.parametrize('defect', ['unverified', 'model_only', 'empty', 'no_result', 'cross_scope',
                                    'cross_source', 'record_scope', 'maintenance', 'unbound', 'unique'])
def test_incomplete_delivery_evidence_stays_diagnostic(tmp_path, defect):
    runtime = Runtime.create(root=tmp_path)
    try:
        seed(runtime, defect=defect)
        report = _run_quality_gap_intake(runtime, scope=SCOPE, reports={
            'recall_delivery:model_claim': {'target_capability': 'memory.recall',
                'verified': True, 'severity': 'severe', 'quality_gate': {'ok': False}},
        })
        assert report['created_count'] == 0
    finally:
        runtime.close()
