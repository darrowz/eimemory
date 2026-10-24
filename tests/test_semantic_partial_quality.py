"""Partial relevance needs bound, blind confirmation before a lower-priority gap."""
import json

import pytest

from eimemory.api.runtime import Runtime
from eimemory.evaluation import semantic_relevance_monitor as monitor
from eimemory.models.records import RecordEnvelope, ScopeRef
from eimemory.retrieval.proactive import ProactiveRecallService
from eimemory.scheduler.jobs import _run_quality_gap_intake
from eimemory.governance.curiosity import generate_learning_goals
from eimemory.governance.self_model import build_self_model
from test_semantic_relevance_monitor import answer, delivery
from test_quality_gap_intake import SCOPE, _register_memory_recall


@pytest.fixture
def runtime(tmp_path):
    value = Runtime.create(root=tmp_path)
    _register_memory_recall(value)
    yield value
    value.close()


def distinct_delivery(runtime):
    first, release = delivery(runtime, defect=None)
    second = RecordEnvelope.create(kind='memory', title='Second item', summary='PRIVATE other evidence',
        source='hermes.memory', source_id='archive', scope=ScopeRef.from_dict(SCOPE))
    runtime.store.append(second)
    render = ProactiveRecallService._render_snapshot_digest(second.title, ProactiveRecallService._record_text(second))
    with runtime.store.locked() as db:
        db.execute("UPDATE proactive_decision_items SET record_id=?,render_digest=? "
                   "WHERE decision_id=? AND citation=?", (second.record_id, render, 'delivery', 'M1'))
        db.conn.commit()
    return first, second, release


def install_reviews(monkeypatch, *, mixed=True, confirmation='agree'):
    calls = []
    def complete(system, user):
        payload = json.loads(user)
        calls.append((system, payload))
        assert set(payload) == {'query', 'items'}  # No first-verdict anchoring.
        if len(calls) > 1 and confirmation == 'unavailable':
            raise monitor.ToolFreeUnavailable('private-error')
        if len(calls) > 1 and confirmation == 'unknown':
            return answer(['unknown'] * len(payload['items']), unanswered='unknown')
        if len(calls) > 1 and confirmation == 'disagree':
            return answer(['relevant'] * len(payload['items']))
        labels = ['unrelated' if mixed and item['title'] == 'Second item' else 'relevant'
                  for item in payload['items']]
        return answer(labels, duplicates=not mixed)
    monkeypatch.setattr(monitor, '_complete_tool_free', complete)
    return calls


@pytest.mark.parametrize('mixed,kind', [(True, 'semantic_mixed'), (False, 'semantic_redundancy')])
def test_partial_quality_is_confirmed_bound_redacted_and_idempotent(runtime, monkeypatch, mixed, kind):
    distinct_delivery(runtime)
    calls = install_reviews(monkeypatch, mixed=mixed)
    result = _run_quality_gap_intake(runtime, scope=SCOPE, reports={})
    assert result['created_count'] == 1
    assert len(calls) == result['semantic_relevance']['provider_calls'] == 2
    assert calls[0][0] != calls[1][0]
    assert calls[1][1]['items'] == list(reversed(calls[0][1]['items']))
    gap = runtime.store.get_by_id(result['created_record_ids'][0], scope=SCOPE)
    observation = gap.content['source_report']['observation']
    assert observation['severity'] == 'moderate'
    assert observation['evidence_kind'] == kind
    confirmation = runtime.store.get_by_id(observation['confirmation_ref'], scope=SCOPE)
    assert confirmation.content['observation_ref'] == gap.content['source_report']['record_id']
    assert confirmation.content['observation_digest'] == observation['report_digest']
    assert 'PRIVATE' not in json.dumps(gap.to_dict()) + json.dumps(confirmation.to_dict())
    again = _run_quality_gap_intake(runtime, scope=SCOPE, reports={})
    assert again['created_count'] == 0
    assert again['semantic_relevance']['provider_calls'] == 0
    assert len(calls) == 2


