"""Read-only deployment applicability for dynamic capability evidence.

This module is the v3 boundary between deployment assurance and cognitive
state. A deployment assertion is deliberately narrower than a capability
identity:

* only an explicit ``deployment_dependent`` declaration asks for current
  deployment authority;
* that authority is the immutable ``(commit, receipt, session)`` tuple;
* package versions and host/machine fingerprints are never authority inputs;
* a portable observation remains portable across an environment change;
* an environment-specific observation is invalidated only when it explicitly
  declares a hashed environment constraint; and
* implementation changes are evaluated against declared capability revision
  domains, not against a fixed release-domain universe.

The public results are intentionally bounded DTOs. They never echo raw
environment fingerprints, versions, paths, or arbitrary provenance payloads.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from hashlib import sha256
import json
import re
from typing import Any

from eimemory.capabilities.observations import CapabilityObservations
from eimemory.capabilities.registry import exact_runtime_scope
from eimemory.governance.evidence_contract import (
    ReleaseIdentity,
    current_release_identity,
    same_release_authority,
    same_scope,
    verified_deployment_receipt_identity,
)
from eimemory.models.records import ScopeRef


# Keep the historic name as the public schema token used by the first v3
# assessment consumers. The authority and declaration schemas are separate
# because neither is a capability identity schema.
RELEASE_APPLICABILITY_SCHEMA = "capability.release_applicability.v1"
CAPABILITY_DEPLOYMENT_AUTHORITY_SCHEMA = "capability.deployment_authority.v1"
CAPABILITY_DEPLOYMENT_APPLICABILITY_SCHEMA = "capability.deployment_applicability.v1"

MAX_DEPLOYMENT_EVIDENCE = 500
MAX_IMPLEMENTATION_DOMAINS = 32
MAX_IDENTIFIER_CHARS = 512
_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_COMMIT_RE = re.compile(r"[0-9a-f]{40}")
_IDENTIFIER_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:@/-]{0,511}")
_DIAGNOSTIC_VERSION_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,127}")


@dataclass(frozen=True, slots=True)
class _DeploymentAuthority:
    deployment_dependent: bool
    release: ReleaseIdentity | None
    implementation_domains: tuple[str, ...]
    implementation_digest: str
    environment_dependent: bool
    environment_constraint_digest: str
    error: str = ""


@dataclass(frozen=True, slots=True)
class _RevisionApplicability:
    implementation_domains: tuple[str, ...]
    affected_implementation_domains: tuple[str, ...]
    error: str = ""


@dataclass(frozen=True, slots=True)
class _CatalogEntity:
    entity_id: str
    descriptor: Mapping[str, Any]
    status: str


@dataclass(frozen=True, slots=True)
class _CapabilityCatalog:
    revisions: Mapping[str, _CatalogEntity]
    bindings: Mapping[str, _CatalogEntity]
    truncated: bool


class CapabilityDeploymentEvidenceService:
    """Bounded, side-effect-free verifier for v3 deployment applicability.

    The service reads capability DTOs through ``RuntimeStore`` and deployment
    authority through ``current_release_identity``. It does not query raw SQL,
    invoke health endpoints, mutate records, or turn a host/version into a
    capability fact.
    """

    def __init__(self, runtime: Any) -> None:
        self._runtime = runtime

    def verify(
        self,
        *,
        scope: ScopeRef | Mapping[str, Any],
        capability_scope: str,
        evidence: Mapping[str, Any],
        current_release: ReleaseIdentity | None = None,
    ) -> dict[str, Any]:
        """Verify one supplied evidence DTO without persisting anything.

        ``evidence`` may be a direct observation/evaluation payload or a DTO
        returned by the capability store (with a ``payload`` member). The
        result has an ``ok`` tri-state: ``True`` for a verified required
        assertion, ``False`` for a failed required assertion, and ``None`` for
        portable evidence that does not need deployment verification.
        """

        scope_ref = exact_runtime_scope(scope)
        descriptor = _evidence_descriptor(evidence)
        authority = _deployment_authority(descriptor["payload"].get("deployment_authority"))
        if not authority.deployment_dependent:
            return _portable_evidence_result(descriptor, authority.error)
        catalog = self._catalog(scope_ref, capability_scope)
        release = current_release if isinstance(current_release, ReleaseIdentity) else current_release_identity(
            self._runtime,
            scope_ref,
        )
        verified = _verify_deployment_dependent_evidence(
            descriptor,
            authority=authority,
            current_release=release,
            catalog=catalog,
            runtime=self._runtime,
            scope=scope_ref,
        )
        return {
            **verified,
            "required": True,
            "blocking": verified.get("ok") is not True,
        }

    def build_assurance(
        self,
        *,
        scope: ScopeRef | Mapping[str, Any],
        capability_scope: str,
        limit: int = MAX_DEPLOYMENT_EVIDENCE,
    ) -> dict[str, Any]:
        """Return the independent deployment-assurance axis for a v3 scope."""

        scope_ref = exact_runtime_scope(scope)
        normalized_limit = _bounded_limit(limit)
        evidence_rows, input_truncated = _bounded_evidence_rows(
            self._runtime,
            scope=scope_ref,
            capability_scope=capability_scope,
            limit=normalized_limit,
        )
        descriptors = [_evidence_descriptor(row) for row in evidence_rows]
        authorities = [
            _deployment_authority(descriptor["payload"].get("deployment_authority"))
            for descriptor in descriptors
        ]
        dependent_indexes = [
            index for index, authority in enumerate(authorities) if authority.deployment_dependent
        ]
        portable_count = len(descriptors) - len(dependent_indexes)
        current = current_release_identity(self._runtime, scope_ref)

        # A capped source page cannot prove a required assertion is complete.
        # When no evidence declares deployment dependence it remains a neutral,
        # explicitly non-blocking unknown rather than an accidental green pass.
        if not dependent_indexes:
            return _assurance_without_required_evidence(
                descriptors=descriptors,
                portable_count=portable_count,
                current_release=current,
                input_truncated=input_truncated,
            )

        catalog = self._catalog(scope_ref, capability_scope)
        results = [
            _verify_deployment_dependent_evidence(
                descriptors[index],
                authority=authorities[index],
                current_release=current,
                catalog=catalog,
                runtime=self._runtime,
                scope=scope_ref,
            )
            for index in dependent_indexes
        ]
        invalidated = [result for result in results if result.get("ok") is not True]
        incomplete = bool(input_truncated or catalog.truncated)
        if incomplete:
            status = "blocked"
            reason = "deployment_evidence_window_truncated" if input_truncated else "capability_catalog_window_truncated"
        elif invalidated:
            status = "degraded"
            reason = "deployment_evidence_invalidated"
        else:
            status = "ready"
            reason = "all_declared_deployment_evidence_verified"

        result = {
            "schema": RELEASE_APPLICABILITY_SCHEMA,
            "ok": bool(not incomplete and not invalidated),
            "required": True,
            "blocking": bool(incomplete or invalidated),
            "status": status,
            "reason": reason,
            "portable_observation_count": portable_count,
            "portable_evidence_count": portable_count,
            "deployment_dependent_observation_count": len(dependent_indexes),
            "deployment_dependent_evidence_count": len(dependent_indexes),
            "verified_evidence_count": len(results) - len(invalidated),
            "invalidated_evidence_count": len(invalidated),
            "mismatch_count": len(invalidated),
            "current_release": _release_authority_payload(current),
            "evidence_refs": [result["evidence_id"] for result in results],
            "invalidations": [_public_verification_result(result) for result in invalidated],
            # ``mismatches`` was the original lightweight public field. Keep
            # it as a strict alias so old v3 shadow/report consumers do not
            # need to parse raw authority payloads.
            "mismatches": [_public_verification_result(result) for result in invalidated],
            "input_truncated": input_truncated,
            "catalog_truncated": catalog.truncated,
            "diagnostics": _non_authoritative_diagnostics(current),
        }
        result["digest"] = _assurance_digest(result)
        return result

    def _catalog(self, scope: ScopeRef, capability_scope: str) -> _CapabilityCatalog:
        return _read_capability_catalog(self._runtime, scope=scope, capability_scope=capability_scope)


def build_capability_deployment_assurance(
    runtime: Any,
    *,
    scope: ScopeRef | Mapping[str, Any],
    capability_scope: str,
    limit: int = MAX_DEPLOYMENT_EVIDENCE,
) -> dict[str, Any]:
    """Build the strict-but-independent L5 v3 deployment axis.

    A lack of explicit deployment-dependent evidence is represented as
    ``status='not_evaluated'`` and ``ok=None``. It therefore neither grants
    deployment assurance nor globally resets cognitive maturity.
    """

    return CapabilityDeploymentEvidenceService(runtime).build_assurance(
        scope=scope,
        capability_scope=capability_scope,
        limit=limit,
    )


def verify_capability_deployment_evidence(
    runtime: Any,
    *,
    scope: ScopeRef | Mapping[str, Any],
    capability_scope: str,
    evidence: Mapping[str, Any],
    current_release: ReleaseIdentity | None = None,
) -> dict[str, Any]:
    """Verify one v3 observation/evaluation deployment declaration.

    This small public facade is useful to replay/evaluation callers that need
    a reason code before placing an observation in a larger assessment. It is
    read-only and bounded by a one-row input plus the capped registry DTO read.
    """

    return CapabilityDeploymentEvidenceService(runtime).verify(
        scope=scope,
        capability_scope=capability_scope,
        evidence=evidence,
        current_release=current_release,
    )


def compatible_evidence_inheritance(
    *,
    source_revision: Mapping[str, Any],
    target_revision: Mapping[str, Any],
    source_binding: Mapping[str, Any] | None = None,
    target_binding: Mapping[str, Any] | None = None,
    implementation_domains: Sequence[object] = (),
    source_applicability: Mapping[str, Any] | None = None,
    target_applicability: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Decide explicit portable-evidence inheritance deterministically.

    Compatibility is never inferred from package version, provider instance,
    machine, or a matching capability name. A target revision must explicitly
    supersede the source and carry a compatibility policy digest. If its
    implementation digest changes, the evidence may only carry where the
    target declares the affected implementation domains and the evidence does
    not cover one of them.
    """

    source_id = _text(source_revision.get("revision_id"), max_chars=MAX_IDENTIFIER_CHARS)
    target_id = _text(target_revision.get("revision_id"), max_chars=MAX_IDENTIFIER_CHARS)
    compatible = str(target_revision.get("compatibility") or "") == "compatible"
    supersedes = _text(target_revision.get("supersedes_revision_id"), max_chars=MAX_IDENTIFIER_CHARS)
    policy_id = _text(target_revision.get("compatibility_policy_id"), max_chars=MAX_IDENTIFIER_CHARS)
    policy_digest = _sha256_text(target_revision.get("compatibility_policy_digest"))
    source_capability_id = _text(source_revision.get("capability_id"), max_chars=MAX_IDENTIFIER_CHARS)
    target_capability_id = _text(target_revision.get("capability_id"), max_chars=MAX_IDENTIFIER_CHARS)
    same_capability = bool(source_capability_id and source_capability_id == target_capability_id)
    base_ok = bool(
        compatible
        and source_id
        and target_id
        and supersedes == source_id
        and policy_id
        and policy_digest
        and same_capability
    )
    domains, domain_error = _implementation_domains(implementation_domains, required=False)
    source_declaration = (
        _revision_applicability(source_revision)
        if source_applicability is None
        else _revision_applicability_from_mapping(source_applicability)
    )
    target_declaration = (
        _revision_applicability(target_revision)
        if target_applicability is None
        else _revision_applicability_from_mapping(target_applicability)
    )
    implementation_changed = False
    if source_binding is not None and target_binding is not None:
        implementation_changed = (
            _sha256_text(source_binding.get("implementation_digest"))
            != _sha256_text(target_binding.get("implementation_digest"))
        )

    affected = target_declaration.affected_implementation_domains
    domains_declared = bool(domains)
    domains_valid = bool(
        not domain_error
        and (
            not domains_declared
            or (
                not source_declaration.error
                and not target_declaration.error
                and set(domains).issubset(source_declaration.implementation_domains)
                and set(domains).issubset(target_declaration.implementation_domains)
            )
        )
    )
    affected_overlap = bool(set(domains).intersection(affected))
    implementation_safe = bool(
        not implementation_changed
        or (
            domains_declared
            and domains_valid
            and bool(affected)
            and not affected_overlap
        )
    )
    ok = bool(base_ok and domains_valid and implementation_safe)
    if not base_ok:
        reason = "missing_compatibility_lineage"
    elif domain_error or not domains_valid:
        reason = "implementation_domain_declaration_invalid"
    elif implementation_changed and not affected:
        reason = "implementation_change_domain_undeclared"
    elif implementation_changed and affected_overlap:
        reason = "affected_implementation_domain_changed"
    elif implementation_changed:
        reason = "explicit_compatible_unaffected_domain_inheritance"
    else:
        reason = "explicit_compatible_portable_inheritance"
    return {
        "schema": RELEASE_APPLICABILITY_SCHEMA,
        "ok": ok,
        "reason": reason,
        "policy_id": policy_id,
        "policy_digest": policy_digest,
        "implementation_changed": implementation_changed,
        "implementation_domains": list(domains),
        "affected_implementation_domains": list(affected),
    }


