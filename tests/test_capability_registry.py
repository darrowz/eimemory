from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from eimemory.capabilities.models import (
    CapabilityBinding,
    CapabilityDefinition,
    CapabilityRelation,
    CapabilityRevision,
)
from eimemory.capabilities.registry import CapabilityRegistry, CapabilityRegistryError
from eimemory.api.runtime import Runtime
from eimemory.models.records import ScopeRef
from eimemory.storage.capability_store import CapabilityConflict
from eimemory.storage.runtime_store import RuntimeStore


SCOPE = {
    "tenant_id": "tenant-registry",
    "agent_id": "agent-registry",
    "workspace_id": "workspace-registry",
    "user_id": "user-registry",
}
STAMP = "2020-08-20T00:00:00+00:00"


def _definition(capability_id: str = "planning.dynamic_resolution") -> CapabilityDefinition:
    return CapabilityDefinition(
        capability_id=capability_id,
        display_name=capability_id.replace(".", " ").title(),
        description="A dynamically registered bounded capability.",
        owner="registry-test",
        risk_tier="bounded_read",
        tags=("planning", "dynamic"),
        provenance={"source": "registry-test"},
        created_at=STAMP,
    )


def _revision(definition: CapabilityDefinition, *, name: str = "v1", field: str = "constraints") -> CapabilityRevision:
    return CapabilityRevision(
        revision_id=f"{definition.capability_id}:{name}",
        capability_id=definition.capability_id,
        contract={
            "input_schema": {"type": "object", "required": [field]},
            "output_schema": {"type": "object", "required": ["decision"]},
            "success_invariants": ["decision_traceable"],
            "failure_invariants": ["unsafe_input_blocked"],
            "evidence_requirements": {"minimum_refs": 1},
            "dependencies": [],
            "composition": [],
            "risk_tier": "bounded_read",
            "side_effect_class": "none",
        },
        compatibility="incompatible",
        provenance={"source": "registry-test"},
        created_at=STAMP,
    )


def _binding(definition: CapabilityDefinition, revision: CapabilityRevision, *, name: str = "codex") -> CapabilityBinding:
    return CapabilityBinding(
        binding_id=f"binding.{name}.{definition.capability_id}:v1",
        capability_id=definition.capability_id,
        capability_revision_id=revision.revision_id,
        provider_kind=name,
        provider_instance_id=f"{name}-runtime",
        implementation_digest=("a" if name == "codex" else "b") * 64,
        operations=("resolve",),
        limits={"max_items": 32},
        environment_fingerprint={"runtime": "isolated"},
        applicability={"scope": "global"},
        advertisement_evidence_refs=(f"artifact://{name}/advertisement.json",),
        provenance={"source": "registry-test"},
        created_at=STAMP,
    )


@pytest.fixture()
def registry(tmp_path: Path):
    store = RuntimeStore(tmp_path)
    try:
        yield CapabilityRegistry(store), store
    finally:
        store.close()


def test_runtime_registry_registers_and_resolves_without_fixed_capability_source(registry) -> None:
    capabilities, _store = registry
    definition = _definition()
    revision = _revision(definition)
    binding = _binding(definition, revision)

    first = capabilities.register_definition(definition, runtime_scope=SCOPE, request_key="definition-1")
    repeated = capabilities.register_definition(definition, runtime_scope=SCOPE, request_key="definition-1")
    capabilities.register_revision(revision, runtime_scope=SCOPE, request_key="revision-1")
    capabilities.bind(binding, runtime_scope=SCOPE, request_key="binding-1")

    assert first.idempotent is False
    assert repeated.idempotent is True
    resolved = capabilities.resolve(
        definition.capability_id,
        runtime_scope=SCOPE,
        capability_scope="global",
        operation="resolve",
    )
    assert resolved.ok is True
    assert resolved.reason == "resolved"
    assert resolved.revisions[0]["entity_id"] == revision.revision_id
    assert resolved.bindings[0]["entity_id"] == binding.binding_id

    other_scope = {**SCOPE, "tenant_id": "tenant-other"}
    assert capabilities.resolve(
        definition.capability_id,
        runtime_scope=other_scope,
        capability_scope="global",
    ).reason == "capability_not_found"
    with pytest.raises(CapabilityRegistryError, match="exact tenant"):
        capabilities.list_definitions(runtime_scope={"tenant_id": "x"}, capability_scope="global")
    with pytest.raises(CapabilityRegistryError, match="exact tenant"):
        capabilities.list_definitions(
            runtime_scope={**SCOPE, "untrusted_extra": "must-not-fallback"},
            capability_scope="global",
        )
    with pytest.raises(CapabilityRegistryError, match="exact tenant"):
        capabilities.list_definitions(
            runtime_scope=ScopeRef(tenant_id="tenant", agent_id=1, workspace_id="workspace", user_id="user"),  # type: ignore[arg-type]
            capability_scope="global",
        )
    with pytest.raises(CapabilityConflict, match="future created_at"):
        capabilities.register_definition(
            replace(
                definition,
                capability_id="planning.future_activation",
                created_at="2099-01-01T00:00:00+00:00",
            ),
            runtime_scope=SCOPE,
            request_key="future-definition",
        )


