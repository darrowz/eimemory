from copy import deepcopy
from hashlib import sha256
import json

import pytest

from test_explicit_recall_capture import setup, BASE, call
from eimemory.adapters.runtime.channel import resolve_channel_scope
from eimemory.evaluation.explicit_recall import (
    CAPTURE_SOURCE, INTENT_SOURCE, load_explicit_capture, load_explicit_capture_review_input,
)


def private_call(service, **kwargs):
    return call(service, task_context={'source_ids': ['shared']},
                explicit_request={'session_id': 'real-host-session', 'request_id': 'real-tool-call',
                                  'acceptance_generated': False, 'capture_mode': 'private'}, **kwargs)


def enable(monkeypatch, channel='codex'):
    monkeypatch.setenv('EIMEMORY_CAPTURE_ORIGINAL_QUERY', '1')
    monkeypatch.setenv('EIMEMORY_CAPTURE_QUERY_SCOPES', json.dumps([
        dict(resolve_channel_scope(channel, BASE), channel=channel, source_id='shared')]))


def test_private_original_and_real_result_are_bound_but_never_exported(setup, monkeypatch):
    runtime, service, gold = setup
    enable(monkeypatch)
    query = 'PRIVATE query marker for Orchid service connection'
    result = private_call(service, query=query, task_type=query)
    assert result['ok'] and result['capture']['input_status'] == 'captured'
    capture_id = result['capture']['record_id']
    exact = resolve_channel_scope('codex', BASE)
    capture = load_explicit_capture(runtime, capture_id, scope=exact)
    assert capture.content['request']['source_ids'] == ['shared']
    assert capture.content['request']['query_digest'] == sha256(query.encode()).hexdigest()
    records = runtime.store.list_records(kinds=['evaluation_packet'], scope=exact, limit=100)
    assert {r.source for r in records} >= {CAPTURE_SOURCE, INTENT_SOURCE}
    assert all(query not in json.dumps(r.to_dict()) for r in records)
    assert any(i['record_id'] == gold.record_id for i in result['bundle']['items'])
    recovered = load_explicit_capture_review_input(runtime, capture_id, scope=exact)
    assert recovered['query'] == query
    assert recovered['result'] == {k: v for k, v in result.items() if k != 'capture'}
    assert private_call(service, query=query, task_type=query) == result
    with pytest.raises(ValueError, match='identity conflict'):
        private_call(service, query='different query')
    assert runtime.store.sqlite.conn.execute('SELECT COUNT(*) FROM proactive_decisions').fetchone()[0] == 0


@pytest.mark.parametrize('policy,status', [
    ('disabled', 'disabled'), ('missing', 'scope_not_enabled'), ('[]', 'scope_not_enabled'),
    ('malformed', 'scope_policy_invalid'), ('other_user', 'scope_not_enabled'),
    ('other_source', 'scope_not_enabled'), ('other_channel', 'scope_not_enabled')])
def test_private_opt_in_is_exact_and_never_blocks_real_retrieval(setup, monkeypatch, policy, status):
    runtime, service, gold = setup
    enable(monkeypatch)
    if policy == 'disabled':
        monkeypatch.setenv('EIMEMORY_CAPTURE_ORIGINAL_QUERY', '0')
    elif policy == 'missing':
        monkeypatch.delenv('EIMEMORY_CAPTURE_QUERY_SCOPES')
    elif policy.startswith('other_'):
        entry = dict(resolve_channel_scope('codex', BASE), channel='codex', source_id='shared')
        entry[{'other_user': 'user_id', 'other_source': 'source_id', 'other_channel': 'channel'}[policy]] = 'other'
        monkeypatch.setenv('EIMEMORY_CAPTURE_QUERY_SCOPES', json.dumps([entry]))
    else:
        monkeypatch.setenv('EIMEMORY_CAPTURE_QUERY_SCOPES', policy)
    query = 'PRIVATE marker Orchid service connection'
    result = private_call(service, query=query, task_type=query)
    assert result['ok'] and any(i['record_id'] == gold.record_id for i in result['bundle']['items'])
    assert result['capture']['input_status'] == status
    assert private_call(service, query=query, task_type=query) == result
    capture_id = result['capture']['record_id']
    exact = resolve_channel_scope('codex', BASE)
    with pytest.raises(ValueError, match='input_unavailable'):
        load_explicit_capture_review_input(runtime, capture_id, scope=exact)
    assert query not in json.dumps(load_explicit_capture(runtime, capture_id, scope=exact).to_dict())


