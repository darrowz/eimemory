"""Regression coverage for incomplete nightly evaluation evidence."""
from __future__ import annotations

import json
import os
import sys
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace

import pytest

from eimemory.api.runtime import Runtime
from eimemory.evaluation.capability_catalog import CapabilityEvaluationCatalog, CatalogCase
from eimemory.governance.learning.replay_dataset import _cases_from_evaluation_catalog
from eimemory.intake import closure_review
from eimemory.scheduler.research_review_diagnostics import research_review_diagnostics
from eimemory.governance.learning.supervisor import (
    build_supervisor_contract, persist_supervisor_summary, supervisor_summary,
)
from eimemory.llm.completion_timing import FAILURE_SCHEMA
from eimemory.intake.closure import (
    RESEARCH_CLOSURE_REPORT_TYPE, REVIEW_STATUS_PENDING_MODEL, REVIEW_STATUS_UNAVAILABLE,
)
from eimemory.models.records import RecordEnvelope, ScopeRef, TimeRef
from eimemory.scheduler.jobs import _run_memory_eval_ci
from eimemory.scheduler.result_contract import _nightly_step, nightly_result_diagnostics

SCOPE = ScopeRef(agent_id="research", workspace_id="nightly-evidence", user_id="operator")
REVIEW = {"verdict": "approve", "rationale": "The supplied artifact supports the landing point.",
          "required_followup": "Add an independent replay case.", "risk": "low"}


def _closure(runtime, *, status=REVIEW_STATUS_PENDING_MODEL):
    record = RecordEnvelope.create(kind="replay_result", title="Research closure review",
        summary="Replay buffer research", scope=SCOPE,
        content={"report_type": RESEARCH_CLOSURE_REPORT_TYPE, "review_status": status},
        meta={"report_type": RESEARCH_CLOSURE_REPORT_TYPE, "review_status": status})
    record.time = TimeRef(created_at="2020-01-01T00:00:00Z", updated_at="2020-01-01T00:00:00Z",
                          occurred_at="2020-01-01T00:00:00Z")
    runtime.store.append(record)
    return record


@pytest.mark.parametrize("allowed_model", ["configured-model", "other-model"])
def test_research_review_uses_configured_bridge_and_persists_actual_model(tmp_path, monkeypatch, allowed_model):
    bridge = tmp_path / "review_bridge.py"
    response = json.dumps({"text": json.dumps(REVIEW), "provider_id": "local-review", "model_id": "configured-model"})
    bridge.write_text("import json, sys\nrequest = json.load(sys.stdin)\n"
                     "assert request['json_mode'] is True\n"
                     "assert 'Research closure review' in request['user_prompt']\n"
                     f"print({response!r})\n")
    monkeypatch.setenv("EIMEMORY_LLM_COMMAND", json.dumps([sys.executable, str(bridge)]))
    monkeypatch.setenv("EIMEMORY_ALLOWED_REVIEW_MODELS", allowed_model)
    monkeypatch.delenv("EIMEMORY_RESEARCH_REVIEW_LLM_COMMAND", raising=False)
    monkeypatch.setattr(closure_review, "codex_exec", lambda *_: pytest.fail("configured review must use the bridge"))
    with Runtime.create(root=tmp_path / "data") as runtime:
        record = _closure(runtime)
        report = closure_review.review_pending_research_closures(runtime, scope=SCOPE)
        saved = runtime.store.get_by_id(record.record_id, scope=SCOPE)
        if allowed_model != "configured-model":
            assert report["ok"] is False and report["reviewed"] == 0
            assert saved.meta["review_status"] == REVIEW_STATUS_UNAVAILABLE
            assert saved.content["model_review"] == ""
            return
        assert report["ok"] is True and report["reviewed"] == 1
        assert saved.meta["review_model_used"] == "configured-model"
        assert saved.meta["review_provider_used"] == "local-review"
        assert json.loads(saved.content["model_review"]) == REVIEW


