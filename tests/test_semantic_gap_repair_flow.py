"""Actual temporary storage + native signed replay -> native exact-parent owner."""
from copy import deepcopy
from dataclasses import asdict
import json

import pytest

from eimemory.evaluation import semantic_relevance_monitor as semantic
from eimemory.evaluation import semantic_gap_repair as repair
from eimemory.evaluation import explicit_recall as explicit
from eimemory.governance.learning.quality_gap_intake import _ingest_verified_findings
from eimemory.models.records import RecallBundle, RecordEnvelope
from test_semantic_gap_repair import incident


def verdict(label='relevant'):
    return semantic._parse_result(json.dumps(dict(relevance=[label], off_topic=label == 'unrelated',
        duplicates=False, unanswered=True if label == 'unrelated' else 'unknown' if label == 'unknown' else False)), 1)


@pytest.fixture
def replay_case(incident, monkeypatch):
    runtime, scope, gap, old, old_record, decision = incident
    current = RecordEnvelope.create(kind='memory', title='Current answer', summary='PRIVATE current answer',
        scope=scope, source='hermes.memory', source_id='hermes',
        meta={'force_capture': True, 'memory_type': 'durable_fact'})
    runtime.store.append(current)
    calls = []
    options = dict(complete=True, status='evidence_found', top='evidence_found', source_budget=0,
                   recall_budget=0, extra=[], cascade=[])
    def recall(**kwargs):
        calls.append(deepcopy(kwargs))
        return RecallBundle(items=[current, *options['extra']], rules=[], reflections=[], confidence=.9,
            next_action_hint='', explanation={'retrieval_status': options['top'],
            'relevance_selector': {'collection_complete': options['complete'], 'status': options['status']},
            'engine_diagnostics': {'source_budget_exhausted': options['source_budget'],
                'drops': {'recall_budget_exhausted': options['recall_budget']}},
            'cascade_evidence': options['cascade']})
    monkeypatch.setattr(runtime.memory, 'recall', recall)
    judges = []
    def evaluate(query, items):
        judges.append((query, deepcopy(items)))
        return verdict()
    monkeypatch.setattr(semantic, '_evaluate', evaluate)
    return runtime, scope, gap, old, current, calls, judges, options


def test_default_nightly_path_autoreplays_once_and_native_owner_resolves_exact_parent(replay_case):
    runtime, scope, gap, old, current, calls, judges, _ = replay_case
    before = [runtime.store.get_by_id(item.record_id).to_dict() for item in (gap, old)]
    report, findings = repair.collect_semantic_gap_repair_findings(runtime, scope=scope, max_new=2)
    assert report['replay_count'] == report['provider_calls'] == 1
    assert len(findings) == len(calls) == len(judges) == 1
    assert calls[0]['query'] == 'PRIVATE original user question'
    assert calls[0]['scope'] == asdict(scope)
    assert calls[0]['task_context'] == dict(source_ids=['hermes'], runtime_channel='proactive',
        task_type='research.task', exact_scope_only=True)
    intake = _ingest_verified_findings(runtime, findings=findings, scope=scope)
    assert len(intake['resolved_record_ids']) == 1
    resolution = runtime.store.get_by_id(intake['resolved_record_ids'][0], scope=scope)
    assert resolution.content['resolves_gap_id'] == gap.record_id
    assert resolution.content['repair_evidence']['review_ref'] == findings[0]['_semantic_repair']['review_ref']
    assert resolution.meta['release_commit'] == 'a' * 40
    assert resolution.status == 'resolved'
    assert before == [runtime.store.get_by_id(item.record_id).to_dict() for item in (gap, old)]
    assert 'PRIVATE' not in json.dumps(resolution.to_dict())
    again, repeated = repair.collect_semantic_gap_repair_findings(runtime, scope=scope, max_new=0)
    assert again['replay_count'] == again['provider_calls'] == 0
    assert len(calls) == len(judges) == 1
    assert repeated == []
    duplicate = _ingest_verified_findings(runtime, findings=findings, scope=scope)
    assert not duplicate['resolved_record_ids']
    assert duplicate['deduplicated_record_ids'] == [resolution.record_id]
    with runtime.store.locked() as db:
        assert db.execute('SELECT count(*) FROM proactive_decisions').fetchone()[0] == 1
        pin = db.execute('SELECT reason FROM proactive_decision_retention_pins').fetchone()[0]
        assert pin == 'pending_case'
        assert db.execute("SELECT count(*) FROM records WHERE source LIKE '%production_recall%accepted%'").fetchone()[0] == 0


