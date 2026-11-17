"""Canonical title provenance, using stub verdicts rather than semantic scores."""
from copy import deepcopy
from dataclasses import replace
from hashlib import sha256
import json

import pytest

from eimemory.raw.answer_support import MAX_SOURCE_TITLE_CHARS
from eimemory.recall.raw_citation import collect_citation, attach_compact_lane, render_citation
from test_raw_answer_support import Client, assess, passage
from test_raw_answer_support_consumer import configured, fetch
from test_runtime_raw_citation_delivery import (
    CANONICAL, TEXT, add_raw, configure_proactive, delivery_expectation,
    final_context, owner, persona, persist_mutation, proactive, runtime,
)


def titled(runtime, title, text=TEXT):
    record = add_raw(runtime, text, scope=owner(CANONICAL))
    record.title = title
    return persist_mutation(runtime, record)


def test_canonical_title_is_separate_from_selected_body_and_original_offsets(runtime, monkeypatch):
    title = 'Current handbook / volume I — "not a quotation"'
    record = titled(runtime, title)
    client = Client()
    configured(monkeypatch, client)
    result = fetch(runtime)
    lane = result['bundle']['raw_citation']
    packet = json.loads(client.calls[0]['user_prompt'])
    assert packet['passages'] == [{'id': 'p0w0', 'text': TEXT, 'source_title': title}]
    selected = lane['support_evidence'][0]
    assert selected['ref'][0] == record.record_id
    assert selected['text'] == TEXT and selected['span'] == [0, len(TEXT)]
    assert selected['excerpt_digest'] == sha256(TEXT.encode()).hexdigest()
    assert selected['source_title'] == title
    assert title not in selected['text']
    assert 'source_title' in result['context']
    assert '"evidence_role":"support_material"' in result['context']


def test_caller_context_cannot_replace_the_canonical_title(runtime, monkeypatch):
    titled(runtime, 'Canonical source identity')
    client = Client()
    configured(monkeypatch, client)
    result = fetch(runtime, source_title='Caller-invented answer', title='Caller-invented answer',
                   provenance={'source_title': 'Caller-invented answer'})
    packet = json.loads(client.calls[0]['user_prompt'])
    assert packet['passages'][0]['source_title'] == 'Canonical source identity'
    assert 'Caller-invented answer' not in client.calls[0]['user_prompt']
    assert result['bundle']['raw_citation']['support_evidence'][0]['source_title'] == 'Canonical source identity'


def test_title_changes_are_request_bound_even_if_a_caller_reuses_other_digests():
    original = passage(source_title='Volume one')
    first = assess(Client(), [original])
    stale = {'request_digest': first.request_digest, 'status': 'supported',
             'reason': 'direct_support', 'selected': ['p0w0']}
    changed = replace(original, source_title='Volume two')
    second = assess(Client(lambda packet: stale), [changed])
    assert second.request_digest != first.request_digest
    assert second.status == 'unknown' and second.reason == 'request_binding_mismatch'
    assert not second.selected and not second.reviewed


def test_title_changed_during_model_is_withheld_before_delivery(runtime, monkeypatch):
    record = titled(runtime, 'Volume one')
    def mutate(packet):
        assert packet['passages'][0]['source_title'] == 'Volume one'
        record.title = 'Volume two'
        persist_mutation(runtime, record)
        return {'request_digest': packet['request_digest'], 'status': 'supported',
                'reason': 'direct_support', 'selected': ['p0w0']}
    client = Client(mutate)
    configured(monkeypatch, client)
    result = fetch(runtime)
    lane = result['bundle']['raw_citation']
    assert len(client.calls) == 1
    assert lane['admission']['reason'] == 'support_snapshot_changed'
    assert not lane['support_evidence'] and not lane['evidence']
    assert TEXT not in result['context']


def test_cached_title_change_invalidates_source_snapshot_but_preserves_independent_l1(runtime, monkeypatch):
    record = titled(runtime, 'Volume one')
    persona(runtime, CANONICAL)
    configure_proactive(runtime)
    configured(monkeypatch, Client())
    payload = proactive(runtime, base=CANONICAL)
    assert payload['bundle']['raw_citation']['support_evidence'][0]['source_title'] == 'Volume one'
    assert TEXT in final_context(runtime, payload, base=CANONICAL)
    record.title = 'Volume two'
    persist_mutation(runtime, record)
    expected = delivery_expectation(runtime, base=CANONICAL)
    actual = final_context(runtime, payload, base=CANONICAL, expected_authority=expected)
    assert TEXT not in actual and 'Volume one' not in actual
    assert 'Prefer concise' in actual


def test_cached_title_bytes_cannot_be_changed_under_old_sidecar_binding(runtime, monkeypatch):
    titled(runtime, 'Canonical identity')
    persona(runtime, CANONICAL)
    configure_proactive(runtime)
    configured(monkeypatch, Client())
    payload = proactive(runtime, base=CANONICAL)
    changed = deepcopy(payload)
    changed['bundle']['raw_citation']['support_evidence'][0]['source_title'] = 'Injected identity'
    actual = final_context(runtime, changed, base=CANONICAL)
    assert TEXT not in actual and 'Injected identity' not in actual
    assert 'Prefer concise' in actual


def test_oversized_canonical_title_is_omitted_without_losing_answerable_body(runtime, monkeypatch):
    title = 'x' * (MAX_SOURCE_TITLE_CHARS + 1)
    titled(runtime, title)
    client = Client()
    configured(monkeypatch, client)
    result = fetch(runtime)
    lane = result['bundle']['raw_citation']
    assert lane['answer_support']['status'] == 'supported'
    assert json.loads(client.calls[0]['user_prompt'])['passages'] == [{'id': 'p0w0', 'text': TEXT}]
    assert lane['support_evidence'][0]['text'] == TEXT
    assert 'source_title' not in lane['support_evidence'][0]


