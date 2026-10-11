"""Deduplication witnesses observe the ledger; they never certify recall quality."""
import json
import sqlite3

import pytest

from eimemory.api.runtime import Runtime
from eimemory.evaluation.production_query_auto_review import auto_review_pending_production_queries
from eimemory.evaluation.production_query_dataset import (
    build_production_query_dataset, collect_pending_production_queries,
)
from eimemory.retrieval.proactive import ProactiveRecallService
from test_proactive_recall import (
    BASE_SCOPE, RELEASE, _dedupe_decision, _exact_transition, _record, _service,
)


@pytest.mark.parametrize('channel', ['hermes', 'codex'])
def test_delivered_not_used_witness_survives_restart_and_empty_review(tmp_path, monkeypatch, channel):
    monkeypatch.setenv('EIMEMORY_EVIDENCE_RECEIPT_HMAC_KEY', 'fixture-only-0123456789-abcdefghijklmnopqrstuvwxyz')
    monkeypatch.delenv('EIMEMORY_PRODUCTION_RECALL_AUTO_REVIEW', raising=False)
    records = [_record('PRIVATE_MEMORY alpha durable evidence', channel=channel),
               _record('PRIVATE_MEMORY beta durable evidence', channel=channel)]
    runtime, engine, service = _service(tmp_path, records)
    args = dict(channel=channel, scope=BASE_SCOPE, source_ids=['alpha'],
                session_id='provenance-session', query='Recall PRIVATE_QUERY durable evidence',
                task_type='memory.recall')
    try:
        first = service.decide(**args, query_id='first')
        assert len(first['items']) == 2
        exact = {**_exact_transition(first, session_id=args['session_id'], turn_id='first'), 'channel': channel}
        assert service.mark_injected(**exact, injected_citations=[i['citation'] for i in first['items']])['ok']
        assert service.mark_terminal(**exact)['ok']
        stored_first = runtime.store.load_proactive_decision(first['decision_id'])
        assert all(i['state'] == 'not_used' and i['ever_injected'] for i in stored_first['items'])
        second = service.decide(**args, query_id='second')
        assert second['items'] == [] and second['context'] == ''
        audit = second['retrieval_diagnostics']['session_dedupe']
        assert audit['removed_count'] == audit['recorded_count'] == 2
        assert audit['missing_match_count'] == 0 and audit['truncated'] is False
        assert {i['record_id'] for i in audit['matches']} == {r.record_id for r in records}
        for match in audit['matches']:
            prior = next(i for i in stored_first['items'] if i['record_id'] == match['record_id'])
            assert match['prior_decision_id'] == first['decision_id']
            assert match['prior_citation'] == prior['citation']
            assert match['prior_state'] == 'not_used' and match['prior_terminal'] is True
            assert match['prior_ever_injected'] is True and match['basis'] == 'ever_injected'
            assert match['prior_item_updated_at'] == prior['updated_at']
        assert 'PRIVATE_MEMORY' not in json.dumps(audit) and 'PRIVATE_QUERY' not in json.dumps(audit)
        # Current candidates stay empty, even though we know precisely what was deduped.
        collected = collect_pending_production_queries(
            runtime, scope=BASE_SCOPE, channel=channel, decision_id=second['decision_id'])
        assert collected['new_count'] == 1
        pending_id = collected['new_pending_record_ids'][0]
        pending = runtime.store.get_by_id(pending_id)
        assert pending.content['candidate_refs'] == [] and pending.evidence == []
        review = auto_review_pending_production_queries(runtime, scope=BASE_SCOPE, channel=channel)
        assert review['accepted_count'] == 0 and review['open_review_count'] == 0
        assert review['review_results'][0]['reasons'] == ['no_candidate_refs']
        terminal_id = review['review_results'][0]['review_receipt_id']
        terminal = runtime.store.get_by_id(terminal_id).content
        assert terminal['disposition'] == 'not_evaluable' and terminal['close_reason'] == 'no_recall'
        assert terminal['reasons'] == ['no_candidate_refs']
        assert terminal['review_complete'] is True and terminal['does_not_certify_answer'] is True
    finally:
        runtime.close()
    with Runtime.create(root=tmp_path, recall_engine=engine) as restarted:
        persisted = restarted.store.load_proactive_decision(second['decision_id'])
        assert persisted['retrieval_diagnostics']['session_dedupe'] == audit
        assert persisted['items'] == []
        assert restarted.store.get_by_id(terminal_id).content['answer_quality'] == 'not_evaluable'
        assert build_production_query_dataset(restarted, scope=BASE_SCOPE)['progress']['accepted_case_count'] == 0
        replay = ProactiveRecallService(restarted, release_identity=RELEASE, control_percent=0)
        assert replay.decide(**args, query_id='second')['retrieval_diagnostics']['session_dedupe'] == audit


