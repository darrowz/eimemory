from __future__ import annotations

from dataclasses import asdict
from typing import Any

from eimemory.core.clock import now_iso
from eimemory.models.records import ScopeRef


def invalidate_dynamic_binding_before_apply(
    runtime: Any,
    *,
    code_patch: dict[str, Any],
    scope: dict[str, Any] | ScopeRef,
    opportunity_id: str,
) -> dict[str, Any]:
    """Stale a dynamic implementation claim immediately before repository write.

    This lives below the autonomous planner so every code-patch promotion uses
    the same lifecycle order: all non-mutating promotion gates first, then the
    binding transition, then the first repository write.  A failed projection
    refresh is compensated while no write has started.
    """

    scope_ref = scope if isinstance(scope, ScopeRef) else ScopeRef.from_dict(scope)
    scope_payload = asdict(scope_ref)
    context = code_patch.get("capability_hypothesis")
    if context is None:
        return {"required": False, "ok": True, "status": "not_dynamic_capability_patch"}
    if not isinstance(context, dict):
        return {"required": True, "ok": False, "reason": "dynamic_hypothesis_context_invalid"}
    binding_id = str(code_patch.get("provider_binding_id") or "").strip()
    capability_id = str(context.get("capability_id") or code_patch.get("target_capability") or "").strip()
    revision_id = str(context.get("capability_revision_id") or code_patch.get("capability_revision_id") or "").strip()
    capability_scope = str(context.get("capability_scope") or "").strip()
    if not binding_id or not capability_id or not revision_id or not capability_scope:
        return {"required": True, "ok": False, "reason": "dynamic_binding_context_incomplete"}
    service = getattr(runtime, "capabilities", None)
    binding_context = getattr(service, "binding_context", None)
    transition = getattr(service, "transition_status", None)
    if not callable(binding_context) or not callable(transition):
        return {"required": True, "ok": False, "reason": "capability_lifecycle_service_unavailable"}
    try:
        current = binding_context(
            binding_id,
            runtime_scope=scope_payload,
            capability_scope=capability_scope,
        )
    except Exception as exc:
        return {"required": True, "ok": False, "reason": f"binding_context_error:{type(exc).__name__}"}
    if not isinstance(current, dict):
        return {"required": True, "ok": False, "reason": "dynamic_binding_not_found"}
    descriptor = current.get("descriptor") if isinstance(current.get("descriptor"), dict) else {}
    if (
        str(descriptor.get("capability_id") or "") != capability_id
        or str(descriptor.get("capability_revision_id") or "") != revision_id
    ):
        return {"required": True, "ok": False, "reason": "dynamic_binding_target_mismatch"}
    status = str(current.get("status") or "")
    base = {
        "required": True,
        "binding_id": binding_id,
        "capability_id": capability_id,
        "capability_scope": capability_scope,
        "profile_key": str(code_patch.get("profile_key") or ""),
        "implementation_digest": str(descriptor.get("implementation_digest") or ""),
        "opportunity_id": opportunity_id,
    }
    if status == "stale":
        projection_refresh = refresh_dynamic_capability_state(
            runtime,
            scope=scope_payload,
            code_patch=code_patch,
            capability_id=capability_id,
            capability_scope=capability_scope,
        )
        if projection_refresh.get("ok") is not True:
            return {
                **base,
                "ok": False,
                "reason": str(projection_refresh.get("reason") or "dynamic_projection_refresh_failed"),
                "status": "already_stale",
                "projection_refresh": projection_refresh,
            }
        return {
            **base,
            "ok": True,
            "status": "already_stale",
            "projection_refresh": projection_refresh,
        }
    if status != "active":
        return {**base, "ok": False, "reason": f"dynamic_binding_not_active:{status or 'unknown'}"}
    try:
        receipt = transition(
            entity_type="binding",
            entity_id=binding_id,
            entity_digest=str(current.get("entity_digest") or ""),
            target_status="stale",
            runtime_scope=scope_payload,
            capability_scope=capability_scope,
            expected_state_version=int(current.get("state_version") or 0),
            expected_state_digest=str(current.get("state_digest") or ""),
            effective_at=now_iso(),
            reason="dynamic_code_evolution_pending_rebind",
            provenance={
                "source": "eimemory.governance.promotion_manager",
                "opportunity_id": opportunity_id,
                "capability_id": capability_id,
                "capability_revision_id": revision_id,
                "previous_implementation_digest": str(descriptor.get("implementation_digest") or ""),
            },
            request_key=f"dynamic-binding-stale:{opportunity_id}:{binding_id}",
        )
    except Exception as exc:
        return {**base, "ok": False, "reason": f"binding_stale_transition_error:{type(exc).__name__}"}
    receipt_payload = receipt.to_dict() if callable(getattr(receipt, "to_dict", None)) else {}
    result = {**base, "ok": True, "status": "stale_transitioned", "transition": receipt_payload}
    projection_refresh = refresh_dynamic_capability_state(
        runtime,
        scope=scope_payload,
        code_patch=code_patch,
        capability_id=capability_id,
        capability_scope=capability_scope,
    )
    result["projection_refresh"] = projection_refresh
    if projection_refresh.get("ok") is True:
        return result
    compensation = restore_dynamic_binding_after_unstarted_apply(
        runtime,
        scope=scope_payload,
        invalidation=result,
        reason="dynamic_projection_refresh_failed_before_write",
    )
    return {
        **result,
        "ok": False,
        "reason": str(projection_refresh.get("reason") or "dynamic_projection_refresh_failed"),
        "compensation": compensation,
    }


