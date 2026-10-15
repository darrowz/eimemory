"""1.14.85: Codex Responses in-stream error frames are classified and retried once."""
import json

import pytest

from eimemory.api.runtime import Runtime
from eimemory.intake import closure_review
from eimemory.llm.completion_timing import provider_failure, safe_provider_failure
from eimemory.scheduler.result_contract import _nightly_step
from test_hermes_provider_failures import sdk_host
from test_nightly_evidence_workflow import SCOPE, _closure
from test_research_review_hermes import host  # noqa: F401  (fixture)


class _StreamErrorEvent(Exception):
    def __init__(self, code):
        super().__init__("private-provider-message")
        self.code, self.status_code = code, None


@pytest.mark.parametrize("code,expected,retryable", [
    ("server_error", "provider_server_error", True),
    ("rate_limit_exceeded", "rate_limited", True),
    ("invalid_request_error", "request_rejected", False),
    ("private-free-text", "unknown", False),
    (None, "unknown", False),
])
def test_stream_error_frames_classify_by_typed_code_only(code, expected, retryable):
    detail = provider_failure(_StreamErrorEvent(code))
    assert detail["sdk_error_type"] == "_StreamErrorEvent"
    assert detail["failure_class"] == expected and detail["retryable"] is retryable
    assert detail.get("stream_error_code", "") == (code if expected != "unknown" else "")
    assert safe_provider_failure(detail) == detail
    assert "private" not in json.dumps(detail)


def test_forged_stream_code_class_is_rejected():
    detail = provider_failure(_StreamErrorEvent("server_error"))
    assert safe_provider_failure({**detail, "stream_error_code": "invalid_request_error"}) == {}


def test_transient_stream_error_recovers_with_one_retry_without_sdk_policy(host, tmp_path):
    # Codex auxiliary clients expose no max_retries; in-stream frames are never SDK-retried.
    sdk_host(host, error_type="_StreamErrorEvent", code="server_error", failure_until=1,
             sdk_retry_policy_unverified=True)
    with Runtime.create(root=tmp_path / "store") as runtime:
        _closure(runtime)
        steps = []
        report = _nightly_step(steps, "research_closure_review",
            lambda: closure_review.review_pending_research_closures(runtime, scope=SCOPE))
        assert report["ok"] and report["reviewed"] == 1
    assert (host[1] / "sdk-calls").read_text() == "xx"


def test_rejected_stream_error_is_not_retried(host, tmp_path):
    sdk_host(host, error_type="_StreamErrorEvent", code="invalid_request_error",
             sdk_retry_policy_unverified=True)
    with Runtime.create(root=tmp_path / "store") as runtime:
        record = _closure(runtime)
        steps = []
        report = _nightly_step(steps, "research_closure_review",
            lambda: closure_review.review_pending_research_closures(runtime, scope=SCOPE))
        assert not report["ok"]
        detail = runtime.store.get_by_id(record.record_id, scope=SCOPE).content["review_failure"]["completion_timing"]["provider_failure"]
        assert detail["failure_class"] == "request_rejected" and detail["retry_stop"] == "not_retryable"
    assert (host[1] / "sdk-calls").read_text() == "x"
