"""Real-store regressions for revoked and unevaluable closure evidence."""
from __future__ import annotations

from contextlib import closing
from dataclasses import asdict, replace
from datetime import datetime, timezone
import json

import pytest

from eimemory.api.runtime import Runtime
from eimemory.evaluation.task_replay import run_real_task_replay
from eimemory.governance.l5 import openclaw_channel_acceptance as channel
from eimemory.governance.l5.real_replay_gate import build_verified_real_replay_summary
from eimemory.governance.release import evidence_contract
from eimemory.governance.release.evidence_contract import ReleaseIdentity
from eimemory.models.records import RecallBundle, RecordEnvelope, ScopeRef
from tests.test_real_replay_gate import SCOPE, _append_real_source, _dataset, _seed_recall_target, _ten_sources


@pytest.fixture
def replay_runtime(tmp_path, monkeypatch):
    # This suite tests report lifecycle, not changing concurrent source bytes.
    for module in ("eimemory.evaluation.task_replay", "eimemory.governance.l5.real_replay_gate"):
        monkeypatch.setattr(module + ".runtime_package_tree_digest", lambda: "7" * 64)
    with closing(Runtime.create(root=tmp_path / "replay")) as runtime:
        _seed_recall_target(runtime)
        yield runtime, _dataset(_ten_sources(runtime))


@pytest.mark.parametrize("status", ["quarantined", "archived", "superseded", "rejected"])
def test_revoked_latest_replay_cannot_reuse_older_success(replay_runtime, status):
    runtime, dataset = replay_runtime
    old = run_real_task_replay(runtime, dataset, seed=False, persist_report=True)
    latest = run_real_task_replay(runtime, dataset, seed=False, persist_report=True)
    assert old["persisted_record_id"] != latest["persisted_record_id"]
    record = runtime.store.get_by_id(latest["persisted_record_id"], scope=SCOPE)
    record.status = status
    runtime.store.rewrite(record)

    result = build_verified_real_replay_summary(runtime, scope=SCOPE)
    assert result["ok"] is False
    assert result["reason"] == "replay_report_inactive"
    assert result["record_id"] == latest["persisted_record_id"]
    from eimemory.governance.l5.closure_rehearsal import _replay_authenticity_deficits
    assert _replay_authenticity_deficits(result) == ["verified_real_replay_authenticity_failed"]


def test_new_actual_replay_restores_gate_after_revocation(replay_runtime):
    runtime, dataset = replay_runtime
    first = run_real_task_replay(runtime, dataset, seed=False, persist_report=True)
    record = runtime.store.get_by_id(first["persisted_record_id"], scope=SCOPE)
    record.status = "quarantined"
    runtime.store.rewrite(record)
    assert build_verified_real_replay_summary(runtime, scope=SCOPE)["ok"] is False

    recovered = run_real_task_replay(runtime, dataset, seed=False, persist_report=True)
    result = build_verified_real_replay_summary(runtime, scope=SCOPE)
    assert result["ok"] is True
    assert result["record_id"] == recovered["persisted_record_id"]
    assert result["replay_execution_id"] != first["replay_execution_id"]


@pytest.mark.parametrize("other_identity", ["scope", "code"])
def test_unrelated_revoked_replay_does_not_revoke_current_evidence(replay_runtime, other_identity):
    runtime, dataset = replay_runtime
    current = run_real_task_replay(runtime, dataset, seed=False, persist_report=True)
    original = runtime.store.get_by_id(current["persisted_record_id"], scope=SCOPE)
    payload = original.to_dict()
    payload["record_id"] += "-unrelated"
    payload["status"] = "quarantined"
    if other_identity == "scope":
        payload["scope"]["user_id"] = "another-user"
    else:
        payload["meta"]["package_tree_digest"] = "8" * 64
        payload["content"]["report"]["package_tree_digest"] = "8" * 64
    runtime.store.append(RecordEnvelope.from_dict(payload))

    result = build_verified_real_replay_summary(runtime, scope=SCOPE)
    assert result["ok"] is True
    assert result["record_id"] == current["persisted_record_id"]


