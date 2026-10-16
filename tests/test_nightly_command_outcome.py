"""Scheduler success must not certify the later CLI identity repair."""
from copy import deepcopy
from dataclasses import asdict
import importlib
import json
from types import SimpleNamespace

import pytest

from deploy.diagnose_recall_release import diagnose
from eimemory.api.runtime import Runtime
from eimemory.contracts.release_identity import ReleaseIdentity, release_identity_payload
from eimemory.governance.learning.supervisor import (
    build_supervisor_contract, persist_nightly_command_outcome, persist_supervisor_summary, supervisor_summary,
)
from eimemory.models.records import ScopeRef

cli = importlib.import_module("eimemory.cli.main")
SCOPE = ScopeRef(agent_id="nightly", workspace_id="command-outcome", user_id="operator")
RELEASE = ReleaseIdentity("a" * 40, "1.14.114", "receipt-current", "session-current")


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    monkeypatch.setattr("eimemory.governance.release.evidence_contract.current_release_identity",
                        lambda *_args, **_kwargs: RELEASE)
    with Runtime.create(root=tmp_path / "runtime") as value:
        yield value


def scheduler(runtime, *, run_id="run-current", ok=True):
    summary = supervisor_summary(command="nightly", ok=ok, duration_ms=10, memory_peak=0,
                                 run_id=run_id, started_at="2026-10-16T00:00:00Z")
    summary["nightly_diagnostics"] = {"execution_ok": ok, "failed_steps": [] if ok else ["scheduler_test"]}
    saved = persist_supervisor_summary(runtime, scope=SCOPE, summary=summary)
    return {**saved.content, "record_id": saved.record_id}


def run_cli(runtime, monkeypatch, repair, *, scheduler_ok=True):
    summary = scheduler(runtime, ok=scheduler_ok)
    monkeypatch.setattr(cli, "run_nightly_jobs", lambda *_args, **_kwargs: {
        "ok": scheduler_ok, "supervisor_summary": summary,
    })
    monkeypatch.setattr(cli, "repair_hongtu_identity", repair)
    return cli._cmd_nightly(SimpleNamespace(), runtime, asdict(SCOPE)), summary


def command_boundary(report):
    result = diagnose(expected_commit=RELEASE.commit, reports={"nightly": report})
    return next(item for item in result["boundaries"] if item["boundary"] == "nightly_command")


def test_post_scheduler_rejection_is_persisted_without_changing_scheduler(runtime, monkeypatch, capsys):
    repair = {"ok": False, "blocked_count": 249,
              "outcome_counts": {"source_projection_or_digest_mismatch": 249},
              "mismatch_field_counts": {"payload_roundtrip_digest": 249},
              "repair_outcomes": [{"record_id": "private-record", "body": "private-body"}]}
    code, summary = run_cli(runtime, monkeypatch, lambda *_args, **_kwargs: deepcopy(repair))
    output = json.loads(capsys.readouterr().out)
    assert code == 1 and output["ok"] is False and output["identity_repair"] == repair
    ending = output["command_execution"]
    assert ending["exit_code"] == 1 and ending["scheduler_execution_ok"] is True
    assert ending["first_failed_phase"] == "identity_repair"
    assert ending["run_id"] == summary["run_id"] and ending["supervisor_record_id"] == summary["record_id"]
    assert ending["identity_repair"]["mismatch_field_counts"] == {"payload_roundtrip_digest": 249}
    stored = runtime.store.get_by_id(summary["record_id"], scope=SCOPE, exact_scope=True)
    assert stored.content["ok"] is True
    assert stored.content["nightly_diagnostics"] == {"execution_ok": True, "failed_steps": []}
    receipt = runtime.store.get_by_id(ending["record_id"], scope=SCOPE, exact_scope=True)
    assert "private" not in json.dumps(receipt.to_dict())
    contract = build_supervisor_contract(runtime, scope=SCOPE)
    assert contract["status"] == "degraded" and contract["scheduler_status"] == "healthy"
    assert contract["runs"]["nightly"]["command_execution"] == ending
    result = diagnose(expected_commit=RELEASE.commit, reports={"nightly": contract})
    by = {item["boundary"]: item for item in result["boundaries"]}
    assert by["nightly_execution"]["state"] == "reported_pass"
    assert by["nightly_command"]["state"] == "failed"


@pytest.mark.parametrize("legacy", [False, True])
def test_scheduler_only_is_not_a_whole_command_success(runtime, legacy):
    if legacy:
        persist_supervisor_summary(runtime, scope=SCOPE,
            summary=supervisor_summary(command="nightly", ok=True, duration_ms=1, memory_peak=0))
    else:
        scheduler(runtime)
    contract = build_supervisor_contract(runtime, scope=SCOPE)
    assert contract["status"] == "unknown" and contract["scheduler_status"] == "healthy"
    assert contract["runs"]["nightly"]["command_execution"]["status"] == "not_recorded"
    assert command_boundary(contract)["state"] == "unverified"


