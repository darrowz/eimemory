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


def test_preparation_is_request_local_and_closed_on_error(monkeypatch):
    events = []
    client = SimpleNamespace(argv=['node', '/release/eimemory/llm/openclaw_gateway.mjs'],
        prepare=lambda: events.append('prepare'), close=lambda: events.append('close'))
    monkeypatch.setenv('EIMEMORY_CALLER_ASSISTED_RECALL_ENABLED', '1')
    monkeypatch.setenv('EIMEMORY_RECALL_GATEWAY_PREWARM', '1')
    monkeypatch.setattr(assistance, 'llm_client_from_env', lambda _: client)
    import pytest
    with pytest.raises(ValueError):
        with assistance.prepared_verification('Must I only read the title?'):
            assert assistance._PREPARED.get() is client
            raise ValueError('selection failed')
    assert assistance._PREPARED.get() is None
    assert events == ['prepare', 'close']
    with assistance.prepared_verification('What is the supply method?'):
        assert assistance._PREPARED.get() is None
    assert events == ['prepare', 'close']


def test_preparation_never_starts_arbitrary_commands(monkeypatch):
    client = SimpleNamespace(argv=['arbitrary-command'],
        prepare=lambda: (_ for _ in ()).throw(AssertionError('must not execute')))
    monkeypatch.setenv('EIMEMORY_CALLER_ASSISTED_RECALL_ENABLED', '1')
    monkeypatch.setenv('EIMEMORY_RECALL_GATEWAY_PREWARM', '1')
    monkeypatch.setattr(assistance, 'llm_client_from_env', lambda _: client)
    with assistance.prepared_verification('Is this only a title?'):
        assert assistance._PREPARED.get() is None
