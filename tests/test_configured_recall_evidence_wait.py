"""Native natural captures with insufficient independent labels stay strict waits."""
import json

import pytest

from eimemory.api.runtime import Runtime
from eimemory.adapters.runtime.channel import resolve_channel_scope
from eimemory.evaluation.production_query_dataset import (
    accept_pending_production_query, build_production_query_dataset,
    collect_pending_production_queries,
)
from eimemory.evaluation.recall_agent_scopes import recall_agent_scopes, run_per_agent
from eimemory.governance.release.release_closure import _recall_result_allows_bootstrap_pending
from eimemory.scheduler import jobs
from eimemory.scheduler.result_contract import _nightly_step
from test_production_query_dataset import BASE_SCOPE, LABEL_PACKET_EVIDENCE, _seed_decision


@pytest.fixture(autouse=True)
def isolated_dataset_environment(monkeypatch):
    for name in ("EIMEMORY_PRODUCTION_RECALL_DATASET", "EIMEMORY_PRODUCTION_RECALL_DATASETS",
                 "EIMEMORY_RELEASE_SCOPE_BINDINGS_FILE", "EIMEMORY_RECALL_GATE_AGENTS"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("EIMEMORY_EVIDENCE_RECEIPT_HMAC_KEY",
                       "fixture-only-0123456789-abcdefghijklmnopqrstuvwxyz")


@pytest.mark.parametrize("accepted_count", [0, 1, 2])
def test_native_unready_build_is_not_execution_failure_or_quality_acceptance(
        tmp_path, monkeypatch, accepted_count):
    with Runtime.create(root=tmp_path / "runtime") as runtime:
        gold = [_seed_decision(runtime, channel="hermes", index=index) for index in range(3)]
        collected = collect_pending_production_queries(runtime, scope=BASE_SCOPE)
        assert collected["created"] == 3
        for index, pending_id in enumerate(collected["pending_record_ids"][:accepted_count]):
            pending = runtime.store.get_by_id(pending_id)
            target = next(item for item in gold if item.record_id in pending.content["candidate_refs"])
            accept_pending_production_query(runtime, pending_record_id=pending_id,
                query_features={"terms": ["hermes", "verified", "release", f"case-{index}"]},
                labels=[{"record_ref": target.record_id, "grade": 3}], labeler="operator",
                operator_scope=BASE_SCOPE, label_packet_evidence=LABEL_PACKET_EVIDENCE)
        built = build_production_query_dataset(runtime, scope=BASE_SCOPE)
        assert built["ok"] is True and built["ready"] is False
        assert built["progress"]["accepted_case_count"] == accepted_count
        assert built["progress"]["required_case_count"] == 3
        monkeypatch.setattr(runtime, "run_production_recall_eval", lambda *args, **kwargs: pytest.fail(
            "An unready dataset must not execute or certify retrieval"))
        facade = runtime.run_configured_production_recall_gate(scope=BASE_SCOPE)
        assert facade["accepted"] is False and facade["gate_status"] == "not_run"
        assert facade["blocked_reason"] == "production_dataset_not_ready"
        assert facade["dataset_progress"] == built["progress"]
        assert _recall_result_allows_bootstrap_pending(facade) is True
        report = jobs._run_production_recall_eval(runtime, scope=BASE_SCOPE)
        assert report["ok"] is True and report["accepted"] is False and report["gate_ok"] is False
        assert report["dataset_source"] == "agent_accepted_cases_incomplete"
        assert report["dataset_progress"] == built["progress"]
        assert report["quality_gate"]["evidence_status"] == "insufficient"
        assert report["quality_gate"]["recall_quality_evidence"]["execution_ok"] is True
        scopes = recall_agent_scopes(runtime, BASE_SCOPE)
        per_agent = run_per_agent(scopes, {**BASE_SCOPE, "agent_id": "primary"}, {},
                                  lambda scope: jobs._run_production_recall_eval(runtime, scope=scope))
        assert per_agent["combined"]["ok"] is False
        assert per_agent["combined"]["blocked_reason"] == "recall_quality_evidence_incomplete"
        bounded = jobs._per_agent_bounded_gate({"ok": True}, per_agent)
        steps = []
        _nightly_step(steps, "recall_quality_gate", lambda: bounded)
        assert steps[0]["execution_ok"] is True
        assert steps[0]["evaluation_status"] == "awaiting_evidence"
        assert bounded["ok"] is False


@pytest.mark.parametrize("boundary", ["foreign_user", "invalid_capture", "no_capture"])
def test_unverified_or_absent_capture_is_not_classified_as_native_evidence_wait(tmp_path, boundary):
    with Runtime.create(root=tmp_path / "runtime") as runtime:
        requested = dict(BASE_SCOPE)
        if boundary != "no_capture":
            _seed_decision(runtime, channel="hermes", index=0)
            assert collect_pending_production_queries(runtime, scope=BASE_SCOPE)["created"] == 1
        if boundary == "foreign_user":
            requested["user_id"] = "other-user"
        elif boundary == "invalid_capture":
            with runtime.store.locked() as sqlite:
                sqlite.execute("UPDATE proactive_decisions SET query_digest=?", ("f" * 64,))
        selected = jobs._per_agent_production_recall_dataset(runtime, scope=requested)
        assert selected is None


@pytest.mark.parametrize("configuration", ["missing_agent_file", "missing_shared_file", "empty_agent_file"])
def test_explicit_external_configuration_never_becomes_native_evidence_wait(
        tmp_path, monkeypatch, trusted_dataset_path_ancestors, configuration):
    with Runtime.create(root=tmp_path / "runtime") as runtime:
        _seed_decision(runtime, channel="hermes", index=0)
        assert collect_pending_production_queries(runtime, scope=BASE_SCOPE)["created"] == 1
        path = tmp_path / "explicit-dataset.json"
        if configuration == "empty_agent_file":
            path.write_text(json.dumps({"cases": []}))
            path.chmod(0o600)
        if configuration == "missing_shared_file":
            monkeypatch.setenv("EIMEMORY_PRODUCTION_RECALL_DATASET", str(path))
        else:
            monkeypatch.setenv("EIMEMORY_PRODUCTION_RECALL_DATASETS",
                               json.dumps({BASE_SCOPE["agent_id"]: str(path)}))
        report = jobs._run_production_recall_eval(runtime, scope=BASE_SCOPE)
        assert report.get("dataset_source") != "agent_accepted_cases_incomplete"
        assert report.get("blocked_reason") != "recall_quality_evidence_incomplete"
        if configuration == "empty_agent_file":
            assert report["eval_skipped_reason"] == "production_recall_dataset_empty"
        else:
            assert report["ok"] is False
            assert report["error"] in {"DatasetUnreadableError", "FileNotFoundError"}


@pytest.mark.parametrize("mapping", ["{bad-json", "[]", '{"main": 42}'])
def test_invalid_explicit_agent_mapping_is_hard_failure(tmp_path, monkeypatch, mapping):
    with Runtime.create(root=tmp_path / "runtime") as runtime:
        _seed_decision(runtime, channel="hermes", index=0)
        collect_pending_production_queries(runtime, scope=BASE_SCOPE)
        monkeypatch.setenv("EIMEMORY_PRODUCTION_RECALL_DATASETS", mapping)
        report = jobs._run_production_recall_eval(runtime, scope=BASE_SCOPE)
        assert report["ok"] is False and report["error"] == "DatasetUnreadableError"


@pytest.mark.parametrize("channel", ["hermes", "codex", "nested-invalid"])
def test_explicit_channel_wait_requires_its_own_verified_capture(tmp_path, channel):
    with Runtime.create(root=tmp_path / "runtime") as runtime:
        _seed_decision(runtime, channel="hermes", index=0)
        collect_pending_production_queries(runtime, scope=BASE_SCOPE)
        requested = resolve_channel_scope("hermes" if channel == "nested-invalid" else channel, BASE_SCOPE)
        if channel == "nested-invalid":
            requested["workspace_id"] += "::channel::hermes"
        selected = jobs._per_agent_production_recall_dataset(runtime, scope=requested)
        if channel == "hermes":
            assert selected is not None and selected[2] == "agent_accepted_cases_incomplete"
            assert runtime.run_configured_production_recall_gate(scope=requested)["blocked_reason"] == "production_dataset_not_ready"
        else:
            assert selected is None


@pytest.mark.parametrize("owner", ["agent", "shared"])
@pytest.mark.parametrize("broken", ["directory", "symlink"])
def test_existing_invalid_conventional_path_is_hard_failure_with_native_pending(tmp_path, owner, broken):
    with Runtime.create(root=tmp_path / "runtime") as runtime:
        _seed_decision(runtime, channel="hermes", index=0)
        assert collect_pending_production_queries(runtime, scope=BASE_SCOPE)["created"] == 1
        directory = runtime.store.root / "evaluation"
        if owner == "agent":
            directory = directory / "agents" / BASE_SCOPE["agent_id"]
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / "production_recall.json"
        if broken == "directory":
            path.mkdir()
        else:
            try:
                path.symlink_to(tmp_path / "missing-dataset.json")
            except OSError:
                pytest.skip("symlink creation unavailable")
        report = jobs._run_production_recall_eval(runtime, scope=BASE_SCOPE)
        assert report["ok"] is False and report.get("error")
        assert report.get("dataset_source") != "agent_accepted_cases_incomplete"
        assert report.get("blocked_reason") != "recall_quality_evidence_incomplete"
        with pytest.raises((OSError, jobs.DatasetUnreadableError)):
            runtime.run_configured_production_recall_gate(scope=BASE_SCOPE)