def test_successful_cli_has_a_matching_persisted_ending(runtime, monkeypatch, capsys):
    code, _ = run_cli(runtime, monkeypatch, lambda *_args, **_kwargs: {"ok": True})
    output = json.loads(capsys.readouterr().out)
    assert code == 0 and output["command_execution"]["exit_code"] == 0
    assert build_supervisor_contract(runtime, scope=SCOPE)["status"] == "healthy"
    assert command_boundary(output)["state"] == "reported_pass"


def test_failed_scheduler_stays_failed_when_identity_succeeds(runtime, monkeypatch, capsys):
    code, _ = run_cli(runtime, monkeypatch, lambda *_args, **_kwargs: {"ok": True}, scheduler_ok=False)
    output = json.loads(capsys.readouterr().out)
    assert code == 1 and output["command_execution"]["first_failed_phase"] == "scheduler"
    assert output["command_execution"]["identity_repair_ok"] is True
    assert output["supervisor_summary"]["nightly_diagnostics"]["execution_ok"] is False


def test_identity_exception_is_recorded_and_original_exception_rethrown(runtime, monkeypatch, capsys):
    error = RuntimeError("original identity error")
    def repair(*_args, **_kwargs):
        raise error
    with pytest.raises(RuntimeError) as caught:
        run_cli(runtime, monkeypatch, repair)
    assert caught.value is error
    output = json.loads(capsys.readouterr().out)
    assert output["ok"] is False and output["command_execution"]["identity_repair"]["exception_raised"] is True
    assert build_supervisor_contract(runtime, scope=SCOPE)["status"] == "degraded"


def test_persistence_failure_cannot_print_success(runtime, monkeypatch, capsys):
    def fail(*_args, **_kwargs):
        raise OSError("private persistence error")
    monkeypatch.setattr(cli, "persist_nightly_command_outcome", fail)
    code, _ = run_cli(runtime, monkeypatch, lambda *_args, **_kwargs: {"ok": True})
    output = json.loads(capsys.readouterr().out)
    assert code == 1 and output["ok"] is False
    assert output["command_execution"]["first_failed_phase"] == "command_outcome_persistence"
    assert "private" not in json.dumps(output)
    assert build_supervisor_contract(runtime, scope=SCOPE)["status"] == "unknown"
    assert command_boundary(output)["state"] == "failed"


def test_late_old_run_ending_cannot_override_new_scheduler(runtime):
    old, new = scheduler(runtime, run_id="run-old"), scheduler(runtime, run_id="run-new")
    assert old["record_id"] == new["record_id"]
    with pytest.raises(ValueError, match="scheduler_run_changed"):
        persist_nightly_command_outcome(runtime, scope=SCOPE, supervisor=old, identity_repair={"ok": False})
    assert build_supervisor_contract(runtime, scope=SCOPE)["status"] == "unknown"
    new_end = persist_nightly_command_outcome(runtime, scope=SCOPE, supervisor=new, identity_repair={"ok": True})
    contract = build_supervisor_contract(runtime, scope=SCOPE)
    assert contract["status"] == "healthy" and contract["runs"]["nightly"]["command_execution"] == new_end
    assert new_end["run_id"] == "run-new"


@pytest.mark.parametrize("field", ["run_id", "supervisor_record_id", *release_identity_payload(RELEASE)])
def test_diagnosis_cannot_borrow_another_run_or_release_ending(runtime, field):
    summary = scheduler(runtime)
    ending = persist_nightly_command_outcome(runtime, scope=SCOPE, supervisor=summary, identity_repair={"ok": True})
    ending[field] = "another"
    assert command_boundary({"supervisor_summary": summary, "command_execution": ending})["state"] == "failed"


@pytest.mark.parametrize("field", ["record_id", *release_identity_payload(RELEASE)])
def test_persistence_requires_exact_scheduler_ref_and_release(runtime, field):
    summary = scheduler(runtime)
    summary[field] = "another"
    with pytest.raises(ValueError):
        persist_nightly_command_outcome(runtime, scope=SCOPE, supervisor=summary, identity_repair={"ok": True})


def test_command_receipt_is_immutable_and_cannot_turn_failure_into_success(runtime):
    summary = scheduler(runtime)
    first = persist_nightly_command_outcome(runtime, scope=SCOPE, supervisor=summary, identity_repair={"ok": False})
    again = persist_nightly_command_outcome(runtime, scope=SCOPE, supervisor=summary, identity_repair={"ok": False})
    assert first == again
    with pytest.raises(ValueError):
        persist_nightly_command_outcome(runtime, scope=SCOPE, supervisor=summary, identity_repair={"ok": True})
    assert build_supervisor_contract(runtime, scope=SCOPE)["status"] == "degraded"


