"""Isolated SQLite observations; no production incident/model/policy is used."""
from copy import deepcopy
from dataclasses import asdict
from datetime import datetime, timezone
import importlib
import json

import pytest

from eimemory.api.runtime import Runtime
from eimemory.contracts.nightly_step_evidence import native_l5_failure_diagnostics, step_evidence_diagnostics
from eimemory.contracts.release_identity import ReleaseIdentity, release_identity_payload
from eimemory.governance.evolution.system_code_maintenance import run_system_code_maintenance
from eimemory.governance.learning.nightly_step_evidence import (
    archive_nightly_step_evidence, read_nightly_step_evidence, report_digest,
)
from eimemory.governance.learning.supervisor import (
    build_supervisor_contract, persist_nightly_command_outcome, persist_supervisor_summary, supervisor_summary,
)
from eimemory.governance.l5.native_assessment import run_native_l5_assessment
from eimemory.models.records import ScopeRef
from eimemory.ops.runtime_identity_drift import detect_runtime_identity_drift
from eimemory.scheduler import jobs
from eimemory.scheduler.result_contract import nightly_result_diagnostics

SCOPE = ScopeRef(tenant_id="tenant", agent_id="agent", workspace_id="workspace", user_id="user")
RELEASE = ReleaseIdentity("a" * 40, "1.14.155", "receipt-isolated", "session-isolated")
RUN = "nightly-isolated-observation"


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    monkeypatch.setattr("eimemory.governance.release.evidence_contract.current_release_identity", lambda *_a, **_k: RELEASE)
    monkeypatch.setattr("eimemory.governance.l5.native_assessment.current_release_identity", lambda *_a, **_k: RELEASE)
    with Runtime.create(root=tmp_path / "runtime") as value:
        yield value


def report(status="idle", *, ok=True):
    detector = detect_runtime_identity_drift(expected_commit=RELEASE.commit, scope=asdict(SCOPE),
        unit_environments={"eimemory-rpc.service": "EIMEMORY_RUNTIME_COMMIT=" + RELEASE.commit},
        detected_at="2026-10-19T00:00:00Z")
    return {"schema": "system_code_maintenance.v1", "ok": ok, "execution_ok": ok,
        "status": status, "repair_complete": False, "system_detection_enabled": True,
        "runtime_identity": detector, "native_policy_owner": {"ok": True, "status": "idle", "processed": []},
        "system_code_repair": {"ok": True, "status": status, "processed": []},
        "code_evolution": {"ok": ok, "reports": []}}


def archive(runtime, original, *, run=RUN):
    return archive_nightly_step_evidence(runtime, scope=SCOPE, run_id=run, step="code_evolution", report=original)


def read(runtime, reference, *, run=RUN, scope=SCOPE, step="code_evolution", release=None):
    return read_nightly_step_evidence(runtime, scope=scope, run_id=run, step=step, reference=reference,
        expected_release=release_identity_payload(RELEASE) if release is None else release)


@pytest.mark.parametrize("status,ok", [("idle", True), ("processed", True), ("deferred", True), ("blocked", False)])
def test_complete_original_roundtrip_preserves_unknown_and_private_body_only_in_archive(runtime, status, ok):
    original = report(status, ok=ok)
    original["code_evolution"]["reports"] = [{"ok": ok, "status": status, "private_context": "private-body-" * 8000}]
    original["system_code_repair"]["processed"] = [] if status == "idle" else [{"transaction_id": "isolated-tx", "status": status}]
    reference = archive(runtime, original)
    assert step_evidence_diagnostics(reference) == reference
    saved = read(runtime, reference)
    assert saved.content["step_report"] == original
    assert saved.content["report_digest"] == report_digest(original)
    assert saved.content["step_report"]["runtime_identity"]["known_before_detection"] is None
    assert saved.content["step_report"]["runtime_identity"]["source_facts"]["facts"]["manual_bootstrap"] is None
    assert reference["report_ok"] is ok and reference["report_status"] == status
    assert reference["repair_complete"] is False and "private" not in json.dumps(reference)
    original["runtime_identity"]["known_before_detection"] = False
    assert read(runtime, reference).content["step_report"]["runtime_identity"]["known_before_detection"] is None


