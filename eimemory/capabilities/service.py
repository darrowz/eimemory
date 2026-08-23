"""Bounded Runtime façade for the dynamic capability control plane.

The façade keeps callers above the capability repository boundary: no SQLite
rows, connection handles, host fingerprints, or hard-coded capability lists
escape through it.  Definitions and bindings remain dynamic inputs; the
service merely coordinates the registry, Profile resolver, and durable audit
status that those inputs require.
"""

from __future__ import annotations

from typing import Any, Mapping

from eimemory.capabilities.models import (
    AdapterCapabilityAdvertisement,
    CapabilityBinding,
    CapabilityDefinition,
    CapabilityProfile,
    CapabilityRelation,
    CapabilityRevision,
    EvaluationRun,
    EvaluationSpec,
)
from eimemory.capabilities.profiles import CapabilityProfiles
from eimemory.capabilities.registry import (
    CapabilityRegistry,
    CapabilityResolution,
    MutationReceipt,
    exact_runtime_scope,
)
from eimemory.models.records import ScopeRef
from eimemory.storage.runtime_store import RuntimeStore


class CapabilityService:
    """The Runtime-facing capability API with exact-scope, bounded DTOs."""

    def __init__(self, store: RuntimeStore) -> None:
        self._store = store
        self._registry = CapabilityRegistry(store)
        self._profiles = CapabilityProfiles(store)

    def register_definition(
        self,
        definition: CapabilityDefinition,
        *,
        runtime_scope: ScopeRef | Mapping[str, Any],
        request_key: str = "",
    ) -> MutationReceipt:
        return self._registry.register_definition(
            definition,
            runtime_scope=runtime_scope,
            request_key=request_key,
        )

    def register_revision(
        self,
        revision: CapabilityRevision,
        *,
        runtime_scope: ScopeRef | Mapping[str, Any],
        request_key: str = "",
    ) -> MutationReceipt:
        return self._registry.register_revision(
            revision,
            runtime_scope=runtime_scope,
            request_key=request_key,
        )

    def relate(
        self,
        relation: CapabilityRelation,
        *,
        runtime_scope: ScopeRef | Mapping[str, Any],
        request_key: str = "",
    ) -> MutationReceipt:
        return self._registry.relate(
            relation,
            runtime_scope=runtime_scope,
            request_key=request_key,
        )

    def bind(
        self,
        binding: CapabilityBinding,
        *,
        runtime_scope: ScopeRef | Mapping[str, Any],
        request_key: str = "",
    ) -> MutationReceipt:
        return self._registry.bind(
            binding,
            runtime_scope=runtime_scope,
            request_key=request_key,
        )

    def register_evaluation_spec(
        self,
        spec: EvaluationSpec,
        *,
        runtime_scope: ScopeRef | Mapping[str, Any],
        profile_id: str | None = None,
        request_key: str = "",
    ) -> MutationReceipt:
        return self._registry.register_evaluation_spec(
            spec,
            runtime_scope=runtime_scope,
            profile_id=profile_id,
            request_key=request_key,
        )

    def record_evaluation_run(
        self,
        run: EvaluationRun,
        *,
        runtime_scope: ScopeRef | Mapping[str, Any],
        profile_id: str | None = None,
        request_key: str = "",
    ) -> MutationReceipt:
        return self._registry.record_evaluation_run(
            run,
            runtime_scope=runtime_scope,
            profile_id=profile_id,
            request_key=request_key,
        )

    def advertise(
        self,
        advertisement: AdapterCapabilityAdvertisement,
        *,
        runtime_scope: ScopeRef | Mapping[str, Any],
        request_key: str = "",
    ) -> MutationReceipt:
        """Persist one adapter advertisement without exposing storage rows."""

        return self._registry.advertise(
            advertisement,
            runtime_scope=runtime_scope,
            request_key=request_key,
        )

    def register_profile(
        self,
        profile: CapabilityProfile,
        *,
        runtime_scope: ScopeRef | Mapping[str, Any],
        request_key: str = "",
    ) -> MutationReceipt:
        return self._profiles.register(
            profile,
            runtime_scope=runtime_scope,
            request_key=request_key,
        )

    def transition_status(
        self,
        *,
        entity_type: str,
        entity_id: str,
        entity_digest: str,
        target_status: str,
        runtime_scope: ScopeRef | Mapping[str, Any],
        capability_scope: str,
        expected_state_version: int,
        expected_state_digest: str,
        effective_at: str,
        reason: str,
        provenance: Mapping[str, Any],
        request_key: str = "",
    ) -> MutationReceipt:
        return self._registry.transition_status(
            entity_type=entity_type,
            entity_id=entity_id,
            entity_digest=entity_digest,
            target_status=target_status,
            runtime_scope=runtime_scope,
            capability_scope=capability_scope,
            expected_state_version=expected_state_version,
            expected_state_digest=expected_state_digest,
            effective_at=effective_at,
            reason=reason,
            provenance=provenance,
            request_key=request_key,
        )

    def list_definitions(
        self,
        *,
        runtime_scope: ScopeRef | Mapping[str, Any],
        capability_scope: str,
        status: str | None = None,
        at_time: str = "",
        cursor: str = "",
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        return self._registry.list_definitions(
            runtime_scope=runtime_scope,
            capability_scope=capability_scope,
            status=status,
            at_time=at_time,
            cursor=cursor,
            limit=limit,
        )

    def list_lifecycle_events(
        self,
        *,
        entity_type: str,
        entity_id: str,
        runtime_scope: ScopeRef | Mapping[str, Any],
        capability_scope: str,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Read append-only lifecycle provenance through the registry boundary."""

        return self._registry.list_lifecycle_events(
            entity_type=entity_type,
            entity_id=entity_id,
            runtime_scope=runtime_scope,
            capability_scope=capability_scope,
            limit=limit,
        )

    def list_advertisements(
        self,
        *,
        runtime_scope: ScopeRef | Mapping[str, Any],
        capability_scope: str,
        binding_id: str = "",
        adapter_id: str = "",
        provider_kind: str = "",
        provider_instance_id: str = "",
        status: str | None = "active",
        at_time: str = "",
        fresh_at: str = "",
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Read bounded advertisements through the Registry contract."""

        return self._registry.list_advertisements(
            runtime_scope=runtime_scope,
            capability_scope=capability_scope,
            binding_id=binding_id,
            adapter_id=adapter_id,
            provider_kind=provider_kind,
            provider_instance_id=provider_instance_id,
            status=status,
            at_time=at_time,
            fresh_at=fresh_at,
            limit=limit,
        )

    def list_adapter_advertisements(
        self,
        *,
        runtime_scope: ScopeRef | Mapping[str, Any],
        capability_scope: str,
        adapter_id: str = "",
        binding_id: str = "",
        at_time: str = "",
        limit: int = 100,
        fresh_at: str = "",
        provider_kind: str = "",
        provider_instance_id: str = "",
        status: str | None = "active",
    ) -> list[dict[str, Any]]:
        """Stable Runtime capabilities façade for adapter-readiness consumers."""

        return self._registry.list_adapter_advertisements(
            runtime_scope=runtime_scope,
            capability_scope=capability_scope,
            adapter_id=adapter_id,
            binding_id=binding_id,
            at_time=at_time,
            limit=limit,
            fresh_at=fresh_at,
            provider_kind=provider_kind,
            provider_instance_id=provider_instance_id,
            status=status,
        )

    def binding_context(
        self,
        binding_id: str,
        *,
        runtime_scope: ScopeRef | Mapping[str, Any],
        capability_scope: str,
        at_time: str = "",
    ) -> dict[str, Any] | None:
        """Return one effective binding DTO for adapter-outcome normalization.

        The adapter supplies only a binding/revision assertion; capability
        identity is recovered from the registered binding contract rather than
        accepted from an untrusted host event.  No raw database handle crosses
        the adapter boundary.
        """

        scope = exact_runtime_scope(runtime_scope)
        rows = self._store.read_capabilities(
            lambda repository: repository.list_effective_entities(
                entity_type="binding",
                scope=scope,
                capability_scope=capability_scope,
                entity_id=str(binding_id),
                at_time=at_time,
                limit=2,
            )
        )
        if len(rows) != 1:
            return None
        row = rows[0]
        return {
            "entity_type": row.entity_type,
            "entity_id": row.entity_id,
            "entity_digest": row.entity_digest,
            "status": row.status,
            "state_version": row.state_version,
            "state_digest": row.state_digest,
            "effective_at": row.effective_at,
            "descriptor": dict(row.payload),
        }

    def advertisement_context(
        self,
        advertisement_id: str,
        *,
        runtime_scope: ScopeRef | Mapping[str, Any],
        capability_scope: str,
        at_time: str = "",
    ) -> dict[str, Any] | None:
        """Return one immutable advertisement by exact identity.

        This view intentionally does not apply a freshness filter.  A code-
        evolution transaction is bound to the advertisement that authorized
        provider resolution at proposal time, while current liveness is proven
        independently by the latest fresh advertisement.
        """

        scope = exact_runtime_scope(runtime_scope)
        rows = self._store.read_capabilities(
            lambda repository: repository.list_effective_entities(
                entity_type="advertisement",
                scope=scope,
                capability_scope=capability_scope,
                entity_id=str(advertisement_id),
                at_time=at_time,
                limit=2,
            )
        )
        if len(rows) != 1:
            return None
        row = rows[0]
        return {
            "entity_type": row.entity_type,
            "entity_id": row.entity_id,
            "entity_digest": row.entity_digest,
            "status": row.status,
            "state_version": row.state_version,
            "state_digest": row.state_digest,
            "effective_at": row.effective_at,
            "descriptor": dict(row.payload),
        }

    def incubation_context(
        self,
        capability_id: str,
        *,
        runtime_scope: ScopeRef | Mapping[str, Any],
        capability_scope: str,
        at_time: str = "",
        limit: int = 100,
    ) -> dict[str, Any]:
        """Read active revisions/bindings for one discovered definition.

        This bounded exact-scope view exists only for the incubation control
        plane. It does not make the definition resolvable to ordinary active
        consumers before its lifecycle transition.
        """

        scope = exact_runtime_scope(runtime_scope)
        budget = max(1, min(499, int(limit)))

        def reader(repository):
            revisions = repository.list_effective_entities(
                entity_type="revision",
                scope=scope,
                capability_scope=capability_scope,
                status="active",
                at_time=at_time,
                capability_id=str(capability_id),
                limit=budget + 1,
            )
            bindings = repository.list_effective_entities(
                entity_type="binding",
                scope=scope,
                capability_scope=capability_scope,
                status="active",
                at_time=at_time,
                capability_id=str(capability_id),
                limit=budget + 1,
            )
            return revisions, bindings

        revisions, bindings = self._store.read_capabilities(reader)
        if len(revisions) > budget or len(bindings) > budget:
            raise ValueError("capability incubation context exceeds bounded limit")

        def public(row):
            return {
                "entity_type": row.entity_type,
                "entity_id": row.entity_id,
                "entity_digest": row.entity_digest,
                "status": row.status,
                "state_version": row.state_version,
                "state_digest": row.state_digest,
                "effective_at": row.effective_at,
                "descriptor": dict(row.payload),
            }

        return {
            "capability_id": str(capability_id),
            "capability_scope": capability_scope,
            "revisions": [public(row) for row in revisions],
            "bindings": [public(row) for row in bindings],
        }

    def resolve(
        self,
        capability_id: str,
        *,
        runtime_scope: ScopeRef | Mapping[str, Any],
        capability_scope: str,
        revision_id: str = "",
        binding_id: str = "",
        provider_kind: str = "",
        provider_instance_id: str = "",
        operation: str = "",
        at_time: str = "",
        limit: int = 100,
    ) -> CapabilityResolution:
        return self._registry.resolve(
            capability_id,
            runtime_scope=runtime_scope,
            capability_scope=capability_scope,
            revision_id=revision_id,
            binding_id=binding_id,
            provider_kind=provider_kind,
            provider_instance_id=provider_instance_id,
            operation=operation,
            at_time=at_time,
            limit=limit,
        )

    def resolve_profile(
        self,
        profile_key: str,
        *,
        runtime_scope: ScopeRef | Mapping[str, Any],
        capability_scope: str,
        at_time: str = "",
        max_candidates: int = 100,
    ) -> dict[str, Any]:
        return self._profiles.resolve(
            profile_key,
            runtime_scope=runtime_scope,
            capability_scope=capability_scope,
            at_time=at_time,
            max_candidates=max_candidates,
        )

    def apply_seed_manifest(
        self,
        *,
        runtime_scope: ScopeRef | Mapping[str, Any],
        capability_scope: str = "global",
        manifest: object | None = None,
    ) -> Any:
        """Explicitly apply a versioned bootstrap manifest.

        Import and invocation are both deliberate: merely constructing
        ``Runtime`` cannot create a capability taxonomy.  The manifest module
        returns its own bounded receipt so future manifest revisions need not
        expand this façade's contract.
        """

        from eimemory.capabilities.seed_manifest import apply_seed_manifest

        return apply_seed_manifest(
            self._registry,
            runtime_scope=runtime_scope,
            capability_scope=capability_scope,
            manifest=manifest,
        )

    def status(self) -> dict[str, Any]:
        """Expose durable control-plane export health, not an L5 claim."""

        return {
            "schema": "capability.service_status.v1",
            "audit_export": self._store.capability_export_status(),
        }


__all__ = ["CapabilityService"]
