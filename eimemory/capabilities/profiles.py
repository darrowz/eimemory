"""Versioned, bounded resolution of dynamic capability profiles.

Profiles are declarative selection and readiness-requirement data.  This
module deliberately does *not* project observations or fabricate capability
maturity: later work packages own that evidence-to-state calculation.  Its
job is narrower and replayable: select the immutable profile revision that was
effective at a requested time, expand its exact/selector rules over active
registry descriptors, and retain enough digests and lifecycle watermarks for a
future projector to reproduce the decision.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, fields
from typing import Any, Mapping, TypeVar

from eimemory.capabilities.contracts import (
    CapabilityContractError,
    contract_digest,
    normalize_opaque_id,
    require_timestamp,
)
from eimemory.capabilities.models import (
    CapabilityBinding,
    CapabilityDefinition,
    CapabilityProfile,
    CapabilityRevision,
    legacy_profile_payload,
)
from eimemory.capabilities.registry import (
    CapabilityRegistryError,
    MutationReceipt,
    exact_runtime_scope,
)
from eimemory.models.records import ScopeRef
from eimemory.storage.capability_store import EffectiveCapabilityEntity
from eimemory.storage.runtime_store import RuntimeStore


_MAX_CANDIDATES = 499
_PROFILE_RESOLUTION_SCHEMA = "capability.profile_resolution.v1"
_REGISTRY_WATERMARK_SCHEMA = "capability.registry_watermark.v1"
_LIFECYCLE_WATERMARK_SCHEMA = "capability.lifecycle_watermark.v1"

_ContractT = TypeVar(
    "_ContractT",
    CapabilityDefinition,
    CapabilityRevision,
    CapabilityBinding,
    CapabilityProfile,
)


class CapabilityProfileError(CapabilityRegistryError):
    """A Profile cannot safely be registered or expanded at this boundary."""


@dataclass(frozen=True, slots=True)
class _LegacyProfileContract:
    """Read-only compatibility view for a pre-WP4 exact Profile descriptor."""

    profile_id: str
    profile_key: str
    requirements: Mapping[str, Any]
    scope: str
    revision: str
    profile_digest: str


def _logical_scope(value: object) -> str:
    try:
        return normalize_opaque_id(value, field="capability_scope")
    except CapabilityContractError as exc:
        raise CapabilityProfileError(str(exc)) from exc


def _bounded_limit(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise CapabilityProfileError("max_candidates must be an integer from 1 to 499")
    if not 1 <= value <= _MAX_CANDIDATES:
        raise CapabilityProfileError("max_candidates must be an integer from 1 to 499")
    return value


def _normalized_at_time(value: object) -> str:
    try:
        return require_timestamp(value, field="at_time", required=False)
    except CapabilityContractError as exc:
        raise CapabilityProfileError(str(exc)) from exc


def _descriptor_view(entity: EffectiveCapabilityEntity) -> dict[str, Any]:
    """Return a public DTO, never the SQLite row that produced it."""

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


def _contract_from_entity(
    entity: EffectiveCapabilityEntity,
    contract_type: type[_ContractT],
    *,
    digest_field: str,
) -> _ContractT | _LegacyProfileContract:
    """Revalidate persisted descriptor payloads before selector evaluation."""

    payload = dict(entity.payload)
    if contract_type is CapabilityProfile and "profile_key" not in payload:
        try:
            legacy_payload, legacy_digest = legacy_profile_payload(payload)
        except (TypeError, ValueError, CapabilityContractError) as exc:
            raise CapabilityProfileError(
                f"stored legacy profile {entity.entity_id!r} does not satisfy its typed contract"
            ) from exc
        if legacy_digest != entity.entity_digest:
            raise CapabilityProfileError(
                f"stored legacy profile {entity.entity_id!r} digest does not match its descriptor"
            )
        return _LegacyProfileContract(
            profile_id=str(legacy_payload["profile_id"]),
            profile_key=str(legacy_payload["profile_id"]),
            requirements=legacy_payload["requirements"],
            scope=str(legacy_payload["scope"]),
            revision=str(legacy_payload["revision"]),
            profile_digest=legacy_digest,
        )
    try:
        init_values = {
            item.name: payload[item.name]
            for item in fields(contract_type)
            if item.init and item.name in payload
        }
        contract = contract_type(**init_values)
    except (KeyError, TypeError, ValueError) as exc:
        raise CapabilityProfileError(
            f"stored {entity.entity_type} {entity.entity_id!r} does not satisfy its typed contract"
        ) from exc
    if str(getattr(contract, digest_field)) != entity.entity_digest:
        raise CapabilityProfileError(
            f"stored {entity.entity_type} {entity.entity_id!r} digest does not match its descriptor"
        )
    return contract


def _rule_requirement(raw: Mapping[str, Any]) -> dict[str, Any]:
    """Strip resolution-only fields from a declared requirement rule."""

    return {
        str(key): deepcopy(value)
        for key, value in raw.items()
        if key not in {"selector", "priority", "capability_id"}
    }


def _selector_priority(raw: Mapping[str, Any]) -> int:
    value = raw.get("priority", 0)
    # CapabilityProfile has already validated this, but keep the public
    # resolver fail-closed if an old/corrupted profile bypassed that contract.
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise CapabilityProfileError("profile selector priority must be a non-negative integer")
    return value


def _all_present(required: object, available: object) -> bool:
    return set(str(item) for item in required or ()).issubset(set(str(item) for item in available or ()))


def _any_present(required: object, available: object) -> bool:
    return bool(set(str(item) for item in required or ()).intersection(str(item) for item in available or ()))


def _selector_matches(
    selector: Mapping[str, Any],
    *,
    definition: CapabilityDefinition,
    effective_definition_status: str,
    revisions: tuple[CapabilityRevision, ...],
    bindings: tuple[CapabilityBinding, ...],
) -> tuple[bool, tuple[str, ...], tuple[str, ...]]:
    """Evaluate the constrained selector DSL against one semantic capability.

    When a selector includes both revision and binding predicates, the binding
    predicates are evaluated only over bindings of a matching revision.  That
    avoids falsely satisfying a mixed selector with facts from two unrelated
    implementations.
    """

    if "tags_all" in selector and not _all_present(selector["tags_all"], definition.tags):
        return False, (), ()
    if "tags_any" in selector and not _any_present(selector["tags_any"], definition.tags):
        return False, (), ()
    if "owners_any" in selector and definition.owner not in set(selector["owners_any"]):
        return False, (), ()
    if "risk_tiers_any" in selector and definition.risk_tier not in set(selector["risk_tiers_any"]):
        return False, (), ()
    # ``definition.status`` is the immutable descriptor's initial status.  A
    # selector must use the lifecycle-effective state that was actually queried
    # at this point in time, otherwise activating a discovered capability would
    # remain invisible forever.
    if "statuses_any" in selector and effective_definition_status not in set(selector["statuses_any"]):
        return False, (), ()

    matching_revisions = revisions
    if "revision_ids_any" in selector:
        accepted_revisions = set(str(item) for item in selector["revision_ids_any"])
        matching_revisions = tuple(item for item in revisions if item.revision_id in accepted_revisions)
        if not matching_revisions:
            return False, (), ()

    allowed_revision_ids = {item.revision_id for item in matching_revisions}
    matching_bindings = tuple(
        item for item in bindings if item.capability_revision_id in allowed_revision_ids
    )
    binding_predicates = {
        "provider_kinds_any",
        "provider_instance_ids_any",
        "operations_all",
        "operations_any",
    }
    if binding_predicates.intersection(selector):
        if "provider_kinds_any" in selector:
            kinds = set(str(item) for item in selector["provider_kinds_any"])
            matching_bindings = tuple(item for item in matching_bindings if item.provider_kind in kinds)
        if "provider_instance_ids_any" in selector:
            instances = set(str(item) for item in selector["provider_instance_ids_any"])
            matching_bindings = tuple(
                item for item in matching_bindings if item.provider_instance_id in instances
            )
        if "operations_all" in selector:
            matching_bindings = tuple(
                item for item in matching_bindings if _all_present(selector["operations_all"], item.operations)
            )
        if "operations_any" in selector:
            matching_bindings = tuple(
                item for item in matching_bindings if _any_present(selector["operations_any"], item.operations)
            )
        if not matching_bindings:
            return False, (), ()
        # A provider predicate selects *implementations*, not merely proof
        # that some implementation exists.  Keep only revisions represented
        # by a surviving binding so downstream readiness cannot accidentally
        # inherit this rule for a different provider/revision.
        selected_binding_revision_ids = {
            item.capability_revision_id for item in matching_bindings
        }
        matching_revisions = tuple(
            item for item in matching_revisions if item.revision_id in selected_binding_revision_ids
        )
        if not matching_revisions:
            return False, (), ()

    # A selector that only constrains definition fields applies to all active
    # revisions/bindings.  A revision-only selector narrows the revisions and
    # their associated bindings even when no provider predicate is present.
    if "revision_ids_any" in selector:
        selected_bindings = matching_bindings
    else:
        selected_bindings = matching_bindings if binding_predicates.intersection(selector) else bindings
    return (
        True,
        tuple(item.revision_id for item in matching_revisions),
        tuple(item.binding_id for item in selected_bindings),
    )


def _merged_selector_requirement(
    matches: list[tuple[str, Mapping[str, Any], tuple[str, ...], tuple[str, ...]]],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Choose highest-priority selector rules and fail on a semantic conflict."""

    highest = max(_selector_priority(rule) for _, rule, _, _ in matches)
    selected = [item for item in matches if _selector_priority(item[1]) == highest]
    selected.sort(key=lambda item: item[0])
    merged: dict[str, Any] = {}
    revision_ids: set[str] = set()
    binding_ids: set[str] = set()
    for rule_id, rule, matched_revisions, matched_bindings in selected:
        for key, value in _rule_requirement(rule).items():
            if key in merged and merged[key] != value:
                raise CapabilityProfileError(
                    "conflicting same-priority selector requirements for "
                    f"{rule_id!r} field {key!r}"
                )
            merged[key] = deepcopy(value)
        revision_ids.update(matched_revisions)
        binding_ids.update(matched_bindings)
    return merged, {
        "kind": "selector",
        "rule_ids": [item[0] for item in selected],
        "priority": highest,
        "matched_revision_ids": sorted(revision_ids),
        "matched_binding_ids": sorted(binding_ids),
    }