@pytest.mark.parametrize("criterion", [{}, {"expected_text": []}, {"expected_text": "  "}])
def test_unlabelled_replay_cannot_certify_empty_recall(replay_runtime, monkeypatch, criterion):
    runtime, dataset = replay_runtime
    calls = []
    def recall(**kwargs):
        calls.append(kwargs)
        return RecallBundle([], [], [], 0.0, "none", {})
    monkeypatch.setattr(runtime.memory, "recall", recall)
    for case in dataset["cases"]:
        case.pop("expected_text")
        case.update(criterion)
    report = run_real_task_replay(runtime, dataset, seed=False, persist_report=True)
    gate = build_verified_real_replay_summary(runtime, scope=SCOPE)
    assert report["pass_count"] == 0 and report["verdict"] == "fail"
    assert {sample["failure_reason"] for sample in report["samples"]} == {"missing_expectation"}
    assert not calls
    assert gate["ok"] is False and gate["sample_count"] == 0


@pytest.mark.parametrize("explanation", [
    {"retrieval_status": "unavailable"},
    {"retrieval_status": "degraded"},
    {"relevance_selector": {"collection_complete": False}},
    {"relevance_selector": {"collection_complete": "true"}},
    {"engine_diagnostics": {"source_budget_exhausted": 1}},
    {"engine_diagnostics": {"drops": {"recall_budget_exhausted": 1}}},
])
def test_incomplete_recall_is_not_a_passing_real_replay(replay_runtime, monkeypatch, explanation):
    runtime, dataset = replay_runtime
    target = RecordEnvelope.create(kind="memory", title="cobalt", scope=SCOPE)
    monkeypatch.setattr(runtime.memory, "recall", lambda **kwargs:
        RecallBundle([target], [], [], 1.0, "use", explanation))
    report = run_real_task_replay(runtime, dataset, seed=False, persist_report=True)
    gate = build_verified_real_replay_summary(runtime, scope=SCOPE)
    assert report["pass_count"] == 0
    assert {sample["failure_reason"] for sample in report["samples"]} == {"retrieval_incomplete"}
    assert gate["ok"] is False and gate["sample_count"] == 0


def test_empty_replay_does_not_pass_even_with_zero_threshold(tmp_path):
    with closing(Runtime.create(root=tmp_path)) as runtime:
        report = run_real_task_replay(runtime, {"cases": [], "threshold": 0.0}, seed=False)
    assert report["verdict"] == "fail"


def test_available_negative_only_replay_keeps_its_real_verdict(replay_runtime, monkeypatch):
    runtime, dataset = replay_runtime
    for case in dataset["cases"]:
        case.pop("expected_text")
        case["negative_expected_text"] = ["forbidden answer"]
    monkeypatch.setattr(runtime.memory, "recall", lambda **kwargs:
        RecallBundle([], [], [], 0.0, "none", {"retrieval_status": "no_evidence",
            "relevance_selector": {"collection_complete": True}}))
    report = run_real_task_replay(runtime, dataset, seed=False, persist_report=True)
    assert report["verdict"] == "pass" and report["pass_count"] == 10
    assert build_verified_real_replay_summary(runtime, scope=SCOPE)["ok"] is True


@pytest.mark.parametrize("invalid", [None, "true", "false", 1, "missing"])
def test_replay_requires_explicit_boolean_evaluation_proof(replay_runtime, invalid):
    runtime, dataset = replay_runtime
    report = run_real_task_replay(runtime, dataset, seed=False, persist_report=True)
    record = runtime.store.get_by_id(report["persisted_record_id"], scope=SCOPE)
    sample = record.content["report"]["samples"][0]
    if invalid == "missing":
        sample.pop("evaluable")
    else:
        sample["evaluable"] = invalid
    runtime.store.rewrite(record)
    gate = build_verified_real_replay_summary(runtime, scope=SCOPE)
    assert gate["ok"] is False
    assert gate["reason"] == "verified_real_replay_authenticity_failed"
    assert gate["rejection_reasons"] == {"sample_evaluation_unverified": 1}


