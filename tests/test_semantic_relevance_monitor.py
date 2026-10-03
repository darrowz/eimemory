"""Controlled completions exercise wiring, not live model accuracy."""
import json

import pytest

from eimemory.api.runtime import Runtime
from eimemory.evaluation import semantic_relevance_monitor as monitor
from eimemory.models.records import ScopeRef
from eimemory.retrieval.proactive import ProactiveRecallService
from eimemory.scheduler.jobs import _run_quality_gap_intake
from test_thin_recall_handoff import seed
from test_quality_gap_intake import SCOPE, _register_memory_recall
from eimemory.governance.curiosity import generate_learning_goals
from eimemory.governance.self_model import build_self_model


@pytest.fixture
def runtime(tmp_path):
    value = Runtime.create(root=tmp_path)
    yield value
    value.close()


def delivery(runtime, *, query='Where is the secret archive?', defect='unique', decision_id='delivery', release=None):
    record, release = seed(runtime, defect=defect, query=query, decision_id=decision_id, release=release)
    # Legacy persisted query fixture only: production must keep its privacy scrub.
    with runtime.store.locked() as db:
        db.execute('UPDATE proactive_decisions SET query_text=? WHERE decision_id=?', (query, decision_id))
        digest = ProactiveRecallService._render_snapshot_digest(record.title, ProactiveRecallService._record_text(record))
        db.execute('UPDATE proactive_decision_items SET render_digest=? WHERE decision_id=?', (digest, decision_id))
        db.conn.commit()
    return record, release


def saved_reports(runtime):
    return [dict(r.content, persisted_record_id=r.record_id) for r in
            runtime.store.list_records(kinds=['evaluation_packet'], scope=ScopeRef.from_dict(SCOPE), limit=512)
            if r.source == monitor.SOURCE]


def answer(labels, *, unanswered=False, duplicates=False):
    return json.dumps(dict(relevance=labels, off_topic=bool(labels) and all(x == 'unrelated' for x in labels),
                           duplicates=duplicates, unanswered=unanswered))


@pytest.mark.parametrize('query', ['Where is the secret archive?', 'How can I locate the confidential repository?'])
def test_relevant_and_paraphrase(runtime, monkeypatch, query):
    delivery(runtime, query=query)
    def complete(system, user):
        payload = json.loads(user)
        assert payload['query'] == query
        assert payload['items'][0]['text'] == 'private answer'
        return answer(['relevant'])
    monkeypatch.setattr(monitor, '_complete_tool_free', complete)
    result = _run_quality_gap_intake(runtime, scope=SCOPE, reports={})
    assert result['created_count'] == 0
    assert saved_reports(runtime)[0]['verdict'] == 'relevant'


def test_off_topic_storage_intake_dedup_and_goal(runtime, monkeypatch):
    _register_memory_recall(runtime)
    record, release = delivery(runtime, query='Explain lunar eclipses')
    monkeypatch.setattr(monitor, '_complete_tool_free', lambda *_: answer(['unrelated'], unanswered=True))
    result = _run_quality_gap_intake(runtime, scope=SCOPE, reports={})
    assert result['created_count'] == 1
    gap = runtime.store.get_by_id(result['created_record_ids'][0], scope=SCOPE)
    assert gap.content['blocking_metrics']['semantic_off_topic']['actual'] == 1
    assert gap.content['source_report']['observation']['release_identity'] == release
    report = saved_reports(runtime)[0]
    saved = runtime.store.get_by_id(report['persisted_record_id'], scope=SCOPE)
    assert saved.content['verdict'] == 'off_topic'
    assert 'lunar eclipses' not in json.dumps(saved.to_dict())
    assert 'private answer' not in json.dumps(gap.to_dict()) + json.dumps(saved.to_dict())
    delivery(runtime, query='Explain lunar eclipses', decision_id='retry', release=release)
    assert _run_quality_gap_intake(runtime, scope=SCOPE, reports={})['created_count'] == 0
    goals = generate_learning_goals(build_self_model(runtime, scope=SCOPE, persist=False), [], goal_registry={}, thoughts=[], max_goals=20)
    assert any(gap.record_id in goal.get('source_record_ids', []) for goal in goals)
    assert not runtime.store.list_records(kinds=['capability_hypothesis'], scope=SCOPE, limit=50)
    delivery(runtime, query='Explain solar eclipses', decision_id='new-query', release=release)
    assert _run_quality_gap_intake(runtime, scope=SCOPE, reports={})['created_count'] == 1


