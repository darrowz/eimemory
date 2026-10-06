"""Posthoc scheduling uses configured authorization, never observed user IDs."""
from dataclasses import asdict
import json

import pytest

from eimemory.evaluation import semantic_relevance_monitor as monitor
from eimemory.models.records import ScopeRef
from eimemory.scheduler.jobs import _run_semantic_relevance_monitor
from test_semantic_relevance_monitor import runtime, delivery, saved_reports, answer, SCOPE


OPERATOR = dict(SCOPE, workspace_id='product', user_id='operator')
GRANT = dict(SCOPE, channel='hermes', source_id='archive')


@pytest.fixture(autouse=True)
def policy(monkeypatch):
    monkeypatch.setenv('EIMEMORY_CAPTURE_QUERY_SCOPES', json.dumps([GRANT]))


def test_authorized_user_is_scheduled_from_operator(runtime, monkeypatch):
    delivery(runtime)
    monkeypatch.setattr(monitor, '_complete_tool_free', lambda *_: answer(['relevant']))
    result = _run_semantic_relevance_monitor(runtime, scope=OPERATOR)
    assert result['new_count'] == 1
    assert result['by_channel'] == {'hermes': 1}
    assert saved_reports(runtime)[0]['scope'] == SCOPE


@pytest.mark.parametrize('field', ['tenant_id', 'agent_id', 'workspace_id', 'user_id', 'channel', 'source_id'])
def test_wrong_authorization_never_evaluates(runtime, monkeypatch, field):
    delivery(runtime)
    monkeypatch.setenv('EIMEMORY_CAPTURE_QUERY_SCOPES', json.dumps([{**GRANT, field: 'other'}]))
    monkeypatch.setattr(monitor, '_complete_tool_free', lambda *_: pytest.fail('unauthorized'))
    assert _run_semantic_relevance_monitor(runtime, scope=OPERATOR)['new_count'] == 0
    assert saved_reports(runtime) == []


@pytest.mark.parametrize('policy_value', ['[]', '[{}]', 'null', '{', json.dumps([GRANT] * 101)])
def test_revoked_or_invalid_policy_does_not_discover_from_records(runtime, monkeypatch, policy_value):
    delivery(runtime)
    monkeypatch.setenv('EIMEMORY_CAPTURE_QUERY_SCOPES', policy_value)
    monkeypatch.setattr(monitor, '_complete_tool_free', lambda *_: pytest.fail('unauthorized'))
    assert _run_semantic_relevance_monitor(runtime, scope=OPERATOR)['new_count'] == 0
    assert saved_reports(runtime) == []


def test_cross_tenant_even_explicit_grant_is_not_scheduled(runtime, monkeypatch):
    delivery(runtime)
    monkeypatch.setattr(monitor, '_complete_tool_free', lambda *_: pytest.fail('cross tenant'))
    result = _run_semantic_relevance_monitor(runtime, scope={**OPERATOR, 'tenant_id': 'foreign'})
    assert result['new_count'] == 0


def test_ungranted_source_is_excluded_before_loading_decision(runtime, monkeypatch):
    delivery(runtime)
    monkeypatch.setenv('EIMEMORY_CAPTURE_QUERY_SCOPES', json.dumps([{**GRANT, 'source_id': 'other'}]))
    monkeypatch.setattr(runtime.store.sqlite, 'load_proactive_decision',
                        lambda *_: pytest.fail('ungranted decision loaded'))
    result = _run_semantic_relevance_monitor(runtime, scope=OPERATOR)
    assert result['ok'] and result['new_count'] == 0


def test_removal_revokes_cached_scope(runtime, monkeypatch):
    delivery(runtime)
    monkeypatch.setattr(monitor, '_complete_tool_free', lambda *_: answer(['relevant']))
    assert _run_semantic_relevance_monitor(runtime, scope=OPERATOR)['new_count'] == 1
    monkeypatch.setenv('EIMEMORY_CAPTURE_QUERY_SCOPES', '[]')
    result = _run_semantic_relevance_monitor(runtime, scope=OPERATOR)
    assert result['reused_count'] == result['new_count'] == 0