def test_partial_replay_cannot_close_gate_by_dropping_unavailable_half(replay_runtime, monkeypatch):
    runtime, dataset = replay_runtime
    for index in range(10, 20):
        source = _append_real_source(runtime, index=index, task_type=f"production.task.{index % 5}")
        dataset["cases"].extend(_dataset([source])["cases"])
    native_recall = runtime.memory.recall
    calls = 0
    def recall(**kwargs):
        nonlocal calls
        calls += 1
        if calls <= 10:
            return native_recall(**kwargs)
        return RecallBundle([], [], [], 0.0, "none", {"retrieval_status": "unavailable"})
    monkeypatch.setattr(runtime.memory, "recall", recall)
    report = run_real_task_replay(runtime, dataset, seed=False, persist_report=True)
    gate = build_verified_real_replay_summary(runtime, scope=SCOPE)
    assert report["verdict"] == "fail" and report["pass_count"] == 10
    assert gate["sample_count"] == 10 and gate["pass_rate"] == 1.0
    assert gate["ok"] is False
    assert gate["evaluation_complete"] is False and gate["unevaluable_count"] == 10
    assert gate["reason"] == "verified_real_replay_incomplete"

    from eimemory.governance.l5 import closure_rehearsal
    from tests.test_l5_closure_rehearsal import _honrui_like_zero_real_task_readiness
    release = ReleaseIdentity("a" * 40, "synthetic", "receipt", "session")
    readiness = _honrui_like_zero_real_task_readiness(release, "pending")
    readiness["verified_real_replay"] = gate
    assert not closure_rehearsal._compatible_live_task_accumulation(readiness, release=release)
    deficits = closure_rehearsal.bootstrap_pending_non_recall_deficits(readiness, release=release)
    assert "verified_real_replay_incomplete" in deficits
    diagnostics = {}
    reason = closure_rehearsal._bootstrap_pending_readiness_evidence_reason(
        runtime, readiness, scope=SCOPE, release=release, pending_record_id="pending",
        repo_root="", diagnostics=diagnostics,
    )
    assert reason == closure_rehearsal.NON_RECALL_EVIDENCE_INCOMPLETE
    assert "verified_real_replay_incomplete" in diagnostics["non_recall_evidence_deficits"]
    assert closure_rehearsal._real_business_gate({}, {**gate, "ok": True})["ok"] is False


@pytest.mark.parametrize("report", [
    {"gate_ok": "false"},
    {"gate_ok": 1},
    {"passed_threshold": "true"},
    {"gate_ok": False, "passed_threshold": True},
    {"gate_ok": True, "passed_threshold": False},
    {"gate_ok": False, "quality_gate": {"ok": True}},
    {"ok": False, "quality_gate": {"ok": True}},
    {"execution_ok": False, "quality_gate": {"ok": True}},
    {"error": "native evaluation unavailable", "quality_gate": {"ok": True}},
    {"quality_gate": {"ok": True, "blocking_metrics": {"failed": {"actual": 1}}}},
    *[{"quality_gate": {"ok": True}, name: {"ok": False}}
      for name in ("threshold_gate", "safety_gate", "isolation_gate", "gate")],
    {"quality_gate": {"ok": True}, "threshold_gate": {"ok": True, "error": "source unavailable"}},
    {"quality_gate": {"ok": True}, "safety_gate": {"ok": True, "blocking_metrics": {"failed": 1}}},
])
def test_malformed_quality_success_does_not_resolve_existing_gap(tmp_path, report):
    from eimemory.governance.learning.quality_gap_intake import ingest_quality_gate_reports

    with closing(Runtime.create(root=tmp_path)) as runtime:
        first = ingest_quality_gate_reports(runtime, scope=asdict(SCOPE), reports={
            "production_recall": {"quality_gate": {"ok": False, "blocked_reason": "recall_failed"}}})
        assert first["created_count"] == 1
        result = ingest_quality_gate_reports(runtime, scope=asdict(SCOPE), reports={"production_recall": report})
        assert result["resolved_count"] == 0
        assert all(finding["gate_ok"] is False for finding in result["findings"])


