"""Executed safety outcomes must survive scheduler persistence and readback."""
from copy import deepcopy

import pytest

from eimemory.api.runtime import Runtime
from eimemory.governance.learning.supervisor import build_supervisor_contract
from eimemory.scheduler import jobs
from tests.test_l5_consciousness_loop import (
    SCOPE, _PassingPromptSafetyExecutor, _seed_fake_cycle_evidence, _seed_release,
)


@pytest.mark.parametrize("safety_scenario", ["failed", "not_ready", "passed", "partial_failed", "partial_passed"])
def test_native_l5_prompt_safety_outcome_survives_nightly_and_restart(tmp_path, monkeypatch, safety_scenario):
    safety_status = "not_ready" if safety_scenario.startswith("partial_") else safety_scenario
    expected_ok = safety_scenario not in {"failed", "partial_failed"}

    class ControlledExecutor(_PassingPromptSafetyExecutor):
        def execute_case(self, **kwargs):
            if safety_scenario.startswith("partial_") and kwargs["case"]["case_id"] == "clean_control":
                raise RuntimeError("controlled executor unavailable for one case")
            result = super().execute_case(**kwargs)
            if safety_scenario == "failed" or (
                safety_scenario == "partial_failed" and kwargs["case"]["case_id"] == "direct_injection"
            ):
                result["passed"] = False
                result["checks"] = {**result["checks"], "policy_compliant": False, "secret_exposed": True}
                result["semantic_judgment"] = {
                    **result["semantic_judgment"], "verdict": "unsafe", "secret_exposed": True,
                }
            return result

    monkeypatch.setenv("EIMEMORY_L5_LOOP_ENABLED", "1")
    monkeypatch.setenv("EIMEMORY_L5_LOOP_APPLY", "0")
    monkeypatch.setenv("EIMEMORY_L5_LOOP_NETWORK", "0")
    native_step = jobs._nightly_step
    controls = {
        "production_recall": {"ok": True, "quality_gate": {"ok": True}},
        "recall_quality_per_agent": {"ok": True, "combined": {"ok": True}},
    }
    monkeypatch.setattr(jobs, "_nightly_step", lambda reports, name, run:
        native_step(reports, name, run if name == "l5_loop" else lambda: controls.get(name, {"ok": True})))
    root = tmp_path / "runtime"
    with Runtime.create(root=root) as runtime:
        _seed_release(runtime)
        runtime.prompt_safety_executor = None if safety_scenario == "not_ready" else ControlledExecutor()
        monkeypatch.setattr(runtime, "run_autonomous_learning_cycle", lambda **_kwargs:
            _seed_fake_cycle_evidence(runtime, loop_id="controlled-autonomous-result", rollback=False))
        captured = []
        native_cycle = runtime.run_l5_cycle

        def record_native_cycle(**kwargs):
            report = native_cycle(**kwargs)
            captured.append(deepcopy(report))
            return report

        monkeypatch.setattr(runtime, "run_l5_cycle", record_native_cycle)
        report = jobs.run_nightly_jobs(runtime, scope=SCOPE)
        assert len(captured) == 1
        native = captured[0]["prompt_safety"]
        assert native["status"] == safety_status
        assert (native["executed_count"] > 0) is (safety_scenario != "not_ready")
        if safety_scenario.startswith("partial_"):
            assert 0 < native["executed_count"] < native["expected_count"]
        failed_cases = [item["case_id"] for item in native["case_results"]
                        if item["executed"] is True and item["valid"] is True and item["passed"] is False]
        assert bool(failed_cases) is (not expected_ok)
        assert "nightly_unhandled_exception" not in report["nightly_diagnostics"]["failed_steps"]
        assert report["ok"] is expected_ok, report["nightly_diagnostics"]
        assert report["l5_loop"]["ok"] is expected_ok
        assert report["l5_loop"]["prompt_safety"]["status"] == safety_status
        assert report["l5_loop"]["prompt_safety"]["failed_count"] == len(failed_cases)
        assert report["l5_loop"]["prompt_safety"]["failed_case_ids"] == failed_cases
        assert report["l5_loop"]["awaiting_evidence"] is (safety_status == "not_ready" and expected_ok)
        assert report["l5_loop"]["complete"] is False
        supervisor_id = report["supervisor_summary"]["record_id"]
        prompt_record_id = native["persisted_record_id"]
    with Runtime.create(root=root) as reopened:
        saved = reopened.store.get_by_id(supervisor_id, scope=SCOPE)
        assert saved.content["nightly_diagnostics"] == report["nightly_diagnostics"]
        assert reopened.store.get_by_id(prompt_record_id, scope=SCOPE).content["status"] == safety_status
        assert build_supervisor_contract(reopened, scope=SCOPE)["runs"]["nightly"]["ok"] is expected_ok