def test_native_conversation_maintenance_result_uses_its_own_contract_not_explicit_loadout(replay_case):
    """A fixture contrast, not reconstruction of 136's unretained raw records."""
    from eimemory.adapters.runtime.service import AgentRuntimeMemoryService
    from eimemory.evaluation.explicit_input_vault import load_explicit_input
    runtime, scope, gap, old, current, calls, judges, _ = replay_case
    before = [runtime.store.get_by_id(item.record_id).to_dict() for item in (gap, old)]
    current.title = 'Completed turn fixture'
    current.meta['memory_type'] = 'conversation'
    runtime.store.rewrite(current, previous_scope=scope)
    raw = RecallBundle(items=[current], rules=[], reflections=[], confidence=.9, next_action_hint='',
        explanation={'retrieval_status': 'evidence_found',
            'relevance_selector': {'collection_complete': True, 'status': 'evidence_found'},
            'engine_diagnostics': {'source_budget_exhausted': 0, 'drops': {}}})
    native = raw.to_compact_dict(limit=8)
    explicit_tool = AgentRuntimeMemoryService._assemble_recall_bundle(raw, limit=8)
    assert len(native['items']) == 1 and native['retrieval_status'] == 'evidence_found'
    assert explicit_tool['items'] == [] and explicit_tool['retrieval_status'] == 'unavailable'

    report, findings = repair.collect_semantic_gap_repair_findings(runtime, scope=scope, max_new=2)
    assert report['replay_count'] == report['provider_calls'] == len(calls) == len(judges) == 1
    packet = runtime.store.get_by_id(findings[0]['_semantic_repair']['packet_ref'], scope=scope)
    body = explicit._verify(packet, repair.CAPTURE_SOURCE, scope)
    assert body['result_contract'] == repair.RESULT_CONTRACT == 'native_recall_compact.v1'
    assert body['host_delivery_certified'] is False
    assert body['natural_benchmark_eligible'] is body['memory_contribution_certified'] is False
    actual = load_explicit_input(runtime, capture=packet)['result']
    assert actual['result_contract'] == repair.RESULT_CONTRACT
    assert actual['bundle']['items'] == [dict(native['items'][0], scope=asdict(scope))]
    assert actual['bundle']['retrieval_status'] == body['retrieval_state']['final_output_status'] == 'evidence_found'
    assert len(body['references']) == 1 and body['references'][0]['record_ref'] == current.record_id
    assert len(_ingest_verified_findings(runtime, findings=findings, scope=scope)['resolved_record_ids']) == 1
    assert before == [runtime.store.get_by_id(item.record_id).to_dict() for item in (gap, old)]
    with runtime.store.locked() as db:
        assert db.execute('SELECT count(*) FROM proactive_decisions').fetchone()[0] == 1
        assert db.execute("SELECT count(*) FROM records WHERE source LIKE '%production_recall%accepted%'").fetchone()[0] == 0


@pytest.mark.parametrize('mutation', ['absent_contract', 'bad_contract', 'claimed_host_delivery', 'absent_host_claim'])
def test_absent_or_wrong_signed_native_result_contract_is_rejected_before_judge(replay_case, mutation):
    runtime, scope, gap, _, _, _, judges, _ = replay_case
    result = repair.capture_semantic_gap_replay(runtime, gap_id=gap.record_id, scope=scope)
    packet = runtime.store.get_by_id(result['packet_ref'], scope=scope)
    body = deepcopy(packet.content)
    body.pop('signature')
    if mutation == 'absent_contract':
        body.pop('result_contract')  # A 136-style packet cannot be reinterpreted.
    elif mutation == 'bad_contract':
        body['result_contract'] = 'explicit_service_loadout.v1'
    elif mutation == 'claimed_host_delivery':
        body['host_delivery_certified'] = True
    else:
        body.pop('host_delivery_certified')
    packet.content = explicit._sign(body)
    runtime.store.rewrite(packet, previous_scope=scope)
    explicit._verify(packet, repair.CAPTURE_SOURCE, scope)
    with pytest.raises(ValueError, match='semantic_repair_packet_invalid'):
        repair.review_semantic_gap_replay(runtime, packet_ref=packet.record_id, scope=scope)
    assert judges == [] and runtime.store.get_by_id(gap.record_id).status == 'active'


