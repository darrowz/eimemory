from copy import deepcopy
from types import SimpleNamespace

import pytest

from eimemory.governance import release_closure
from eimemory.governance import release_pre_observation as module
from eimemory.models.records import ScopeRef


SCOPE = dict(tenant_id="default", agent_id="a", workspace_id="w", user_id="u")
TX = dict(transaction_id="tx", profile_key="custom.profile", capability_id="custom.capability",
          base_commit="a" * 40, candidate_commit="b" * 40, deployed_commit="",
          origin="system_detector", known_before_detection=False, prior_user_reported=False,
          manual_bootstrap=False, current_state="DEPLOY_INTENT", terminal=False, **SCOPE)
RECEIPT = dict(ok=True, strict_transaction=True, transaction_id="tx", promotion_request_id="receipt",
               release_session_id="receipt", commit="b" * 40, version="1.0", release_path="/release")


def test_strict_deployment_dispatches_before_legacy_recall(monkeypatch):
    monkeypatch.setenv("EIMEMORY_CODE_EVOLUTION_TRANSACTION_MODE", "1")
    monkeypatch.setenv("EIMEMORY_CODE_EVOLUTION_TRANSACTION_ID", "tx")
    monkeypatch.setenv("EIMEMORY_RUNTIME_COMMIT", "b" * 40)
    expected = {"ok": True, "closure_complete": False, "status": "ready_for_observation"}
    calls = []
    monkeypatch.setattr(module, "run_pre_observation_closure", lambda runtime, **kw: calls.append(kw) or expected)
    receipt_calls = []
    runtime = SimpleNamespace(
        verify_and_record_deployment=lambda **kw: receipt_calls.append(kw) or deepcopy(RECEIPT)
    )
    result = release_closure.run_release_closure(runtime, scope=SCOPE, repo_root="/repo",
                                                current_link="/current", health_url="http://health", prior_commit="a" * 40)
    assert result == expected
    assert receipt_calls[0]["deployed_commit"] == "b" * 40
    assert calls[0]["transaction_id"] == "tx"
    assert calls[0]["receipt"] == RECEIPT


def test_strict_deployment_rejects_missing_candidate_identity(monkeypatch):
    monkeypatch.setenv("EIMEMORY_CODE_EVOLUTION_TRANSACTION_MODE", "1")
    monkeypatch.delenv("EIMEMORY_RUNTIME_COMMIT", raising=False)
    runtime = SimpleNamespace(
        verify_and_record_deployment=lambda **_kw: pytest.fail("receipt must not use checkout HEAD")
    )

    result = release_closure.run_release_closure(
        runtime,
        scope=SCOPE,
        repo_root="/repo",
        current_link="/current",
        health_url="http://health",
        prior_commit="a" * 40,
    )

    assert result["blocked_stage"] == "deployment_receipt"
    assert result["blocked_reason"] == "strict_deployed_commit_required"


def _runtime(monkeypatch, *, transaction=None, strict_error=""):
    monkeypatch.setattr(module, "CodeEvolutionStore", lambda store: SimpleNamespace(
        get_transaction=lambda txid: deepcopy(TX if transaction is None else transaction)))
    monkeypatch.setattr(module, "strict_code_evolution_receipt_error", lambda *a, **kw: strict_error)
    record = SimpleNamespace(scope=ScopeRef.from_dict(SCOPE))
    return SimpleNamespace(store=SimpleNamespace(get_by_id=lambda rid, **kw: record))


@pytest.mark.parametrize("mutation,reason", [
    ({"strict_transaction": False}, "strict_deployment_receipt_required"),
    ({"transaction_id": "other"}, "strict_deployment_receipt_required"),
    ({"commit": "c" * 40}, "pre_observation_transaction_mismatch"),
])
def test_pre_observation_rejects_unbound_receipts(monkeypatch, mutation, reason):
    receipt = {**RECEIPT, **mutation}
    result = module.run_pre_observation_closure(_runtime(monkeypatch), receipt=receipt, transaction_id="tx",
                                              identity_kwargs=dict(scope=SCOPE, repo_root="/repo"))
    assert result["ok"] is False
    assert result["blocked_reason"] == reason


@pytest.mark.parametrize("mutation", [
    {"current_state": "RECOVERY_QUARANTINED"}, {"terminal": True}, {"user_id": "other"}, {"profile_key": ""},
])
def test_pre_observation_rejects_terminal_cross_scope_or_unprofiled_transaction(monkeypatch, mutation):
    result = module.run_pre_observation_closure(_runtime(monkeypatch, transaction={**TX, **mutation}),
        receipt=RECEIPT, transaction_id="tx", identity_kwargs=dict(scope=SCOPE, repo_root="/repo"))
    assert result["blocked_reason"] == "pre_observation_transaction_mismatch"