@pytest.mark.parametrize('mutation', ['query', 'result', 'request', 'expired', 'deleted'])
def test_private_input_tampering_or_expiry_cannot_replace_original(setup, monkeypatch, mutation):
    runtime, service, _ = setup
    enable(monkeypatch)
    result = private_call(service)
    capture_id = result['capture']['record_id']
    with runtime.store.locked() as db:
        row = db.execute('SELECT payload FROM explicit_query_input_vault WHERE capture_id=?', (capture_id,)).fetchone()
        if mutation == 'expired':
            db.execute("UPDATE explicit_query_input_vault SET created_at=datetime('now','-31 days')")
        elif mutation == 'deleted':
            db.execute('DELETE FROM explicit_query_input_vault')
        else:
            payload = json.loads(row['payload'])
            if mutation == 'query': payload['query'] = 'forged'
            elif mutation == 'result': payload['result']['bundle']['items'] = []
            else: payload['request']['source_ids'] = ['other']
            serialized = json.dumps(payload, sort_keys=True)
            db.execute('UPDATE explicit_query_input_vault SET payload=?, input_digest=?',
                       (serialized, sha256(serialized.encode()).hexdigest()))
        db.commit()
    with pytest.raises(ValueError, match='digest_mismatch'):
        load_explicit_capture_review_input(runtime, capture_id, scope=resolve_channel_scope('codex', BASE))


@pytest.mark.parametrize('context', [
    {'scope_aliases': ['other']}, {'source_ids': ['shared'], 'exact_scope_only': False},
    {'source_ids': []}, {'source_ids': '*'}, {'source_ids': ['*']},
    {'source_ids': ['shared', 'SHARED']}, {'source_ids': [1]},
])
def test_explicit_context_only_accepts_bounded_source_filter_before_retrieval(setup, monkeypatch, context):
    runtime, service, _ = setup
    monkeypatch.setattr(runtime.memory, 'recall', lambda **kwargs: pytest.fail('unexpected retrieval'))
    with pytest.raises(ValueError):
        call(service, task_context=context)


def test_signed_source_selector_is_applied_and_cannot_change_on_retry(setup):
    runtime, service, gold = setup
    result = call(service, task_context={'source_ids': ['other']})
    assert result['ok'] and result['bundle']['items'] == []
    capture = load_explicit_capture(runtime, result['capture']['record_id'], scope=resolve_channel_scope('codex', BASE))
    assert capture.content['request']['source_ids'] == ['other']
    with pytest.raises(ValueError, match='identity conflict'):
        call(service, task_context={'source_ids': ['shared']})


def test_untrusted_result_from_wrong_source_is_never_attested(setup, monkeypatch):
    runtime, service, _ = setup
    original = service._prefetch_result
    def ignore_filter(**kwargs):
        kwargs['task_context'] = None
        return original(**kwargs)
    monkeypatch.setattr(service, '_prefetch_result', ignore_filter)
    result = call(service, task_context={'source_ids': ['other']})
    assert result['ok'] is False
    capture = load_explicit_capture(runtime, result['capture']['record_id'], scope=resolve_channel_scope('codex', BASE))
    assert not capture.content['references']


def test_recall_bundle_payload_substitution_is_rejected_against_physical_record(setup, monkeypatch):
    runtime, service, gold = setup
    from eimemory.models.records import RecallBundle
    changed = deepcopy(gold)
    changed.summary = 'Forged service address'
    monkeypatch.setattr(runtime.memory, 'recall', lambda **kwargs: RecallBundle(
        items=[changed], rules=[], reflections=[], confidence=1, next_action_hint=''))
    assert call(service)['ok'] is False


def test_real_compact_truncation_and_evidence_excerpt_can_be_captured(setup, monkeypatch):
    runtime, service, gold = setup
    from eimemory.models.records import RecallBundle
    gold.summary = 'Orchid uses rpc.example:7443. ' * 25
    runtime.store.append(gold)
    # A server-selected fragment is an additional compact projection field;
    # it must not make the otherwise authoritative envelope fail capture.
    from eimemory.retrieval.evidence_fragments import evidence_fragments
    from eimemory.retrieval.postgres_vector import candidate_record_keyword_text
    from dataclasses import asdict
    fragment = evidence_fragments(candidate_record_keyword_text(gold, max_text_chars=16000))[0]
    bundle = RecallBundle(items=[gold], rules=[], reflections=[], confidence=1, next_action_hint='',
        explanation={'relevance_selector': {'status': 'evidence_found', 'collection_complete': True,
            'scored': [{'record_id': gold.record_id, 'source_id': gold.source_id, 'scope': asdict(gold.scope),
                        'admitted': True, 'fragment_id': fragment['id']}]}})
    monkeypatch.setattr(runtime.memory, 'recall', lambda **kwargs: bundle)
    result = call(service)
    assert result['ok']
    assert result['bundle']['items'][0]['evidence_excerpt'] == fragment['text']
    captured = load_explicit_capture(runtime, result['capture']['record_id'], scope=resolve_channel_scope('codex', BASE))
    assert captured.content['retrieval_state']['collection_complete'] is True