@pytest.mark.parametrize('defect,reason', [('no_result', 'empty_delivery'), ('unique', 'completion_unavailable')])
def test_unknown_does_not_change_delivery(runtime, monkeypatch, defect, reason):
    delivery(runtime, defect=defect)
    before = runtime.store.load_proactive_decision('delivery')
    def fail(*_):
        raise RuntimeError('private failure')
    monkeypatch.setattr(monitor, '_complete_tool_free', fail)
    result = _run_semantic_relevance_monitor(runtime, scope=OPERATOR)
    assert result['verdict_counts']['unknown'] == 1
    assert saved_reports(runtime)[0]['reason'] == reason
    assert not quality_gaps(runtime, SCOPE) and not quality_gaps(runtime, OPERATOR)
    assert runtime.store.load_proactive_decision('delivery') == before


def test_revoked_record_stays_unknown(runtime, monkeypatch):
    record, _ = delivery(runtime)
    record.status = 'revoked'
    runtime.store.rewrite(record, previous_scope=record.scope)
    monkeypatch.setattr(monitor, '_complete_tool_free', lambda *_: pytest.fail('revoked'))
    result = _run_semantic_relevance_monitor(runtime, scope=OPERATOR)
    assert result['verdict_counts']['unknown'] == 1


def test_missing_receipt_is_still_rejected(runtime, monkeypatch):
    delivery(runtime, defect='unverified')
    monkeypatch.setattr(monitor, '_complete_tool_free', lambda *_: pytest.fail('missing receipt'))
    result = _run_semantic_relevance_monitor(runtime, scope=OPERATOR)
    assert result['skipped_count'] == 1
    assert saved_reports(runtime) == []


@pytest.mark.parametrize('mutation', ['none', 'removed', 'receipt_revoked', 'cross_tenant'])
def test_shared_release_proof_is_required_and_revocable(runtime, tmp_path, monkeypatch, mutation):
    from test_release_scope_binding import configure
    _, release = delivery(runtime)
    receipt = runtime.store.get_by_id(release['deployment_receipt_id'])
    previous = receipt.scope
    receipt.scope = ScopeRef.from_dict(OPERATOR)
    effect = receipt.content['side_effect']
    effect['deployment']['current_link'] = '/opt/eimemory/current'
    effect['post_deploy_health'].update(current_link='/opt/eimemory/current',
                                       url='http://127.0.0.1:8091/health')
    runtime.store.rewrite(receipt, previous_scope=previous)
    # 1.14.42 (bd6518eb) resolves trusted shared-service release proof
    # independently of caller scope and of the scope-bindings file (see
    # test_release_scope_binding.py), so removing the binding or moving the
    # receipt's tenant no longer hides it. Revoking the receipt still must.
    path = configure(tmp_path, monkeypatch, receipt, target=SCOPE)
    if mutation == 'removed':
        path.write_text('[]')
    elif mutation in ('receipt_revoked', 'cross_tenant'):
        previous = receipt.scope
        if mutation == 'receipt_revoked':
            receipt.status = 'revoked'
        else:
            receipt.scope = ScopeRef.from_dict({**OPERATOR, 'tenant_id': 'foreign'})
        runtime.store.rewrite(receipt, previous_scope=previous)
        configure(tmp_path, monkeypatch, receipt, target=SCOPE)
    calls = []
    monkeypatch.setattr(monitor, '_complete_tool_free', lambda *_: calls.append(True) or answer(['relevant']))
    result = _run_semantic_relevance_monitor(runtime, scope=OPERATOR)
    assert result['new_count'] == (0 if mutation == 'receipt_revoked' else 1)
    assert len(calls) == (0 if mutation == 'receipt_revoked' else 1)


