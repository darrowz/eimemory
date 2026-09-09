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


@pytest.mark.parametrize('unattested_labeler', ['codex', 'delegated_ai'])
def test_automatic_review_cannot_pass_as_an_operator_label(case, unattested_labeler):
    from eimemory.evaluation.production_query_dataset import accept_pending_production_query
    runtime, gold, pending_id, _, _ = case
    with pytest.raises(ValueError, match='trusted operator labeler required'):
        accept_pending_production_query(runtime, pending_record_id=pending_id,
            query_features={'terms': ['archive', 'project', 'reports'], 'intent': 'memory recall'},
            labels=[{'record_ref': gold.record_id, 'grade': 3}], labeler=unattested_labeler, operator_scope=BASE,
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


@pytest.mark.parametrize('column,value', [('user_id', 'other'), ('channel', 'hermes'),
    ('source_ids_json', '["hermes"]')])
def test_foreign_decision_provenance_is_not_read(case, column, value):
    runtime, _, _, _, _ = case
    runtime.store.sqlite.conn.execute(
        f'UPDATE proactive_decisions SET {column}=?, acceptance_generated=1', (value,))
    runtime.store.sqlite.conn.commit()
    result = review(case)['reviews'][0]
    receipt = runtime.store.get_by_id(result['record_id'])
    assert result['disposition'] == 'evidence_insufficient'
    assert receipt.content['facts']['decision_provenance'] is None
    assert result['reasons'] == ['pending_capture_decision_missing']


@pytest.fixture
def positive_grant(case, monkeypatch):
    from types import SimpleNamespace
    runtime, gold, pending_id, path, packet = case
    packet['actions'] = ['review_pending', 'accept_positive_labels']
    path.write_text(json.dumps(packet))
    calls = []
    response = {'query_features': {'terms': ['archive', 'project', 'reports'], 'intent': 'memory recall'},
        'labels': [{'record_ref': gold.record_id, 'grade': 3,
            'quote': 'Archive completed reports in the project folder.',
            'reason': 'The question asks where reports go; the record gives the project folder as the destination.'}]}
    class Client:
        timeout_seconds = 9
        def complete(self, **kwargs):
            calls.append(kwargs)
            return SimpleNamespace(text=json.dumps(response), model_id='test/reviewer')
        def close(self): pass
    monkeypatch.setattr('eimemory.retrieval.caller_assistance.configured_client', Client)
    monkeypatch.setenv('EIMEMORY_RECALL_EXPECTED_MODEL', 'test/reviewer')
    return response, calls


def test_new_delegated_positive_creates_valid_gold_once(case, positive_grant):
    from eimemory.evaluation.production_query_dataset import accepted_production_query_validation_error
    from eimemory.evaluation.dataset_authority import validate_case_authority
    runtime, gold, _, _, _ = case
    first = review(case)
    item = first['reviews'][0]
    assert item['disposition'] == 'accepted' and item['natural_gold_created'] is True
    accepted = runtime.store.get_by_id(item['accepted_record_ids'][0])
    assert not accepted_production_query_validation_error(runtime, accepted, exact_scope=EXACT, channel='codex')
    assert not validate_case_authority(runtime, accepted.content['case'])
    from eimemory.evaluation.real_query_gate import _freeze_real_query_case, _hydrate_real_query_labels
    frozen, reasons = _freeze_real_query_case(accepted.content['case'], index=0, base_scope=BASE)
    assert not reasons
    assert _hydrate_real_query_labels(runtime, [frozen])[0]
    label = accepted.content['case']['labels'][0]
    assert label['provenance']['labeler'] == 'delegated_ai'
    evidence = runtime.store.get_by_id(label['provenance']['evidence_ref'])
    assert evidence.content['delegated_authority']['model_id'] == 'test/reviewer'
    assert 'operator_packet_evidence' not in evidence.content
    second = review(case)
    assert second['created'] == 0 and len(positive_grant[1]) == 1
    assert second['reviews'] == first['reviews']
    gold.content['fact'] = 'Use the unrelated folder.'
    runtime.store.rewrite(gold, previous_scope=gold.scope)
    assert validate_case_authority(runtime, accepted.content['case']) == 'delegated_label_evidence_stale'


@pytest.mark.parametrize('failure', ['irrelevant', 'invented_quote', 'model', 'exception'])
def test_semantic_negative_never_creates_gold(case, positive_grant, monkeypatch, failure):
    response, calls = positive_grant
    if failure == 'irrelevant': response['labels'] = []
    elif failure == 'invented_quote': response['labels'][0]['quote'] = 'Put completed reports in the public trash.'
    elif failure == 'model': monkeypatch.setenv('EIMEMORY_RECALL_EXPECTED_MODEL', 'different/model')
    else:
        monkeypatch.setattr('eimemory.retrieval.caller_assistance.configured_client', lambda: None)
    result = review(case)['reviews'][0]
    assert result['disposition'] in {'rejected', 'evidence_insufficient'}
    assert result['natural_gold_created'] is False
    assert case[0].store.sqlite.conn.execute("SELECT COUNT(*) FROM records WHERE source='eimemory.production_recall.accepted_case'").fetchone()[0] == 0


def test_semantic_empty_selection_needs_no_positive_query_features(case, positive_grant):
    response, _ = positive_grant
    response.update(labels=[], query_features={})
    result = review(case)['reviews'][0]
    assert result['disposition'] == 'rejected'
    assert result['reasons'] == ['semantic_answer_not_supported']


def test_normal_worker_collects_and_reviews_with_configured_grant(case, positive_grant, monkeypatch):
    from eimemory.cli.l1_worker import drain_l1
    runtime, _, pending_id, path, _ = case
    runtime.store.sqlite.conn.execute('DELETE FROM records WHERE record_id=?', (pending_id,))
    runtime.store.sqlite.conn.commit()
    monkeypatch.setenv('EIMEMORY_CODEX_REVIEW_DELEGATION', str(path))
    report = drain_l1(root=str(runtime.store.root), limit=1)
    assert report['delegated_review']['dispositions'] == {'accepted': 1}
    again = drain_l1(root=str(runtime.store.root), limit=1)
    assert again['delegated_review']['created'] == 0
    assert len(positive_grant[1]) == 1



def test_semantic_review_cannot_commit_after_evidence_changes(case, positive_grant, monkeypatch):
    from eimemory.evaluation import delegated_label_authority as authority
    actual = authority.semantic_review
    runtime, gold, _, _, _ = case
    def change(*args):
        result = actual(*args)
        gold.content['fact'] = 'No relevant archival destination is known.'
        runtime.store.rewrite(gold, previous_scope=gold.scope)
        return result
    monkeypatch.setattr(authority, 'semantic_review', change)
    with pytest.raises(ValueError, match='review_evidence_changed'):
        review(case)
    assert runtime.store.sqlite.conn.execute("SELECT COUNT(*) FROM records WHERE source='eimemory.production_recall.accepted_case'").fetchone()[0] == 0


def test_delegated_signature_is_required_by_dataset_reader(case, positive_grant):
    from eimemory.evaluation.dataset_authority import validate_case_authority
    runtime = case[0]
    result = review(case)['reviews'][0]
    accepted = runtime.store.get_by_id(result['accepted_record_ids'][0])
    label = accepted.content['case']['labels'][0]
    evidence = runtime.store.get_by_id(label['provenance']['evidence_ref'])
    evidence.content['delegated_authority']['delegation']['delegator'] = 'other'
    runtime.store.rewrite(evidence, previous_scope=evidence.scope)
    assert validate_case_authority(runtime, accepted.content['case']) in {
        'label_evidence_identity_invalid', 'delegated_label_signature_invalid'}


def test_temporary_reviewer_failure_is_deferred_then_retried(case, positive_grant, monkeypatch):
    from eimemory.evaluation import delegated_recall_review as reviews
    from eimemory.retrieval import caller_assistance
    real = caller_assistance.configured_client
    monkeypatch.setattr(caller_assistance, 'configured_client', lambda: None)
    first = review(case)['reviews'][0]
    monkeypatch.setattr(caller_assistance, 'configured_client', real)
    assert review(case)['created'] == 0
    now = datetime.now(timezone.utc)
    class Later(datetime):
        @classmethod
        def now(cls, tz=None): return now + timedelta(minutes=6)
    monkeypatch.setattr(reviews, 'datetime', Later)
    result = review(case)
    assert result['new_accepted_count'] == 1
    assert result['reviews'][0]['record_id'] != first['record_id']
    assert case[0].store.get_by_id(first['record_id']).content['disposition'] == 'evidence_insufficient'



@pytest.mark.parametrize('missing', ['host', 'provenance', 'candidate', 'maintenance', 'foreign_user', 'foreign_channel'])
def test_positive_grant_cannot_override_capture_boundaries(case, positive_grant, missing):
    runtime, gold, _, _, _ = case
    conn = runtime.store.sqlite.conn
    if missing == 'host': conn.execute('DELETE FROM proactive_query_input_vault')
    elif missing == 'provenance': conn.execute('UPDATE proactive_decisions SET acceptance_generated=NULL')
    elif missing == 'candidate':
        gold.status = 'expired'; runtime.store.rewrite(gold, previous_scope=gold.scope)
    elif missing == 'maintenance': conn.execute('UPDATE proactive_decisions SET acceptance_generated=1')
    elif missing == 'foreign_user': conn.execute("UPDATE proactive_decisions SET user_id='other'")
    else: conn.execute("UPDATE proactive_decisions SET channel='hermes'")
    conn.commit()
    result = review(case)
    assert result['new_accepted_count'] == 0 and result['model_calls'] == 0
    assert not positive_grant[1]


def test_delegation_locator_preserves_real_user_message_identity(case, positive_grant):
    _, _, _, path, packet = case
    packet['authorization_ref'].update(source_message_id=63467, source_store='hermes_user_history')
    path.write_text(json.dumps(packet))
    result = review(case)['reviews'][0]
    receipt = case[0].store.get_by_id(result['record_id'])
    assert receipt.content['authorization_ref']['source_message_id'] == 63467


def test_foreign_source_pending_does_not_block_authorized_review(case, positive_grant):
    runtime = case[0]
    foreign = RecordEnvelope.create(kind='evaluation_packet', title='Other source pending',
        source='eimemory.production_recall.pending_case', source_id='default', scope=EXACT,
        content={'capture_ref':'foreign-decision', 'acceptance_generated':True})
    runtime.store.append(foreign)
    result = review(case)
    assert result['reviewed_count'] == 1 and result['new_accepted_count'] == 1
    assert runtime.store.get_by_id(foreign.record_id).content == foreign.content



def test_configured_collector_does_not_project_foreign_sources(case, positive_grant, monkeypatch):
    from eimemory.evaluation.delegated_recall_review import collect_and_review_configured
    runtime, _, pending_id, path, _ = case
    runtime.store.sqlite.conn.execute('DELETE FROM records WHERE record_id=?', (pending_id,))
    runtime.store.sqlite.conn.execute("UPDATE proactive_decisions SET source_ids_json='[\"default\"]'")
    runtime.store.sqlite.conn.commit()
    monkeypatch.setenv('EIMEMORY_CODEX_REVIEW_DELEGATION', str(path))
    result = collect_and_review_configured(runtime)
    assert result['collection']['pending_record_ids'] == []
    assert result['new_accepted_count'] == 0



def test_delegated_promotion_rolls_back_all_records_on_write_failure(case, positive_grant, monkeypatch):
    runtime = case[0]
    actual = runtime.store.sqlite.upsert
    def fail(record, **kwargs):
        if record.source == 'eimemory.production_recall.accepted_case':
            raise RuntimeError('injected accepted write failure')
        return actual(record, **kwargs)
    monkeypatch.setattr(runtime.store.sqlite, 'upsert', fail)
    with pytest.raises(RuntimeError, match='injected accepted write failure'):
        review(case)
    assert runtime.store.sqlite.conn.execute("SELECT COUNT(*) FROM records WHERE source IN ('eimemory.production_recall.label_evidence','eimemory.production_recall.accepted_case','eimemory.production_recall.delegated_review')").fetchone()[0] == 0
