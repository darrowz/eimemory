from __future__ import annotations

import pytest

from eimemory.api.runtime import Runtime
from eimemory.capabilities import (
    CapabilityBinding,
    CapabilityDefinition,
    CapabilityProfile,
    CapabilityRevision,
)
from eimemory.evaluation.capability_catalog import (
    CapabilityEvaluationCatalog,
    CatalogCase,
    CatalogResolutionError,
)


SCOPE = {
    "tenant_id": "tenant-dynamic-eval",
    "agent_id": "agent-dynamic-eval",
    "workspace_id": "workspace-dynamic-eval",
    "user_id": "user-dynamic-eval",
}
STAMP = "2020-08-20T00:00:00Z"


def _definition() -> CapabilityDefinition:
    return CapabilityDefinition(
        capability_id="dynamic.catalog",
        display_name="Dynamic Catalog",
        description="A runtime-registered evaluation target.",
        owner="governance",
        created_at=STAMP,
        provenance={"source": "dynamic-eval-test"},
    )


def _revision(definition: CapabilityDefinition, *, revision_id: str = "dynamic.catalog:v1") -> CapabilityRevision:
    return CapabilityRevision(
        revision_id=revision_id,
        capability_id=definition.capability_id,
        contract={
            "input_schema": {"type": "object", "required": ["request"]},
            "output_schema": {"type": "object", "required": ["decision"]},
            "success_invariants": ["decision_is_traceable"],
            "failure_invariants": ["blocked_input"],
            "evidence_requirements": {"minimum_refs": 1},
            "dependencies": [],
            "composition": [],
            "risk_tier": "low",
            "side_effect_class": "none",
        },
        compatibility="incompatible",
        created_at=STAMP,
        provenance={"source": "dynamic-eval-test"},
    )


def _binding(definition: CapabilityDefinition, revision: CapabilityRevision, *, binding_id: str = "binding.dynamic.catalog:v1") -> CapabilityBinding:
    return CapabilityBinding(
        binding_id=binding_id,
        capability_id=definition.capability_id,
        capability_revision_id=revision.revision_id,
        provider_kind="module",
        provider_instance_id="dynamic-local",
        implementation_digest="a" * 64,
        operations=("evaluate",),
        limits={"max_requests": 8},
        environment_fingerprint={"runtime": "test"},
        applicability={"scope": "global"},
        advertisement_evidence_refs=("artifact://dynamic/catalog-advertisement.json",),
        provenance={"source": "dynamic-eval-test"},
        created_at=STAMP,
    )


def _profile(definition: CapabilityDefinition) -> CapabilityProfile:
    return CapabilityProfile(
        profile_id="profile.dynamic.catalog:v1",
        profile_key="profile.dynamic.catalog",
        requirements={definition.capability_id: {"minimum_maturity": "evaluated"}},
        provenance={"source": "dynamic-eval-test"},
        created_at=STAMP,
    )


def _catalog() -> CapabilityEvaluationCatalog:
    catalog = CapabilityEvaluationCatalog()
    catalog.register_executor(
        executor_id="eimemory.eval.dynamic-catalog",
        revision="v1",
        handler=lambda _input, _fixture, _runtime: {"decision": "traceable", "evidence_count": 1},
    )
    catalog.register_case(
        CatalogCase(
            case_id="dynamic_catalog_contract",
            capability_id="dynamic.catalog",
            executor_id="eimemory.eval.dynamic-catalog",
            input_data={"request": "rehearse"},
            fixture={"fixture_id": "dynamic-catalog-v1"},
            expected_invariants=[
                {"field": "decision", "op": "eq", "value": "traceable"},
                {"field": "evidence_count", "op": "min", "value": 1},
            ],
            binding_selector={"operations_all": ["evaluate"]},
        )
    )
    return catalog


def test_catalog_rejects_executable_selector_dsl() -> None:
    with pytest.raises(CatalogResolutionError, match="executable key"):
        CatalogCase(
            case_id="unsafe_selector",
            capability_id="dynamic.catalog",
            executor_id="eimemory.eval.dynamic-catalog",
            input_data={"request": "rehearse"},
            fixture={"fixture_id": "unsafe"},
            expected_invariants=[{"field": "decision", "op": "nonempty"}],
            binding_selector={"command": "should-not-run"},
        )


def test_catalog_execution_rejects_tampered_artifact() -> None:
    catalog = _catalog()
    artifact = catalog.case_artifact("dynamic_catalog_contract")
    artifact["input"]["request"] = "forged"

    result = catalog.execute(artifact, runtime=None, evidence_ref="probe.dynamic.tamper")

    assert result["passed"] is False
    assert result["verdict"] == "blocked"
    assert "tampered" in result["error"]


def test_profile_backed_catalog_evaluation_persists_spec_and_run(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    definition = _definition()
    revision = _revision(definition)
    binding = _binding(definition, revision)
    profile = _profile(definition)
    catalog = _catalog()
    try:
        runtime.capabilities.register_definition(definition, runtime_scope=SCOPE)
        runtime.capabilities.register_revision(revision, runtime_scope=SCOPE)
        runtime.capabilities.bind(binding, runtime_scope=SCOPE)
        runtime.capabilities.register_profile(profile, runtime_scope=SCOPE)

        report = runtime.run_capability_acceptance(
            scope=SCOPE,
            persist=True,
            catalog=catalog,
            profile_key=profile.profile_key,
            capability_scope="global",
            runtime_scope=SCOPE,
        )
        replay = runtime.build_capability_replay_packs(
            scope=SCOPE,
            persist=True,
            catalog=catalog,
            profile_key=profile.profile_key,
            capability_scope="global",
            runtime_scope=SCOPE,
            acceptance_execution_id=report["execution_id"],
            acceptance_probe_ids_by_case={
                item["case_id"]: item["probe_id"]
                for item in report["results"]
            },
        )
    finally:
        runtime.close()

    assert report["ok"] is True
    assert report["case_count"] == 1
    assert report["results"][0]["evaluation_spec_id"]
    assert report["results"][0]["evaluation_run_id"]
    assert report["results"][0]["capability_revision_id"] == revision.revision_id
    assert report["results"][0]["provider_binding_id"] == binding.binding_id
    assert replay["ok"] is True
    assert replay["legacy_compatibility"] is False
    assert replay["capabilities"] == [definition.capability_id]
    assert replay["packs"][0]["case_results"][0]["verdict"] == "pass"


def test_profile_backed_evaluation_fails_closed_for_multiple_bindings(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    definition = _definition()
    revision = _revision(definition)
    profile = _profile(definition)
    try:
        runtime.capabilities.register_definition(definition, runtime_scope=SCOPE)
        runtime.capabilities.register_revision(revision, runtime_scope=SCOPE)
        runtime.capabilities.bind(_binding(definition, revision), runtime_scope=SCOPE)
        runtime.capabilities.bind(
            _binding(definition, revision, binding_id="binding.dynamic.catalog:v2"),
            runtime_scope=SCOPE,
        )
        runtime.capabilities.register_profile(profile, runtime_scope=SCOPE)

        report = runtime.run_capability_acceptance(
            scope=SCOPE,
            persist=True,
            catalog=_catalog(),
            profile_key=profile.profile_key,
            capability_scope="global",
            runtime_scope=SCOPE,
        )
    finally:
        runtime.close()

    assert report["ok"] is False
    assert report["status"] == "rejected"
    assert report["blocked_reasons"] == ["profile_evaluation_selection_blocked"]
    assert any("ambiguous_or_missing_binding" in value for value in report["dynamic_selection"]["errors"])
