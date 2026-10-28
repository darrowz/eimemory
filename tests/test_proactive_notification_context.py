"""Gateway notices must not become entities for a later human request."""
from dataclasses import asdict
from hashlib import sha256
from types import SimpleNamespace

import pytest

from eimemory.retrieval.proactive import ProactiveRecallService as Service
from tests.test_proactive_recall import BASE_SCOPE, _record, _service

NOTICE = '[IMPORTANT: Background process proc_test completed normally (exit code 0). Command: model stale-worker]'


def test_completed_notification_does_not_seed_next_request(tmp_path):
    runtime, engine, service = _service(tmp_path, [_record('A durable fact')])
    service.complete_turn(channel='codex', scope=BASE_SCOPE, source_ids=['alpha'],
        session_id='s', turn_id='notice', user_summary=NOTICE, assistant_summary='')
    service.decide(channel='codex', scope=BASE_SCOPE, source_ids=['alpha'],
        session_id='s', query_id='human', query='查看最新的nightly')
    assert engine.requests[0].query == '查看最新的nightly'
    runtime.close()


def rehydrate(record):
    service = Service.__new__(Service)
    scope = asdict(record.scope)
    service.runtime = SimpleNamespace(store=SimpleNamespace(
        list_proactive_session_item_refs=lambda *a, **k: [(record.record_id, record.source_id)],
        get_by_exact_ref=lambda *a, **k: record))
    digests = [sha256(term.encode()).hexdigest() for term in Service._entities('User: ' + NOTICE)]
    return service._rehydrate_turn_context(channel='codex', exact_scope=scope,
        source_ids=('alpha',), session_id='s', persisted_turns=[{'entity_digests': digests}])


def test_restart_cannot_reintroduce_notice_via_authorized_record():
    record = _record('User: ' + NOTICE)
    assert rehydrate(record) == []


def test_human_discussion_of_background_process_is_preserved():
    query = 'Recall yesterday’s discussion'
    expanded = Service._recall_query(query, ['User: How should Project Zephyr monitor background processes?'])
    assert 'zephyr' in expanded.casefold()


@pytest.mark.parametrize('query', ['现在eimemory的状态', '查看最新的nightly', '你现在用什么大模型', '项目 Zephyr 最新任务状态', '项目 Zephyr 上次授权的任务'])
def test_restart_notice_does_not_expand_original_request(query):
    record = _record('User: ' + NOTICE)
    assert Service._recall_query(query, rehydrate(record)) == query


def test_regular_history_rehydrates_and_preserves_named_project():
    record = _record('User: Project Zephyr uses PostgreSQL Assistant: Confirmed')
    service = Service.__new__(Service)
    service.runtime = SimpleNamespace(store=SimpleNamespace(
        list_proactive_session_item_refs=lambda *a, **k: [(record.record_id, record.source_id)],
        get_by_exact_ref=lambda *a, **k: record))
    turns = service._rehydrate_turn_context(channel='codex', exact_scope=asdict(record.scope),
        source_ids=('alpha',), session_id='s', persisted_turns=[{'entity_digests': [sha256(b'zephyr').hexdigest()]}])
    assert 'zephyr' in Service._recall_query('Recall storage', turns).casefold()


def test_notification_history_cannot_pad_a_no_answer(tmp_path):
    runtime, engine, service = _service(tmp_path, [])
    service.complete_turn(channel='codex', scope=BASE_SCOPE, source_ids=['alpha'],
        session_id='s', turn_id='notice', user_summary=NOTICE, assistant_summary='')
    decision = service.decide(channel='codex', scope=BASE_SCOPE, source_ids=['alpha'],
        session_id='s', query_id='human', query='Recall the unrecorded project decision')
    assert len(engine.requests) == 1
    assert engine.requests[0].query == 'Recall the unrecorded project decision'
    assert decision['items'] == []
    runtime.close()