def test_private_actual_result_must_also_bind_the_native_result_contract(replay_case, monkeypatch):
    runtime, scope, gap, _, _, _, judges, _ = replay_case
    native = repair._actual_result
    def other_contract(*args, **kwargs):
        actual, references = native(*args, **kwargs)
        actual['result_contract'] = 'explicit_service_loadout.v1'
        return actual, references
    monkeypatch.setattr(repair, '_actual_result', other_contract)
    report, findings = repair.collect_semantic_gap_repair_findings(runtime, scope=scope, max_new=2)
    assert set(report['reason_counts']) == {'semantic_repair_replay_input_mismatch'}
    assert report['reason_counts']['semantic_repair_replay_input_mismatch'] >= 1
    assert not findings and judges == [] and runtime.store.get_by_id(gap.record_id).status == 'active'


@pytest.mark.parametrize('options', [dict(complete=False), dict(complete=None), dict(status='degraded'),
    dict(top='degraded'), dict(top=None), dict(top='no_evidence'), dict(source_budget=1), dict(recall_budget=1)])
def test_incomplete_result_never_calls_judge_or_resolves(replay_case, options):
    runtime, scope, gap, _, _, calls, judges, config = replay_case
    config.update(options)
    report, findings = repair.collect_semantic_gap_repair_findings(runtime, scope=scope, max_new=2)
    assert len(calls) == 1 and judges == [] and findings == []
    assert report['provider_calls'] == 0
    assert runtime.store.get_by_id(gap.record_id).status == 'active'


@pytest.mark.parametrize('label', ['unrelated', 'unknown'])
def test_failed_or_unknown_real_review_is_retained_and_not_rejudged_each_nightly(replay_case, monkeypatch, label):
    runtime, scope, gap, _, _, calls, _, _ = replay_case
    count = []
    monkeypatch.setattr(semantic, '_evaluate', lambda *args: count.append(True) or verdict(label))
    first, findings = repair.collect_semantic_gap_repair_findings(runtime, scope=scope, max_new=2)
    # A changed capture policy cannot turn a genuine negative review into a
    # request for another model judgment or maintenance recall.
    monkeypatch.setenv('EIMEMORY_CAPTURE_ORIGINAL_QUERY', '0')
    second, repeated = repair.collect_semantic_gap_repair_findings(runtime, scope=scope, max_new=2)
    assert not findings and not repeated
    assert first['provider_calls'] == 1 and second['provider_calls'] == 0
    assert len(count) == len(calls) == 1
    assert runtime.store.get_by_id(gap.record_id).status == 'active'


def _audits(runtime, scope):
    return [record for record in runtime.store.list_records(kinds=['evaluation_packet'], scope=scope, limit=100)
            if record.source == 'eimemory.semantic_relevance_monitor.repair_audit']


@pytest.mark.parametrize('disabled', [None, '0', 'true'])
def test_same_release_capture_policy_recovery_resolves_once_without_rewriting_old_audit(
        replay_case, monkeypatch, disabled):
    from eimemory.evaluation.query_input_vault import configured_capture_scopes
    runtime, scope, gap, _, _, calls, judges, _ = replay_case
    if disabled is None:
        monkeypatch.delenv('EIMEMORY_CAPTURE_ORIGINAL_QUERY')
    else:
        monkeypatch.setenv('EIMEMORY_CAPTURE_ORIGINAL_QUERY', disabled)
    fingerprint = repair._capture_policy_fingerprint(configured_capture_scopes())
    first, findings = repair.collect_semantic_gap_repair_findings(runtime, scope=scope, max_new=2)
    assert not findings and first['reason_counts'] == {'semantic_repair_capture_unavailable': 1}
    old_audit = _audits(runtime, scope)[0]
    before = old_audit.to_dict()
    signed = explicit._verify(old_audit, old_audit.source, scope)
    assert signed['capture_policy_fingerprint'] == fingerprint
    assert old_audit.meta['semantic_repair_capture_policy_fingerprint'] == fingerprint
    assert 'PRIVATE' not in json.dumps(signed)
    assert len(fingerprint) == 64 and set(fingerprint) <= set('0123456789abcdef')
    second, repeated = repair.collect_semantic_gap_repair_findings(runtime, scope=scope, max_new=2)
    assert not repeated and second['unverifiable_count'] == 0
    assert calls == judges == []

    monkeypatch.setenv('EIMEMORY_CAPTURE_ORIGINAL_QUERY', '1')
    recovered, findings = repair.collect_semantic_gap_repair_findings(runtime, scope=scope, max_new=2)
    assert recovered['replay_count'] == recovered['provider_calls'] == len(calls) == len(judges) == 1
    assert len(_ingest_verified_findings(runtime, findings=findings, scope=scope)['resolved_record_ids']) == 1
    assert before == runtime.store.get_by_id(old_audit.record_id, scope=scope).to_dict()
    again, repeated = repair.collect_semantic_gap_repair_findings(runtime, scope=scope, max_new=2)
    assert not repeated and again['replay_count'] == again['provider_calls'] == 0
    assert len(calls) == len(judges) == 1


