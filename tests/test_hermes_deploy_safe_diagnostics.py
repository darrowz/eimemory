"""Synthetic verifier failures; no network, provider runtime, or real receipts."""
from __future__ import annotations

import json
import traceback

import pytest

from deploy.verify_hermes_integration import (
    _RPCFailure,
    _rpc_tool_result,
    _timed_recall_tool_result,
)


class _Replies:
    def __init__(self, *replies):
        self.replies = list(replies)
        self.calls = 0

    def handle_tool_call(self, _name, _args):
        self.calls += 1
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return json.dumps(reply)


def _call(provider, **kwargs):
    return _rpc_tool_result(
        provider, "eimemory_verify_outcome", {"result": "private_context_value"},
        operation="verified outcome", **kwargs,
    )


@pytest.mark.parametrize("status,code", [(400, "invalid_request"), (401, "unauthorized")],
                         ids=["business-refusal", "authorization-refusal"])
def test_http_refusal_reports_safe_diagnostic_and_never_retries(monkeypatch, status, code):
    sleeps = []
    monkeypatch.setattr("deploy.verify_hermes_integration.time.sleep", sleeps.append)
    provider = _Replies(
        {"ok": False, "bypassed": True, "error": "adapter_unavailable", "result": None,
         "diagnostic": {"reason": "http_error", "http_status": status, "rpc_error": code,
                        "url": "https://private.invalid", "body": "private_body_value"}},
        {"ok": True, "result": {"ok": True, "outcome_trace": "must_not_consume"}},
    )
    with pytest.raises(_RPCFailure, match="RPC operation failed") as caught:
        _call(provider)
    assert provider.calls == 1 and sleeps == []
    assert caught.value.diagnostics == [{
        "method": "adapter.record_terminal", "attempt": 1, "ok": False, "bypassed": True,
        "reason": "http_error", "http_status": status, "rpc_error": code,
    }]
    assert "private" not in str(caught.value)


@pytest.mark.parametrize("reason", ["timeout", "connection_error", "circuit_open"])
def test_transient_reply_can_recover_within_existing_bound(monkeypatch, reason):
    sleeps = []
    monkeypatch.setattr("deploy.verify_hermes_integration.time.sleep", sleeps.append)
    successful = {"ok": True, "outcome_trace": "synthetic_receipt_reference"}
    provider = _Replies(
        {"ok": False, "bypassed": True, "error": "adapter_unavailable",
         "diagnostic": {"reason": reason}},
        {"ok": True, "result": successful},
    )
    assert _call(provider) == successful
    assert provider.calls == 2 and sleeps == [0.25]


def test_failure_lists_only_actual_attempts_in_order(monkeypatch):
    sleeps = []
    monkeypatch.setattr("deploy.verify_hermes_integration.time.sleep", sleeps.append)
    provider = _Replies(*[
        {"ok": False, "bypassed": True, "diagnostic": {"reason": reason}}
        for reason in ("timeout", "connection_error", "circuit_open")
    ])
    with pytest.raises(_RPCFailure) as caught:
        _call(provider)
    assert provider.calls == 3 and sleeps == [0.25, 0.5]
    assert caught.value.diagnostics == [
        {"method": "adapter.record_terminal", "attempt": attempt,
         "ok": False, "bypassed": True, "reason": reason}
        for attempt, reason in enumerate(("timeout", "connection_error", "circuit_open"), 1)
    ]


def test_prior_transient_then_business_refusal_stops_with_both_observations(monkeypatch):
    sleeps = []
    monkeypatch.setattr("deploy.verify_hermes_integration.time.sleep", sleeps.append)
    provider = _Replies(
        {"ok": False, "bypassed": True, "diagnostic": {"reason": "timeout"}},
        {"ok": False, "bypassed": True,
         "diagnostic": {"reason": "http_error", "http_status": 400}},
        {"ok": True, "result": {"ok": True}},
    )
    with pytest.raises(_RPCFailure) as caught:
        _call(provider)
    assert provider.calls == 2 and sleeps == [0.25]
    assert [item["attempt"] for item in caught.value.diagnostics] == [1, 2]
    assert caught.value.diagnostics[-1]["http_status"] == 400


def test_provider_local_failure_does_not_invent_transport_cause_or_retry():
    provider = _Replies({"ok": False, "bypassed": True, "error": "adapter_unavailable"})
    with pytest.raises(_RPCFailure) as caught:
        _call(provider)
    assert provider.calls == 1
    assert caught.value.diagnostics == [{
        "method": "adapter.record_terminal", "attempt": 1, "ok": False,
        "bypassed": True, "reason": "adapter_unavailable",
    }]


