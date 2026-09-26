"""One-shot Python protocol regressions. No live provider, corpus or deployment."""
import json
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest

from eimemory.llm import command_client as cc
from eimemory.llm.completion_timing import safe_timing, safe_child_timing, FAILURE_SCHEMA, BRIDGE_TIMING_FIELDS
from eimemory.retrieval import caller_assistance as ca
from eimemory.retrieval.diagnostics import compact_recall_diagnostics


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    for key in ('EIMEMORY_RECALL_GATEWAY_POOL', 'EIMEMORY_RECALL_GATEWAY_PREWARM',
                'EIMEMORY_RECALL_ATTRIBUTE_PRECHECK'):
        monkeypatch.setenv(key, '0')
    for key in ('EIMEMORY_RECALL_EXPECTED_MODEL', 'EIMEMORY_RECALL_LLM_COMMAND', 'EIMEMORY_LLM_COMMAND',
                'EIMEMORY_RECALL_FALLBACK_MODEL', 'EIMEMORY_RECALL_FALLBACK_PROVIDER'):
        monkeypatch.delenv(key, raising=False)


def complete(script, timeout=2):
    command = cc.CommandLLMClient([sys.executable, '-c', script], timeout_seconds=2)
    command.timeout_seconds = timeout
    return command.complete(system_prompt='fixture-only', user_prompt='fixture-only')


SUCCESS = "import json,sys;json.load(sys.stdin);print(json.dumps({'text':'{\\\"selected\\\":[]}', 'provider_id':'fixture-provider', 'model_id':'fixture-model'}))"


def frame(error='provider_request_failed', diagnostics=None):
    return {'schema': FAILURE_SCHEMA, 'error': error,
            'diagnostics': diagnostics if diagnostics is not None else {'provider_response_ms': 12.25}}


def failure_command(payload, exit_code=1):
    return ('import sys,json;json.load(sys.stdin);'
            + 'sys.stderr.write("private-exception request-token quote\\n");'
            + 'sys.stdout.write(' + repr(json.dumps(payload)) + ');'
            + f'raise SystemExit({exit_code})')


def candidate(text='Read the complete document, not merely its title.'):
    return [(SimpleNamespace(record_id='fixture-memory', aliases=()), text)]


def client(monkeypatch, text='{"selected":[]}', error=None, model_id='fixture-model', provider_id='fixture-provider'):
    calls = []
    def run(**kwargs):
        calls.append(kwargs)
        if error is not None:
            raise error
        return SimpleNamespace(text=text, model_id=model_id, provider_id=provider_id, diagnostics={
            'provider_response_ms': 12.5, 'private_exception': 'must-not-appear'})
    obj = SimpleNamespace(complete=run, timeout_seconds=90)
    monkeypatch.setattr(ca, 'configured_client', lambda: obj)
    return obj, calls


@pytest.mark.parametrize('key', BRIDGE_TIMING_FIELDS)
def test_all_python_bridge_timings_pass_whitelist(key):
    assert safe_timing({key: 1.25, 'secret': 'private'}) == {key: 1.25}
    assert safe_child_timing({key: 1.25}) == {key: 1.25}


@pytest.mark.parametrize('value', [None, [], 'private', True, False, -1, float('nan'), float('inf'), -float('inf')])
def test_unmeasured_or_nonfinite_is_missing_not_zero(value):
    assert safe_timing({'provider_response_ms': value}) == {}


def test_large_integers_are_bounded_without_float_overflow():
    assert safe_timing({'provider_response_ms': 10**500}) == {'provider_response_ms': 1_000_000}


def test_children_cannot_spoof_parent_preparation_or_measurements():
    assert safe_child_timing({'command_prepared': True, 'command_spawn_ms': 1, 'command_io_ms': 1,
                              'provider_response_ms': 4}) == {'provider_response_ms': 4}


def test_success_and_two_layer_json_unchanged():
    result = complete(SUCCESS)
    assert json.loads(result.text) == {'selected': []}
    assert result.provider_id == 'fixture-provider' and result.model_id == 'fixture-model'
    assert result.diagnostics['command_prepared'] is False
    for key in ('command_spawn_ms', 'command_io_ms', 'command_decode_ms'):
        assert result.diagnostics[key] >= 0
    assert 'bridge_import_ms' not in result.diagnostics


