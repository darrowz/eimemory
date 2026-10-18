"""Predefined complete-window selection; providers cannot rewrite evidence."""
from hashlib import sha256
import json
from types import SimpleNamespace

import pytest

from eimemory.models.records import RecordEnvelope, ScopeRef
from eimemory.retrieval import caller_assistance as assistance
from eimemory.retrieval import independent_evidence
from eimemory.retrieval.relevance import record_digest


@pytest.fixture(autouse=True)
def isolated_verifier(monkeypatch):
    monkeypatch.setattr(independent_evidence, 'active', lambda: False)
    monkeypatch.setattr(assistance, 'current_verifier_route', lambda: None)
    monkeypatch.delenv('EIMEMORY_RECALL_EXPECTED_MODEL', raising=False)
    monkeypatch.setenv('EIMEMORY_OPERATOR_DISPLAY_NAME', '鸿哥')


def record(text, *, user='operator', source='fixture'):
    return RecordEnvelope.create(kind='memory', title='Original evidence', summary=text,
        content={'text':text}, source_id=source,
        scope=ScopeRef(tenant_id='fixture', agent_id='agent', workspace_id='workspace', user_id=user))


def verify(monkeypatch, rows, callback, *, query='How should Acme publish updates?', limit=3):
    prompts = []
    def complete(**kwargs):
        prompt = json.loads(kwargs['user_prompt'])
        prompts.append((prompt, kwargs['system_prompt']))
        payload = callback(prompt)
        return SimpleNamespace(text=json.dumps(payload, ensure_ascii=False))
    monkeypatch.setattr(assistance, 'configured_client', lambda: SimpleNamespace(
        timeout_seconds=9, complete=complete))
    chosen, report = assistance.verify_candidates(query=query,
        candidates=[(row, row.summary) for row in rows], limit=limit)
    return chosen, report, prompts


def test_window_schema_chooses_complete_server_text_with_qualifiers(monkeypatch):
    text = 'Acme sends updates only after approval. Do not publish an unapproved draft.'
    row = record(text)
    chosen, report, prompts = verify(monkeypatch, [row], lambda p: {
        'selected':[p['candidates'][0]['windows'][0]['id']]})
    assert chosen == [row] and report['outcome'] == 'supported'
    assert report['policy'] == 'caller-original-evidence-verification.v5'
    assert report['selection_schema'] == assistance.WINDOW_SELECTION_SCHEMA
    prompt, system = prompts[0]
    assert prompt['selection_schema'] == assistance.WINDOW_SELECTION_SCHEMA
    assert prompt['original_query'] == 'How should Acme publish updates?'
    assert prompt['candidates'][0]['text'] == text  # legacy request remains compatible
    assert 'at most 1 distinct parent' in system
    proof = report['proofs'][0]
    excerpt = text[proof['span_start']:proof['span_end']]
    assert excerpt == prompt['candidates'][0]['windows'][0]['text'] == text
    assert 'only after approval' in excerpt and 'Do not publish' in excerpt
    assert proof['quote_digest'] == sha256(text.encode()).hexdigest()
    assert proof['record_digest'] == record_digest(row)
    assert proof['window_start'] == proof['span_start']
    assert proof['window_end'] == proof['span_end'] <= 16000
    assert 'quote' not in proof


@pytest.mark.parametrize('selected,reason', [
    (['p1w0'], 'invalid_assistance_reference'),
    (['0'], 'invalid_assistance_reference'),
    (['p0w99'], 'invalid_assistance_reference'),
    (['p0w0','p0w0'], 'invalid_assistance_reference'),
    (['p0w0','p0w0','p0w0'], 'invalid_assistance_reference'),
    ([0], 'invalid_assistance_selection'),
    ([None], 'invalid_assistance_selection'),
    (['p0w0', {'id':'0','quote':'Acme publishes approved updates.'}], 'invalid_assistance_selection'),
    ([{'window_id':'p0w0'}], 'invalid_assistance_selection'),
    ([{'id':'0','quote':'invented evidence'}], 'invalid_assistance_quote'),
    ([{'id':'0','quote':'Acme publishes approved updates.'}]*2, 'invalid_assistance_reference'),
])
def test_unknown_duplicate_mixed_and_fabricated_selections_fail_closed(monkeypatch, selected, reason):
    row = record('Acme publishes approved updates.')
    chosen, report, _ = verify(monkeypatch, [row], lambda _: {'selected':selected})
    assert chosen == [] and report['outcome'] == 'unavailable'
    assert report['validation_reason'] == reason and not report.get('proofs')