def test_exact_intent_can_resume_after_interruption_without_rewriting_old_capture(setup):
    runtime, service, _ = setup
    result = call(service)
    capture_id = result['capture']['record_id']
    with runtime.store.locked() as db:
        db.execute('DELETE FROM records WHERE record_id=?', (capture_id,))
        db.commit()
    resumed = call(service)
    assert resumed['ok'] and resumed['capture']['record_id'] == capture_id
    assert call(service) == resumed


@pytest.mark.parametrize('stage', ['intent', 'capture', 'release', 'private_cache'])
def test_optional_capture_failure_preserves_the_successful_real_read(setup, monkeypatch, stage):
    runtime, service, gold = setup
    enable(monkeypatch)
    from eimemory.evaluation import explicit_recall as explicit, explicit_input_vault as vault
    def fail(*args, **kwargs):
        raise RuntimeError('PRIVATE storage failure detail')
    if stage == 'private_cache':
        monkeypatch.setattr(vault, '_save', fail)
    elif stage == 'release':
        monkeypatch.setattr(service, '_proactive_release', fail)
    else:
        original = explicit._insert_once
        def insert(runtime, record):
            if record.source == (INTENT_SOURCE if stage == 'intent' else CAPTURE_SOURCE):
                return fail()
            return original(runtime, record)
        monkeypatch.setattr(explicit, '_insert_once', insert)
    result = private_call(service)
    assert result['ok'] and any(i['record_id'] == gold.record_id for i in result['bundle']['items'])
    assert 'PRIVATE storage failure detail' not in json.dumps(result)
    if stage == 'private_cache':
        assert result['capture']['input_status'] == 'capture_unavailable'
    else:
        assert result['explicit_capture']['status'] == 'not_captured'


def test_private_cache_begin_failure_does_not_abort_retrieval(setup, monkeypatch):
    runtime, service, _ = setup
    enable(monkeypatch)
    from eimemory.evaluation.explicit_input_vault import capture_explicit_input
    from eimemory.models.records import ScopeRef
    import sqlite3
    with runtime.store.locked() as db:
        db.conn.set_authorizer(lambda action, arg1, *rest:
            sqlite3.SQLITE_DENY if action == sqlite3.SQLITE_TRANSACTION and arg1 == 'BEGIN' else sqlite3.SQLITE_OK)
        try:
            status = capture_explicit_input(runtime, capture_id='fixture', scope=ScopeRef.from_dict(resolve_channel_scope('codex', BASE)),
                request={'channel': 'codex', 'source_ids': ['shared']}, query='private', result={'ok': True})
        finally:
            db.conn.set_authorizer(None)
    assert status['status'] == 'capture_unavailable'


@pytest.mark.parametrize('foreign', ['source', 'user', 'channel'])
def test_auxiliary_evidence_without_exact_authority_is_never_delivered_or_signed(setup, monkeypatch, foreign):
    runtime, service, gold = setup
    from eimemory.models.records import RecallBundle, ScopeRef
    other = deepcopy(gold)
    other.record_id = 'mem_' + 'f' * 32
    other.title = 'PRIVATE foreign auxiliary marker'
    if foreign == 'source': other.source_id = 'other'
    elif foreign == 'user': other.scope = ScopeRef.from_dict({**BASE, 'user_id': 'other'})
    else: other.scope = ScopeRef.from_dict(resolve_channel_scope('hermes', BASE))
    runtime.store.append(other)
    monkeypatch.setattr(runtime.memory, 'recall', lambda **kwargs: RecallBundle(
        items=[gold], rules=[], reflections=[], confidence=1, next_action_hint='',
        explanation={'cascade_evidence': runtime.memory._selected_record_summaries([other])}))
    result = private_call(service)
    assert result['ok'] and 'PRIVATE foreign' not in json.dumps(result)
    captured = load_explicit_capture(runtime, result['capture']['record_id'], scope=resolve_channel_scope('codex', BASE))
    assert 'PRIVATE foreign' not in json.dumps(captured.to_dict())
    assert all(ref['record_ref'] != other.record_id for ref in captured.content['references'])
