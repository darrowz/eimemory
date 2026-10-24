"""Native review caches must not revive invalid or foreign-partition evidence."""
from copy import deepcopy

import pytest

from eimemory.evaluation import semantic_relevance_monitor as monitor
from eimemory.models.records import RecordEnvelope, ScopeRef
from test_quality_gap_intake import SCOPE
from test_semantic_partial_quality import (
    runtime, distinct_delivery, install_reviews, prepare_partial_gap, collect_fixed_repair,
)
from test_semantic_relevance_monitor import answer


def _seed(runtime, monkeypatch):
    distinct_delivery(runtime)
    calls = install_reviews(monkeypatch)
    scope = ScopeRef.from_dict(SCOPE)
    _, findings = monitor.monitor_deliveries(runtime, scope=scope)
    assert len(findings) == 1 and len(calls) == 2
    observation = runtime.store.get_by_id(findings[0]['source_report_record_id'], scope=scope)
    return scope, calls, findings[0], observation


def _append_observation(runtime, old, *, status='active', source_id='default', disagree=False):
    body, meta = deepcopy(old.content), deepcopy(old.meta)
    if disagree:
        body.update(monitor._parse_result(answer(['relevant'] * len(body['relevance'])), len(body['relevance'])))
        meta['semantic_monitor_digest'] = monitor._digest(body)
    later = RecordEnvelope.create(kind=old.kind, title=old.title, content=body, meta=meta,
        scope=old.scope, source=old.source, status=status, source_id=source_id)
    # A newer insertion need not have a larger wall-clock timestamp or UUID.
    # The old implementation sorts by update time and would see the old verdict first.
    later.time.updated_at = '2000-01-01T00:00:00Z'
    runtime.store.append(later)
    return later


@pytest.mark.parametrize('change', ['quarantine_original', 'append_quarantined', 'append_disagree'])
def test_first_review_latest_invalidity_or_disagreement_vetoes_old_positive(runtime, monkeypatch, change):
    scope, calls, _finding, old = _seed(runtime, monkeypatch)
    if change == 'quarantine_original':
        old.status = 'quarantined'
        runtime.store.rewrite(old)
    else:
        _append_observation(runtime, old, status='quarantined' if change == 'append_quarantined' else 'active',
                            disagree=change == 'append_disagree')
    result, findings = monitor.monitor_deliveries(runtime, scope=scope, max_new=0)
    assert findings == []
    assert result['provider_calls'] == 0 and len(calls) == 2


@pytest.mark.parametrize('field,value', [('status', 'quarantined'), ('kind', 'reflection'),
                                        ('source_id', 'other-import')])
def test_first_review_contract_rejects_inactive_wrong_kind_and_source_drift(runtime, monkeypatch, field, value):
    scope, _calls, _finding, record = _seed(runtime, monkeypatch)
    observation = {key: value for key, value in record.content.items()
                   if key not in {'verdict', 'reason', 'relevance', 'off_topic', 'duplicates', 'unanswered'}}
    setattr(record, field, value)
    assert monitor._cached_report(record, scope, observation, len(record.content['relevance'])) is None


@pytest.mark.parametrize('stage', ['observation', 'confirmation'])
def test_foreign_source_partition_cannot_supply_either_review(runtime, monkeypatch, stage):
    scope, calls, finding, observation = _seed(runtime, monkeypatch)
    if stage == 'observation':
        old = observation
    else:
        old = runtime.store.get_by_id(finding['observation']['confirmation_ref'], scope=scope)
    foreign = RecordEnvelope.create(kind=old.kind, title=old.title,
        content=deepcopy(old.content), meta=deepcopy(old.meta), scope=scope,
        source=old.source, source_id='other-import')
    foreign.time.updated_at = '9999-12-31T23:59:59Z'
    runtime.store.append(foreign)
    result, findings = monitor.monitor_deliveries(runtime, scope=scope, max_new=0)
    if findings:
        field = (findings[0]['source_report_record_id'] if stage == 'observation'
                 else findings[0]['observation']['confirmation_ref'])
        assert field != foreign.record_id
    assert result['provider_calls'] == 0 and len(calls) == 2


def test_invalid_first_review_can_recover_only_through_fresh_bound_reviews(runtime, monkeypatch):
    scope, calls, initial, old = _seed(runtime, monkeypatch)
    _append_observation(runtime, old, status='quarantined')
    _, findings = monitor.monitor_deliveries(runtime, scope=scope, max_new=0)
    assert findings == [] and len(calls) == 2
    result, findings = monitor.monitor_deliveries(runtime, scope=scope, max_new=2)
    assert result['provider_calls'] == 2 and len(calls) == 4
    assert len(findings) == 1
    assert findings[0]['source_report_record_id'] != initial['source_report_record_id']
    assert findings[0]['observation']['confirmation_ref'] != initial['observation']['confirmation_ref']
    result, reused = monitor.monitor_deliveries(runtime, scope=scope, max_new=0)
    assert result['provider_calls'] == 0 and len(calls) == 4
    assert reused[0]['source_report_record_id'] == findings[0]['source_report_record_id']


@pytest.mark.parametrize('state', ['quarantined', 'disagree'])
def test_superseded_first_review_cannot_prepare_or_commit_old_gap_repair(runtime, monkeypatch, state):
    from eimemory.governance.learning.quality_gap_intake import _ingest_verified_findings
    scope, gap_id = prepare_partial_gap(runtime, monkeypatch)
    _, repair_findings = collect_fixed_repair(runtime, monkeypatch, scope)
    gap = runtime.store.get_by_id(gap_id, scope=scope)
    old = runtime.store.get_by_id(gap.content['source_report']['record_id'], scope=scope)
    _append_observation(runtime, old, status='quarantined' if state == 'quarantined' else 'active',
                        disagree=state == 'disagree')
    prepared = monitor.prepare_semantic_gap_replay(runtime, gap_id=gap_id, scope=scope)
    assert prepared['status'] == 'unverifiable'
    assert prepared['reason'] == 'semantic_repair_observation_unavailable'
    result = _ingest_verified_findings(runtime, findings=repair_findings, scope=scope)
    assert result['resolved_record_ids'] == []
    assert runtime.store.get_by_id(gap_id, scope=scope).status == 'active'
