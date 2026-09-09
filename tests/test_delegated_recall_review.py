from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json

import pytest

from eimemory.api.runtime import Runtime
from eimemory.models.records import RecordEnvelope, ScopeRef
from eimemory.evaluation.production_query_dataset import collect_pending_production_queries


BASE = ScopeRef('default', 'hongtu', 'embodied', 'darrow')
EXACT = ScopeRef('default', 'hongtu', 'embodied::channel::codex', 'darrow')


@pytest.fixture
def case(tmp_path, monkeypatch, trusted_dataset_path_ancestors):
    monkeypatch.setenv('EIMEMORY_EVIDENCE_RECEIPT_HMAC_KEY', 'test-delegation-key-not-production')
    monkeypatch.setenv('EIMEMORY_CAPTURE_ORIGINAL_QUERY', '1')
    monkeypatch.setattr('eimemory.evaluation.delegated_recall_review._local_principal', lambda: 'darrow')
    runtime = Runtime.create(root=tmp_path / 'runtime')
    gold = RecordEnvelope.create(kind='memory', title='Routing destination',
        content={'fact': 'Archive completed reports in the project folder.'},
        source='codex.memory', source_id='codex', scope=EXACT)
    runtime.store.append(gold)
    query = 'Where should completed project reports be archived?'
    digest = sha256(query.encode()).hexdigest()
    from eimemory.retrieval.query_identity import effective_query_digest
    runtime.store.record_proactive_decision({
        'decision_id': 'decision-review', 'channel': 'codex', 'scope': asdict(EXACT),
        'source_key': sha256(b'codex').hexdigest(), 'source_ids': ['codex'],
        'session_id': 'host-session', 'turn_id': 'turn', 'query_id': 'query',
        'query_digest': digest, 'effective_query_digest': effective_query_digest('memory.recall', query),
        'task_type': 'memory.recall', 'policy_version': 'test.v1',
        'release_identity': {'release_commit': 'a' * 40, 'release_version': '1.13.2',
                             'deployment_receipt_id': 'receipt', 'release_session_id': 'receipt'},
        'release_bound': True, 'control_cohort': False, 'acceptance_generated': False,
    }, [{'citation': 'M1', 'record_id': gold.record_id, 'source_id': 'codex',
         'confidence': .9, 'order': 0, 'render_digest': 'd' * 64}], [])
    from eimemory.evaluation.query_input_vault import capture_query_input
    capture_query_input(runtime, decision_id='decision-review', query=query,
        effective_query=query, host_query=query,
        explanation={'retrieval_status': 'evidence_found', 'task_context': {}})
    pending_id = collect_pending_production_queries(runtime, scope=BASE, channel='codex')['pending_record_ids'][0]
    packet = {'schema': 'production_recall_review_delegation.v1',
        'scope': asdict(EXACT), 'channel': 'codex', 'source_id': 'codex',
        'delegator': 'darrow', 'delegate': 'codex', 'actions': ['review_pending'],
        'authorization_ref': {'kind': 'user_instruction', 'session_id': 'host-session',
                              'message_digest': digest},
        'expires_at': (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()}
    path = tmp_path / 'delegation.json'
    path.write_text(json.dumps(packet)); path.chmod(0o600)
    yield runtime, gold, pending_id, path, packet
    runtime.close()


def review(case, **kwargs):
    from eimemory.evaluation.delegated_recall_review import review_pending_production_queries
    runtime, _, _, path, _ = case
    return review_pending_production_queries(runtime, scope=kwargs.pop('scope', BASE),
        channel=kwargs.pop('channel', 'codex'), delegation_path=path, **kwargs)


def test_delegated_review_is_explicit_non_gold_and_idempotent(case):
    runtime, _, pending_id, _, _ = case
    first = review(case)
    second = review(case)
    assert first['created'] == 1 and second['created'] == 0
    assert first['reviews'] == second['reviews']
    result = first['reviews'][0]
    assert result['disposition'] == 'pending_independent_review'
    assert result['reviewer'] == 'codex' and result['delegator'] == 'darrow'
    assert result['natural_gold_created'] is False
    assert runtime.store.get_by_id(pending_id).status == 'active'
    assert runtime.store.sqlite.conn.execute("SELECT COUNT(*) FROM records WHERE source='eimemory.production_recall.accepted_case'").fetchone()[0] == 0


@pytest.mark.parametrize('mutation,expected', [
    ('unknown', 'capture_provenance_unknown'),
    ('vault_missing', 'original_query_input_boundary_mismatch'),
    ('empty_failure', 'retrieval_unavailable'),
    ('candidate_missing', 'returned_candidate_unavailable'),
])
def test_review_preserves_insufficient_observations(case, mutation, expected):
    runtime, gold, pending_id, _, _ = case
    c = runtime.store.sqlite.conn
    if mutation == 'unknown':
        c.execute('UPDATE proactive_decisions SET acceptance_generated=NULL')
    elif mutation == 'vault_missing':
        c.execute('DELETE FROM proactive_query_input_vault')
    elif mutation == 'empty_failure':
        c.execute("UPDATE proactive_query_input_vault SET retrieval_status='unavailable'")
        c.execute('DELETE FROM proactive_decision_items')
        pending = runtime.store.get_by_id(pending_id)
        pending.content['candidate_refs'] = []; pending.evidence = []
        runtime.store.rewrite(pending, previous_scope=pending.scope)
    else:
        gold.status = 'expired'; runtime.store.rewrite(gold, previous_scope=gold.scope)
    c.commit()
    result = review(case)['reviews'][0]
    assert result['disposition'] == 'evidence_insufficient'
    assert expected in result['reasons']
    assert runtime.store.get_by_id(pending_id).status == 'active'


def test_maintenance_is_never_accepted_even_with_candidates(case):
    runtime, _, _, _, _ = case
    runtime.store.sqlite.conn.execute('UPDATE proactive_decisions SET acceptance_generated=1')
    runtime.store.sqlite.conn.commit()
    result = review(case)['reviews'][0]
    assert result['disposition'] == 'maintenance'
    assert 'maintenance_capture_not_natural' in result['reasons']
    assert result['natural_gold_created'] is False


@pytest.mark.parametrize('changed', ['user', 'channel', 'delegate', 'actions', 'expired', 'writable'])
def test_delegation_rejects_unauthorized_identity_before_writes(case, changed):
    runtime, _, _, path, packet = case
    if changed == 'user': packet['scope']['user_id'] = 'other'
    elif changed == 'channel': packet['channel'] = 'hermes'
    elif changed == 'delegate': packet['delegate'] = 'release_operator'
    elif changed == 'actions': packet['actions'] = ['accept_gold']
    elif changed == 'expired': packet['expires_at'] = '2020-01-01T00:00:00+00:00'
    path.write_text(json.dumps(packet))
    if changed == 'writable': path.chmod(0o666)
    before = runtime.store.sqlite.conn.execute('SELECT COUNT(*) FROM records').fetchone()[0]
    with pytest.raises(ValueError): review(case)
    assert runtime.store.sqlite.conn.execute('SELECT COUNT(*) FROM records').fetchone()[0] == before


def test_wrong_candidate_reference_is_rejected_without_a_label(case):
    runtime, _, pending_id, _, _ = case
    pending = runtime.store.get_by_id(pending_id)
    pending.content['candidate_refs'] = ['invented']; pending.evidence = ['invented']
    runtime.store.rewrite(pending, previous_scope=pending.scope)
    result = review(case)['reviews'][0]
    assert result['disposition'] == 'rejected'
    assert 'pending_capture_decision_mismatch' in result['reasons']


def test_review_receipt_tampering_fails_verification(case):
    from eimemory.evaluation.delegated_recall_review import verify_delegated_review
    runtime, _, _, _, _ = case
    result = review(case)['reviews'][0]
    record = runtime.store.get_by_id(result['record_id'])
    assert verify_delegated_review(record, scope=EXACT)['disposition'] == 'pending_independent_review'
    record.content['disposition'] = 'accepted'
    runtime.store.rewrite(record, previous_scope=record.scope)
    with pytest.raises(ValueError, match='signature'):
        verify_delegated_review(runtime.store.get_by_id(record.record_id), scope=EXACT)


def test_quarantine_review_preserves_original_evidence(case):
    runtime, _, pending_id, _, _ = case
    pending = runtime.store.get_by_id(pending_id); pending.status = 'quarantined'
    pending.meta['quarantine_reason'] = 'label_candidate_boundary_invalid'
    runtime.store.rewrite(pending, previous_scope=pending.scope)
    result = review(case)['reviews'][0]
    assert result['disposition'] == 'rejected'
    assert result['reasons'] == ['quarantined_evidence']
    assert runtime.store.get_by_id(pending_id).status == 'quarantined'


def test_changed_evidence_appends_new_review_and_keeps_previous(case):
    runtime, gold, _, _, _ = case
    first = review(case)['reviews'][0]
    gold.status = 'expired'; runtime.store.rewrite(gold, previous_scope=gold.scope)
    second = review(case)['reviews'][0]
    assert first['record_id'] != second['record_id']
    assert runtime.store.get_by_id(first['record_id']).content['disposition'] == 'pending_independent_review'
    assert second['disposition'] == 'evidence_insufficient'


def test_collector_runs_delegated_review_through_cli(case, monkeypatch, capsys):
    from eimemory.cli.main import main
    runtime, _, pending_id, path, _ = case
    monkeypatch.setenv('EIMEMORY_ROOT', str(runtime.store.root))
    args = ['eval', 'production-query', 'collect', '--channel', 'codex',
            '--scope-agent', 'hongtu', '--scope-workspace', 'embodied', '--scope-user', 'darrow',
            '--review-delegation-json', str(path)]
    assert main(args) == 0
    result = json.loads(capsys.readouterr().out)
    assert result['pending_record_ids'] == [pending_id]
    assert result['delegated_review']['reviewed_count'] == 1
    assert main(args) == 0
    assert json.loads(capsys.readouterr().out)['delegated_review']['created'] == 0


def test_unknown_codex_provenance_cannot_use_operator_acceptance(case):
    from eimemory.evaluation.production_query_dataset import accept_pending_production_query
    runtime, gold, pending_id, _, _ = case
    runtime.store.sqlite.conn.execute('UPDATE proactive_decisions SET acceptance_generated=NULL')
    runtime.store.sqlite.conn.commit()
    with pytest.raises(ValueError, match='pending_capture_provenance_unknown'):
        accept_pending_production_query(runtime, pending_record_id=pending_id,
            query_features={'terms': ['archive', 'project', 'reports'], 'intent': 'memory recall'},
            labels=[{'record_ref': gold.record_id, 'grade': 3}], labeler='operator', operator_scope=BASE,
            label_packet_evidence={'schema': 'secure_dataset_fingerprint.v1',
                'digest': 'd' * 64, 'device': 1, 'inode': 1, 'size': 100})


def test_automatic_review_cannot_pass_as_an_operator_label(case):
    from eimemory.evaluation.production_query_dataset import accept_pending_production_query
    runtime, gold, pending_id, _, _ = case
    with pytest.raises(ValueError, match='trusted operator labeler required'):
        accept_pending_production_query(runtime, pending_record_id=pending_id,
            query_features={'terms': ['archive', 'project', 'reports'], 'intent': 'memory recall'},
            labels=[{'record_ref': gold.record_id, 'grade': 3}], labeler='codex', operator_scope=BASE,
            label_packet_evidence={})


def test_review_only_recognizes_existing_valid_operator_gold(case):
    from eimemory.evaluation.production_query_dataset import accept_pending_production_query
    runtime, gold, pending_id, _, _ = case
    accepted = accept_pending_production_query(runtime, pending_record_id=pending_id,
        query_features={'terms': ['archive', 'project', 'reports'], 'intent': 'memory recall'},
        labels=[{'record_ref': gold.record_id, 'grade': 3}], labeler='operator', operator_scope=BASE,
        label_packet_evidence={'schema': 'secure_dataset_fingerprint.v1',
            'digest': 'd' * 64, 'device': 1, 'inode': 1, 'size': 100})
    result = review(case)['reviews'][0]
    assert result['disposition'] == 'accepted'
    assert result['accepted_record_ids'] == [accepted['record_id']]
    assert result['natural_gold_created'] is False
    gold.status = 'expired'; runtime.store.rewrite(gold, previous_scope=gold.scope)
    invalid = review(case)['reviews'][0]
    assert invalid['disposition'] == 'evidence_insufficient'
    assert 'existing_operator_authority_invalid' in invalid['reasons']


def test_delegation_requires_the_actual_local_principal(case, monkeypatch):
    monkeypatch.setattr('eimemory.evaluation.delegated_recall_review._local_principal', lambda: 'other')
    with pytest.raises(ValueError, match='review_delegation_authority_invalid'):
        review(case)