def test_invocation_exception_hides_raw_text_and_has_no_fabricated_reply():
    secret = "exception_contains_sensitive_context"
    provider = _Replies(RuntimeError(secret))
    with pytest.raises(_RPCFailure) as caught:
        _call(provider)
    assert provider.calls == 1
    assert caught.value.diagnostics == [{"method": "adapter.record_terminal", "attempt": 1}]
    assert secret not in "".join(traceback.format_exception(caught.value))
    assert caught.value.__suppress_context__ is True


@pytest.mark.parametrize("payload", [
    {"ok": False, "bypassed": True, "error": "opaque_secret_value",
     "diagnostic": {"reason": "opaque_secret_value", "http_status": True,
                    "rpc_error": "opaque_secret_value"}},
    {"ok": False, "bypassed": True, "diagnostic": {"reason": ["timeout"],
     "http_status": "401", "rpc_error": {"invalid_request": "opaque_secret_value"}}},
    {"ok": False, "bypassed": True, "diagnostic": {"reason": "opaque_secret_value",
     "http_status": 999, "rpc_error": "opaque_secret_value"}},
], ids=["lexically-safe-secret", "wrong-types", "out-of-range-status"])
def test_unknown_codes_and_private_payload_are_omitted(payload):
    payload.update({"url": "https://private.invalid", "body": "private_body_value",
                    "auth": "private_auth_value", "context": {"text": "private_context_value"}})
    provider = _Replies(payload)
    with pytest.raises(_RPCFailure) as caught:
        _call(provider)
    assert caught.value.diagnostics == [{
        "method": "adapter.record_terminal", "attempt": 1, "ok": False, "bypassed": True,
    }]
    assert "private" not in str(caught.value) and "opaque_secret_value" not in str(caught.value)
    assert provider.calls == 1


@pytest.mark.parametrize("payload", [None, [], "private_body_value"],
                         ids=["null", "array", "string"])
def test_invalid_envelope_is_fail_closed_without_fabricated_boolean(payload):
    provider = _Replies(payload)
    with pytest.raises(_RPCFailure) as caught:
        _call(provider)
    assert provider.calls == 1
    assert caught.value.diagnostics == [{
        "method": "adapter.record_terminal", "attempt": 1, "reason": "invalid_response",
    }]
    assert "private_body_value" not in str(caught.value)


def test_nested_rejection_cannot_be_retried_or_promoted_by_outer_ok():
    provider = _Replies({"ok": True, "bypassed": False, "result": {
        "ok": False, "error": "original_proactive_release_unverified", "body": "private_body_value",
    }})
    with pytest.raises(_RPCFailure, match="RPC operation failed") as caught:
        _call(provider)
    assert provider.calls == 1
    assert caught.value.diagnostics == [{
        "method": "adapter.record_terminal", "attempt": 1, "ok": True, "bypassed": False,
        "rpc_error": "original_proactive_release_unverified",
    }]


def test_timed_recall_filters_reason_and_remains_single_attempt(monkeypatch):
    monkeypatch.setattr("deploy.verify_hermes_integration.time.monotonic", lambda: 100.0)
    provider = _Replies({"ok": False, "bypassed": True,
                         "diagnostic": {"reason": "private_reason_value", "http_status": 401}})
    with pytest.raises(_RPCFailure, match=r"recall tool call failed \(transport\)") as caught:
        _timed_recall_tool_result(provider, {"query": "private_context_value"}, timeout_seconds=30.0)
    assert provider.calls == 1
    assert caught.value.diagnostics == [{
        "method": "adapter.prefetch", "attempt": 1, "ok": False, "bypassed": True, "http_status": 401,
    }]
    assert "private" not in str(caught.value)


def test_malformed_json_reply_is_sanitized_and_never_retried():
    class MalformedReply:
        calls = 0

        def handle_tool_call(self, _name, _args):
            self.calls += 1
            return '{"private_body_value":'

    provider = MalformedReply()
    with pytest.raises(_RPCFailure) as caught:
        _call(provider)
    assert provider.calls == 1
    assert caught.value.diagnostics == [{
        "method": "adapter.record_terminal", "attempt": 1, "reason": "invalid_response",
    }]
    assert "private_body_value" not in "".join(traceback.format_exception(caught.value))


def test_bypassed_reply_cannot_pass_even_with_success_booleans():
    provider = _Replies({"ok": True, "bypassed": True, "error": "adapter_unavailable",
                         "result": {"ok": True, "outcome_trace": "not_a_verified_result"}})
    with pytest.raises(_RPCFailure):
        _call(provider)
    assert provider.calls == 1