@pytest.mark.parametrize('defect', ['no_result', 'cross_scope', 'cross_source', 'record_scope', 'unverified', 'unbound', 'model_only', 'maintenance'])
def test_unverified_never_calls_model(runtime, monkeypatch, defect):
    delivery(runtime, defect=defect)
    calls = []
    monkeypatch.setattr(monitor, '_complete_tool_free', lambda *_: calls.append(True))
    result = _run_quality_gap_intake(runtime, scope=SCOPE, reports={})
    assert result['created_count'] == 0
    assert not calls
    assert all(r['verdict'] == 'unknown' for r in saved_reports(runtime))


@pytest.mark.parametrize('raw', ['', '{}', 'not json', answer([]), answer(['unrelated']),
                                  '{"relevance": ["relevant"], "off_topic": true, "duplicates": false, "unanswered": false}'])
def test_malformed_or_inconsistent(runtime, monkeypatch, raw):
    delivery(runtime)
    monkeypatch.setattr(monitor, '_complete_tool_free', lambda *_: raw)
    result = _run_quality_gap_intake(runtime, scope=SCOPE, reports={})
    assert result['created_count'] == 0
    assert saved_reports(runtime)[0]['verdict'] == 'unknown'


def test_default_transport_and_missing_query_fail_closed(runtime, monkeypatch):
    from eimemory.llm.command_client import CommandLLMClient
    monkeypatch.setattr(CommandLLMClient, 'complete', lambda *a, **k: pytest.fail('unsafe command'))
    from eimemory.llm import hermes_tool_free
    monkeypatch.setattr(hermes_tool_free.shutil, 'which', lambda _: None)
    monkeypatch.delenv('EIMEMORY_HERMES_BIN', raising=False)
    delivery(runtime)
    first = _run_quality_gap_intake(runtime, scope=SCOPE, reports={})
    assert saved_reports(runtime)[0]['reason'] == 'tool_free_transport_unavailable'
    seed(runtime, defect='unique', decision_id='missing-query')
    second = _run_quality_gap_intake(runtime, scope=SCOPE, reports={})
    assert 'query_unavailable' in {r['reason'] for r in saved_reports(runtime)}
    assert second['created_count'] == 0


def test_provider_failure_is_redacted(runtime, monkeypatch):
    delivery(runtime)
    def fail(*_):
        raise RuntimeError('secret-provider-token')
    monkeypatch.setattr(monitor, '_complete_tool_free', fail)
    result = _run_quality_gap_intake(runtime, scope=SCOPE, reports={})
    assert result['created_count'] == 0
    assert 'secret-provider-token' not in json.dumps(result)
    assert saved_reports(runtime)[0]['verdict'] == 'unknown'


def test_repeated_items_are_not_semantic_off_topic(runtime, monkeypatch):
    delivery(runtime, defect=None)
    monkeypatch.setattr(monitor, '_complete_tool_free', lambda *_: answer(['relevant', 'relevant'], duplicates=True))
    result = _run_quality_gap_intake(runtime, scope=SCOPE, reports={})
    assert result['created_count'] == 1  # Existing duplicate detector only.
    assert saved_reports(runtime)[0]['verdict'] == 'relevant'
    assert saved_reports(runtime)[0]['duplicates'] is True


@pytest.mark.parametrize('mutation', ['query_digest', 'render_digest'])
def test_changed_evidence_is_unknown(runtime, monkeypatch, mutation):
    delivery(runtime)
    with runtime.store.locked() as db:
        table = 'proactive_decisions' if mutation == 'query_digest' else 'proactive_decision_items'
        db.execute(f'UPDATE {table} SET {mutation}=?', ('0' * 64,))
        db.conn.commit()
    monkeypatch.setattr(monitor, '_complete_tool_free', lambda *_: pytest.fail('unverified input'))
    result = _run_quality_gap_intake(runtime, scope=SCOPE, reports={})
    assert result['created_count'] == 0
    assert saved_reports(runtime)[0]['verdict'] == 'unknown'