def test_legacy_audit_without_policy_fingerprint_allows_one_retry_and_is_preserved(replay_case):
    runtime, scope, gap, _, _, calls, judges, _ = replay_case
    body = dict(schema=repair.SCHEMA, status='unverifiable', reason='semantic_repair_capture_unavailable',
        release_identity=repair._current(runtime, scope), gap_ref=gap.record_id,
        gap_digest=explicit._digest(gap.to_dict()), packet_ref='', packet_digest='',
        natural_benchmark_eligible=False, passing_resolution_created=False)
    legacy = explicit._record('eimemory.semantic_relevance_monitor.repair_audit', 'srpa_legacy135', scope, body)
    legacy.meta.update(semantic_repair_gap_ref=gap.record_id, semantic_repair_packet_ref='',
        semantic_repair_release_digest=explicit._digest(body['release_identity']))
    runtime.store.append(legacy)
    before = runtime.store.get_by_id(legacy.record_id, scope=scope).to_dict()
    report, findings = repair.collect_semantic_gap_repair_findings(runtime, scope=scope, max_new=2)
    assert report['replay_count'] == report['provider_calls'] == len(calls) == len(judges) == 1
    assert len(_ingest_verified_findings(runtime, findings=findings, scope=scope)['resolved_record_ids']) == 1
    assert before == runtime.store.get_by_id(legacy.record_id, scope=scope).to_dict()
    again, repeated = repair.collect_semantic_gap_repair_findings(runtime, scope=scope, max_new=2)
    assert not repeated and again['replay_count'] == again['provider_calls'] == 0


def test_scope_order_and_duplicate_entries_do_not_invalidate_negative_policy_cache(replay_case, monkeypatch):
    from eimemory.evaluation.query_input_vault import configured_capture_scopes
    runtime, scope, _, _, _, calls, judges, _ = replay_case
    monkeypatch.setenv('EIMEMORY_CAPTURE_ORIGINAL_QUERY', '0')
    allowed = configured_capture_scopes()
    first_fingerprint = repair._capture_policy_fingerprint(allowed)
    repair.collect_semantic_gap_repair_findings(runtime, scope=scope, max_new=2)
    monkeypatch.setenv('EIMEMORY_CAPTURE_QUERY_SCOPES', json.dumps([*reversed(allowed), allowed[0]]))
    assert repair._capture_policy_fingerprint(configured_capture_scopes()) == first_fingerprint
    report, findings = repair.collect_semantic_gap_repair_findings(runtime, scope=scope, max_new=2)
    assert not findings and report['unverifiable_count'] == 0
    assert len(_audits(runtime, scope)) == 1 and calls == judges == []


@pytest.mark.parametrize('policy', ['[]', 'foreign', 'malformed'])
def test_changed_invalid_or_foreign_scope_policy_never_creates_traffic(replay_case, monkeypatch, policy):
    runtime, scope, gap, _, _, calls, judges, _ = replay_case
    monkeypatch.setenv('EIMEMORY_CAPTURE_ORIGINAL_QUERY', '0')
    repair.collect_semantic_gap_repair_findings(runtime, scope=scope, max_new=2)
    monkeypatch.setenv('EIMEMORY_CAPTURE_ORIGINAL_QUERY', '1')
    configured = json.dumps([dict(asdict(scope), user_id='foreign', channel='hermes', source_id='hermes')])
    monkeypatch.setenv('EIMEMORY_CAPTURE_QUERY_SCOPES', configured if policy == 'foreign' else policy)
    if policy == 'malformed':
        with pytest.raises(ValueError, match='scope_policy_invalid'):
            repair.collect_semantic_gap_repair_findings(runtime, scope=scope, max_new=2)
    else:
        report, findings = repair.collect_semantic_gap_repair_findings(runtime, scope=scope, max_new=2)
        assert report['status'] == 'scope_not_authorized' and findings == []
    assert calls == judges == [] and runtime.store.get_by_id(gap.record_id).status == 'active'