def test_deduplicated_bounded_scopes_preserve_operator_and_channel_totals(monkeypatch):
    from eimemory.adapters.runtime.channel import resolve_channel_scope
    calls = []
    def observe(runtime, *, scope, max_new, **kwargs):
        calls.append((asdict(scope), max_new))
        return dict(status='observation_only', new_count=1,
                    verdict_counts={'unknown': 1}), []
    monkeypatch.setattr(monitor, 'monitor_deliveries', observe)
    operator_grant = dict(resolve_channel_scope('hermes', OPERATOR), channel='hermes', source_id='archive')
    monkeypatch.setenv('EIMEMORY_CAPTURE_QUERY_SCOPES', json.dumps([GRANT] * 99 + [operator_grant]))
    report, _ = monitor.monitor_channel_deliveries(
        object(), scope=ScopeRef.from_dict(OPERATOR), max_new=2, include_capture_scopes=True)
    assert len(calls) == 4
    assert all(budget == 2 for _, budget in calls)
    assert all((resolve_channel_scope(c, OPERATOR), 2) in calls for c in ('hermes', 'codex', 'openclaw'))
    assert (SCOPE, 2) in calls
    assert report['by_channel'] == {'hermes': 2, 'codex': 1, 'openclaw': 1}


def test_request_scoped_intake_does_not_expand_to_capture_users(runtime, monkeypatch):
    delivery(runtime)
    monkeypatch.setattr(monitor, '_complete_tool_free', lambda *_: pytest.fail('other user'))
    report, findings = monitor.monitor_channel_deliveries(runtime, scope=OPERATOR)
    assert report['new_count'] == 0 and findings == []


def test_monitor_exception_is_contained_by_scheduler(runtime, monkeypatch):
    delivery(runtime)
    before = runtime.store.load_proactive_decision('delivery')
    def fail(*args, **kwargs):
        raise RuntimeError('private failure')
    monkeypatch.setattr(monitor, 'monitor_channel_deliveries', fail)
    result = _run_semantic_relevance_monitor(runtime, scope=OPERATOR)
    assert result['ok'] is False and result['status'] == 'blocked'
    assert 'private failure' not in json.dumps(result)
    assert runtime.store.load_proactive_decision('delivery') == before


def quality_gaps(runtime, scope):
    return [r for r in runtime.store.list_records(kinds=['reflection'], scope=ScopeRef.from_dict(scope), limit=100)
            if r.source == 'eimemory.l5.quality_gap_intake']


def test_nightly_registers_cached_finding_only_in_owning_scope(runtime, monkeypatch):
    delivery(runtime)
    monkeypatch.setattr(monitor, '_complete_tool_free', lambda *_: answer(['unrelated'], unanswered=True))
    monitor.monitor_channel_deliveries(runtime, scope=OPERATOR, include_capture_scopes=True)
    before = runtime.store.load_proactive_decision('delivery')
    monkeypatch.setattr(monitor, '_complete_tool_free', lambda *_: pytest.fail('cached'))
    result = _run_semantic_relevance_monitor(runtime, scope=OPERATOR)
    assert result['ok'] and result['off_topic_finding_count'] == 1
    gaps = quality_gaps(runtime, SCOPE)
    assert len(gaps) == 1
    assert gaps[0].source_id == GRANT['source_id']
    assert gaps[0].content['source_report']['observation']['scope'] == SCOPE
    assert not quality_gaps(runtime, OPERATOR)
    assert _run_semantic_relevance_monitor(runtime, scope=OPERATOR)['ok']
    assert len(quality_gaps(runtime, SCOPE)) == 1
    assert runtime.store.load_proactive_decision('delivery') == before
    assert not runtime.store.list_records(kinds=['capability_hypothesis'], scope=SCOPE, limit=10)


def test_request_intake_channel_finding_keeps_exact_scope(runtime, monkeypatch):
    from eimemory.adapters.runtime.channel import base_scope_from_channel
    from eimemory.governance.quality_gap_intake import ingest_quality_gate_reports
    delivery(runtime)
    monkeypatch.setattr(monitor, '_complete_tool_free', lambda *_: answer(['unrelated'], unanswered=True))
    base = base_scope_from_channel('hermes', SCOPE)
    result = ingest_quality_gate_reports(runtime, scope=base, reports={})
    assert result['created_count'] == 1
    assert len(quality_gaps(runtime, SCOPE)) == 1
    assert not quality_gaps(runtime, base)