def test_runtime_exposes_opt_in_dynamic_capability_service_without_auto_seed(tmp_path: Path) -> None:
    store = RuntimeStore(tmp_path)
    runtime = Runtime(store)
    try:
        assert runtime.capabilities.list_definitions(
            runtime_scope=SCOPE,
            capability_scope="global",
        ) == []
        definition = _definition("planning.runtime_facade")
        receipt = runtime.capabilities.register_definition(
            definition,
            runtime_scope=SCOPE,
            request_key="runtime-facade-definition",
        )
        assert receipt.entity_id == definition.capability_id
        seeded = runtime.capabilities.apply_seed_manifest(runtime_scope=SCOPE)
        assert seeded.created_count == 22
        assert runtime.capabilities.apply_seed_manifest(runtime_scope=SCOPE).idempotent is True
        status = runtime.capabilities.status()
        assert status["schema"] == "capability.service_status.v1"
        assert status["audit_export"]["pending"] == 0
    finally:
        runtime.close()


def test_registry_fails_closed_for_ambiguous_revisions_but_preserves_multi_provider_bindings(registry) -> None:
    capabilities, _store = registry
    definition = _definition("memory.dynamic_recall")
    revision_v1 = _revision(definition, name="v1", field="query")
    revision_v2 = _revision(definition, name="v2", field="query_v2")
    codex = _binding(definition, revision_v1, name="codex")
    hermes = _binding(definition, revision_v1, name="hermes")
    for entity, key, method in (
        (definition, "definition", capabilities.register_definition),
        (revision_v1, "revision-v1", capabilities.register_revision),
        (revision_v2, "revision-v2", capabilities.register_revision),
        (codex, "binding-codex", capabilities.bind),
        (hermes, "binding-hermes", capabilities.bind),
    ):
        method(entity, runtime_scope=SCOPE, request_key=key)

    ambiguous = capabilities.resolve(
        definition.capability_id,
        runtime_scope=SCOPE,
        capability_scope="global",
    )
    assert ambiguous.ok is False
    assert ambiguous.reason == "ambiguous_active_revisions"
    assert {item["entity_id"] for item in ambiguous.revisions} == {revision_v1.revision_id, revision_v2.revision_id}

    selected = capabilities.resolve(
        definition.capability_id,
        runtime_scope=SCOPE,
        capability_scope="global",
        revision_id=revision_v1.revision_id,
    )
    assert selected.ok is True
    assert {item["entity_id"] for item in selected.bindings} == {codex.binding_id, hermes.binding_id}
    # An explicit provider selector is also a valid binding selector.  It may
    # disambiguate revisions, but cannot make a hostname/version part of the
    # capability's semantic identity.
    selected_codex = capabilities.resolve(
        definition.capability_id,
        runtime_scope=SCOPE,
        capability_scope="global",
        provider_kind="codex",
    )
    assert selected_codex.ok is True
    assert [item["entity_id"] for item in selected_codex.revisions] == [revision_v1.revision_id]
    assert [item["entity_id"] for item in selected_codex.bindings] == [codex.binding_id]
    assert capabilities.resolve(
        definition.capability_id,
        runtime_scope=SCOPE,
        capability_scope="global",
        binding_id=hermes.binding_id,
    ).bindings[0]["entity_id"] == hermes.binding_id