def test_packet_failure_audit_retries_after_policy_recovery_without_recalling(replay_case, monkeypatch):
    runtime, scope, gap, _, _, calls, judges, _ = replay_case
    packet = repair.capture_semantic_gap_replay(runtime, gap_id=gap.record_id, scope=scope)
    native = repair._load_replay
    def unavailable(*args, **kwargs):
        raise ValueError('semantic_repair_evidence_invalid')
    monkeypatch.setattr(repair, '_load_replay', unavailable)
    first, findings = repair.collect_semantic_gap_repair_findings(runtime, scope=scope, max_new=2)
    assert not findings and first['unverifiable_count'] == 1
    audit = _audits(runtime, scope)[0]
    assert audit.content['packet_ref'] == packet['packet_ref']
    before = audit.to_dict()
    monkeypatch.setattr(repair, '_load_replay', native)
    second, repeated = repair.collect_semantic_gap_repair_findings(runtime, scope=scope, max_new=2)
    assert not repeated and second['provider_calls'] == 0 and judges == []
    monkeypatch.setenv('EIMEMORY_CAPTURE_ORIGINAL_QUERY', '0')
    recovered, findings = repair.collect_semantic_gap_repair_findings(runtime, scope=scope, max_new=2)
    assert recovered['replay_count'] == 0 and recovered['provider_calls'] == len(judges) == 1
    assert len(calls) == 1
    assert len(_ingest_verified_findings(runtime, findings=findings, scope=scope)['resolved_record_ids']) == 1
    assert before == runtime.store.get_by_id(audit.record_id, scope=scope).to_dict()


def test_audit_uses_selection_policy_snapshot_during_configuration_change(replay_case, monkeypatch):
    from eimemory.evaluation.query_input_vault import configured_capture_scopes
    runtime, scope, _, _, _, calls, judges, _ = replay_case
    monkeypatch.setenv('EIMEMORY_CAPTURE_ORIGINAL_QUERY', '0')
    previous = repair._capture_policy_fingerprint(configured_capture_scopes())
    native = repair.capture_semantic_gap_replay
    def changed(*args, **kwargs):
        monkeypatch.setenv('EIMEMORY_CAPTURE_ORIGINAL_QUERY', '1')
        raise ValueError('semantic_repair_capture_unavailable')
    monkeypatch.setattr(repair, 'capture_semantic_gap_replay', changed)
    repair.collect_semantic_gap_repair_findings(runtime, scope=scope, max_new=2)
    assert _audits(runtime, scope)[0].content['capture_policy_fingerprint'] == previous
    assert previous != repair._capture_policy_fingerprint(configured_capture_scopes())
    monkeypatch.setattr(repair, 'capture_semantic_gap_replay', native)
    recovered, findings = repair.collect_semantic_gap_repair_findings(runtime, scope=scope, max_new=2)
    assert recovered['replay_count'] == recovered['provider_calls'] == len(calls) == len(judges) == len(findings) == 1


def test_capture_policy_recovery_keeps_replay_and_judge_within_shared_budget(replay_case, monkeypatch):
    runtime, scope, _, _, _, calls, judges, _ = replay_case
    monkeypatch.setenv('EIMEMORY_CAPTURE_ORIGINAL_QUERY', '0')
    repair.collect_semantic_gap_repair_findings(runtime, scope=scope, max_new=2)
    monkeypatch.setenv('EIMEMORY_CAPTURE_ORIGINAL_QUERY', '1')
    first, findings = repair.collect_semantic_gap_repair_findings(runtime, scope=scope, max_new=1)
    assert not findings and first['replay_count'] == 1 and first['provider_calls'] == 0
    assert first['deferred_count'] == 1
    second, findings = repair.collect_semantic_gap_repair_findings(runtime, scope=scope, max_new=1)
    assert second['replay_count'] == 0 and second['provider_calls'] == 1
    assert len(findings) == len(calls) == len(judges) == 1