def environment_constraint_digest(fingerprint: Mapping[str, Any]) -> str:
    """Return a stable opaque digest for an explicitly environment-bound fact.

    Callers must opt into environment dependence separately. This helper does
    not expose the fingerprint and its result must not be used as a capability,
    revision, binding, or release identity.
    """

    if not isinstance(fingerprint, Mapping) or not fingerprint:
        raise ValueError("environment fingerprint must be a non-empty mapping")
    return _digest(dict(fingerprint))


def _assurance_without_required_evidence(
    *,
    descriptors: list[dict[str, Any]],
    portable_count: int,
    current_release: ReleaseIdentity | None,
    input_truncated: bool,
) -> dict[str, Any]:
    status = "unknown" if input_truncated else "not_evaluated"
    reason = "evidence_window_truncated_without_deployment_declaration" if input_truncated else "no_deployment_dependent_capability_evidence"
    result = {
        "schema": RELEASE_APPLICABILITY_SCHEMA,
        "ok": None,
        # A complete scan can prove that no evidence declared deployment
        # dependence. A capped scan cannot, so it remains an explicit unknown
        # deployment gate rather than an inferred portable state.
        "required": None if input_truncated else False,
        "blocking": input_truncated,
        "status": status,
        "reason": reason,
        "portable_observation_count": portable_count,
        "portable_evidence_count": portable_count,
        "deployment_dependent_observation_count": 0,
        "deployment_dependent_evidence_count": 0,
        "verified_evidence_count": 0,
        "invalidated_evidence_count": 0,
        "mismatch_count": 0,
        "current_release": _release_authority_payload(current_release),
        "evidence_refs": [descriptor["evidence_id"] for descriptor in descriptors],
        "invalidations": [],
        "mismatches": [],
        "input_truncated": input_truncated,
        "catalog_truncated": False,
        "diagnostics": _non_authoritative_diagnostics(current_release),
    }
    result["digest"] = _assurance_digest(result)
    return result


