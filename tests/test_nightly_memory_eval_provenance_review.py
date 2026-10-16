"""Scheduler provenance must survive the durable report boundary."""
from dataclasses import asdict
import json

import pytest

from eimemory.api.runtime import Runtime
from eimemory.models.records import ScopeRef
from eimemory.scheduler import jobs
from eimemory.scheduler.result_contract import nightly_result_diagnostics


SCOPE = ScopeRef(agent_id="provenance-agent", workspace_id="workspace::channel::hermes",
                 user_id="provenance-member")


@pytest.mark.usefixtures("trusted_dataset_path_ancestors")
@pytest.mark.parametrize("source", ["env", "conventional_path", "replay_dataset"])
def test_nonempty_memory_eval_keeps_loader_provenance_after_reopen(tmp_path, monkeypatch, source):
    monkeypatch.delenv("EIMEMORY_MEMORY_EVAL_DATASET", raising=False)
    monkeypatch.delenv("EIMEMORY_RELEASE_SCOPE_BINDINGS_FILE", raising=False)
    dataset = {"name": "isolated-negative", "scope": asdict(SCOPE), "threshold": 1.0,
               "cases": [{"case_id": "absent", "query": "Atlas notebook colour",
                          "phase": "usage", "expected_empty": True}]}
    if source != "replay_dataset":
        directory = tmp_path / "evaluation"
        directory.mkdir(mode=0o700)
        path = directory / "memory_eval.json"
        path.write_text(json.dumps(dataset))
        path.chmod(0o600)
        if source == "env":
            monkeypatch.setenv("EIMEMORY_MEMORY_EVAL_DATASET", str(path))
    with Runtime.create(root=tmp_path) as runtime:
        monkeypatch.setattr(runtime, "build_replay_dataset", lambda **kwargs:
                            {"ok": True, "cases": dataset["cases"]})
        report = jobs._run_memory_eval_ci(runtime, scope=asdict(SCOPE))
        assert report["ok"] is True
        assert report["dataset_source"] == source
        assert report["retrieval_case_count"] == 1
        assert report["configured"] is True
        if source == "replay_dataset":
            assert report.get("dataset_generation") == {
                "ok": True, "success_confirmed": True, "reason": ""}
        record_id = report["persisted_record_id"]
    with Runtime.create(root=tmp_path) as runtime:
        saved = runtime.store.get_by_id(record_id, scope=SCOPE).content["report"]
        assert saved.get("dataset_source") == source
        assert saved.get("configured") is report["configured"]
        if source == "replay_dataset":
            assert saved["dataset_generation"] == report["dataset_generation"] == {
                "ok": True, "success_confirmed": True, "reason": ""}
            generation = nightly_result_diagnostics({"memory_eval_ci": saved}, [])["memory_benchmark"]["dataset_generation"]
            assert generation["ok"] is generation["success_confirmed"] is True
        assert nightly_result_diagnostics({"memory_eval_ci": saved}, []) == (
            nightly_result_diagnostics({"memory_eval_ci": report}, []))