def restore_dynamic_binding_after_unstarted_apply(
    runtime: Any,
    *,
    scope: dict[str, Any] | ScopeRef,
    invalidation: dict[str, Any],
    reason: str,
) -> dict[str, Any]:
    """Restore a binding only when this promotion staled it before any write."""

    if str(invalidation.get("status") or "") != "stale_transitioned":
        return {"ok": True, "status": "not_required"}
    scope_ref = scope if isinstance(scope, ScopeRef) else ScopeRef.from_dict(scope)
    scope_payload = asdict(scope_ref)
    binding_id = str(invalidation.get("binding_id") or "").strip()
    capability_scope = str(invalidation.get("capability_scope") or "").strip()
    capability_id = str(invalidation.get("capability_id") or "").strip()
    if not binding_id or not capability_scope or not capability_id:
        return {"ok": False, "reason": "dynamic_binding_compensation_context_incomplete"}
    service = getattr(runtime, "capabilities", None)
    binding_context = getattr(service, "binding_context", None)
    transition = getattr(service, "transition_status", None)
    if not callable(binding_context) or not callable(transition):
        return {"ok": False, "reason": "capability_lifecycle_service_unavailable"}
    try:
        current = binding_context(
            binding_id,
            runtime_scope=scope_payload,
            capability_scope=capability_scope,
        )
    except Exception as exc:
        return {"ok": False, "reason": f"binding_compensation_context_error:{type(exc).__name__}"}
    if not isinstance(current, dict):
        return {"ok": False, "reason": "dynamic_binding_not_found"}
    if str(current.get("status") or "") != "stale":
        return {"ok": False, "reason": f"dynamic_binding_compensation_not_stale:{str(current.get('status') or 'unknown')}"}
    try:
        receipt = transition(
            entity_type="binding",
            entity_id=binding_id,
            entity_digest=str(current.get("entity_digest") or ""),
            target_status="active",
            runtime_scope=scope_payload,
            capability_scope=capability_scope,
            expected_state_version=int(current.get("state_version") or 0),
            expected_state_digest=str(current.get("state_digest") or ""),
            effective_at=now_iso(),
            reason=reason,
            provenance={
                "source": "eimemory.governance.promotion_manager",
                "binding_id": binding_id,
                "capability_id": capability_id,
                "compensates": "dynamic_code_evolution_pending_rebind",
            },
            request_key=f"dynamic-binding-restore:{invalidation.get('opportunity_id') or binding_id}:{binding_id}",
        )
    except Exception as exc:
        return {"ok": False, "reason": f"binding_restore_transition_error:{type(exc).__name__}"}
    receipt_payload = receipt.to_dict() if callable(getattr(receipt, "to_dict", None)) else {}
    return {"ok": True, "status": "restored_active", "binding_id": binding_id, "transition": receipt_payload}


def refresh_dynamic_capability_state(
    runtime: Any,
    *,
    scope: dict[str, Any] | ScopeRef,
    code_patch: dict[str, Any],
    capability_id: str,
    capability_scope: str,
) -> dict[str, Any]:
    """Persist a conservative affected projection and L5 re-evaluation."""

    scope_ref = scope if isinstance(scope, ScopeRef) else ScopeRef.from_dict(scope)
    scope_payload = asdict(scope_ref)
    profile_key = str(code_patch.get("profile_key") or "").strip()
    if not profile_key:
        return {"ok": False, "reason": "dynamic_profile_key_missing"}
    store = getattr(runtime, "store", None)
    if store is None:
        return {"ok": False, "reason": "dynamic_projection_store_unavailable"}
    try:
        from eimemory.capabilities.projector import CapabilityStateProjector

        projection = CapabilityStateProjector(store).project_affected(
            profile_key,
            runtime_scope=scope_payload,
            capability_scope=capability_scope,
            affected_capability_ids=[capability_id],
            max_candidates=100,
            observation_limit=500,
            persist=True,
        ).to_dict()
    except Exception as exc:
        return {"ok": False, "reason": f"dynamic_projection_failed:{type(exc).__name__}"}
    try:
        from eimemory.governance.l5_assessment_v3 import build_l5_assessment_v3

        assessment = build_l5_assessment_v3(
            runtime,
            profile_key=profile_key,
            scope=scope_payload,
            capability_scope=capability_scope,
            persist=True,
            max_candidates=100,
            observation_limit=500,
        )
    except Exception as exc:
        return {
            "ok": False,
            "reason": f"dynamic_l5_reassessment_failed:{type(exc).__name__}",
            "projection": projection,
        }
    return {
        "ok": True,
        "profile_key": profile_key,
        "affected_capability_ids": [capability_id],
        "projection": projection,
        "assessment": assessment,
        "assessment_ready": assessment.get("ok") is True if isinstance(assessment, dict) else False,
    }