def test_missing_old_decision_audits_without_model_replay_or_passing_resolution(incident):
    runtime, scope, gap, _, _, decision = incident
    with runtime.store.locked() as db:
        db.execute('DELETE FROM proactive_decisions WHERE decision_id=?', (decision,));db.commit()
    for _ in range(2):
        report, findings = repair.collect_semantic_gap_repair_findings(runtime, scope=scope, max_new=2)
        assert not findings and report['replay_count'] == report['provider_calls'] == 0
    audits = [r for r in runtime.store.list_records(kinds=['evaluation_packet'], scope=scope, limit=100)
              if r.source.endswith('.repair_audit')]
    assert len(audits) == 1 and audits[0].content['status'] == 'unverifiable'
    assert runtime.store.get_by_id(gap.record_id).status == 'active'


def test_private_vault_and_typed_packet_roll_back_together_and_retry_recovers(replay_case, monkeypatch):
    runtime, scope, gap, _, _, _, _, _ = replay_case
    native = explicit._record
    def failed(source, *args, **kwargs):
        if source == repair.CAPTURE_SOURCE:
            raise RuntimeError('injected signing failure')
        return native(source, *args, **kwargs)
    monkeypatch.setattr(explicit, '_record', failed)
    with pytest.raises(RuntimeError):
        repair.capture_semantic_gap_replay(runtime, gap_id=gap.record_id, scope=scope)
    with runtime.store.locked() as db:
        exists = db.execute("SELECT 1 FROM sqlite_master WHERE name='explicit_query_input_vault'").fetchone()
        assert not exists or db.execute('SELECT count(*) FROM explicit_query_input_vault').fetchone()[0] == 0
    monkeypatch.setattr(explicit, '_record', native)
    assert repair.capture_semantic_gap_replay(runtime, gap_id=gap.record_id, scope=scope)['status'] == 'captured'


def test_parent_superseded_after_review_cannot_be_cleared(replay_case):
    runtime, scope, gap, _, _, _, _, _ = replay_case
    packet = repair.capture_semantic_gap_replay(runtime, gap_id=gap.record_id, scope=scope)
    review = repair.review_semantic_gap_replay(runtime, packet_ref=packet['packet_ref'], scope=scope)
    finding = repair.verified_repair_finding(runtime, review_ref=review['review_ref'], scope=scope)
    newer = RecordEnvelope.from_dict(gap.to_dict())
    newer.record_id = 'ref_' + 'f' * 12
    newer.content['supersedes_gap_id'] = gap.record_id
    newer.meta['supersedes_gap_id'] = gap.record_id
    runtime.store.append(newer)
    result = _ingest_verified_findings(runtime, findings=[finding], scope=scope)
    assert not result['resolved_record_ids']
    assert runtime.store.get_by_id(newer.record_id).status == 'active'


@pytest.mark.parametrize('mutation', ['signature', 'vault', 'physical', 'release'])
def test_changed_current_authority_is_rejected_before_judge(replay_case, mutation):
    runtime, scope, gap, _, current, _, judges, _ = replay_case
    result = repair.capture_semantic_gap_replay(runtime, gap_id=gap.record_id, scope=scope)
    packet = runtime.store.get_by_id(result['packet_ref'], scope=scope)
    if mutation == 'signature':
        packet.content['retrieval_state']['collection_complete'] = False
        runtime.store.rewrite(packet, previous_scope=scope)
    elif mutation == 'vault':
        with runtime.store.locked() as db:
            db.execute("UPDATE explicit_query_input_vault SET payload='{}' WHERE capture_id=?", (packet.record_id,));db.commit()
    elif mutation == 'physical':
        current.summary = 'changed memory'
        runtime.store.rewrite(current, previous_scope=scope)
    else:
        runtime._test_runtime_commit = 'c' * 40
    with pytest.raises(ValueError):
        repair.review_semantic_gap_replay(runtime, packet_ref=packet.record_id, scope=scope)
    assert judges == []


def test_legacy_cascade_evidence_is_unverifiable_instead_of_hidden_before_judge(replay_case):
    runtime, scope, gap, _, _, _, judges, options = replay_case
    options['cascade'] = [dict(summary='PRIVATE off-topic legacy cascade')]
    report, findings = repair.collect_semantic_gap_repair_findings(runtime, scope=scope, max_new=2)
    assert not findings and judges == []
    assert report['reason_counts']['semantic_repair_legacy_evidence_unverifiable'] == 1


