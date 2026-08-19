"""Validated, side-effect-free domain contracts for dynamic L5 capabilities.

The objects in this module deliberately describe facts rather than perform
registry, storage, or evaluation work.  Later work packages assign persistence
and projection ownership; these contracts make their inputs deterministic and
safe to replay.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any

from eimemory.capabilities.contracts import (
    CAPABILITY_SCHEMA_VERSION,
    CapabilityContractError,
    contract_digest,
    ensure_allowed,
    ensure_probability,
    ensure_timestamp_order,
    normalize_capability_id,
    normalize_json_payload,
    normalize_opaque_id,
    normalize_optional_text,
    normalize_sha256,
    normalize_string_sequence,
    normalize_text,
    require_timestamp,
)


DEFINITION_STATUSES = frozenset({"discovered", "active", "deprecated", "retired", "quarantined"})
REVISION_STATUSES = frozenset({"active", "deprecated", "retired", "quarantined"})
RELATION_TYPES = frozenset({"parent_of", "depends_on", "composes", "conflicts_with", "supersedes", "related_to"})
BINDING_STATUSES = frozenset({"active", "stale", "disabled", "deprecated", "quarantined"})
PROFILE_STATUSES = frozenset({"active", "deprecated", "retired"})
EVAL_SPEC_STATUSES = frozenset({"active", "deprecated", "retired", "quarantined"})
GRADER_TYPES = frozenset({"code", "schema_rule", "model"})
RUN_VERDICTS = frozenset({"pass", "fail", "blocked", "inconclusive", "stale", "invalid"})
OBSERVATION_VERDICTS = RUN_VERDICTS
KNOWLEDGE_LINK_TYPES = frozenset({"supports", "refutes", "informs_eval", "informs_change", "explains_outcome", "limits_applicability"})
KNOWLEDGE_SOURCE_STATUSES = frozenset({"active", "candidate", "needs_refresh", "conflicted", "stale", "unverified", "deprecated", "rejected", "blocked"})
KNOWLEDGE_APPLICABILITY = frozenset({"candidate", "applicable", "rejected", "blocked"})
KNOWLEDGE_TRUST_LEVELS = frozenset({"unverified", "low", "medium", "high"})
KNOWLEDGE_REVIEW_STATES = frozenset({"unreviewed", "reviewed", "approved", "rejected"})
KNOWLEDGE_CONTRADICTION_STATES = frozenset({"none", "contradicted", "resolved"})
MATURITY_STATES = frozenset({"unknown", "observed", "evaluated", "reliable", "regressed", "quarantined", "retired"})
LOOP_MATURITY_STATES = frozenset({"observing", "diagnosing", "experimenting", "evolving", "compounding"})
ADAPTER_READINESS_STATES = frozenset({"ready", "degraded", "blocked", "unknown", "not_ready"})
DEPLOYMENT_ASSURANCE_STATES = frozenset({"ready", "degraded", "blocked", "unknown", "not_evaluated"})
COMPATIBILITY_MODES = frozenset({"incompatible", "compatible"})
RISK_TIERS = frozenset({"low", "medium", "high", "critical", "bounded_read", "bounded_write"})
DEFINITION_RISK_TIERS = RISK_TIERS
SIDE_EFFECT_CLASSES = frozenset({"none", "internal_state", "local_io", "network", "external_mutation"})
REVISION_CONTRACT_KEYS = frozenset(
    {
        "input_schema",
        "output_schema",
        "observable_invariants",
        "success_semantics",
        "success_invariants",
        "failure_semantics",
        "failure_invariants",
        "evidence_requirements",
        "dependencies",
        "composition",
        "risk_tier",
        "side_effect_class",
    }
)


def _freeze_json(value: Any) -> Any:
    """Recursively freeze normalized JSON so cached digests cannot go stale."""

    if isinstance(value, Mapping):
        return MappingProxyType({key: _freeze_json(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze_json(item) for item in value)
    return value


def _thaw_json(value: Any) -> Any:
    """Return a regular JSON-compatible copy for public serialization."""

    if isinstance(value, Mapping):
        return {key: _thaw_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw_json(item) for item in value]
    return value


def _mapping(
    value: Mapping[str, Any] | None,
    *,
    field_name: str,
    executable: bool = False,
    required: bool = False,
) -> Mapping[str, Any]:
    if value is None:
        if required:
            raise CapabilityContractError(f"{field_name} is required")
        return MappingProxyType({})
    return _freeze_json(normalize_json_payload(value, field=field_name, reject_executable=executable))


def _evidence_refs(value: Sequence[object] | object, *, field_name: str = "evidence_refs") -> tuple[str, ...]:
    return normalize_string_sequence(value, field=field_name, item_field="evidence_ref")


def _optional_timestamp(value: object, *, field_name: str) -> str:
    return require_timestamp(value, field=field_name, required=False)


def _required_digest(value: object, *, field_name: str) -> str:
    return normalize_sha256(value, field=field_name)


def _nonempty_contract_part(contract: Mapping[str, Any], *keys: str) -> bool:
    return any(bool(contract.get(key)) for key in keys)


def _validate_revision_contract(contract: Mapping[str, Any]) -> None:
    unknown = set(contract).difference(REVISION_CONTRACT_KEYS)
    if unknown:
        raise CapabilityContractError(f"contract contains unsupported keys: {', '.join(sorted(unknown))}")
    has_schema = "input_schema" in contract and "output_schema" in contract
    has_invariants = _nonempty_contract_part(contract, "observable_invariants")
    if not has_schema and not has_invariants:
        raise CapabilityContractError("contract requires input/output schema or observable_invariants")
    if not _nonempty_contract_part(contract, "success_semantics", "success_invariants"):
        raise CapabilityContractError("contract requires success semantics")
    if not _nonempty_contract_part(contract, "failure_semantics", "failure_invariants"):
        raise CapabilityContractError("contract requires failure semantics")
    if "evidence_requirements" not in contract:
        raise CapabilityContractError("contract requires evidence_requirements")
    if not isinstance(contract["evidence_requirements"], Mapping):
        raise CapabilityContractError("contract.evidence_requirements must be an object")
    if not contract["evidence_requirements"]:
        raise CapabilityContractError("contract.evidence_requirements must not be empty")
    if has_schema:
        for field_name in ("input_schema", "output_schema"):
            if not isinstance(contract[field_name], Mapping):
                raise CapabilityContractError(f"contract.{field_name} must be an object")
    if has_invariants:
        invariants = contract["observable_invariants"]
        if isinstance(invariants, str) or not isinstance(invariants, Sequence):
            raise CapabilityContractError("contract.observable_invariants must be a sequence")
    if "dependencies" not in contract or "composition" not in contract:
        raise CapabilityContractError("contract requires dependencies and composition declarations")
    for field_name in ("dependencies", "composition"):
        value = contract[field_name]
        if isinstance(value, str) or not isinstance(value, Sequence):
            raise CapabilityContractError(f"contract.{field_name} must be a sequence")
    risk_tier = contract.get("risk_tier")
    side_effect_class = contract.get("side_effect_class")
    ensure_allowed(risk_tier, field="contract.risk_tier", allowed=RISK_TIERS)
    ensure_allowed(side_effect_class, field="contract.side_effect_class", allowed=SIDE_EFFECT_CLASSES)


def _validate_profile_requirements(value: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    if not value:
        raise CapabilityContractError("requirements must not be empty")
    normalized: dict[str, dict[str, Any]] = {}
    for raw_capability_id, raw_requirement in value.items():
        capability_id = normalize_capability_id(raw_capability_id, field="requirements capability_id")
        if capability_id in normalized:
            raise CapabilityContractError(f"duplicate requirement for {capability_id}")
        requirement = dict(
            _mapping(raw_requirement, field_name=f"requirements.{capability_id}", executable=True, required=True)
        )
        maturity = ensure_allowed(
            requirement.get("minimum_maturity"),
            field=f"requirements.{capability_id}.minimum_maturity",
            allowed=MATURITY_STATES,
        )
        if "min_pass_rate" in requirement:
            requirement["min_pass_rate"] = ensure_probability(
                requirement["min_pass_rate"], field=f"requirements.{capability_id}.min_pass_rate"
            )
        requirement["minimum_maturity"] = maturity
        normalized[capability_id] = requirement
    return normalized


def _validate_retry_policy(value: Mapping[str, Any]) -> Mapping[str, Any]:
    policy = _mapping(value, field_name="retry_policy", executable=True, required=True)
    attempts = policy.get("max_attempts")
    if isinstance(attempts, bool) or not isinstance(attempts, int) or not 1 <= attempts <= 32:
        raise CapabilityContractError("retry_policy.max_attempts must be an integer from 1 to 32")
    return policy


def _validate_stability_policy(value: Mapping[str, Any]) -> Mapping[str, Any]:
    policy = _mapping(value, field_name="stability_policy", executable=True, required=True)
    consecutive = policy.get("min_consecutive_passes")
    if isinstance(consecutive, bool) or not isinstance(consecutive, int) or not 1 <= consecutive <= 128:
        raise CapabilityContractError("stability_policy.min_consecutive_passes must be an integer from 1 to 128")
    return policy


def _validate_resource_budget(value: Mapping[str, Any]) -> Mapping[str, Any]:
    budget = _mapping(value, field_name="resource_budget", executable=True, required=True)
    allowed = {"timeout_seconds", "max_memory_mb", "max_artifact_bytes"}
    unknown = set(budget).difference(allowed)
    if unknown:
        raise CapabilityContractError(f"resource_budget contains unsupported keys: {', '.join(sorted(unknown))}")
    if not budget:
        raise CapabilityContractError("resource_budget must not be empty")
    for key, raw in budget.items():
        if isinstance(raw, bool) or not isinstance(raw, int) or raw < 1:
            raise CapabilityContractError(f"resource_budget.{key} must be a positive integer")
    return budget


def _validate_readiness(value: Mapping[str, Any], *, field_name: str, allowed: frozenset[str]) -> dict[str, str]:
    if not value:
        raise CapabilityContractError(f"{field_name} must not be empty")
    result: dict[str, str] = {}
    for raw_key, raw_state in value.items():
        key = normalize_opaque_id(raw_key, field=f"{field_name} key")
        if key in result:
            raise CapabilityContractError(f"{field_name} contains duplicate key: {key}")
        result[key] = ensure_allowed(raw_state, field=f"{field_name}.{key}", allowed=allowed)
    return result


def _validate_capability_readiness(value: Mapping[str, Any]) -> Mapping[str, Mapping[str, Mapping[str, Any]]]:
    """Validate ``revision -> (_revision | binding_id) -> state`` readiness.

    The explicit second key preserves multiple simultaneous adapter bindings for
    one semantic revision. ``_revision`` carries revision-wide state; every
    other key is a provider binding ID assigned by eimemory.
    """

    if not value:
        raise CapabilityContractError("capability_readiness must not be empty")
    result: dict[str, Mapping[str, Mapping[str, Any]]] = {}
    for raw_revision_id, raw_binding_states in value.items():
        revision_id = normalize_opaque_id(raw_revision_id, field="capability_readiness revision_id")
        if revision_id in result:
            raise CapabilityContractError(f"capability_readiness contains duplicate revision: {revision_id}")
        binding_states = _mapping(
            raw_binding_states,
            field_name=f"capability_readiness.{revision_id}",
            executable=True,
            required=True,
        )
        if not binding_states:
            raise CapabilityContractError(f"capability_readiness.{revision_id} must not be empty")
        normalized_bindings: dict[str, Mapping[str, Any]] = {}
        for raw_binding_key, raw_state in binding_states.items():
            if raw_binding_key == "_revision":
                binding_key = raw_binding_key
            else:
                binding_key = normalize_opaque_id(
                    raw_binding_key, field=f"capability_readiness.{revision_id} binding_id"
                )
            state = dict(
                _mapping(
                    raw_state,
                    field_name=f"capability_readiness.{revision_id}.{binding_key}",
                    executable=True,
                    required=True,
                )
            )
            state["maturity"] = ensure_allowed(
                state.get("maturity"),
                field=f"capability_readiness.{revision_id}.{binding_key}.maturity",
                allowed=MATURITY_STATES,
            )
            snapshot_id = normalize_optional_text(
                state.get("snapshot_id"),
                field=f"capability_readiness.{revision_id}.{binding_key}.snapshot_id",
                max_chars=512,
            )
            if not snapshot_id:
                raise CapabilityContractError(
                    f"capability_readiness.{revision_id}.{binding_key}.snapshot_id is required"
                )
            state["snapshot_id"] = normalize_opaque_id(
                snapshot_id, field=f"capability_readiness.{revision_id}.{binding_key}.snapshot_id"
            )
            normalized_bindings[binding_key] = _freeze_json(state)
        result[revision_id] = _freeze_json(normalized_bindings)
    return _freeze_json(result)


@dataclass(frozen=True, slots=True)
class CapabilityDefinition:
    capability_id: str
    display_name: str
    description: str
    owner: str
    created_at: str
    status: str = "active"
    scope: str = "global"
    risk_tier: str = "low"
    tags: Sequence[object] = ()
    supersedes: Sequence[object] = ()
    provenance: Mapping[str, Any] = field(default_factory=dict)
    evidence_refs: Sequence[object] = ()
    schema_version: str = CAPABILITY_SCHEMA_VERSION
    definition_digest: str = field(init=False)

    def __post_init__(self) -> None:
        capability_id = normalize_capability_id(self.capability_id)
        if isinstance(self.supersedes, str) or not isinstance(self.supersedes, Sequence):
            raise CapabilityContractError("supersedes must be a sequence of capability IDs")
        supersedes = tuple(
            normalize_capability_id(item, field="supersedes capability_id") for item in self.supersedes
        )
        if capability_id in supersedes:
            raise CapabilityContractError("capability definition cannot supersede itself")
        if len(set(supersedes)) != len(supersedes):
            raise CapabilityContractError("supersedes contains duplicate capability IDs")
        object.__setattr__(self, "capability_id", capability_id)
        object.__setattr__(self, "display_name", normalize_text(self.display_name, field="display_name"))
        object.__setattr__(self, "description", normalize_text(self.description, field="description"))
        object.__setattr__(self, "owner", normalize_text(self.owner, field="owner", max_chars=256))
        object.__setattr__(self, "created_at", require_timestamp(self.created_at, field="created_at"))
        object.__setattr__(self, "status", ensure_allowed(self.status, field="status", allowed=DEFINITION_STATUSES))
        object.__setattr__(self, "scope", normalize_opaque_id(self.scope, field="scope"))
        object.__setattr__(self, "risk_tier", ensure_allowed(self.risk_tier, field="risk_tier", allowed=DEFINITION_RISK_TIERS))
        object.__setattr__(self, "tags", normalize_string_sequence(self.tags, field="tags", item_field="tag"))
        object.__setattr__(self, "supersedes", tuple(sorted(supersedes)))
        object.__setattr__(self, "provenance", _mapping(self.provenance, field_name="provenance", executable=True))
        object.__setattr__(self, "evidence_refs", _evidence_refs(self.evidence_refs))
        object.__setattr__(self, "schema_version", normalize_opaque_id(self.schema_version, field="schema_version"))
        object.__setattr__(self, "definition_digest", contract_digest(self.to_dict(include_digest=False)))

    @property
    def capability_identity(self) -> str:
        """The semantic identity; never derived from provider or environment."""

        return self.capability_id

    def to_dict(self, *, include_digest: bool = True) -> dict[str, Any]:
        result = {
            "capability_id": self.capability_id,
            "display_name": self.display_name,
            "description": self.description,
            "owner": self.owner,
            "created_at": self.created_at,
            "status": self.status,
            "scope": self.scope,
            "risk_tier": self.risk_tier,
            "tags": list(self.tags),
            "supersedes": list(self.supersedes),
            "provenance": _thaw_json(self.provenance),
            "evidence_refs": list(self.evidence_refs),
            "schema_version": self.schema_version,
        }
        if include_digest:
            result["definition_digest"] = self.definition_digest
        return result


@dataclass(frozen=True, slots=True)
class CapabilityRevision:
    revision_id: str
    capability_id: str
    contract: Mapping[str, Any]
    compatibility: str
    created_at: str
    status: str = "active"
    scope: str = "global"
    supersedes_revision_id: str = ""
    compatibility_policy_id: str = ""
    compatibility_policy_digest: str = ""
    provenance: Mapping[str, Any] = field(default_factory=dict)
    evidence_refs: Sequence[object] = ()
    schema_version: str = CAPABILITY_SCHEMA_VERSION
    contract_digest: str = field(init=False)

    def __post_init__(self) -> None:
        revision_id = normalize_opaque_id(self.revision_id, field="revision_id")
        contract = _mapping(self.contract, field_name="contract", executable=True, required=True)
        _validate_revision_contract(contract)
        compatibility = ensure_allowed(self.compatibility, field="compatibility", allowed=COMPATIBILITY_MODES)
        supersedes_revision_id = normalize_optional_text(
            self.supersedes_revision_id, field="supersedes_revision_id", max_chars=512
        )
        if supersedes_revision_id:
            supersedes_revision_id = normalize_opaque_id(supersedes_revision_id, field="supersedes_revision_id")
        policy_id = normalize_optional_text(self.compatibility_policy_id, field="compatibility_policy_id", max_chars=512)
        if policy_id:
            policy_id = normalize_opaque_id(policy_id, field="compatibility_policy_id")
        policy_digest = normalize_optional_text(
            self.compatibility_policy_digest, field="compatibility_policy_digest", max_chars=64
        )
        if policy_digest:
            policy_digest = normalize_sha256(policy_digest, field="compatibility_policy_digest")
        if compatibility == "compatible":
            if not supersedes_revision_id or not policy_id or not policy_digest:
                raise CapabilityContractError(
                    "compatible revision requires supersedes_revision_id and compatibility policy id/digest"
                )
            if supersedes_revision_id == revision_id:
                raise CapabilityContractError("capability revision cannot supersede itself")
        elif supersedes_revision_id or policy_id or policy_digest:
            raise CapabilityContractError("incompatible revision cannot declare compatibility inheritance")
        object.__setattr__(self, "revision_id", revision_id)
        object.__setattr__(self, "capability_id", normalize_capability_id(self.capability_id))
        object.__setattr__(self, "contract", contract)
        object.__setattr__(self, "compatibility", compatibility)
        object.__setattr__(self, "created_at", require_timestamp(self.created_at, field="created_at"))
        object.__setattr__(self, "status", ensure_allowed(self.status, field="status", allowed=REVISION_STATUSES))
        object.__setattr__(self, "scope", normalize_opaque_id(self.scope, field="scope"))
        object.__setattr__(self, "supersedes_revision_id", supersedes_revision_id)
        object.__setattr__(self, "compatibility_policy_id", policy_id)
        object.__setattr__(self, "compatibility_policy_digest", policy_digest)
        object.__setattr__(self, "provenance", _mapping(self.provenance, field_name="provenance", executable=True))
        object.__setattr__(self, "evidence_refs", _evidence_refs(self.evidence_refs))
        object.__setattr__(self, "schema_version", normalize_opaque_id(self.schema_version, field="schema_version"))
        object.__setattr__(self, "contract_digest", contract_digest(contract))

    def to_dict(self, *, include_digest: bool = True) -> dict[str, Any]:
        result = {
            "revision_id": self.revision_id,
            "capability_id": self.capability_id,
            "contract": _thaw_json(self.contract),
            "compatibility": self.compatibility,
            "created_at": self.created_at,
            "status": self.status,
            "scope": self.scope,
            "supersedes_revision_id": self.supersedes_revision_id,
            "compatibility_policy_id": self.compatibility_policy_id,
            "compatibility_policy_digest": self.compatibility_policy_digest,
            "provenance": _thaw_json(self.provenance),
            "evidence_refs": list(self.evidence_refs),
            "schema_version": self.schema_version,
        }
        if include_digest:
            result["contract_digest"] = self.contract_digest
        return result


@dataclass(frozen=True, slots=True)
class CapabilityRelation:
    source_capability_id: str
    target_capability_id: str
    relation_type: str
    created_at: str
    relation_policy: Mapping[str, Any] = field(default_factory=dict)
    relation_id: str = ""
    scope: str = "global"
    status: str = "active"
    provenance: Mapping[str, Any] = field(default_factory=dict)
    evidence_refs: Sequence[object] = ()
    relation_digest: str = field(init=False)

    def __post_init__(self) -> None:
        source = normalize_capability_id(self.source_capability_id, field="source_capability_id")
        target = normalize_capability_id(self.target_capability_id, field="target_capability_id")
        if source == target:
            raise CapabilityContractError("capability relation cannot be a self relation")
        relation_type = ensure_allowed(self.relation_type, field="relation_type", allowed=RELATION_TYPES)
        relation_policy = _mapping(self.relation_policy, field_name="relation_policy", executable=True)
        if relation_type in {"depends_on", "composes"} and not relation_policy:
            raise CapabilityContractError(f"{relation_type} relation requires a declarative relation_policy")
        relation_id = normalize_optional_text(self.relation_id, field="relation_id", max_chars=512)
        if not relation_id:
            relation_id = f"{source}:{relation_type}:{target}"
        object.__setattr__(self, "source_capability_id", source)
        object.__setattr__(self, "target_capability_id", target)
        object.__setattr__(self, "relation_type", relation_type)
        object.__setattr__(self, "relation_policy", relation_policy)
        object.__setattr__(self, "relation_id", normalize_opaque_id(relation_id, field="relation_id"))
        object.__setattr__(self, "scope", normalize_opaque_id(self.scope, field="scope"))
        object.__setattr__(self, "status", ensure_allowed(self.status, field="status", allowed=DEFINITION_STATUSES))
        object.__setattr__(self, "provenance", _mapping(self.provenance, field_name="provenance", executable=True))
        object.__setattr__(self, "evidence_refs", _evidence_refs(self.evidence_refs))
        object.__setattr__(self, "created_at", require_timestamp(self.created_at, field="created_at"))
        object.__setattr__(self, "relation_digest", contract_digest(self.to_dict(include_digest=False)))

    def to_dict(self, *, include_digest: bool = True) -> dict[str, Any]:
        result = {
            "relation_id": self.relation_id,
            "source_capability_id": self.source_capability_id,
            "target_capability_id": self.target_capability_id,
            "relation_type": self.relation_type,
            "relation_policy": _thaw_json(self.relation_policy),
            "scope": self.scope,
            "status": self.status,
            "provenance": _thaw_json(self.provenance),
            "evidence_refs": list(self.evidence_refs),
            "created_at": self.created_at,
        }
        if include_digest:
            result["relation_digest"] = self.relation_digest
        return result


@dataclass(frozen=True, slots=True)
class CapabilityBinding:
    binding_id: str
    capability_id: str
    capability_revision_id: str
    provider_kind: str
    provider_instance_id: str
    implementation_digest: str
    operations: Sequence[object]
    limits: Mapping[str, Any]
    environment_fingerprint: Mapping[str, Any]
    created_at: str
    status: str = "active"
    scope: str = "global"
    advertised_at: str = ""
    applicability: Mapping[str, Any] = field(default_factory=dict)
    advertisement_evidence_refs: Sequence[object] = ()
    provenance: Mapping[str, Any] = field(default_factory=dict)
    binding_digest: str = field(init=False)

    def __post_init__(self) -> None:
        created_at = require_timestamp(self.created_at, field="created_at")
        advertised_at = _optional_timestamp(self.advertised_at, field_name="advertised_at") or created_at
        operations = normalize_string_sequence(self.operations, field="operations", item_field="operation")
        if not operations:
            raise CapabilityContractError("operations must not be empty")
        limits = _mapping(self.limits, field_name="limits", executable=True, required=True)
        if not limits:
            raise CapabilityContractError("limits must not be empty")
        environment_fingerprint = _mapping(
            self.environment_fingerprint, field_name="environment_fingerprint", executable=True, required=True
        )
        if not environment_fingerprint:
            raise CapabilityContractError("environment_fingerprint must not be empty")
        applicability = _mapping(self.applicability, field_name="applicability", executable=True, required=True)
        if not applicability:
            raise CapabilityContractError("applicability must not be empty")
        advertisement_evidence_refs = _evidence_refs(
            self.advertisement_evidence_refs, field_name="advertisement_evidence_refs"
        )
        if not advertisement_evidence_refs:
            raise CapabilityContractError("advertisement_evidence_refs must not be empty")
        object.__setattr__(self, "binding_id", normalize_opaque_id(self.binding_id, field="binding_id"))
        object.__setattr__(self, "capability_id", normalize_capability_id(self.capability_id))
        object.__setattr__(self, "capability_revision_id", normalize_opaque_id(self.capability_revision_id, field="capability_revision_id"))
        object.__setattr__(self, "provider_kind", normalize_opaque_id(self.provider_kind, field="provider_kind"))
        object.__setattr__(self, "provider_instance_id", normalize_opaque_id(self.provider_instance_id, field="provider_instance_id"))
        object.__setattr__(self, "implementation_digest", _required_digest(self.implementation_digest, field_name="implementation_digest"))
        object.__setattr__(self, "operations", operations)
        object.__setattr__(self, "limits", limits)
        object.__setattr__(self, "environment_fingerprint", environment_fingerprint)
        object.__setattr__(self, "created_at", created_at)
        object.__setattr__(self, "status", ensure_allowed(self.status, field="status", allowed=BINDING_STATUSES))
        object.__setattr__(self, "scope", normalize_opaque_id(self.scope, field="scope"))
        object.__setattr__(self, "advertised_at", advertised_at)
        object.__setattr__(self, "applicability", applicability)
        object.__setattr__(self, "advertisement_evidence_refs", advertisement_evidence_refs)
        object.__setattr__(self, "provenance", _mapping(self.provenance, field_name="provenance", executable=True))
        object.__setattr__(self, "binding_digest", contract_digest(self.to_dict(include_digest=False)))

    @property
    def capability_identity(self) -> str:
        return self.capability_id

    def to_dict(self, *, include_digest: bool = True) -> dict[str, Any]:
        result = {
            "binding_id": self.binding_id,
            "capability_id": self.capability_id,
            "capability_revision_id": self.capability_revision_id,
            "provider_kind": self.provider_kind,
            "provider_instance_id": self.provider_instance_id,
            "implementation_digest": self.implementation_digest,
            "operations": list(self.operations),
            "limits": _thaw_json(self.limits),
            "environment_fingerprint": _thaw_json(self.environment_fingerprint),
            "created_at": self.created_at,
            "status": self.status,
            "scope": self.scope,
            "advertised_at": self.advertised_at,
            "applicability": _thaw_json(self.applicability),
            "advertisement_evidence_refs": list(self.advertisement_evidence_refs),
            "provenance": _thaw_json(self.provenance),
        }
        if include_digest:
            result["binding_digest"] = self.binding_digest
        return result


@dataclass(frozen=True, slots=True)
class CapabilityProfile:
    profile_id: str
    requirements: Mapping[str, Any]
    created_at: str
    status: str = "active"
    scope: str = "global"
    revision: str = "v1"
    provenance: Mapping[str, Any] = field(default_factory=dict)
    profile_digest: str = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "profile_id", normalize_opaque_id(self.profile_id, field="profile_id"))
        object.__setattr__(self, "requirements", _freeze_json(_validate_profile_requirements(self.requirements)))
        object.__setattr__(self, "created_at", require_timestamp(self.created_at, field="created_at"))
        object.__setattr__(self, "status", ensure_allowed(self.status, field="status", allowed=PROFILE_STATUSES))
        object.__setattr__(self, "scope", normalize_opaque_id(self.scope, field="scope"))
        object.__setattr__(self, "revision", normalize_opaque_id(self.revision, field="revision"))
        object.__setattr__(self, "provenance", _mapping(self.provenance, field_name="provenance", executable=True))
        object.__setattr__(self, "profile_digest", contract_digest(self.to_dict(include_digest=False)))

    def to_dict(self, *, include_digest: bool = True) -> dict[str, Any]:
        result = {
            "profile_id": self.profile_id,
            "requirements": _thaw_json(self.requirements),
            "created_at": self.created_at,
            "status": self.status,
            "scope": self.scope,
            "revision": self.revision,
            "provenance": _thaw_json(self.provenance),
        }
        if include_digest:
            result["profile_digest"] = self.profile_digest
        return result


@dataclass(frozen=True, slots=True)
class EvaluationSpec:
    eval_spec_id: str
    capability_id: str
    capability_revision_id: str
    grader_type: str
    executor_id: str
    executor_contract_digest: str
    fixture_refs: Sequence[object]
    checks: Sequence[object]
    required_metrics: Sequence[object]
    retry_policy: Mapping[str, Any]
    stability_policy: Mapping[str, Any]
    applicability: Mapping[str, Any]
    resource_budget: Mapping[str, Any]
    provenance: Mapping[str, Any]
    created_at: str
    binding_selector: Mapping[str, Any] = field(default_factory=dict)
    model_grader_policy: Mapping[str, Any] = field(default_factory=dict)
    status: str = "active"
    scope: str = "global"
    revision: str = "v1"
    spec_digest: str = field(init=False)

    def __post_init__(self) -> None:
        grader_type = ensure_allowed(self.grader_type, field="grader_type", allowed=GRADER_TYPES)
        checks = normalize_string_sequence(self.checks, field="checks", item_field="check")
        if not checks:
            raise CapabilityContractError("checks must not be empty")
        required_metrics = normalize_string_sequence(
            self.required_metrics, field="required_metrics", item_field="required_metric"
        )
        if not required_metrics:
            raise CapabilityContractError("required_metrics must not be empty")
        model_grader_policy = dict(
            _mapping(self.model_grader_policy, field_name="model_grader_policy", executable=True)
        )
        if grader_type == "model":
            max_tokens = model_grader_policy.get("max_tokens")
            if isinstance(max_tokens, bool) or not isinstance(max_tokens, int) or not 1 <= max_tokens <= 16_384:
                raise CapabilityContractError("model_grader_policy.max_tokens must be an integer from 1 to 16384")
            tie_breaker = normalize_optional_text(
                model_grader_policy.get("tie_breaker"), field="model_grader_policy.tie_breaker", max_chars=512
            )
            has_tie_breaker = bool(tie_breaker)
            if not has_tie_breaker and model_grader_policy.get("fail_closed") is not True:
                raise CapabilityContractError("model grader requires deterministic tie_breaker or fail_closed")
            if tie_breaker:
                model_grader_policy["tie_breaker"] = normalize_opaque_id(
                    tie_breaker, field="model_grader_policy.tie_breaker"
                )
        elif model_grader_policy:
            raise CapabilityContractError("model_grader_policy is only valid for grader_type=model")
        object.__setattr__(self, "eval_spec_id", normalize_opaque_id(self.eval_spec_id, field="eval_spec_id"))
        object.__setattr__(self, "capability_id", normalize_capability_id(self.capability_id))
        object.__setattr__(self, "capability_revision_id", normalize_opaque_id(self.capability_revision_id, field="capability_revision_id"))
        object.__setattr__(self, "grader_type", grader_type)
        object.__setattr__(self, "executor_id", normalize_opaque_id(self.executor_id, field="executor_id"))
        object.__setattr__(self, "executor_contract_digest", _required_digest(self.executor_contract_digest, field_name="executor_contract_digest"))
        object.__setattr__(self, "fixture_refs", _evidence_refs(self.fixture_refs, field_name="fixture_refs"))
        object.__setattr__(self, "checks", checks)
        object.__setattr__(self, "required_metrics", required_metrics)
        object.__setattr__(self, "retry_policy", _validate_retry_policy(self.retry_policy))
        object.__setattr__(self, "stability_policy", _validate_stability_policy(self.stability_policy))
        applicability = _mapping(self.applicability, field_name="applicability", executable=True, required=True)
        if not applicability:
            raise CapabilityContractError("applicability must not be empty")
        object.__setattr__(self, "applicability", applicability)
        object.__setattr__(self, "resource_budget", _validate_resource_budget(self.resource_budget))
        object.__setattr__(self, "provenance", _mapping(self.provenance, field_name="provenance", executable=True, required=True))
        object.__setattr__(self, "created_at", require_timestamp(self.created_at, field="created_at"))
        object.__setattr__(self, "binding_selector", _mapping(self.binding_selector, field_name="binding_selector", executable=True))
        object.__setattr__(self, "model_grader_policy", _freeze_json(model_grader_policy))
        object.__setattr__(self, "status", ensure_allowed(self.status, field="status", allowed=EVAL_SPEC_STATUSES))
        object.__setattr__(self, "scope", normalize_opaque_id(self.scope, field="scope"))
        object.__setattr__(self, "revision", normalize_opaque_id(self.revision, field="revision"))
        object.__setattr__(self, "spec_digest", contract_digest(self.to_dict(include_digest=False)))

    def to_dict(self, *, include_digest: bool = True) -> dict[str, Any]:
        result = {
            "eval_spec_id": self.eval_spec_id,
            "capability_id": self.capability_id,
            "capability_revision_id": self.capability_revision_id,
            "grader_type": self.grader_type,
            "executor_id": self.executor_id,
            "executor_contract_digest": self.executor_contract_digest,
            "fixture_refs": list(self.fixture_refs),
            "checks": list(self.checks),
            "required_metrics": list(self.required_metrics),
            "retry_policy": _thaw_json(self.retry_policy),
            "stability_policy": _thaw_json(self.stability_policy),
            "applicability": _thaw_json(self.applicability),
            "resource_budget": _thaw_json(self.resource_budget),
            "provenance": _thaw_json(self.provenance),
            "created_at": self.created_at,
            "binding_selector": _thaw_json(self.binding_selector),
            "model_grader_policy": _thaw_json(self.model_grader_policy),
            "status": self.status,
            "scope": self.scope,
            "revision": self.revision,
        }
        if include_digest:
            result["spec_digest"] = self.spec_digest
        return result


@dataclass(frozen=True, slots=True)
class EvaluationRun:
    run_id: str
    eval_spec_id: str
    capability_id: str
    capability_revision_id: str
    provider_binding_id: str
    idempotency_key: str
    verdict: str
    source: str
    executor_id: str
    executor_contract_digest: str
    grader_id: str
    grader_revision: str
    input_digest: str
    output_digest: str
    evidence_digest: str
    evidence_refs: Sequence[object]
    environment_fingerprint: Mapping[str, Any]
    provenance: Mapping[str, Any]
    metrics: Mapping[str, Any]
    error_taxonomy: Mapping[str, Any]
    started_at: str
    finished_at: str
    scope: str = "global"
    deployment_authority: Mapping[str, Any] = field(default_factory=dict)
    run_digest: str = field(init=False)

    def __post_init__(self) -> None:
        started_at = require_timestamp(self.started_at, field="started_at")
        finished_at = require_timestamp(self.finished_at, field="finished_at")
        ensure_timestamp_order(started_at, finished_at, earlier_field="started_at", later_field="finished_at")
        verdict = ensure_allowed(self.verdict, field="verdict", allowed=RUN_VERDICTS)
        metrics = _mapping(self.metrics, field_name="metrics", executable=True, required=True)
        if not metrics:
            raise CapabilityContractError("metrics must not be empty")
        error_taxonomy = _mapping(self.error_taxonomy, field_name="error_taxonomy", executable=True, required=True)
        if verdict != "pass" and not error_taxonomy:
            raise CapabilityContractError("non-pass evaluation run requires error_taxonomy")
        environment_fingerprint = _mapping(
            self.environment_fingerprint, field_name="environment_fingerprint", executable=True, required=True
        )
        provenance = _mapping(self.provenance, field_name="provenance", executable=True, required=True)
        if not environment_fingerprint or not provenance:
            raise CapabilityContractError("environment_fingerprint and provenance must not be empty")
        object.__setattr__(self, "run_id", normalize_opaque_id(self.run_id, field="run_id"))
        object.__setattr__(self, "eval_spec_id", normalize_opaque_id(self.eval_spec_id, field="eval_spec_id"))
        object.__setattr__(self, "capability_id", normalize_capability_id(self.capability_id))
        object.__setattr__(self, "capability_revision_id", normalize_opaque_id(self.capability_revision_id, field="capability_revision_id"))
        object.__setattr__(self, "provider_binding_id", normalize_opaque_id(self.provider_binding_id, field="provider_binding_id"))
        object.__setattr__(self, "idempotency_key", normalize_opaque_id(self.idempotency_key, field="idempotency_key"))
        object.__setattr__(self, "verdict", verdict)
        object.__setattr__(self, "source", normalize_opaque_id(self.source, field="source"))
        object.__setattr__(self, "executor_id", normalize_opaque_id(self.executor_id, field="executor_id"))
        object.__setattr__(self, "executor_contract_digest", _required_digest(self.executor_contract_digest, field_name="executor_contract_digest"))
        object.__setattr__(self, "grader_id", normalize_opaque_id(self.grader_id, field="grader_id"))
        object.__setattr__(self, "grader_revision", normalize_opaque_id(self.grader_revision, field="grader_revision"))
        object.__setattr__(self, "input_digest", _required_digest(self.input_digest, field_name="input_digest"))
        object.__setattr__(self, "output_digest", _required_digest(self.output_digest, field_name="output_digest"))
        object.__setattr__(self, "evidence_digest", _required_digest(self.evidence_digest, field_name="evidence_digest"))
        evidence_refs = _evidence_refs(self.evidence_refs)
        if not evidence_refs:
            raise CapabilityContractError("evidence_refs must not be empty")
        object.__setattr__(self, "evidence_refs", evidence_refs)
        object.__setattr__(self, "environment_fingerprint", environment_fingerprint)
        object.__setattr__(self, "provenance", provenance)
        object.__setattr__(self, "metrics", metrics)
        object.__setattr__(self, "error_taxonomy", error_taxonomy)
        object.__setattr__(self, "started_at", started_at)
        object.__setattr__(self, "finished_at", finished_at)
        object.__setattr__(self, "scope", normalize_opaque_id(self.scope, field="scope"))
        object.__setattr__(self, "deployment_authority", _mapping(self.deployment_authority, field_name="deployment_authority", executable=True))
        object.__setattr__(self, "run_digest", contract_digest(self.to_dict(include_digest=False)))

    def to_dict(self, *, include_digest: bool = True) -> dict[str, Any]:
        result = {
            "run_id": self.run_id,
            "eval_spec_id": self.eval_spec_id,
            "capability_id": self.capability_id,
            "capability_revision_id": self.capability_revision_id,
            "provider_binding_id": self.provider_binding_id,
            "idempotency_key": self.idempotency_key,
            "verdict": self.verdict,
            "source": self.source,
            "executor_id": self.executor_id,
            "executor_contract_digest": self.executor_contract_digest,
            "grader_id": self.grader_id,
            "grader_revision": self.grader_revision,
            "input_digest": self.input_digest,
            "output_digest": self.output_digest,
            "evidence_digest": self.evidence_digest,
            "evidence_refs": list(self.evidence_refs),
            "environment_fingerprint": _thaw_json(self.environment_fingerprint),
            "provenance": _thaw_json(self.provenance),
            "metrics": _thaw_json(self.metrics),
            "error_taxonomy": _thaw_json(self.error_taxonomy),
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "scope": self.scope,
            "deployment_authority": _thaw_json(self.deployment_authority),
        }
        if include_digest:
            result["run_digest"] = self.run_digest
        return result


@dataclass(frozen=True, slots=True)
class CapabilityObservation:
    observation_id: str
    capability_id: str
    capability_revision_id: str
    provider_binding_id: str
    idempotency_key: str
    verdict: str
    source: str
    executor_id: str
    executor_contract_digest: str
    grader_id: str
    grader_revision: str
    input_digest: str
    output_digest: str
    evidence_digest: str
    evidence_refs: Sequence[object]
    environment_fingerprint: Mapping[str, Any]
    provenance: Mapping[str, Any]
    metrics: Mapping[str, Any]
    error_taxonomy: Mapping[str, Any]
    observed_at: str
    scope: str = "global"
    deployment_authority: Mapping[str, Any] = field(default_factory=dict)
    observation_digest: str = field(init=False)

    def __post_init__(self) -> None:
        verdict = ensure_allowed(self.verdict, field="verdict", allowed=OBSERVATION_VERDICTS)
        metrics = _mapping(self.metrics, field_name="metrics", executable=True, required=True)
        if not metrics:
            raise CapabilityContractError("metrics must not be empty")
        error_taxonomy = _mapping(self.error_taxonomy, field_name="error_taxonomy", executable=True, required=True)
        if verdict != "pass" and not error_taxonomy:
            raise CapabilityContractError("non-pass observation requires error_taxonomy")
        environment_fingerprint = _mapping(
            self.environment_fingerprint, field_name="environment_fingerprint", executable=True, required=True
        )
        provenance = _mapping(self.provenance, field_name="provenance", executable=True, required=True)
        if not environment_fingerprint or not provenance:
            raise CapabilityContractError("environment_fingerprint and provenance must not be empty")
        object.__setattr__(self, "observation_id", normalize_opaque_id(self.observation_id, field="observation_id"))
        object.__setattr__(self, "capability_id", normalize_capability_id(self.capability_id))
        object.__setattr__(self, "capability_revision_id", normalize_opaque_id(self.capability_revision_id, field="capability_revision_id"))
        object.__setattr__(self, "provider_binding_id", normalize_opaque_id(self.provider_binding_id, field="provider_binding_id"))
        object.__setattr__(self, "idempotency_key", normalize_opaque_id(self.idempotency_key, field="idempotency_key"))
        object.__setattr__(self, "verdict", verdict)
        object.__setattr__(self, "source", normalize_opaque_id(self.source, field="source"))
        object.__setattr__(self, "executor_id", normalize_opaque_id(self.executor_id, field="executor_id"))
        object.__setattr__(self, "executor_contract_digest", _required_digest(self.executor_contract_digest, field_name="executor_contract_digest"))
        object.__setattr__(self, "grader_id", normalize_opaque_id(self.grader_id, field="grader_id"))
        object.__setattr__(self, "grader_revision", normalize_opaque_id(self.grader_revision, field="grader_revision"))
        object.__setattr__(self, "input_digest", _required_digest(self.input_digest, field_name="input_digest"))
        object.__setattr__(self, "output_digest", _required_digest(self.output_digest, field_name="output_digest"))
        object.__setattr__(self, "evidence_digest", _required_digest(self.evidence_digest, field_name="evidence_digest"))
        evidence_refs = _evidence_refs(self.evidence_refs)
        if not evidence_refs:
            raise CapabilityContractError("evidence_refs must not be empty")
        object.__setattr__(self, "evidence_refs", evidence_refs)
        object.__setattr__(self, "environment_fingerprint", environment_fingerprint)
        object.__setattr__(self, "provenance", provenance)
        object.__setattr__(self, "metrics", metrics)
        object.__setattr__(self, "error_taxonomy", error_taxonomy)
        object.__setattr__(self, "observed_at", require_timestamp(self.observed_at, field="observed_at"))
        object.__setattr__(self, "scope", normalize_opaque_id(self.scope, field="scope"))
        object.__setattr__(self, "deployment_authority", _mapping(self.deployment_authority, field_name="deployment_authority", executable=True))
        object.__setattr__(self, "observation_digest", contract_digest(self.to_dict(include_digest=False)))

    def to_dict(self, *, include_digest: bool = True) -> dict[str, Any]:
        result = {
            "observation_id": self.observation_id,
            "capability_id": self.capability_id,
            "capability_revision_id": self.capability_revision_id,
            "provider_binding_id": self.provider_binding_id,
            "idempotency_key": self.idempotency_key,
            "verdict": self.verdict,
            "source": self.source,
            "executor_id": self.executor_id,
            "executor_contract_digest": self.executor_contract_digest,
            "grader_id": self.grader_id,
            "grader_revision": self.grader_revision,
            "input_digest": self.input_digest,
            "output_digest": self.output_digest,
            "evidence_digest": self.evidence_digest,
            "evidence_refs": list(self.evidence_refs),
            "environment_fingerprint": _thaw_json(self.environment_fingerprint),
            "provenance": _thaw_json(self.provenance),
            "metrics": _thaw_json(self.metrics),
            "error_taxonomy": _thaw_json(self.error_taxonomy),
            "observed_at": self.observed_at,
            "scope": self.scope,
            "deployment_authority": _thaw_json(self.deployment_authority),
        }
        if include_digest:
            result["observation_digest"] = self.observation_digest
        return result


@dataclass(frozen=True, slots=True)
class CapabilityKnowledgeLink:
    link_id: str
    capability_id: str
    capability_revision_id: str
    knowledge_record_id: str
    relation_type: str
    source_status: str
    applicability: str
    source_trust: str
    review_state: str
    temporal_validity: Mapping[str, Any]
    environment_constraints: Mapping[str, Any]
    contradiction_state: str
    applicability_score: float
    applicability_evidence_refs: Sequence[object]
    evidence_refs: Sequence[object]
    created_at: str
    scope: str = "global"
    provenance: Mapping[str, Any] = field(default_factory=dict)
    link_digest: str = field(init=False)

    def __post_init__(self) -> None:
        source_status = ensure_allowed(self.source_status, field="source_status", allowed=KNOWLEDGE_SOURCE_STATUSES)
        applicability = ensure_allowed(self.applicability, field="applicability", allowed=KNOWLEDGE_APPLICABILITY)
        source_trust = ensure_allowed(self.source_trust, field="source_trust", allowed=KNOWLEDGE_TRUST_LEVELS)
        review_state = ensure_allowed(self.review_state, field="review_state", allowed=KNOWLEDGE_REVIEW_STATES)
        contradiction_state = ensure_allowed(
            self.contradiction_state, field="contradiction_state", allowed=KNOWLEDGE_CONTRADICTION_STATES
        )
        if applicability == "applicable" and (
            source_status in {"conflicted", "stale", "unverified", "rejected", "blocked"}
            or review_state in {"unreviewed", "rejected"}
            or contradiction_state == "contradicted"
        ):
            raise CapabilityContractError("unverified, stale, rejected, or contradicted knowledge cannot be applicable")
        temporal_validity = _mapping(self.temporal_validity, field_name="temporal_validity", executable=True, required=True)
        environment_constraints = _mapping(
            self.environment_constraints, field_name="environment_constraints", executable=True, required=True
        )
        applicability_evidence_refs = _evidence_refs(
            self.applicability_evidence_refs, field_name="applicability_evidence_refs"
        )
        if not applicability_evidence_refs:
            raise CapabilityContractError("applicability_evidence_refs must not be empty")
        object.__setattr__(self, "link_id", normalize_opaque_id(self.link_id, field="link_id"))
        object.__setattr__(self, "capability_id", normalize_capability_id(self.capability_id))
        object.__setattr__(self, "capability_revision_id", normalize_opaque_id(self.capability_revision_id, field="capability_revision_id"))
        object.__setattr__(self, "knowledge_record_id", normalize_opaque_id(self.knowledge_record_id, field="knowledge_record_id"))
        object.__setattr__(self, "relation_type", ensure_allowed(self.relation_type, field="relation_type", allowed=KNOWLEDGE_LINK_TYPES))
        object.__setattr__(self, "source_status", source_status)
        object.__setattr__(self, "applicability", applicability)
        object.__setattr__(self, "source_trust", source_trust)
        object.__setattr__(self, "review_state", review_state)
        object.__setattr__(self, "temporal_validity", temporal_validity)
        object.__setattr__(self, "environment_constraints", environment_constraints)
        object.__setattr__(self, "contradiction_state", contradiction_state)
        object.__setattr__(self, "applicability_score", ensure_probability(self.applicability_score, field="applicability_score"))
        object.__setattr__(self, "applicability_evidence_refs", applicability_evidence_refs)
        evidence_refs = _evidence_refs(self.evidence_refs)
        if not evidence_refs:
            raise CapabilityContractError("evidence_refs must not be empty")
        object.__setattr__(self, "evidence_refs", evidence_refs)
        object.__setattr__(self, "created_at", require_timestamp(self.created_at, field="created_at"))
        object.__setattr__(self, "scope", normalize_opaque_id(self.scope, field="scope"))
        object.__setattr__(self, "provenance", _mapping(self.provenance, field_name="provenance", executable=True))
        object.__setattr__(self, "link_digest", contract_digest(self.to_dict(include_digest=False)))

    def to_dict(self, *, include_digest: bool = True) -> dict[str, Any]:
        result = {
            "link_id": self.link_id,
            "capability_id": self.capability_id,
            "capability_revision_id": self.capability_revision_id,
            "knowledge_record_id": self.knowledge_record_id,
            "relation_type": self.relation_type,
            "source_status": self.source_status,
            "applicability": self.applicability,
            "source_trust": self.source_trust,
            "review_state": self.review_state,
            "temporal_validity": _thaw_json(self.temporal_validity),
            "environment_constraints": _thaw_json(self.environment_constraints),
            "contradiction_state": self.contradiction_state,
            "applicability_score": self.applicability_score,
            "applicability_evidence_refs": list(self.applicability_evidence_refs),
            "evidence_refs": list(self.evidence_refs),
            "created_at": self.created_at,
            "scope": self.scope,
            "provenance": _thaw_json(self.provenance),
        }
        if include_digest:
            result["link_digest"] = self.link_digest
        return result


@dataclass(frozen=True, slots=True)
class CapabilityStateSnapshot:
    snapshot_id: str
    capability_id: str
    capability_revision_id: str
    profile_id: str
    maturity: str
    confidence: float
    evidence_refs: Sequence[object]
    sample_sufficiency: Mapping[str, Any]
    reliability_metrics: Mapping[str, Any]
    latest_success_ref: str
    latest_failure_ref: str
    regression_streak: int
    dependency_state: Mapping[str, Any]
    knowledge_applicability: Mapping[str, Any]
    provider_applicability: Mapping[str, Any]
    environment_applicability: Mapping[str, Any]
    input_watermark: str
    algorithm_revision: str
    computed_at: str
    scope: str = "global"
    reason_codes: Sequence[object] = ()
    input_digests: Mapping[str, Any] = field(default_factory=dict)
    snapshot_digest: str = field(init=False)

    def __post_init__(self) -> None:
        sample_sufficiency = _mapping(
            self.sample_sufficiency, field_name="sample_sufficiency", executable=True, required=True
        )
        reliability_metrics = _mapping(
            self.reliability_metrics, field_name="reliability_metrics", executable=True, required=True
        )
        dependency_state = _mapping(self.dependency_state, field_name="dependency_state", executable=True, required=True)
        knowledge_applicability = _mapping(
            self.knowledge_applicability, field_name="knowledge_applicability", executable=True, required=True
        )
        provider_applicability = _mapping(
            self.provider_applicability, field_name="provider_applicability", executable=True, required=True
        )
        environment_applicability = _mapping(
            self.environment_applicability, field_name="environment_applicability", executable=True, required=True
        )
        if not all(
            (
                sample_sufficiency,
                reliability_metrics,
                dependency_state,
                knowledge_applicability,
                provider_applicability,
                environment_applicability,
            )
        ):
            raise CapabilityContractError("snapshot projection mappings must not be empty")
        if isinstance(self.regression_streak, bool) or not isinstance(self.regression_streak, int) or self.regression_streak < 0:
            raise CapabilityContractError("regression_streak must be a non-negative integer")
        object.__setattr__(self, "snapshot_id", normalize_opaque_id(self.snapshot_id, field="snapshot_id"))
        object.__setattr__(self, "capability_id", normalize_capability_id(self.capability_id))
        object.__setattr__(self, "capability_revision_id", normalize_opaque_id(self.capability_revision_id, field="capability_revision_id"))
        object.__setattr__(self, "profile_id", normalize_opaque_id(self.profile_id, field="profile_id"))
        object.__setattr__(self, "maturity", ensure_allowed(self.maturity, field="maturity", allowed=MATURITY_STATES))
        object.__setattr__(self, "confidence", ensure_probability(self.confidence, field="confidence"))
        evidence_refs = _evidence_refs(self.evidence_refs)
        if not evidence_refs:
            raise CapabilityContractError("evidence_refs must not be empty")
        object.__setattr__(self, "evidence_refs", evidence_refs)
        object.__setattr__(self, "sample_sufficiency", sample_sufficiency)
        object.__setattr__(self, "reliability_metrics", reliability_metrics)
        object.__setattr__(self, "latest_success_ref", normalize_optional_text(self.latest_success_ref, field="latest_success_ref", max_chars=512))
        object.__setattr__(self, "latest_failure_ref", normalize_optional_text(self.latest_failure_ref, field="latest_failure_ref", max_chars=512))
        object.__setattr__(self, "regression_streak", self.regression_streak)
        object.__setattr__(self, "dependency_state", dependency_state)
        object.__setattr__(self, "knowledge_applicability", knowledge_applicability)
        object.__setattr__(self, "provider_applicability", provider_applicability)
        object.__setattr__(self, "environment_applicability", environment_applicability)
        object.__setattr__(self, "input_watermark", normalize_opaque_id(self.input_watermark, field="input_watermark"))
        object.__setattr__(self, "algorithm_revision", normalize_opaque_id(self.algorithm_revision, field="algorithm_revision"))
        object.__setattr__(self, "computed_at", require_timestamp(self.computed_at, field="computed_at"))
        object.__setattr__(self, "scope", normalize_opaque_id(self.scope, field="scope"))
        object.__setattr__(self, "reason_codes", normalize_string_sequence(self.reason_codes, field="reason_codes", item_field="reason_code"))
        object.__setattr__(self, "input_digests", _mapping(self.input_digests, field_name="input_digests", executable=True))
        object.__setattr__(self, "snapshot_digest", contract_digest(self.to_dict(include_digest=False)))

    def to_dict(self, *, include_digest: bool = True) -> dict[str, Any]:
        result = {
            "snapshot_id": self.snapshot_id,
            "capability_id": self.capability_id,
            "capability_revision_id": self.capability_revision_id,
            "profile_id": self.profile_id,
            "maturity": self.maturity,
            "confidence": self.confidence,
            "evidence_refs": list(self.evidence_refs),
            "sample_sufficiency": _thaw_json(self.sample_sufficiency),
            "reliability_metrics": _thaw_json(self.reliability_metrics),
            "latest_success_ref": self.latest_success_ref,
            "latest_failure_ref": self.latest_failure_ref,
            "regression_streak": self.regression_streak,
            "dependency_state": _thaw_json(self.dependency_state),
            "knowledge_applicability": _thaw_json(self.knowledge_applicability),
            "provider_applicability": _thaw_json(self.provider_applicability),
            "environment_applicability": _thaw_json(self.environment_applicability),
            "input_watermark": self.input_watermark,
            "algorithm_revision": self.algorithm_revision,
            "computed_at": self.computed_at,
            "scope": self.scope,
            "reason_codes": list(self.reason_codes),
            "input_digests": _thaw_json(self.input_digests),
        }
        if include_digest:
            result["snapshot_digest"] = self.snapshot_digest
        return result


@dataclass(frozen=True, slots=True)
class L5AssessmentV3:
    assessment_id: str
    profile_id: str
    loop_maturity: str
    capability_snapshot_ids: Sequence[object]
    capability_readiness: Mapping[str, Any]
    adapter_readiness: Mapping[str, Any]
    deployment_assurance: Mapping[str, Any]
    evidence_refs: Sequence[object]
    created_at: str
    scope: str = "global"
    algorithm_revision: str = "l5-assessment.v3"
    input_watermarks: Mapping[str, Any] = field(default_factory=dict)
    assessment_digest: str = field(init=False)

    def __post_init__(self) -> None:
        deployment_assurance = dict(
            _mapping(self.deployment_assurance, field_name="deployment_assurance", executable=True, required=True)
        )
        status = ensure_allowed(
            deployment_assurance.get("status"),
            field="deployment_assurance.status",
            allowed=DEPLOYMENT_ASSURANCE_STATES,
        )
        deployment_assurance["status"] = status
        object.__setattr__(self, "assessment_id", normalize_opaque_id(self.assessment_id, field="assessment_id"))
        object.__setattr__(self, "profile_id", normalize_opaque_id(self.profile_id, field="profile_id"))
        object.__setattr__(self, "loop_maturity", ensure_allowed(self.loop_maturity, field="loop_maturity", allowed=LOOP_MATURITY_STATES))
        capability_snapshot_ids = normalize_string_sequence(
            self.capability_snapshot_ids, field="capability_snapshot_ids", item_field="capability_snapshot_id"
        )
        if not capability_snapshot_ids:
            raise CapabilityContractError("capability_snapshot_ids must not be empty")
        capability_readiness = _validate_capability_readiness(self.capability_readiness)
        for revision_id, binding_states in capability_readiness.items():
            for binding_key, readiness in binding_states.items():
                if readiness["snapshot_id"] not in capability_snapshot_ids:
                    raise CapabilityContractError(
                        f"capability_readiness.{revision_id}.{binding_key}.snapshot_id "
                        "must be listed in capability_snapshot_ids"
                    )
        object.__setattr__(self, "capability_snapshot_ids", capability_snapshot_ids)
        object.__setattr__(self, "capability_readiness", capability_readiness)
        object.__setattr__(self, "adapter_readiness", _freeze_json(_validate_readiness(self.adapter_readiness, field_name="adapter_readiness", allowed=ADAPTER_READINESS_STATES)))
        object.__setattr__(self, "deployment_assurance", _freeze_json(deployment_assurance))
        evidence_refs = _evidence_refs(self.evidence_refs)
        if not evidence_refs:
            raise CapabilityContractError("evidence_refs must not be empty")
        object.__setattr__(self, "evidence_refs", evidence_refs)
        object.__setattr__(self, "created_at", require_timestamp(self.created_at, field="created_at"))
        object.__setattr__(self, "scope", normalize_opaque_id(self.scope, field="scope"))
        object.__setattr__(self, "algorithm_revision", normalize_opaque_id(self.algorithm_revision, field="algorithm_revision"))
        object.__setattr__(self, "input_watermarks", _mapping(self.input_watermarks, field_name="input_watermarks", executable=True))
        object.__setattr__(self, "assessment_digest", contract_digest(self.to_dict(include_digest=False)))

    def to_dict(self, *, include_digest: bool = True) -> dict[str, Any]:
        result = {
            "assessment_id": self.assessment_id,
            "profile_id": self.profile_id,
            "loop_maturity": self.loop_maturity,
            "capability_snapshot_ids": list(self.capability_snapshot_ids),
            "capability_readiness": _thaw_json(self.capability_readiness),
            "adapter_readiness": _thaw_json(self.adapter_readiness),
            "deployment_assurance": _thaw_json(self.deployment_assurance),
            "evidence_refs": list(self.evidence_refs),
            "created_at": self.created_at,
            "scope": self.scope,
            "algorithm_revision": self.algorithm_revision,
            "input_watermarks": _thaw_json(self.input_watermarks),
        }
        if include_digest:
            result["assessment_digest"] = self.assessment_digest
        return result