def test_pre_observation_revalidates_durable_strict_authority(monkeypatch):
    result = module.run_pre_observation_closure(_runtime(monkeypatch, strict_error="ledger_invalid"),
        receipt=RECEIPT, transaction_id="tx", identity_kwargs=dict(scope=SCOPE, repo_root="/repo"))
    assert result["blocked_reason"] == "ledger_invalid"


def test_profile_replay_budget_respects_each_capability_threshold():
    replay = {"selection_contract": {"mode": "dynamic_profile", "profile_key": "p",
        "capabilities": ["custom.one", "custom.two"], "minimums_by_capability": {
            "custom.one": {"minimum_executed": 5, "minimum_distinct_evidence": 4},
            "custom.two": {"minimum_executed": 2, "minimum_distinct_evidence": 2}},
        "expected_case_ids": {"custom.one": ["a", "b"], "custom.two": ["c"]}}}
    assert module._replay_round_count(replay, "p") == 3
    with pytest.raises(ValueError):
        module._replay_round_count(replay, "wrong")
    replay["selection_contract"]["minimums_by_capability"]["custom.two"]["minimum_executed"] = 1000
    with pytest.raises(ValueError, match="budget"):
        module._replay_round_count(replay, "p")


def _pending_readiness():
    gaps = ["terminal_receipt_unbound", "transaction_evidence_unverified", "no_qualifying_terminal_receipt",
            "nonterminal_transaction_exists", "observation_not_valid"]
    return dict(schema="l5.reader.v4", schema_version="l5_readiness.v4", report_type="l5_readiness_report",
        reader_mode="v3", profile_key=TX["profile_key"], ok=False, product_l5_complete=False,
        completion_status="incomplete", status="incomplete", control_plane_ok=True, control_plane_status="ready",
        axes=dict(capability_ready=True, adapter_ready=True, deployment_assurance="ready"),
        code_evolution=dict(provider_ready=True, catalog_ready=True, advertisement_fresh=True,
                            transaction_verified=False, current_lineage_compatible=True, gaps=gaps),
        transaction_evidence=dict(**TX, nonterminal=True, quarantined=False), gaps=gaps)


def _full_fixture(monkeypatch, *, transaction=None):
    from eimemory.governance import closure_rehearsal, l5_readiness, l5_reader
    runtime = _runtime(monkeypatch, transaction=transaction)
    calls = []
    bootstrap = dict(ok=True, capability_replay=dict(selection_contract=dict(mode="dynamic_profile",
        profile_key=TX["profile_key"], capabilities=["custom.capability"],
        minimums_by_capability={"custom.capability": dict(minimum_executed=2, minimum_distinct_evidence=2)},
        expected_case_ids={"custom.capability": ["case"]})))
    rehearsal = dict(ok=False, blocked_reasons=["l5_readiness_not_l5"], skill_call=dict(ok=True),
                     rollback=dict(ok=True), capability_dashboard=dict(ok=True))
    live = dict(ok=True, case_count=10, pass_count=10, fail_count=0, distinct_task_types=10,
                deployment=dict(commit=RECEIPT["commit"], release_path="/release", promotion_request_id="receipt"),
                cases=[dict(record_id=f"case-{i}") for i in range(10)])
    lineage = dict(ok=True, validated=True, compatible=True,
                   current_release=dict(commit=RECEIPT["commit"], receipt_id="receipt", session_id="receipt"))
    readiness = _pending_readiness()
    if transaction is not None:
        readiness["transaction_evidence"].update(transaction)
    monkeypatch.setattr(l5_readiness, "_storage_migration_status", lambda runtime: dict(ok=True))
    monkeypatch.setattr(closure_rehearsal, "run_capability_replay_gate", lambda runtime, **kw: calls.append(("replay", kw)) or deepcopy(bootstrap))
    monkeypatch.setattr(closure_rehearsal, "run_l5_closure_rehearsal", lambda runtime, **kw: calls.append(("rehearsal", kw)) or deepcopy(rehearsal))
    monkeypatch.setattr(module, "_current_replay_cohort", lambda *args: dict(ok=True, manifest_record_ids=["m1", "m2"]))
    monkeypatch.setattr(l5_reader, "build_l5_effective_report", lambda *args, **kw: deepcopy(readiness))
    runtime.run_live_task_acceptance = lambda **kw: deepcopy(live)
    runtime.record_release_lineage = lambda **kw: calls.append(("lineage", kw)) or deepcopy(lineage)
    return runtime, calls, rehearsal, live, lineage, readiness


