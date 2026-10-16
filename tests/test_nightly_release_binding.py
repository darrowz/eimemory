"""Nightly evidence must retain its verified release and execution identity."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from deploy.diagnose_recall_release import diagnose
from eimemory.api.runtime import Runtime
from eimemory.contracts.release_identity import ReleaseIdentity, release_identity_payload
from eimemory.governance.learning.supervisor import (
    _summary_from_record, build_supervisor_contract, persist_supervisor_summary, supervisor_summary,
)
from eimemory.models.records import RecordEnvelope, ScopeRef
from eimemory.scheduler import jobs

COMMIT = "a" * 40
RELEASE = ReleaseIdentity(COMMIT, "1.14.110", "receipt-current", "session-current")
SCOPE = ScopeRef(agent_id="nightly", workspace_id="release-binding", user_id="operator")


def _bound_nightly(**updates):
    return {
        "ok": True,
        **release_identity_payload(RELEASE),
        "nightly_diagnostics": {"execution_ok": True},
        "recall_quality_gate": {"ok": True, "vacuous": False},
        **updates,
    }


def _boundaries(nightly, **options):
    result = diagnose(expected_commit=COMMIT, reports={"nightly": nightly}, **options)
    return {item["boundary"]: item for item in result["boundaries"]}


@pytest.mark.parametrize("envelope", ["direct", "cli", "contract"])
def test_old_nightly_cannot_pass_current_release_diagnosis(envelope):
    nightly = _bound_nightly(release_commit="b" * 40)
    if envelope == "cli":
        nightly = {"ok": True, "supervisor_summary": nightly,
                   "recall_quality_gate": {"ok": True, "vacuous": False}}
    elif envelope == "contract":
        nightly = {"runs": {"nightly": nightly}}
    boundaries = _boundaries(nightly)
    assert boundaries["nightly_execution"]["state"] == "failed"
    assert boundaries["nightly_execution"]["reason"] == "release_commit_mismatch"
    assert boundaries["recall_quality"]["state"] == "failed"


def test_version_label_does_not_supply_missing_commit_binding():
    nightly = _bound_nightly()
    nightly.pop("release_commit")
    nightly["version"] = RELEASE.version
    boundaries = _boundaries(nightly)
    assert boundaries["nightly_execution"]["state"] == "unverified"
    assert boundaries["nightly_execution"]["reason"] == "nightly_release_identity_missing"
    assert boundaries["recall_quality"]["state"] == "unverified"


def test_current_wrapper_does_not_relabel_an_old_supervisor_run():
    nightly = _bound_nightly(supervisor_summary=_bound_nightly(release_commit="b" * 40))
    assert _boundaries(nightly)["nightly_execution"]["reason"] == "release_commit_mismatch"


@pytest.mark.parametrize("field,expected_reason", [
    ("deployment_receipt_id", "release_receipt_mismatch"),
    ("release_session_id", "release_session_mismatch"),
])
def test_nightly_rejects_another_deployment_of_the_same_commit(field, expected_reason):
    boundaries = _boundaries(_bound_nightly(**{field: "old"}),
                             expected_receipt=RELEASE.receipt_id, expected_session=RELEASE.session_id)
    assert boundaries["nightly_execution"]["state"] == "failed"
    assert boundaries["nightly_execution"]["reason"] == expected_reason


def test_nightly_missing_requested_receipt_is_unverified():
    boundaries = _boundaries(_bound_nightly(deployment_receipt_id=""),
                             expected_receipt=RELEASE.receipt_id)
    assert boundaries["nightly_execution"]["state"] == "unverified"
    assert boundaries["nightly_execution"]["reason"] == "receipt_session_binding_not_supplied"


def test_matching_nightly_preserves_actual_execution_failure():
    nightly = _bound_nightly(ok=False, nightly_diagnostics={
        "execution_ok": False, "first_failed_step": "production_recall",
    })
    boundary = _boundaries(nightly)["nightly_execution"]
    assert boundary["state"] == "failed"
    assert boundary["reason"] == "nightly_step_failed"
    assert boundary["first_failed_step"] == "production_recall"


def test_matching_verified_nightly_report_can_be_correlated():
    boundaries = _boundaries(_bound_nightly(), expected_receipt=RELEASE.receipt_id,
                             expected_session=RELEASE.session_id)
    assert boundaries["nightly_execution"]["state"] == "reported_pass"
    assert boundaries["recall_quality"]["state"] == "reported_pass"


def test_supervisor_preserves_verified_release_and_run_on_rewrite(tmp_path, monkeypatch):
    monkeypatch.setattr("eimemory.governance.release.evidence_contract.current_release_identity",
                        lambda *_args, **_kwargs: RELEASE)
    with Runtime.create(root=tmp_path) as runtime:
        record_ids = []
        for run_id, ok in (("run-first", True), ("run-second", False)):
            summary = supervisor_summary(command="nightly", ok=ok, duration_ms=1, memory_peak=0,
                                         run_id=run_id, started_at="2026-10-16T00:00:00Z")
            # The caller cannot relabel the authority supplied by the existing
            # deployment receipt path during a supervisor rewrite.
            summary["release_commit"] = "b" * 40
            saved = persist_supervisor_summary(runtime, scope=SCOPE, summary=summary)
            record_ids.append(saved.record_id)
            contract = build_supervisor_contract(runtime, scope=SCOPE)["runs"]["nightly"]
            assert contract["run_id"] == run_id
            assert contract["started_at"] == summary["started_at"]
            assert contract["record_id"] == saved.record_id
            for key, value in release_identity_payload(RELEASE).items():
                assert saved.content[key] == value
                assert saved.meta[key] == value
                assert contract[key] == value
        assert record_ids[0] == record_ids[1]


def test_legacy_supervisor_release_metadata_is_exposed():
    record = RecordEnvelope.create(kind="reflection", title="Legacy supervisor", summary="nightly",
        scope=SCOPE, meta={"report_type": "supervisor_run", "command": "nightly",
                          **release_identity_payload(RELEASE)},
        content={"report_type": "supervisor_run", "command": "nightly", "ok": True})
    runtime = SimpleNamespace(store=SimpleNamespace(list_records_by_meta_value=lambda **_: [record]))
    contract = build_supervisor_contract(runtime, scope=SCOPE)["runs"]["nightly"]
    assert contract["release_commit"] == COMMIT
    assert contract["record_id"] == record.record_id
    assert "run_id" not in contract


@pytest.mark.parametrize("extra", [{}, {"meta": None}, {"meta": []}, {"meta": "legacy"}])
def test_content_only_supervisor_reader_does_not_invent_binding(extra):
    record = SimpleNamespace(content={"ok": False, "nightly_diagnostics": {
        "execution_ok": False, "first_failed_step": "provider_binding",
    }}, **extra)
    summary = _summary_from_record(record, command="nightly")
    assert summary["nightly_diagnostics"]["first_failed_step"] == "provider_binding"
    assert "record_id" not in summary
    assert "run_id" not in summary
    assert not any(key in summary for key in release_identity_payload(RELEASE))


def test_supervisor_does_not_manufacture_binding_from_caller_labels(tmp_path, monkeypatch):
    monkeypatch.setattr("eimemory.governance.release.evidence_contract.current_release_identity",
                        lambda *_args, **_kwargs: None)
    with Runtime.create(root=tmp_path) as runtime:
        summary = supervisor_summary(command="nightly", ok=True, duration_ms=1, memory_peak=0)
        summary.update(release_identity_payload(RELEASE))
        saved = persist_supervisor_summary(runtime, scope=SCOPE, summary=summary)
        assert "release_commit" not in saved.content
        assert "release_commit" not in saved.meta
        assert "release_commit" not in build_supervisor_contract(runtime, scope=SCOPE)["runs"]["nightly"]


def test_unhandled_nightly_failure_persists_distinct_execution_identity(monkeypatch):
    captured = []
    runtime = SimpleNamespace(
        evolution=SimpleNamespace(build_roi_report=lambda **_: {"ok": True}),
        store=SimpleNamespace(list_records=lambda **_: (_ for _ in ()).throw(RuntimeError("failed"))),
    )
    monkeypatch.setattr(jobs, "persist_supervisor_summary",
                        lambda _runtime, **kwargs: captured.append(kwargs["summary"]))
    for _ in range(2):
        with pytest.raises(RuntimeError, match="failed"):
            jobs.run_nightly_jobs(runtime, scope={})
    assert captured[0]["run_id"] != captured[1]["run_id"]
    for summary in captured:
        assert summary["started_at"]
        assert summary["ok"] is False
        assert summary["nightly_diagnostics"]["unhandled_exception_type"] == "RuntimeError"