def test_failed_configured_research_review_never_falls_back(tmp_path, monkeypatch):
    shared_response = json.dumps({"text": json.dumps(REVIEW), "provider_id": "shared", "model_id": "shared-model"})
    monkeypatch.setenv("EIMEMORY_LLM_COMMAND", json.dumps([sys.executable, "-c", f"print({shared_response!r})"]))
    monkeypatch.setenv("EIMEMORY_RESEARCH_REVIEW_LLM_COMMAND", json.dumps([sys.executable, "-c", "raise SystemExit(1)"]))
    monkeypatch.setattr(closure_review, "codex_exec", lambda *_: pytest.fail("failed provider must not fall back"))
    with Runtime.create(root=tmp_path / "data") as runtime:
        record = _closure(runtime)
        report = closure_review.review_pending_research_closures(runtime, scope=SCOPE)
        assert report["ok"] is False and report["execution_ok"] is False
        assert report["reviewed"] == 0 and report["unavailable"] == 1
        saved = runtime.store.get_by_id(record.record_id, scope=SCOPE)
        assert saved.meta["review_status"] == REVIEW_STATUS_UNAVAILABLE
        assert saved.content["model_review"] == ""


def test_unconfigured_review_reports_missing_route_without_invoking_codex(tmp_path, monkeypatch):
    monkeypatch.delenv("EIMEMORY_LLM_COMMAND", raising=False)
    monkeypatch.delenv("EIMEMORY_RESEARCH_REVIEW_LLM_COMMAND", raising=False)
    monkeypatch.setattr(closure_review, "codex_exec", lambda *_: pytest.fail("no implicit codex dependency"))
    with Runtime.create(root=tmp_path) as runtime:
        _closure(runtime)
        report = closure_review.review_pending_research_closures(runtime, scope=SCOPE)
        assert report["ok"] is False and report["reviewed"] == 0
        assert report["unavailable_records"][0]["error"] == "research_review_llm_unconfigured"