@pytest.mark.parametrize('confirmation', ['disagree', 'unknown', 'unavailable'])
def test_unconfirmed_partial_quality_does_not_open_gap(runtime, monkeypatch, confirmation):
    distinct_delivery(runtime)
    calls = install_reviews(monkeypatch, confirmation=confirmation)
    result = _run_quality_gap_intake(runtime, scope=SCOPE, reports={})
    assert result['created_count'] == 0
    assert len(calls) == 2
    assert 'private-error' not in json.dumps(result)


def test_confirmation_shares_budget_and_can_resume_from_cached_observation(runtime, monkeypatch):
    distinct_delivery(runtime)
    calls = install_reviews(monkeypatch)
    scope = ScopeRef.from_dict(SCOPE)
    first, findings = monitor.monitor_deliveries(runtime, scope=scope, max_new=1)
    assert not findings and len(calls) == first['provider_calls'] == 1
    second, findings = monitor.monitor_deliveries(runtime, scope=scope, max_new=1)
    assert len(findings) == 1
    assert len(calls) == 2 and second['provider_calls'] == 1


@pytest.mark.parametrize('change', ['render', 'confirmation_digest', 'confirmation_scope'])
def test_changed_evidence_or_confirmation_cannot_reuse_positive_review(runtime, monkeypatch, change):
    first, second, _ = distinct_delivery(runtime)
    install_reviews(monkeypatch)
    _, findings = monitor.monitor_deliveries(runtime, scope=ScopeRef.from_dict(SCOPE))
    assert len(findings) == 1
    confirmation = runtime.store.get_by_id(findings[0]['observation']['confirmation_ref'], scope=SCOPE)
    if change == 'render':
        second.summary = 'changed private evidence'
        runtime.store.rewrite(second)
    elif change == 'confirmation_digest':
        confirmation.content['observation_digest'] = '0' * 64
        runtime.store.rewrite(confirmation)
    else:
        confirmation.scope = ScopeRef.from_dict({**SCOPE, 'user_id': 'other'})
        runtime.store.rewrite(confirmation, previous_scope=ScopeRef.from_dict(SCOPE))
    monkeypatch.setattr(monitor, '_complete_tool_free', lambda *_: pytest.fail('budget is zero'))
    _, findings = monitor.monitor_deliveries(runtime, scope=ScopeRef.from_dict(SCOPE), max_new=0)
    assert not findings


def test_partial_gap_is_a_lower_priority_learning_goal_than_off_topic(runtime, monkeypatch):
    _, _, release = distinct_delivery(runtime)
    install_reviews(monkeypatch)
    mixed = _run_quality_gap_intake(runtime, scope=SCOPE, reports={})
    assert mixed['created_count'] == 1
    mixed_id = mixed['created_record_ids'][0]
    delivery(runtime, query='Entirely different question', defect='unique', decision_id='off-topic', release=release)
    monkeypatch.setattr(monitor, '_complete_tool_free', lambda *_: answer(['unrelated'], unanswered=True))
    severe = _run_quality_gap_intake(runtime, scope=SCOPE, reports={})
    assert severe['created_count'] == 1
    severe_id = severe['created_record_ids'][0]
    goals = generate_learning_goals(build_self_model(runtime, scope=SCOPE, persist=False), [],
        goal_registry={}, thoughts=[], max_goals=20)
    priorities = {record_id: goal['priority'] for goal in goals for record_id in goal.get('source_record_ids', [])}
    assert priorities[mixed_id] < priorities[severe_id]


