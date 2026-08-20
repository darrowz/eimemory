"""Dynamic capability registry over the Storage v2 transaction boundary.

This module owns semantic registration and effective resolution.  It never
opens SQLite, never exposes rows, and never reads legacy L5 score/taxonomy
lists.  The only mutable owner is ``RuntimeStore.mutate_capabilities_atomically``.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from eimemory.capabilities.contracts import (
    normalize_opaque_id,
    normalize_sha256,
    require_timestamp,
)
from eimemory.capabilities.models import (
    AdapterCapabilityAdvertisement,
    CapabilityBinding,
    CapabilityDefinition,
    CapabilityRelation,
    CapabilityRevision,
    EvaluationRun,
    EvaluationSpec,
)
from eimemory.models.records import ScopeRef
from eimemory.storage.capability_store import (
    CapabilityConflict,
    EffectiveCapabilityEntity,
    LifecycleTransitionReceipt,
    StoredCapabilityEntity,
)
from eimemory.storage.runtime_store import RuntimeStore


class CapabilityRegistryError(RuntimeError):
    """A bounded registry request is malformed or cannot be resolved safely."""


_SEED_MANIFEST_PROVENANCE_SOURCE = "eimemory.capability_seed_manifest"


def exact_runtime_scope(value: ScopeRef | Mapping[str, Any]) -> ScopeRef:
    """Decode a capability request scope without legacy default fallbacks."""

    required = ("tenant_id", "agent_id", "workspace_id", "user_id")
    if isinstance(value, ScopeRef):
        values = {
            "tenant_id": value.tenant_id,
            "agent_id": value.agent_id,
            "workspace_id": value.workspace_id,
            "user_id": value.user_id,
        }
    elif isinstance(value, Mapping):
        if set(value) != set(required) or any(not isinstance(value[key], str) for key in required):
            raise CapabilityRegistryError("runtime_scope requires exact tenant/agent/workspace/user strings")
        values = {key: str(value[key]) for key in required}
    else:
        raise CapabilityRegistryError("runtime_scope must be ScopeRef or an exact scope mapping")
    if any(not isinstance(values[key], str) for key in required):
        raise CapabilityRegistryError("runtime_scope requires exact tenant/agent/workspace/user strings")
    if not values["tenant_id"].strip():
        raise CapabilityRegistryError("runtime_scope.tenant_id must not be empty")
    return ScopeRef(**values)


def _capability_scope(value: object) -> str:
    text = str(value or "").strip()
    if not text:
        raise CapabilityRegistryError("capability_scope is required")
    return text


def _public_entity(entity: EffectiveCapabilityEntity) -> dict[str, Any]:
    return {
        "entity_type": entity.entity_type,
        "entity_id": entity.entity_id,
        "entity_digest": entity.entity_digest,
        "status": entity.status,
        "state_version": entity.state_version,
        "state_digest": entity.state_digest,
        "effective_at": entity.effective_at,
        "descriptor": deepcopy(dict(entity.payload)),
    }


def _public_advertisement_entity(
    entity: EffectiveCapabilityEntity,
    *,
    fresh_at: str = "",
) -> dict[str, Any]:
    """Expose lifecycle and freshness separately for an immutable ad DTO."""

    result = _public_entity(entity)
    descriptor = result["descriptor"]
    checked_at = require_timestamp(fresh_at, field="fresh_at", required=False) if fresh_at else ""
    advertised_at = str(descriptor.get("advertised_at") or "")
    expires_at = str(descriptor.get("expires_at") or "")
    is_fresh = (
        result["status"] == "active"
        and bool(checked_at)
        and advertised_at <= checked_at < expires_at
    )
    result["freshness"] = {
        "checked_at": checked_at,
        "advertised_at": advertised_at,
        "expires_at": expires_at,
        "is_fresh": is_fresh if checked_at else None,
    }
    return result


@dataclass(frozen=True, slots=True)
class MutationReceipt:
    entity_type: str
    entity_id: str
    entity_digest: str
    operation_id: str
    ledger_event_id: str
    idempotent: bool
    status: str = ""
    state_version: int = 0
    state_digest: str = ""
    effective_at: str = ""

    @classmethod
    def from_stored(cls, value: StoredCapabilityEntity | LifecycleTransitionReceipt) -> "MutationReceipt":
        if isinstance(value, LifecycleTransitionReceipt):
            return cls(
                entity_type=value.entity_type,
                entity_id=value.entity_id,
                entity_digest=value.target_entity_digest,
                operation_id=value.operation_id,
                ledger_event_id=value.ledger_event_id,
                idempotent=value.idempotent,
                status=value.status,
                state_version=value.state_version,
                state_digest=value.state_digest,
                effective_at=value.effective_at,
            )
        return cls(
            entity_type=value.entity_type,
            entity_id=value.entity_id,
            entity_digest=value.entity_digest,
            operation_id=value.operation_id,
            ledger_event_id=value.ledger_event_id,
            idempotent=value.idempotent,
        )

    def to_dict(self) -> dict[str, Any]:
        result = {
            "entity_type": self.entity_type,
            "entity_id": self.entity_id,
            "entity_digest": self.entity_digest,
            "operation_id": self.operation_id,
            "ledger_event_id": self.ledger_event_id,
            "idempotent": self.idempotent,
        }
        if self.status:
            result.update(
                {
                    "status": self.status,
                    "state_version": self.state_version,
                    "state_digest": self.state_digest,
                    "effective_at": self.effective_at,
                }
            )
        return result


@dataclass(frozen=True, slots=True)
class CapabilityResolution:
    capability_id: str
    capability_scope: str
    at_time: str
    ok: bool
    reason: str
    definition: Mapping[str, Any] | None
    revisions: tuple[Mapping[str, Any], ...]
    bindings: tuple[Mapping[str, Any], ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "reason": self.reason,
            "capability_id": self.capability_id,
            "capability_scope": self.capability_scope,
            "at_time": self.at_time,
            "definition": deepcopy(dict(self.definition)) if self.definition is not None else None,
            "revisions": [deepcopy(dict(item)) for item in self.revisions],
            "bindings": [deepcopy(dict(item)) for item in self.bindings],
        }


class CapabilityRegistry:
    """Register and resolve arbitrary capability descriptors at runtime."""

    def __init__(self, store: RuntimeStore) -> None:
        self._store = store

    def register_definition(
        self,
        definition: CapabilityDefinition,
        *,
        runtime_scope: ScopeRef | Mapping[str, Any],
        request_key: str = "",
    ) -> MutationReceipt:
        scope = exact_runtime_scope(runtime_scope)
        result = self._store.mutate_capabilities_atomically(
            lambda repository: repository.register_definition(definition, scope=scope, request_key=request_key)
        )
        return MutationReceipt.from_stored(result)

    def register_revision(
        self,
        revision: CapabilityRevision,
        *,
        runtime_scope: ScopeRef | Mapping[str, Any],
        request_key: str = "",
    ) -> MutationReceipt:
        scope = exact_runtime_scope(runtime_scope)
        result = self._store.mutate_capabilities_atomically(
            lambda repository: repository.register_revision(revision, scope=scope, request_key=request_key)
        )
        return MutationReceipt.from_stored(result)

    def relate(
        self,
        relation: CapabilityRelation,
        *,
        runtime_scope: ScopeRef | Mapping[str, Any],
        request_key: str = "",
    ) -> MutationReceipt:
        scope = exact_runtime_scope(runtime_scope)
        result = self._store.mutate_capabilities_atomically(
            lambda repository: repository.register_relation(relation, scope=scope, request_key=request_key)
        )
        return MutationReceipt.from_stored(result)

    def bind(
        self,
        binding: CapabilityBinding,
        *,
        runtime_scope: ScopeRef | Mapping[str, Any],
        request_key: str = "",
    ) -> MutationReceipt:
        scope = exact_runtime_scope(runtime_scope)
        result = self._store.mutate_capabilities_atomically(
            lambda repository: repository.register_binding(binding, scope=scope, request_key=request_key)
        )
        return MutationReceipt.from_stored(result)

    def register_evaluation_spec(
        self,
        spec: EvaluationSpec,
        *,
        runtime_scope: ScopeRef | Mapping[str, Any],
        profile_id: str | None = None,
        request_key: str = "",
    ) -> MutationReceipt:
        """Register an immutable evaluation descriptor in the capability ledger."""

        scope = exact_runtime_scope(runtime_scope)
        result = self._store.mutate_capabilities_atomically(
            lambda repository: repository.register_evaluation_spec(
                spec,
                scope=scope,
                profile_id=profile_id,
                request_key=request_key,
            )
        )
        return MutationReceipt.from_stored(result)

    def record_evaluation_run(
        self,
        run: EvaluationRun,
        *,
        runtime_scope: ScopeRef | Mapping[str, Any],
        profile_id: str | None = None,
        request_key: str = "",
    ) -> MutationReceipt:
        """Record one independently evidenced evaluation run atomically."""

        scope = exact_runtime_scope(runtime_scope)
        result = self._store.mutate_capabilities_atomically(
            lambda repository: repository.record_evaluation_run(
                run,
                scope=scope,
                profile_id=profile_id,
                request_key=request_key,
            )
        )
        return MutationReceipt.from_stored(result)

    def advertise(
        self,
        advertisement: AdapterCapabilityAdvertisement,
        *,
        runtime_scope: ScopeRef | Mapping[str, Any],
        request_key: str = "",
    ) -> MutationReceipt:
        """Register one immutable provider/binding advertisement revision."""

        scope = exact_runtime_scope(runtime_scope)
        result = self._store.register_capability_advertisement(
            advertisement,
            scope=scope,
            request_key=request_key,
        )
        return MutationReceipt.from_stored(result)

    def register_seed_manifest(
        self,
        *,
        definitions: Sequence[CapabilityDefinition],
        revisions: Sequence[CapabilityRevision],
        manifest_id: str,
        manifest_version: str,
        manifest_digest: str,
        runtime_scope: ScopeRef | Mapping[str, Any],
    ) -> tuple[tuple[MutationReceipt, ...], tuple[MutationReceipt, ...]]:
        """Atomically install one validated, immutable bootstrap declaration.

        The registry owns the one write transaction.  A matching immutable
        descriptor receipt can be replayed idempotently; a changed manifest
        identity is rejected before any descriptor is written.  The method is
        intentionally generic over descriptors rather than a fixed capability
        list, so future manifest versions remain data-only changes.
        """

        scope = exact_runtime_scope(runtime_scope)
        normalized_manifest_id = normalize_opaque_id(manifest_id, field="manifest_id")
        normalized_manifest_version = normalize_opaque_id(manifest_version, field="manifest_version")
        normalized_manifest_digest = normalize_sha256(manifest_digest, field="manifest_digest")
        normalized_definitions = tuple(definitions)
        normalized_revisions = tuple(revisions)
        if not normalized_definitions or len(normalized_definitions) > 128:
            raise CapabilityRegistryError("seed manifest must contain 1..128 capability definitions")
        if len(normalized_revisions) != len(normalized_definitions):
            raise CapabilityRegistryError("seed manifest must contain one revision for every definition")
        capability_scope = normalized_definitions[0].scope
        definition_ids = {item.capability_id for item in normalized_definitions}
        if len(definition_ids) != len(normalized_definitions):
            raise CapabilityRegistryError("seed manifest contains duplicate capability definitions")
        revision_ids = {item.revision_id for item in normalized_revisions}
        if len(revision_ids) != len(normalized_revisions):
            raise CapabilityRegistryError("seed manifest contains duplicate capability revisions")
        if any(item.scope != capability_scope for item in normalized_definitions) or any(
            item.scope != capability_scope for item in normalized_revisions
        ):
            raise CapabilityRegistryError("seed manifest descriptors must share one capability scope")
        if {item.capability_id for item in normalized_revisions} != definition_ids:
            raise CapabilityRegistryError("seed manifest revisions must match its definition identities")
        expected_definition_digests = {item.capability_id: item.definition_digest for item in normalized_definitions}
        expected_revisions = {
            item.revision_id: (item.capability_id, item.contract_digest) for item in normalized_revisions
        }
        expected_seed_provenance = {
            "source": _SEED_MANIFEST_PROVENANCE_SOURCE,
            "manifest_id": normalized_manifest_id,
            "manifest_version": normalized_manifest_version,
            "manifest_digest": normalized_manifest_digest,
        }
        for definition in normalized_definitions:
            if definition.status != "discovered" or any(
                definition.provenance.get(key) != value for key, value in expected_seed_provenance.items()
            ):
                raise CapabilityRegistryError(
                    "seed manifest definitions must be discovered and carry the exact manifest provenance"
                )
        for revision in normalized_revisions:
            if (
                revision.status != "active"
                or revision.compatibility != "incompatible"
                or any(revision.provenance.get(key) != value for key, value in expected_seed_provenance.items())
            ):
                raise CapabilityRegistryError(
                    "seed manifest revisions must be active/incompatible and carry the exact manifest provenance"
                )

        def request_key(entity_type: str, entity_id: str) -> str:
            return (
                f"seed-manifest:{normalized_manifest_id}:{normalized_manifest_version}:"
                f"{normalized_manifest_digest}:{entity_type}:{entity_id}"
            )

        def mutation(repository):
            existing_definitions = repository.find_seed_manifest_definitions(
                scope=scope,
                capability_scope=capability_scope,
                manifest_id=normalized_manifest_id,
                manifest_version=normalized_manifest_version,
                limit=129,
            )
            existing_revisions = repository.find_seed_manifest_revisions(
                scope=scope,
                capability_scope=capability_scope,
                manifest_id=normalized_manifest_id,
                manifest_version=normalized_manifest_version,
                limit=129,
            )
            if len(existing_definitions) >= 129 or len(existing_revisions) >= 129:
                raise CapabilityRegistryError("seed manifest receipt count exceeds its bounded declaration limit")
            existing_digests = {
                str(item["manifest_digest"]) for item in (*existing_definitions, *existing_revisions)
            }
            if existing_digests and existing_digests != {normalized_manifest_digest}:
                raise CapabilityRegistryError(
                    "capability seed manifest id/version was already applied with a different digest"
                )
            existing_definition_ids = {str(item["capability_id"]) for item in existing_definitions}
            if not existing_definition_ids.issubset(definition_ids):
                raise CapabilityRegistryError(
                    "capability seed manifest receipt has an unexpected definition for this manifest id/version"
                )
            for item in existing_definitions:
                capability_id = str(item["capability_id"])
                if str(item["definition_digest"]) != expected_definition_digests[capability_id]:
                    raise CapabilityRegistryError(
                        "capability seed manifest receipt has a changed definition for this manifest id/version"
                    )
            existing_revision_ids = {str(item["revision_id"]) for item in existing_revisions}
            if not existing_revision_ids.issubset(revision_ids):
                raise CapabilityRegistryError(
                    "capability seed manifest receipt has an unexpected revision for this manifest id/version"
                )
            for item in existing_revisions:
                revision_id = str(item["revision_id"])
                expected_capability_id, expected_contract_digest = expected_revisions[revision_id]
                if (
                    str(item["capability_id"]) != expected_capability_id
                    or str(item["contract_digest"]) != expected_contract_digest
                ):
                    raise CapabilityRegistryError(
                        "capability seed manifest receipt has a changed revision for this manifest id/version"
                    )
            definition_receipts = tuple(
                MutationReceipt.from_stored(
                    repository.register_definition(
                        definition,
                        scope=scope,
                        request_key=request_key("definition", definition.capability_id),
                    )
                )
                for definition in normalized_definitions
            )
            revision_receipts = tuple(
                MutationReceipt.from_stored(
                    repository.register_revision(
                        revision,
                        scope=scope,
                        request_key=request_key("revision", revision.revision_id),
                    )
                )
                for revision in normalized_revisions
            )
            return definition_receipts, revision_receipts

        return self._store.mutate_capabilities_atomically(mutation)

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
        scope = exact_runtime_scope(runtime_scope)
        logical_scope = _capability_scope(capability_scope)
        result = self._store.mutate_capabilities_atomically(
            lambda repository: repository.transition_lifecycle(
                entity_type=entity_type,
                entity_id=entity_id,
                entity_digest=entity_digest,
                target_status=target_status,
                scope=scope,
                capability_scope=logical_scope,
                expected_state_version=expected_state_version,
                expected_state_digest=expected_state_digest,
                effective_at=effective_at,
                reason=reason,
                provenance=provenance,
                request_key=request_key,
            )
        )
        return MutationReceipt.from_stored(result)

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
        scope = exact_runtime_scope(runtime_scope)
        logical_scope = _capability_scope(capability_scope)
        entities = self._store.read_capabilities(
            lambda repository: repository.list_effective_entities(
                entity_type="definition",
                scope=scope,
                capability_scope=logical_scope,
                status=status,
                at_time=at_time,
                cursor=cursor,
                limit=limit,
            )
        )
        return [_public_entity(entity) for entity in entities]

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
        """Return bounded, lifecycle-effective advertisement DTOs.

        The descriptors retain their immutable provider facts.  A caller must
        still select its own provider/binding; this registry never infers a
        semantic capability from host identity or picks a newest provider.
        The separate ``freshness`` member is evaluated at ``fresh_at`` (or
        ``at_time`` when no explicit freshness time was supplied).
        """

        scope = exact_runtime_scope(runtime_scope)
        logical_scope = _capability_scope(capability_scope)
        entities = self._store.list_adapter_advertisements(
            scope=scope,
            capability_scope=logical_scope,
            binding_id=binding_id,
            adapter_id=adapter_id,
            provider_kind=provider_kind,
            provider_instance_id=provider_instance_id,
            status=status,
            at_time=at_time,
            fresh_at=fresh_at,
            limit=limit,
        )
        freshness_time = fresh_at or at_time
        return [
            _public_advertisement_entity(entity, fresh_at=freshness_time)
            for entity in entities
        ]

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
        """Stable public spelling for adapter-readiness consumers."""

        return self.list_advertisements(
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
        """Resolve an effective capability without implicitly picking a provider.

        Multiple active revisions are legitimate.  If the caller does not
        select one, resolution fails closed instead of guessing from version,
        machine, provider, or newest timestamp.  Multiple bindings of one
        selected revision are returned independently rather than collapsed.
        """

        scope = exact_runtime_scope(runtime_scope)
        logical_scope = _capability_scope(capability_scope)
        capability_id = str(capability_id or "").strip()
        if not capability_id:
            raise CapabilityRegistryError("capability_id is required")

        def reader(repository):
            definition_rows = repository.list_effective_entities(
                entity_type="definition",
                scope=scope,
                capability_scope=logical_scope,
                entity_id=capability_id,
                at_time=at_time,
                limit=1,
            )
            if not definition_rows:
                return None, [], []
            definition = definition_rows[0]
            revisions = repository.list_effective_entities(
                entity_type="revision",
                scope=scope,
                capability_scope=logical_scope,
                capability_id=capability_id,
                entity_id=revision_id,
                at_time=at_time,
                limit=limit,
            )
            bindings = repository.list_effective_entities(
                entity_type="binding",
                scope=scope,
                capability_scope=logical_scope,
                capability_id=capability_id,
                entity_id=binding_id,
                at_time=at_time,
                limit=limit,
            )
            return definition, revisions, bindings

        definition, revisions, bindings = self._store.read_capabilities(reader)
        if definition is None:
            return CapabilityResolution(
                capability_id=capability_id,
                capability_scope=logical_scope,
                at_time=at_time,
                ok=False,
                reason="capability_not_found",
                definition=None,
                revisions=(),
                bindings=(),
            )
        public_definition = _public_entity(definition)
        if definition.status != "active":
            return CapabilityResolution(
                capability_id=capability_id,
                capability_scope=logical_scope,
                at_time=at_time,
                ok=False,
                reason=f"definition_{definition.status}",
                definition=public_definition,
                revisions=(),
                bindings=(),
            )
        active_revisions = [item for item in revisions if item.status == "active"]
        active_revision_ids = {item.entity_id for item in active_revisions}
        active_bindings = [
            item
            for item in bindings
            if item.status == "active"
            and str(item.payload.get("capability_revision_id") or "") in active_revision_ids
        ]
        if binding_id:
            active_bindings = [item for item in active_bindings if item.entity_id == binding_id]
        if provider_kind:
            active_bindings = [
                item for item in active_bindings if str(item.payload.get("provider_kind") or "") == provider_kind
            ]
        if provider_instance_id:
            active_bindings = [
                item
                for item in active_bindings
                if str(item.payload.get("provider_instance_id") or "") == provider_instance_id
            ]
        if operation:
            active_bindings = [
                item for item in active_bindings if operation in tuple(item.payload.get("operations") or ())
            ]

        has_binding_selector = bool(binding_id or provider_kind or provider_instance_id or operation)
        if revision_id:
            active_revisions = [item for item in active_revisions if item.entity_id == revision_id]
        elif has_binding_selector:
            selected_by_binding = {
                str(item.payload.get("capability_revision_id") or "") for item in active_bindings
            }
            active_revisions = [item for item in active_revisions if item.entity_id in selected_by_binding]

        if not active_revisions:
            return CapabilityResolution(
                capability_id=capability_id,
                capability_scope=logical_scope,
                at_time=at_time,
                ok=False,
                reason="binding_unavailable" if has_binding_selector else "no_active_revision",
                definition=public_definition,
                revisions=(),
                bindings=(),
            )
        if not revision_id and len(active_revisions) != 1:
            return CapabilityResolution(
                capability_id=capability_id,
                capability_scope=logical_scope,
                at_time=at_time,
                ok=False,
                reason="ambiguous_active_revisions",
                definition=public_definition,
                revisions=tuple(_public_entity(item) for item in active_revisions),
                bindings=(),
            )
        selected_revisions = active_revisions
        selected_revision_ids = {item.entity_id for item in selected_revisions}
        active_bindings = [
            item
            for item in active_bindings
            if str(item.payload.get("capability_revision_id") or "") in selected_revision_ids
        ]
        if has_binding_selector and not active_bindings:
            return CapabilityResolution(
                capability_id=capability_id,
                capability_scope=logical_scope,
                at_time=at_time,
                ok=False,
                reason="binding_unavailable",
                definition=public_definition,
                revisions=tuple(_public_entity(item) for item in selected_revisions),
                bindings=(),
            )
        return CapabilityResolution(
            capability_id=capability_id,
            capability_scope=logical_scope,
            at_time=at_time,
            ok=True,
            reason="resolved",
            definition=public_definition,
            revisions=tuple(_public_entity(item) for item in selected_revisions),
            bindings=tuple(_public_entity(item) for item in active_bindings),
        )


__all__ = [
    "CapabilityRegistry",
    "CapabilityRegistryError",
    "CapabilityResolution",
    "MutationReceipt",
    "exact_runtime_scope",
]
