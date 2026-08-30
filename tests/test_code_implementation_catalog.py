from __future__ import annotations

from copy import deepcopy
from pathlib import Path

import eimemory.evaluation.hongtu_code_implementation as catalog_module
from eimemory.api.runtime import Runtime
from eimemory.capabilities import CapabilityBinding, CapabilityDefinition, CapabilityRevision
from eimemory.adapters.hermes.code_implementation import OPERATION, build_attestation
from eimemory.evaluation.hongtu_code_implementation import (
    CATALOG_CASE_ID,
    evaluate_code_implementation,
    evaluate_static_code_implementation_proposal,
    install_code_implementation_catalog,
    run_code_implementation_catalog_pass,
)
from eimemory.evaluation.capability_catalog import (
    ApplicationCatalogBootstrap,
    CapabilityEvaluationCatalog,
)
from eimemory.governance.capability_probe_executor import (
    execute_probe,
    validate_execution_evidence,
)


class _Provider:
    last_timeout_seconds = 0.0

    def __init__(self, *, timeout_seconds: float = 0.0) -> None:
        type(self).last_timeout_seconds = float(timeout_seconds)

    def propose_patch_v2(self, request):
        response = {
            "schema": "code_implementation_response.v2",
            "request_id": request["request_id"],
            "request_digest": request["request_digest"],
            "file_updates": [
                {
                    "path": request["allowed_files"][0]["path"],
                    "prior_sha256": request["allowed_files"][0]["sha256"],
                    "content": "VALUE = 2\n",
                }
            ],
            "rationale": "bounded fixture repair",
            "assumptions": [],
        }
        return {
            "ok": True,
            "operation": OPERATION,
            "attestation": build_attestation(
                request,
                response,
                completed_at="2026-08-23T00:00:00Z",
                nonce=request["nonce"],
            ),
            "response": response,
        }


def test_catalog_pass_proves_live_provider_did_not_write_fixture(tmp_path: Path) -> None:
    fixture = tmp_path / "fixture.py"
    fixture.write_text("VALUE = 1\n", encoding="utf-8")
    before = fixture.read_bytes()

    result = run_code_implementation_catalog_pass(
        provider=_Provider(),
        fixture_root=tmp_path,
        fixture_files=("fixture.py",),
        evaluator=lambda root, updates: root.joinpath("fixture.py").read_text(encoding="utf-8") == "VALUE = 2\n",
        now="2026-08-23T00:00:00Z",
    )

    assert result["ok"] is True
    assert result["provider_invoked"] is True
    assert result["fixture_before_digest"] == result["fixture_after_digest"]
    assert fixture.read_bytes() == before
    assert result["evaluation"]["ok"] is True
    assert result["receipt_digest"]


def test_catalog_pass_fails_when_provider_mutates_source_fixture(tmp_path: Path) -> None:
    fixture = tmp_path / "fixture.py"
    fixture.write_text("VALUE = 1\n", encoding="utf-8")

    class MutatingProvider(_Provider):
        def propose_patch_v2(self, request):
            fixture.write_text("tampered\n", encoding="utf-8")
            return super().propose_patch_v2(request)

    result = run_code_implementation_catalog_pass(
        provider=MutatingProvider(),
        fixture_root=tmp_path,
        fixture_files=("fixture.py",),
        evaluator=lambda *_args: True,
        now="2026-08-23T00:00:00Z",
    )

    assert result["ok"] is False
    assert result["reason"] == "provider_mutated_fixture"


def test_catalog_pass_rejects_unattested_provider_response(tmp_path: Path) -> None:
    fixture = tmp_path / "fixture.py"
    fixture.write_text("VALUE = 1\n", encoding="utf-8")

    class BareProvider(_Provider):
        def propose_patch_v2(self, request):
            return super().propose_patch_v2(request)["response"]

    result = run_code_implementation_catalog_pass(
        provider=BareProvider(),
        fixture_root=tmp_path,
        fixture_files=("fixture.py",),
        evaluator=evaluate_static_code_implementation_proposal,
        now="2026-08-23T00:00:00Z",
    )

    assert result["ok"] is False
    assert result["reason"] == "provider_attestation_invalid"


def test_catalog_pass_detects_unlisted_fixture_mutation(tmp_path: Path) -> None:
    fixture = tmp_path / "fixture.py"
    fixture.write_text("VALUE = 1\n", encoding="utf-8")

    class AddingProvider(_Provider):
        def propose_patch_v2(self, request):
            (tmp_path / "unlisted.py").write_text("tampered = True\n", encoding="utf-8")
            return super().propose_patch_v2(request)

    result = run_code_implementation_catalog_pass(
        provider=AddingProvider(),
        fixture_root=tmp_path,
        fixture_files=("fixture.py",),
        evaluator=evaluate_static_code_implementation_proposal,
        now="2026-08-23T00:00:00Z",
    )

    assert result["ok"] is False
    assert result["reason"] == "provider_mutated_fixture"


def test_static_catalog_evaluator_rejects_invalid_python(tmp_path: Path) -> None:
    path = tmp_path / "fixture.py"
    path.write_text("VALUE =\n", encoding="utf-8")

    result = evaluate_static_code_implementation_proposal(
        tmp_path,
        [{"path": "fixture.py", "content": "VALUE =\n"}],
    )

    assert result["ok"] is False
    assert result["syntax_valid"] is False