def test_one_run_cannot_overwrite_its_original_observation(runtime):
    original = report()
    first = archive(runtime, original)
    assert archive(runtime, original) == first
    with pytest.raises(ValueError, match="run_report_changed"):
        archive(runtime, report("blocked", ok=False))
    assert read(runtime, first).content["step_report"] == original


def test_native_archive_uses_real_precise_creation_time_and_replay_keeps_original_clock(runtime):
    before = datetime.now(timezone.utc)
    original = report()
    first = archive(runtime, original)
    saved = read(runtime, first)
    after = datetime.now(timezone.utc)
    occurred = datetime.fromisoformat(saved.time.occurred_at.replace('Z', '+00:00'))
    assert before <= occurred <= after
    assert archive(runtime, original) == first
    assert read(runtime, first).time.occurred_at == saved.time.occurred_at


@pytest.mark.parametrize('invalid', ['2026-10-19T00:00:00', '2026-10-19T08:00:00+08:00', False, 0, 'not-a-time'])
def test_optional_precise_record_time_rejects_unknown_or_non_utc_without_writes(runtime, invalid):
    from eimemory.governance.learning.learning_state import append_learning_record_once
    before = runtime.store.count_records(kinds=['reflection'], scope=SCOPE)
    with pytest.raises(ValueError, match='occurred_at_invalid'):
        append_learning_record_once(runtime, kind='reflection', title='isolated time validation',
            summary='', scope=SCOPE, loop_id='fixture', step_name='time', semantic_key='fixture', occurred_at=invalid)
    assert runtime.store.count_records(kinds=['reflection'], scope=SCOPE) == before


@pytest.mark.parametrize("wrong", ["run", "scope", "step", "release", "digest", "missing"])
def test_exact_readback_cannot_borrow_another_scope_run_release_or_partial_snapshot(runtime, wrong):
    reference = archive(runtime, report())
    args = {}
    if wrong == "run": args["run"] = "another-run"
    if wrong == "scope": args["scope"] = ScopeRef(**{**asdict(SCOPE), "user_id": "foreign"})
    if wrong == "step": args["step"] = "native_l5_assessment_failure"
    if wrong == "release": args["release"] = {**release_identity_payload(RELEASE), "deployment_receipt_id": "foreign"}
    if wrong == "digest": reference["envelope_digest"] = "f" * 64
    if wrong == "missing": reference.pop("archive_complete")
    with pytest.raises(ValueError, match="binding_mismatch"):
        read(runtime, reference, **args)


@pytest.mark.parametrize("change", ["report", "schema", "run", "source", "status", "release", "nested_unknown"])
def test_changed_archive_never_matches_the_original_reference_even_with_recomputed_report_digest(runtime, change):
    reference = archive(runtime, report())
    saved = read(runtime, reference)
    if change == "report": saved.content["step_report"]["native_policy_owner"]["status"] = "processed"
    if change == "schema": saved.content.pop("schema")
    if change == "run": saved.content["run_id"] = "foreign"
    if change == "source": saved.source = "foreign"
    if change == "status": saved.status = "active"
    if change == "release": saved.content["release_commit"] = "b" * 40
    if change == "nested_unknown": saved.content["step_report"]["runtime_identity"]["known_before_detection"] = False
    saved.content["report_digest"] = report_digest(saved.content["step_report"])
    runtime.store.rewrite(saved)
    with pytest.raises(ValueError, match="binding_mismatch"):
        read(runtime, reference)


