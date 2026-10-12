from dataclasses import asdict

import pytest

from eimemory.api.runtime import Runtime
from eimemory.models.records import RecordEnvelope, ScopeRef
from eimemory.scheduler import jobs
from eimemory.scheduler.result_contract import nightly_result_diagnostics


SCOPE = ScopeRef(agent_id="benchmark-agent", workspace_id="workspace::channel::hermes",
                 user_id="benchmark-member")


def memory(runtime):
    record = RecordEnvelope.create(kind="memory", title="Atlas notebooks",
        summary="Atlas uses green notebooks", content={"text": "Atlas uses green notebooks"},
        scope=SCOPE, source="operator.memory", source_id="hermes",
        meta={"memory_type": "fact", "force_capture": True})
    runtime.store.append(record)
    return record


@pytest.fixture(autouse=True)
def isolate_configuration(monkeypatch):
    monkeypatch.delenv("EIMEMORY_MEMORY_EVAL_DATASET", raising=False)
    monkeypatch.delenv("EIMEMORY_RELEASE_SCOPE_BINDINGS_FILE", raising=False)
    monkeypatch.delenv("EIMEMORY_SOURCE_IDS", raising=False)


def test_empty_replay_runs_actual_source_bound_benchmark_and_persists_without_formal_acceptance(tmp_path, monkeypatch):
    with Runtime.create(root=tmp_path) as runtime:
        target = memory(runtime)
        monkeypatch.setattr(runtime, "build_replay_dataset", lambda **kwargs: {"ok": True, "cases": []})
        monkeypatch.setattr(runtime, "run_memory_eval_ci", lambda *args, **kwargs:
                            pytest.fail("controlled checks use the configured runtime recall path"))
        report = jobs._run_memory_eval_ci(runtime, scope=asdict(SCOPE))
        assert report["dataset_source"] == "controlled_records"
        assert report["memory_benchmark_status"] == "evaluated"
        assert report["retrieval_case_count"] == report["pass_count"] == 1
        assert report["samples"][0]["returned_refs"][0]["record_id"] == target.record_id
        assert report["controlled_benchmark_passed_threshold"] is True
        assert report["memory_benchmark_accepted"] is False
        assert report["formal_business_evidence"] is False
        assert report["certifies_natural_queries"] is False
        record_id = report["persisted_record_id"]
    with Runtime.create(root=tmp_path) as runtime:
        saved = runtime.store.get_by_id(record_id, scope=SCOPE).content["report"]
        assert saved["memory_benchmark_accepted"] is False
        assert saved["controlled_benchmark_passed_threshold"] is True
        diagnostics = nightly_result_diagnostics({"memory_eval_ci": saved}, [])
        assert diagnostics["memory_benchmark_accepted"] is False
        assert diagnostics["memory_benchmark"]["dataset_source"] == "controlled_records"


@pytest.mark.parametrize("source", ["env", "conventional_path"])
def test_configured_empty_dataset_never_uses_existing_records_as_fallback(tmp_path, monkeypatch, source):
    with Runtime.create(root=tmp_path) as runtime:
        memory(runtime)
        monkeypatch.setattr(jobs, "_memory_eval_ci_dataset", lambda *args, **kwargs:
                            ({"cases": []}, True, source))
        report = jobs._run_memory_eval_ci(runtime, scope=asdict(SCOPE))
        assert report["dataset_source"] == source
        assert report["retrieval_case_count"] == 0
        assert report["blocked_reason"] == "memory_eval_dataset_empty"
        assert report["memory_benchmark_accepted"] is False
        assert "controlled_context" not in report


def test_failed_replay_generation_cannot_be_masked_by_source_material(tmp_path, monkeypatch):
    with Runtime.create(root=tmp_path) as runtime:
        memory(runtime)
        monkeypatch.setattr(runtime, "build_replay_dataset", lambda **kwargs:
                            {"ok": False, "reason": "catalog_binding_changed", "cases": []})
        report = jobs._run_memory_eval_ci(runtime, scope=asdict(SCOPE))
        assert report["ok"] is False
        assert report["blocked_reason"] == "memory_eval_dataset_generation_failed"
        assert report["dataset_generation"]["reason"] == "catalog_binding_changed"
        assert report["retrieval_case_count"] == 0
        assert "controlled_context" not in report


def test_unreported_replay_success_cannot_authorize_controlled_fallback(tmp_path, monkeypatch):
    with Runtime.create(root=tmp_path) as runtime:
        memory(runtime)
        monkeypatch.setattr(runtime, "build_replay_dataset", lambda **kwargs: {"cases": []})
        report = jobs._run_memory_eval_ci(runtime, scope=asdict(SCOPE))
        assert report["retrieval_case_count"] == 0
        assert report["memory_benchmark_accepted"] is False
        assert "controlled_context" not in report


def test_controlled_pass_does_not_conceal_rejected_catalog_case(tmp_path, monkeypatch):
    with Runtime.create(root=tmp_path) as runtime:
        memory(runtime)
        monkeypatch.setattr(runtime, "build_replay_dataset", lambda **kwargs: {"ok": True,
            "cases": [{"case_id": "unsupported-catalog-route", "source": "capability_evaluation_catalog",
                       "execution_type": "unregistered-operation"}]})
        report = jobs._run_memory_eval_ci(runtime, scope=asdict(SCOPE))
        assert report["controlled_benchmark_passed_threshold"] is True
        assert report["ok"] is False
        assert report["execution_counts"]["rejected"] == 1
        assert report["execution_results"][0]["reason"] == "unknown_execution_type"
        assert report["memory_benchmark_accepted"] is False


def test_unbound_scope_remains_empty_without_guessing_hermes_or_other_members(tmp_path, monkeypatch):
    with Runtime.create(root=tmp_path) as runtime:
        memory(runtime)
        monkeypatch.setattr(runtime, "build_replay_dataset", lambda **kwargs: {"ok": True, "cases": []})
        report = jobs._run_memory_eval_ci(runtime, scope={"agent_id": "benchmark-agent"})
        assert report["retrieval_case_count"] == 0
        assert report["blocked_reason"] == "memory_eval_dataset_empty"
        assert report["controlled_context"]["status"] == "blocked"
        assert report["memory_benchmark_accepted"] is False