def _bounded_evidence_rows(
    runtime: Any,
    *,
    scope: ScopeRef,
    capability_scope: str,
    limit: int,
) -> tuple[list[dict[str, Any]], bool]:
    """Read at most ``limit`` canonical observations/evaluations.

    Evaluations recorded through the normal facade also create a derived
    observation. Directly persisted evaluations are retained by reading the
    remaining bounded budget and suppressing known derived duplicates.
    """

    observations = CapabilityObservations(runtime.store).list(
        runtime_scope=scope,
        capability_scope=capability_scope,
        limit=limit,
    )
    observation_rows = [dict(row) for row in observations if isinstance(row, Mapping)]
    truncated = len(observation_rows) >= limit
    remaining = max(0, limit - len(observation_rows))
    runs: list[dict[str, Any]] = []
    if remaining:
        raw_runs = runtime.store.read_capabilities(
            lambda repository: repository.list_evaluation_runs(
                scope=scope,
                capability_scope=capability_scope,
                limit=remaining,
            )
        )
        runs = [dict(row) for row in raw_runs if isinstance(row, Mapping)]
        truncated = bool(truncated or len(runs) >= remaining)
    derived_run_ids = {
        _text(
            _mapping_value(
                _mapping_value(row.get("payload"), "provenance"),
                "evaluation_run_id",
            ),
            max_chars=MAX_IDENTIFIER_CHARS,
        )
        for row in observation_rows
    }
    rows = [
        {**row, "evidence_type": "observation"}
        for row in observation_rows
    ]
    rows.extend(
        {**row, "evidence_type": "evaluation_run"}
        for row in runs
        if _text(row.get("run_id"), max_chars=MAX_IDENTIFIER_CHARS) not in derived_run_ids
    )
    rows.sort(
        key=lambda row: (
            str(row.get("evidence_type") or ""),
            _row_evidence_id(row),
        )
    )
    # Deduplication can only reduce the returned count; the source-page
    # truncation signal remains conservative.
    return rows[:limit], truncated