@pytest.mark.parametrize('state,terminal,basis', [
    ('volunteered', 0, 'in_flight'), ('used', 1, 'host_used'), ('rejected', 1, 'host_rejected'),
    ('injected', 0, 'ever_injected'), ('not_used', 1, None), ('suppressed', 1, None),
    ('volunteered', 1, None),
])
def test_witness_preserves_existing_state_rules(tmp_path, state, terminal, basis):
    runtime, _engine, service = _service(tmp_path, [])
    try:
        payload = _dedupe_decision(runtime, service, decision_id='pd:history', record_id='record-a',
                                   state=state, terminal=terminal)
        refs, audit = runtime.store.proactive_session_dedupe(payload, [('record-a', 'alpha')])
        assert refs == runtime.store.proactive_session_refs(payload)
        assert audit['removed_count'] == audit['recorded_count'] == int(basis is not None)
        if basis:
            assert audit['matches'][0]['basis'] == basis
            assert audit['matches'][0]['prior_state'] == state
            assert audit['matches'][0]['prior_terminal'] == bool(terminal)
    finally:
        runtime.close()


@pytest.mark.parametrize('partition', ['channel', 'source_key', 'session_id',
                                      'tenant_id', 'agent_id', 'workspace_id', 'user_id', 'source_id'])
def test_witness_never_crosses_exact_partition(tmp_path, partition):
    runtime, _engine, service = _service(tmp_path, [])
    try:
        payload = _dedupe_decision(runtime, service, decision_id='pd:history', record_id='record-a',
                                   state='injected', terminal=1)
        candidate = ('record-a', 'alpha')
        if partition in payload:
            payload[partition] = 'other'
        elif partition == 'source_id':
            candidate = ('record-a', 'other')
        else:
            payload['scope'][partition] = 'other'
        _refs, audit = runtime.store.proactive_session_dedupe(payload, [candidate])
        assert audit['removed_count'] == 0 and audit['matches'] == []
    finally:
        runtime.close()


def test_witness_uses_the_same_snapshot_when_another_connection_closes_inflight(tmp_path, monkeypatch):
    runtime, _engine, service = _service(tmp_path, [])
    try:
        payload = _dedupe_decision(runtime, service, decision_id='pd:history', record_id='record-a',
                                   state='volunteered', terminal=0)
        path = runtime.store.sqlite.conn.execute('PRAGMA database_list').fetchone()[2]
        original = runtime.store.sqlite.proactive_session_refs
        def close_after_refs(*args, **kwargs):
            refs = original(*args, **kwargs)
            with sqlite3.connect(path) as writer:
                writer.execute("UPDATE proactive_decisions SET terminal=1 WHERE decision_id='pd:history'")
                writer.execute("UPDATE proactive_decision_items SET state='not_used' WHERE decision_id='pd:history'")
            return refs
        monkeypatch.setattr(runtime.store.sqlite, 'proactive_session_refs', close_after_refs)
        refs, audit = runtime.store.proactive_session_dedupe(payload, [('record-a', 'alpha')])
        assert refs == {('record-a', 'alpha')}
        assert audit['missing_match_count'] == 0
        assert audit['matches'][0]['basis'] == 'in_flight'
        assert audit['matches'][0]['prior_terminal'] is False
        assert runtime.store.load_proactive_decision('pd:history')['terminal'] is True
        assert runtime.store.sqlite.conn.in_transaction is False
    finally:
        runtime.close()