def _selection_for_candidate(
    requirements: Mapping[str, Any],
    *,
    definition: CapabilityDefinition,
    effective_definition_status: str,
    revisions: tuple[CapabilityRevision, ...],
    bindings: tuple[CapabilityBinding, ...],
) -> tuple[dict[str, Any], dict[str, Any]] | None:
    exact = requirements.get(definition.capability_id)
    if isinstance(exact, Mapping) and "selector" not in exact:
        return _rule_requirement(exact), {
            "kind": "exact",
            "rule_ids": [definition.capability_id],
            "priority": None,
            "matched_revision_ids": [item.revision_id for item in revisions],
            "matched_binding_ids": [item.binding_id for item in bindings],
        }

    selector_matches: list[tuple[str, Mapping[str, Any], tuple[str, ...], tuple[str, ...]]] = []
    for raw_rule_id, raw_rule in requirements.items():
        if not isinstance(raw_rule, Mapping) or "selector" not in raw_rule:
            continue
        raw_selector = raw_rule.get("selector")
        if not isinstance(raw_selector, Mapping):
            raise CapabilityProfileError(f"profile selector {raw_rule_id!r} is malformed")
        matched, revision_ids, binding_ids = _selector_matches(
            raw_selector,
            definition=definition,
            effective_definition_status=effective_definition_status,
            revisions=revisions,
            bindings=bindings,
        )
        if matched:
            selector_matches.append((str(raw_rule_id), raw_rule, revision_ids, binding_ids))
    if not selector_matches:
        return None
    return _merged_selector_requirement(selector_matches)