def _read_capability_catalog(
    runtime: Any,
    *,
    scope: ScopeRef,
    capability_scope: str,
) -> _CapabilityCatalog:
    """Load bounded lifecycle DTOs, never raw descriptor rows."""

    try:
        revisions, bindings = runtime.store.read_capabilities(
            lambda repository: (
                repository.list_effective_entities(
                    entity_type="revision",
                    scope=scope,
                    capability_scope=capability_scope,
                    status=None,
                    limit=MAX_DEPLOYMENT_EVIDENCE,
                ),
                repository.list_effective_entities(
                    entity_type="binding",
                    scope=scope,
                    capability_scope=capability_scope,
                    status=None,
                    limit=MAX_DEPLOYMENT_EVIDENCE,
                ),
            )
        )
    except (AttributeError, RuntimeError, TypeError, ValueError):
        # The public verifier never leaks exception messages or raw stored
        # payload fragments into a deployment report.
        return _CapabilityCatalog(revisions={}, bindings={}, truncated=True)
    revision_entities = [_catalog_entity(item) for item in revisions]
    binding_entities = [_catalog_entity(item) for item in bindings]
    return _CapabilityCatalog(
        revisions={item.entity_id: item for item in revision_entities if item.entity_id},
        bindings={item.entity_id: item for item in binding_entities if item.entity_id},
        truncated=bool(
            len(revision_entities) >= MAX_DEPLOYMENT_EVIDENCE
            or len(binding_entities) >= MAX_DEPLOYMENT_EVIDENCE
        ),
    )


def _verified_historical_release_authority(
    runtime: Any,
    *,
    scope: ScopeRef,
    release: ReleaseIdentity,
) -> bool:
    """Confirm a source receipt before an explicit cross-release inheritance.

    This is deliberately narrower than a deployment-health check: it verifies
    the persisted immutable receipt in the exact scope, then compares only its
    commit/receipt/session authority.  It does not inspect package version or
    host metadata and cannot make a source release current.
    """

    if not isinstance(release, ReleaseIdentity) or not release.complete:
        return False
    store = getattr(runtime, "store", None)
    getter = getattr(store, "get_by_id", None)
    if not callable(getter):
        return False
    try:
        record = getter(release.receipt_id, scope=scope)
    except (AttributeError, RuntimeError, TypeError, ValueError):
        return False
    record_scope = record.get("scope") if isinstance(record, Mapping) else getattr(record, "scope", None)
    if record is None or not same_scope(record_scope, scope):
        return False
    return same_release_authority(verified_deployment_receipt_identity(record), release)