def test_prepare_carries_actual_popen_measurement(monkeypatch):
    original = cc.subprocess.Popen
    popens = []
    def delayed(*args, **kwargs):
        popens.append(1)
        time.sleep(.025)
        return original(*args, **kwargs)
    monkeypatch.setattr(cc.subprocess, 'Popen', delayed)
    command = cc.CommandLLMClient([sys.executable, '-c', SUCCESS], timeout_seconds=2)
    try:
        command.prepare()
        measured = command._prepared_spawn_ms
        assert measured >= 20
        result = command.complete(system_prompt='fixture', user_prompt='fixture')
        assert result.diagnostics['command_prepared'] is True
        assert result.diagnostics['command_spawn_ms'] == round(measured, 3)
        assert len(popens) == 1
        assert command._prepared_spawn_ms is None and command._prepared_process is None
        second = command.complete(system_prompt='fixture', user_prompt='fixture')
        assert second.diagnostics['command_prepared'] is False and len(popens) == 2
    finally:
        command.close()


def test_close_discards_prepared_measurement():
    command = cc.CommandLLMClient([sys.executable, '-c', 'import time;time.sleep(10)'])
    command.prepare()
    command.close()
    assert command._prepared_spawn_ms is None and command._prepared_process is None


def test_external_prepared_process_without_measurement_does_not_invent_spawn():
    process = subprocess.Popen([sys.executable, '-c', SUCCESS], stdin=subprocess.PIPE,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    timings = {}
    code, _, _ = cc.run_bounded_command([], b'{}', timeout_seconds=2,
                                         prepared_process=process, timings=timings)
    assert code == 0 and timings['command_prepared'] is True
    assert 'command_spawn_ms' not in timings


def test_failed_spawn_is_measured_without_io():
    command = cc.CommandLLMClient(['/definitely-not-a-real-eimemory-command'])
    with pytest.raises(FileNotFoundError) as exc:
        command.complete(system_prompt='fixture', user_prompt='fixture')
    assert exc.value.completion_timing['command_spawn_ms'] >= 0
    assert 'command_io_ms' not in exc.value.completion_timing


def test_parent_timeout_does_not_fabricate_child_stages():
    with pytest.raises(subprocess.TimeoutExpired) as exc:
        complete('import time;time.sleep(3)', timeout=.05)
    timing = exc.value.completion_timing
    assert timing['command_spawn_ms'] >= 0 and timing['command_io_ms'] >= 40
    assert not any(key in timing for key in BRIDGE_TIMING_FIELDS)
    assert 'command_decode_ms' not in timing


@pytest.mark.parametrize('category', ['bridge_import_failed', 'bridge_client_setup_failed',
    'provider_request_failed', 'bridge_response_validation_failed', 'bridge_output_invalid', 'bridge_failed'])
def test_nonzero_safe_failure_channel_remains_failed(category):
    with pytest.raises(cc.CommandCompletionError) as exc:
        complete(failure_command(frame(category)))
    assert exc.value.failure_category == category
    assert exc.value.completion_timing['provider_response_ms'] == 12.25
    assert 'private' not in str(exc.value) + repr(vars(exc.value))


@pytest.mark.parametrize('payload', [
    {'schema': 'other', 'error': 'provider_request_failed', 'diagnostics': {}},
    {'schema': FAILURE_SCHEMA, 'error': 'private-exception-text', 'diagnostics': {}},
    {'schema': FAILURE_SCHEMA, 'error': 'provider_request_failed', 'diagnostics': {}, 'message': 'secret'},
    {'schema': FAILURE_SCHEMA, 'error': 'provider_request_failed', 'diagnostics': 'secret'},
    {'text': '{}', 'provider_id': 'fixture', 'model_id': 'fixture'},
])
def test_malformed_failure_frames_do_not_become_answers_or_leak(payload):
    with pytest.raises(cc.CommandCompletionError) as exc:
        complete(failure_command(payload))
    assert exc.value.failure_category == ''
    assert not any(key in exc.value.completion_timing for key in BRIDGE_TIMING_FIELDS)


@pytest.mark.parametrize('raw', [b'log\n{}', b'{}\n{}', b'\xff', b'x'*4097,
    b'{"schema":"eimemory.command-failure.v1","schema":"eimemory.command-failure.v1","error":"bridge_failed","diagnostics":{}}',
    b'{"schema":"eimemory.command-failure.v1","error":"bridge_failed","diagnostics":{"provider_response_ms":NaN}}'])
def test_failure_parser_rejects_mixed_duplicate_invalid_or_unbounded_frames(raw):
    assert cc._failure_frame(raw) == ('', {})


def test_stderr_only_frame_is_not_a_diagnostic_channel():
    script = 'import sys;sys.stderr.write(' + repr(json.dumps(frame())) + ');sys.exit(1)'
    with pytest.raises(cc.CommandCompletionError) as exc:
        complete(script)
    assert exc.value.failure_category == '' and 'provider_response_ms' not in exc.value.completion_timing


def test_failure_envelope_on_zero_exit_is_not_success():
    with pytest.raises(ValueError):
        complete(failure_command(frame(), exit_code=0))


@pytest.mark.parametrize('mode', ['empty', 'nonpositive', 'budget', 'unconfigured', 'badjson', 'timeout', 'identity'])
def test_every_verification_exit_has_total_timing(monkeypatch, mode):
    kwargs = dict(query='How should this be read?', candidates=candidate(), limit=1)
    if mode == 'empty': kwargs['candidates'] = []
    elif mode == 'nonpositive': kwargs['limit'] = 0
    elif mode == 'budget': kwargs['deadline_at'] = time.perf_counter() - 1
    elif mode == 'unconfigured': monkeypatch.setattr(ca, 'configured_client', lambda: None)
    elif mode == 'badjson': client(monkeypatch, text='not JSON')
    elif mode == 'timeout': client(monkeypatch, error=subprocess.TimeoutExpired('fixture', .1))
    elif mode == 'identity':
        client(monkeypatch)
        monkeypatch.setenv('EIMEMORY_RECALL_EXPECTED_MODEL', 'other')
    chosen, report = ca.verify_candidates(**kwargs)
    assert not chosen and report['elapsed_ms'] >= 0 and isinstance(report['stages_ms'], dict)
    if mode not in ('empty', 'nonpositive'):
        assert report['status'] == 'unavailable'
    if mode == 'identity':
        assert report['transport']['provider_response_ms'] == 12.5


def test_channel_route_accepts_that_channels_model_only(monkeypatch):
    quote = 'Read the complete document'
    text = json.dumps({'selected': [{'id': '0', 'quote': quote}]})
    monkeypatch.setenv('EIMEMORY_RECALL_EXPECTED_MODEL', 'gpt-5.6-luna')
    from eimemory.llm.command_client import bind_verifier_route, reset_verifier_route
    token = bind_verifier_route({'provider': 'xai-oauth', 'model': 'grok-4.7',
                                 'fallback_provider': 'xai-oauth', 'fallback_model': 'grok-4.6'})
    try:
        client(monkeypatch, text=text, model_id='grok-4.7', provider_id='xai-oauth')
        chosen, report = ca.verify_candidates(query='How should this be read?', candidates=candidate(), limit=1)
        assert chosen and report['model_route'] == 'channel'
        client(monkeypatch, text=text, model_id='grok-4.6', provider_id='xai-oauth')
        chosen, report = ca.verify_candidates(query='How should this be read?', candidates=candidate(), limit=1)
        assert chosen and report['model_route'] == 'channel_fallback'
        client(monkeypatch, text=text, model_id='gpt-6', provider_id='openai-codex')
        chosen, report = ca.verify_candidates(query='How should this be read?', candidates=candidate(), limit=1)
        assert not chosen and report['reason'] == 'caller_model_identity_changed'
    finally:
        reset_verifier_route(token)


def test_nonzero_bridge_reason_and_timing_reach_compact_rpc(monkeypatch):
    command = cc.CommandLLMClient([sys.executable, '-c', failure_command(frame())], timeout_seconds=2)
    monkeypatch.setattr(ca, 'configured_client', lambda: command)
    _, report = ca.verify_candidates(query='How should this be read?', candidates=candidate(), limit=1)
    assert report['status'] == 'unavailable' and report['reason'] == 'caller_verification_failed'
    compact = compact_recall_diagnostics({'engine_diagnostics': {}, 'relevance_selector': {
        'status': report['status'], 'caller_assistance': report}})
    assert compact['admission_timing_available'] is False
    value = compact['caller_assistance']
    assert value['failure_category'] == 'provider_request_failed'
    assert value['transport']['provider_response_ms'] == 12.25
    assert 'bridge_client_setup_ms' not in value['transport']
    assert 'private' not in json.dumps(compact)


@pytest.mark.parametrize('configured_timeout,expected_timeout', [
    (2, 2), (9, 9), (90, 90), (900, 600),
])
def test_proof_and_candidate_limits_are_unchanged(monkeypatch, configured_timeout, expected_timeout):
    quote = 'Read the complete document'
    obj, calls = client(monkeypatch, text=json.dumps({'selected': [{'id': '0', 'quote': quote}]}))
    obj.timeout_seconds = configured_timeout
    items = candidate('Read the complete document' + 'x' * 1000) * 10
    chosen, report = ca.verify_candidates(query='How should this be read?', candidates=items, limit=1)
    assert chosen == [items[0][0]] and report['proofs'][0]['span_end'] == len(quote)
    sent = json.loads(calls[0]['user_prompt'])['candidates']
    assert len(sent) == 8 and all(len(row['text']) == 768 for row in sent)
    assert obj.timeout_seconds == expected_timeout


def test_expired_collection_budget_never_starts_verifier(monkeypatch):
    _, calls = client(monkeypatch)
    chosen, report = ca.verify_candidates(
        query='How should this be read?', candidates=candidate(), limit=1,
        deadline_at=time.perf_counter() - 1)
    assert not chosen and not calls
    assert report['status'] == 'unavailable'
    assert report['reason'] == 'assistance_budget_exhausted'


def test_fabricated_quote_still_fails_with_provider_timing_retained(monkeypatch):
    client(monkeypatch, text='{"selected":[{"id":"0","quote":"invented evidence"}]}')
    _, report = ca.verify_candidates(query='How should this be read?', candidates=candidate(), limit=1)
    assert report['status'] == 'unavailable' and report['transport']['provider_response_ms'] == 12.5


def test_compact_diagnostics_drop_unknown_fields_and_preserve_missing():
    result = compact_recall_diagnostics({'engine_diagnostics': {}, 'relevance_selector': {
        'caller_assistance': {'status': 'unavailable', 'failure_category': 'private',
            'elapsed_ms': 1.2, 'transport': {'bridge_import_ms': True,
                'bridge_elapsed_ms': 10, 'provider_response_ms': -1,
                'command_prepared': True, 'raw_stderr': 'secret'}}}})
    assist = result['caller_assistance']
    assert 'failure_category' not in assist
    assert assist['transport'] == {'bridge_elapsed_ms': 10, 'command_prepared': True}
    assert not result['admission_timing_available']


def test_diagnostics_do_not_change_value_equality_or_hash():
    a = cc.LLMResult('{}', 'fixture', 'fixture')
    b = cc.LLMResult('{}', 'fixture', 'fixture', {'bridge_import_ms': 1})
    assert a == b and hash(a) == hash(b)


def test_compact_large_integers_do_not_break_reporting():
    huge = 10**500
    value = compact_recall_diagnostics({'engine_diagnostics': {'elapsed_ms': huge},
        'relevance_selector': {'elapsed_ms': huge, 'caller_assistance': {
            'elapsed_ms': huge, 'stages_ms': {'completion': huge},
            'transport': {'provider_response_ms': huge}}}})
    assert value['elapsed_ms'] == 1_000_000
    assert value['admission_timing_available'] is True
    assert value['caller_assistance']['transport']['provider_response_ms'] == 1_000_000