def prepare_partial_gap(runtime, monkeypatch, *, mixed=True):
    import eimemory.identity as identity
    from eimemory.evaluation.query_input_vault import capture_query_input
    from eimemory.retrieval.query_identity import effective_query_digest

    monkeypatch.setattr(identity, 'HONGTU_AGENT_ID', SCOPE['agent_id'])
    monkeypatch.setenv('EIMEMORY_EVIDENCE_RECEIPT_HMAC_KEY', 'fixture-only-0123456789-abcdefghijklmnopqrstuvwxyz')
    runtime._test_runtime_commit = 'a' * 40
    distinct_delivery(runtime)
    query = 'Where is the secret archive?'
    monkeypatch.setenv('EIMEMORY_CAPTURE_ORIGINAL_QUERY', '1')
    monkeypatch.setenv('EIMEMORY_CAPTURE_QUERY_SCOPES', json.dumps([
        dict(SCOPE, channel='hermes', source_id='archive')]))
    with runtime.store.locked() as db:
        db.execute('UPDATE proactive_decisions SET effective_query_digest=? WHERE decision_id=?',
                   (effective_query_digest('memory.recall', query), 'delivery'))
        db.conn.commit()
    captured = capture_query_input(runtime, decision_id='delivery', query=query, effective_query=query,
        explanation={'task_context': {'source_ids': ['archive'], 'runtime_channel': 'proactive',
                     'task_type': 'memory.recall', 'exact_scope_only': True}}, host_query=query)
    assert captured['status'] == 'captured'
    install_reviews(monkeypatch, mixed=mixed)
    intake = _run_quality_gap_intake(runtime, scope=SCOPE, reports={})
    gap_id = intake['created_record_ids'][0]
    scope = ScopeRef.from_dict(SCOPE)
    prepared = monitor.prepare_semantic_gap_replay(runtime, gap_id=gap_id, scope=scope)
    assert prepared['status'] == 'ready', prepared
    return scope, gap_id


def collect_fixed_repair(runtime, monkeypatch, scope):
    from eimemory.evaluation import semantic_gap_repair as repair
    from eimemory.models.records import RecallBundle

    current = RecordEnvelope.create(kind='memory', title='Fixed answer', summary='PRIVATE fixed answer',
        scope=scope, source='hermes.memory', source_id='archive',
        meta={'force_capture': True, 'memory_type': 'durable_fact'})
    runtime.store.append(current)
    monkeypatch.setattr(runtime.memory, 'recall', lambda **_: RecallBundle(items=[current], rules=[],
        reflections=[], confidence=.9, next_action_hint='', explanation={'retrieval_status': 'evidence_found',
        'relevance_selector': {'collection_complete': True, 'status': 'evidence_found'},
        'engine_diagnostics': {'source_budget_exhausted': 0, 'drops': {}}}))
    monkeypatch.setattr(monitor, '_evaluate', lambda *_: monitor._parse_result(answer(['relevant']), 1))
    replay, findings = repair.collect_semantic_gap_repair_findings(runtime, scope=scope, max_new=2)
    assert len(findings) == 1, json.dumps(replay, sort_keys=True)
    return replay, findings


@pytest.mark.parametrize('mixed', [True, False])
def test_confirmed_partial_gap_uses_existing_exact_parent_repair_flow(runtime, monkeypatch, mixed):
    from eimemory.governance.learning.quality_gap_intake import _ingest_verified_findings
    scope, gap_id = prepare_partial_gap(runtime, monkeypatch, mixed=mixed)
    replay, findings = collect_fixed_repair(runtime, monkeypatch, scope)
    resolved = _ingest_verified_findings(runtime, findings=findings, scope=scope)
    assert len(resolved['resolved_record_ids']) == 1
    resolution = runtime.store.get_by_id(resolved['resolved_record_ids'][0], scope=scope)
    assert resolution.content['resolves_gap_id'] == gap_id
    assert 'PRIVATE' not in json.dumps(resolution.to_dict())
    assert replay['provider_calls'] == replay['replay_count'] == 1