def _verify_deployment_dependent_evidence(
    descriptor: Mapping[str, Any],
    *,
    authority: _DeploymentAuthority,
    current_release: ReleaseIdentity | None,
    catalog: _CapabilityCatalog,
    runtime: Any,
    scope: ScopeRef,
) -> dict[str, Any]:
    base = _verification_base(descriptor)
    if authority.error:
        return {**base, "ok": False, "reason": authority.error}
    if current_release is None or not current_release.complete:
        return {**base, "ok": False, "reason": "current_release_authority_unavailable"}
    if catalog.truncated:
        return {**base, "ok": False, "reason": "capability_catalog_window_truncated"}

    payload = descriptor["payload"]
    revision_id = _text(payload.get("capability_revision_id"), max_chars=MAX_IDENTIFIER_CHARS)
    binding_id = _text(payload.get("provider_binding_id"), max_chars=MAX_IDENTIFIER_CHARS)
    capability_id = _text(payload.get("capability_id"), max_chars=MAX_IDENTIFIER_CHARS)
    source_revision = catalog.revisions.get(revision_id)
    source_binding = catalog.bindings.get(binding_id)
    if source_revision is None:
        return {**base, "ok": False, "reason": "capability_revision_not_registered"}
    if source_binding is None:
        return {**base, "ok": False, "reason": "provider_binding_not_registered"}
    if (
        _text(source_revision.descriptor.get("capability_id"), max_chars=MAX_IDENTIFIER_CHARS) != capability_id
        or _text(source_binding.descriptor.get("capability_id"), max_chars=MAX_IDENTIFIER_CHARS) != capability_id
        or _text(source_binding.descriptor.get("capability_revision_id"), max_chars=MAX_IDENTIFIER_CHARS) != revision_id
    ):
        return {**base, "ok": False, "reason": "evidence_binding_contract_mismatch"}
    source_digest = _sha256_text(source_binding.descriptor.get("implementation_digest"))
    if not source_digest or authority.implementation_digest != source_digest:
        return {**base, "ok": False, "reason": "implementation_digest_mismatch"}
    source_declaration = _revision_applicability(source_revision.descriptor)
    if source_declaration.error:
        return {**base, "ok": False, "reason": source_declaration.error}
    if not set(authority.implementation_domains).issubset(source_declaration.implementation_domains):
        return {**base, "ok": False, "reason": "implementation_domain_not_declared"}

    # A direct deployment-dependent assertion remains strict: it must name the
    # current immutable commit/receipt/session authority.  An older assertion
    # can cross a release only through the separately declared, deterministic
    # revision-compatibility rule below.  Version and machine values are never
    # part of either route.
    release_matches_current = bool(
        authority.release is not None
        and same_release_authority(authority.release, current_release)
    )
    if not release_matches_current:
        if authority.release is None or not _verified_historical_release_authority(
            runtime,
            scope=scope,
            release=authority.release,
        ):
            return {**base, "ok": False, "reason": "deployment_release_authority_mismatch"}
        if authority.environment_dependent:
            return {**base, "ok": False, "reason": "environment_dependent_evidence_not_inheritable"}
        inherited = _compatible_active_target(
            catalog,
            source_revision=source_revision,
            source_binding=source_binding,
            authority=authority,
            capability_id=capability_id,
        )
        if inherited is not None:
            return {**base, **inherited}
        return {**base, "ok": False, "reason": "deployment_release_compatibility_not_declared"}

    direct_candidates = _active_binding_candidates(
        catalog,
        revision_id=revision_id,
        capability_id=capability_id,
        provider_kind=_text(source_binding.descriptor.get("provider_kind"), max_chars=MAX_IDENTIFIER_CHARS),
        implementation_digest=authority.implementation_digest,
    )
    direct_match = _select_environment_match(direct_candidates, authority)
    if direct_match is not None:
        return {
            **base,
            "ok": True,
            "reason": "current_revision_implementation_matches",
            "effective_revision_id": revision_id,
            "effective_binding_id": direct_match.entity_id,
        }
    if direct_candidates and authority.environment_dependent:
        return {**base, "ok": False, "reason": "environment_constraint_changed"}
    replacement_candidates = _active_binding_candidates(
        catalog,
        revision_id=revision_id,
        capability_id=capability_id,
        provider_kind=_text(source_binding.descriptor.get("provider_kind"), max_chars=MAX_IDENTIFIER_CHARS),
        implementation_digest="",
    )
    if replacement_candidates:
        return {**base, "ok": False, "reason": "implementation_contract_changed"}

    inherited = _compatible_active_target(
        catalog,
        source_revision=source_revision,
        source_binding=source_binding,
        authority=authority,
        capability_id=capability_id,
    )
    if inherited is not None:
        return {**base, **inherited}
    return {**base, "ok": False, "reason": "current_implementation_contract_unavailable"}


