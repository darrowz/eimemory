"""Historical authority preparation only: no live model, recall, or repair credit."""
from dataclasses import asdict
from hashlib import sha256
import json

import pytest

from eimemory.api.runtime import Runtime
from eimemory.models.records import RecordEnvelope, ScopeRef
from eimemory.evaluation import semantic_relevance_monitor as monitor
from eimemory.evaluation.query_input_vault import capture_query_input
from eimemory.governance.learning.quality_gap_intake import _gap_record, _quality_finding
from eimemory.retrieval.proactive import ProactiveRecallService
from eimemory.retrieval.query_identity import effective_query_digest
from test_explicit_recall_capture import _verified_release_fixture


@pytest.fixture
def incident(tmp_path, monkeypatch):
    import eimemory.identity as identity
    monkeypatch.setattr(identity, 'HONGTU_AGENT_ID', 'current-agent')
    monkeypatch.setenv('EIMEMORY_LEGACY_AGENT_IDS', 'former-agent')
    exact = ScopeRef('default', 'current-agent', 'workspace::channel::hermes', 'owner')
    original = dict(asdict(exact), agent_id='former-agent')
    monkeypatch.setenv('EIMEMORY_EVIDENCE_RECEIPT_HMAC_KEY', 'fixture-only-0123456789-abcdefghijklmnopqrstuvwxyz')
    monkeypatch.setenv('EIMEMORY_CAPTURE_ORIGINAL_QUERY', '1')
    monkeypatch.setenv('EIMEMORY_CAPTURE_QUERY_SCOPES', json.dumps([
        dict(owner, channel='hermes', source_id='hermes') for owner in (asdict(exact), original)]))
    runtime = Runtime.create(root=tmp_path)
    runtime._test_runtime_commit = 'a' * 40
    _, release = _verified_release_fixture(runtime, asdict(exact))
    query = 'PRIVATE original user question'
    record = RecordEnvelope.create(kind='memory', title='Old returned item', summary='PRIVATE unrelated old item',
        source='hermes.memory', source_id='hermes', scope=exact)
    runtime.store.append(record)
    decision_id = 'pd:' + '1' * 32
    render = ProactiveRecallService._render_snapshot_digest(record.title, ProactiveRecallService._record_text(record))
    payload = dict(decision_id=decision_id, channel='hermes', scope=original,
        source_key='source', source_ids=['hermes'], session_id='original-session', turn_id='turn', query_id='turn',
        query_digest=sha256(query.encode()).hexdigest(), effective_query_digest=effective_query_digest('research.task', query),
        task_type='research.task', policy_version='fixture', release_identity=release,
        release_bound=True, control_cohort=False, acceptance_generated=False)
    runtime.store.record_proactive_decision(payload, [dict(citation='M1', record_id=record.record_id,
        source_id='hermes', confidence=.9, order=0, state='injected', render_digest=render)], [])
    captured = capture_query_input(runtime, decision_id=decision_id, query=query, effective_query=query,
        explanation={'task_context': {'source_ids': ['hermes'], 'runtime_channel': 'proactive',
                     'task_type': 'research.task', 'exact_scope_only': True}}, host_query=query)
    assert captured['status'] == 'captured'
    observation = dict(version=monitor.VERSION, scope=original, release_digest=monitor._digest(release),
        query_digest=payload['query_digest'], source_digests=[monitor._digest('hermes')],
        decision_digest=monitor._digest(decision_id), record_digests=[monitor._digest(record.record_id)],
        decision_surface='research.task', channel='hermes', release_identity=release)
    observation['evaluation_identity'] = monitor._digest(dict(version=monitor.VERSION, decision_id=decision_id,
        scope=original, source_ids=['hermes'], query_digest=payload['query_digest'], release_identity=release,
        delivered=[(record.record_id, 'hermes', render)]))
    body = dict(observation, verdict='off_topic', reason='evaluated', relevance=['unrelated'],
                off_topic=True, duplicates=False, unanswered=True)
    old = RecordEnvelope.create(kind='evaluation_packet', title='Semantic relevance observation', scope=exact,
        source=monitor.SOURCE, content=body, meta={'report_type': monitor.VERSION,
        'semantic_monitor_digest': monitor._digest(body), 'semantic_monitor_identity': observation['evaluation_identity']})
    runtime.store.append(old)
    owner = dict(scope=original, source_id='hermes', query_digest=payload['query_digest'],
                 release_identity=release, decision_surface='research.task')
    finding = _quality_finding('recall_semantic:' + monitor._digest(owner), dict(target_capability='memory.recall',
        report_type=monitor.VERSION, record_id=old.record_id, sample_count=1,
        quality_gate={'ok': False, 'blocked_reason': 'confirmed_semantic_off_topic',
        'blocking_metrics': {'semantic_off_topic': {'actual': 1, 'threshold': 0, 'operator': '=='}}}))
    finding['observation'] = dict(owner, version=monitor.VERSION, severity='severe',
        evidence_kind='semantic_off_topic', report_digest=monitor._digest(body))
    gap = _gap_record(finding, scope=exact, supersedes=None)
    runtime.store.append(gap)
    monkeypatch.setattr(monitor, '_evaluate', lambda *_: pytest.fail('preparation called judge'))
    monkeypatch.setattr(runtime.memory, 'recall', lambda **_: pytest.fail('preparation called recall'))
    yield runtime, exact, gap, old, record, decision_id
    runtime.close()


