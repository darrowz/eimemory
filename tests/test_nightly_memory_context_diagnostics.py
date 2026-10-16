"""Memory benchmark summaries preserve bounded context without granting scope."""
from copy import deepcopy
from dataclasses import asdict
import getpass
import json

import pytest

from eimemory.api.runtime import Runtime
from eimemory.governance.learning.supervisor import (
    build_supervisor_contract, persist_supervisor_summary, supervisor_summary,
)
from eimemory.models.records import ScopeRef
from eimemory.scheduler.jobs import _run_memory_eval_ci
from eimemory.scheduler.result_contract import nightly_result_diagnostics


def test_native_missing_channel_binding_keeps_resolution_in_durable_summary(tmp_path, monkeypatch):
    requested = ScopeRef(agent_id="isolated-agent", workspace_id="isolated-workspace",
                         user_id="explicit-logical-user")
    bindings = tmp_path / "bindings.json"
    bindings.write_text(json.dumps([{"scope": {
        **asdict(requested), "workspace_id": "isolated-workspace::channel::hermes",
        "user_id": "other-logical-user"}}]))
    bindings.chmod(0o600)
    monkeypatch.setenv("EIMEMORY_RELEASE_SCOPE_BINDINGS_FILE", str(bindings))
    monkeypatch.delenv("EIMEMORY_MEMORY_EVAL_DATASET", raising=False)
    monkeypatch.delenv("EIMEMORY_DEPLOY_SCOPE_USER", raising=False)
    monkeypatch.setattr(getpass, "getuser", lambda: "isolated-host-user")
    with Runtime.create(root=tmp_path / "data") as runtime:
        monkeypatch.setattr(runtime, "build_replay_dataset", lambda **kwargs: {"ok": True, "cases": []})
        report = _run_memory_eval_ci(runtime, scope=asdict(requested))
        diagnostics = nightly_result_diagnostics({"memory_eval_ci": report}, [])
        memory = diagnostics["memory_benchmark"]
        assert memory["dataset_source"] == "replay_dataset"
        assert memory["configured"] is False
        assert memory["retrieval_case_count"] == 0
        assert memory["dataset_generation"]["ok"] is True
        assert memory["dataset_generation"]["success_confirmed"] is True
        assert memory["dataset_generation"]["scanned_count"] is None
        assert memory["controlled_context"]["reason"] == "memory_benchmark_channel_binding_missing"
        assert memory["controlled_context"]["resolution"] == {
            "logical_scope_reason": "explicit_user_preserved", "logical_user_rewritten": False,
            "channel_source": "operator_binding", "matching_channel_count": 0}
        assert diagnostics["memory_benchmark_accepted"] is False
        summary = supervisor_summary(command="nightly", ok=True, duration_ms=1, memory_peak=0)
        summary["nightly_diagnostics"] = diagnostics
        saved_id = persist_supervisor_summary(runtime, scope=requested, summary=summary).record_id
    with Runtime.create(root=tmp_path / "data") as runtime:
        assert runtime.store.get_by_id(saved_id, scope=requested).content["nightly_diagnostics"] == diagnostics
        assert build_supervisor_contract(runtime, scope=requested)["runs"]["nightly"]["nightly_diagnostics"] == diagnostics
    serialized = json.dumps(diagnostics)
    assert "explicit-logical-user" not in serialized
    assert "other-logical-user" not in serialized
    assert str(bindings) not in serialized


@pytest.mark.parametrize("configured", [True, False, None, 0, 1, "private-input"])
def test_configured_dataset_presence_remains_strict_and_missing_is_unknown(configured):
    report = {"memory_eval_ci": {"configured": configured}}
    result = nightly_result_diagnostics(report, [])["memory_benchmark"]
    assert result["configured"] is (configured if type(configured) is bool else None)
    assert nightly_result_diagnostics({"memory_eval_ci": {"ok": True}}, [])["memory_benchmark"]["configured"] is None


def test_context_projection_keeps_unknowns_and_drops_private_values():
    report = {"memory_eval_ci": {
        "dataset_source": "replay_dataset", "configured": "private-value",
        "dataset_generation": {"ok": True, "success_confirmed": 1, "reason": ""},
        "controlled_context": {"status": "blocked", "reason": "memory_benchmark_channel_binding_missing",
            "resolution": {"logical_scope_reason": "private-value", "logical_user_rewritten": 1,
                "channel_source": "private-value", "matching_channel_count": False,
                "user_id": "private-value", "scope": {"agent_id": "private-value"}}}}}
    snapshot = deepcopy(report)
    result = nightly_result_diagnostics(report, [])["memory_benchmark"]
    assert result["configured"] is None
    assert result["dataset_generation"]["success_confirmed"] is None
    assert result["controlled_context"]["resolution"] == {
        "logical_scope_reason": "not_allowlisted", "logical_user_rewritten": None,
        "channel_source": "not_allowlisted", "matching_channel_count": None}
    assert "private-value" not in json.dumps(result)
    assert report == snapshot