@pytest.mark.parametrize("failure,expected", [
    ("unconfigured", "research_review_llm_unconfigured"),
    ("bad_configuration", "research_review_llm_configuration_invalid"),
    ("missing_executable", "research_review_command_not_found"),
    ("command_failed", "command_completion_failed"),
    ("bridge_failed", "command_completion_failed:provider_request_failed"),
    ("timeout", "research_review_command_timeout"),
    ("bad_envelope", "research_review_command_response_invalid"),
    ("bad_review", "research_review_output_invalid"),
    ("disallowed_model", "review_model_not_allowed"),
])
def test_review_failure_survives_supervisor_receipt_readback(tmp_path, monkeypatch, failure, expected):
    monkeypatch.delenv("EIMEMORY_LLM_COMMAND", raising=False)
    monkeypatch.delenv("EIMEMORY_RESEARCH_REVIEW_LLM_COMMAND", raising=False)
    monkeypatch.delenv("EIMEMORY_ALLOWED_REVIEW_MODELS", raising=False)
    monkeypatch.setenv("EIMEMORY_RESEARCH_REVIEW_LLM_TIMEOUT_SECONDS", "1")
    envelope = {"text": json.dumps(REVIEW), "provider_id": "fixture", "model_id": "fixture-model"}
    script = "import json,sys;json.load(sys.stdin);print(" + repr(json.dumps(envelope)) + ")"
    if failure == "bad_configuration":
        monkeypatch.setenv("EIMEMORY_RESEARCH_REVIEW_LLM_COMMAND", "private-invalid-configuration")
    elif failure == "missing_executable":
        monkeypatch.setenv("EIMEMORY_RESEARCH_REVIEW_LLM_COMMAND", json.dumps([str(tmp_path / "missing-private-path")]))
    elif failure != "unconfigured":
        if failure in {"command_failed", "bridge_failed"}:
            frame = {"schema": FAILURE_SCHEMA, "error": "provider_request_failed",
                     "diagnostics": {"provider_response_ms": 12.25, "private": "private-token"}}
            stdout = json.dumps(frame) if failure == "bridge_failed" else "private-command-output"
            script = ("import json,sys;json.load(sys.stdin);sys.stderr.write('private-token');"
                      f"print({stdout!r});raise SystemExit(1)")
        elif failure == "timeout":
            script = "import json,sys,time;json.load(sys.stdin);time.sleep(10)"
        elif failure == "bad_envelope":
            script = "import json,sys;json.load(sys.stdin);print('private-invalid-response')"
        elif failure == "bad_review":
            envelope["text"] = "private-invalid-review"
            script = "import json,sys;json.load(sys.stdin);print(" + repr(json.dumps(envelope)) + ")"
        elif failure == "disallowed_model":
            monkeypatch.setenv("EIMEMORY_ALLOWED_REVIEW_MODELS", "another-model")
        monkeypatch.setenv("EIMEMORY_RESEARCH_REVIEW_LLM_COMMAND", json.dumps([sys.executable, "-c", script]))

    with Runtime.create(root=tmp_path) as runtime:
        record = _closure(runtime)
        steps = []
        review = _nightly_step(steps, "research_closure_review",
            lambda: closure_review.review_pending_research_closures(runtime, scope=SCOPE))
        assert steps[0]["error"] == expected and steps[0]["execution_ok"] is False
        assert review["error_counts"] == {expected: 1} and review["reviewed"] == 0
        diagnostics = nightly_result_diagnostics({"research_closure_review": review}, steps)
        assert diagnostics["execution_ok"] is False
        # A failed review also remains a failure if a reader has no step list.
        assert nightly_result_diagnostics({"research_closure_review": review}, [])["execution_ok"] is False
        summary = supervisor_summary(command="nightly", ok=False, duration_ms=1, memory_peak=0)
        summary["nightly_diagnostics"] = diagnostics
        receipt = persist_supervisor_summary(runtime, scope=SCOPE, summary=summary)
        receipt_id = receipt.record_id

    with Runtime.create(root=tmp_path) as runtime:
        saved = runtime.store.get_by_id(record.record_id, scope=SCOPE)
        assert saved.meta["review_status"] == REVIEW_STATUS_UNAVAILABLE
        assert saved.content["model_review"] == ""
        assert saved.content["review_error"] == saved.meta["review_error"] == expected
        assert saved.content["review_failure"] == saved.meta["review_failure"]
        assert "private" not in json.dumps(saved.content["review_failure"])
        receipt = runtime.store.get_by_id(receipt_id, scope=SCOPE)
        projected = receipt.content["nightly_diagnostics"]["research_closure_review"]
        assert projected["reason_counts"] == {expected: 1}
        assert projected["unavailable_records"][0]["record_id"] == record.record_id
        assert "private" not in json.dumps(projected)
        contract = build_supervisor_contract(runtime, scope=SCOPE)
        assert contract["runs"]["nightly"]["nightly_diagnostics"] == diagnostics
        if failure == "bridge_failed":
            diagnostic = projected["unavailable_records"][0]
            assert diagnostic["failure_category"] == "provider_request_failed"
            assert diagnostic["completion_timing"]["provider_response_ms"] == 12.25
        if failure == "bad_review":
            assert projected["unavailable_records"][0]["validation_reason"] == "invalid_json"
        closure_review.retry_unavailable_research_closures(runtime, scope=SCOPE)
        reset = runtime.store.get_by_id(record.record_id, scope=SCOPE)
        assert reset.content["review_failure"] == reset.meta["review_failure"] == {}
        monkeypatch.delenv("EIMEMORY_ALLOWED_REVIEW_MODELS", raising=False)
        success = closure_review.review_pending_research_closures(runtime, scope=SCOPE,
            executor=lambda *_: json.dumps(REVIEW))
        assert success["error"] == "" and success["error_counts"] == {}
        # Retrying a record must not remove the previous run's receipt reason.
        assert runtime.store.get_by_id(receipt_id, scope=SCOPE).content["nightly_diagnostics"] == diagnostics