@pytest.mark.parametrize("report", [
    {"gate_ok": True}, {"passed_threshold": True},
    {"gate_ok": True, "passed_threshold": True},
    {"ok": True, "execution_ok": True, "quality_gate": {"ok": True}},
])
def test_consistent_quality_pass_still_resolves_existing_gap(tmp_path, report):
    from eimemory.governance.learning.quality_gap_intake import ingest_quality_gate_reports

    with closing(Runtime.create(root=tmp_path)) as runtime:
        ingest_quality_gate_reports(runtime, scope=asdict(SCOPE), reports={
            "production_recall": {"quality_gate": {"ok": False, "blocked_reason": "recall_failed"}}})
        result = ingest_quality_gate_reports(runtime, scope=asdict(SCOPE), reports={"production_recall": report})
        assert result["resolved_count"] == 1


@pytest.mark.parametrize("field", ["quality_gate", "threshold_gate", "safety_gate", "isolation_gate", "gate"])
@pytest.mark.parametrize("invalid", [
    pytest.param(None, id="null"), pytest.param(False, id="false"),
    pytest.param(True, id="true"), pytest.param(0, id="zero"),
    pytest.param(1, id="one"), pytest.param("", id="empty_string"),
    pytest.param("false", id="string"), pytest.param([], id="empty_list"),
    pytest.param([{"ok": False}], id="list_of_gates"),
])
@pytest.mark.parametrize("success_source", ["native", "legacy"])
def test_quality_gate_shape_cannot_be_masked_by_another_success(tmp_path, field, invalid, success_source):
    from eimemory.governance.learning.quality_gap_intake import ingest_quality_gate_reports

    with closing(Runtime.create(root=tmp_path)) as runtime:
        first = ingest_quality_gate_reports(runtime, scope=asdict(SCOPE), reports={
            "production_recall": {"quality_gate": {"ok": False, "blocked_reason": "recall_failed"}}})
        assert first["created_count"] == 1
        valid = ({"threshold_gate" if field == "quality_gate" else "quality_gate": {"ok": True}}
                 if success_source == "native" else {"gate_ok": True})
        result = ingest_quality_gate_reports(runtime, scope=asdict(SCOPE), reports={
            "production_recall": {**valid, field: invalid}})
        assert result["resolved_count"] == 0
        assert result["findings"][0]["gate_ok"] is False
        assert result["findings"][0]["blocked_reason"] == "quality_gate_shape_invalid"

        recovered = ingest_quality_gate_reports(runtime, scope=asdict(SCOPE), reports={
            "production_recall": valid})
        assert recovered["resolved_count"] == 1


@pytest.fixture
def channel_runtime(tmp_path, monkeypatch):
    scope = ScopeRef(agent_id="closure-agent", workspace_id="closure-workspace", user_id="owner")
    with closing(Runtime.create(root=tmp_path / "channel")) as runtime:
        receipt = runtime.store.append(RecordEnvelope.create(
            kind="promotion_request", title="Channel test deployment receipt", scope=scope,
            source="eimemory.deployment_receipt", status="deployed"))
        release = ReleaseIdentity("a" * 40, "synthetic", receipt.record_id, "session-1")
        current = [release]
        monkeypatch.setattr(channel, "verified_deployment_receipt_identity", lambda _: current[0])
        monkeypatch.setattr(evidence_contract, "current_release_identity", lambda *args: current[0])
        accepted = int(datetime.now(timezone.utc).timestamp() * 1000) + 10
        entry = {"status": "platform_accepted", "delivery_message_id": "private-platform-receipt",
                 "runtime_commit": release.commit, "session_key": "agent:main:feishu:direct:private-user",
                 "received_at_ms": accepted, "platform_accepted_at_ms": accepted + 1000}
        path = tmp_path / "channel-state.json"
        def save():
            path.write_text(json.dumps({"schema_version": "openclaw_reply_delivery.v2",
                                        "entries": {"private-inbound": entry}}))
        save()
        yield runtime, scope, current, path, entry, save