def test_monitor_revisits_renamed_decision_without_inventing_delivery(incident):
    runtime, scope, gap, old, record, _ = incident
    before = [runtime.store.get_by_id(item.record_id).to_dict() for item in (gap, old, record)]
    report, findings = monitor.monitor_deliveries(runtime, scope=scope, max_new=1)
    assert report['new_count'] == 1
    assert report['provider_calls'] == 0
    assert findings == []
    latest = runtime.store.latest_record_by_meta_value_exact_scope(
        kind='evaluation_packet', source=monitor.SOURCE, scope=scope, status=None,
        meta_key='semantic_monitor_identity', meta_value=old.content['evaluation_identity'])
    assert latest.content['reason'] == 'unverified_delivery'
    assert latest.content['evaluation_identity'] == old.content['evaluation_identity']
    assert before == [runtime.store.get_by_id(item.record_id).to_dict() for item in (gap, old, record)]


@pytest.mark.parametrize('boundary', ['alias_removed', 'user', 'tenant', 'workspace', 'channel', 'source'])
def test_monitor_alias_scan_keeps_other_authority_boundaries(incident, monkeypatch, boundary):
    runtime, scope, _, _, _, _ = incident
    options = {}
    if boundary == 'alias_removed':
        monkeypatch.setenv('EIMEMORY_LEGACY_AGENT_IDS', 'unrelated-agent')
    elif boundary == 'user':
        scope = ScopeRef.from_dict(dict(asdict(scope), user_id='other-owner'))
    elif boundary in {'tenant', 'workspace', 'channel'}:
        key = 'tenant_id' if boundary == 'tenant' else 'workspace_id'
        value = 'workspace::channel::openclaw' if boundary == 'channel' else 'foreign'
        scope = ScopeRef.from_dict(dict(asdict(scope), **{key: value}))
    else:
        options['source_ids'] = {'other-source'}
    report, findings = monitor.monitor_deliveries(runtime, scope=scope, max_new=1, **options)
    assert report['new_count'] == report['provider_calls'] == 0
    assert not findings


def test_monitor_alias_scan_judges_only_original_exact_render(incident, monkeypatch):
    runtime, scope, _, old, record, _ = incident
    original = ScopeRef.from_dict(old.content['scope'])
    record.scope = original
    runtime.store.rewrite(record, previous_scope=scope)
    calls = []
    def evaluate(query, items):
        calls.append((query, len(items)))
        return monitor._parse_result(json.dumps(dict(relevance=['relevant'],
            off_topic=False, duplicates=False, unanswered=False)), 1)
    monkeypatch.setattr(monitor, '_evaluate', evaluate)
    monkeypatch.setenv('EIMEMORY_PRODUCTION_RECALL_AUTO_REVIEW', '0')
    report, findings = monitor.monitor_deliveries(runtime, scope=scope, max_new=1)
    assert report['provider_calls'] == 1
    assert calls == [('PRIVATE original user question', 1)]
    assert not findings