def test_lifecycle_cas_is_effective_at_time_replayable_and_blocks_current_resolution(registry) -> None:
    capabilities, store = registry
    definition = _definition("research.lifecycle_target")
    receipt = capabilities.register_definition(definition, runtime_scope=SCOPE, request_key="definition")
    before = capabilities.list_definitions(runtime_scope=SCOPE, capability_scope="global", limit=5)[0]

    transitioned = capabilities.transition_status(
        entity_type="definition",
        entity_id=definition.capability_id,
        entity_digest=receipt.entity_digest,
        target_status="deprecated",
        runtime_scope=SCOPE,
        capability_scope="global",
        expected_state_version=before["state_version"],
        expected_state_digest=before["state_digest"],
        effective_at="2020-08-20T01:00:00+00:00",
        reason="machine policy retired the old surface",
        provenance={"policy_id": "policy.lifecycle-test"},
        request_key="definition-deprecate",
    )
    assert transitioned.status == "deprecated"
    assert transitioned.state_version == 2
    assert capabilities.list_definitions(runtime_scope=SCOPE, capability_scope="global", status="active") == []
    historical = capabilities.list_definitions(
        runtime_scope=SCOPE,
        capability_scope="global",
        at_time="2020-08-20T00:30:00+00:00",
    )
    assert historical[0]["status"] == "active"
    assert capabilities.register_definition(definition, runtime_scope=SCOPE, request_key="definition").idempotent is True
    replay = capabilities.transition_status(
        entity_type="definition",
        entity_id=definition.capability_id,
        entity_digest=receipt.entity_digest,
        target_status="deprecated",
        runtime_scope=SCOPE,
        capability_scope="global",
        expected_state_version=before["state_version"],
        expected_state_digest=before["state_digest"],
        effective_at="2020-08-20T01:00:00+00:00",
        reason="machine policy retired the old surface",
        provenance={"policy_id": "policy.lifecycle-test"},
        request_key="definition-deprecate",
    )
    assert replay.idempotent is True
    with pytest.raises(CapabilityConflict, match="compare-and-swap"):
        capabilities.transition_status(
            entity_type="definition",
            entity_id=definition.capability_id,
            entity_digest=receipt.entity_digest,
            target_status="quarantined",
            runtime_scope=SCOPE,
            capability_scope="global",
            expected_state_version=before["state_version"],
            expected_state_digest=before["state_digest"],
            effective_at="2020-08-20T02:00:00+00:00",
            reason="stale writer",
            provenance={"policy_id": "policy.lifecycle-test"},
            request_key="stale-writer",
        )
    current = capabilities.list_definitions(runtime_scope=SCOPE, capability_scope="global", limit=5)[0]
    with pytest.raises(CapabilityConflict, match="cannot schedule a future"):
        capabilities.transition_status(
            entity_type="definition",
            entity_id=definition.capability_id,
            entity_digest=receipt.entity_digest,
            target_status="retired",
            runtime_scope=SCOPE,
            capability_scope="global",
            expected_state_version=current["state_version"],
            expected_state_digest=current["state_digest"],
            effective_at="2099-01-01T00:00:00+00:00",
            reason="future scheduling is not a current-state transition",
            provenance={"policy_id": "policy.lifecycle-test"},
            request_key="future-writer",
        )

    rebuilt = store.rebuild_sqlite_from_jsonl(replace=True)
    assert rebuilt["ok"] is True, rebuilt
    assert capabilities.list_definitions(runtime_scope=SCOPE, capability_scope="global")[0]["status"] == "deprecated"


def test_registry_rejects_dependency_cycles_and_ignores_retired_relation(registry) -> None:
    capabilities, store = registry
    first = _definition("planning.cycle_one")
    second = _definition("planning.cycle_two")
    capabilities.register_definition(first, runtime_scope=SCOPE, request_key="first")
    capabilities.register_definition(second, runtime_scope=SCOPE, request_key="second")
    forward = CapabilityRelation(
        source_capability_id=first.capability_id,
        target_capability_id=second.capability_id,
        relation_type="depends_on",
        relation_policy={"on_dependency_failure": "blocked"},
        provenance={"source": "registry-test"},
        created_at=STAMP,
    )
    reverse = CapabilityRelation(
        source_capability_id=second.capability_id,
        target_capability_id=first.capability_id,
        relation_type="depends_on",
        relation_policy={"on_dependency_failure": "blocked"},
        provenance={"source": "registry-test"},
        created_at=STAMP,
    )
    forward_receipt = capabilities.relate(forward, runtime_scope=SCOPE, request_key="forward")
    with pytest.raises(CapabilityConflict, match="cycle"):
        capabilities.relate(reverse, runtime_scope=SCOPE, request_key="reverse-cycle")
    current = store.read_capabilities(
        lambda repository: repository.list_effective_entities(
            entity_type="relation",
            scope=ScopeRef(**SCOPE),
            capability_scope="global",
            entity_id=forward.relation_id,
            limit=1,
        )[0]
    )
    assert forward_receipt.entity_id == forward.relation_id
    capabilities.transition_status(
        entity_type="relation",
        entity_id=forward.relation_id,
        entity_digest=forward_receipt.entity_digest,
        target_status="retired",
        runtime_scope=SCOPE,
        capability_scope="global",
        expected_state_version=current.state_version,
        expected_state_digest=current.state_digest,
        effective_at="2020-08-20T01:00:00+00:00",
        reason="superseded graph edge",
        provenance={"policy_id": "policy.graph-test"},
        request_key="retire-forward",
    )
    assert capabilities.relate(reverse, runtime_scope=SCOPE, request_key="reverse-after-retire").idempotent is False
    # Replaying the immutable retired descriptor must not be interpreted as a
    # request to reactivate its old edge against the new reverse relation.
    assert capabilities.relate(forward, runtime_scope=SCOPE, request_key="forward").idempotent is True
    # Records-stream recovery preserves the relation/lifecycle sequence rather
    # than globally replaying every relation before every transition.
    rebuilt = store.rebuild_sqlite_from_jsonl(replace=True)
    assert rebuilt["ok"] is True, rebuilt
    assert capabilities.relate(reverse, runtime_scope=SCOPE, request_key="reverse-after-retire").idempotent is True