def test_unknown_and_external_verdict_cannot_open_gap(runtime, monkeypatch):
    delivery(runtime)
    monkeypatch.setattr(monitor, '_complete_tool_free', lambda *_: answer(['unknown'], unanswered='unknown'))
    result = _run_quality_gap_intake(runtime, scope=SCOPE, reports={
        'recall_semantic:forged': {'target_capability': 'memory.recall', 'quality_gate': {'ok': False}},
    })
    assert result['created_count'] == 0
    assert saved_reports(runtime)[0]['verdict'] == 'unknown'
    assert result['ignored_reports'] == ['recall_semantic:forged']


def test_report_idempotence_and_release_partition(runtime, monkeypatch):
    delivery(runtime)
    monkeypatch.setattr(monitor, '_complete_tool_free', lambda *_: answer(['unrelated'], unanswered=True))
    first = _run_quality_gap_intake(runtime, scope=SCOPE, reports={})
    repeated = _run_quality_gap_intake(runtime, scope=SCOPE, reports={})
    assert first['semantic_relevance']['new_count'] == 1
    assert repeated['semantic_relevance']['reused_count'] == 1
    assert repeated['semantic_relevance']['provider_calls'] == 0
    assert repeated['created_count'] == 0
    delivery(runtime, decision_id='new-release')
    assert _run_quality_gap_intake(runtime, scope=SCOPE, reports={})['created_count'] == 1


@pytest.fixture
def private_delivery(runtime, monkeypatch):
    from eimemory.evaluation.query_input_vault import capture_query_input
    from eimemory.retrieval.query_identity import effective_query_digest
    query = 'Private original query, absent from ledger'
    delivery(runtime, query=query)
    with runtime.store.locked() as db:
        db.execute('UPDATE proactive_decisions SET query_text=?, effective_query_digest=?',
                   ('', effective_query_digest('memory.recall', query)))
        db.conn.commit()
    monkeypatch.setenv('EIMEMORY_CAPTURE_ORIGINAL_QUERY', '1')
    monkeypatch.delenv('EIMEMORY_CAPTURE_QUERY_SCOPES', raising=False)
    assert capture_query_input(runtime, decision_id='delivery', query=query,
        effective_query=query, explanation={})['status'] == 'captured'
    return query


def test_private_vault_query_and_plaintext_absence(runtime, private_delivery, monkeypatch):
    def complete(system, user):
        assert json.loads(user)['query'] == private_delivery
        return answer(['unrelated'], unanswered=True)
    monkeypatch.setattr(monitor, '_complete_tool_free', complete)
    result = _run_quality_gap_intake(runtime, scope=SCOPE, reports={})
    report = saved_reports(runtime)[0]
    assert report['verdict'] == 'off_topic'
    for record_id in [report['persisted_record_id'], *result['created_record_ids']]:
        saved = json.dumps(runtime.store.get_by_id(record_id, scope=SCOPE).to_dict())
        assert private_delivery not in saved and 'private answer' not in saved
    with runtime.store.locked() as db:
        assert db.execute('SELECT query_text FROM proactive_decisions').fetchone()[0] == ''


@pytest.mark.parametrize('mismatch', ['scope', 'channel', 'source', 'decision', 'digest'])
def test_private_vault_mismatch_never_calls_provider(runtime, private_delivery, monkeypatch, mismatch):
    from eimemory.evaluation import query_input_vault
    # Even a hash-bound legacy copy cannot override conflicting private evidence.
    with runtime.store.locked() as db:
        db.execute('UPDATE proactive_decisions SET query_text=?', (private_delivery,))
        db.conn.commit()
    original = query_input_vault.load_query_input
    def load(runtime, **kwargs):
        if mismatch == 'scope':
            kwargs['scope'] = {**SCOPE, 'user_id': 'foreign'}
        elif mismatch == 'channel':
            kwargs['channel'] = 'codex'
        elif mismatch == 'source':
            kwargs['source_id'] = 'foreign'
        elif mismatch == 'decision':
            kwargs['decision_id'] = 'foreign'
        return original(runtime, **kwargs)
    monkeypatch.setattr(query_input_vault, 'load_query_input', load)
    if mismatch == 'digest':
        with runtime.store.locked() as db:
            db.execute('UPDATE proactive_query_input_vault SET input_digest=?', ('0' * 64,))
            db.conn.commit()
    monkeypatch.setattr(monitor, '_complete_tool_free', lambda *_: pytest.fail('unbound input'))
    result = _run_quality_gap_intake(runtime, scope=SCOPE, reports={})
    assert result['created_count'] == 0
    assert saved_reports(runtime)[0]['verdict'] == 'unknown'