def test_actual_recovery_failure_from_real_isolated_ledger_is_archived_without_terminal_or_idle(runtime, monkeypatch):
    from test_code_evolution_recovery import _observing_manager
    manager = _observing_manager(runtime.store, "isolated-observing-failure")
    monkeypatch.setattr("eimemory.governance.code_evolution_effects.sample_code_evolution_observation",
        lambda *_a, **_k: {"ok": False, "reason": "observation_release_identity_unavailable"})
    original = run_system_code_maintenance(runtime, scope=SCOPE, owner_id="isolated-nightly", detect_and_route=False)
    assert original["ok"] is False and original["execution_ok"] is False
    assert len(original["reports"]) == 1
    assert manager.store.get_transaction("isolated-observing-failure")["current_state"] == "OBSERVING"
    assert manager.store.get_terminal_receipt("isolated-observing-failure") is None
    reference = archive(runtime, original)
    assert read(runtime, reference).content["step_report"] == original
    assert reference["report_ok"] is False and reference["report_status"] != "idle"


def test_actual_recovery_progress_is_archived_without_inventing_repair_completion(runtime, monkeypatch):
    from eimemory.governance.evolution.code_evolution_transaction import CodeEvolutionTransactionManager
    manager = CodeEvolutionTransactionManager(runtime.store, owner_id="isolated-recovery-owner")
    manager.create_detected({"transaction_id": "isolated-pending", "idempotency_key": "isolated-pending",
        "scope": asdict(SCOPE), "incident": {"incident_id": "isolated-fixture-incident", "incident_class": "code"},
        "repository": {"root": "/isolated-fixture", "remote": "origin", "ref": "master", "base_commit": "a" * 40},
        "candidate_tree_digest": "b" * 64, "candidate_commit": "c" * 40})
    for state in ("DIAGNOSED", "PROVIDER_RESOLVED", "PATCH_PROPOSED", "PATCH_VALIDATED", "CANDIDATE_MATERIALIZED",
                  "FOCUSED_VERIFIED", "REGRESSION_VERIFIED", "FULL_SUITE_VERIFIED", "POLICY_AUTHORIZED", "COMMIT_INTENT"):
        manager.transition("isolated-pending", state)
    monkeypatch.setattr("eimemory.governance.code_evolution_effects.read_code_evolution_external_state", lambda *_a, **_k: {
        "candidate_commit": "c" * 40, "base_commit": "a" * 40, "parent": "a" * 40,
        "tree_matches": True, "transaction_trailer_matches": True})
    original = run_system_code_maintenance(runtime, scope=SCOPE, owner_id="isolated-recovery", detect_and_route=False)
    assert original["ok"] is True and original["reports"][0]["current_state"] == "COMMITTED"
    assert manager.store.get_transaction("isolated-pending")["current_state"] == "COMMITTED"
    assert manager.store.get_terminal_receipt("isolated-pending") is None
    reference = archive(runtime, original)
    assert read(runtime, reference).content["step_report"] == original
    assert reference["repair_complete"] is False


@pytest.mark.parametrize("ok", [False, True])
def test_nightly_wrapper_preserves_actual_result_and_requires_complete_archive(runtime, monkeypatch, ok):
    original = report("processed" if ok else "blocked", ok=ok)
    original["system_code_repair"]["processed"] = [{"incident_id": "isolated", "transaction_id": "isolated-tx", "status": "submitted"}]
    monkeypatch.setattr(jobs, "_run_code_evolution_maintenance", lambda *_a, **_k: deepcopy(original))
    wrapped = jobs._run_archived_code_maintenance(runtime, scope=asdict(SCOPE), run_id=RUN)
    assert wrapped["ok"] is ok and wrapped["status"] == original["status"]
    assert read(runtime, wrapped["evidence_archive"]).content["step_report"] == original
    def fail(*_a, **_k): raise OSError("private disk diagnostic")
    monkeypatch.setattr("eimemory.governance.learning.nightly_step_evidence.archive_nightly_step_evidence", fail)
    failed = jobs._run_archived_code_maintenance(runtime, scope=asdict(SCOPE), run_id="another-run")
    assert failed["ok"] is False and failed["system_code_repair"] == original["system_code_repair"]
    assert failed["maintenance_execution_ok"] is ok
    assert "private" not in json.dumps(failed["evidence_archive"])


