"""Reversible reader selection for legacy L5 and dynamic L5 v3.

The selection is deliberately a deployment policy rather than a version,
machine, or health check.  ``legacy`` remains available only for the declared
rollback window, ``shadow`` preserves the legacy response while attaching a
comparison, and ``v3`` makes the profile-backed four-axis assessment the
primary L5 result.  No mode synthesizes a capability taxonomy.
"""

from __future__ import annotations

from collections.abc import Mapping
import os
from typing import Any

from eimemory.capabilities.profile_bootstrap import DEFAULT_L5_PROFILE_KEY
from eimemory.models.records import ScopeRef


L5_READER_SCHEMA = "l5.reader.v3"
_READER_MODES = frozenset({"legacy", "shadow", "v3"})


class L5ReaderError(ValueError):
    """A requested L5 reader mode is malformed or lacks its profile."""


def resolve_l5_reader_mode(mode: str = "") -> str:
    """Resolve a bounded, explicit deployment reader mode.

    V3 is the production default.  ``legacy`` is an explicit rollback-window
    selection only; when the mandatory profile is not configured, v3 blocks
    visibly instead of silently reviving a compiled capability taxonomy.
    """

    candidate = str(mode or os.environ.get("EIMEMORY_L5_READER_MODE") or "v3").strip().lower()
    if candidate not in _READER_MODES:
        raise L5ReaderError("l5 reader mode must be legacy, shadow, or v3")
    return candidate


def build_l5_effective_report(
    runtime: Any,
    *,
    scope: Mapping[str, Any] | ScopeRef | None = None,
    persist: bool = False,
    limit: int = 500,
    loop_id: str = "l5_readiness",
    repo_root: str = "/dev-project/eimemory",
    reader_mode: str = "",
    profile_key: str = "",
    capability_scope: str = "global",
    runtime_scope: Mapping[str, Any] | ScopeRef | None = None,
    at_time: str = "",
    catalog: Any | None = None,
) -> dict[str, Any]:
    """Read L5 through a policy-selected primary reader.

    This function is the sole runtime cutover seam.  It never writes a reader
    selection, never converts legacy scores into v3 observations, and never
    lets a missing profile silently fall back when v3 was selected.
    """

    mode = resolve_l5_reader_mode(reader_mode)
    profile = str(
        profile_key
        or os.environ.get("EIMEMORY_L5_V3_PROFILE")
        or DEFAULT_L5_PROFILE_KEY
    ).strip()
    scope_ref = scope if isinstance(scope, ScopeRef) else ScopeRef.from_dict(scope)
    # ``scope`` remains the evidence/report scope.  Dynamic Profile resolution
    # can intentionally use a different exact runtime scope, but it must be
    # supplied by the caller rather than inferred from a machine or release.
    selector_scope = runtime_scope if runtime_scope is not None else scope_ref
    if mode == "legacy":
        return _legacy_report(
            runtime,
            scope=scope_ref,
            persist=persist,
            limit=limit,
            loop_id=loop_id,
            repo_root=repo_root,
            mode=mode,
            profile_key=profile,
            capability_scope=capability_scope,
            runtime_scope=runtime_scope,
            at_time=at_time,
            catalog=catalog,
        )
    if not profile:
        return {
            "schema": L5_READER_SCHEMA,
            "schema_version": "l5_readiness.v3",
            "report_type": "l5_readiness_report",
            "reader_mode": mode,
            "ok": False,
            "status": "blocked",
            "reason": "l5_v3_profile_not_configured",
            "capability_scope": capability_scope,
            "scope": {
                "tenant_id": scope_ref.tenant_id,
                "agent_id": scope_ref.agent_id,
                "workspace_id": scope_ref.workspace_id,
                "user_id": scope_ref.user_id,
            },
        }
    if mode == "v3":
        from eimemory.governance.l5_assessment_v3 import build_l5_assessment_v3

        assessment = build_l5_assessment_v3(
            runtime,
            profile_key=profile,
            scope=selector_scope,
            capability_scope=capability_scope,
            persist=persist,
            at_time=at_time,
            max_candidates=min(499, max(1, int(limit))),
            observation_limit=min(500, max(1, int(limit))),
        )
        return _v3_readiness_envelope(
            assessment,
            scope=scope_ref,
            profile_key=profile,
            capability_scope=capability_scope,
        )
    from eimemory.governance.l5_shadow import build_l5_v3_shadow

    shadow = build_l5_v3_shadow(
        runtime,
        profile_key=profile,
        scope=scope_ref,
        runtime_scope=runtime_scope,
        capability_scope=capability_scope,
        persist=persist,
        at_time=at_time,
        max_candidates=min(499, max(1, int(limit))),
        observation_limit=min(500, max(1, int(limit))),
        repo_root=repo_root,
        catalog=catalog,
    )
    legacy = dict(shadow.get("v2") or {})
    legacy["reader_mode"] = "shadow"
    legacy["reader_schema"] = L5_READER_SCHEMA
    legacy["l5_v3_shadow"] = shadow
    legacy["l5_v3_profile_key"] = profile
    legacy["l5_v3_capability_scope"] = capability_scope
    return legacy