def test_known_maintenance_admission_survives_independent_installer_recheck(monkeypatch):
    transaction = dict(TX, origin="user_reported", known_before_detection=1, prior_user_reported=1)
    runtime, _, _, _, _, readiness = _full_fixture(monkeypatch, transaction=transaction)
    readiness["transaction_evidence"].update(known_before_detection=True, prior_user_reported=True)
    readiness["gaps"].extend([
        "incident_known_before_system_detection", "incident_not_system_originated",
        "incident_prior_knowledge_unproven", "incident_not_user_reported_unproven",
    ])
    result = module.run_pre_observation_closure(runtime, receipt=RECEIPT, transaction_id="tx",
        identity_kwargs=dict(scope=SCOPE, repo_root="/repo"))
    assert result["ok"] is True
    assert result["transaction"]["origin"] == "user_reported"
    assert result["transaction"]["known_before_detection"] is True
    assert result["transaction"]["prior_user_reported"] is True
    assert result["transaction"]["manual_bootstrap"] is False
    assert result["closure_complete"] is False
    assert result["readiness"]["product_l5_complete"] is False
    assert module.pre_observation_report_ok(result) is True
    for field, value in (("origin", "system_detector"), ("known_before_detection", False),
                         ("manual_bootstrap", True), ("candidate_commit", "c" * 40)):
        tampered = deepcopy(result)
        tampered["transaction"][field] = value
        assert module.pre_observation_report_ok(tampered) is False


def test_strict_admission_runs_dynamic_checks_without_claiming_l5_or_starting_clock(monkeypatch):
    runtime, calls, *_ = _full_fixture(monkeypatch)
    result = module.run_pre_observation_closure(runtime, receipt=RECEIPT, transaction_id="tx",
        identity_kwargs=dict(scope=SCOPE, repo_root="/repo"))
    assert result["ok"] is True
    assert result["status"] == "ready_for_observation"
    assert result["closure_complete"] is False
    assert result["data_accumulating"] is False
    assert result["observation_started"] is False
    assert result["readiness"]["product_l5_complete"] is False
    assert module.pre_observation_report_ok(result)
    assert [name for name, _ in calls] == ["replay", "replay", "rehearsal", "lineage"]
    assert all(kw["profile_key"] == "custom.profile" for name, kw in calls if name != "lineage")
    assert calls[2][1]["correction_capability_id"] == "custom.capability"
    gates = calls[-1][1]["gate_evidence"]
    assert gates["code.evolution"] == gates["deployment.runtime"] == ["receipt"]
    assert gates["memory.governance"] == ["m1", "m2"]
    from deploy.summarize_release_closure import _release_closure_summary_contract_ok, summarize_release_closure
    assert _release_closure_summary_contract_ok(result, summarize_release_closure(result))
    for key in ("closure_complete", "observation_started", "data_accumulating"):
        tampered = {**result, key: True}
        assert not module.pre_observation_report_ok(tampered)


@pytest.mark.parametrize("failure,stage", [
    ("skill", "closure_rehearsal"), ("rollback", "closure_rehearsal"),
    ("earlier_rehearsal", "closure_rehearsal"), ("lineage", "release_lineage"),
    ("live", "live_acceptance"), ("capability", "readiness"), ("extra_gap", "readiness"),
    ("wrong_transaction", "readiness"), ("premature_l5", "readiness"),
])
def test_strict_admission_never_softens_non_observation_failures(monkeypatch, failure, stage):
    runtime, _, rehearsal, live, lineage, readiness = _full_fixture(monkeypatch)
    if failure == "skill":
        rehearsal["skill_call"]["ok"] = False
    elif failure == "rollback":
        rehearsal["rollback"]["ok"] = False
    elif failure == "earlier_rehearsal":
        rehearsal["blocked_reasons"] = ["capability_replay_invalid"]
    elif failure == "lineage":
        lineage["compatible"] = False
    elif failure == "live":
        live["pass_count"] = 9
    elif failure == "capability":
        readiness["axes"]["capability_ready"] = False
    elif failure == "extra_gap":
        readiness["gaps"].append("current_lineage_incompatible")
    elif failure == "wrong_transaction":
        readiness["transaction_evidence"]["transaction_id"] = "other"
    elif failure == "premature_l5":
        readiness["product_l5_complete"] = True
    result = module.run_pre_observation_closure(runtime, receipt=RECEIPT, transaction_id="tx",
        identity_kwargs=dict(scope=SCOPE, repo_root="/repo"))
    assert result["ok"] is False
    assert result["blocked_stage"] == stage
    assert not module.pre_observation_report_ok(result)