def append_confirmation(runtime, scope, gap_id, *, state):
    from copy import deepcopy
    gap = runtime.store.get_by_id(gap_id, scope=scope)
    ref = gap.content['source_report']['observation']['confirmation_ref']
    old = runtime.store.get_by_id(ref, scope=scope)
    content = deepcopy(old.content)
    if state == 'disagree':
        content['result'] = monitor._parse_result(answer(['relevant', 'relevant']), 2)
    meta = deepcopy(old.meta)
    meta['confirmation_digest'] = monitor._digest(content)
    later = RecordEnvelope.create(kind=old.kind, title=old.title, summary=old.summary,
        source=old.source, content=content, meta=meta, scope=scope,
        status='quarantined' if state == 'quarantined' else 'active')
    runtime.store.append(later)
    return later


@pytest.mark.parametrize('state', ['quarantined', 'disagree'])
@pytest.mark.parametrize('when', ['before_prepare', 'before_persist'])
def test_revoked_confirmation_cannot_authorize_old_gap_repair(runtime, monkeypatch, state, when):
    from eimemory.governance.learning.quality_gap_intake import _ingest_verified_findings
    scope, gap_id = prepare_partial_gap(runtime, monkeypatch)
    if when == 'before_persist':
        _, findings = collect_fixed_repair(runtime, monkeypatch, scope)
    append_confirmation(runtime, scope, gap_id, state=state)
    prepared = monitor.prepare_semantic_gap_replay(runtime, gap_id=gap_id, scope=scope)
    assert prepared['status'] == 'unverifiable'
    assert prepared['reason'] == 'semantic_repair_confirmation_invalid'
    if when == 'before_persist':
        result = _ingest_verified_findings(runtime, findings=findings, scope=scope)
        assert result['resolved_record_ids'] == []
        assert runtime.store.get_by_id(gap_id, scope=scope).status == 'active'


def test_fresh_confirmation_creates_current_gap_and_recovers_repair(runtime, monkeypatch):
    from eimemory.governance.learning.quality_gap_intake import _ingest_verified_findings
    scope, old_id = prepare_partial_gap(runtime, monkeypatch)
    append_confirmation(runtime, scope, old_id, state='quarantined')
    fresh = append_confirmation(runtime, scope, old_id, state='agree')
    _, findings = monitor.monitor_deliveries(runtime, scope=scope, max_new=0)
    assert len(findings) == 1
    result = _ingest_verified_findings(runtime, findings=findings, scope=scope)
    assert len(result['created_record_ids']) == 1
    new_id = result['created_record_ids'][0]
    assert new_id != old_id
    new = runtime.store.get_by_id(new_id, scope=scope)
    assert new.content['source_report']['observation']['confirmation_ref'] == fresh.record_id
    assert monitor.prepare_semantic_gap_replay(runtime, gap_id=old_id, scope=scope)['status'] == 'unverifiable'
    assert monitor.prepare_semantic_gap_replay(runtime, gap_id=new_id, scope=scope)['status'] == 'ready'
    _, repair_findings = collect_fixed_repair(runtime, monkeypatch, scope)
    resolved = _ingest_verified_findings(runtime, findings=repair_findings, scope=scope)
    resolution = runtime.store.get_by_id(resolved['resolved_record_ids'][0], scope=scope)
    assert resolution.content['resolves_gap_id'] == new_id


def test_quarantined_latest_confirmation_does_not_reveal_older_approval(runtime, monkeypatch):
    from copy import deepcopy
    distinct_delivery(runtime)
    install_reviews(monkeypatch)
    scope = ScopeRef.from_dict(SCOPE)
    _, findings = monitor.monitor_deliveries(runtime, scope=scope)
    old = runtime.store.get_by_id(findings[0]['observation']['confirmation_ref'], scope=scope)
    later = RecordEnvelope.create(kind=old.kind, title=old.title, summary=old.summary,
        source=old.source, content=deepcopy(old.content), meta=deepcopy(old.meta), scope=scope,
        status='quarantined')
    runtime.store.append(later)
    monkeypatch.setattr(monitor, '_complete_tool_free', lambda *_: pytest.fail('budget is zero'))
    _, findings = monitor.monitor_deliveries(runtime, scope=scope, max_new=0)
    assert findings == []