def persisted_summary(runtime, reference, *, ok=True):
    summary = supervisor_summary(command="nightly", ok=ok, duration_ms=1, memory_peak=0, run_id=RUN)
    summary["nightly_diagnostics"] = {"execution_ok": ok, "failed_steps": [] if ok else ["native_l5_assessment"],
                                       "code_maintenance_evidence": reference}
    record = persist_supervisor_summary(runtime, scope=SCOPE, summary=summary)
    return {**record.content, "record_id": record.record_id}


def test_cli_supervisor_and_independent_ending_bind_complete_original_and_recheck_on_reopen(runtime):
    reference = archive(runtime, report())
    summary = persisted_summary(runtime, reference)
    ending = persist_nightly_command_outcome(runtime, scope=SCOPE, supervisor=summary, identity_repair={"ok": True})
    assert ending["step_evidence"] == {"code_evolution": reference}
    cli = importlib.import_module("eimemory.cli.main")
    output = cli._nightly_cli_summary({"ok": True, "supervisor_summary": summary})
    assert output["supervisor_summary"]["nightly_diagnostics"]["code_maintenance_evidence"] == reference
    assert "step_report" not in json.dumps(output)
    with Runtime.create(root=runtime.store.root) as reopened:
        assert build_supervisor_contract(reopened, scope=SCOPE)["runs"]["nightly"]["command_execution"] == ending
    saved = read(runtime, reference)
    saved.content["step_report"]["runtime_identity"]["prior_user_reported"] = False
    saved.content["report_digest"] = report_digest(saved.content["step_report"])
    runtime.store.rewrite(saved)
    assert build_supervisor_contract(runtime, scope=SCOPE)["runs"]["nightly"]["command_execution"]["status"] == "not_recorded"


def test_successful_supervisor_cannot_point_to_another_run_or_unverified_archive(runtime):
    reference = archive(runtime, report(), run="foreign-run")
    with pytest.raises(ValueError, match="binding_mismatch"):
        persisted_summary(runtime, reference)
    with pytest.raises(ValueError, match="unverified"):
        persisted_summary(runtime, {"ok": False, "archive_verified": False})
    failed = persisted_summary(runtime, step_evidence_diagnostics(None), ok=False)
    assert failed["ok"] is False
    ending = persist_nightly_command_outcome(runtime, scope=SCOPE, supervisor=failed, identity_repair={"ok": True})
    assert ending["ok"] is False and ending["first_failed_phase"] == "scheduler"


@pytest.mark.parametrize("error", [RuntimeError("private prompt"), ValueError("native_l5_private_prompt secret"),
                                   OSError("private credential")])
def test_actual_reader_exception_retains_stage_type_and_durable_original_without_arbitrary_text(runtime, monkeypatch, error):
    monkeypatch.setattr("eimemory.governance.l5.native_assessment._capability_selector", lambda owner: owner)
    monkeypatch.setattr("eimemory.governance.evolution.code_evolution_provider_authority.delegated_provider_authority",
                        lambda *_a, **_k: None)
    def fail(**_kwargs): raise error
    monkeypatch.setattr(runtime, "build_l5_readiness_report", fail)
    result = run_native_l5_assessment(runtime, scope=asdict(SCOPE), loop_id=RUN)
    assert result["error"] == "native_l5_execution_failed" and result["error_type"] == type(error).__name__
    assert result["failure_stage"] == "readiness_reader" and result["execution_ok"] is False
    assert result["archive_verified"] is False and result["record_id"] == ""
    assert result["failure_evidence"]["archive_verified"] is True
    original = read(runtime, result["failure_evidence"], step="native_l5_assessment_failure").content["step_report"]
    assert original == {key: value for key, value in result.items() if key != "failure_evidence"}
    assert "private" not in json.dumps(result)
    diagnostics = nightly_result_diagnostics({"native_l5_assessment": result}, [])
    public = diagnostics["native_l5_assessment"]
    assert public["failure_stage"] == "readiness_reader" and public["error_type"] == type(error).__name__
    assert public["failure_evidence"] == result["failure_evidence"]
    assert diagnostics["execution_ok"] is False and "native_l5_assessment" in diagnostics["failed_steps"]
    summary = supervisor_summary(command="nightly", ok=False, duration_ms=1, memory_peak=0, run_id=RUN)
    summary["nightly_diagnostics"] = diagnostics
    stored = persist_supervisor_summary(runtime, scope=SCOPE, summary=summary)
    summary = {**stored.content, "record_id": stored.record_id}
    ending = persist_nightly_command_outcome(runtime, scope=SCOPE, supervisor=summary, identity_repair={"ok": True})
    assert ending["ok"] is False and ending["step_evidence"]["native_l5_assessment_failure"] == result["failure_evidence"]
    assert build_supervisor_contract(runtime, scope=SCOPE)["runs"]["nightly"]["command_execution"] == ending