def test_renamed_agent_original_input_and_physical_render_prepare_without_mutation(incident):
    runtime, scope, gap, old, record, _ = incident
    before = [runtime.store.get_by_id(item.record_id).to_dict() for item in (gap, old, record)]
    count = runtime.store.sqlite.conn.execute('SELECT count(*) FROM records').fetchone()[0]
    result = monitor.prepare_semantic_gap_replay(runtime, gap_id=gap.record_id, scope=scope)
    assert result['status'] == 'ready'
    assert result['canonical_agent_alias_only'] is True
    assert result['natural_benchmark_eligible'] is result['passing_resolution_created'] is False
    assert 'PRIVATE' not in json.dumps(result) and 'owner' not in json.dumps(result)
    assert count == runtime.store.sqlite.conn.execute('SELECT count(*) FROM records').fetchone()[0]
    assert before == [runtime.store.get_by_id(item.record_id).to_dict() for item in (gap, old, record)]


@pytest.mark.parametrize('defect', ['missing_decision', 'missing_input', 'changed_render', 'wrong_owner',
    'wrong_source', 'renamed_not_configured', 'wrong_observation', 'forged_incident', 'maintenance',
    'control', 'wrong_current_release', 'policy_missing'])
def test_missing_or_changed_authority_stays_unverifiable_and_never_becomes_pass(incident, monkeypatch, defect):
    runtime, scope, gap, old, record, decision = incident
    if defect == 'missing_decision':
        with runtime.store.locked() as db:
            db.execute('DELETE FROM proactive_decisions WHERE decision_id=?', (decision,));db.commit()
    elif defect == 'missing_input':
        with runtime.store.locked() as db:
            db.execute('DELETE FROM proactive_query_input_vault WHERE decision_id=?', (decision,));db.commit()
    elif defect == 'changed_render':
        record.summary = 'different current body';runtime.store.rewrite(record, previous_scope=record.scope)
    elif defect == 'wrong_owner':
        scope = ScopeRef(scope.tenant_id, scope.agent_id, scope.workspace_id, 'foreign')
    elif defect == 'wrong_source':
        with runtime.store.locked() as db:
            db.execute("UPDATE proactive_decision_items SET source_id='foreign' WHERE decision_id=?", (decision,));db.commit()
    elif defect == 'renamed_not_configured':
        monkeypatch.setenv('EIMEMORY_LEGACY_AGENT_IDS', 'unrelated-agent')
    elif defect == 'wrong_observation':
        old.content['query_digest'] = '0' * 64;runtime.store.rewrite(old, previous_scope=old.scope)
    elif defect == 'forged_incident':
        gap.content['source_report']['name'] = 'recall_semantic:' + '0' * 64
        runtime.store.rewrite(gap, previous_scope=gap.scope)
    elif defect in {'maintenance', 'control'}:
        column = 'acceptance_generated' if defect == 'maintenance' else 'control_cohort'
        with runtime.store.locked() as db:
            db.execute('UPDATE proactive_decisions SET ' + column + '=1 WHERE decision_id=?', (decision,));db.commit()
    elif defect == 'wrong_current_release':
        runtime._test_runtime_commit = 'c' * 40
    else:
        monkeypatch.setenv('EIMEMORY_CAPTURE_QUERY_SCOPES', '[]')
    result = monitor.prepare_semantic_gap_replay(runtime, gap_id=gap.record_id, scope=scope)
    assert result['status'] == 'unverifiable'
    assert result['passing_resolution_created'] is False
    assert result['natural_benchmark_eligible'] is False
    assert 'PRIVATE' not in json.dumps(result)
    assert runtime.store.get_by_id(gap.record_id).status == 'active'
