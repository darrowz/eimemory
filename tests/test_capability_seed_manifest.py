from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from eimemory.capabilities.models import CapabilityDefinition, CapabilityRevision
from eimemory.capabilities.registry import CapabilityRegistry
from eimemory.capabilities.seed_manifest import (
    CapabilitySeedManifestError,
    apply_seed_manifest,
    canonical_manifest_digest,
    load_seed_manifest,
    validate_seed_manifest,
)
from eimemory.storage.runtime_store import RuntimeStore
from eimemory.storage.capability_store import CapabilityConflict


SCOPE = {
    "tenant_id": "tenant-seed",
    "agent_id": "agent-seed",
    "workspace_id": "workspace-seed",
    "user_id": "user-seed",
}
LEGACY_CAPABILITIES = {
    "memory.recall",
    "tool.routing",
    "knowledge.intake",
    "proactive.judgment",
    "search.discovery",
    "code.implementation",
    "operations.uumit",
    "office.daily_task",
    "device.control",
    "research.synthesis",
    "safety.boundary",
}


@pytest.fixture()
def registry(tmp_path: Path):
    store = RuntimeStore(tmp_path)
    try:
        yield CapabilityRegistry(store)
    finally:
        store.close()


def test_versioned_manifest_is_declarative_and_covers_exact_legacy_vocabulary() -> None:
    manifest = load_seed_manifest()

    assert manifest.manifest_id == "legacy-capability-bootstrap"
    assert manifest.version == "v1"
    assert {item.capability_id for item in manifest.capabilities} == LEGACY_CAPABILITIES
    assert all(item.revision_id == f"{item.capability_id}:v1" for item in manifest.capabilities)
    assert all(item.to_dict()["revision"]["compatibility"] == "incompatible" for item in manifest.capabilities)
    assert canonical_manifest_digest(manifest.to_dict()) == manifest.manifest_digest


def test_seed_is_explicit_idempotent_and_creates_no_bindings(registry: CapabilityRegistry) -> None:
    assert registry.list_definitions(runtime_scope=SCOPE, capability_scope="global") == []

    first = apply_seed_manifest(registry, runtime_scope=SCOPE)
    second = apply_seed_manifest(registry, runtime_scope=SCOPE)

    assert first.created_count == 22
    assert first.idempotent is False
    assert len(first.definition_receipts) == len(LEGACY_CAPABILITIES)
    assert len(first.revision_receipts) == len(LEGACY_CAPABILITIES)
    assert second.created_count == 0
    assert second.idempotent is True
    definitions = registry.list_definitions(runtime_scope=SCOPE, capability_scope="global", limit=100)
    assert {item["entity_id"] for item in definitions} == LEGACY_CAPABILITIES
    assert {item["status"] for item in definitions} == {"discovered"}
    assert all(item["descriptor"]["provenance"]["source"] == "eimemory.capability_seed_manifest" for item in definitions)
    for capability_id in LEGACY_CAPABILITIES:
        resolution = registry.resolve(capability_id, runtime_scope=SCOPE, capability_scope="global")
        assert resolution.reason == "definition_discovered"
        assert resolution.bindings == ()


def test_same_manifest_id_and_version_cannot_be_applied_with_changed_digest(registry: CapabilityRegistry) -> None:
    first = apply_seed_manifest(registry, runtime_scope=SCOPE)
    changed = load_seed_manifest().to_dict()
    changed["capabilities"][0]["description"] = "Changed content must require a new manifest version."
    changed["manifest_digest"] = canonical_manifest_digest(changed)

    with pytest.raises(CapabilitySeedManifestError, match="already applied with a different digest"):
        apply_seed_manifest(registry, runtime_scope=SCOPE, manifest=changed)

    assert first.created_count == 22
    assert len(registry.list_definitions(runtime_scope=SCOPE, capability_scope="global")) == 11


def test_seed_manifest_instance_is_revalidated_at_the_registry_boundary(registry: CapabilityRegistry) -> None:
    manifest = load_seed_manifest()
    forged = replace(manifest, manifest_digest="a" * 64)

    with pytest.raises(CapabilitySeedManifestError, match="digest does not match"):
        apply_seed_manifest(registry, runtime_scope=SCOPE, manifest=forged)

    assert registry.list_definitions(runtime_scope=SCOPE, capability_scope="global") == []


def test_seed_manifest_rejects_an_unlisted_revision_with_copied_provenance(
    registry: CapabilityRegistry,
) -> None:
    manifest = load_seed_manifest()
    apply_seed_manifest(registry, runtime_scope=SCOPE, manifest=manifest)
    seed_capability = manifest.capabilities[0]
    shadow_contract = seed_capability.to_dict()["revision"]["contract"]
    shadow_contract["success_invariants"].append("manifest_identity_bound")
    registry.register_revision(
        CapabilityRevision(
            revision_id=f"{seed_capability.capability_id}:shadow-v2",
            capability_id=seed_capability.capability_id,
            contract=shadow_contract,
            compatibility="incompatible",
            created_at=manifest.created_at,
            status="active",
            scope="global",
            provenance={
                "source": "eimemory.capability_seed_manifest",
                "manifest_id": manifest.manifest_id,
                "manifest_version": manifest.version,
                "manifest_digest": manifest.manifest_digest,
            },
        ),
        runtime_scope=SCOPE,
        request_key="seed-manifest-shadow-revision",
    )

    with pytest.raises(CapabilitySeedManifestError, match="unexpected revision"):
        apply_seed_manifest(registry, runtime_scope=SCOPE, manifest=manifest)


def test_seed_manifest_is_one_atomic_data_unit_when_a_late_descriptor_conflicts(
    registry: CapabilityRegistry,
) -> None:
    manifest = load_seed_manifest()
    conflicting_seed = manifest.capabilities[-1]
    registry.register_definition(
        CapabilityDefinition(
            capability_id=conflicting_seed.capability_id,
            display_name="Conflicting pre-existing definition",
            description="Forces the last seed descriptor to reject the whole batch.",
            owner="test",
            risk_tier=conflicting_seed.risk_tier,
            tags=("test",),
            provenance={"source": "unrelated"},
            created_at=manifest.created_at,
            scope="global",
        ),
        runtime_scope=SCOPE,
        request_key="external-conflict",
    )
    with pytest.raises(CapabilityConflict, match="definition .*immutable"):
        apply_seed_manifest(registry, runtime_scope=SCOPE)

    # Every preceding definition/revision was rolled back with the conflict;
    # a retry cannot observe a half-applied manifest receipt.
    definitions = registry.list_definitions(runtime_scope=SCOPE, capability_scope="global", limit=100)
    assert [item["entity_id"] for item in definitions] == [conflicting_seed.capability_id]
    assert definitions[0]["descriptor"]["provenance"] == {"source": "unrelated"}


def test_loader_rejects_unknown_or_executable_manifest_fields_before_registry_write() -> None:
    changed = load_seed_manifest().to_dict()
    changed["capabilities"][0]["revision"]["contract"]["command"] = "powershell -EncodedCommand ..."
    changed["manifest_digest"] = canonical_manifest_digest(changed)

    with pytest.raises(CapabilitySeedManifestError, match="invalid declarative fields"):
        validate_seed_manifest(changed)