@pytest.mark.parametrize('raw', [answer(['relevant']), answer(['unrelated'], unanswered=True),
                               answer(['unknown'], unanswered='unknown'), 'not json'])
def test_provider_only_once_per_identity(runtime, monkeypatch, raw):
    delivery(runtime)
    calls = []
    def complete(*_):
        calls.append(True)
        return raw
    monkeypatch.setattr(monitor, '_complete_tool_free', complete)
    first = _run_quality_gap_intake(runtime, scope=SCOPE, reports={})
    second = _run_quality_gap_intake(runtime, scope=SCOPE, reports={})
    retry = raw == 'not json'
    assert len(calls) == (2 if retry else 1)
    assert second['semantic_relevance']['reused_count'] == (0 if retry else 1)
    assert second['semantic_relevance']['provider_calls'] == (1 if retry else 0)
    assert len(saved_reports(runtime)) == (2 if retry else 1)
    if raw == answer(['unrelated'], unanswered=True):
        assert first['created_count'] == 1
        assert second['created_count'] == 0
        assert second['semantic_relevance']['verdict_counts']['off_topic'] == 1
        assert any(f.get('observation', {}).get('severity') == 'severe' for f in second['findings'])


def test_backlog_bounded_and_output_redacted(runtime, monkeypatch):
    for index in range(19):
        delivery(runtime, decision_id=f'delivery-{index}')
    calls = []
    monkeypatch.setattr(monitor, '_complete_tool_free', lambda *_: calls.append(True) or answer(['relevant']))
    for expected_calls, expected_new, expected_deferred in [(8, 8, 11), (16, 8, 3), (19, 3, 0), (19, 0, 0)]:
        result = _run_quality_gap_intake(runtime, scope=SCOPE, reports={})
        summary = result['semantic_relevance']
        assert len(calls) == expected_calls
        assert summary['new_count'] == expected_new
        assert summary['deferred_count'] == expected_deferred
        assert 'reports' not in summary
        assert len(json.dumps(summary)) < 400
        assert 'secret archive' not in json.dumps(result)
        assert 'private answer' not in json.dumps(result)


def test_unavailable_is_retried(runtime, monkeypatch):
    delivery(runtime)
    calls = []
    def fail(*_):
        calls.append(True)
        raise monitor.ToolFreeUnavailable('private-provider-error')
    monkeypatch.setattr(monitor, '_complete_tool_free', fail)
    for _ in range(3):
        result = _run_quality_gap_intake(runtime, scope=SCOPE, reports={})
        assert result['semantic_relevance']['verdict_counts']['unknown'] == 1
        assert 'private-provider-error' not in json.dumps(result)
    assert len(calls) == 3
    monkeypatch.setattr(monitor, "_complete_tool_free", lambda *_: answer(["relevant"]))
    result = _run_quality_gap_intake(runtime, scope=SCOPE, reports={})
    assert result["semantic_relevance"]["verdict_counts"]["relevant"] == 1


@pytest.mark.parametrize('mutation', ['query_digest', 'render_digest'])
def test_cached_identity_changes_with_evidence(runtime, monkeypatch, mutation):
    delivery(runtime)
    calls = []
    monkeypatch.setattr(monitor, '_complete_tool_free', lambda *_: calls.append(True) or answer(['relevant']))
    _run_quality_gap_intake(runtime, scope=SCOPE, reports={})
    with runtime.store.locked() as db:
        table = 'proactive_decisions' if mutation == 'query_digest' else 'proactive_decision_items'
        db.execute(f'UPDATE {table} SET {mutation}=?', ('0' * 64,))
        db.conn.commit()
    result = _run_quality_gap_intake(runtime, scope=SCOPE, reports={})
    assert result['semantic_relevance']['reused_count'] == 0
    assert result['semantic_relevance']['new_count'] == 1
    assert len({r['evaluation_identity'] for r in saved_reports(runtime)}) == 2
    assert len(calls) == 1  # Changed evidence fails verification before the provider.