def test_default_monitor_connection_consumes_replay_and_native_ingestion(replay_case):
    from eimemory.scheduler.jobs import _run_semantic_relevance_monitor
    runtime, scope, gap, _, _, _, _, _ = replay_case
    result = _run_semantic_relevance_monitor(runtime, scope=asdict(scope))
    assert result['ok'] is True
    assert result['semantic_gap_repair']['provider_calls'] == 1
    assert result['semantic_gap_repair']['replay_count'] == 1
    records = runtime.store.list_records(kinds=['reflection'], scope=scope, limit=100)
    assert any(r.status == 'resolved' and r.content.get('resolves_gap_id') == gap.record_id for r in records)


def test_final_serialized_proof_binding_failure_cannot_become_semantic_pass(replay_case, monkeypatch):
    runtime, scope, gap, _, _, _, judges, _ = replay_case
    native = runtime.memory.recall
    def invalid_proof(**kwargs):
        bundle = native(**kwargs)
        bundle.explanation['relevance_selector']['caller_assistance'] = dict(
            status='evidence_found', outcome='supported', proofs=[])
        return bundle
    monkeypatch.setattr(runtime.memory, 'recall', invalid_proof)
    report, findings = repair.collect_semantic_gap_repair_findings(runtime, scope=scope, max_new=2)
    assert not findings and judges == []
    assert report['provider_calls'] == 0
    captures = runtime.store.list_records_by_meta_value(kinds=['evaluation_packet'], scope=scope,
        meta_key='report_type', meta_value='semantic_gap_replay_capture', limit=10)
    assert len(captures) == 1
    assert captures[0].content['retrieval_state']['status'] == 'evidence_found'
    assert captures[0].content['retrieval_state']['final_output_status'] == 'unavailable'


def test_resolved_historical_active_records_do_not_starve_the_only_current_incident(replay_case):
    from eimemory.governance.learning.quality_gap_intake import _quality_finding, _resolution_record
    runtime, scope, gap, _, _, calls, _, _ = replay_case
    gap.time.updated_at = '2000-01-01T00:00:00Z'
    runtime.store.rewrite(gap, previous_scope=scope)
    for index in range(32):
        older = RecordEnvelope.from_dict(gap.to_dict())
        older.record_id = 'ref_' + f'{index:012x}'
        key = 'quality-gap:recall_semantic:' + f'{index:064x}' + ':memory.recall'
        older.content['semantic_key'] = key;older.meta['semantic_key'] = key
        older.time.updated_at = '2099-01-01T00:00:00Z'
        runtime.store.append(older)
        finding = _quality_finding('recall_semantic:' + f'{index:064x}', dict(target_capability='memory.recall',
            quality_gate={'ok': True}, sample_count=1))
        resolution = _resolution_record(finding, scope=scope, resolves=older)
        runtime.store.append(resolution)
    report, findings = repair.collect_semantic_gap_repair_findings(runtime, scope=scope, max_new=2)
    assert report['replay_count'] == len(calls) == 1
    assert len(findings) == 1 and findings[0]['_semantic_repair']['expected_gap_id'] == gap.record_id


def test_unverifiable_current_release_audits_do_not_starve_later_incidents(replay_case):
    runtime, scope, gap, _, _, calls, _, _ = replay_case
    gap.time.updated_at = '2099-01-01T00:00:00Z'
    runtime.store.rewrite(gap, previous_scope=scope)
    for index in range(32):
        missing = RecordEnvelope.from_dict(gap.to_dict())
        missing.record_id = 'ref_' + f'{index:012x}'
        key = 'quality-gap:recall_semantic:' + f'{index:064x}' + ':memory.recall'
        missing.content['semantic_key'] = key;missing.meta['semantic_key'] = key
        missing.time.updated_at = '2000-01-01T00:00:00Z'
        runtime.store.append(missing)
    first, initial = repair.collect_semantic_gap_repair_findings(runtime, scope=scope, max_new=2)
    assert first['replay_count'] == 0 and not initial
    assert first['unverifiable_count'] == 32
    second, findings = repair.collect_semantic_gap_repair_findings(runtime, scope=scope, max_new=2)
    assert second['replay_count'] == len(calls) == 1
    assert len(findings) == 1 and findings[0]['_semantic_repair']['expected_gap_id'] == gap.record_id