def test_only_fixed_counter_names_are_saved_and_reopened(runtime):
    summary = scheduler(runtime)
    ending = persist_nightly_command_outcome(runtime, scope=SCOPE, supervisor=summary, identity_repair={
        "ok": False, "outcome_counts": {"private-record-body": 9, "transaction_failed:PrivateError": 2},
        "mismatch_field_counts": {"payload_roundtrip_digest": 249, "private-field-body": 3},
        "roundtrip_field_counts": {"aliases": 200, "private-field-value": 49, "content": -1},
        "roundtrip_missing_field_counts": {"source_id": 199, "unknown_fields": 50},
        "legacy_normalization_counts": {"missing_source_id": 199, "missing_aliases": 200,
            "missing_aliases_version": 200, "source_partition_overlay": 2, "private-normalization": 9},
    })
    assert ending["identity_repair"]["outcome_counts"] == {"other": 9, "transaction_failed": 2}
    assert "private" not in json.dumps(ending)
    assert ending["identity_repair"]["roundtrip_field_counts"] == {"aliases": 200, "unknown_fields": 49}
    assert ending["identity_repair"]["roundtrip_missing_field_counts"] == {"source_id": 199, "unknown_fields": 50}
    assert ending["identity_repair"]["legacy_normalization_counts"] == {
        "missing_source_id": 199, "missing_aliases": 200, "missing_aliases_version": 200, "source_partition_overlay": 2}
    with Runtime.create(root=runtime.store.root) as reopened:
        contract = build_supervisor_contract(reopened, scope=SCOPE)
        assert contract["runs"]["nightly"]["command_execution"] == ending and contract["status"] == "degraded"


def test_changed_current_release_cannot_bind_an_old_run_ending(runtime, monkeypatch):
    summary = scheduler(runtime)
    other = ReleaseIdentity("b" * 40, "other", "other-receipt", "other-session")
    monkeypatch.setattr("eimemory.governance.release.evidence_contract.current_release_identity",
                        lambda *_args, **_kwargs: other)
    with pytest.raises(ValueError, match="release_or_run_mismatch"):
        persist_nightly_command_outcome(runtime, scope=SCOPE, supervisor=summary, identity_repair={"ok": True})
    assert build_supervisor_contract(runtime, scope=SCOPE)["status"] == "unknown"


def test_same_run_caller_cannot_replace_the_stored_scheduler_result(runtime):
    summary = scheduler(runtime)
    summary["ok"] = False
    summary["nightly_diagnostics"] = {"execution_ok": False, "failed_steps": ["invented"]}
    with pytest.raises(ValueError, match="scheduler_result_changed"):
        persist_nightly_command_outcome(runtime, scope=SCOPE, supervisor=summary, identity_repair={"ok": True})
    assert build_supervisor_contract(runtime, scope=SCOPE)["status"] == "unknown"


def test_unbound_execution_receipt_cannot_borrow_a_current_release_label(runtime, monkeypatch):
    monkeypatch.setattr("eimemory.governance.release.evidence_contract.current_release_identity",
                        lambda *_args, **_kwargs: None)
    summary = scheduler(runtime)
    ending = persist_nightly_command_outcome(runtime, scope=SCOPE, supervisor=summary, identity_repair={"ok": True})
    report = {**release_identity_payload(RELEASE), "supervisor_summary": summary, "command_execution": ending}
    boundary = command_boundary(report)
    assert boundary["state"] == "unverified" and boundary["reason"] == "nightly_command_release_identity_missing"


@pytest.mark.parametrize("change", [{"ok": True}, {"exit_code": 0}, {"identity_repair_ok": True},
                                    {"first_failed_phase": ""}])
def test_inconsistent_stored_outcome_cannot_certify_command_success(runtime, change):
    summary = scheduler(runtime)
    ending = persist_nightly_command_outcome(runtime, scope=SCOPE, supervisor=summary, identity_repair={"ok": False})
    record = runtime.store.get_by_id(ending["record_id"], scope=SCOPE, exact_scope=True)
    record.content.update(change)
    runtime.store.rewrite(record)
    assert build_supervisor_contract(runtime, scope=SCOPE)["status"] == "unknown"


@pytest.mark.parametrize("change", [
    {"identity_repair": {"ok": False, "exception_raised": True}, "first_failed_phase": "identity_repair"},
    {"identity_repair": {}}, {"identity_repair": None}, {"identity_repair": {"ok": True}},
    {"first_failed_phase": "identity_repair"},
])
def test_diagnosis_rejects_missing_or_contradictory_nested_identity_result(runtime, change):
    summary = scheduler(runtime)
    ending = persist_nightly_command_outcome(runtime, scope=SCOPE, supervisor=summary, identity_repair={"ok": True})
    ending.update(change)
    boundary = command_boundary({"supervisor_summary": summary, "command_execution": ending})
    assert boundary["state"] == "failed" and boundary["reason"] == "nightly_command_outcome_inconsistent"
