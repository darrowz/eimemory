"""Trusted failed assertions must reach the ledger, without granting success."""
from copy import deepcopy
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from eimemory.api.runtime import Runtime
from eimemory.capabilities.projector import CapabilityStateProjector
from eimemory.evaluation.capability_catalog import CapabilityEvaluationCatalog, CatalogResolutionError
from eimemory.experience.capability_contract import validate_capability_contract
from eimemory.experience.outcome import build_outcome_trace_record, OutcomeTraceBuildError
from eimemory.governance.capability.capability_replay_executor import execute_capability_replay_case
from test_dynamic_capability_evals import SCOPE, _definition, _revision, _binding, _profile, _catalog


@pytest.fixture
def native_case(tmp_path):
    runtime = Runtime.create(root=tmp_path)
    definition = _definition()
    revision = _revision(definition)
    binding = _binding(definition, revision)
    profile = _profile(definition)
    for register, value in (
        (runtime.capabilities.register_definition, definition),
        (runtime.capabilities.register_revision, revision),
        (runtime.capabilities.bind, binding),
        (runtime.capabilities.register_profile, profile),
    ):
        register(value, runtime_scope=SCOPE)
    state = {"decision": "traceable", "mode": "business"}

    def execute(_input, _fixture, _runtime):
        if state["mode"] == "raise":
            raise RuntimeError("unavailable")
        if state["mode"] == "invalid":
            return None
        return {"decision": state["decision"], "evidence_count": 1}

    catalog = CapabilityEvaluationCatalog()
    catalog.register_executor(executor_id="eimemory.eval.dynamic-catalog", revision="v1", handler=execute)
    catalog.register_cases(_catalog().list_cases())

    def run():
        return runtime.run_capability_acceptance(scope=SCOPE, runtime_scope=SCOPE, persist=True,
            catalog=catalog, profile_key=profile.profile_key, capability_scope="global")

    def project():
        return CapabilityStateProjector(runtime.store).project(profile.profile_key,
            runtime_scope=SCOPE, capability_scope="global", persist=False,
            at_time=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")).to_dict()

    yield SimpleNamespace(runtime=runtime, catalog=catalog, state=state, run=run, project=project)
    runtime.close()


def test_business_failure_is_durable_and_regresses_previously_reliable_state(native_case):
    case = native_case
    assert case.run()["ok"] is True
    assert case.project()["snapshots"][0]["maturity"] == "reliable"
    case.state["decision"] = "wrong"
    failed = case.run()
    result = failed["results"][0]
    assert failed["ok"] is False and result["passed"] is False
    assert result["trace_record_id"] and result["evaluation_run_id"]
    trace = case.runtime.store.get_by_id(result["trace_record_id"], scope=SCOPE)
    payload = trace.content["payload"]
    assert payload["verifier"]["independent"] is True
    assert payload["verifier"]["passed"] is False and payload["outcome"]["success"] is False
    # Existing authorization consumers remain strict.
    assert validate_capability_contract(payload["capability_contract"], catalog=case.catalog)
    projected = case.project()
    assert projected["snapshots"][0]["maturity"] == "regressed"
    assert projected["blocked"][0]["actual_maturity"] == "regressed"
    assert result["trace_record_id"] in projected["snapshots"][0]["evidence_refs"]
    case.state["decision"] = "traceable"
    assert case.run()["ok"] is True
    assert case.project()["snapshots"][0]["maturity"] == "reliable"


def test_failed_trace_replay_remains_failure_and_reappend_is_idempotent(native_case):
    case = native_case
    case.state["decision"] = "wrong"
    report = case.run()
    result = report["results"][0]
    trace = case.runtime.store.get_by_id(result["trace_record_id"], scope=SCOPE)
    replayed = case.runtime.record_outcome_trace(deepcopy(trace.content["payload"]), scope=SCOPE, catalog=case.catalog)
    assert replayed["ok"] is True and replayed["idempotent"] is True
    assert replayed["record_id"] == trace.record_id
    replay = execute_capability_replay_case(case.runtime, {
        "case_id": result["case_id"], "target_capability": result["capability"], "scope": SCOPE,
        "capability_revision_id": result["capability_revision_id"],
        "acceptance_execution_id": report["execution_id"], "required_probe_source_id": result["probe_id"],
    }, catalog=case.catalog)
    assert replay["verdict"] == "fail", replay
    assert replay["reason"] == "verified_rehearsal_outcome_required", replay


@pytest.mark.parametrize("mode", ["raise", "invalid"])
def test_transport_or_invalid_output_cannot_masquerade_as_business_failure(native_case, mode):
    case = native_case
    case.state["mode"] = mode
    result = case.run()["results"][0]
    assert result["passed"] is False
    assert result["trace_record_id"] == result["evaluation_run_id"] == ""
    assert not case.project()["snapshots"]


@pytest.mark.parametrize("mutation", ["flip_check", "omit_check", "wrong_case", "wrong_probe",
                                      "missing_field", "claim_success", "not_independent"])
def test_malformed_negative_evidence_cannot_become_authoritative(native_case, mutation):
    case = native_case
    case.state["decision"] = "wrong"
    result = case.run()["results"][0]
    trace = case.runtime.store.get_by_id(result["trace_record_id"], scope=SCOPE)
    payload = deepcopy(trace.content["payload"])
    contract = payload["capability_contract"]
    if mutation == "flip_check":
        contract["checks"][0]["passed"] = True
    elif mutation == "omit_check":
        contract["checks"].pop()
    elif mutation == "wrong_case":
        contract["evaluation_case_digest"] = "f" * 64
    elif mutation == "wrong_probe":
        contract["checks"][0]["evidence_ref"] = "unrelated-probe"
    elif mutation == "missing_field":
        contract["observations"].pop("evidence_count")
    elif mutation == "claim_success":
        payload["outcome"].update(status="success", success=True)
        payload["verifier"]["passed"] = True
    elif mutation == "not_independent":
        payload["verifier"]["independent"] = False
    with pytest.raises(OutcomeTraceBuildError):
        build_outcome_trace_record(payload, scope=SCOPE, catalog=case.catalog)


def test_failed_execution_cannot_be_rebound_to_another_scope(native_case):
    case = native_case
    case.state["decision"] = "wrong"
    report = case.run()
    result = report["results"][0]
    selection = case.catalog.resolve_profile_cases(case.runtime, profile_key="profile.dynamic.catalog",
        runtime_scope=SCOPE, capability_scope="global")
    selected = selection["cases"][0]
    with pytest.raises(CatalogResolutionError):
        case.catalog.execute_and_persist(case.runtime, artifact=selected["artifact"], target=selected["target"],
            runtime_scope={**SCOPE, "user_id": "another-user"}, capability_scope="global",
            evidence_ref=result["probe_id"], probe_id=result["probe_id"],
            trace_record_id=result["trace_record_id"], execution_id=report["execution_id"])