def test_cached_off_topic_still_requires_vault_boundary(runtime, private_delivery, monkeypatch):
    monkeypatch.setattr(monitor, '_complete_tool_free', lambda *_: answer(['unrelated'], unanswered=True))
    first = _run_quality_gap_intake(runtime, scope=SCOPE, reports={})
    assert first['created_count'] == 1
    with runtime.store.locked() as db:
        db.execute('UPDATE proactive_query_input_vault SET input_digest=?', ('0' * 64,))
        db.conn.commit()
    monkeypatch.setattr(monitor, '_complete_tool_free', lambda *_: pytest.fail('unbound input'))
    second = _run_quality_gap_intake(runtime, scope=SCOPE, reports={})
    assert second['semantic_relevance']['verdict_counts']['off_topic'] == 0
    assert second['created_count'] == 0
    assert 'query_unavailable' in {r['reason'] for r in saved_reports(runtime)}


def test_corrupt_cached_report_is_not_reused(runtime, monkeypatch):
    delivery(runtime)
    calls = []
    monkeypatch.setattr(monitor, '_complete_tool_free', lambda *_: calls.append(True) or answer(['relevant']))
    _run_quality_gap_intake(runtime, scope=SCOPE, reports={})
    original = runtime.store.list_records_by_meta_value
    def corrupt(**kwargs):
        records = original(**kwargs)
        if kwargs['meta_key'] == 'semantic_monitor_identity' and records:
            records[0].content['verdict'] = 'off_topic'
        return records
    monkeypatch.setattr(runtime.store, 'list_records_by_meta_value', corrupt)
    result = _run_quality_gap_intake(runtime, scope=SCOPE, reports={})
    assert len(calls) == 2
    assert result['semantic_relevance']['reused_count'] == 0
    assert result['created_count'] == 0


def _as_research_task(runtime, decision_id='delivery'):
    with runtime.store.locked() as db:
        db.execute('UPDATE proactive_decisions SET task_type=? WHERE decision_id=?', ('research.task', decision_id))
        db.conn.commit()


def test_research_task_is_judged_per_channel_scope_with_surface_provenance(runtime, monkeypatch):
    from eimemory.adapters.runtime.channel import base_scope_from_channel
    record, _ = delivery(runtime)
    _as_research_task(runtime)
    calls = []
    monkeypatch.setattr(monitor, '_complete_tool_free', lambda *_: calls.append(True) or answer(['relevant']))
    # Nightly passes the base scope; the Hermes decision lives in its channel scope.
    base = base_scope_from_channel('hermes', SCOPE)
    assert base != SCOPE
    summary, findings = monitor.monitor_channel_deliveries(runtime, scope=base)
    assert len(calls) == 1 and not findings
    assert summary['by_surface'] == {'research.task': 1}
    assert summary['by_channel'] == {'hermes': 1}
    report = saved_reports(runtime)[0]
    assert report['decision_surface'] == 'research.task'
    assert report['channel'] == 'hermes'
    assert report['scope'] == SCOPE
    assert report['verdict'] == 'relevant'
    # The pre-1.14.17 base-only run never saw the channel decision.
    assert monitor.monitor_deliveries(runtime, scope=ScopeRef.from_dict(base))[0]['new_count'] == 0


def test_research_task_uses_same_fail_closed_verifier(runtime, monkeypatch):
    delivery(runtime, defect='unverified')
    _as_research_task(runtime)
    monkeypatch.setattr(monitor, '_complete_tool_free', lambda *_: pytest.fail('unverified input'))
    result = _run_quality_gap_intake(runtime, scope=SCOPE, reports={})
    assert result['created_count'] == 0
    assert result['semantic_relevance']['provider_calls'] == 0