def _compatible_active_target(
    catalog: _CapabilityCatalog,
    *,
    source_revision: _CatalogEntity,
    source_binding: _CatalogEntity,
    authority: _DeploymentAuthority,
    capability_id: str,
) -> dict[str, Any] | None:
    provider_kind = _text(source_binding.descriptor.get("provider_kind"), max_chars=MAX_IDENTIFIER_CHARS)
    source_revision_id = source_revision.entity_id
    candidates: list[tuple[str, str, _CatalogEntity, _CatalogEntity, dict[str, Any]]] = []
    for target_revision_id, target_revision in sorted(catalog.revisions.items()):
        if target_revision.status != "active" or target_revision_id == source_revision_id:
            continue
        if _text(target_revision.descriptor.get("capability_id"), max_chars=MAX_IDENTIFIER_CHARS) != capability_id:
            continue
        target_declaration = _revision_applicability(target_revision.descriptor)
        if target_declaration.error:
            continue
        for target_binding in _active_binding_candidates(
            catalog,
            revision_id=target_revision_id,
            capability_id=capability_id,
            provider_kind=provider_kind,
            implementation_digest="",
        ):
            inheritance = compatible_evidence_inheritance(
                source_revision=source_revision.descriptor,
                target_revision=target_revision.descriptor,
                source_binding=source_binding.descriptor,
                target_binding=target_binding.descriptor,
                implementation_domains=authority.implementation_domains,
                source_applicability=_revision_applicability_mapping(source_revision.descriptor),
                target_applicability=_revision_applicability_mapping(target_revision.descriptor),
            )
            if inheritance.get("ok") is not True:
                continue
            if _select_environment_match([target_binding], authority) is None:
                continue
            candidates.append(
                (target_revision_id, target_binding.entity_id, target_revision, target_binding, inheritance)
            )
    if not candidates:
        return None
    target_revision_id, target_binding_id, _revision, _binding, inheritance = min(candidates)
    return {
        "ok": True,
        "reason": str(inheritance.get("reason") or "explicit_compatible_portable_inheritance"),
        "effective_revision_id": target_revision_id,
        "effective_binding_id": target_binding_id,
        "compatibility_policy_digest": str(inheritance.get("policy_digest") or ""),
    }


def _active_binding_candidates(
    catalog: _CapabilityCatalog,
    *,
    revision_id: str,
    capability_id: str,
    provider_kind: str,
    implementation_digest: str,
) -> list[_CatalogEntity]:
    candidates = [
        binding
        for binding in catalog.bindings.values()
        if binding.status == "active"
        and _text(binding.descriptor.get("capability_revision_id"), max_chars=MAX_IDENTIFIER_CHARS) == revision_id
        and _text(binding.descriptor.get("capability_id"), max_chars=MAX_IDENTIFIER_CHARS) == capability_id
        and _text(binding.descriptor.get("provider_kind"), max_chars=MAX_IDENTIFIER_CHARS) == provider_kind
        and (
            not implementation_digest
            or _sha256_text(binding.descriptor.get("implementation_digest")) == implementation_digest
        )
    ]
    return sorted(candidates, key=lambda binding: binding.entity_id)


def _select_environment_match(
    candidates: Sequence[_CatalogEntity],
    authority: _DeploymentAuthority,
) -> _CatalogEntity | None:
    for candidate in candidates:
        if not authority.environment_dependent:
            return candidate
        fingerprint = candidate.descriptor.get("environment_fingerprint")
        if isinstance(fingerprint, Mapping) and environment_constraint_digest(fingerprint) == authority.environment_constraint_digest:
            return candidate
    return None


def _deployment_authority(value: Any) -> _DeploymentAuthority:
    if value is None or value == {}:
        return _DeploymentAuthority(False, None, (), "", False, "")
    if not isinstance(value, Mapping):
        return _DeploymentAuthority(True, None, (), "", False, "", "deployment_authority_malformed")
    raw_dependent = value.get("deployment_dependent")
    if raw_dependent is not True:
        if raw_dependent not in (None, False):
            return _DeploymentAuthority(True, None, (), "", False, "", "deployment_dependency_flag_invalid")
        return _DeploymentAuthority(False, None, (), "", False, "")
    schema = _text(value.get("schema"), max_chars=MAX_IDENTIFIER_CHARS)
    if schema and schema != CAPABILITY_DEPLOYMENT_AUTHORITY_SCHEMA:
        return _DeploymentAuthority(True, None, (), "", False, "", "deployment_authority_schema_unsupported")
    release_payload = value.get("release")
    if release_payload is None:
        release_payload = value
    if not isinstance(release_payload, Mapping):
        return _DeploymentAuthority(True, None, (), "", False, "", "deployment_release_authority_missing")
    commit = _text(
        release_payload.get("commit") or release_payload.get("release_commit"),
        max_chars=40,
    ).lower()
    receipt_id = _text(
        release_payload.get("receipt_id") or release_payload.get("deployment_receipt_id"),
        max_chars=MAX_IDENTIFIER_CHARS,
    )
    session_id = _text(
        release_payload.get("session_id") or release_payload.get("release_session_id"),
        max_chars=MAX_IDENTIFIER_CHARS,
    )
    if not _COMMIT_RE.fullmatch(commit) or not _safe_identifier(receipt_id) or not _safe_identifier(session_id):
        return _DeploymentAuthority(True, None, (), "", False, "", "deployment_release_authority_missing")
    implementation = value.get("implementation") if isinstance(value.get("implementation"), Mapping) else value
    raw_domains = _mapping_value(implementation, "domains")
    if raw_domains is None:
        raw_domains = _mapping_value(implementation, "implementation_domains")
    if raw_domains is None:
        raw_domains = _mapping_value(implementation, "implementation_domain")
    domains, domain_error = _implementation_domains(raw_domains, required=True)
    implementation_digest = _sha256_text(
        _mapping_value(implementation, "digest")
        or _mapping_value(implementation, "implementation_digest")
        or value.get("implementation_digest")
    )
    if domain_error:
        return _DeploymentAuthority(True, None, (), "", False, "", domain_error)
    if not implementation_digest:
        return _DeploymentAuthority(True, None, domains, "", False, "", "implementation_digest_missing")
    environment = value.get("environment") if isinstance(value.get("environment"), Mapping) else {}
    raw_environment_dependent = (
        environment.get("dependent")
        if "dependent" in environment
        else value.get("environment_dependent", False)
    )
    if not isinstance(raw_environment_dependent, bool):
        return _DeploymentAuthority(True, None, domains, implementation_digest, False, "", "environment_dependency_flag_invalid")
    environment_digest = _sha256_text(
        environment.get("constraint_digest")
        or environment.get("fingerprint_digest")
        or value.get("environment_constraint_digest")
        or value.get("environment_fingerprint_digest")
    )
    if raw_environment_dependent and not environment_digest:
        return _DeploymentAuthority(True, None, domains, implementation_digest, True, "", "environment_constraint_digest_missing")
    return _DeploymentAuthority(
        True,
        ReleaseIdentity(commit=commit, version="", receipt_id=receipt_id, session_id=session_id),
        domains,
        implementation_digest,
        raw_environment_dependent,
        environment_digest,
    )


