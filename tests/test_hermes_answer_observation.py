"""Actual host callbacks expose bounded digest evidence for answer review."""
from hashlib import sha256
import json
import logging

import pytest

from eimemory.adapters.runtime.channel import resolve_channel_scope
from test_real_effect_signals import live

PREFIX = 'eimemory_hermes_answer_observation '


def events(caplog):
    return [json.loads(record.getMessage()[len(PREFIX):]) for record in caplog.records
            if record.getMessage().startswith(PREFIX)]


def enable(monkeypatch, provider):
    scope = resolve_channel_scope('hermes', provider._scope)
    monkeypatch.setenv('EIMEMORY_CAPTURE_ANSWER_OBSERVATIONS', '1')
    monkeypatch.setenv('EIMEMORY_CAPTURE_QUERY_SCOPES', json.dumps([
        dict(scope, channel='hermes', source_id=provider._source_ids()[0])]))


def complete(provider, query, answer, *, turn='answer-turn'):
    provider.on_pre_llm_call(user_message=query, session_id='signal-session', turn_id=turn)
    provider.prefetch(query, session_id='signal-session')
    provider.on_post_llm_call(user_message=query, assistant_message=answer,
                             session_id='signal-session', turn_id=turn)
    provider._effects.flush()


def test_callback_binds_full_answer_and_keeps_task_outcome_unverified(live, monkeypatch, caplog):
    runtime, _, provider, _, _ = live
    enable(monkeypatch, provider)
    caplog.set_level(logging.INFO)
    query = 'Private question about the saved deployment configuration'
    answer = 'Private complete answer. ' + 'Evidence and conditions. ' * 430
    complete(provider, query, answer)
    observed, = events(caplog)
    assert observed['schema'] == 'eimemory.hermes_answer_observation.v2'
    assert observed['query_digest'] == sha256(query.encode()).hexdigest()
    assert observed['host_query_digest'] == sha256(query.encode()).hexdigest()
    assert observed['answer_digest'] == sha256(answer.encode()).hexdigest()
    assert observed['answer_chars'] == len(answer) > 8000
    assert observed['observation_only'] is True
    assert observed['verified_task_outcome'] is False
    encoded = json.dumps(observed)
    assert query not in encoded and 'Private complete answer' not in encoded
    assert 'signal-session' not in encoded and str(provider._hermes_home) not in encoded
    decision = runtime.store.load_proactive_decision(next(iter(provider._effects.previous.values()))['namespace']['decision_id'])
    assert observed['query_digest'] == decision['query_digest']
    assert not decision['outcome_verified'] and decision['outcome_success'] is None
    assert all(json.loads(row[0])['phase'] != 'answer_observed' for row in
               runtime.store.sqlite.conn.execute('SELECT payload_json FROM proactive_effect_signals'))


def test_equal_prefixes_keep_different_complete_answer_digests(live, monkeypatch, caplog):
    _, _, provider, _, _ = live
    enable(monkeypatch, provider)
    caplog.set_level(logging.INFO)
    prefix = 'Same prefix ' * 800
    complete(provider, 'Question one', prefix + 'Fact A.', turn='turn-a')
    complete(provider, 'Question two', prefix + 'Fact B.', turn='turn-b')
    first, second = events(caplog)
    assert first['answer_digest'] != second['answer_digest']
    assert first['host_turn_digest'] != second['host_turn_digest']


def test_late_post_without_observed_turn_is_not_assigned_to_newer_turn(live, monkeypatch, caplog):
    _, _, provider, _, _ = live
    enable(monkeypatch, provider)
    caplog.set_level(logging.INFO)
    query = 'Same business question across overlapping callbacks'
    provider.on_pre_llm_call(user_message=query, session_id='signal-session', turn_id='older-turn')
    provider.prefetch(query, session_id='signal-session')
    provider.on_pre_llm_call(user_message=query, session_id='signal-session', turn_id='newer-turn')
    provider.on_post_llm_call(user_message=query, assistant_message='Late older answer',
                             session_id='signal-session')
    assert events(caplog) == []


def test_explicit_turn_does_not_replace_query_consistency(live, monkeypatch, caplog):
    _, _, provider, _, _ = live
    enable(monkeypatch, provider)
    caplog.set_level(logging.INFO)
    query = 'Original exact question'
    provider.prefetch(query, session_id='signal-session')
    provider.on_pre_llm_call(user_message=query, session_id='signal-session', turn_id='bound-turn')
    next(iter(provider._pending_proactive.values()))['query'] = 'Different request with no pre callback'
    provider.on_post_llm_call(user_message=query, assistant_message='Answer',
                             session_id='signal-session', turn_id='bound-turn')
    assert events(caplog) == []