def test_mixed_review_errors_remain_distinct_in_durable_diagnostics(tmp_path):
    with Runtime.create(root=tmp_path) as runtime:
        records = [_closure(runtime), _closure(runtime)]
        outputs = iter(["", "not JSON"])
        report = closure_review.review_pending_research_closures(runtime, scope=SCOPE,
            executor=lambda *_: next(outputs))
        assert report["error"] == "research_review_multiple_failures"
        assert report["error_counts"] == {"research_review_output_empty": 1, "research_review_output_invalid": 1}
        diagnostic = nightly_result_diagnostics({"research_closure_review": report}, [])["research_closure_review"]
        assert diagnostic["reason_counts"] == report["error_counts"]
        assert {row["record_id"] for row in diagnostic["unavailable_records"]} == {r.record_id for r in records}


def test_research_diagnostics_reject_private_or_unbounded_legacy_errors():
    rows = [{"record_id": "private-reference", "error": "private-token",
             "stage": "private-stage", "error_type": "private-exception",
             "failure_category": "private-provider", "validation_reason": "private-reason",
             "completion_timing": {"private": "private-token", "provider_response_ms": float('nan')}}] * 501
    diagnostic = research_review_diagnostics({"unavailable_records": rows})
    assert diagnostic["reason_counts"] == {"reason_not_allowlisted": 500}
    assert diagnostic["unavailable_records"] == [{"error": "reason_not_allowlisted"}] * 20
    assert diagnostic["unavailable_records_truncated"] is True and diagnostic["reasons_truncated"] is True
    legacy = research_review_diagnostics({"ok": False, "unavailable": 1})
    assert legacy["reason_counts"] == {"reason_not_reported": 1}


@pytest.mark.parametrize("output", ["", "   ", "not JSON", "{}", '{"verdict":"approve"}',
    json.dumps({**REVIEW, "risk": 0}), json.dumps({**REVIEW, "verdict": "invented"}),
    '{"verdict":"reject",' + json.dumps(REVIEW)[1:]])
def test_invalid_review_cannot_complete_closure(tmp_path, output):
    with Runtime.create(root=tmp_path) as runtime:
        record = _closure(runtime)
        report = closure_review.review_pending_research_closures(runtime, scope=SCOPE, executor=lambda *_: output)
        assert report["ok"] is False and report["reviewed"] == 0
        saved = runtime.store.get_by_id(record.record_id, scope=SCOPE)
        assert saved.meta["review_status"] == REVIEW_STATUS_UNAVAILABLE


@pytest.mark.parametrize("status", [REVIEW_STATUS_PENDING_MODEL, REVIEW_STATUS_UNAVAILABLE])
def test_research_queue_is_not_hidden_by_recent_replay_records(tmp_path, status):
    with Runtime.create(root=tmp_path) as runtime:
        record = _closure(runtime, status=status)
        for index in range(30):
            runtime.store.append(RecordEnvelope.create(kind="replay_result", title=f"Unrelated {index}",
                summary="Recent replay", scope=SCOPE, content={}, meta={"report_type": "other"}))
        if status == REVIEW_STATUS_UNAVAILABLE:
            retry = closure_review.retry_unavailable_research_closures(runtime, scope=SCOPE, limit=1)
            assert retry["record_ids"] == [record.record_id]
        report = closure_review.review_pending_research_closures(runtime, scope=SCOPE, limit=1,
            executor=lambda *_: json.dumps(REVIEW))
        assert report["reviewed"] == 1


