"""Explicit registration of the immutable Hermes code-implementation v9 facts."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
import time
from typing import Any

from eimemory.adapters.hermes.code_implementation import (
    BINDING_ID,
    CAPABILITY_ID,
    IMPLEMENTATION_DIGEST,
    OPERATION,
    PROVIDER_RATE_LIMIT,
    PROVIDER_RATE_WINDOW_SECONDS,
    PROVIDER_INSTANCE_ID,
    PROVIDER_KIND,
    REVISION_ID,
    REQUEST_SCHEMA,
    RESPONSE_SCHEMA,
    CodeImplementationError,
    CodeImplementationSocketClient,
    canonical_json,
    implementation_digest,
)
from eimemory.capabilities.models import CapabilityBinding, CapabilityRevision
from eimemory.capabilities.registry import exact_runtime_scope
from eimemory.core.clock import now_iso


CODE_IMPLEMENTATION_BOOTSTRAP_SCHEMA = "code.implementation.bootstrap.v9"
# The workspace clock is UTC on the previous calendar day while the operator
# date is Asia/Shanghai.  Keep the immutable bootstrap fact at a non-future
# UTC instant so the registry's online timestamp guard remains effective.
CODE_IMPLEMENTATION_CREATED_AT = "2026-08-22T00:00:00Z"
CODE_IMPLEMENTATION_ADAPTER_ID = "hermes.code-implementation"
CODE_IMPLEMENTATION_SOCKET = "/var/lib/eimemory/run/hermes-code-implementation.v2.sock"
LEGACY_REVISION_ID = "code.implementation:v1"
SUPERSEDED_REVISION_IDS = (
    LEGACY_REVISION_ID,
    "code.implementation:v2",
    "code.implementation:v3",
    "code.implementation:v4",
    "code.implementation:v5",
    "code.implementation:v6",
    "code.implementation:v7",
    "code.implementation:v8",
)
PROVIDER_HEALTH_RETRY_ATTEMPTS = 15
PROVIDER_HEALTH_RETRY_DELAY_SECONDS = 2.0
_TRANSIENT_PROVIDER_HEALTH_ERRORS = frozenset(
    {"provider_transport_unavailable", "socket_eof"}
)


def code_implementation_contract() -> dict[str, Any]:
    return {
        "input_schema": {
            "type": "object",
            "additionalProperties": False,
            "resource": "code_implementation_request.v2",
            "required": ["transaction_id", "request_id", "nonce", "incident", "base", "allowed_files", "bounds", "test_plan_id", "test_plan_digest"],
        },
        "output_schema": {
            "type": "object",
            "additionalProperties": False,
            "resource": "code_implementation_response.v2",
            "required": ["request_id", "request_digest", "file_updates", "rationale", "assumptions"],
        },
        "observable_invariants": [
            "response_is_proposal_only",
            "source_fixture_unchanged",
            "provider_attestation_matches_binding",
        ],
        "success_invariants": ["bounded_schema_valid_proposal"],
        "failure_invariants": ["provider_unavailable_is_blocked", "contract_mismatch_is_blocked"],
        "evidence_requirements": {
            "implementation_digest": IMPLEMENTATION_DIGEST,
            "advertisement_ttl_seconds": 3600,
            "catalog_passes": 2,
        },
        "dependencies": [],
        "composition": [],
        "risk_tier": "bounded_write",
        "side_effect_class": "network",
    }


def code_implementation_revision() -> CapabilityRevision:
    return CapabilityRevision(
        revision_id=REVISION_ID,
        capability_id=CAPABILITY_ID,
        contract=code_implementation_contract(),
        compatibility="incompatible",
        created_at=CODE_IMPLEMENTATION_CREATED_AT,
        status="active",
        scope="global",
        provenance={
            "source": "eimemory.code_implementation_bootstrap",
            "schema": CODE_IMPLEMENTATION_BOOTSTRAP_SCHEMA,
            "manual_bootstrap": True,
            "qualifying": False,
        },
        evidence_refs=("bootstrap://code-implementation-v9-contract",),
    )


def code_implementation_binding(*, implementation_digest_value: str = "") -> CapabilityBinding:
    actual_digest = str(IMPLEMENTATION_DIGEST or implementation_digest()).strip().lower()
    supplied_digest = str(implementation_digest_value or "").strip().lower()
    if supplied_digest and supplied_digest != actual_digest:
        raise ValueError("implementation_digest_mismatch")
    digest = actual_digest
    return CapabilityBinding(
        binding_id=BINDING_ID,
        capability_id=CAPABILITY_ID,
        capability_revision_id=REVISION_ID,
        provider_kind=PROVIDER_KIND,
        provider_instance_id=PROVIDER_INSTANCE_ID,
        implementation_digest=digest,
        operations=(OPERATION,),
        limits={
            "request_bytes": 128 * 1024,
            "response_bytes": 256 * 1024,
            "concurrency": 1,
            "requests_per_minute": PROVIDER_RATE_LIMIT,
            "rate_window_seconds": int(PROVIDER_RATE_WINDOW_SECONDS),
            "timeout_seconds": 120,
        },
        environment_fingerprint={
            "implementation_digest": digest,
            "socket_path": CODE_IMPLEMENTATION_SOCKET,
            "operation": OPERATION,
            "structured_completion": True,
        },
        created_at=CODE_IMPLEMENTATION_CREATED_AT,
        status="active",
        scope="global",
        applicability={"capability_id": CAPABILITY_ID, "revision_id": REVISION_ID, "provider_kind": PROVIDER_KIND},
        advertisement_evidence_refs=("bootstrap://code-implementation-v9-binding",),
        provenance={
            "source": "eimemory.code_implementation_bootstrap",
            "schema": CODE_IMPLEMENTATION_BOOTSTRAP_SCHEMA,
            "manual_bootstrap": True,
            "qualifying": False,
        },
    )


def register_code_implementation_v2(
    runtime: Any,
    *,
    runtime_scope: Mapping[str, Any],
    capability_scope: str = "global",
    implementation_digest_value: str = "",
) -> dict[str, Any]:
    """Register the v9 immutable facts and preserve superseded revisions."""

    scope = exact_runtime_scope(runtime_scope)
    resolution = runtime.capabilities.resolve(
        CAPABILITY_ID,
        runtime_scope=scope,
        capability_scope=capability_scope,
        revision_id="",
        binding_id="",
        at_time=CODE_IMPLEMENTATION_CREATED_AT,
    )
    definition = resolution.definition
    if definition is None:
        return {"ok": False, "status": "blocked", "reason": "legacy_definition_required", "qualifying": False}
    expected_definition = {
        "capability_id": CAPABILITY_ID,
        "display_name": "Code implementation",
        "description": "Produce a bounded implementation artifact from a declared contract.",
        "owner": "eimemory",
    }
    definition_descriptor = definition.get("descriptor") if isinstance(definition.get("descriptor"), Mapping) else definition
    if any(str(definition_descriptor.get(key) or "") != value for key, value in expected_definition.items()):
        return {"ok": False, "status": "blocked", "reason": "legacy_definition_changed", "qualifying": False}
    revision = code_implementation_revision()
    binding = code_implementation_binding(implementation_digest_value=implementation_digest_value)
    try:
        revision_receipt = runtime.capabilities.register_revision(
            revision,
            runtime_scope=scope,
            request_key=f"code-implementation-v9:revision:{revision.contract_digest}",
        )
        binding_receipt = runtime.capabilities.bind(
            binding,
            runtime_scope=scope,
            request_key=f"code-implementation-v9:binding:{binding.binding_digest}",
        )
    except Exception as exc:
        return {"ok": False, "status": "blocked", "reason": f"registration_failed:{type(exc).__name__}", "qualifying": False}
    # Superseded revisions are intentionally incompatible. Keeping them active
    # makes the generic Profile resolver reject the capability as ambiguous.
    # Register v9 first, then preserve prior facts through lifecycle events.
    try:
        context = runtime.capabilities.incubation_context(
            CAPABILITY_ID,
            runtime_scope=scope,
            capability_scope=capability_scope,
            limit=16,
        )
        active_revisions = {
            str(row.get("entity_id") or ""): row
            for row in context.get("revisions") or ()
        }
    except Exception as exc:
        return {
            "ok": False,
            "status": "blocked",
            "reason": f"revision_lifecycle_query_failed:{type(exc).__name__}",
            "qualifying": False,
        }
    allowed_revisions = {*SUPERSEDED_REVISION_IDS, REVISION_ID}
    unexpected = sorted(set(active_revisions) - allowed_revisions)
    if unexpected or REVISION_ID not in active_revisions:
        return {
            "ok": False,
            "status": "blocked",
            "reason": "active_revision_set_invalid",
            "active_revision_ids": sorted(active_revisions),
            "qualifying": False,
        }
    superseded_transitions: list[dict[str, Any]] = []
    for superseded_revision_id in SUPERSEDED_REVISION_IDS:
        superseded = active_revisions.get(superseded_revision_id)
        if superseded is None:
            continue
        try:
            transition = runtime.capabilities.transition_status(
                entity_type="revision",
                entity_id=superseded_revision_id,
                entity_digest=str(superseded.get("entity_digest") or ""),
                target_status="deprecated",
                runtime_scope=scope,
                capability_scope=capability_scope,
                expected_state_version=int(superseded.get("state_version") or 0),
                expected_state_digest=str(superseded.get("state_digest") or ""),
                effective_at=now_iso(),
                reason=f"incompatible {REVISION_ID} supersedes {superseded_revision_id}",
                provenance={
                    "source": "eimemory.code_implementation_bootstrap",
                    "schema": CODE_IMPLEMENTATION_BOOTSTRAP_SCHEMA,
                    "superseded_by": REVISION_ID,
                    "manual_bootstrap": True,
                    "qualifying": False,
                },
                request_key=(
                    f"code-implementation-v9:deprecate:{superseded_revision_id}:"
                    f"{superseded.get('state_digest')}:{revision.contract_digest}"
                ),
            )
        except Exception as exc:
            return {
                "ok": False,
                "status": "blocked",
                "reason": f"superseded_revision_deprecation_failed:{type(exc).__name__}",
                "qualifying": False,
            }
        superseded_transitions.append(transition.to_dict())
    legacy_transition = next(
        (
            item
            for item in superseded_transitions
            if item.get("entity_id") == LEGACY_REVISION_ID
        ),
        None,
    )
    return {
        "ok": True,
        "status": "registered",
        "capability_id": CAPABILITY_ID,
        "revision_id": REVISION_ID,
        "binding_id": BINDING_ID,
        "provider_kind": PROVIDER_KIND,
        "provider_instance_id": PROVIDER_INSTANCE_ID,
        "implementation_digest": binding.implementation_digest,
        "contract_digest": revision.contract_digest,
        "revision_receipt": revision_receipt.to_dict(),
        "binding_receipt": binding_receipt.to_dict(),
        "legacy_revision_transition": legacy_transition,
        "superseded_revision_transitions": superseded_transitions,
        "manual_bootstrap": True,
        "qualifying": False,
    }


def advertise_code_implementation_v2(
    runtime: Any,
    *,
    runtime_scope: Mapping[str, Any],
    advertised_at: str,
    expires_at: str,
    capability_scope: str = "global",
    implementation_digest_value: str = "",
) -> dict[str, Any]:
    from eimemory.adapters.runtime.capability import AdapterCapabilityService

    try:
        advertised_time = datetime.fromisoformat(str(advertised_at).replace("Z", "+00:00"))
        expiry_time = datetime.fromisoformat(str(expires_at).replace("Z", "+00:00"))
    except ValueError:
        return {"ok": False, "status": "blocked", "reason": "advertisement_timestamp_invalid", "qualifying": False}
    if (
        advertised_time.tzinfo is None
        or expiry_time.tzinfo is None
        or (expiry_time - advertised_time).total_seconds() != 3600
    ):
        return {"ok": False, "status": "blocked", "reason": "advertisement_ttl_invalid", "qualifying": False}
    binding = code_implementation_binding(implementation_digest_value=implementation_digest_value)
    revision = code_implementation_revision()
    nonce = sha256(f"advertise:{advertised_at}:{expires_at}:{binding.implementation_digest}".encode()).hexdigest()[:32]
    client = CodeImplementationSocketClient()
    health: dict[str, Any] | None = None
    for attempt in range(1, PROVIDER_HEALTH_RETRY_ATTEMPTS + 1):
        try:
            health = client.health(nonce=nonce)
            break
        except CodeImplementationError as exc:
            transient = str(exc) in _TRANSIENT_PROVIDER_HEALTH_ERRORS
            if not transient or attempt >= PROVIDER_HEALTH_RETRY_ATTEMPTS:
                return {
                    "ok": False,
                    "status": "blocked",
                    "reason": "provider_health_unavailable",
                    "qualifying": False,
                }
            time.sleep(PROVIDER_HEALTH_RETRY_DELAY_SECONDS)
    if health is None:
        raise AssertionError("provider health retry loop exited without a result")
    health_digest = sha256(canonical_json(health).encode()).hexdigest()
    service = AdapterCapabilityService(runtime, adapter_id=CODE_IMPLEMENTATION_ADAPTER_ID, provider_kind=PROVIDER_KIND)
    result = service.advertise_capabilities(
        {
            "advertisement_id": f"advertisement.hermes.code-implementation:{sha256(f'{advertised_at}:{expires_at}'.encode()).hexdigest()[:24]}",
            "advertisement_revision": "v6",
            "binding_id": binding.binding_id,
            "capability_revision_id": revision.revision_id,
            "provider_instance_id": binding.provider_instance_id,
            "contract_digest": revision.contract_digest,
            "operations": [OPERATION],
            "limits": dict(binding.limits),
            "side_effect_class": "network",
            "host_event_types": ["code.implementation.propose_patch_v2"],
            "environment_fingerprint": {
                "implementation_digest": binding.implementation_digest,
                "provider_instance_id": binding.provider_instance_id,
                "operation": OPERATION,
                "health_attestation_digest": health_digest,
            },
            "applicability": {"capability_id": CAPABILITY_ID, "revision_id": REVISION_ID},
            "evidence_refs": [
                "bootstrap://code-implementation-v9-advertisement",
                f"provider-health://{health_digest}",
            ],
            "advertised_at": advertised_at,
            "expires_at": expires_at,
            "created_at": advertised_at,
            "status": "active",
            "capability_scope": capability_scope,
            "provenance": {
                "source": "eimemory.code_implementation_bootstrap",
                "manual_bootstrap": True,
                "qualifying": False,
            },
        },
        runtime_scope=runtime_scope,
        now=advertised_at,
    )
    return {
        **result,
        "capability_id": CAPABILITY_ID,
        "revision_id": REVISION_ID,
        "binding_id": BINDING_ID,
        "implementation_digest": binding.implementation_digest,
        "health_attestation_digest": health_digest,
        "manual_bootstrap": True,
        "qualifying": False,
    }


__all__ = [
    "BINDING_ID",
    "CAPABILITY_ID",
    "CODE_IMPLEMENTATION_ADAPTER_ID",
    "CODE_IMPLEMENTATION_BOOTSTRAP_SCHEMA",
    "LEGACY_REVISION_ID",
    "OPERATION",
    "PROVIDER_INSTANCE_ID",
    "REVISION_ID",
    "SUPERSEDED_REVISION_IDS",
    "advertise_code_implementation_v2",
    "code_implementation_binding",
    "code_implementation_contract",
    "code_implementation_revision",
    "register_code_implementation_v2",
]