def _legacy_report(
    runtime: Any,
    *,
    scope: ScopeRef,
    persist: bool,
    limit: int,
    loop_id: str,
    repo_root: str,
    mode: str,
    profile_key: str,
    capability_scope: str,
    runtime_scope: Mapping[str, Any] | ScopeRef | None,
    at_time: str,
    catalog: Any | None,
) -> dict[str, Any]:
    from eimemory.governance.l5_readiness import build_l5_readiness_report

    report = build_l5_readiness_report(
        runtime,
        scope=scope,
        persist=persist,
        limit=limit,
        loop_id=loop_id,
        repo_root=repo_root,
        profile_key=profile_key,
        capability_scope=capability_scope,
        runtime_scope=runtime_scope,
        at_time=at_time,
        catalog=catalog,
        # The reader's rollback path is the one explicit consumer allowed to
        # reproduce the frozen v2 taxonomy.  The readiness implementation now
        # defaults to dynamic profile selection, so this intent must be
        # carried explicitly rather than inferred from a reader mode string.
        legacy_compatibility=True,
    )
    result = dict(report)
    result["reader_mode"] = mode
    result["reader_schema"] = L5_READER_SCHEMA
    return result


def _v3_readiness_envelope(
    assessment: Mapping[str, Any],
    *,
    scope: ScopeRef,
    profile_key: str,
    capability_scope: str,
) -> dict[str, Any]:
    """Expose a bounded familiar envelope without flattening v3 axes."""

    loop_maturity = str(assessment.get("loop_maturity") or "observing")
    capability_ready = bool(assessment.get("ok") is True)
    deployment = assessment.get("deployment_assurance") if isinstance(assessment.get("deployment_assurance"), Mapping) else {}
    adapters = assessment.get("adapter_readiness") if isinstance(assessment.get("adapter_readiness"), Mapping) else {}
    adapter_ready = bool(adapters) and all(str(value) == "ready" for value in adapters.values())
    # Deployment remains an independent assurance axis.  No explicit
    # deployment-dependent evidence is a neutral, visible state (``None``),
    # not a fabricated green deployment result and not a reason to reset the
    # profile-backed cognitive result.  A declared requirement must, however,
    # pass its immutable commit/receipt/session verification.
    deployment_present = bool(deployment)
    deployment_required = deployment.get("required") is True
    deployment_blocking = deployment.get("blocking") is True or not deployment_present
    deployment_ready = (
        deployment.get("ok") is True
        if deployment_required
        else (None if deployment_present else False)
    )
    deployment_gate_ok = bool(
        not deployment_blocking
        and (not deployment_required or deployment_ready is True)
    )
    # Do not retain an upstream ``ready`` label after this reader has applied
    # the independent adapter/deployment gates.  In particular, a declared
    # deployment requirement that fails must be visible as a blocked reader
    # result even though it deliberately does not rewrite the cognitive axis.
    upstream_status = str(assessment.get("status") or "")
    if upstream_status == "blocked":
        status = "blocked"
    elif capability_ready and adapter_ready and deployment_gate_ok:
        status = "ready"
    elif deployment_blocking:
        status = "blocked"
    elif not adapter_ready:
        status = "degraded"
    else:
        status = upstream_status if upstream_status and upstream_status != "ready" else "degraded"
    return {
        "schema": L5_READER_SCHEMA,
        "schema_version": "l5_readiness.v3",
        "report_type": "l5_readiness_report",
        "reader_mode": "v3",
        "ok": capability_ready and adapter_ready and deployment_gate_ok,
        "status": status,
        "profile_key": profile_key,
        "capability_scope": capability_scope,
        "scope": {
            "tenant_id": scope.tenant_id,
            "agent_id": scope.agent_id,
            "workspace_id": scope.workspace_id,
            "user_id": scope.user_id,
        },
        "loop_maturity": loop_maturity,
        "capability_ready": capability_ready,
        "adapter_ready": adapter_ready,
        "deployment_ready": deployment_ready,
        "deployment_required": deployment_required,
        "deployment_blocking": deployment_blocking,
        "assessment": dict(assessment),
        "gaps": list(assessment.get("gaps") or ()),
    }


__all__ = [
    "L5_READER_SCHEMA",
    "L5ReaderError",
    "build_l5_effective_report",
    "resolve_l5_reader_mode",
]