def test_memory_catalog_invariants_use_executor_and_cannot_replace_memory_benchmark(monkeypatch):
    calls, records = [], []
    catalog = CapabilityEvaluationCatalog()
    registration = catalog.register_executor(executor_id="memory.structural-probe", revision="v1",
        handler=lambda data, fixture, runtime: calls.append(data) or {"scope_isolated": True})
    canonical = catalog.register_case(CatalogCase(case_id="memory-probe", capability_id="memory.recall",
        executor_id=registration.executor_id, executor_revision=registration.revision,
        executor_contract_digest=registration.contract_digest, input_data={"query": "known item"}, fixture={},
        expected_invariants=[{"field": "scope_isolated", "op": "eq", "value": True}]))
    cases = _cases_from_evaluation_catalog({"cases": [{"artifact": canonical.to_artifact(),
        "target": {"capability_revision_id": "memory.recall:v1", "provider_binding_id": "memory.binding"}}]})
    assert cases[0]["execution_type"] == "capability_evaluation"
    monkeypatch.delenv("EIMEMORY_MEMORY_EVAL_DATASET", raising=False)
    runtime = SimpleNamespace(store=SimpleNamespace(append=records.append), capability_catalog=catalog,
        build_replay_dataset=lambda **kwargs: {"ok": True, "cases": cases},
        run_memory_eval_ci=lambda *args, **kwargs: pytest.fail("catalog invariants are not retrieval gold"))
    report = _run_memory_eval_ci(runtime, scope={"agent_id": "main"})
    assert report["ok"] is True and report["execution_counts"]["pass"] == 1
    assert calls == [{"query": "known item"}]
    assert report["memory_benchmark_status"] == "not_run"
    assert report["memory_benchmark_accepted"] is False and report["passed_threshold"] is False
    assert records[0].content["report"]["memory_benchmark_accepted"] is False
    diagnostics = nightly_result_diagnostics({"memory_eval_ci": report}, [])
    assert diagnostics["execution_ok"] is True
    assert diagnostics["evidence_waits"] == ["memory_eval_ci"]
    assert diagnostics["memory_benchmark_accepted"] is False


@pytest.mark.parametrize("cases", [[], [{"case_id": "untrusted", "query": "must not run", "execution_type": "retrieval"}]])
def test_failed_dataset_generation_is_not_disguised_as_empty_success(monkeypatch, cases):
    monkeypatch.delenv("EIMEMORY_MEMORY_EVAL_DATASET", raising=False)
    records = []
    runtime = SimpleNamespace(store=SimpleNamespace(append=records.append),
        build_replay_dataset=lambda **kwargs: {"ok": False, "reason": "profile_evaluation_selection_blocked", "cases": cases},
        run_memory_eval_ci=lambda *args, **kwargs: pytest.fail("blocked dataset must not run"))
    report = _run_memory_eval_ci(runtime, scope={"agent_id": "main"})
    assert report["ok"] is False
    assert report["blocked_reason"] == "memory_eval_dataset_generation_failed"
    assert report["dataset_generation"]["reason"] == "profile_evaluation_selection_blocked"
    assert report["persisted"] is True and report["passed_threshold"] is False
    assert nightly_result_diagnostics({"memory_eval_ci": report}, [])["failed_steps"] == ["memory_eval_ci"]


@pytest.mark.usefixtures("trusted_dataset_path_ancestors")
def test_nightly_uses_secure_configured_memory_dataset_and_reads_receipt_back(tmp_path, monkeypatch):
    monkeypatch.delenv("EIMEMORY_MEMORY_EVAL_DATASET", raising=False)
    dataset_dir = tmp_path / "evaluation"
    dataset_dir.mkdir(mode=0o700)
    dataset = {"name": "reviewed-negative-recall", "scope": asdict(SCOPE), "threshold": 0.8,
               "cases": [{"case_id": "negative-query", "query": "No matching memory exists",
                          "phase": "usage", "expected_empty": True}]}
    path = dataset_dir / "memory_eval.json"
    path.write_text(json.dumps(dataset))
    path.chmod(0o600)
    with Runtime.create(root=tmp_path) as runtime:
        monkeypatch.setattr(runtime, "build_replay_dataset", lambda **kwargs: pytest.fail("configured dataset takes precedence"))
        report = _run_memory_eval_ci(runtime, scope=asdict(SCOPE))
        assert report["dataset_source"] == "conventional_path"
        assert report["memory_benchmark_status"] == "evaluated" and report["retrieval_case_count"] == 1
        assert report["threshold"] == 0.8
        assert report["memory_benchmark_accepted"] is True and report["passed_threshold"] is True
        saved = runtime.store.get_by_id(report["persisted_record_id"], scope=SCOPE)
        assert saved.content["report"]["memory_benchmark_status"] == "evaluated"
        assert saved.content["report"]["passed_threshold"] is True


