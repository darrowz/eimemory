"""Declarative bootstrap manifest for the legacy capability vocabulary.

The manifest is intentionally data, rather than a Python ``SEEDED_CAPABILITIES``
list.  Loading it does not write to a runtime; callers must explicitly invoke
``apply_seed_manifest`` through the dynamic registry transaction boundary.
"""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass
from hashlib import sha256
from importlib.resources import files
import json
import re
from types import MappingProxyType
from typing import Any

from eimemory.capabilities.models import (
    RISK_TIERS,
    SIDE_EFFECT_CLASSES,
    CapabilityDefinition,
    CapabilityRevision,
)
from eimemory.capabilities.registry import (
    CapabilityRegistry,
    CapabilityRegistryError,
    MutationReceipt,
    exact_runtime_scope,
)
from eimemory.models.records import ScopeRef


SEED_MANIFEST_SCHEMA_VERSION = "capability_seed_manifest.v1"
SEED_MANIFEST_RESOURCE = "legacy_capabilities.v1.json"
_SEED_PROVENANCE_SOURCE = "eimemory.capability_seed_manifest"
_OPAQUE_TEXT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,511}$")


class CapabilitySeedManifestError(ValueError):
    """The declarative seed manifest is malformed or conflicts with its receipt."""


@dataclass(frozen=True, slots=True)
class SeedCapability:
    """A declarative definition and its initial incompatible revision."""

    capability_id: str
    display_name: str
    description: str
    owner: str
    risk_tier: str
    tags: tuple[str, ...]
    revision_id: str
    revision_contract: Mapping[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "capability_id": self.capability_id,
            "display_name": self.display_name,
            "description": self.description,
            "owner": self.owner,
            "risk_tier": self.risk_tier,
            "tags": list(self.tags),
            "revision": {
                "revision_id": self.revision_id,
                "compatibility": "incompatible",
                "contract": _thaw_json(self.revision_contract),
            },
        }


@dataclass(frozen=True, slots=True)
class CapabilitySeedManifest:
    """A verified, immutable declaration suitable for deterministic seeding."""

    manifest_id: str
    version: str
    created_at: str
    manifest_digest: str
    capabilities: tuple[SeedCapability, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SEED_MANIFEST_SCHEMA_VERSION,
            "manifest_id": self.manifest_id,
            "version": self.version,
            "created_at": self.created_at,
            "manifest_digest": self.manifest_digest,
            "capabilities": [capability.to_dict() for capability in self.capabilities],
        }


@dataclass(frozen=True, slots=True)
class SeedManifestApplyResult:
    """Bounded public result of an explicit manifest application."""

    manifest_id: str
    version: str
    manifest_digest: str
    capability_scope: str
    definition_receipts: tuple[MutationReceipt, ...]
    revision_receipts: tuple[MutationReceipt, ...]

    @property
    def created_count(self) -> int:
        return sum(not item.idempotent for item in (*self.definition_receipts, *self.revision_receipts))

    @property
    def idempotent(self) -> bool:
        return bool(self.definition_receipts or self.revision_receipts) and self.created_count == 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "manifest_id": self.manifest_id,
            "version": self.version,
            "manifest_digest": self.manifest_digest,
            "capability_scope": self.capability_scope,
            "created_count": self.created_count,
            "idempotent": self.idempotent,
            "definitions": [item.to_dict() for item in self.definition_receipts],
            "revisions": [item.to_dict() for item in self.revision_receipts],
        }