def _revision_applicability(revision: Mapping[str, Any]) -> _RevisionApplicability:
    contract = revision.get("contract") if isinstance(revision.get("contract"), Mapping) else {}
    requirements = contract.get("evidence_requirements") if isinstance(contract.get("evidence_requirements"), Mapping) else {}
    return _revision_applicability_from_mapping(requirements)


def _revision_applicability_mapping(revision: Mapping[str, Any]) -> Mapping[str, Any]:
    contract = revision.get("contract") if isinstance(revision.get("contract"), Mapping) else {}
    requirements = contract.get("evidence_requirements") if isinstance(contract.get("evidence_requirements"), Mapping) else {}
    return requirements


def _revision_applicability_from_mapping(requirements: Mapping[str, Any]) -> _RevisionApplicability:
    declaration = requirements.get("deployment_applicability")
    if not isinstance(declaration, Mapping):
        declaration = requirements.get("release_applicability")
    if not isinstance(declaration, Mapping):
        # Accept the concise form inside evidence_requirements during the data
        # migration, but never infer it from a package or host property.
        declaration = requirements
    schema = _text(declaration.get("schema"), max_chars=MAX_IDENTIFIER_CHARS)
    if schema and schema != CAPABILITY_DEPLOYMENT_APPLICABILITY_SCHEMA:
        return _RevisionApplicability((), (), "deployment_applicability_schema_unsupported")
    domains, domain_error = _implementation_domains(
        declaration.get("implementation_domains")
        if "implementation_domains" in declaration
        else declaration.get("implementation_domain"),
        required=True,
    )
    if domain_error:
        return _RevisionApplicability((), (), "implementation_domain_declaration_missing")
    affected, affected_error = _implementation_domains(
        declaration.get("affected_implementation_domains")
        if "affected_implementation_domains" in declaration
        else declaration.get("changed_implementation_domains"),
        required=False,
    )
    if affected_error or not set(affected).issubset(domains):
        return _RevisionApplicability((), (), "affected_implementation_domain_declaration_invalid")
    return _RevisionApplicability(domains, affected)


def _implementation_domains(value: Any, *, required: bool) -> tuple[tuple[str, ...], str]:
    if value is None or value == "":
        return ((), "implementation_domain_declaration_missing") if required else ((), "")
    raw_items: Sequence[object]
    if isinstance(value, str):
        raw_items = (value,)
    elif isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        raw_items = value
    else:
        return (), "implementation_domain_declaration_invalid"
    if not 1 <= len(raw_items) <= MAX_IMPLEMENTATION_DOMAINS:
        return (), "implementation_domain_declaration_invalid"
    normalized: list[str] = []
    for raw_item in raw_items:
        item = _text(raw_item, max_chars=MAX_IDENTIFIER_CHARS)
        if not _safe_identifier(item):
            return (), "implementation_domain_declaration_invalid"
        normalized.append(item)
    return tuple(sorted(set(normalized))), ""


def _evidence_descriptor(value: Mapping[str, Any]) -> dict[str, Any]:
    payload = value.get("payload") if isinstance(value.get("payload"), Mapping) else value
    payload = dict(payload) if isinstance(payload, Mapping) else {}
    evidence_type = _text(value.get("evidence_type"), max_chars=64)
    if not evidence_type:
        evidence_type = "evaluation_run" if ("run_id" in value or "run_id" in payload) else "observation"
    if evidence_type not in {"observation", "evaluation_run"}:
        evidence_type = "observation"
    evidence_id = _row_evidence_id(value, evidence_type=evidence_type)
    if not evidence_id:
        evidence_id = "unidentified-evidence"
    return {"evidence_id": evidence_id, "evidence_type": evidence_type, "payload": payload}


def _row_evidence_id(value: Mapping[str, Any], *, evidence_type: str = "") -> str:
    resolved_type = evidence_type or _text(value.get("evidence_type"), max_chars=64)
    key = "run_id" if resolved_type == "evaluation_run" else "observation_id"
    return _text(value.get(key), max_chars=MAX_IDENTIFIER_CHARS) or _text(
        _mapping_value(value.get("payload"), key),
        max_chars=MAX_IDENTIFIER_CHARS,
    )