def test_two_distinct_windows_from_one_parent_are_not_two_selections(monkeypatch):
    body = ('Acme records background facts. ' * 45) + 'Acme publishes only approved updates.'
    row = record(body)
    def select(prompt):
        windows = prompt['candidates'][0]['windows']
        assert len(windows) == 2 and windows[0]['id'] != windows[1]['id']
        return {'selected':[window['id'] for window in windows]}
    chosen, report, _ = verify(monkeypatch, [row], select)
    assert not chosen and report['validation_reason'] == 'invalid_assistance_reference'


def test_duplicate_exact_parent_entries_cannot_produce_two_proofs(monkeypatch):
    row = record('Acme publishes approved updates.')
    chosen, report, _ = verify(monkeypatch, [row, row], lambda _: {'selected':['p0w0','p1w0']})
    assert not chosen and report['validation_reason'] == 'invalid_assistance_reference'


@pytest.mark.parametrize('selected', [['p0w0','p1w0','p2w0','p3w0'], ['p8w0']])
def test_eight_parent_and_three_selection_bounds_remain_strict(monkeypatch, selected):
    rows = [record(f'Acme publishes approved update number {index}.') for index in range(9)]
    chosen, report, prompts = verify(monkeypatch, rows, lambda _: {'selected':selected})
    assert not chosen and report['outcome'] == 'unavailable'
    assert len(prompts[0][0]['candidates']) == 8
    assert 'at most 3 distinct parent' in prompts[0][1]


def test_limit_applies_after_three_unique_parent_selections(monkeypatch):
    rows = [record(f'Acme publishes approved update number {index}.') for index in range(3)]
    chosen, report, _ = verify(monkeypatch, rows, lambda _: {'selected':['p0w0','p1w0','p2w0']}, limit=1)
    assert chosen == rows[:1] and len(report['proofs']) == 1
    assert report['model_selected_count'] == 3 and report['accepted_selection_count'] == 1


@pytest.mark.parametrize('mutation', ['status','summary','scope','source_id'])
def test_completion_time_parent_version_drift_is_rejected(monkeypatch, mutation):
    row = record('Acme publishes approved updates.')
    def select(_):
        if mutation == 'scope':
            row.scope = ScopeRef(user_id='other-user')
        else:
            setattr(row, mutation, 'changed-after-projection')
        return {'selected':['p0w0']}
    chosen, report, _ = verify(monkeypatch, [row], select)
    assert not chosen and report['outcome'] == 'unavailable'
    assert report['validation_reason'] == 'assistance_parent_version_changed'


def test_selected_complete_window_still_requires_requested_attribute(monkeypatch):
    row = record('项目 Atlas 已签约，合同总金额尚未确定。')
    chosen, report, _ = verify(monkeypatch, [row], lambda _: {'selected':['p0w0']},
                               query='项目 Atlas 合同总金额是多少？')
    assert not chosen and report['outcome'] == 'no_support'
    assert report['answer_requirement_rejections'] == 1 and not report.get('proofs')


def test_empty_selection_stays_no_support_without_fallback(monkeypatch):
    chosen, report, _ = verify(monkeypatch, [record('Acme publishes approved updates.')],
                               lambda _: {'selected':[]})
    assert not chosen and report['outcome'] == 'no_support' and report['calls'] == 1
    assert report['reason'] == 'model_no_selection' and not report.get('proofs')


def test_legacy_verbatim_response_remains_strictly_supported(monkeypatch):
    row = record('Acme publishes approved updates. Do not publish drafts.')
    quote = 'Acme publishes approved updates.'
    chosen, report, _ = verify(monkeypatch, [row], lambda _: {
        'selected':[{'id':'0','quote':quote}]})
    assert chosen == [row] and report['selection_schema'] == 'legacy-verbatim-quote.v1'
    proof = report['proofs'][0]
    assert row.summary[proof['span_start']:proof['span_end']] == quote
