"""Explicit, taxonomy-free bootstrap for the default L5 profile.

The profile is intentionally a selector over whatever definitions are active
in an exact runtime/capability scope.  It is not installed during Runtime
construction: an operator or migration must explicitly invoke the bootstrap,
which keeps an empty registry truthful and avoids silently creating a legacy
capability universe.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from eimemory.capabilities.models import CapabilityProfile
from eimemory.capabilities.registry import MutationReceipt, exact_runtime_scope
from eimemory.models.records import ScopeRef


DEFAULT_L5_PROFILE_ID = "l5.default:v1"
DEFAULT_L5_PROFILE_KEY = "l5.default"
DEFAULT_L5_PROFILE_REVISION = "v1"
DEFAULT_L5_PROFILE_CREATED_AT = "2026-08-19T00:00:00+00:00"
DEFAULT_L5_PROFILE_SCHEMA = "capability.profile.bootstrap.v1"


def default_l5_profile(*, capability_scope: str = "global") -> CapabilityProfile:
    """Build the immutable generic L5 profile descriptor.

    A newly activated definition automatically enters this profile through the
    constrained lifecycle selector.  Its own revision, binding, catalog
    cases, evidence, dependencies, and deployment applicability still decide
    whether it can ever become ready; the profile does not grant maturity.
    """

    return CapabilityProfile(
        profile_id=DEFAULT_L5_PROFILE_ID,
        profile_key=DEFAULT_L5_PROFILE_KEY,
        requirements={
            "all_active_capabilities": {
                "selector": {"statuses_any": ["active"]},
                "minimum_maturity": "reliable",
                "min_pass_rate": 0.8,
                "min_evidence_count": 3,
                "min_sample_count": 3,
                "min_consecutive_passes": 2,
                "require_dependencies": True,
                "priority": 100,
                "planning_policy": {
                    "user_value": 0.5,
                    "risk": 0.5,
                    "cost": 0.5,
                    "priority_weight": 0.5,
                },
            }
        },
        created_at=DEFAULT_L5_PROFILE_CREATED_AT,
        status="active",
        scope=capability_scope,
        revision=DEFAULT_L5_PROFILE_REVISION,
        provenance={
            "source": "eimemory.capabilities.profile_bootstrap",
            "schema": DEFAULT_L5_PROFILE_SCHEMA,
            "selection": "active_definitions_only",
        },
    )


def ensure_default_l5_profile(
    runtime: Any,
    *,
    runtime_scope: ScopeRef | Mapping[str, Any],
    capability_scope: str = "global",
    request_key: str = "",
) -> MutationReceipt:
    """Explicitly install or replay the generic L5 profile through the service.

    The operation is idempotent because the profile descriptor and request key
    are stable.  It deliberately does not activate definitions, register
    bindings, or add evaluation cases.
    """

    scope = exact_runtime_scope(runtime_scope)
    service = getattr(runtime, "capabilities", None)
    register = getattr(service, "register_profile", None)
    if not callable(register):
        raise RuntimeError("runtime capability profile service is unavailable")
    profile = default_l5_profile(capability_scope=capability_scope)
    return register(
        profile,
        runtime_scope=scope,
        request_key=request_key or f"profile-bootstrap:{profile.profile_digest}",
    )


__all__ = [
    "DEFAULT_L5_PROFILE_CREATED_AT",
    "DEFAULT_L5_PROFILE_ID",
    "DEFAULT_L5_PROFILE_KEY",
    "DEFAULT_L5_PROFILE_REVISION",
    "DEFAULT_L5_PROFILE_SCHEMA",
    "default_l5_profile",
    "ensure_default_l5_profile",
]