@pytest.mark.usefixtures("trusted_dataset_path_ancestors")
@pytest.mark.parametrize("failure", ["invalid_json", "symlink"])
def test_invalid_conventional_memory_dataset_cannot_fall_back_to_catalog_success(tmp_path, monkeypatch, failure):
    monkeypatch.delenv("EIMEMORY_MEMORY_EVAL_DATASET", raising=False)
    directory = tmp_path / "evaluation"
    directory.mkdir(mode=0o700)
    path = directory / "memory_eval.json"
    if failure == "symlink":
        target = tmp_path / "elsewhere.json"
        target.write_text('{"cases":[]}')
        path.symlink_to(target)
    else:
        path.write_text("invalid JSON")
    runtime = SimpleNamespace(store=SimpleNamespace(root=tmp_path),
        build_replay_dataset=lambda **kwargs: pytest.fail("configured input cannot fall back"),
        run_memory_eval_ci=lambda *args, **kwargs: pytest.fail("invalid input cannot run"))
    report = _run_memory_eval_ci(runtime, scope=asdict(SCOPE))
    assert report["ok"] is False and report["passed_threshold"] is False
    if failure == "symlink":
        assert report["error"] == "DatasetUnreadableError"
        assert "must not be a symlink" in report["detail"]
    else:
        assert report["error"] == "StrictJSONError"


@pytest.mark.skipif(os.name == "nt", reason="POSIX ownership and mode checks")
@pytest.mark.usefixtures("trusted_dataset_path_ancestors")
@pytest.mark.parametrize("failure", ["foreign_owner", "group_writable", "world_writable"])
def test_conventional_memory_dataset_retains_parent_security_checks(tmp_path, monkeypatch, failure):
    monkeypatch.delenv("EIMEMORY_MEMORY_EVAL_DATASET", raising=False)
    directory = tmp_path / "evaluation"
    directory.mkdir(mode=0o700)
    path = directory / "memory_eval.json"
    path.write_text('{"cases": []}')
    path.chmod(0o600)
    if failure == "foreign_owner":
        real_lstat = Path.lstat

        def foreign_parent_lstat(candidate):
            metadata = real_lstat(candidate)
            if candidate == directory:
                values = list(metadata)
                values[4] = os.geteuid() + 1
                return os.stat_result(values)
            return metadata

        monkeypatch.setattr(Path, "lstat", foreign_parent_lstat)
        reason = "parent owner is not trusted"
    else:
        directory.chmod(0o770 if failure == "group_writable" else 0o707)
        reason = "parent must not be group/world writable"
    runtime = SimpleNamespace(store=SimpleNamespace(root=tmp_path),
        build_replay_dataset=lambda **kwargs: pytest.fail("unsafe input cannot fall back"),
        run_memory_eval_ci=lambda *args, **kwargs: pytest.fail("unsafe input cannot run"))
    try:
        report = _run_memory_eval_ci(runtime, scope=asdict(SCOPE))
        assert report["ok"] is False and report["passed_threshold"] is False
        assert report["error"] == "DatasetUnreadableError"
        assert reason in report["detail"]
    finally:
        directory.chmod(0o700)
