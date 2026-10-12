"""Native isolated Hermes SDK failures, bounded retries and durable readbacks."""
from email.utils import format_datetime
from datetime import datetime, timedelta, timezone
import json
import sys
import time
from types import SimpleNamespace

import pytest

from eimemory.api.runtime import Runtime
from eimemory.intake import closure_review
from eimemory.intake.closure import REVIEW_STATUS_REVIEWED, REVIEW_STATUS_UNAVAILABLE
from eimemory.llm import hermes_review_command as bridge
from eimemory.llm.command_client import CommandCompletionError, CommandLLMClient, _failure_frame
from eimemory.llm.completion_timing import (
    FAILURE_SCHEMA, MAX_PROVIDER_RETRY_AFTER_MS, provider_failure,
    safe_child_timing, safe_provider_failure, safe_timing,
)
from eimemory.llm.research_review import research_review_client
from eimemory.scheduler.result_contract import _nightly_step, nightly_result_diagnostics
from eimemory.governance.learning.supervisor import supervisor_summary, persist_supervisor_summary
from test_nightly_evidence_workflow import SCOPE, _closure
from test_research_review_hermes import host


PRIVATE = "private-provider-token private-query private-response-body private-header"


def sdk_host(host, *, error_type="RuntimeError", status=None, **changes):
    root, home, _binary, path, config = host
    module = root / "agent/auxiliary_client.py"
    source = module.read_text()
    source = source.replace("if value.get('failure'):\n            raise RuntimeError('private-provider-token')", """
        if value.get('failure_kind') and len(marker.read_text()) <= value.get('failure_until', 100):
            error_class = type(value['failure_kind'], (Exception,), {})
            error = error_class('private-provider-token private-query private-response-body')
            error.status_code = value.get('status_code')
            error.response = NS(status_code=value.get('status_code'),
                headers={'retry-after': value.get('retry_after'), 'authorization': 'private-header'},
                text='private-response-body', body='private-provider-token')
            if value.get('flip_provider'):
                client._hermes_aux_effective_provider = 'unapproved-alternate-provider'
            if value.get('flip_retry_policy'):
                client.max_retries = 2
            raise error
""")
    source = source.replace("return NS(chat=NS(completions=NS(create=create)), close=lambda: None), model", """
    client = NS(chat=NS(completions=NS(create=create)), close=lambda: None,
                _hermes_aux_effective_provider=provider)
    if not value.get('sdk_retry_policy_unverified'):
        client.max_retries = value.get('sdk_max_retries', 0)
    if value.get('close_failure'):
        def close():
            raise RuntimeError('private-client-cleanup-token')
        client.close = close
    return client, model
""")
    source = source.replace("message = NS(content=json.dumps(review),", """
        if value.get('bad_review'):
            review = {'unapproved_field': 'private-review-body'}
        message = NS(content=json.dumps(review),""")
    module.write_text(source)
    config.update(failure_kind=error_type, status_code=status, **changes)
    path.write_text(json.dumps(config))
    return config


@pytest.mark.parametrize("error_type,status,expected,attempts", [
    ("AuthenticationError", 401, "authentication_rejected", 1),
    ("PermissionDeniedError", 403, "permission_rejected", 1),
    ("RateLimitError", 429, "rate_limited", 2),
    ("InternalServerError", 503, "provider_server_error", 2),
    ("APITimeoutError", None, "request_timeout", 2),
    ("APIConnectionError", None, "connection_failed", 2),
    ("BadRequestError", 400, "request_rejected", 1),
    ("RuntimeError", None, "unknown", 1),
])
def test_native_sdk_failures_keep_only_typed_bounded_metadata(host, tmp_path, error_type, status, expected, attempts):
    sdk_host(host, error_type=error_type, status=status)
    with Runtime.create(root=tmp_path / "store") as runtime:
        record = _closure(runtime)
        # Old unrelated business metadata remains historical, never selected
        # as this attempt's SDK status or provider error evidence.
        record.meta["business_meta"] = {"review_failure": {"http_status": 403, "error": "private-historical-token"}}
        runtime.store.rewrite(record)
        steps = []
        report = _nightly_step(steps, "research_closure_review",
            lambda: closure_review.review_pending_research_closures(runtime, scope=SCOPE))
        assert not report["ok"] and report["reviewed"] == 0
        assert report["unavailable_records"][0]["error"] == "command_completion_failed:provider_request_failed"
        source = runtime.store.get_by_id(record.record_id, scope=SCOPE)
        assert source.meta["review_status"] == REVIEW_STATUS_UNAVAILABLE
        detail = source.content["review_failure"]["completion_timing"]["provider_failure"]
        assert detail["failure_class"] == expected
        assert detail["bridge_attempt_count"] == attempts
        assert detail["outcome"] == "failed" and detail["sdk_max_retries"] == 0
        assert detail.get("http_status") == status
        assert "private" not in json.dumps(source.content["review_failure"])
        assert source.meta["business_meta"]["review_failure"]["http_status"] == 403
        summary = supervisor_summary(command="nightly", ok=False, duration_ms=1, memory_peak=0)
        summary["nightly_diagnostics"] = nightly_result_diagnostics({"research_closure_review": report}, steps)
        receipt = persist_supervisor_summary(runtime, scope=SCOPE, summary=summary)
        receipt_id = receipt.record_id
    with Runtime.create(root=tmp_path / "store") as runtime:
        source = runtime.store.get_by_id(record.record_id, scope=SCOPE)
        persisted = runtime.store.get_by_id(receipt_id, scope=SCOPE)
        item = persisted.content["nightly_diagnostics"]["research_closure_review"]["unavailable_records"][0]
        assert item["record_id"] == record.record_id
        assert item["completion_timing"]["provider_failure"] == detail
        assert source.meta["review_failure"] == source.content["review_failure"]
        assert "private" not in json.dumps(item)
    assert (host[1] / "sdk-calls").read_text() == "x" * attempts


