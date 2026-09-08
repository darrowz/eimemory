from types import SimpleNamespace
from eimemory.retrieval import caller_assistance as assistance


def test_assistance_requires_verbatim_authoritative_span(monkeypatch):
    record = SimpleNamespace(record_id='memory-1')
    client = SimpleNamespace(timeout_seconds=90, complete=lambda **_: SimpleNamespace(
        text='{"selected":[{"id":"0","quote":"fabricated evidence"}]}'))
    monkeypatch.setattr(assistance, 'llm_client_from_env', lambda _: client)
    selected, report = assistance.verify_candidates(query='Must I skip the full document?',
        candidates=[(record, 'Read the complete document, not merely its title.')], limit=1)
    assert selected == [] and report['status'] == 'unavailable'
    client.complete = lambda **_: SimpleNamespace(text='{"selected":[{"id":"0","quote":"Read the complete document"}]}')
    selected, report = assistance.verify_candidates(query='Must I skip the full document?',
        candidates=[(record, 'Read the complete document, not merely its title.')], limit=1)
    assert selected == [record] and report['status'] == 'evidence_found'
    assert 'quote' not in report['proofs'][0]
    assert client.timeout_seconds <= 9


def test_assistance_does_not_call_model_when_budget_is_spent(monkeypatch):
    monkeypatch.setattr(assistance, 'llm_client_from_env', lambda _: (_ for _ in ()).throw(AssertionError()))
    selected, report = assistance.verify_candidates(query='query',
        candidates=[(SimpleNamespace(record_id='one'), 'evidence')], limit=1, deadline_at=1)
    assert selected == [] and report['calls'] == 0