def canonical_manifest_digest(payload: Mapping[str, Any]) -> str:
    """Return the stable SHA-256 checksum of declarative manifest content."""

    normalized = _copy_plain_json(payload, field_name="manifest")
    if not isinstance(normalized, dict):
        raise CapabilitySeedManifestError("manifest must be a JSON object")
    normalized.pop("manifest_digest", None)
    encoded = json.dumps(normalized, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return sha256(encoded.encode("utf-8")).hexdigest()


def load_seed_manifest() -> CapabilitySeedManifest:
    """Load and validate the packaged immutable legacy seed declaration."""

    resource = files("eimemory.capabilities").joinpath("data", SEED_MANIFEST_RESOURCE)
    try:
        raw = json.loads(resource.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CapabilitySeedManifestError("unable to load capability seed manifest") from exc
    return validate_seed_manifest(raw)


def validate_seed_manifest(value: Mapping[str, Any]) -> CapabilitySeedManifest:
    """Validate the closed, non-executable manifest schema and its checksum.

    The accepted grammar is deliberately smaller than the capability contracts:
    each initial revision is an ``incompatible`` v1 with object schemas and no
    dependency or composition declarations.  There is no field that can carry
    a command, program, provider binding, environment selector, or SQL.
    """

    raw = _expect_mapping(value, field_name="manifest")
    _expect_exact_keys(
        raw,
        {
            "schema_version",
            "manifest_id",
            "version",
            "created_at",
            "manifest_digest",
            "capabilities",
        },
        field_name="manifest",
    )
    if raw["schema_version"] != SEED_MANIFEST_SCHEMA_VERSION:
        raise CapabilitySeedManifestError("unsupported capability seed manifest schema_version")
    manifest_id = _opaque_text(raw["manifest_id"], field_name="manifest_id")
    version = _opaque_text(raw["version"], field_name="version")
    created_at = _timestamp_text(raw["created_at"], field_name="created_at")
    manifest_digest = _digest_text(raw["manifest_digest"], field_name="manifest_digest")
    calculated_digest = canonical_manifest_digest(raw)
    if manifest_digest != calculated_digest:
        raise CapabilitySeedManifestError("capability seed manifest digest does not match declarative content")

    raw_capabilities = raw["capabilities"]
    if not isinstance(raw_capabilities, list) or not raw_capabilities:
        raise CapabilitySeedManifestError("manifest.capabilities must be a non-empty array")
    if len(raw_capabilities) > 128:
        raise CapabilitySeedManifestError("manifest.capabilities exceeds the bounded 128-entry limit")

    capabilities: list[SeedCapability] = []
    seen_capability_ids: set[str] = set()
    seen_revision_ids: set[str] = set()
    for index, item in enumerate(raw_capabilities):
        capability = _validate_seed_capability(item, index=index)
        if capability.capability_id in seen_capability_ids:
            raise CapabilitySeedManifestError(f"manifest contains duplicate capability_id {capability.capability_id!r}")
        if capability.revision_id in seen_revision_ids:
            raise CapabilitySeedManifestError(f"manifest contains duplicate revision_id {capability.revision_id!r}")
        seen_capability_ids.add(capability.capability_id)
        seen_revision_ids.add(capability.revision_id)
        capabilities.append(capability)
    return CapabilitySeedManifest(
        manifest_id=manifest_id,
        version=version,
        created_at=created_at,
        manifest_digest=manifest_digest,
        capabilities=tuple(capabilities),
    )


def apply_seed_manifest(
    registry: CapabilityRegistry,
    *,
    runtime_scope: ScopeRef | Mapping[str, Any],
    capability_scope: str = "global",
    manifest: CapabilitySeedManifest | Mapping[str, Any] | None = None,
) -> SeedManifestApplyResult:
    """Explicitly seed a verified declaration through the dynamic registry.

    This function is deliberately never called during ``Runtime`` construction.
    Each request key includes the immutable manifest digest, making retries
    idempotent.  A prior descriptor's seed provenance is the receipt that
    prevents a changed payload from reusing an existing manifest ID/version.
    """

    seed = _coerce_manifest(manifest)
    scope = exact_runtime_scope(runtime_scope)
    logical_scope = _opaque_text(capability_scope, field_name="capability_scope")
    definitions: list[CapabilityDefinition] = []
    revisions: list[CapabilityRevision] = []
    for capability in seed.capabilities:
        definition = CapabilityDefinition(
            capability_id=capability.capability_id,
            display_name=capability.display_name,
            description=capability.description,
            owner=capability.owner,
            created_at=seed.created_at,
            status="discovered",
            scope=logical_scope,
            risk_tier=capability.risk_tier,
            tags=capability.tags,
            provenance=_seed_provenance(seed),
        )
        definitions.append(definition)
        revision = CapabilityRevision(
            revision_id=capability.revision_id,
            capability_id=capability.capability_id,
            contract=_thaw_json(capability.revision_contract),
            compatibility="incompatible",
            created_at=seed.created_at,
            status="active",
            scope=logical_scope,
            provenance=_seed_provenance(seed),
        )
        revisions.append(revision)
    try:
        definition_receipts, revision_receipts = registry.register_seed_manifest(
            definitions=definitions,
            revisions=revisions,
            manifest_id=seed.manifest_id,
            manifest_version=seed.version,
            manifest_digest=seed.manifest_digest,
            runtime_scope=scope,
        )
    except CapabilityRegistryError as exc:
        raise CapabilitySeedManifestError(str(exc)) from exc
    return SeedManifestApplyResult(
        manifest_id=seed.manifest_id,
        version=seed.version,
        manifest_digest=seed.manifest_digest,
        capability_scope=logical_scope,
        definition_receipts=definition_receipts,
        revision_receipts=revision_receipts,
    )


def _coerce_manifest(value: CapabilitySeedManifest | Mapping[str, Any] | None) -> CapabilitySeedManifest:
    if value is None:
        return load_seed_manifest()
    if isinstance(value, CapabilitySeedManifest):
        # Frozen dataclasses prevent ordinary mutation, but callers can still
        # construct one directly (or use dataclasses.replace).  Re-validate
        # its canonical declarative representation at the trust boundary so a
        # forged digest, unsupported nested value, or injected capability can
        # never enter the registry merely by bypassing the JSON loader.
        return validate_seed_manifest(value.to_dict())
    if isinstance(value, Mapping):
        return validate_seed_manifest(value)
    raise CapabilitySeedManifestError("manifest must be a CapabilitySeedManifest or declarative mapping")


def _seed_provenance(seed: CapabilitySeedManifest) -> dict[str, str]:
    return {
        "source": _SEED_PROVENANCE_SOURCE,
        "manifest_id": seed.manifest_id,
        "manifest_version": seed.version,
        "manifest_digest": seed.manifest_digest,
    }


def _validate_seed_capability(value: object, *, index: int) -> SeedCapability:
    raw = _expect_mapping(value, field_name=f"manifest.capabilities[{index}]")
    _expect_exact_keys(
        raw,
        {"capability_id", "display_name", "description", "owner", "risk_tier", "tags", "revision"},
        field_name=f"manifest.capabilities[{index}]",
    )
    capability_id = _capability_id(raw["capability_id"], field_name=f"manifest.capabilities[{index}].capability_id")
    display_name = _text(raw["display_name"], field_name=f"manifest.capabilities[{index}].display_name")
    description = _text(raw["description"], field_name=f"manifest.capabilities[{index}].description")
    owner = _text(raw["owner"], field_name=f"manifest.capabilities[{index}].owner")
    risk_tier = _allowed_text(raw["risk_tier"], allowed=RISK_TIERS, field_name=f"manifest.capabilities[{index}].risk_tier")
    tags = _tags(raw["tags"], field_name=f"manifest.capabilities[{index}].tags")
    revision = _expect_mapping(raw["revision"], field_name=f"manifest.capabilities[{index}].revision")
    _expect_exact_keys(
        revision,
        {"revision_id", "compatibility", "contract"},
        field_name=f"manifest.capabilities[{index}].revision",
    )
    revision_id = _opaque_text(revision["revision_id"], field_name=f"manifest.capabilities[{index}].revision.revision_id")
    if revision_id != f"{capability_id}:v1":
        raise CapabilitySeedManifestError("seed revision_id must be the capability's immutable :v1 identifier")
    if revision["compatibility"] != "incompatible":
        raise CapabilitySeedManifestError("seed revisions must declare compatibility=incompatible")
    contract = _validate_seed_contract(
        revision["contract"],
        expected_risk_tier=risk_tier,
        field_name=f"manifest.capabilities[{index}].revision.contract",
    )
    return SeedCapability(
        capability_id=capability_id,
        display_name=display_name,
        description=description,
        owner=owner,
        risk_tier=risk_tier,
        tags=tags,
        revision_id=revision_id,
        revision_contract=_freeze_json(contract),
    )


def _validate_seed_contract(value: object, *, expected_risk_tier: str, field_name: str) -> dict[str, Any]:
    raw = _expect_mapping(value, field_name=field_name)
    _expect_exact_keys(
        raw,
        {
            "input_schema",
            "output_schema",
            "success_invariants",
            "failure_invariants",
            "evidence_requirements",
            "dependencies",
            "composition",
            "risk_tier",
            "side_effect_class",
        },
        field_name=field_name,
    )
    if raw["risk_tier"] != expected_risk_tier:
        raise CapabilitySeedManifestError(f"{field_name}.risk_tier must equal the definition risk_tier")
    side_effect_class = _allowed_text(
        raw["side_effect_class"],
        allowed=SIDE_EFFECT_CLASSES,
        field_name=f"{field_name}.side_effect_class",
    )
    input_schema = _object_schema(raw["input_schema"], field_name=f"{field_name}.input_schema")
    output_schema = _object_schema(raw["output_schema"], field_name=f"{field_name}.output_schema")
    success_invariants = _invariant_names(raw["success_invariants"], field_name=f"{field_name}.success_invariants")
    failure_invariants = _invariant_names(raw["failure_invariants"], field_name=f"{field_name}.failure_invariants")
    evidence = _expect_mapping(raw["evidence_requirements"], field_name=f"{field_name}.evidence_requirements")
    _expect_exact_keys(evidence, {"minimum_refs"}, field_name=f"{field_name}.evidence_requirements")
    if type(evidence["minimum_refs"]) is not int or not 1 <= evidence["minimum_refs"] <= 64:
        raise CapabilitySeedManifestError(f"{field_name}.evidence_requirements.minimum_refs must be an integer 1..64")
    for key in ("dependencies", "composition"):
        if raw[key] != []:
            raise CapabilitySeedManifestError(f"{field_name}.{key} must be an empty declarative list in seed v1")
    return {
        "input_schema": input_schema,
        "output_schema": output_schema,
        "success_invariants": success_invariants,
        "failure_invariants": failure_invariants,
        "evidence_requirements": {"minimum_refs": evidence["minimum_refs"]},
        "dependencies": [],
        "composition": [],
        "risk_tier": expected_risk_tier,
        "side_effect_class": side_effect_class,
    }


def _object_schema(value: object, *, field_name: str) -> dict[str, str]:
    raw = _expect_mapping(value, field_name=field_name)
    _expect_exact_keys(raw, {"type"}, field_name=field_name)
    if raw["type"] != "object":
        raise CapabilitySeedManifestError(f"{field_name}.type must be the declarative value 'object'")
    return {"type": "object"}


def _invariant_names(value: object, *, field_name: str) -> list[str]:
    if not isinstance(value, list) or not value:
        raise CapabilitySeedManifestError(f"{field_name} must be a non-empty array")
    if len(value) > 32:
        raise CapabilitySeedManifestError(f"{field_name} exceeds the 32-item bound")
    result: list[str] = []
    for index, item in enumerate(value):
        result.append(_opaque_text(item, field_name=f"{field_name}[{index}]"))
    return result


def _tags(value: object, *, field_name: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not value:
        raise CapabilitySeedManifestError(f"{field_name} must be a non-empty array")
    if len(value) > 32:
        raise CapabilitySeedManifestError(f"{field_name} exceeds the 32-item bound")
    tags = tuple(_opaque_text(item, field_name=f"{field_name}[{index}]") for index, item in enumerate(value))
    if len(set(tags)) != len(tags):
        raise CapabilitySeedManifestError(f"{field_name} must not contain duplicate tags")
    return tags


def _expect_mapping(value: object, *, field_name: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise CapabilitySeedManifestError(f"{field_name} must be a JSON object")
    copied = _copy_plain_json(value, field_name=field_name)
    if not isinstance(copied, dict):  # Defensive: _copy_plain_json should preserve mappings.
        raise CapabilitySeedManifestError(f"{field_name} must be a JSON object")
    return copied


def _expect_exact_keys(value: Mapping[str, Any], expected: set[str], *, field_name: str) -> None:
    actual = set(value)
    if actual != expected:
        missing = sorted(expected.difference(actual))
        unexpected = sorted(actual.difference(expected))
        details: list[str] = []
        if missing:
            details.append(f"missing {', '.join(missing)}")
        if unexpected:
            details.append(f"unsupported {', '.join(unexpected)}")
        raise CapabilitySeedManifestError(f"{field_name} has invalid declarative fields ({'; '.join(details)})")


def _copy_plain_json(value: object, *, field_name: str) -> Any:
    """Copy only primitive JSON values; reject objects that could execute code."""

    if value is None or type(value) in {str, int, float, bool}:
        return value
    if isinstance(value, Mapping):
        copied: dict[str, Any] = {}
        for raw_key, item in value.items():
            if type(raw_key) is not str:
                raise CapabilitySeedManifestError(f"{field_name} object keys must be strings")
            copied[raw_key] = _copy_plain_json(item, field_name=f"{field_name}.{raw_key}")
        return copied
    if isinstance(value, list):
        return [_copy_plain_json(item, field_name=f"{field_name}[{index}]") for index, item in enumerate(value)]
    if isinstance(value, tuple):
        raise CapabilitySeedManifestError(f"{field_name} must use JSON arrays, not executable tuple-like values")
    raise CapabilitySeedManifestError(f"{field_name} contains a non-declarative value")


def _freeze_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({str(key): _freeze_json(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze_json(item) for item in value)
    return value


def _thaw_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _thaw_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw_json(item) for item in value]
    return deepcopy(value)


def _opaque_text(value: object, *, field_name: str) -> str:
    if type(value) is not str or not _OPAQUE_TEXT.fullmatch(value):
        raise CapabilitySeedManifestError(f"{field_name} must be a bounded opaque identifier")
    return value


def _capability_id(value: object, *, field_name: str) -> str:
    text = _opaque_text(value, field_name=field_name)
    if "." not in text:
        raise CapabilitySeedManifestError(f"{field_name} must be a dotted semantic capability identifier")
    return text


def _text(value: object, *, field_name: str) -> str:
    if type(value) is not str or not value.strip() or len(value) > 4_096:
        raise CapabilitySeedManifestError(f"{field_name} must be a non-empty bounded string")
    return value.strip()


def _timestamp_text(value: object, *, field_name: str) -> str:
    text = _text(value, field_name=field_name)
    if "T" not in text or not (text.endswith("Z") or "+" in text[10:] or "-" in text[10:]):
        raise CapabilitySeedManifestError(f"{field_name} must be an RFC3339 timestamp with timezone")
    return text


def _digest_text(value: object, *, field_name: str) -> str:
    if type(value) is not str or not re.fullmatch(r"[0-9a-f]{64}", value):
        raise CapabilitySeedManifestError(f"{field_name} must be a lowercase SHA-256 digest")
    return value


def _allowed_text(value: object, *, allowed: frozenset[str], field_name: str) -> str:
    text = _opaque_text(value, field_name=field_name)
    if text not in allowed:
        raise CapabilitySeedManifestError(f"{field_name} is not an allowed declarative value")
    return text


__all__ = [
    "CapabilitySeedManifest",
    "CapabilitySeedManifestError",
    "SEED_MANIFEST_RESOURCE",
    "SEED_MANIFEST_SCHEMA_VERSION",
    "SeedCapability",
    "SeedManifestApplyResult",
    "apply_seed_manifest",
    "canonical_manifest_digest",
    "load_seed_manifest",
    "validate_seed_manifest",
]
