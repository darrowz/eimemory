"""Only current provider observations reach durable nightly research summaries."""
from copy import deepcopy
import json

import pytest

from eimemory.api.runtime import Runtime
from eimemory.governance.learning.supervisor import (
    build_supervisor_contract, persist_supervisor_summary, supervisor_summary,
)
from eimemory.intake import closure_review
from eimemory.intake.closure import REVIEW_STATUS_REVIEWED, REVIEW_STATUS_UNAVAILABLE
from eimemory.scheduler.research_review_diagnostics import research_review_diagnostics
from eimemory.scheduler.result_contract import nightly_result_diagnostics
from test_nightly_evidence_workflow import SCOPE, _closure
from test_research_review_hermes import host


def _provider_failure(*, outcome="recovered", status=429, attempts=2):
    return {"schema": "provider_failure.v1", "phase": "provider_request",
            "failure_class": "rate_limited" if status == 429 else "authentication_rejected",
            "sdk_error_type": "RateLimitError" if status == 429 else "AuthenticationError",
            "http_status": status, "retryable": status == 429, "bridge_attempt_count": attempts,
            "outcome": outcome, "retry_stop": "completion_received" if outcome == "recovered" else "not_retryable"}


def _sdk_failure(host, *, status, recover):
    root, _home, _binary, _path, _value = host
    module = root / "agent/auxiliary_client.py"
    source = module.read_text()
    error = "RateLimitError" if status == 429 else "AuthenticationError"
    source += f"\nclass {error}(Exception):\n    status_code = {status}\n    retry_after = 0\n"
    guard = "marker.read_text() == 'x'" if recover else "True"
    source = source.replace("        if value.get('failure'):\n",
        f"        if {guard}:\n            raise {error}('private-sdk-body-and-credential')\n"
        "        if value.get('failure'):\n")
    source = source.replace("return NS(chat=NS(completions=NS(create=create)), close=lambda: None), model",
        "return NS(chat=NS(completions=NS(create=create)), max_retries=0, "
        "_hermes_aux_effective_provider=provider, close=lambda: None), model")
    module.write_text(source)


@pytest.mark.parametrize("status,recover,review_status,expected_calls", [
    (429, True, REVIEW_STATUS_REVIEWED, "xx"),
    (401, False, REVIEW_STATUS_UNAVAILABLE, "x"),
])
def test_actual_sdk_command_review_and_nightly_readback_preserve_current_failure(
    host, tmp_path, status, recover, review_status, expected_calls,
):
    _sdk_failure(host, status=status, recover=recover)
    data = tmp_path / "native-research-runtime"
    with Runtime.create(root=data) as runtime:
        original = _closure(runtime)
        historical = _provider_failure(outcome="failed", status=401, attempts=1)
        original.meta["business_meta"] = {"review_failure": {
            "completion_timing": {"provider_failure": historical}, "body": "private-historical-body"}}
        runtime.store.rewrite(original)
        report = closure_review.review_pending_research_closures(runtime, scope=SCOPE)
        assert report["ok"] is recover
        assert (host[1] / "sdk-calls").read_text() == expected_calls
        row = report["reviewed_records" if recover else "unavailable_records"][0]
        current = row["completion_timing"]["provider_failure"]
        assert current["http_status"] == status
        assert current["bridge_attempt_count"] == len(expected_calls)
        assert current["outcome"] == ("recovered" if recover else "failed")
        if recover:
            assert current["first_failure"]["http_status"] == 429
            assert current["first_failure"]["bridge_attempt_count"] == 1
            assert current["first_failure"]["outcome"] == "failed"
        saved = runtime.store.get_by_id(original.record_id, scope=SCOPE)
        assert saved.content["review_status"] == review_status
        assert saved.content["review_completion_timing"]["provider_failure"] == current
        assert saved.meta["business_meta"]["review_failure"]["completion_timing"]["provider_failure"] == historical
        diagnostics = nightly_result_diagnostics({"research_closure_review": report}, [])
        research = diagnostics["research_closure_review"]
        native = research["reviewed_provider_records" if recover else "unavailable_records"][0]
        assert native["record_id"] == original.record_id
        assert native["completion_timing"]["provider_failure"] == current
        if recover:
            assert diagnostics["execution_ok"] is True
            assert diagnostics["failed_steps"] == []
            assert research["reason_counts"] == {}
        else:
            assert diagnostics["execution_ok"] is False
            assert research["reason_counts"] == {"command_completion_failed:provider_request_failed": 1}
        assert "private" not in json.dumps(diagnostics)
        summary = supervisor_summary(command="nightly", ok=recover, duration_ms=1, memory_peak=0)
        summary["nightly_diagnostics"] = diagnostics
        receipt_id = persist_supervisor_summary(runtime, scope=SCOPE, summary=summary).record_id
    with Runtime.create(root=data) as runtime:
        assert runtime.store.get_by_id(original.record_id, scope=SCOPE).content["review_status"] == review_status
        durable = runtime.store.get_by_id(receipt_id).content["nightly_diagnostics"]
        assert durable == diagnostics
        contract = build_supervisor_contract(runtime, scope=SCOPE)["runs"]["nightly"]["nightly_diagnostics"]
        assert contract["research_closure_review"] == research


def test_legacy_success_and_historical_business_metadata_do_not_certify_provider_diagnostics():
    report = {"ok": True, "reviewed_records": [{"record_id": "rec_" + "a" * 12,
              "business_meta": {"review_failure": {"completion_timing": {
                  "provider_failure": _provider_failure()}}}}]}
    snapshot = deepcopy(report)
    diagnostics = nightly_result_diagnostics({"research_closure_review": report}, [])
    assert "research_closure_review" not in diagnostics
    assert report == snapshot
    legacy = research_review_diagnostics({"ok": False, "unavailable_records": [{
        "record_id": "rec_" + "a" * 12, "error": "research_review_execution_failed",
        "business_meta": report["reviewed_records"][0]["business_meta"]}]})
    assert "provider_failure" not in json.dumps(legacy)


def test_success_provider_projection_is_bounded_typed_and_excludes_private_fields():
    rows = [{"record_id": "replay_" + f"{index:012x}", "provider": "private-provider",
             "review_output": "private-review-body", "completion_timing": {
                 "provider_failure": {**_provider_failure(), "body": "private-provider-body", "api_key": "private-key"},
                 "provider_response_ms": index, "command_spawn_ms": None,
                 "private-timing": "private-body"}} for index in range(25)]
    rows.append({"record_id": "private-member", "completion_timing": {
        "provider_failure": {**_provider_failure(), "http_status": True}}})
    report = {"ok": True, "reviewed_records": rows}
    snapshot = deepcopy(report)
    result = research_review_diagnostics(report)
    assert result["reviewed_provider_record_count"] == 25
    assert len(result["reviewed_provider_records"]) == 20
    assert result["reviewed_provider_records_truncated"] is True
    assert result["reason_counts"] == {}
    assert "command_spawn_ms" not in result["reviewed_provider_records"][0]["completion_timing"]
    assert "private" not in json.dumps(result)
    assert report == snapshot