def test_wrong_semantic_identity_at_deterministic_resolution_id_is_never_deduped(replay_case):
    from hashlib import sha256
    from eimemory.governance.learning.quality_gap_intake import QUALITY_GAP_SOURCE
    runtime, scope, gap, _, _, _, _, _ = replay_case
    packet = repair.capture_semantic_gap_replay(runtime, gap_id=gap.record_id, scope=scope)
    review = repair.review_semantic_gap_replay(runtime, packet_ref=packet['packet_ref'], scope=scope)
    finding = repair.verified_repair_finding(runtime, review_ref=review['review_ref'], scope=scope)
    identifier = 'ref_' + sha256(('semantic-repair:' + gap.record_id + ':' + review['review_ref']).encode()).hexdigest()[:12]
    fake = RecordEnvelope.create(kind='reflection', title='Invalid resolution identity', scope=scope,
        source=QUALITY_GAP_SOURCE, status='resolved',
        content=dict(resolves_gap_id=gap.record_id, repair_evidence=finding['_semantic_repair']),
        meta=dict(semantic_key='quality-gap:unrelated:memory.recall'))
    fake.record_id = identifier
    runtime.store.append(fake)
    result = _ingest_verified_findings(runtime, findings=[finding], scope=scope)
    assert result['resolved_record_ids'] == result['deduplicated_record_ids'] == []
    assert result['ignored_reports'] == [finding['report_name']]
    assert runtime.store.get_by_id(gap.record_id).status == 'active'


@pytest.mark.parametrize('field,value', [(field, value) for field in ('source', 'recall')
    for value in (None, -1, False, '0', 0.0)])
def test_raw_budget_counter_invalid_values_are_unknown_and_never_reviewed(replay_case, field, value):
    runtime, scope, _, _, _, _, judges, options = replay_case
    options['source_budget' if field == 'source' else 'recall_budget'] = value
    report, findings = repair.collect_semantic_gap_repair_findings(runtime, scope=scope, max_new=2)
    assert report['replay_count'] == 1 and report['provider_calls'] == 0
    assert judges == [] and findings == []
    packets = runtime.store.list_records_by_meta_value(kinds=['evaluation_packet'], scope=scope,
        meta_key='report_type', meta_value='semantic_gap_replay_capture', limit=10)
    assert len(packets) == 1
    key = 'source_budget_exhausted' if field == 'source' else 'recall_budget_exhausted'
    assert packets[0].content['retrieval_state'][key] is None


@pytest.mark.parametrize('mutation', ['source_missing', 'drops_missing', 'drops_none', 'drops_list', 'drops_truncated'])
def test_missing_or_incomplete_raw_budget_evidence_never_becomes_zero(replay_case, monkeypatch, mutation):
    runtime, scope, _, _, _, _, judges, _ = replay_case
    native = runtime.memory.recall
    def altered(**kwargs):
        bundle = native(**kwargs)
        engine = bundle.explanation['engine_diagnostics']
        if mutation == 'source_missing':
            del engine['source_budget_exhausted']
        elif mutation == 'drops_missing':
            del engine['drops']
        elif mutation == 'drops_none':
            engine['drops'] = None
        elif mutation == 'drops_list':
            engine['drops'] = []
        else:
            engine['drops'] = {'other_' + str(i): 10 for i in range(8)}
        return bundle
    monkeypatch.setattr(runtime.memory, 'recall', altered)
    report, findings = repair.collect_semantic_gap_repair_findings(runtime, scope=scope, max_new=2)
    assert report['replay_count'] == 1 and report['provider_calls'] == 0
    assert judges == [] and findings == []


def test_native_sparse_empty_drops_is_a_legitimate_zero_and_can_be_repaired(replay_case, monkeypatch):
    runtime, scope, _, _, _, _, judges, _ = replay_case
    native = runtime.memory.recall
    def sparse(**kwargs):
        bundle = native(**kwargs)
        bundle.explanation['engine_diagnostics']['drops'] = {}
        return bundle
    monkeypatch.setattr(runtime.memory, 'recall', sparse)
    report, findings = repair.collect_semantic_gap_repair_findings(runtime, scope=scope, max_new=2)
    assert report['replay_count'] == report['provider_calls'] == len(judges) == len(findings) == 1
    assert len(_ingest_verified_findings(runtime, findings=findings, scope=scope)['resolved_record_ids']) == 1
