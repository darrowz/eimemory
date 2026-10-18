from copy import deepcopy
from dataclasses import asdict

import pytest

from eimemory.ops import controlled_rpc_probe as probe
from test_controlled_production_acceptance import OWNER, RELEASE, setup


def client_fixture(setup, monkeypatch, mutate=lambda result, params: None):
    calls = []
    class Client:
        def __init__(self, **kwargs):
            assert kwargs["base_url"] == "http://127.0.0.1:8091/rpc"
            assert kwargs["auth_token"] == "fixture-secret"
        def call(self, method, params):
            assert method == "memory.recall" and params["scope"]["preserve_scope"] is True
            assert params["task_context"]["acceptance_generated"] is True
            calls.append(deepcopy(params))
            result = {"ok": True, "contract_version": probe.EIMEMORY_RPC_CONTRACT_VERSION,
                      "result": setup.recall(**params).to_dict()}
            mutate(result, params)
            return result
    monkeypatch.setattr(probe, "AgentRuntimeRPCClient", Client)
    monkeypatch.setattr(probe, "rpc_probe_token", lambda: "fixture-secret")
    monkeypatch.setattr(probe, "collect_controlled_endpoint_identity", lambda **kw: endpoint_identity())
    return calls


def endpoint_identity():
    return {"schema": "controlled_rpc_release_identity.v1", "ok": True,
        "expected_commit": RELEASE["release_commit"], "version": RELEASE["release_version"],
        "package_tree_digest": "f" * 64, "identity_only": True, "retrieval_health_certified": False}


def run(setup):
    return probe.run_controlled_rpc_probe(setup.runtime, scope=asdict(OWNER),
                                         expected_commit=RELEASE["release_commit"])


def test_actual_rpc_contract_is_exercised_once_per_authorized_scope_without_labels(setup, monkeypatch):
    calls = client_fixture(setup, monkeypatch)
    before = setup.runtime.store.count_records()
    report = run(setup)
    assert report["ok"] and report["pass_count"] == report["sample_count"] == len(calls) == 4, report
    assert {c["scope"]["agent_id"] for c in calls} == {"hongrui", "hongtai", "hongxin", "xiaomage"}
    assert report["natural_samples_created"] == 0 and not report["certifies_natural_queries"]
    assert setup.runtime.store.count_records() == before
    assert report["endpoint_release_verified"] is True
    assert report["endpoint_identity_before"] == report["endpoint_identity_after"] == endpoint_identity()
    assert "fixture-secret" not in str(report) and "green notebook" not in str(report)


@pytest.mark.parametrize("problem", ["wrong_scope", "rewritten_record", "incomplete", "fallback",
    "budget", "no_target", "malformed_response", "source_drift", "only_rules", "only_reflections",
    "over_limit", "wrong_contract", "missing_contract"])
def test_transport_success_cannot_hide_wrong_or_incomplete_results(setup, monkeypatch, problem):
    def mutate(response, params):
        result = response["result"]
        if problem == "wrong_scope": result["items"][0]["scope"]["user_id"] = "foreign"
        if problem == "rewritten_record": result["items"][0]["content"]["text"] = "Invented answer."
        if problem == "incomplete": result["explanation"]["relevance_selector"]["collection_complete"] = False
        if problem == "fallback": result["explanation"]["engine_diagnostics"]["fallback"] = True
        if problem == "budget": result["explanation"]["engine_diagnostics"]["source_budget_exhausted"] = 2
        if problem == "no_target": result["items"] = []
        if problem == "malformed_response": response["result"] = []
        if problem == "only_rules": result["rules"], result["items"] = result["items"], []
        if problem == "only_reflections": result["reflections"], result["items"] = result["items"], []
        if problem == "over_limit": result["items"] *= params["limit"] + 1
        if problem == "wrong_contract": response["contract_version"] = "foreign-contract"
        if problem == "missing_contract": response.pop("contract_version")
        if problem == "source_drift":
            row = deepcopy(next(record for record in setup.memories[params["scope"]["agent_id"]]
                                if record.record_id == result["items"][0]["record_id"]))
            row.content["text"] = "Changed during HTTP request."
            setup.runtime.store.append(row)
    client_fixture(setup, monkeypatch, mutate)
    assert run(setup)["ok"] is False


def test_missing_authentication_does_not_send_a_request(setup, monkeypatch):
    calls = client_fixture(setup, monkeypatch)
    monkeypatch.setattr(probe, "rpc_probe_token", lambda: "")
    result = run(setup)
    assert not result["ok"] and not calls


def test_policy_change_during_rpc_run_is_not_accepted(setup, monkeypatch):
    client_fixture(setup, monkeypatch,
        lambda response, params: setup.policy.update(request_digest="d" * 64))
    assert run(setup)["ok"] is False


def test_wrong_actual_endpoint_release_stops_before_any_recall(setup, monkeypatch):
    calls = client_fixture(setup, monkeypatch)
    monkeypatch.setattr(probe, "collect_controlled_endpoint_identity", lambda **kw: {
        **endpoint_identity(), "ok": False, "expected_commit": "b" * 40})
    result = run(setup)
    assert not result["ok"] and not result["endpoint_release_verified"] and not calls


@pytest.mark.parametrize("change", ["unavailable", "digest", "version"])
def test_endpoint_identity_drift_after_real_requests_rejects(setup, monkeypatch, change):
    calls = client_fixture(setup, monkeypatch)
    after = endpoint_identity()
    if change == "unavailable": after["ok"] = False
    if change == "digest": after["package_tree_digest"] = "a" * 64
    if change == "version": after["version"] = "old-version"
    identities = iter([endpoint_identity(), after])
    monkeypatch.setattr(probe, "collect_controlled_endpoint_identity", lambda **kw: next(identities))
    result = run(setup)
    assert len(calls) == 4 and not result["ok"] and not result["endpoint_release_verified"]


@pytest.mark.parametrize("field,value", [("expected_commit", "b" * 40), ("version", "foreign"),
    ("package_tree_digest", "unknown"), ("identity_only", False), ("retrieval_health_certified", True)])
def test_ok_claim_without_exact_endpoint_proof_never_sends_query(setup, monkeypatch, field, value):
    calls = client_fixture(setup, monkeypatch)
    monkeypatch.setattr(probe, "collect_controlled_endpoint_identity", lambda **kw: {**endpoint_identity(), field: value})
    result = run(setup)
    assert not result["ok"] and not result["endpoint_release_verified"] and not calls