def _catalog_entity(value: Any) -> _CatalogEntity:
    descriptor = getattr(value, "payload", None)
    if not isinstance(descriptor, Mapping) and isinstance(value, Mapping):
        descriptor = value.get("payload") or value.get("descriptor")
    return _CatalogEntity(
        entity_id=_text(getattr(value, "entity_id", None) if not isinstance(value, Mapping) else value.get("entity_id"), max_chars=MAX_IDENTIFIER_CHARS),
        descriptor=dict(descriptor) if isinstance(descriptor, Mapping) else {},
        status=_text(getattr(value, "status", None) if not isinstance(value, Mapping) else value.get("status"), max_chars=64),
    )


def _verification_base(descriptor: Mapping[str, Any]) -> dict[str, Any]:
    payload = descriptor["payload"]
    return {
        "evidence_id": descriptor["evidence_id"],
        "evidence_type": descriptor["evidence_type"],
        "capability_id": _text(payload.get("capability_id"), max_chars=MAX_IDENTIFIER_CHARS),
        "capability_revision_id": _text(payload.get("capability_revision_id"), max_chars=MAX_IDENTIFIER_CHARS),
        "provider_binding_id": _text(payload.get("provider_binding_id"), max_chars=MAX_IDENTIFIER_CHARS),
    }


def _portable_evidence_result(
    descriptor: Mapping[str, Any],
    authority_error: str,
) -> dict[str, Any]:
    base = _verification_base(descriptor)
    if authority_error:
        return {**base, "ok": False, "required": True, "blocking": True, "reason": authority_error}
    return {
        **base,
        "ok": None,
        "required": False,
        "blocking": False,
        "reason": "deployment_not_declared",
    }


def _public_verification_result(result: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: result.get(key)
        for key in (
            "evidence_id",
            "evidence_type",
            "capability_id",
            "capability_revision_id",
            "provider_binding_id",
            "reason",
            "effective_revision_id",
            "effective_binding_id",
            "compatibility_policy_digest",
        )
        if result.get(key) not in (None, "")
    }


def _release_authority_payload(release: ReleaseIdentity | None) -> dict[str, str]:
    if release is None:
        return {"commit": "", "receipt_id": "", "session_id": ""}
    return {
        "commit": str(release.commit or ""),
        "receipt_id": str(release.receipt_id or ""),
        "session_id": str(release.session_id or ""),
    }


def _non_authoritative_diagnostics(release: ReleaseIdentity | None) -> dict[str, Any]:
    # Retain a bounded version hint for operators without allowing it to affect
    # authority, status, or the assurance digest. Machine metadata is omitted
    # entirely: adapters may retain sanitized diagnostics elsewhere, but it is
    # not a deployment-assurance selector.
    return {
        "release_version": _diagnostic_version(release.version if release is not None else ""),
        "release_version_authoritative": False,
        "machine_identity_used": False,
    }


def _assurance_digest(result: Mapping[str, Any]) -> str:
    """Hash authority-bearing fields only; diagnostics are intentionally out."""

    material = {
        key: result.get(key)
        for key in (
            "schema",
            "ok",
            "required",
            "blocking",
            "status",
            "reason",
            "portable_evidence_count",
            "deployment_dependent_evidence_count",
            "verified_evidence_count",
            "invalidated_evidence_count",
            "current_release",
            "evidence_refs",
            "invalidations",
            "input_truncated",
            "catalog_truncated",
        )
    }
    return _digest(material)


def _bounded_limit(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("deployment evidence limit must be an integer")
    if not 1 <= value <= MAX_DEPLOYMENT_EVIDENCE:
        raise ValueError(f"deployment evidence limit must be from 1 to {MAX_DEPLOYMENT_EVIDENCE}")
    return value


def _mapping_value(value: Any, key: str) -> Any:
    return value.get(key) if isinstance(value, Mapping) else None


def _text(value: Any, *, max_chars: int) -> str:
    text = str(value or "").strip()
    return text if 0 < len(text) <= max_chars else ""


def _safe_identifier(value: str) -> bool:
    return bool(value and _IDENTIFIER_RE.fullmatch(value))


def _sha256_text(value: Any) -> str:
    text = _text(value, max_chars=64).lower()
    return text if _SHA256_RE.fullmatch(text) else ""


def _diagnostic_version(value: Any) -> str:
    text = _text(value, max_chars=128)
    return text if _DIAGNOSTIC_VERSION_RE.fullmatch(text) else ("[REDACTED]" if text else "")


def _digest(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return sha256(encoded.encode("utf-8")).hexdigest()


__all__ = [
    "CAPABILITY_DEPLOYMENT_APPLICABILITY_SCHEMA",
    "CAPABILITY_DEPLOYMENT_AUTHORITY_SCHEMA",
    "CapabilityDeploymentEvidenceService",
    "MAX_DEPLOYMENT_EVIDENCE",
    "RELEASE_APPLICABILITY_SCHEMA",
    "build_capability_deployment_assurance",
    "compatible_evidence_inheritance",
    "environment_constraint_digest",
    "verify_capability_deployment_evidence",
]