def test_recovered_retry_keeps_same_discovered_configuration_and_current_durable_record(host, tmp_path):
    sdk_host(host, error_type="RateLimitError", status=429, failure_until=1, retry_after="0.001")
    with Runtime.create(root=tmp_path / "store") as runtime:
        record = _closure(runtime)
        record.meta["business_meta"] = {"review_failure": {"error": "private-old-failure"}}
        runtime.store.rewrite(record)
        report = closure_review.review_pending_research_closures(runtime, scope=SCOPE)
        assert report["ok"] and report["reviewed"] == 1
        source = runtime.store.get_by_id(record.record_id, scope=SCOPE)
        assert source.meta["review_status"] == REVIEW_STATUS_REVIEWED
        assert source.meta["review_model_used"] == "gpt-6.1-sol"
        assert source.meta["review_provider_used"] == "custom-review"
        assert source.meta["review_error"] == "" and source.meta["review_failure"] == {}
        diagnostic = source.meta["review_completion_timing"]["provider_failure"]
        assert diagnostic["failure_class"] == "rate_limited" and diagnostic["http_status"] == 429
        assert diagnostic["outcome"] == "recovered" and diagnostic["bridge_attempt_count"] == 2
        assert diagnostic["retry_stop"] == "completion_received"
        assert source.content["review_completion_timing"] == source.meta["review_completion_timing"]
        assert report["reviewed_records"][0]["completion_timing"]["provider_failure"] == diagnostic
        assert "private" not in json.dumps(source.meta["review_completion_timing"])
    assert (host[1] / "sdk-calls").read_text() == "xx"


@pytest.mark.parametrize("invalid", [{"response_model": "different-model"}, {"tools": [{"name": "terminal"}]}])
def test_retry_response_validation_failure_is_terminal_and_counts_the_actual_second_call(host, invalid):
    sdk_host(host, error_type="RateLimitError", status=429, failure_until=1, retry_after="0", **invalid)
    with pytest.raises(CommandCompletionError) as error:
        research_review_client().complete(system_prompt="fixture system", user_prompt="fixture", json_mode=True)
    assert error.value.failure_category == "bridge_response_validation_failed"
    detail = error.value.completion_timing["provider_failure"]
    assert detail["bridge_attempt_count"] == 2 and detail["outcome"] == "failed"
    assert detail["retry_stop"] == "response_validation_failed"
    assert (host[1] / "sdk-calls").read_text() == "xx"


def test_retry_recovery_does_not_certify_invalid_final_research_review(host, tmp_path):
    sdk_host(host, error_type="RateLimitError", status=429, failure_until=1, retry_after="0", bad_review=True)
    with Runtime.create(root=tmp_path / "store") as runtime:
        record = _closure(runtime)
        report = closure_review.review_pending_research_closures(runtime, scope=SCOPE)
        assert not report['ok'] and report['reviewed'] == 0
        saved = runtime.store.get_by_id(record.record_id, scope=SCOPE)
        assert saved.content['review_error'] == 'research_review_output_invalid'
        assert saved.content['model_review'] == ''
        current = saved.content['review_failure']
        assert current['stage'] == 'output_validation'
        detail = current['completion_timing']['provider_failure']
        assert detail['bridge_attempt_count'] == 2 and detail['outcome'] == 'failed'
        assert detail['retry_stop'] == 'review_validation_failed'
        assert 'private' not in json.dumps(current)
    assert (host[1] / 'sdk-calls').read_text() == 'xx'