def test_catalog_executor_uses_a_fresh_socket_client_and_sealed_fixture(monkeypatch) -> None:
    monkeypatch.setattr(catalog_module, "CodeImplementationSocketClient", _Provider)

    result = evaluate_code_implementation(
        {"operation": OPERATION},
        {
            "fixture_schema": "code_implementation_static_fixture.v1",
            "fixture_sha256": catalog_module._STATIC_FIXTURE_DIGEST,
        },
        object(),
    )

    assert result["execution_ok"] is True
    assert result["provider_ready"] is True
    assert _Provider.last_timeout_seconds == 125.0
    assert len(result["receipt_digest"]) == 64
    assert len(result["provider_attestation_digest"]) == 64
    assert result["receipt"]["implementation_digest"]


def test_recorded_code_implementation_evidence_validates_without_provider_reexecution(
    monkeypatch,
) -> None:
    catalog = CapabilityEvaluationCatalog()
    install_code_implementation_catalog(ApplicationCatalogBootstrap(catalog))
    catalog.seal()
    artifact = catalog.case_artifact(CATALOG_CASE_ID)
    monkeypatch.setattr(catalog_module, "CodeImplementationSocketClient", _Provider)
    evidence = execute_probe(
        artifact,
        runtime=object(),
        evidence_ref="recorded-provider-run",
        catalog=catalog,
    )
    assert evidence["passed"] is True

    class ReexecutionForbidden:
        def __init__(self, **_kwargs) -> None:
            raise AssertionError("recorded evidence must not invoke the provider")

    monkeypatch.setattr(
        catalog_module,
        "CodeImplementationSocketClient",
        ReexecutionForbidden,
    )
    assert validate_execution_evidence(
        artifact,
        runtime=object(),
        evidence_ref="recorded-provider-run",
        evidence=evidence,
        catalog=catalog,
    ) == ""

    tampered = deepcopy(evidence)
    tampered["output"]["receipt_digest"] = "0" * 64
    assert validate_execution_evidence(
        artifact,
        runtime=object(),
        evidence_ref="recorded-provider-run",
        evidence=tampered,
        catalog=catalog,
    ) == "recorded_provider_receipt_invalid"

    tampered = deepcopy(evidence)
    tampered["grader_revision"] = "forged"
    assert validate_execution_evidence(
        artifact,
        runtime=object(),
        evidence_ref="recorded-provider-run",
        evidence=tampered,
        catalog=catalog,
    ) == "recorded_grader_revision_mismatch"


def test_persisted_code_implementation_probe_replays_with_complete_execution_evidence(
    tmp_path, monkeypatch,
) -> None:
    scope = {"tenant_id": "test", "agent_id": "test", "workspace_id": "test", "user_id": "test"}
    stamp = "2020-01-01T00:00:00Z"
    provenance = {"source": "persisted-replay-test"}
    catalog = CapabilityEvaluationCatalog()
    install_code_implementation_catalog(ApplicationCatalogBootstrap(catalog))
    catalog.seal()
    monkeypatch.setattr(catalog_module, "CodeImplementationSocketClient", _Provider)
    runtime = Runtime.create(root=tmp_path)
    try:
        runtime.capabilities.register_definition(CapabilityDefinition(
            capability_id=catalog_module.CAPABILITY_ID, display_name="Code implementation",
            description="Persisted replay fixture", owner="test", created_at=stamp,
            provenance=provenance,
        ), runtime_scope=scope)
        runtime.capabilities.register_revision(CapabilityRevision(
            revision_id=catalog_module.REVISION_ID, capability_id=catalog_module.CAPABILITY_ID,
            contract={
                "input_schema": {"type": "object"}, "output_schema": {"type": "object"},
                "success_invariants": ["proposal_verified"], "failure_invariants": ["invalid_proposal"],
                "evidence_requirements": {"minimum_refs": 1}, "dependencies": [], "composition": [],
                "risk_tier": "low", "side_effect_class": "none",
            }, compatibility="incompatible", created_at=stamp, provenance=provenance,
        ), runtime_scope=scope)
        runtime.capabilities.bind(CapabilityBinding(
            binding_id=catalog_module.BINDING_ID, capability_id=catalog_module.CAPABILITY_ID,
            capability_revision_id=catalog_module.REVISION_ID, provider_kind="module",
            provider_instance_id="test", implementation_digest="a" * 64,
            operations=(OPERATION,), limits={"max_requests": 1}, environment_fingerprint={"runtime": "test"},
            applicability={"scope": "global"}, advertisement_evidence_refs=("artifact://test/provider",),
            created_at=stamp, provenance=provenance,
        ), runtime_scope=scope)
        acceptance = runtime.run_capability_acceptance(
            scope=scope, persist=True, catalog=catalog, case_ids=[CATALOG_CASE_ID],
        )
        assert acceptance["ok"] is True, acceptance

        class ReexecutionForbidden:
            def __init__(self, **_kwargs):
                raise AssertionError("persisted replay must not reexecute the provider")

        monkeypatch.setattr(catalog_module, "CodeImplementationSocketClient", ReexecutionForbidden)
        replay = runtime.build_capability_replay_packs(
            scope=scope, persist=True, catalog=catalog, capabilities=[catalog_module.CAPABILITY_ID],
            acceptance_execution_id=acceptance["execution_id"],
            acceptance_probe_ids_by_case={item["case_id"]: item["probe_id"] for item in acceptance["results"]},
        )
        assert replay["ok"] is True, replay
        case = replay["packs"][0]["case_results"][0]
        assert case["verdict"] == "pass", case
    finally:
        runtime.close()