def test_openclaw_recorder_rejects_quarantined_idempotent_receipt(channel_runtime):
    runtime, scope, current, path, _, _ = channel_runtime
    first = runtime.record_openclaw_channel_acceptance(scope=asdict(scope), current_release=current[0], state_path=path)
    record = runtime.store.get_by_id(first["record_id"], scope=scope)
    record.status = "quarantined"
    runtime.store.rewrite(record)
    assert not channel.validate_openclaw_channel_acceptance(record, current_release=current[0])
    repeated = runtime.record_openclaw_channel_acceptance(scope=asdict(scope), current_release=current[0], state_path=path)
    assert repeated["ok"] is False
    assert repeated["error"] == "persisted_channel_acceptance_invalid"


def test_openclaw_records_distinct_sessions_without_runtime_release_discovery(channel_runtime, monkeypatch):
    runtime, scope, current, path, _, _ = channel_runtime
    # Receipt verification succeeds independently; no server release stamp is
    # available. Each receipt/session must still bind its own returned record.
    monkeypatch.setattr(evidence_contract, "current_release_identity", lambda *args: None)
    first_release = current[0]
    first = runtime.record_openclaw_channel_acceptance(scope=asdict(scope), current_release=first_release, state_path=path)
    current[0] = replace(first_release, session_id="session-2")
    second = runtime.record_openclaw_channel_acceptance(scope=asdict(scope), current_release=current[0], state_path=path)
    assert first["ok"] and second["ok"]
    assert first["record_id"] != second["record_id"]
    first_record = runtime.store.get_by_id(first["record_id"], scope=scope)
    second_record = runtime.store.get_by_id(second["record_id"], scope=scope)
    assert channel.validate_openclaw_channel_acceptance(first_record, current_release=first_release)
    assert channel.validate_openclaw_channel_acceptance(second_record, current_release=current[0])
    assert not channel.validate_openclaw_channel_acceptance(first_record, current_release=current[0])


@pytest.mark.parametrize("invalid", ["string", "fraction", "infinity"])
def test_openclaw_rejects_noninteger_native_timestamps(channel_runtime, invalid):
    runtime, scope, current, path, entry, save = channel_runtime
    value = entry["platform_accepted_at_ms"]
    entry["platform_accepted_at_ms"] = str(value) if invalid == "string" else value + 0.5 if invalid == "fraction" else float("inf")
    save()
    report = runtime.record_openclaw_channel_acceptance(scope=asdict(scope), current_release=current[0], state_path=path)
    assert report["ok"] is False


def test_openclaw_rejects_receipt_readback_changed_during_write(channel_runtime, monkeypatch):
    runtime, scope, current, path, _, _ = channel_runtime
    native_append = channel.append_learning_record_once
    def tamper(*args, **kwargs):
        record = native_append(*args, **kwargs)
        record.content["passed"] = False
        runtime.store.rewrite(record)
        return record
    monkeypatch.setattr(channel, "append_learning_record_once", tamper)
    report = runtime.record_openclaw_channel_acceptance(scope=asdict(scope), current_release=current[0], state_path=path)
    assert report["ok"] is False
    assert report["error"] == "persisted_channel_acceptance_invalid"


@pytest.mark.parametrize("recorder", ["openclaw", "external_inherited"])
def test_failed_old_release_recording_cannot_certify_new_server_release(channel_runtime, monkeypatch, recorder):
    from eimemory.governance.l5 import external_channel_acceptance as external

    runtime, scope, current, path, entry, save = channel_runtime
    old = current[0]
    new = replace(old, commit="b" * 40, receipt_id="receipt-current", session_id="session-current")
    monkeypatch.setattr(evidence_contract, "current_release_identity", lambda *args: new)
    if recorder == "openclaw":
        report = runtime.record_openclaw_channel_acceptance(scope=asdict(scope), current_release=old, state_path=path)
        validate = channel.validate_openclaw_channel_acceptance
    else:
        monkeypatch.setattr(evidence_contract, "verified_deployment_receipt_identity", lambda _: old)
        entry["runtime_commit"] = "c" * 40
        save()
        report = runtime.record_external_channel_acceptance(scope=asdict(scope), current_release=old,
            openclaw_state_path=path, external_state_path=path.with_name("absent-state.json"))
        validate = external.validate_external_channel_acceptance
    assert report["ok"] is False
    record = runtime.store.get_by_id(report["record_id"], scope=scope)
    assert not validate(record, current_release=old)
    assert not validate(record, current_release=new)