def test_unsupported_surface_is_not_judged(runtime, monkeypatch):
    delivery(runtime)
    with runtime.store.locked() as db:
        db.execute('UPDATE proactive_decisions SET task_type=?', ('code.patch',))
        db.conn.commit()
    monkeypatch.setattr(monitor, '_complete_tool_free', lambda *_: pytest.fail('ineligible surface'))
    result = _run_quality_gap_intake(runtime, scope=SCOPE, reports={})
    assert result['semantic_relevance']['skipped_count'] >= 1
    assert saved_reports(runtime) == []


def test_research_task_off_topic_opens_recall_gap_with_surface(runtime, monkeypatch):
    _register_memory_recall(runtime)
    delivery(runtime, query='Explain lunar eclipses')
    _as_research_task(runtime)
    monkeypatch.setattr(monitor, '_complete_tool_free', lambda *_: answer(['unrelated'], unanswered=True))
    result = _run_quality_gap_intake(runtime, scope=SCOPE, reports={})
    assert result['created_count'] == 1
    gap = runtime.store.get_by_id(result['created_record_ids'][0], scope=SCOPE)
    assert gap.content['source_report']['observation']['decision_surface'] == 'research.task'


def test_auto_review_reads_real_research_task_observation_in_channel_scope(runtime, monkeypatch):
    from eimemory.evaluation.production_query_auto_review import _semantic_observation
    record, _ = delivery(runtime)
    _as_research_task(runtime)
    monkeypatch.setattr(monitor, '_complete_tool_free', lambda *_: answer(['relevant']))
    _run_quality_gap_intake(runtime, scope=SCOPE, reports={})
    exact = ScopeRef.from_dict(SCOPE)
    with runtime.store.locked() as db:
        decision = db.load_proactive_decision('delivery')
    observed = _semantic_observation(runtime, decision, exact)
    assert observed['status'] == 'evaluated'
    assert observed['decision_surface'] == 'research.task'
    assert observed['relevant_refs'] == [record.record_id]
    # Provenance must match the judged decision surface.
    original = runtime.store.list_records_by_meta_value
    def forged(**kwargs):
        records = original(**kwargs)
        for item in records:
            item.content.pop('decision_surface', None)
        return records
    monkeypatch.setattr(runtime.store, 'list_records_by_meta_value', forged)
    assert _semantic_observation(runtime, decision, exact)['status'] == 'missing'


def test_legacy_memory_recall_observation_is_still_reused(runtime, monkeypatch):
    delivery(runtime)
    calls = []
    monkeypatch.setattr(monitor, '_complete_tool_free', lambda *_: calls.append(True) or answer(['relevant']))
    _run_quality_gap_intake(runtime, scope=SCOPE, reports={})
    # Strip 1.14.17 provenance to simulate a record written by an older release.
    [saved] = [r for r in runtime.store.list_records(kinds=['evaluation_packet'], scope=ScopeRef.from_dict(SCOPE),
                                                      limit=10) if r.source == monitor.SOURCE]
    legacy = {k: v for k, v in saved.content.items() if k not in ('decision_surface', 'channel')}
    original = runtime.store.list_records_by_meta_value
    def old(**kwargs):
        records = original(**kwargs)
        for item in records:
            if item.record_id == saved.record_id:
                item.content = dict(legacy)
                item.meta = {**item.meta, 'semantic_monitor_digest': monitor._digest(legacy)}
        return records
    monkeypatch.setattr(runtime.store, 'list_records_by_meta_value', old)
    result = _run_quality_gap_intake(runtime, scope=SCOPE, reports={})
    assert result['semantic_relevance']['reused_count'] == 1
    assert len(calls) == 1