@pytest.mark.parametrize('policy', ['disabled', 'malformed', 'other_scope'])
def test_capture_requires_exact_enabled_scope(live, monkeypatch, caplog, policy):
    _, _, provider, _, _ = live
    enable(monkeypatch, provider)
    caplog.set_level(logging.INFO)
    if policy == 'disabled':
        monkeypatch.setenv('EIMEMORY_CAPTURE_ANSWER_OBSERVATIONS', '0')
    elif policy == 'malformed':
        monkeypatch.setenv('EIMEMORY_CAPTURE_QUERY_SCOPES', '{invalid')
    else:
        scope = resolve_channel_scope('hermes', provider._scope)
        monkeypatch.setenv('EIMEMORY_CAPTURE_QUERY_SCOPES', json.dumps([
            dict(scope, user_id='other-owner', channel='hermes', source_id='hermes')]))
    complete(provider, 'Question', 'An answer')
    assert events(caplog) == []


@pytest.mark.parametrize('mutation', ['foreign_scope', 'foreign_session', 'changed_query', 'foreign_turn', 'acceptance'])
def test_unbound_or_maintenance_turn_cannot_emit_answer_evidence(live, monkeypatch, caplog, mutation):
    _, _, provider, _, _ = live
    enable(monkeypatch, provider)
    caplog.set_level(logging.INFO)
    query = 'A question with one exact host turn'
    provider.on_pre_llm_call(user_message=query, session_id='signal-session', turn_id='bound-turn')
    provider.prefetch(query, session_id='signal-session')
    pending = dict(next(iter(provider._pending_proactive.values())))
    if mutation == 'foreign_scope':
        pending['scope'] = dict(pending['scope'], user_id='other-owner')
    elif mutation == 'foreign_session':
        pending['session_id'] = 'other-session'
    elif mutation == 'changed_query':
        pending['query'] = query + ' changed'
    elif mutation == 'foreign_turn':
        pending['host_turn_id'] = 'other-turn'
    else:
        pending['acceptance_generated'] = True
    provider._effects.completed('signal-session', 'bound-turn', pending, assistant_message='An answer')
    assert events(caplog) == []


@pytest.mark.parametrize('answer_kind', ['empty', 'missing', 'numeric', 'oversized'])
def test_missing_or_oversized_callback_body_is_not_reported_as_complete(live, monkeypatch, caplog, answer_kind):
    _, _, provider, _, _ = live
    enable(monkeypatch, provider)
    caplog.set_level(logging.INFO)
    provider.on_pre_llm_call(user_message='Question', session_id='signal-session', turn_id='bound-turn')
    provider.prefetch('Question', session_id='signal-session')
    pending = dict(next(iter(provider._pending_proactive.values())))
    answer = 'x' * (1024 * 1024 + 1) if answer_kind == 'oversized' else {
        'empty': '', 'missing': None, 'numeric': 123}[answer_kind]
    provider._effects.completed('signal-session', 'bound-turn', pending, assistant_message=answer)
    assert events(caplog) == []


@pytest.mark.parametrize('query', [
    '中文问题\n  several   words\twith spacing',
    'NUL\x00分隔   中文\t部署版本',
    '  leading and trailing   spaces  ',
    ('中文\n  repeated   words\t' * 600) + ' original host tail',
])
def test_v2_query_digest_matches_actual_decision_and_keeps_full_host_identity(live, monkeypatch, caplog, query):
    runtime, _, provider, _, _ = live
    enable(monkeypatch, provider)
    caplog.set_level(logging.INFO)
    complete(provider, query, 'Observed callback body')
    observed, = events(caplog)
    key = next(iter(provider._effects.previous.values()))['namespace']['decision_id']
    decision = runtime.store.load_proactive_decision(key)
    assert observed['schema'] == 'eimemory.hermes_answer_observation.v2'
    assert observed['query_digest'] == decision['query_digest']
    assert observed['host_query_digest'] == sha256(query.encode()).hexdigest()
    assert observed['host_query_chars'] == len(query)
    assert observed['pre_query_digest'] == sha256(query.strip()[:8000].encode()).hexdigest()
    assert query not in json.dumps(observed)


def test_v2_keeps_distinct_host_bodies_that_share_the_same_8000_character_query(live, monkeypatch, caplog):
    _, _, provider, _, _ = live
    enable(monkeypatch, provider)
    caplog.set_level(logging.INFO)
    prefix = ('中文\n repeated words ' * 600)[:8000]
    first, second = prefix+' first complete host tail', prefix+' second complete host tail'
    complete(provider, first, 'Answer A', turn='long-query-a')
    complete(provider, second, 'Answer B', turn='long-query-b')
    one, two = events(caplog)
    assert one['query_digest'] == two['query_digest']
    assert one['pre_query_digest'] == two['pre_query_digest']
    assert one['host_query_digest'] != two['host_query_digest']


def test_normalization_does_not_weaken_exact_pre_query_matching(live, monkeypatch, caplog):
    _, _, provider, _, _ = live
    enable(monkeypatch, provider)
    caplog.set_level(logging.INFO)
    query = 'Same normalized question'
    provider.on_pre_llm_call(user_message=query, session_id='signal-session', turn_id='bound-turn')
    provider.prefetch(query, session_id='signal-session')
    next(iter(provider._pending_proactive.values()))['query'] = 'Same  normalized question'
    provider.on_post_llm_call(user_message=query, assistant_message='An answer', session_id='signal-session', turn_id='bound-turn')
    assert events(caplog) == []
