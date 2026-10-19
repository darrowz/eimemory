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


@pytest.mark.parametrize("damage", ["", "revoked", "wrong_owner", "partial_contract"])
def test_pinned_code_provider_profile_keeps_acceptance_and_manifest_owned_by_business(tmp_path, monkeypatch, damage):
    """Real Profile/catalog/SQLite evidence; the root-file reader is a fixture.

    This tests delegation semantics, not host root ownership or a production
    model. Those filesystem boundaries have separate native tests.
    """
    from copy import deepcopy
    from dataclasses import replace
    from eimemory.governance.evolution import code_evolution_provider_authority as authority
    from eimemory.governance.capability.provider_selection import verify_selection_authority
    from eimemory.models.records import ScopeRef
    from eimemory.governance.capability.capability_replay_packs import replay_selection_contract_digest
    runtime = Runtime.create(root=tmp_path / "isolated")
    owner = {**SCOPE, "workspace_id": "channel::business", "user_id": "business-openid"}
    provider = dict(SCOPE)
    definition = replace(_definition(), capability_id="code.implementation")
    revision = _revision(definition)
    profile = _profile(definition)
    catalog = CapabilityEvaluationCatalog()
    catalog.register_executor(executor_id="fixture.code-eval", revision="v1",
        handler=lambda _i, _f, _r: {"decision": "traceable"})
    catalog.register_case(CatalogCase(case_id="fixture_code_contract", capability_id=definition.capability_id,
        executor_id="fixture.code-eval", input_data={"request": "verify"}, fixture={"id": "isolated"},
        expected_invariants=[{"field": "decision", "op": "eq", "value": "traceable"}],
        binding_selector={"operations_all": ["evaluate"]}))
    runtime.capabilities.register_definition(definition, runtime_scope=provider)
    runtime.capabilities.register_revision(revision, runtime_scope=provider)
    runtime.capabilities.bind(_binding(definition, revision), runtime_scope=provider)
    runtime.capabilities.register_profile(profile, runtime_scope=provider)
    profile_digest = runtime.capabilities.resolve_profile(profile.profile_key, runtime_scope=provider, capability_scope="global")["profile"]["profile_digest"]
    entry = dict(owner_scope=owner, provider_scope=provider, capability_id=authority.CAPABILITY_ID,
        capability_scope="global", operation=authority.OPERATION, profile_key=profile.profile_key, profile_digest=profile_digest)
    mapping = dict(schema=authority.DELEGATION_SCHEMA, enabled=True, delegations=[entry])
    monkeypatch.setattr(authority, "_secure_delegations", lambda: deepcopy(mapping))
    monkeypatch.setattr(authority, "configured_provider_scope", lambda _owner: provider)
    snapshot = authority.select_provider_authority(runtime, owner_scope=owner, profile_key=profile.profile_key)
    if damage == "revoked":
        mapping["delegations"] = []
    elif damage == "wrong_owner":
        owner = {**owner, "user_id": "other-openid"}
    try:
        acceptance = runtime.run_capability_acceptance(scope=owner, persist=True, catalog=catalog,
            runtime_scope=owner, profile_key=profile.profile_key, provider_authority_snapshot=snapshot)
        if damage in {"revoked", "wrong_owner"}:
            assert acceptance["ok"] is False
            assert runtime.store.list_records(scope=ScopeRef.from_dict(owner), kinds=["replay_result"], limit=50) == []
            return
        assert acceptance["ok"] is True, acceptance
        replay = runtime.build_capability_replay_packs(scope=owner, persist=True, catalog=catalog,
            runtime_scope=owner, profile_key=profile.profile_key, provider_authority_snapshot=snapshot,
            acceptance_execution_id=acceptance["execution_id"],
            acceptance_probe_ids_by_case={row["case_id"]: row["probe_id"] for row in acceptance["results"]})
        assert replay["ok"] is True, replay
        contract = replay["selection_contract"]
        assert acceptance["scope"] == acceptance["runtime_scope"] == contract["runtime_scope"] == owner
        assert contract["provider_scope"] == provider
        assert contract["provider_authority_snapshot"] == snapshot
        manifest = runtime.store.get_by_id(replay["manifest_record_id"], scope=ScopeRef.from_dict(owner))
        assert manifest is not None and manifest.scope == ScopeRef.from_dict(owner)
        assert runtime.store.get_by_id(manifest.record_id, scope=ScopeRef.from_dict(provider)) is None
        verify_selection_authority(runtime, contract, owner_scope=owner)
        if damage == "partial_contract":
            del contract["provider_authority_digest"]
            contract["selection_contract_digest"] = replay_selection_contract_digest(contract)
            with pytest.raises(ValueError, match="incomplete"):
                verify_selection_authority(runtime, contract, owner_scope=owner)
        else:
            mapping["delegations"] = []
            with pytest.raises(ValueError):
                verify_selection_authority(runtime, contract, owner_scope=owner)
    finally:
        runtime.close()