def test_native_client_cleanup_failure_cannot_leave_recovered_current_review(host, tmp_path):
    sdk_host(host, error_type='RateLimitError', status=429, failure_until=1, retry_after='0', close_failure=True)
    with Runtime.create(root=tmp_path / 'store') as runtime:
        record = _closure(runtime)
        report = closure_review.review_pending_research_closures(runtime, scope=SCOPE)
        assert not report['ok'] and report['reviewed'] == 0
        assert report['unavailable_records'][0]['error'] == 'command_completion_failed:bridge_response_validation_failed'
        saved = runtime.store.get_by_id(record.record_id, scope=SCOPE)
        assert saved.meta['review_status'] == REVIEW_STATUS_UNAVAILABLE
        assert saved.content['model_review'] == ''
        detail = saved.content['review_failure']['completion_timing']['provider_failure']
        assert detail['outcome'] == 'failed' and detail['bridge_attempt_count'] == 2
        assert detail['retry_stop'] == 'client_cleanup_failed'
        assert detail['first_failure']['http_status'] == 429
        assert detail['first_failure']['bridge_attempt_count'] == 1
        assert 'private' not in json.dumps(saved.content['review_failure'])
    assert (host[1] / 'sdk-calls').read_text() == 'xx'


@pytest.mark.parametrize("changes,expected", [
    ({"sdk_max_retries": 2}, "sdk_retries_already_configured"),
    ({"sdk_retry_policy_unverified": True}, "sdk_retry_policy_unverified"),
    ({"flip_provider": True}, "provider_identity_changed"),
    ({"flip_retry_policy": True}, "sdk_retry_policy_changed"),
    ({"retry_after": "31"}, "retry_after_exceeds_bound"),
    ({"retry_after": "private-unparseable-header"}, "retry_after_unavailable"),
])
def test_native_retry_stops_do_not_change_provider_or_multiply_sdk_attempts(host, changes, expected):
    sdk_host(host, error_type="RateLimitError", status=429, **changes)
    client = research_review_client()
    with pytest.raises(CommandCompletionError) as result:
        client.complete(system_prompt="fixture system", user_prompt="private artifact", json_mode=True)
    detail = result.value.completion_timing["provider_failure"]
    assert detail["retry_stop"] == expected and detail["bridge_attempt_count"] == 1
    assert (host[1] / "sdk-calls").read_text() == "x"
    assert "private" not in json.dumps(result.value.completion_timing)


def test_request_deadline_includes_retry_delay_and_original_sdk_call():
    calls = []
    failure = type("RateLimitError", (Exception,), {})()
    failure.status_code = 429
    failure.retry_after = .2
    def create(**kwargs):
        calls.append(kwargs)
        time.sleep(.1)
        raise failure
    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)), max_retries=0)
    timings = {}
    with pytest.raises(type(failure)):
        bridge._request_completion(client, {"model": "actual", "timeout": .75}, provider="actual",
            deadline_unix_ms=(time.time() + .75) * 1000, timings=timings)
    assert len(calls) == 1 and calls[0]["timeout"] <= .75
    assert timings["provider_failure"]["retry_stop"] == "deadline_budget"
    assert "provider_retry_wait_ms" not in timings


def test_sdk_status_and_delay_classification_never_reads_message_body_or_unknown_attributes():
    class HTTPStatusError(Exception):
        status_code = 503
        response = SimpleNamespace(status_code=503, headers={"Retry-After": "0.025", "Authorization": PRIVATE})
        def __str__(self):
            raise AssertionError("No exception message access")
        @property
        def body(self):
            raise AssertionError("No error body access")
    result = provider_failure(HTTPStatusError(PRIVATE))
    assert result["http_status"] == 503 and result["retry_after_ms"] == 25
    assert "private" not in json.dumps(result)
    class UnreadableError(Exception):
        @property
        def response(self):
            raise RuntimeError(PRIVATE)
        @property
        def status_code(self):
            raise RuntimeError(PRIVATE)
    unknown = provider_failure(UnreadableError(PRIVATE))
    assert unknown["failure_class"] == "unknown" and "http_status" not in unknown


@pytest.mark.parametrize("delay", [float("nan"), float("inf"), True, -1, "private-token", {}, None])
def test_invalid_retry_after_never_becomes_unbounded_diagnostics(delay):
    failure = type("RateLimitError", (Exception,), {})()
    failure.retry_after = delay
    result = provider_failure(failure)
    assert "retry_after_ms" not in result
    assert "private" not in json.dumps(result)