def _watermark_entity(entity: EffectiveCapabilityEntity) -> dict[str, Any]:
    return {
        "entity_type": entity.entity_type,
        "entity_id": entity.entity_id,
        "entity_digest": entity.entity_digest,
        "state_version": entity.state_version,
        "state_digest": entity.state_digest,
        "effective_at": entity.effective_at,
    }


class CapabilityProfiles:
    """Register and expand immutable readiness-profile revisions.

    The public result is a bounded JSON-compatible DTO.  It intentionally
    contains requirements and descriptor lineage, rather than a fabricated
    capability score or maturity.  A projector can later consume exactly this
    digestable input with observations and evidence it owns.
    """

    def __init__(self, store: RuntimeStore) -> None:
        self._store = store

    def register(
        self,
        profile: CapabilityProfile,
        *,
        runtime_scope: ScopeRef | Mapping[str, Any],
        request_key: str = "",
    ) -> MutationReceipt:
        """Persist one immutable profile revision through the domain mutation."""

        scope = exact_runtime_scope(runtime_scope)
        result = self._store.mutate_capabilities_atomically(
            lambda repository: repository.register_profile(profile, scope=scope, request_key=request_key)
        )
        return MutationReceipt.from_stored(result)

    def resolve(
        self,
        profile_key: str,
        *,
        runtime_scope: ScopeRef | Mapping[str, Any],
        capability_scope: str,
        at_time: str = "",
        max_candidates: int = 100,
    ) -> dict[str, Any]:
        """Resolve one effective profile and its active registry candidates.

        A live query accepts only an active profile.  Supplying ``at_time`` is
        an explicit historical read and can resolve a profile that was later
        deprecated or retired.  All entity reads remain exact-scope and bounded;
        a limit is never turned into a silently truncated readiness set.
        """

        try:
            normalized_key = normalize_opaque_id(profile_key, field="profile_key")
        except CapabilityContractError as exc:
            raise CapabilityProfileError(str(exc)) from exc
        scope = exact_runtime_scope(runtime_scope)
        logical_scope = _logical_scope(capability_scope)
        normalized_at_time = _normalized_at_time(at_time)
        limit = _bounded_limit(max_candidates)
        historical = bool(normalized_at_time)

        def reader(repository):
            profiles = repository.list_profile_revisions(
                profile_key=normalized_key,
                scope=scope,
                capability_scope=logical_scope,
                # An explicit historical read means active *at that time*,
                # not "return an arbitrary descriptor even if it had already
                # been retired".  The storage query evaluates lifecycle state
                # as-of ``at_time``, so this still correctly returns a
                # revision that was later retired.
                status="active",
                at_time=normalized_at_time,
                limit=1,
            )
            if not profiles:
                status_label = "effective historical" if historical else "active"
                raise CapabilityProfileError(
                    f"{status_label} profile {normalized_key!r} is not available in this exact scope"
                )
            profile_entity = profiles[0]
            profile = _contract_from_entity(
                profile_entity,
                CapabilityProfile,
                digest_field="profile_digest",
            )
            if profile.profile_id != profile_entity.entity_id:
                raise CapabilityProfileError("stored profile identity does not match its descriptor")
            if profile.profile_key != normalized_key:
                raise CapabilityProfileError("stored profile key does not match its lineage index")
            if profile.scope != logical_scope:
                raise CapabilityProfileError("stored profile capability scope does not match this request")

            requirements = dict(profile.requirements)
            exact_ids = sorted(
                str(rule_id)
                for rule_id, rule in requirements.items()
                if isinstance(rule, Mapping) and "selector" not in rule
            )
            has_selectors = any(
                isinstance(rule, Mapping) and "selector" in rule
                for rule in requirements.values()
            )
            definition_entities: dict[str, EffectiveCapabilityEntity] = {}
            if has_selectors:
                selector_definitions = repository.list_effective_entities(
                    entity_type="definition",
                    scope=scope,
                    capability_scope=logical_scope,
                    status="active",
                    at_time=normalized_at_time,
                    limit=limit + 1,
                )
                if len(selector_definitions) > limit:
                    raise CapabilityProfileError(
                        "active capability candidate count exceeds max_candidates; refusing truncation"
                    )
                definition_entities.update(
                    {item.entity_id: item for item in selector_definitions}
                )
            for capability_id in exact_ids:
                rows = repository.list_effective_entities(
                    entity_type="definition",
                    scope=scope,
                    capability_scope=logical_scope,
                    status="active",
                    at_time=normalized_at_time,
                    entity_id=capability_id,
                    limit=1,
                )
                if not rows:
                    raise CapabilityProfileError(
                        f"profile exact requirement {capability_id!r} has no active capability definition"
                    )
                definition_entities[capability_id] = rows[0]
            if len(definition_entities) > limit:
                raise CapabilityProfileError(
                    "resolved capability candidate count exceeds max_candidates; refusing truncation"
                )

            expanded: list[
                tuple[
                    EffectiveCapabilityEntity,
                    CapabilityDefinition,
                    tuple[tuple[EffectiveCapabilityEntity, CapabilityRevision], ...],
                    tuple[tuple[EffectiveCapabilityEntity, CapabilityBinding], ...],
                ]
            ] = []
            for capability_id, definition_entity in sorted(definition_entities.items()):
                definition = _contract_from_entity(
                    definition_entity,
                    CapabilityDefinition,
                    digest_field="definition_digest",
                )
                if definition.capability_id != capability_id or definition.scope != logical_scope:
                    raise CapabilityProfileError("stored definition identity or scope does not match this request")
                revision_entities = repository.list_effective_entities(
                    entity_type="revision",
                    scope=scope,
                    capability_scope=logical_scope,
                    status="active",
                    at_time=normalized_at_time,
                    capability_id=capability_id,
                    limit=limit + 1,
                )
                if len(revision_entities) > limit:
                    raise CapabilityProfileError(
                        f"active revisions for {capability_id!r} exceed max_candidates; refusing truncation"
                    )
                revisions: list[tuple[EffectiveCapabilityEntity, CapabilityRevision]] = []
                for revision_entity in revision_entities:
                    revision = _contract_from_entity(
                        revision_entity,
                        CapabilityRevision,
                        digest_field="contract_digest",
                    )
                    if revision.capability_id != capability_id or revision.scope != logical_scope:
                        raise CapabilityProfileError("stored revision identity or scope does not match its definition")
                    revisions.append((revision_entity, revision))

                binding_entities = repository.list_effective_entities(
                    entity_type="binding",
                    scope=scope,
                    capability_scope=logical_scope,
                    status="active",
                    at_time=normalized_at_time,
                    capability_id=capability_id,
                    limit=limit + 1,
                )
                if len(binding_entities) > limit:
                    raise CapabilityProfileError(
                        f"active bindings for {capability_id!r} exceed max_candidates; refusing truncation"
                    )
                active_revision_ids = {item.revision_id for _, item in revisions}
                bindings: list[tuple[EffectiveCapabilityEntity, CapabilityBinding]] = []
                for binding_entity in binding_entities:
                    binding = _contract_from_entity(
                        binding_entity,
                        CapabilityBinding,
                        digest_field="binding_digest",
                    )
                    if binding.capability_id != capability_id or binding.scope != logical_scope:
                        raise CapabilityProfileError("stored binding identity or scope does not match its definition")
                    # A still-active binding to a retired/deprecated revision is
                    # not an active readiness candidate.  Preserve no hidden
                    # fallback to its static descriptor state.
                    if binding.capability_revision_id in active_revision_ids:
                        bindings.append((binding_entity, binding))
                expanded.append((definition_entity, definition, tuple(revisions), tuple(bindings)))
            return profile_entity, profile, expanded

        profile_entity, profile, expanded = self._store.read_capabilities(reader)

        requirements: list[dict[str, Any]] = []
        watermark_entities: list[EffectiveCapabilityEntity] = [profile_entity]
        for definition_entity, definition, revision_pairs, binding_pairs in expanded:
            revisions = tuple(item for _, item in revision_pairs)
            bindings = tuple(item for _, item in binding_pairs)
            selection = _selection_for_candidate(
                profile.requirements,
                definition=definition,
                effective_definition_status=definition_entity.status,
                revisions=revisions,
                bindings=bindings,
            )
            if selection is None:
                continue
            requirement, selection_info = selection
            selected_revision_ids = set(selection_info["matched_revision_ids"])
            selected_binding_ids = set(selection_info["matched_binding_ids"])
            selected_revision_pairs = tuple(
                item for item in revision_pairs if item[1].revision_id in selected_revision_ids
            )
            selected_binding_pairs = tuple(
                item for item in binding_pairs if item[1].binding_id in selected_binding_ids
            )
            watermark_entities.append(definition_entity)
            watermark_entities.extend(item[0] for item in selected_revision_pairs)
            watermark_entities.extend(item[0] for item in selected_binding_pairs)
            requirements.append(
                {
                    "capability_id": definition.capability_id,
                    "requirement": requirement,
                    "selection": selection_info,
                    "definition": _descriptor_view(definition_entity),
                    "revisions": [_descriptor_view(item[0]) for item in selected_revision_pairs],
                    "bindings": [_descriptor_view(item[0]) for item in selected_binding_pairs],
                }
            )

        requirements.sort(key=lambda item: str(item["capability_id"]))
        watermark_entities.sort(
            key=lambda item: (item.entity_type, item.entity_id, item.state_version, item.state_digest)
        )
        registry_entries = [
            {
                "entity_type": item.entity_type,
                "entity_id": item.entity_id,
                "entity_digest": item.entity_digest,
            }
            for item in watermark_entities
        ]
        lifecycle_entries = [_watermark_entity(item) for item in watermark_entities]
        registry_watermark = contract_digest(
            {"schema": _REGISTRY_WATERMARK_SCHEMA, "entities": registry_entries}
        )
        lifecycle_watermark = contract_digest(
            {"schema": _LIFECYCLE_WATERMARK_SCHEMA, "entities": lifecycle_entries}
        )
        result = {
            "schema": _PROFILE_RESOLUTION_SCHEMA,
            "profile": {
                "profile_id": profile.profile_id,
                "profile_key": profile.profile_key,
                "profile_revision": profile.revision,
                "profile_digest": profile_entity.entity_digest,
                "status": profile_entity.status,
                "state_version": profile_entity.state_version,
                "state_digest": profile_entity.state_digest,
                "effective_at": profile_entity.effective_at,
            },
            "capability_scope": logical_scope,
            "at_time": normalized_at_time,
            "requirements": requirements,
            "registry_watermark": registry_watermark,
            "lifecycle_watermark": lifecycle_watermark,
        }
        result["resolution_digest"] = contract_digest(result)
        return result


__all__ = ["CapabilityProfileError", "CapabilityProfiles"]