def test_failure_archive_failure_does_not_mask_original_reader_failure_or_recurse(runtime, monkeypatch):
    monkeypatch.setattr("eimemory.governance.l5.native_assessment._capability_selector", lambda owner: owner)
    monkeypatch.setattr("eimemory.governance.evolution.code_evolution_provider_authority.delegated_provider_authority",
                        lambda *_a, **_k: None)
    def fail_reader(**_kwargs): raise RuntimeError("private original")
    def fail_archive(*_args, **_kwargs): raise OSError("private archive")
    monkeypatch.setattr(runtime, "build_l5_readiness_report", fail_reader)
    monkeypatch.setattr("eimemory.governance.learning.nightly_step_evidence.archive_nightly_step_evidence", fail_archive)
    result = run_native_l5_assessment(runtime, scope=asdict(SCOPE), loop_id=RUN)
    assert result["error_type"] == "RuntimeError" and result["failure_stage"] == "readiness_reader"
    assert result["failure_evidence"]["error_type"] == "OSError" and result["failure_evidence"]["archive_verified"] is False
    assert "private" not in json.dumps(result) and result["execution_ok"] is False


@pytest.mark.parametrize("value", [None, [], {"error": "private"}, {"failure_stage": [], "error": [], "error_type": "private value"}])
def test_safe_diagnostics_never_reflect_arbitrary_provider_payload(value):
    assert "private" not in json.dumps(step_evidence_diagnostics(value))
    if isinstance(value, dict):
        assert "private" not in json.dumps(native_l5_failure_diagnostics(value))


@pytest.mark.parametrize('reason', ['private diagnostic text', 'controlled_acceptance_private_detail', [], None])
def test_lineage_public_diagnostics_never_export_unknown_verifier_text(reason):
    from eimemory.contracts.nightly_step_evidence import native_l5_failure_diagnostics
    value = {'controlled_release_lineage': {'reason': 'current_controlled_recall_evidence_invalid',
                                           'verifier_reason': reason}}
    assert native_l5_failure_diagnostics(value) == {
        'controlled_recall_verifier_reason': 'controlled_acceptance_verification_failed'}


def test_existing_lease_busy_survives_exact_archive_and_public_nightly(runtime):
    from eimemory.scheduler.result_contract import nightly_result_diagnostics
    from eimemory.scheduler.result_contract import _nightly_step
    original = report('deferred')
    original['reason'] = 'existing_lease_busy'
    original['code_evolution']['reports'] = [{'ok': True, 'transaction_id': 'busy-fixture',
        'status': 'in_progress', 'reason': 'existing_lease_busy', 'repair_complete': False}]
    reference = archive(runtime, original)
    assert read(runtime, reference).content['step_report'] == original
    original['evidence_archive'] = reference
    steps = []
    _nightly_step(steps, 'code_evolution', lambda: original)
    diagnostics = nightly_result_diagnostics({'code_evolution': original}, steps)
    assert diagnostics['execution_ok'] is True
    assert diagnostics['code_maintenance_evidence']['report_status'] == 'deferred'
    assert diagnostics['code_maintenance_evidence']['repair_complete'] is False