def test_retry_after_http_date_and_extreme_integer_are_bounded():
    failure = type("RateLimitError", (Exception,), {})()
    failure.retry_after = format_datetime(datetime.now(timezone.utc) + timedelta(seconds=20), usegmt=True)
    assert 0 < provider_failure(failure)["retry_after_ms"] <= 20_000
    failure.retry_after = 10 ** 500
    result = provider_failure(failure)
    assert result["retry_after_ms"] == MAX_PROVIDER_RETRY_AFTER_MS and result["retry_after_truncated"] is True


@pytest.mark.parametrize("mutation", ["wrong_schema", "unknown_class", "private_type", "wrong_retry", "fake_attempts", "unknown_phase"])
def test_safe_projection_rejects_inconsistent_or_private_provider_frames(mutation):
    failure = type("RateLimitError", (Exception,), {})()
    failure.status_code = 429
    value = provider_failure(failure)
    field, forged = {"wrong_schema": ("schema", "private"), "unknown_class": ("failure_class", PRIVATE),
        "private_type": ("sdk_error_type", PRIVATE), "wrong_retry": ("retryable", False),
        "fake_attempts": ("bridge_attempt_count", 50), "unknown_phase": ("phase", PRIVATE)}[mutation]
    value[field] = forged
    assert safe_provider_failure(value) == {}
    assert safe_timing({"provider_failure": value}) == {}
    assert safe_child_timing({"provider_failure": value}) == {}


@pytest.mark.parametrize("error_type", ["RateLimitError", "InternalServerError"])
def test_server_or_rate_sdk_type_without_status_is_classified_but_not_retried(host, error_type):
    sdk_host(host, error_type=error_type)
    with pytest.raises(CommandCompletionError) as error:
        research_review_client().complete(system_prompt="fixture system", user_prompt="fixture", json_mode=True)
    detail = error.value.completion_timing["provider_failure"]
    assert detail["retryable"] is False and detail["retry_stop"] == "not_retryable"
    assert detail["sdk_error_type"] == error_type and "http_status" not in detail
    assert (host[1] / "sdk-calls").read_text() == "x"


def test_parent_failure_frame_strips_private_fields_and_preserves_typed_provider_detail():
    failure = type("AuthenticationError", (Exception,), {})()
    failure.status_code = 401
    diagnostic = provider_failure(failure, retry_stop="not_retryable")
    raw = json.dumps(dict(schema=FAILURE_SCHEMA, error="provider_request_failed", diagnostics={
        "provider_failure": {**diagnostic, "body": PRIVATE, "headers": {"authorization": PRIVATE}},
        "private": PRIVATE, "command_spawn_ms": 9999, "provider_response_ms": 12})).encode()
    category, safe = _failure_frame(raw)
    assert category == "provider_request_failed" and safe["provider_failure"] == diagnostic
    assert "command_spawn_ms" not in safe and "private" not in json.dumps(safe)


def test_final_sdk_failure_keeps_current_status_and_first_failure_separately():
    errors = []
    for status in (429, 503):
        error = type('HTTPStatusError', (Exception,), {})(PRIVATE)
        error.status_code = status
        error.retry_after = 0
        errors.append(error)
    calls = []
    def create(**kwargs):
        calls.append(kwargs)
        raise errors[len(calls) - 1]
    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)), max_retries=0)
    timings = {}
    with pytest.raises(type(errors[-1])):
        bridge._request_completion(client, {'model': 'actual', 'timeout': 2}, provider='actual',
            deadline_unix_ms=(time.time() + 2) * 1000, timings=timings)
    detail = timings['provider_failure']
    assert len(calls) == 2 and detail['bridge_attempt_count'] == 2
    assert detail['http_status'] == 503 and detail['failure_class'] == 'provider_server_error'
    assert detail['first_failure']['http_status'] == 429
    assert detail['first_failure']['bridge_attempt_count'] == 1
    assert detail['first_failure']['failure_class'] == 'rate_limited'
    assert 'private' not in json.dumps(detail)


@pytest.mark.parametrize('value', [[], {}, None, True, 1, 'private'])
def test_safe_provider_enum_fields_never_raise_on_untrusted_json_types(value):
    error = type('AuthenticationError', (Exception,), {})()
    error.status_code = 401
    original = provider_failure(error)
    for field in ('failure_class', 'sdk_error_type', 'outcome'):
        bad = {**original, field: value}
        assert safe_provider_failure(bad) == {}