@pytest.mark.parametrize('mutation', ['scope_grant', 'source_grant', 'record', 'receipt', 'query_digest', 'render_digest'])
def test_cached_off_topic_revocation_prevents_gap_registration(runtime, monkeypatch, mutation):
    record, release = delivery(runtime)
    monkeypatch.setattr(monitor, '_complete_tool_free', lambda *_: answer(['unrelated'], unanswered=True))
    monitor.monitor_channel_deliveries(runtime, scope=OPERATOR, include_capture_scopes=True)
    assert not quality_gaps(runtime, SCOPE)
    if mutation == 'scope_grant':
        monkeypatch.setenv('EIMEMORY_CAPTURE_QUERY_SCOPES', '[]')
    elif mutation == 'source_grant':
        monkeypatch.setenv('EIMEMORY_CAPTURE_QUERY_SCOPES', json.dumps([{**GRANT, 'source_id': 'other'}]))
    elif mutation in ('record', 'receipt'):
        target = record if mutation == 'record' else runtime.store.get_by_id(release['deployment_receipt_id'])
        target.status = 'revoked'
        runtime.store.rewrite(target, previous_scope=target.scope)
    else:
        with runtime.store.locked() as db:
            table = 'proactive_decisions' if mutation == 'query_digest' else 'proactive_decision_items'
            db.execute(f'UPDATE {table} SET {mutation}=?', ('0' * 64,))
            db.conn.commit()
    before = runtime.store.load_proactive_decision('delivery')
    monkeypatch.setattr(monitor, '_complete_tool_free', lambda *_: pytest.fail('revoked cached evidence'))
    result = _run_semantic_relevance_monitor(runtime, scope=OPERATOR)
    assert result['ok'] and result['off_topic_finding_count'] == 0
    assert not quality_gaps(runtime, SCOPE) and not quality_gaps(runtime, OPERATOR)
    assert runtime.store.load_proactive_decision('delivery') == before


def test_forged_report_observation_cannot_choose_scope(runtime, monkeypatch):
    from eimemory.governance.quality_gap_intake import ingest_quality_gate_reports
    monkeypatch.setattr(monitor, '_complete_tool_free', lambda *_: pytest.fail('no delivery'))
    result = ingest_quality_gate_reports(runtime, scope=OPERATOR, reports={'recall_quality': {
        'quality_gate': {'ok': False}, 'observation': {'scope': SCOPE, 'source_id': 'archive'}}})
    assert result['created_count'] == 1
    assert not quality_gaps(runtime, SCOPE)
    assert len(quality_gaps(runtime, OPERATOR)) == 1


def test_gap_append_failure_is_posthoc_and_retryable(runtime, monkeypatch):
    delivery(runtime)
    before = runtime.store.load_proactive_decision('delivery')
    monkeypatch.setattr(monitor, '_complete_tool_free', lambda *_: answer(['unrelated'], unanswered=True))
    append = runtime.store.append
    def fail_gap(record):
        if record.source == 'eimemory.l5.quality_gap_intake':
            raise RuntimeError('private append error')
        return append(record)
    monkeypatch.setattr(runtime.store, 'append', fail_gap)
    result = _run_semantic_relevance_monitor(runtime, scope=OPERATOR)
    assert result['ok'] is False and 'private append error' not in json.dumps(result)
    assert runtime.store.load_proactive_decision('delivery') == before
    monkeypatch.setattr(runtime.store, 'append', append)
    monkeypatch.setattr(monitor, '_complete_tool_free', lambda *_: pytest.fail('cached'))
    assert _run_semantic_relevance_monitor(runtime, scope=OPERATOR)['quality_gap_created_count'] == 1
