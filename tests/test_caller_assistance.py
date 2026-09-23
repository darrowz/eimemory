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
    assert client.timeout_seconds == 90


def test_short_display_name_is_evidence_only_when_it_stands_in_the_record(monkeypatch):
    from eimemory.identity import operator_display_name
    name = operator_display_name()
    record = SimpleNamespace(record_id='memory-1', aliases=())
    captured = {}

    def complete(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(text='{"selected":[{"id":"0","quote":"%s"}]}' % name)

    client = SimpleNamespace(timeout_seconds=90, complete=complete)
    monkeypatch.setattr(assistance, 'configured_client', lambda: client)
    body = ('甲' * 900) + f'用户（{name}）喜欢先给结论。'
    selected, report = assistance.verify_candidates(query=f'用户称呼{name}',
        candidates=[(record, body)], limit=1)
    assert name in captured['user_prompt']
    assert selected == [record] and report['status'] == 'evidence_found'
    assert report['proofs'][0]['span_start'] > 768
    assert client.timeout_seconds == 90
    client.complete = lambda **_: SimpleNamespace(text='{"selected":[{"id":"0","quote":"甲甲"}]}')
    selected, report = assistance.verify_candidates(query=f'用户称呼{name}',
        candidates=[(record, body)], limit=1)
    assert selected == [] and report['reason'] == 'caller_verification_failed'
    embedded = name + '哥'
    client.complete = lambda **_: SimpleNamespace(text='{"selected":[{"id":"0","quote":"%s"}]}' % name)
    selected, report = assistance.verify_candidates(query=f'用户称呼{name}',
        candidates=[(record, embedded)], limit=1)
    assert selected == [] and report['reason'] == 'caller_verification_failed'


def test_display_name_in_the_record_supports_when_the_model_selects_nothing(monkeypatch):
    from eimemory.identity import operator_display_name
    name = operator_display_name()
    record = SimpleNamespace(record_id='memory-name', aliases=())
    client = SimpleNamespace(timeout_seconds=90, complete=lambda **_: SimpleNamespace(text='{"selected":[]}'))
    monkeypatch.setattr(assistance, 'configured_client', lambda: client)
    selected, report = assistance.verify_candidates(
        query=f'用户称呼{name}',
        candidates=[(record, f'用户（{name}）喜欢先给结论。')],
        limit=1,
    )
    assert selected == [record]
    assert report['status'] == 'evidence_found' and report['outcome'] == 'supported'
    assert report['calls'] == 1 and report['literal_display_name'] is True
    assert report['proofs'][0]['span_start'] >= 0
    negated = SimpleNamespace(record_id='memory-negated', aliases=())
    selected, report = assistance.verify_candidates(
        query=f'用户称呼{name}',
        candidates=[(negated, f'不要叫{name}。')],
        limit=1,
    )
    assert selected == [] and report['outcome'] == 'no_support'


def test_model_timeout_stays_a_verification_failure(monkeypatch):
    import subprocess
    def complete(**_kwargs):
        raise subprocess.TimeoutExpired('luna', 90)
    monkeypatch.setattr(assistance, 'configured_client', lambda: SimpleNamespace(
        timeout_seconds=90, complete=complete))
    selected, report = assistance.verify_candidates(query='提交带推送',
        candidates=[(SimpleNamespace(record_id='one', aliases=()), '提交时带上推送说明。')], limit=1)
    assert selected == [] and report['calls'] == 1
    assert report['reason'] == 'caller_verification_failed' and report['error_type'] == 'TimeoutExpired'


def test_assistance_does_not_call_model_when_budget_is_spent(monkeypatch):
    monkeypatch.setattr(assistance, 'llm_client_from_env', lambda _: (_ for _ in ()).throw(AssertionError()))
    selected, report = assistance.verify_candidates(query='query',
        candidates=[(SimpleNamespace(record_id='one'), 'evidence')], limit=1, deadline_at=1)
    assert selected == [] and report['calls'] == 0


def test_yes_no_word_alone_does_not_override_admitted_evidence(monkeypatch):
    # Contract: skip only with asserted independent non-dense evidence.
    # A bare non-empty chosen list (similarity candidates) still requires verify.
    monkeypatch.setenv('EIMEMORY_CALLER_ASSISTED_RECALL_ENABLED','1')
    record = SimpleNamespace(record_id='already-admitted')
    assert assistance.needs_verification('是否有已保存的说明？',[record])
    assert not assistance.needs_verification(
        '是否有已保存的说明？',[record], independent_evidence='identity_lookup')
    assert not assistance.needs_verification(
        'Is there a saved explanation?',[record], independent_evidence='keyword_exact')
    assert assistance.needs_verification(
        '是否只看标题就够了？',[record], independent_evidence='identity_lookup')
    assert assistance.needs_verification(
        'Should I only read the title?',[record], independent_evidence='lexical_durable')
    assert assistance.needs_verification('是否有已保存的说明？',[])


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