def test_total_title_budget_never_truncates_or_invalidates_complete_bodies():
    texts = ['a' * 16000, 'b' * 16000]
    candidates = [passage(text, f'r{i}', source_title='Identity ' + str(i)) for i, text in enumerate(texts)]
    client = Client(lambda packet: {'request_digest': packet['request_digest'], 'status': 'supported',
                                   'reason': 'direct_support', 'selected': [p['id'] for p in packet['passages']]})
    result = assess(client, candidates)
    packet = json.loads(client.calls[0]['user_prompt'])
    assert result.status == 'supported' and len(result.selected) == 2
    assert packet['coverage_complete']
    assert [p['text'] for p in packet['passages']] == texts
    assert all('source_title' not in p for p in packet['passages'])
    assert all((p.span_start, p.span_end) == (0, 16000) for p in result.selected)


def test_repeated_window_titles_count_towards_the_same_optional_context_budget():
    body = 'a' * 15800
    spans = ((0, 7900), (7900, 15800))
    candidates = [passage(body, f'r{i}', source_title='t' * 200, allowed_spans=spans) for i in range(2)]
    client = Client()
    result = assess(client, candidates)
    packet = json.loads(client.calls[0]['user_prompt'])
    assert result.status == 'supported'
    assert len(packet['passages']) == 4
    assert sum(len(p['text']) for p in packet['passages']) == 31600
    assert all('source_title' not in p for p in packet['passages'])
    assert result.selected[0].span_end == 7900


def test_reviewed_identity_must_fit_with_complete_support_under_transport_budget(runtime, monkeypatch):
    title = 'Identity ' + 'x' * 500
    titled(runtime, title)
    configured(monkeypatch, Client())
    lane = collect_citation(runtime.memory,
                            query='Where are Halcyon deployment manifests stored?',
                            scope=owner(CANONICAL), context={'source_ids': ['hermes']})
    without_title = deepcopy(lane)
    without_title.evidence = []
    without_title.support_evidence[0].pop('source_title')
    public = without_title.to_dict()
    cap = len(json.dumps({'raw_citation': public}, ensure_ascii=False,
                         separators=(',', ':')).encode()) + 192
    baseline = attach_compact_lane({}, lane, maximum_bytes=16000)['raw_citation']
    actual = attach_compact_lane({}, lane, maximum_bytes=cap)['raw_citation']
    assert baseline['answer_support']['status'] == 'supported'
    assert baseline['support_evidence'][0]['source_title'] == title
    assert actual['answer_support']['status'] == 'unknown'
    assert actual['answer_support']['reason'] == 'support_context_incomplete'
    assert not actual['support_evidence'] and not actual['answer_support']['selected']
    assert actual['evidence'] and actual['evidence'][0]['text'] == TEXT
    assert lane.answer_support['status'] == 'supported'
    assert lane.support_evidence[0]['source_title'] == title


def test_reviewed_identity_must_fit_with_complete_support_under_render_budget(runtime, monkeypatch):
    title = 'Identity ' + 'x' * 500
    titled(runtime, title)
    configured(monkeypatch, Client())
    lane = fetch(runtime)['bundle']['raw_citation']
    support_only = deepcopy(lane)
    support_only['evidence'] = []
    expected = render_citation('', support_only, max_chars=16000)
    actual = deepcopy(lane)
    rendered = render_citation('', actual, max_chars=len(expected) - len(title))
    assert support_only['answer_support']['status'] == 'supported'
    assert title in expected
    assert actual['answer_support']['status'] == 'unknown'
    assert actual['answer_support']['reason'] == 'support_context_incomplete'
    assert not actual['support_evidence'] and not actual['answer_support']['selected']
    assert TEXT in rendered
    assert '"evidence_role":"related_material"' in rendered
    assert 'Model-reviewed support material:' not in rendered


@pytest.mark.parametrize('delivery', ['render', 'compact'])
@pytest.mark.parametrize('mutation', ['removed', 'changed'])
def test_direct_delivery_cannot_keep_support_with_missing_or_changed_reviewed_title(
    runtime, monkeypatch, delivery, mutation,
):
    title = 'Canonical volume identity'
    titled(runtime, title)
    configured(monkeypatch, Client())
    lane = collect_citation(runtime.memory,
                            query='Where are Halcyon deployment manifests stored?',
                            scope=owner(CANONICAL), context={'source_ids': ['hermes']})
    assert lane.answer_support['status'] == 'supported'
    assert lane.support_evidence[0]['source_title'] == title
    related = deepcopy(lane.evidence)
    if mutation == 'removed':
        lane.support_evidence[0].pop('source_title')
    else:
        lane.support_evidence[0]['source_title'] = 'Different volume identity'

    if delivery == 'compact':
        actual = attach_compact_lane({}, lane, maximum_bytes=16000)['raw_citation']
    else:
        actual = lane.to_dict()
        rendered = render_citation('', actual, max_chars=16000)
        assert TEXT in rendered
        assert 'Model-reviewed support material:' not in rendered
        assert 'Different volume identity' not in rendered

    assert actual['answer_support']['status'] == 'unknown'
    assert actual['answer_support']['reason'] == 'support_context_incomplete'
    assert not actual['answer_support']['selected'] and not actual['support_evidence']
    assert actual['evidence'] == related