def test_nightly_semantic_step_runs_before_auto_review_and_reports_surfaces(runtime, monkeypatch):
    from eimemory.adapters.runtime.channel import base_scope_from_channel
    from eimemory.scheduler import jobs
    from eimemory.scheduler.result_contract import nightly_result_diagnostics
    delivery(runtime)
    _as_research_task(runtime)
    monkeypatch.setattr(monitor, '_complete_tool_free', lambda *_: answer(['relevant']))
    report = jobs._run_semantic_relevance_monitor(runtime, scope=base_scope_from_channel('hermes', SCOPE))
    assert report['ok'] is True and report['provider_calls'] == 1
    diagnostics = nightly_result_diagnostics({'semantic_relevance_monitor': report}, [])['recall_semantic_relevance']
    assert diagnostics['by_surface'] == {'research.task': 1}
    assert diagnostics['by_channel'] == {'hermes': 1}
    assert diagnostics['verdict_counts'] == {'relevant': 1}
    # quality_gap_intake later reuses the cached observation (no second call).
    monkeypatch.setattr(monitor, '_complete_tool_free', lambda *_: pytest.fail('cached'))
    assert _run_quality_gap_intake(runtime, scope=SCOPE, reports={})['semantic_relevance']['reused_count'] == 1
    source = open(jobs.__file__, encoding='utf-8').read()
    assert (source.index('"semantic_relevance_monitor",') < source.index('"production_recall_auto_review",')
            < source.index('"production_recall",'))


@pytest.mark.parametrize('failure', [TimeoutError, RuntimeError])
def test_posthoc_quality_failure_preserves_delivered_results(runtime, monkeypatch, failure):
    delivery(runtime)
    before = runtime.store.load_proactive_decision('delivery')
    assert before['items'] and not any(i.get('render_evidence') for i in before['items'])
    def fail(*_):
        raise failure('quality unavailable')
    monkeypatch.setattr(monitor, '_complete_tool_free', fail)
    result, findings = monitor.monitor_deliveries(runtime, scope=ScopeRef.from_dict(SCOPE))
    assert result['provider_calls'] == 1 and not findings
    assert saved_reports(runtime)[0]['verdict'] == 'unknown'
    assert runtime.store.load_proactive_decision('delivery') == before
    repeated, _ = monitor.monitor_deliveries(runtime, scope=ScopeRef.from_dict(SCOPE))
    assert repeated['provider_calls'] == 1 and repeated['reused_count'] == 0


@pytest.mark.parametrize("initial_query", [None, "wrong original"])
def test_query_failure_cache_does_not_prevent_recovery(runtime, monkeypatch, initial_query):
    from eimemory.evaluation import query_input_vault
    delivery(runtime)
    query = [initial_query]
    monkeypatch.setattr(query_input_vault, "load_query_input", lambda *args, **kwargs: {"query": query[0]})
    monkeypatch.setattr(monitor, "_complete_tool_free", lambda *_: answer(["relevant"]))
    first, findings = monitor.monitor_deliveries(runtime, scope=ScopeRef.from_dict(SCOPE))
    assert first["provider_calls"] == 0 and not findings
    query[0] = "Where is the secret archive?"
    second, findings = monitor.monitor_deliveries(runtime, scope=ScopeRef.from_dict(SCOPE))
    assert second["provider_calls"] == 1 and second["verdict_counts"]["relevant"] == 1
    assert not findings


@pytest.mark.parametrize('defect,reason', [
    ('unverified', 'release_receipt_unavailable'),
    ('unbound', 'release_unbound'),
    ('maintenance', 'maintenance_capture'),
])
def test_zero_call_skip_reason_survives_nightly_boundary(runtime, monkeypatch, defect, reason):
    from eimemory.adapters.runtime.channel import base_scope_from_channel
    from eimemory.scheduler.jobs import _run_semantic_relevance_monitor
    from eimemory.scheduler.result_contract import nightly_result_diagnostics

    delivery(runtime, defect=defect)
    monkeypatch.setattr(monitor, '_complete_tool_free', lambda *_: pytest.fail('must not call provider'))
    before = runtime.store.load_proactive_decision('delivery')
    report = _run_semantic_relevance_monitor(runtime, scope=base_scope_from_channel('hermes', SCOPE))
    assert report['provider_calls'] == report['new_count'] == report['reused_count'] == 0
    assert report['skipped_count'] == 1
    assert report['skip_reason_counts'] == {reason: 1}
    diagnostic = nightly_result_diagnostics({'semantic_relevance_monitor': report}, [])['recall_semantic_relevance']
    assert diagnostic['skip_reason_counts'] == {reason: 1}
    assert runtime.store.load_proactive_decision('delivery') == before
    assert saved_reports(runtime) == []