def test_witness_selects_a_qualifying_history_item_not_a_newer_undelivered_item(tmp_path):
    runtime, _engine, service = _service(tmp_path, [])
    try:
        payload = _dedupe_decision(runtime, service, decision_id='pd:delivered', record_id='record-a',
                                   state='injected', terminal=1)
        _dedupe_decision(runtime, service, decision_id='pd:undelivered', record_id='record-a',
                         state='not_used', terminal=1)
        refs, audit = runtime.store.proactive_session_dedupe(payload, [('record-a', 'alpha')])
        assert refs == {('record-a', 'alpha')}
        assert audit['matches'][0]['prior_decision_id'] == 'pd:delivered'
        assert audit['matches'][0]['prior_ever_injected'] is True
    finally:
        runtime.close()


def test_witness_cap_does_not_limit_deduplication_or_commit_caller_transaction(tmp_path):
    runtime, _engine, service = _service(tmp_path, [], max_decisions=100)
    try:
        payload = _dedupe_decision(runtime, service, decision_id='pd:0', record_id='record-0',
                                   state='injected', terminal=1)
        seed = runtime.store.load_proactive_decision('pd:0')
        candidates = [(f'record-{i}', 'alpha') for i in range(70)]
        for i, (record_id, source_id) in enumerate(candidates[1:], start=1):
            runtime.store.record_proactive_decision(
                {**seed, 'decision_id': f'pd:{i}', 'turn_id': f'turn-{i}', 'query_id': f'query-{i}'},
                [{**seed['items'][0], 'record_id': record_id, 'source_id': source_id, 'state': 'injected'}],
                [], max_global_decisions=100)
        with runtime.store.locked() as sql:
            sql.execute('BEGIN')
            sql.execute("UPDATE proactive_decisions SET task_type='uncommitted' WHERE decision_id='pd:69'")
            refs, audit = runtime.store.proactive_session_dedupe(payload, candidates)
            assert len(refs) == audit['removed_count'] == 70
            assert audit['recorded_count'] == 64 and audit['truncated'] is True
            assert audit['missing_match_count'] == 0
            assert sql.in_transaction is True
            sql.rollback()
        assert runtime.store.load_proactive_decision('pd:69')['task_type'] != 'uncommitted'
    finally:
        runtime.close()


def test_supplied_bundle_cannot_forge_witness_and_mandatory_context_is_not_deduped(tmp_path):
    record = _record('mandatory rule')
    record.kind = 'rule'
    record.meta['mandatory'] = True
    record.content['rule_type'] = 'safety'
    runtime, engine, service = _service(tmp_path, [record])
    try:
        bundle = engine.recall(None)
        bundle.explanation['session_dedupe'] = {'matches': [{'record_id': 'forged-secret'}]}
        args = dict(channel='codex', scope=BASE_SCOPE, source_ids=['alpha'],
                    session_id='mandatory-session', query='Recall mandatory rule', recall_bundle=bundle)
        first = service.decide(**args, query_id='first')
        assert first['items'][0]['mandatory'] is True
        second = service.decide(**args, query_id='second')
        assert second['items'][0]['record_id'] == record.record_id
        audit = second['retrieval_diagnostics']['session_dedupe']
        assert audit['input_count'] == audit['removed_count'] == 0 and audit['matches'] == []
        assert 'forged-secret' not in json.dumps(second['retrieval_diagnostics'])
    finally:
        runtime.close()
