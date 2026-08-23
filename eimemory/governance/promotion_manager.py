from __future__ import annotations

from base64 import b64decode, b64encode
from dataclasses import asdict
import fnmatch
from hashlib import sha256
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import stat
import subprocess
import sys
import tempfile
from typing import Any

from eimemory.governance.code_automation_policy import (
    CODE_AUTOMATION_POLICY_DEFAULT_PATH,
    CODE_AUTOMATION_POLICY_PATH_ENV,
    load_code_automation_policy,
    machine_policy_context_from_mapping,
)
from eimemory.governance.capability_binding_lifecycle import (
    invalidate_dynamic_binding_before_apply,
    restore_dynamic_binding_after_unstarted_apply,
)
from eimemory.governance.capability_hypotheses import hypothesis_behavior_gate
from eimemory.governance.code_patch_command_policy import (
    code_patch_verification_command_error,
)
from eimemory.governance.learning_eval import REGRESSION_THRESHOLD, SAFETY_THRESHOLD
from eimemory.governance.learning_state import append_learning_record_once, stable_semantic_key
from eimemory.governance.promotion_watch import WATCH_STATUS, initialize_promotion_watch
from eimemory.governance.rollout_lifecycle import record_lifecycle_event, standardized_lifecycle_details
from eimemory.models.records import RecordEnvelope, ScopeRef

POLICY_TARGETS = {"tool_route", "prompt_policy", "system_prompt_patch"}
PLAYBOOK_TARGETS = {"eval_case", "skill_draft", "sop_draft", "source_policy"}
CODE_ASSET_TARGETS = {"code_patch"}
UNSUPPORTED_ACTIVE_TARGETS = {"deployment_rollout", "scheduler_policy"}
CAPABILITY_ROLLOUT_ACTION = "capability_promotion"
CODE_APPLY_TRANSACTION_SOURCE = "eimemory.code_apply_transaction"
CODE_APPLY_TRANSACTION_SCHEMA_VERSION = 1
CODE_APPLY_TRANSACTION_IN_FLIGHT = "in_flight"
CODE_APPLY_TRANSACTION_QUARANTINED = "recovery_quarantined"
MACHINE_POLICY_ACTION_LOCAL_APPLY = "local_apply"
MACHINE_POLICY_ACTION_COMMIT = "commit"
MACHINE_POLICY_ACTION_DEPLOYMENT = "deployment"

# L3+ candidates must declare the active safety controls that participate in
# the production governance path.  The names are supplied in the candidate's
# ``safety_wire`` content field and checked before any state mutation runs.
REQUIRED_SAFETY_CONTROLS_L3_PLUS = (
    "kill_switch",
    "audit_verifier",
    "safety_replay",
    "promotion_manager",
)


# A compatibility claim in a persisted candidate is untrusted input: any
# caller able to edit a candidate could otherwise erase a v3 hypothesis and
# re-enable historical code writes.  Only this process-local capability can
# authorize the narrow legacy migration path.  It binds the exact candidate
# digest and scope and cannot be represented in CLI/JSON payloads.
_LEGACY_PROMOTION_AUTHORITY_NONCE = object()


class _LegacyPromotionAuthority:
    __slots__ = ("candidate_id", "scope", "candidate_digest", "_nonce")

    def __init__(self, *, candidate_id: str, scope: ScopeRef, candidate_digest: str) -> None:
        self.candidate_id = candidate_id
        self.scope = scope
        self.candidate_digest = candidate_digest
        self._nonce = _LEGACY_PROMOTION_AUTHORITY_NONCE


def _issue_legacy_promotion_authority(
    runtime: Any,
    *,
    candidate_id: str,
    scope: dict[str, Any] | ScopeRef | None = None,
) -> object:
    """Create an in-memory-only authority for a known legacy migration call."""

    candidate = runtime.store.get_by_id(candidate_id, scope=scope)
    if candidate is None or candidate.kind != "capability_candidate":
        raise ValueError(f"capability candidate not found: {candidate_id}")
    return _LegacyPromotionAuthority(
        candidate_id=candidate.record_id,
        scope=candidate.scope,
        candidate_digest=_candidate_promotion_digest(candidate),
    )


def _candidate_promotion_digest(candidate: RecordEnvelope) -> str:
    payload = {
        "candidate_id": candidate.record_id,
        "scope": asdict(candidate.scope),
        "promotion_target": _promotion_target(candidate),
        "target_capability": _candidate_target_capability(candidate, {}),
        "candidate_patch": dict((candidate.content or {}).get("candidate_patch") or {}),
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return sha256(encoded.encode("utf-8")).hexdigest()


def _has_legacy_promotion_authority(candidate: RecordEnvelope, authority: object | None) -> bool:
    return bool(
        isinstance(authority, _LegacyPromotionAuthority)
        and authority._nonce is _LEGACY_PROMOTION_AUTHORITY_NONCE
        and authority.candidate_id == candidate.record_id
        and authority.scope == candidate.scope
        and authority.candidate_digest == _candidate_promotion_digest(candidate)
    )


def _check_safety_wire(*, authority_tier: str, safety_wire: tuple[str, ...] | list[str]) -> None:
    """Reject L3+ candidates whose safety wire omits active controls.

    Tiers below L3 (L0, L1, L2) do not require a wire and pass through.
    L3 and L4 require every control in REQUIRED_SAFETY_CONTROLS_L3_PLUS.
    Raises ValueError when any required module is absent.
    """
    tier = str(authority_tier or "").upper()
    if tier not in {"L3", "L4"}:
        return
    wire = set(safety_wire or ())
    missing = set(REQUIRED_SAFETY_CONTROLS_L3_PLUS) - wire
    if missing:
        raise ValueError(
            f"safety_wire missing required modules for {tier}: {sorted(missing)}"
        )


def _enforce_harness_patch_v2(runtime: Any, candidate: Any, *, scope: Any) -> None:
    """Run candidate_search v2 enforce_* checks when HARNESS_PATCH_V2=1.

    Re-reads the env var at call time so flipping it after process start (or
    monkeypatching in tests) takes effect. Imports the helpers lazily so the
    import graph stays cheap when v2 is off.
    """
    import os
    if os.environ.get("HARNESS_PATCH_V2") != "1":
        return
    card_data = ((candidate.content or {}).get("proposal_card") if candidate.content else None)
    if not card_data or not isinstance(card_data, dict):
        return
    from eimemory.governance.candidate_search import (
        enforce_diff_size,
        enforce_one_active_per_surface,
    )
    enforce_diff_size(
        diff_lines=_int_value(card_data.get("diff_lines"), default=0),
        diff_tokens=_int_value(card_data.get("diff_tokens"), default=0),
    )
    surface = str(card_data.get("target_surface") or "")
    if not surface:
        return
    # Collect surfaces of currently-active candidates (excluding the one we are
    # about to promote) so enforce_one_active_per_surface can reject duplicates.
    active_surfaces: list[dict[str, Any]] = []
    try:
        for rec in runtime.store.list_records(
            kinds=["capability_candidate"],
            scope=scope,
            limit=500,
        ):
            if rec.record_id == candidate.record_id:
                continue
            status = str(rec.meta.get("status") or "")
            if status not in {"active", "applied", "watch"}:
                continue
            other_card = (rec.content or {}).get("proposal_card") if rec.content else None
            if isinstance(other_card, dict):
                other_surface = str(other_card.get("target_surface") or "")
                if other_surface:
                    active_surfaces.append({"target_surface": other_surface, "id": rec.record_id})
    except Exception:
        # Best-effort: if the store cannot enumerate, skip the check rather
        # than block legitimate promotions on a transient lookup failure.
        active_surfaces = []
    enforce_one_active_per_surface(new_surface=surface, active_surfaces=active_surfaces)


def rollback_capability_candidate(
    runtime: Any,
    *,
    candidate_id: str,
    scope: dict[str, Any] | ScopeRef | None = None,
    loop_id: str = "manual_rollback",
    reason: str = "manual rollback via eimemory patch rollback",
) -> dict[str, Any]:
    """Roll back a single ``capability_candidate`` to ``rolled_back`` status.

    This is the *capability* counterpart of the code-patch ``_rollback_evidence``
    helper: it flips the candidate's status, records a lifecycle event, and
    returns a JSON-friendly summary so the CLI can print it.

    The function is idempotent: rolling back an already-rolled-back candidate
    returns ``ok=True`` without writing a second lifecycle record.
    """
    candidate = runtime.store.get_by_id(candidate_id, scope=scope)
    if candidate is None or candidate.kind != "capability_candidate":
        raise ValueError(f"capability candidate not found: {candidate_id}")
    # Read from ``candidate.status`` (the field mutate operations on) to stay
    # consistent with promote_candidate's writes.
    previous_status = str(candidate.status or candidate.meta.get("status") or "")
    if previous_status == "rolled_back":
        return {
            "ok": True,
            "candidate_id": candidate_id,
            "previous_status": previous_status,
            "new_status": "rolled_back",
            "already_rolled_back": True,
        }
    _record_candidate_lifecycle(
        runtime,
        candidate,
        scope=scope,
        action_type="rolled_back",
        reason=reason,
        details={"previous_status": previous_status},
    )
    candidate.status = "rolled_back"
    candidate.meta["rolled_back_by"] = "eimemory.cli.patch"
    candidate.meta["rolled_back_at_loop_id"] = loop_id
    candidate.meta["rolled_back_reason"] = reason
    runtime.store.rewrite(candidate)
    return {
        "ok": True,
        "candidate_id": candidate_id,
        "previous_status": previous_status,
        "new_status": "rolled_back",
    }


def promote_candidate(
    runtime: Any,
    *,
    candidate_id: str,
    scope: dict[str, Any] | ScopeRef | None = None,
    loop_id: str = "manual",
    apply: bool = True,
    eval_result: dict[str, Any] | None = None,
    health: dict[str, Any] | None = None,
    legacy_authority: object | None = None,
) -> dict[str, Any]:
    candidate = runtime.store.get_by_id(candidate_id, scope=scope)
    if candidate is None or candidate.kind != "capability_candidate":
        raise ValueError(f"capability candidate not found: {candidate_id}")
    tier = str(candidate.meta.get("authority_tier") or candidate.content.get("authority_tier") or "L0").upper()
    _record_candidate_lifecycle(runtime, candidate, scope=scope, action_type="proposed")
    try:
        _check_safety_wire(
            authority_tier=tier,
            safety_wire=tuple((candidate.content or {}).get("safety_wire") or ()),
        )
    except ValueError as exc:
        gate = {"ok": False, "blocked_reasons": ["safety_wire_missing"], "error": str(exc)}
        eval_payload = eval_result or candidate.content.get("eval_result") or {}
        health_payload = health or {"ok": True}
        _record_candidate_lifecycle(runtime, candidate, scope=scope, action_type="gate_failed", test_result=eval_payload, health_result=health_payload, reason="safety_wire_missing", details={"gate": gate})
        request_id = _promotion_record(runtime, candidate, scope=scope, loop_id=loop_id, status="blocked", action="gate_failed", eval_result=eval_payload, health=health_payload, gate=gate)
        return {"ok": False, "applied": False, "blocked_reason": "safety_wire_missing", "promotion_request_id": request_id}
    if _is_code_evolution_v2_candidate(candidate):
        return _promote_code_evolution_v2_candidate(
            runtime,
            candidate,
            scope=scope,
            loop_id=loop_id,
            apply=apply,
            eval_result=eval_result,
            health=health,
        )
    _enforce_harness_patch_v2(runtime, candidate, scope=scope)
    automation_policy = _machine_apply_policy(candidate)
    promotion_target = _promotion_target(candidate)
    eval_payload = eval_result or candidate.content.get("eval_result") or {}
    health_payload = health or {"ok": True}
    hypothesis_gate = _final_code_patch_hypothesis_gate(
        runtime,
        candidate=candidate,
        legacy_authority=legacy_authority,
    )
    if not hypothesis_gate.get("allowed"):
        final_gate = {
            "ok": False,
            "blocked_reasons": [str(hypothesis_gate.get("reason") or "nonlegacy_code_patch_hypothesis_gate_blocked")],
            "capability_hypothesis": hypothesis_gate,
            "automation_policy": automation_policy,
        }
        reason = str(final_gate["blocked_reasons"][0])
        _record_candidate_lifecycle(
            runtime,
            candidate,
            scope=scope,
            action_type="gate_failed",
            test_result=eval_payload,
            health_result=health_payload,
            reason=reason,
            details={"gate": final_gate},
        )
        request_id = _promotion_record(
            runtime,
            candidate,
            scope=scope,
            loop_id=loop_id,
            status="blocked",
            action="capability_hypothesis_blocked",
            eval_result=eval_payload,
            health=health_payload,
            gate=final_gate,
        )
        return {
            "ok": False,
            "applied": False,
            "blocked_reason": reason,
            "promotion_request_id": request_id,
            "capability_hypothesis": hypothesis_gate,
        }
    if tier in {"L2", "L3", "L4"} and _promotion_target(candidate) in CODE_ASSET_TARGETS:
        eval_payload, health_payload = _canonicalize_code_patch_evidence(
            runtime,
            candidate,
            scope=scope,
            loop_id=loop_id,
            eval_result=eval_payload,
            health=health_payload,
        )
    gate = _rollout_gate(eval_payload, health_payload, tier=tier, candidate=candidate)
    gate["automation_policy"] = automation_policy
    gate["capability_hypothesis"] = hypothesis_gate
    if not gate["ok"]:
        _record_candidate_lifecycle(runtime, candidate, scope=scope, action_type="gate_failed", test_result=eval_payload, health_result=health_payload, reason=",".join(gate["blocked_reasons"]), details={"gate": gate})
        request_id = _promotion_record(runtime, candidate, scope=scope, loop_id=loop_id, status="blocked", action="gate_failed", eval_result=eval_payload, health=health_payload, gate=gate)
        return {"ok": False, "applied": False, "blocked_reason": ",".join(gate["blocked_reasons"]), "promotion_request_id": request_id}
    if not apply:
        _record_candidate_lifecycle(runtime, candidate, scope=scope, action_type="gate_passed", test_result=eval_payload, health_result=health_payload, details={"gate": gate})
        request_id = _promotion_record(runtime, candidate, scope=scope, loop_id=loop_id, status="candidate", action="dry_run", eval_result=eval_payload, health=health_payload, gate=gate)
        return {
            "ok": True,
            "applied": False,
            "dry_run": True,
            "promotion_request_id": request_id,
            "automation_policy": automation_policy,
        }
    if promotion_target in CODE_ASSET_TARGETS and not automation_policy["allow_apply"]:
        reason = str(automation_policy["reason"])
        policy_gate = {
            **gate,
            "ok": False,
            "blocked_reasons": [reason],
            "automation_policy": automation_policy,
        }
        _record_candidate_lifecycle(
            runtime,
            candidate,
            scope=scope,
            action_type="gate_failed",
            test_result=eval_payload,
            health_result=health_payload,
            reason=reason,
            details={"gate": policy_gate},
        )
        request_id = _promotion_record(
            runtime,
            candidate,
            scope=scope,
            loop_id=loop_id,
            status="blocked",
            action="automation_policy_blocked",
            eval_result=eval_payload,
            health=health_payload,
            gate=policy_gate,
        )
        return {
            "ok": False,
            "applied": False,
            "blocked_reason": reason,
            "promotion_request_id": request_id,
            "automation_policy": automation_policy,
        }

    _record_candidate_lifecycle(runtime, candidate, scope=scope, action_type="gate_passed", test_result=eval_payload, health_result=health_payload, details={"gate": gate})
    side_effect = _apply_candidate(
        runtime,
        candidate,
        scope=scope,
        loop_id=loop_id,
        eval_result=eval_payload,
        gate=gate,
        automation_policy=automation_policy,
        legacy_authority=legacy_authority,
    )
    if not side_effect.get("ok"):
        request_id = _promotion_record(runtime, candidate, scope=scope, loop_id=loop_id, status="blocked", action="adapter_failed", eval_result=eval_payload, health=health_payload, gate=gate, side_effect=side_effect)
        return {
            "ok": False,
            "applied": False,
            "blocked_reason": str(side_effect.get("blocked_reason") or "rollout_adapter_failed"),
            "promotion_request_id": request_id,
            "side_effect": side_effect,
        }

    post_promotion_status = WATCH_STATUS if bool(side_effect.get("requires_post_promotion_watch")) else "promoted"
    if "applied" not in set(side_effect.get("lifecycle_actions") or []):
        _record_candidate_lifecycle(runtime, candidate, scope=scope, action_type="applied", test_result=eval_payload, health_result=health_payload, side_effect=side_effect)
    candidate.status = post_promotion_status
    candidate.meta["promoted_by"] = "eimemory.autonomous_learning"
    candidate.meta["promotion_tier"] = tier
    candidate.meta["applied_artifact_ids"] = list(side_effect.get("applied_artifact_ids") or [])
    runtime.store.rewrite(candidate)
    request_status = post_promotion_status
    request_action = "applied_shadow" if post_promotion_status == WATCH_STATUS else "applied"
    request_id = _promotion_record(runtime, candidate, scope=scope, loop_id=loop_id, status=request_status, action=request_action, eval_result=eval_payload, health=health_payload, gate=gate, side_effect=side_effect)
    watch = {}
    if post_promotion_status == WATCH_STATUS:
        watch = initialize_promotion_watch(
            runtime,
            candidate=candidate,
            scope=scope,
            promotion_request_id=request_id,
            applied_pattern_ids=[str(item) for item in side_effect.get("applied_artifact_ids") or []],
        )
    return {
        "ok": True,
        "applied": True,
        "authority_tier": tier,
        "candidate_id": candidate_id,
        "promotion_request_id": request_id,
        "post_promotion_status": post_promotion_status,
        "post_promotion_watch": watch,
        "side_effect": side_effect,
        "applied_artifact_ids": list(side_effect.get("applied_artifact_ids") or []),
        "automation_policy": automation_policy,
        "rollback": candidate.content.get("rollback") or "disable candidate",
    }


def backfill_promotion_rollout_ledger(
    runtime: Any,
    *,
    scope: dict[str, Any] | ScopeRef | None = None,
    limit: int = 500,
) -> dict[str, Any]:
    """Backfill rollout ledger rows for historical promotion_request records."""
    scope_ref = scope if isinstance(scope, ScopeRef) else ScopeRef.from_dict(scope)
    max_count = max(0, int(limit))
    page_size = min(100, max_count or 100)
    offset = 0
    records: list[RecordEnvelope] = []
    seen_ids: set[str] = set()
    while len(records) < max_count:
        remaining = max_count - len(records)
        page = runtime.store.list_records(
            kinds=["promotion_request"],
            scope=scope_ref,
            limit=min(page_size, remaining),
            offset=offset,
        )
        if not page:
            break
        for record in page:
            if record.record_id in seen_ids:
                continue
            records.append(record)
            seen_ids.add(record.record_id)
            if len(records) >= max_count:
                break
        if len(page) < min(page_size, remaining):
            break
        offset += len(page)

    created: list[str] = []
    existing: list[str] = []
    for record in records:
        ledger = _ensure_promotion_rollout_ledger(runtime, promotion_record=record, scope=scope_ref)
        if not ledger:
            continue
        if ledger.get("created"):
            created.append(str(ledger.get("id") or ""))
        else:
            existing.append(str(ledger.get("id") or ""))
    return {
        "ok": True,
        "scanned_count": len(records),
        "created_count": len([item for item in created if item]),
        "existing_count": len([item for item in existing if item]),
        "ledger_ids": [item for item in created if item],
        "action_type": CAPABILITY_ROLLOUT_ACTION,
    }


def _final_code_patch_hypothesis_gate(
    runtime: Any,
    *,
    candidate: RecordEnvelope,
    legacy_authority: object | None,
) -> dict[str, Any]:
    """Revalidate dynamic code authority at the final repository-write gate.

    Candidate fields are planning evidence, never authority.  The durable
    hypothesis, link applicability, revision, bounds, and independent evidence
    are reloaded from the candidate's exact persisted scope immediately before
    a promotion can proceed.
    """

    if _promotion_target(candidate) not in CODE_ASSET_TARGETS:
        return {"required": False, "allowed": True, "reason": "not_applicable"}
    patch = _candidate_patch(runtime, candidate, scope=candidate.scope)
    context = patch.get("capability_hypothesis")
    has_context = isinstance(context, dict) and bool(context)
    legacy_allowed = _has_legacy_promotion_authority(candidate, legacy_authority)
    # A real v3 context is always revalidated.  It cannot be downgraded by a
    # candidate's own legacy flag or by an internal compatibility call.
    if legacy_allowed and not has_context:
        return {
            "required": False,
            "allowed": True,
            "reason": "legacy_compatibility_explicit_internal_authority",
        }
    if not has_context:
        return {
            "required": True,
            "allowed": False,
            "reason": "nonlegacy_code_patch_hypothesis_context_missing",
        }
    required_context_fields = (
        "hypothesis_id",
        "link_id",
        "link_digest",
        "capability_id",
        "capability_revision_id",
        "provider_binding_id",
        "capability_scope",
    )
    missing_context = [field for field in required_context_fields if not str(context.get(field) or "").strip()]
    if missing_context:
        return {
            "required": True,
            "allowed": False,
            "reason": "nonlegacy_code_patch_hypothesis_context_incomplete",
            "missing_fields": missing_context,
        }
    required_patch_fields = (
        "target_capability",
        "capability_revision_id",
        "provider_binding_id",
        "profile_key",
        "capability_scope",
        "evidence_watermark",
    )
    missing_patch = [field for field in required_patch_fields if not str(patch.get(field) or "").strip()]
    if missing_patch:
        return {
            "required": True,
            "allowed": False,
            "reason": "nonlegacy_code_patch_binding_context_incomplete",
            "missing_fields": missing_patch,
        }
    for patch_field, context_field in (
        ("target_capability", "capability_id"),
        ("capability_revision_id", "capability_revision_id"),
        ("provider_binding_id", "provider_binding_id"),
        ("capability_scope", "capability_scope"),
    ):
        if str(patch.get(patch_field) or "") != str(context.get(context_field) or ""):
            return {
                "required": True,
                "allowed": False,
                "reason": f"nonlegacy_code_patch_{patch_field}_mismatch",
            }
    try:
        live_gate = dict(
            hypothesis_behavior_gate(
                runtime,
                runtime_scope=candidate.scope,
                hypothesis_id=str(context.get("hypothesis_id") or ""),
            )
        )
    except Exception as exc:
        return {
            "required": True,
            "allowed": False,
            "reason": f"nonlegacy_code_patch_hypothesis_gate_error:{type(exc).__name__}",
        }
    live_gate["required"] = True
    if not live_gate.get("allowed"):
        return live_gate
    for field in ("hypothesis_id", "link_id", "link_digest", "capability_id", "capability_revision_id", "capability_scope"):
        if str(live_gate.get(field) or "") != str(context.get(field) or ""):
            return {
                **live_gate,
                "allowed": False,
                "reason": f"nonlegacy_code_patch_hypothesis_{field}_mismatch",
            }
    for field in ("expected_metric", "candidate_bounds"):
        patch_value = patch.get(field)
        gate_value = live_gate.get(field)
        if not isinstance(patch_value, dict) or not patch_value:
            return {
                **live_gate,
                "allowed": False,
                "reason": f"nonlegacy_code_patch_{field}_missing",
            }
        if not isinstance(gate_value, dict) or not gate_value:
            return {
                **live_gate,
                "allowed": False,
                "reason": f"nonlegacy_code_patch_hypothesis_{field}_missing",
            }
        if dict(patch_value) != dict(gate_value):
            return {
                **live_gate,
                "allowed": False,
                "reason": f"nonlegacy_code_patch_{field}_mismatch",
            }
    recorded_gate = patch.get("capability_hypothesis_gate")
    recorded_feedback_id = (
        str(recorded_gate.get("qualifying_feedback_id") or "")
        if isinstance(recorded_gate, dict)
        else ""
    )
    live_feedback_id = str(live_gate.get("qualifying_feedback_id") or "")
    if not recorded_feedback_id:
        return {
            **live_gate,
            "allowed": False,
            "reason": "nonlegacy_code_patch_hypothesis_evidence_missing",
        }
    if recorded_feedback_id != live_feedback_id:
        return {
            **live_gate,
            "allowed": False,
            "reason": "candidate_hypothesis_evidence_superseded",
        }
    return live_gate


def _machine_apply_policy(candidate: RecordEnvelope, *, strict_v2: bool = False) -> dict[str, Any]:
    """Return explicit machine action authority for a candidate.

    Candidate and patch policy fields are deliberately ignored here.  The only
    authority is the deployment-controlled environment policy, read again at
    promotion time so a proposal cannot smuggle or retain a stale grant.
    """

    context = _candidate_machine_policy_context(candidate)
    if strict_v2:
        # A v2 transaction may never inherit the compatibility v1 environment
        # policy.  The file path is deployment-owned; its contents are
        # reloaded at this effect-owner boundary and the v2 effect map is the
        # only source of forward-action authority.
        policy = load_code_automation_policy(
            path=os.environ.get(CODE_AUTOMATION_POLICY_PATH_ENV) or CODE_AUTOMATION_POLICY_DEFAULT_PATH,
        )
        effects = dict(policy.get("effects") or {})
        actions = {
            MACHINE_POLICY_ACTION_LOCAL_APPLY: policy.get("ok") is True,
            MACHINE_POLICY_ACTION_COMMIT: effects.get("commit") is True and effects.get("push") is True,
            MACHINE_POLICY_ACTION_DEPLOYMENT: effects.get("deployment") is True,
        }
    else:
        policy = load_code_automation_policy(**context)
        actions = dict(policy.get("actions") or {})
    policy_ok = policy.get("ok") is True
    allow_apply = policy_ok and bool(actions.get(MACHINE_POLICY_ACTION_LOCAL_APPLY))
    allow_commit = policy_ok and bool(actions.get(MACHINE_POLICY_ACTION_COMMIT))
    allow_deployment = policy_ok and bool(actions.get(MACHINE_POLICY_ACTION_DEPLOYMENT))
    reason = str(policy.get("reason") or ("machine_policy_local_apply_enabled" if allow_apply else "machine_policy_blocked"))
    return {
        "allow_apply": allow_apply,
        "allow_commit": allow_commit,
        "allow_deployment": allow_deployment,
        "reason": reason,
        "policy_id": str(policy.get("policy_id") or ""),
        "policy_declared": bool(policy.get("declared")),
        "policy_digest": str(policy.get("policy_digest") or ""),
        "policy_source": str(policy.get("source") or ""),
        "policy_status": str(policy.get("status") or "blocked"),
        "actions": {
            MACHINE_POLICY_ACTION_LOCAL_APPLY: bool(actions.get(MACHINE_POLICY_ACTION_LOCAL_APPLY)),
            MACHINE_POLICY_ACTION_COMMIT: bool(actions.get(MACHINE_POLICY_ACTION_COMMIT)),
            MACHINE_POLICY_ACTION_DEPLOYMENT: bool(actions.get(MACHINE_POLICY_ACTION_DEPLOYMENT)),
        },
    }


def _candidate_machine_policy_context(candidate: RecordEnvelope) -> dict[str, str]:
    """Extract only match coordinates; candidate policy fields are ignored."""
    content = candidate.content if isinstance(candidate.content, dict) else {}
    meta = candidate.meta if isinstance(candidate.meta, dict) else {}
    patch = content.get("candidate_patch") if isinstance(content.get("candidate_patch"), dict) else {}
    hypothesis = patch.get("capability_hypothesis") if isinstance(patch.get("capability_hypothesis"), dict) else {}
    return machine_policy_context_from_mapping(
        {
            "profile_key": patch.get("profile_key") or content.get("profile_key") or meta.get("profile_key"),
            "target_capability": patch.get("target_capability") or content.get("target_capability") or meta.get("target_capability"),
            "capability_revision_id": patch.get("capability_revision_id") or content.get("capability_revision_id") or meta.get("capability_revision_id"),
            "capability_scope": patch.get("capability_scope") or content.get("capability_scope") or meta.get("capability_scope"),
            "provider_binding_id": patch.get("provider_binding_id") or content.get("provider_binding_id") or meta.get("provider_binding_id"),
            "capability_hypothesis": hypothesis,
        }
    )


def _rollout_gate(eval_result: dict[str, Any], health: dict[str, Any], *, tier: str, candidate: RecordEnvelope) -> dict[str, Any]:
    scores = dict(eval_result.get("scores") or {})
    blocked = []
    gate_bundle = _gate_bundle(candidate, eval_result)
    target = _promotion_target(candidate)
    gated_tiers = {"L2", "L3", "L4"}
    if tier in gated_tiers and target in CODE_ASSET_TARGETS and not _code_preflight_gate(gate_bundle):
        return {
            "ok": False,
            "blocked_reasons": [_code_preflight_blocked_reason(gate_bundle)],
            "gate_bundle": gate_bundle,
        }
    if str(eval_result.get("verdict") or "pass") != "pass":
        blocked.append("eval_not_pass")
    if _score_value(scores, "safety", default=1.0 if tier in {"L0", "L1"} else 0.0) < (0.95 if tier == "L2" else SAFETY_THRESHOLD):
        blocked.append("safety_gate")
    if _score_value(scores, "regression", default=1.0 if tier in {"L0", "L1"} else 0.0) < (0.95 if tier == "L2" else REGRESSION_THRESHOLD):
        blocked.append("regression_gate")
    if tier in gated_tiers and not health.get("ok", False):
        blocked.append("health_gate")
    if tier in gated_tiers:
        if not gate_bundle:
            blocked.append("gate_bundle_missing")
        if not _evidence_gate(gate_bundle, scores):
            blocked.append("evidence_gate")
        if not _rollback_gate(gate_bundle):
            blocked.append("rollback_gate")
        if not _canary_gate(gate_bundle):
            blocked.append("canary_gate")
        if _int_value(gate_bundle.get("timeout_seconds"), default=0) <= 0:
            blocked.append("timeout_gate")
        if not bool((gate_bundle.get("audit") or {}).get("enabled")):
            blocked.append("audit_gate")
        if target in CODE_ASSET_TARGETS and not _real_task_replay_gate(gate_bundle):
            blocked.append("real_task_replay_gate")
        if target in {"prompt_policy", "system_prompt_patch"} and not _prompt_safety_gate(gate_bundle):
            blocked.append("prompt_safety_gate")
        if not _closed_loop_gate(gate_bundle):
            blocked.append("closed_loop_gate")
    return {"ok": not blocked, "blocked_reasons": blocked, "gate_bundle": gate_bundle}


def _closed_loop_gate(gate_bundle: dict[str, Any]) -> bool:
    closed_loop = gate_bundle.get("closed_loop") or gate_bundle.get("loop_closure") or {}
    if not isinstance(closed_loop, dict):
        return False
    doctor = closed_loop.get("doctor") or {}
    smoke = closed_loop.get("smoke") or {}
    if not isinstance(doctor, dict) or not isinstance(smoke, dict):
        return False
    return bool(doctor.get("ok")) and bool(smoke.get("ok"))


def _score_value(scores: dict[str, Any], key: str, *, default: float) -> float:
    if key not in scores or scores.get(key) is None:
        return float(default)
    try:
        return float(scores.get(key))
    except (TypeError, ValueError):
        return 0.0


def _int_value(value: Any, *, default: int = 0) -> int:
    if value is None:
        return int(default)
    try:
        return int(value)
    except (TypeError, ValueError):
        return int(default)


def _float_value(value: Any, *, default: float = 0.0) -> float:
    if value is None:
        return float(default)
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)


def _is_code_evolution_v2_candidate(candidate: RecordEnvelope) -> bool:
    content = candidate.content if isinstance(candidate.content, dict) else {}
    patch = content.get("candidate_patch") if isinstance(content.get("candidate_patch"), dict) else {}
    return bool(
        content.get("code_evolution_v2") is True
        or patch.get("code_evolution_v2") is True
        or str(patch.get("schema_version") or "") == "code_implementation_proposal.v2"
        or str(content.get("proposal_schema_version") or "") == "code_implementation_proposal.v2"
    )


def _promote_code_evolution_v2_candidate(
    runtime: Any,
    candidate: RecordEnvelope,
    *,
    scope: dict[str, Any] | ScopeRef | None,
    loop_id: str,
    apply: bool,
    eval_result: dict[str, Any] | None,
    health: dict[str, Any] | None,
) -> dict[str, Any]:
    """Route v2 candidates through the durable ledger, never the legacy writer."""

    from eimemory.governance.code_evolution_transaction import (
        CodeEvolutionTransactionError,
        CodeEvolutionTransactionManager,
    )

    scope_ref = scope if isinstance(scope, ScopeRef) else ScopeRef.from_dict(scope)
    content = candidate.content if isinstance(candidate.content, dict) else {}
    patch = content.get("candidate_patch") if isinstance(content.get("candidate_patch"), dict) else {}
    automation_policy = _machine_apply_policy(candidate, strict_v2=True)
    proposal = dict(patch)
    proposal.setdefault("transaction_id", str(content.get("transaction_id") or patch.get("transaction_id") or ""))
    proposal.setdefault("incident", content.get("incident") or patch.get("incident") or {})
    proposal.setdefault("repository", content.get("repository") or patch.get("repository") or {})
    proposal.setdefault("provider", content.get("provider") or patch.get("provider") or {})
    proposal.setdefault("proposal_digest", str(content.get("proposal_digest") or patch.get("proposal_digest") or ""))
    try:
        result = CodeEvolutionTransactionManager(
            runtime,
            owner_id=f"promotion-manager:{candidate.record_id}",
        ).submit_proposal(
            proposal,
            scope=asdict(scope_ref),
            effects_enabled=bool(
                automation_policy.get("allow_apply")
                and automation_policy.get("allow_commit")
                and automation_policy.get("allow_deployment")
            ),
            apply=apply,
        )
    except (CodeEvolutionTransactionError, ValueError, TypeError) as exc:
        result = {
            "ok": False,
            "applied": False,
            "blocked_reason": f"code_evolution_transaction_rejected:{type(exc).__name__}",
            "transaction_id": str(proposal.get("transaction_id") or ""),
        }
    blocked_reason = str(result.get("blocked_reason") or "code_evolution_transaction_blocked")
    gate = {
        "ok": bool(result.get("ok")),
        "blocked_reasons": [] if result.get("ok") else [blocked_reason],
        "automation_policy": automation_policy,
        "transaction_id": str(result.get("transaction_id") or ""),
    }
    _record_candidate_lifecycle(
        runtime,
        candidate,
        scope=scope_ref,
        action_type="applied" if result.get("ok") else "gate_failed",
        test_result=eval_result or {},
        health_result=health or {"ok": True},
        reason="" if result.get("ok") else blocked_reason,
        details={"gate": gate, "code_evolution_transaction": result},
    )
    request_id = _promotion_record(
        runtime,
        candidate,
        scope=scope_ref,
        loop_id=loop_id,
        status="promoted" if result.get("ok") else "blocked",
        action="code_evolution_transaction",
        eval_result=eval_result or {},
        health=health or {"ok": True},
        gate=gate,
        side_effect=result,
    )
    return {
        "ok": bool(result.get("ok")),
        "applied": bool(result.get("applied")),
        "blocked_reason": "" if result.get("ok") else blocked_reason,
        "promotion_request_id": request_id,
        "transaction_id": str(result.get("transaction_id") or ""),
        "side_effect": result,
        "automation_policy": automation_policy,
    }


def _apply_candidate(
    runtime: Any,
    candidate: RecordEnvelope,
    *,
    scope: dict[str, Any] | ScopeRef | None,
    loop_id: str,
    eval_result: dict[str, Any],
    gate: dict[str, Any],
    automation_policy: dict[str, Any],
    legacy_authority: object | None = None,
) -> dict[str, Any]:
    target = _promotion_target(candidate)
    patch = _candidate_patch(runtime, candidate, scope=scope)
    if target in UNSUPPORTED_ACTIVE_TARGETS:
        return {"ok": False, "blocked_reason": f"unsupported_rollout_adapter:{target}", "promotion_target": target}
    if target in POLICY_TARGETS:
        return _apply_policy_candidate(runtime, candidate, patch, scope=scope, loop_id=loop_id, eval_result=eval_result, gate=gate)
    if target in CODE_ASSET_TARGETS:
        return _apply_code_patch_candidate(
            runtime,
            candidate,
            patch,
            scope=scope,
            loop_id=loop_id,
            eval_result=eval_result,
            gate=gate,
            automation_policy=automation_policy,
            legacy_authority=legacy_authority,
            require_transaction=True,
        )
    if target == "memory_rule":
        return _apply_memory_rule_candidate(runtime, candidate, patch, scope=scope)
    if target in PLAYBOOK_TARGETS or target in {"", "unknown"}:
        return _apply_playbook_candidate(runtime, candidate, patch, scope=scope, loop_id=loop_id, eval_result=eval_result, gate=gate)
    return {"ok": False, "blocked_reason": f"unsupported_rollout_adapter:{target}", "promotion_target": target}


def _apply_policy_candidate(
    runtime: Any,
    candidate: RecordEnvelope,
    patch: dict[str, Any],
    *,
    scope: dict[str, Any] | ScopeRef | None,
    loop_id: str,
    eval_result: dict[str, Any],
    gate: dict[str, Any],
) -> dict[str, Any]:
    if not hasattr(runtime, "upsert_intent_pattern"):
        return {"ok": False, "blocked_reason": "intent_pattern_adapter_unavailable"}
    pattern_id = str(patch.get("id") or f"al-{stable_semantic_key('intent_pattern', candidate.record_id)[:20]}")
    target_capability = _candidate_target_capability(candidate, patch)
    if not target_capability:
        return {
            "ok": False,
            "blocked_reason": "target_capability_unclassified",
            "promotion_target": _promotion_target(candidate),
        }
    policy_lines = _list_text(patch.get("execution_policy")) or _list_text(patch.get("policy")) or [candidate.summary]
    pattern = str(patch.get("pattern") or patch.get("user_phrase") or target_capability or candidate.summary).strip()
    payload = {
        "id": pattern_id,
        "pattern": pattern,
        "default_event_type": str(patch.get("default_event_type") or patch.get("event_type") or _event_type_for_capability(target_capability)),
        "interpreted_intent": str(patch.get("interpreted_intent") or candidate.summary or patch.get("summary") or "Apply learned execution policy."),
        "execution_policy": policy_lines,
        "first_questions": _list_text(patch.get("first_questions")),
        "ask_first_boundaries": _list_text(patch.get("ask_first_boundaries")),
        "success_criteria": str(patch.get("success_criteria") or patch.get("summary") or candidate.summary),
        "confidence": min(0.95, max(0.75, _score_value(dict(eval_result.get("scores") or {}), "confidence", default=0.8))),
        "source_opportunity_id": candidate.record_id,
        "source_opportunity": {
            "opportunity_id": candidate.record_id,
            "opportunity_type": "autonomous_learning_policy",
            "promotion_target": _promotion_target(candidate),
            "loop_id": loop_id,
        },
        "trust_report": {"ok": True, "gate": gate},
        "replay_report": {"ok": True, "eval_result": eval_result},
        "is_auto": True,
        "status": "shadow",
        "promotion_details": {
            "post_promotion_status": WATCH_STATUS,
            "required_observations": 3,
        },
    }
    result = runtime.upsert_intent_pattern(payload, scope=_scope_dict(scope or candidate.scope))
    if str(result.get("status") or "active") not in {"active", "shadow"}:
        return {
            "ok": False,
            "blocked_reason": str(result.get("_promotion_budget_decision") or "policy_not_active"),
            "promotion_target": _promotion_target(candidate),
            "applied_artifact_ids": [],
            "adapter_result": result,
        }
    return {
        "ok": True,
        "promotion_target": _promotion_target(candidate),
        "adapter": "intent_pattern",
        "applied_artifact_ids": [str(result.get("id") or pattern_id)],
        "adapter_result": result,
        "requires_post_promotion_watch": True,
    }


def _apply_memory_rule_candidate(
    runtime: Any,
    candidate: RecordEnvelope,
    patch: dict[str, Any],
    *,
    scope: dict[str, Any] | ScopeRef | None,
) -> dict[str, Any]:
    if not hasattr(runtime, "evolution") or not hasattr(runtime.evolution, "store_rule"):
        return {"ok": False, "blocked_reason": "rule_adapter_unavailable"}
    task_type = str(
        patch.get("task_type")
        or patch.get("target_capability")
        or candidate.meta.get("target_capability")
        or candidate.content.get("target_capability")
        or ""
    ).strip()
    if not task_type:
        return {
            "ok": False,
            "blocked_reason": "target_capability_unclassified",
            "promotion_target": "memory_rule",
        }
    rule = runtime.evolution.store_rule(
        title=str(patch.get("title") or candidate.title),
        summary=str(patch.get("summary") or candidate.summary),
        task_type=task_type,
        retrieval_policy=dict(patch.get("retrieval_policy") or {"learned_policy": candidate.summary}),
        response_policy=dict(patch.get("response_policy") or {}),
        scope=_scope_dict(scope or candidate.scope),
        status="active",
    )
    return {"ok": True, "promotion_target": "memory_rule", "adapter": "rule", "applied_artifact_ids": [rule.record_id]}


def run_code_patch_preflight(
    runtime: Any,
    patch: dict[str, Any],
    *,
    scope: dict[str, Any] | ScopeRef | None,
    loop_id: str,
) -> dict[str, Any]:
    """Apply and verify an exact code patch in an isolated repository.

    The returned report is persisted as a ``replay_result`` and is the only
    accepted source for autonomous code replay/canary/doctor/smoke evidence.
    """

    repo_root = _code_repo_root(patch)
    file_updates = _file_updates(patch)
    verification_commands = _normalize_commands(
        patch.get("verification_commands") or patch.get("verify_commands")
    )
    subject_commit = _current_commit_sha(repo_root, timeout_seconds=30) if repo_root else ""
    subject_state_digest = _code_patch_subject_state_digest(repo_root, subject_commit=subject_commit)
    patch_digest = _code_patch_digest(
        patch,
        repo_root=repo_root,
        subject_commit=subject_commit,
        subject_state_digest=subject_state_digest,
        file_updates=file_updates,
        verification_commands=verification_commands,
    )
    contract_error = ""
    if repo_root is None:
        contract_error = "code_patch_repo_root_missing"
    elif not repo_root.exists() or not repo_root.is_dir():
        contract_error = "code_patch_repo_root_not_found"
    elif not file_updates:
        contract_error = "code_patch_requires_file_updates"
    elif not subject_state_digest:
        contract_error = "code_patch_subject_state_unavailable"
    else:
        contract_error = _code_patch_contract_error(patch, repo_root=repo_root, file_updates=file_updates)

    timeout_seconds = max(1, _int_value(patch.get("timeout_seconds"), default=300))
    setup = {"ok": False, "mode": "", "reports": [], "error": contract_error}
    verification: dict[str, Any] = {
        "ok": False,
        "skipped": True,
        "reports": [],
        "error_type": "preflight_not_started",
    }
    cleanup = {"ok": True, "skipped": True, "reports": []}
    applied_paths: list[str] = []
    patch_error = contract_error
    temp_root: Path | None = None
    sandbox_root: Path | None = None
    sandbox_mode = ""

    if not contract_error and repo_root is not None:
        try:
            temp_root = Path(tempfile.mkdtemp(prefix="eimemory-code-preflight-"))
            sandbox_root = temp_root / "repo"
            sandbox_mode, setup = _prepare_code_preflight_sandbox(
                repo_root,
                sandbox_root,
                subject_commit=subject_commit,
                timeout_seconds=timeout_seconds,
            )
        except Exception as exc:
            setup = {
                "ok": False,
                "mode": sandbox_mode,
                "reports": [],
                "error": "code_preflight_setup_failed",
                "detail": str(exc),
            }
        if setup.get("ok"):
            applied, _backups, patch_error = _apply_file_updates(
                sandbox_root,
                file_updates,
                allowed_files=_allowed_files(patch, file_updates),
            )
            applied_paths = [str(item.get("path") or "") for item in applied]
            if not patch_error:
                verification = _run_patch_commands(
                    verification_commands,
                    cwd=sandbox_root,
                    timeout_seconds=timeout_seconds,
                    phase="verify",
                )
        try:
            cleanup = _cleanup_code_preflight_sandbox(
                repo_root,
                temp_root,
                sandbox_root,
                mode=sandbox_mode,
                timeout_seconds=timeout_seconds,
            )
        except Exception as exc:
            cleanup = {
                "ok": False,
                "skipped": False,
                "reports": [],
                "error": "code_preflight_cleanup_failed",
                "detail": str(exc),
            }
            if temp_root is not None:
                try:
                    shutil.rmtree(temp_root)
                except Exception:
                    pass

    command_reports = [dict(item) for item in verification.get("reports") or [] if isinstance(item, dict)]
    executed = bool(command_reports)
    verification_ok = bool(verification.get("ok")) and not bool(verification.get("skipped")) and executed
    ok = bool(setup.get("ok")) and not patch_error and verification_ok and bool(cleanup.get("ok"))
    pass_count = sum(1 for item in command_reports if item.get("ok") is True and item.get("returncode") == 0)
    fail_count = max(0, len(command_reports) - pass_count)
    scope_ref = scope if isinstance(scope, ScopeRef) else ScopeRef.from_dict(scope)
    record = RecordEnvelope.create(
        kind="replay_result",
        title=f"Code patch preflight: {str(patch.get('summary') or patch_digest[:12] or 'candidate')}",
        summary=f"isolated code preflight verdict={'pass' if ok else 'fail'} commands={len(command_reports)}",
        scope=scope_ref,
        source="eimemory.code_patch_preflight",
        status="pass" if ok else "fail",
    )
    evidence_ref = record.record_id
    replay = {
        "ok": ok,
        "report_type": "code_patch_verification_replay",
        "verdict": "pass" if ok else "fail",
        "pass_rate": round(pass_count / len(command_reports), 3) if command_reports else 0.0,
        "threshold": 1.0,
        "sample_count": len(command_reports),
        "pass_count": pass_count,
        "fail_count": fail_count,
        "executed": executed,
        "skipped": bool(verification.get("skipped")),
        "source": "eimemory.code_patch_preflight",
        "evidence_ref": evidence_ref,
        "subject_commit": subject_commit,
        "subject_state_digest": subject_state_digest,
        "patch_digest": patch_digest,
    }
    doctor = {
        "ok": bool(setup.get("ok")) and not patch_error,
        "executed": bool(setup.get("ok")),
        "source": "eimemory.code_patch_preflight",
        "evidence_ref": evidence_ref,
        "sandbox_mode": sandbox_mode,
    }
    smoke = {
        "ok": verification_ok,
        "executed": executed,
        "source": "eimemory.code_patch_preflight",
        "evidence_ref": evidence_ref,
        "command_count": len(command_reports),
    }
    report = {
        "ok": ok,
        "executed": executed,
        "verdict": "pass" if ok else "fail",
        "report_type": "code_patch_preflight",
        "record_id": evidence_ref,
        "loop_id": str(loop_id or ""),
        "repo_root": str(repo_root or ""),
        "subject_commit": subject_commit,
        "subject_state_digest": subject_state_digest,
        "patch_digest": patch_digest,
        "sandbox_mode": sandbox_mode,
        "setup": setup,
        "applied_paths": applied_paths,
        "patch_error": patch_error,
        "verification": verification,
        "cleanup": cleanup,
        "replay": replay,
        "doctor": doctor,
        "smoke": smoke,
    }
    record.content = report
    record.meta = {
        "report_type": "code_patch_preflight",
        "verdict": report["verdict"],
        "executed": executed,
        "subject_commit": subject_commit,
        "subject_state_digest": subject_state_digest,
        "patch_digest": patch_digest,
        "command_count": len(command_reports),
    }
    runtime.store.append(record)
    return report


def _canonicalize_code_patch_evidence(
    runtime: Any,
    candidate: RecordEnvelope,
    *,
    scope: dict[str, Any] | ScopeRef | None,
    loop_id: str,
    eval_result: dict[str, Any],
    health: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    patch = _candidate_patch(runtime, candidate, scope=scope)
    preflight = _matching_code_preflight(runtime, patch, eval_result=eval_result, scope=scope)
    if preflight is None:
        preflight = run_code_patch_preflight(runtime, patch, scope=scope, loop_id=loop_id)
    canonical_eval = dict(eval_result or {})
    canonical_eval["gate_bundle"] = _canonical_code_gate_bundle(
        _gate_bundle(candidate, canonical_eval),
        preflight,
    )
    canonical_health = {
        **dict(health or {}),
        "ok": bool(preflight.get("ok")) and bool((preflight.get("doctor") or {}).get("ok")),
        "source": "eimemory.code_patch_preflight",
        "evidence_ref": str(preflight.get("record_id") or ""),
        "subject_commit": str(preflight.get("subject_commit") or ""),
    }
    return canonical_eval, canonical_health


def _matching_code_preflight(
    runtime: Any,
    patch: dict[str, Any],
    *,
    eval_result: dict[str, Any],
    scope: dict[str, Any] | ScopeRef | None,
) -> dict[str, Any] | None:
    gate_bundle = eval_result.get("gate_bundle") if isinstance(eval_result.get("gate_bundle"), dict) else {}
    supplied = gate_bundle.get("code_preflight") if isinstance(gate_bundle.get("code_preflight"), dict) else {}
    record_id = str(supplied.get("record_id") or "").strip()
    if not record_id:
        return None
    record = runtime.store.get_by_id(record_id, scope=scope)
    if record is None or record.kind != "replay_result" or record.source != "eimemory.code_patch_preflight":
        return None
    content = dict(record.content or {})
    repo_root = _code_repo_root(patch)
    file_updates = _file_updates(patch)
    if (
        repo_root is None
        or not repo_root.exists()
        or not repo_root.is_dir()
        or not file_updates
        or _code_patch_contract_error(patch, repo_root=repo_root, file_updates=file_updates)
    ):
        return None
    subject_commit = _current_commit_sha(repo_root, timeout_seconds=30) if repo_root else ""
    subject_state_digest = _code_patch_subject_state_digest(repo_root, subject_commit=subject_commit)
    expected_digest = _code_patch_digest(
        patch,
        repo_root=repo_root,
        subject_commit=subject_commit,
        subject_state_digest=subject_state_digest,
        file_updates=file_updates,
        verification_commands=_normalize_commands(
            patch.get("verification_commands") or patch.get("verify_commands")
        ),
    )
    if str(content.get("record_id") or "") != record_id:
        return None
    if str(content.get("patch_digest") or "") != expected_digest:
        return None
    if str(content.get("subject_commit") or "") != subject_commit:
        return None
    if str(content.get("subject_state_digest") or "") != subject_state_digest:
        return None
    if not _code_preflight_report_passed(content):
        return None
    return content


def _canonical_code_gate_bundle(existing: dict[str, Any], preflight: dict[str, Any]) -> dict[str, Any]:
    evidence_ref = str(preflight.get("record_id") or "")
    result = dict(existing or {})
    result.pop("prompt_shadow_eval", None)
    result.pop("prompt_injection_check", None)
    result["evidence"] = [
        {
            "tier": "T1",
            "ref": evidence_ref,
            "summary": "Exact code patch executed in isolated preflight.",
            "executed": bool(preflight.get("executed")),
        }
    ]
    result["code_preflight"] = dict(preflight)
    result["rollback"] = {
        "available": True,
        "executable": True,
        "method": "restore_file_backups_or_revert_commit",
        "source": "eimemory.promotion_manager",
    }
    result["canary"] = {
        "passed": bool(preflight.get("ok")),
        "executed": bool(preflight.get("executed")),
        "blast_radius": "low",
        "mode": str(preflight.get("sandbox_mode") or "isolated_preflight"),
        "evidence_ref": evidence_ref,
    }
    result["closed_loop"] = {
        "doctor": dict(preflight.get("doctor") or {}),
        "smoke": dict(preflight.get("smoke") or {}),
    }
    result["real_task_replay"] = dict(preflight.get("replay") or {})
    result["timeout_seconds"] = max(1, _int_value(result.get("timeout_seconds"), default=300))
    result["audit"] = {"enabled": True, "ledger": "promotion_request", "evidence_ref": evidence_ref}
    return result


def _code_preflight_gate(gate_bundle: dict[str, Any]) -> bool:
    preflight = gate_bundle.get("code_preflight") if isinstance(gate_bundle.get("code_preflight"), dict) else {}
    if not _code_preflight_report_passed(preflight):
        return False
    evidence_ref = str(preflight.get("record_id") or "")
    if not evidence_ref:
        return False
    replay = gate_bundle.get("real_task_replay") if isinstance(gate_bundle.get("real_task_replay"), dict) else {}
    canary = gate_bundle.get("canary") if isinstance(gate_bundle.get("canary"), dict) else {}
    closed_loop = gate_bundle.get("closed_loop") if isinstance(gate_bundle.get("closed_loop"), dict) else {}
    doctor = closed_loop.get("doctor") if isinstance(closed_loop.get("doctor"), dict) else {}
    smoke = closed_loop.get("smoke") if isinstance(closed_loop.get("smoke"), dict) else {}
    if str(replay.get("report_type") or "") != "code_patch_verification_replay":
        return False
    if not bool(replay.get("ok")) or not bool(replay.get("executed")) or bool(replay.get("skipped")):
        return False
    if str(replay.get("evidence_ref") or "") != evidence_ref:
        return False
    for item in (canary, doctor, smoke):
        if not bool(item.get("executed")) or str(item.get("evidence_ref") or "") != evidence_ref:
            return False
    return bool(canary.get("passed")) and bool(doctor.get("ok")) and bool(smoke.get("ok"))


def _code_preflight_blocked_reason(gate_bundle: dict[str, Any]) -> str:
    preflight = gate_bundle.get("code_preflight") if isinstance(gate_bundle.get("code_preflight"), dict) else {}
    setup = preflight.get("setup") if isinstance(preflight.get("setup"), dict) else {}
    for value in (preflight.get("patch_error"), setup.get("error")):
        reason = str(value or "").strip()
        if reason.startswith("code_patch_"):
            return reason
    return "code_preflight_gate"


def _code_preflight_report_passed(report: dict[str, Any]) -> bool:
    if not bool(report.get("ok")) or not bool(report.get("executed")) or str(report.get("verdict") or "") != "pass":
        return False
    verification = report.get("verification") if isinstance(report.get("verification"), dict) else {}
    reports = [dict(item) for item in verification.get("reports") or [] if isinstance(item, dict)]
    if not bool(verification.get("ok")) or bool(verification.get("skipped")) or not reports:
        return False
    if not all(item.get("ok") is True and item.get("returncode") == 0 for item in reports):
        return False
    cleanup = report.get("cleanup") if isinstance(report.get("cleanup"), dict) else {}
    return bool(cleanup.get("ok"))


def _code_patch_digest(
    patch: dict[str, Any],
    *,
    repo_root: Path | None,
    subject_commit: str,
    subject_state_digest: str,
    file_updates: list[dict[str, str]],
    verification_commands: list[str | list[str]],
) -> str:
    payload = {
        "repo_root": str(repo_root.resolve()) if repo_root else "",
        "subject_commit": str(subject_commit or ""),
        "subject_state_digest": str(subject_state_digest or ""),
        "allowed_files": sorted(_declared_allowed_files(patch)),
        "file_updates": sorted(file_updates, key=lambda item: (item["path"], item["content"])),
        "verification_commands": verification_commands,
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return sha256(encoded.encode("utf-8")).hexdigest()


def _code_patch_subject_state_digest(repo_root: Path | None, *, subject_commit: str) -> str:
    if repo_root is None or not repo_root.exists() or not repo_root.is_dir():
        return ""
    if subject_commit:
        encoded = f"git:{subject_commit}".encode("utf-8")
        return sha256(encoded).hexdigest()
    ignored_parts = {".git", ".venv", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache"}
    digest = sha256()
    try:
        paths = sorted(
            (
                path
                for path in repo_root.rglob("*")
                if not any(part in ignored_parts for part in path.relative_to(repo_root).parts)
            ),
            key=lambda path: path.relative_to(repo_root).as_posix(),
        )
        for path in paths:
            relative = path.relative_to(repo_root).as_posix()
            if path.is_symlink():
                digest.update(f"link:{relative}\0{os.readlink(path)}\0".encode("utf-8"))
                continue
            if not path.is_file():
                continue
            digest.update(f"file:{relative}\0".encode("utf-8"))
            with path.open("rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(chunk)
            digest.update(b"\0")
    except Exception:
        return ""
    return digest.hexdigest()


def _code_preflight_subject_matches(gate_bundle: dict[str, Any], *, repo_root: Path) -> bool:
    preflight = gate_bundle.get("code_preflight") if isinstance(gate_bundle.get("code_preflight"), dict) else {}
    expected_commit = str(preflight.get("subject_commit") or "")
    expected_state_digest = str(preflight.get("subject_state_digest") or "")
    if not expected_state_digest:
        return False
    current_commit = _current_commit_sha(repo_root, timeout_seconds=30)
    current_state_digest = _code_patch_subject_state_digest(repo_root, subject_commit=current_commit)
    return expected_commit == current_commit and expected_state_digest == current_state_digest


def _prepare_code_preflight_sandbox(
    repo_root: Path,
    sandbox_root: Path,
    *,
    subject_commit: str,
    timeout_seconds: int,
) -> tuple[str, dict[str, Any]]:
    git_probe = subprocess.run(
        ["git", "-C", str(repo_root), "rev-parse", "--is-inside-work-tree"],
        text=True,
        capture_output=True,
        timeout=timeout_seconds,
        check=False,
    )
    if git_probe.returncode == 0 and git_probe.stdout.strip().lower() == "true":
        command = ["git", "-C", str(repo_root), "worktree", "add", "--detach", str(sandbox_root), subject_commit or "HEAD"]
        result = subprocess.run(
            command,
            text=True,
            capture_output=True,
            timeout=timeout_seconds,
            check=False,
        )
        report = {
            "ok": result.returncode == 0,
            "mode": "git_worktree",
            "reports": [
                {
                    "command": command,
                    "returncode": result.returncode,
                    "stdout": (result.stdout or "")[-4000:],
                    "stderr": (result.stderr or "")[-4000:],
                    "ok": result.returncode == 0,
                }
            ],
            "error": "" if result.returncode == 0 else "git_worktree_add_failed",
        }
        return "git_worktree", report
    try:
        shutil.copytree(
            repo_root,
            sandbox_root,
            ignore=shutil.ignore_patterns(".git", ".venv", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache"),
        )
    except Exception as exc:
        return "copy", {"ok": False, "mode": "copy", "reports": [], "error": str(exc)}
    return "copy", {"ok": True, "mode": "copy", "reports": [], "error": ""}


def _cleanup_code_preflight_sandbox(
    repo_root: Path,
    temp_root: Path | None,
    sandbox_root: Path | None,
    *,
    mode: str,
    timeout_seconds: int,
) -> dict[str, Any]:
    if temp_root is None:
        return {"ok": True, "skipped": True, "reports": []}
    reports: list[dict[str, Any]] = []
    ok = True
    if mode == "git_worktree" and sandbox_root is not None:
        command = ["git", "-C", str(repo_root), "worktree", "remove", "--force", str(sandbox_root)]
        result = subprocess.run(command, text=True, capture_output=True, timeout=timeout_seconds, check=False)
        reports.append(
            {
                "command": command,
                "returncode": result.returncode,
                "stdout": (result.stdout or "")[-4000:],
                "stderr": (result.stderr or "")[-4000:],
                "ok": result.returncode == 0,
            }
        )
        ok = result.returncode == 0
        if ok:
            subprocess.run(
                ["git", "-C", str(repo_root), "worktree", "prune"],
                text=True,
                capture_output=True,
                timeout=timeout_seconds,
                check=False,
            )
    if ok:
        try:
            shutil.rmtree(temp_root)
        except FileNotFoundError:
            pass
        except Exception as exc:
            ok = False
            reports.append({"command": ["remove_tree", str(temp_root)], "returncode": None, "stderr": str(exc), "ok": False})
    return {"ok": ok, "skipped": False, "reports": reports}


def recover_incomplete_code_apply(
    runtime: Any,
    *,
    scope: dict[str, Any] | ScopeRef | None = None,
    limit: int = 100,
    repo_root: Path | None = None,
) -> dict[str, Any]:
    """Recover interrupted direct code-apply transactions without retrying them.

    A transaction is written before the first repository write.  This entry
    point deliberately performs *only* a verified rollback (or quarantine): it
    never replays the patch, verification, deployment, or canary steps.  If a
    target path no longer has either the recorded old content or the exact
    recorded patch content, the transaction is marked ``recovery_quarantined``
    and the repository is left untouched.
    """
    transactions = _inflight_code_apply_transactions(
        runtime,
        scope=scope,
        limit=limit,
        repo_root=repo_root,
    )
    reports: list[dict[str, Any]] = []
    recovered_count = 0
    quarantined_count = 0
    for transaction in transactions:
        report = _recover_code_apply_transaction(runtime, transaction)
        reports.append(report)
        if report.get("recovered"):
            recovered_count += 1
        if report.get("recovery_quarantined"):
            quarantined_count += 1
    return {
        "ok": quarantined_count == 0,
        "skipped": False,
        "transaction_count": len(transactions),
        "recovered_count": recovered_count,
        "recovery_quarantined_count": quarantined_count,
        "retried_apply_count": 0,
        "transactions": reports,
    }


def _inflight_code_apply_transactions(
    runtime: Any,
    *,
    scope: dict[str, Any] | ScopeRef | None = None,
    limit: int = 100,
    repo_root: Path | None = None,
    include_quarantined: bool = False,
) -> list[RecordEnvelope]:
    records = runtime.store.list_records(
        kinds=["promotion_request"],
        scope=scope,
        limit=max(1, int(limit)),
    )
    expected_root = str(repo_root.resolve()) if repo_root is not None else ""
    result: list[RecordEnvelope] = []
    for record in records:
        content = record.content if isinstance(record.content, dict) else {}
        if record.source != CODE_APPLY_TRANSACTION_SOURCE:
            continue
        if str(content.get("transaction_type") or "") != "code_apply":
            continue
        statuses = {CODE_APPLY_TRANSACTION_IN_FLIGHT}
        if include_quarantined:
            statuses.add(CODE_APPLY_TRANSACTION_QUARANTINED)
        if str(record.status or "") not in statuses:
            continue
        if expected_root and str(content.get("repo_root") or "") != expected_root:
            continue
        result.append(record)
    return result


def _begin_code_apply_transaction(
    runtime: Any,
    candidate: RecordEnvelope,
    patch: dict[str, Any],
    *,
    scope: dict[str, Any] | ScopeRef | None,
    loop_id: str,
    repo_root: Path,
    file_updates: list[dict[str, str]],
    prepared: list[dict[str, str]],
    backups: list[dict[str, Any]],
    allowed_files: list[str],
    prior_commit_sha: str,
    subject_state_digest: str,
    verification_commands: list[str | list[str]],
    automation_policy: dict[str, Any],
) -> RecordEnvelope:
    scope_ref = (
        scope
        if isinstance(scope, ScopeRef)
        else (ScopeRef.from_dict(scope) if isinstance(scope, dict) else candidate.scope)
    )
    patch_digest = _code_patch_digest(
        patch,
        repo_root=repo_root,
        subject_commit=prior_commit_sha,
        subject_state_digest=subject_state_digest,
        file_updates=file_updates,
        verification_commands=verification_commands,
    )
    planned_files = [
        {
            "path": str(item["path"]),
            "new_content_sha256": sha256(_file_update_written_bytes(str(item["content"]))).hexdigest(),
        }
        for item in prepared
    ]
    serialized_backups = _serialize_code_apply_backups(repo_root, prepared=prepared, backups=backups)
    record = RecordEnvelope.create(
        kind="promotion_request",
        title=f"Code apply transaction: {candidate.title}",
        summary=f"Durable in-flight direct code apply for {candidate.record_id}",
        scope=scope_ref,
        source=CODE_APPLY_TRANSACTION_SOURCE,
        status=CODE_APPLY_TRANSACTION_IN_FLIGHT,
        content={
            "schema_version": CODE_APPLY_TRANSACTION_SCHEMA_VERSION,
            "transaction_type": "code_apply",
            "candidate_id": candidate.record_id,
            "loop_id": str(loop_id or ""),
            "repo_root": str(repo_root.resolve()),
            "patch_digest": patch_digest,
            "prior_commit_sha": str(prior_commit_sha or ""),
            "subject_state_digest": str(subject_state_digest or ""),
            "automation_policy": dict(automation_policy),
            "allowed_files": list(allowed_files),
            "planned_files": planned_files,
            "backups": serialized_backups,
            "recovery_patch": {"rollback_commands": _rollback_commands(patch)},
            "stage": "prepared",
            "stage_history": [{"stage": "prepared"}],
        },
        meta={
            "transaction_type": "code_apply",
            "candidate_id": candidate.record_id,
            "repo_root": str(repo_root.resolve()),
            "patch_digest": patch_digest,
            "automation_policy_id": str(automation_policy.get("policy_id") or ""),
            "stage": "prepared",
        },
    )
    return runtime.store.append(record)


def _serialize_code_apply_backups(
    repo_root: Path,
    *,
    prepared: list[dict[str, str]],
    backups: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    if len(prepared) != len(backups):
        raise ValueError("code_apply_backup_count_mismatch")
    serialized: list[dict[str, Any]] = []
    for item, backup in zip(prepared, backups):
        relative_path = str(item["path"])
        backup_path = backup.get("path")
        if not isinstance(backup_path, Path) or _relative_backup_path(repo_root, backup) != relative_path:
            raise ValueError("code_apply_backup_path_mismatch")
        content = bytes(backup.get("content") or b"")
        serialized.append(
            {
                "path": relative_path,
                "existed": bool(backup.get("existed")),
                "content_b64": b64encode(content).decode("ascii"),
                "content_sha256": sha256(content).hexdigest(),
            }
        )
    return serialized


def _update_code_apply_transaction(
    runtime: Any,
    transaction: RecordEnvelope,
    *,
    stage: str,
    status: str | None = None,
    reason: str = "",
    rollback: dict[str, Any] | None = None,
    commit: dict[str, Any] | None = None,
    deployment: dict[str, Any] | None = None,
    verification: dict[str, Any] | None = None,
    recovery: dict[str, Any] | None = None,
) -> RecordEnvelope:
    content = dict(transaction.content or {})
    history = [dict(item) for item in content.get("stage_history") or [] if isinstance(item, dict)]
    entry: dict[str, Any] = {"stage": str(stage)}
    if reason:
        entry["reason"] = str(reason)
    history.append(entry)
    content["stage"] = str(stage)
    content["stage_history"] = history[-32:]
    if reason:
        content["failure_reason"] = str(reason)
    if rollback is not None:
        content["rollback"] = dict(rollback)
    if commit is not None:
        content["commit"] = dict(commit)
    if deployment is not None:
        content["deployment"] = dict(deployment)
    if verification is not None:
        content["verification"] = dict(verification)
    if recovery is not None:
        content["recovery"] = dict(recovery)
    transaction.content = content
    if status is not None:
        transaction.status = str(status)
    transaction.meta["stage"] = str(stage)
    transaction.meta["transaction_status"] = str(transaction.status)
    if reason:
        transaction.meta["failure_reason"] = str(reason)
    transaction.touch()
    return runtime.store.rewrite(transaction)


def _complete_code_apply_rollback(
    runtime: Any,
    transaction: RecordEnvelope,
    *,
    rollback: dict[str, Any],
    reason: str,
    verification: dict[str, Any] | None = None,
    commit: dict[str, Any] | None = None,
    deployment: dict[str, Any] | None = None,
) -> RecordEnvelope:
    success = bool(rollback.get("ok"))
    return _update_code_apply_transaction(
        runtime,
        transaction,
        stage="rollback_completed" if success else CODE_APPLY_TRANSACTION_QUARANTINED,
        status="rolled_back" if success else CODE_APPLY_TRANSACTION_QUARANTINED,
        reason=reason,
        rollback=rollback,
        verification=verification,
        commit=commit,
        deployment=deployment,
    )


def _recover_code_apply_transaction(runtime: Any, transaction: RecordEnvelope) -> dict[str, Any]:
    content = transaction.content if isinstance(transaction.content, dict) else {}
    transaction_id = transaction.record_id
    repo_root, repo_error = _transaction_repo_root(content)
    if repo_error or repo_root is None:
        _update_code_apply_transaction(
            runtime,
            transaction,
            stage=CODE_APPLY_TRANSACTION_QUARANTINED,
            status=CODE_APPLY_TRANSACTION_QUARANTINED,
            reason=repo_error or "code_apply_repository_unavailable",
            recovery={"action": "quarantined", "retry_apply": False},
        )
        return {
            "transaction_id": transaction_id,
            "ok": False,
            "recovered": False,
            "recovery_quarantined": True,
            "reason": repo_error or "code_apply_repository_unavailable",
            "retried_apply": False,
        }

    backups, planned_files, file_error = _deserialize_code_apply_recovery_files(repo_root, content)
    if file_error:
        _update_code_apply_transaction(
            runtime,
            transaction,
            stage=CODE_APPLY_TRANSACTION_QUARANTINED,
            status=CODE_APPLY_TRANSACTION_QUARANTINED,
            reason=file_error,
            recovery={"action": "quarantined", "retry_apply": False},
        )
        return {
            "transaction_id": transaction_id,
            "ok": False,
            "recovered": False,
            "recovery_quarantined": True,
            "reason": file_error,
            "retried_apply": False,
        }

    unchanged, state_error = _code_apply_recovery_file_state(repo_root, backups=backups, planned_files=planned_files)
    if state_error:
        _update_code_apply_transaction(
            runtime,
            transaction,
            stage=CODE_APPLY_TRANSACTION_QUARANTINED,
            status=CODE_APPLY_TRANSACTION_QUARANTINED,
            reason=state_error,
            recovery={"action": "quarantined", "retry_apply": False},
        )
        return {
            "transaction_id": transaction_id,
            "ok": False,
            "recovered": False,
            "recovery_quarantined": True,
            "reason": state_error,
            "retried_apply": False,
        }

    prior_commit_sha = str(content.get("prior_commit_sha") or "")
    commit = content.get("commit") if isinstance(content.get("commit"), dict) else {}
    new_commit_sha = str(commit.get("commit_sha") or "")
    reset_repo = False
    if (repo_root / ".git").exists():
        current_commit_sha = _current_commit_sha(repo_root, timeout_seconds=30)
        if current_commit_sha == prior_commit_sha:
            reset_repo = False
        elif new_commit_sha and current_commit_sha == new_commit_sha and not _repo_has_dirty_worktree(repo_root):
            reset_repo = True
        else:
            reason = "code_apply_recovery_git_state_ambiguous"
            _update_code_apply_transaction(
                runtime,
                transaction,
                stage=CODE_APPLY_TRANSACTION_QUARANTINED,
                status=CODE_APPLY_TRANSACTION_QUARANTINED,
                reason=reason,
                recovery={
                    "action": "quarantined",
                    "retry_apply": False,
                    "current_commit_sha": current_commit_sha,
                    "prior_commit_sha": prior_commit_sha,
                    "new_commit_sha": new_commit_sha,
                },
            )
            return {
                "transaction_id": transaction_id,
                "ok": False,
                "recovered": False,
                "recovery_quarantined": True,
                "reason": reason,
                "retried_apply": False,
            }

    rollback_side_effect_stages = {
        "deployment_started",
        "deployment_completed",
        "post_deploy_health_started",
        "post_deploy_health_passed",
        "canary_started",
        "canary_completed",
    }
    if unchanged and not reset_repo and str(content.get("stage") or "") not in rollback_side_effect_stages:
        _update_code_apply_transaction(
            runtime,
            transaction,
            stage="recovered_noop",
            status="rolled_back",
            recovery={"action": "noop_already_restored", "retry_apply": False},
        )
        return {
            "transaction_id": transaction_id,
            "ok": True,
            "recovered": True,
            "recovery_quarantined": False,
            "rollback": {"ok": True, "skipped": True, "reason": "already_restored"},
            "retried_apply": False,
        }

    recovery_patch = content.get("recovery_patch") if isinstance(content.get("recovery_patch"), dict) else {}
    rollback = _rollback_code_patch(
        repo_root=repo_root,
        patch=recovery_patch,
        backups=backups,
        timeout_seconds=30,
        phase="crash_recovery",
        prior_commit_sha=prior_commit_sha,
        reset_repo=reset_repo,
    )
    completed = _complete_code_apply_rollback(
        runtime,
        transaction,
        rollback=rollback,
        reason="code_apply_crash_recovery",
    )
    return {
        "transaction_id": transaction_id,
        "ok": bool(rollback.get("ok")),
        "recovered": bool(rollback.get("ok")),
        "recovery_quarantined": completed.status == CODE_APPLY_TRANSACTION_QUARANTINED,
        "rollback": rollback,
        "retried_apply": False,
    }


def _transaction_repo_root(content: dict[str, Any]) -> tuple[Path | None, str]:
    raw = str(content.get("repo_root") or "").strip()
    if not raw:
        return None, "code_apply_recovery_repo_root_missing"
    try:
        repo_root = Path(raw).resolve()
        allowed_root = _allowed_code_repo_root().resolve()
    except OSError:
        return None, "code_apply_recovery_repo_root_invalid"
    if repo_root != allowed_root:
        return None, "code_apply_recovery_repo_root_not_allowed"
    if not repo_root.exists() or not repo_root.is_dir():
        return None, "code_apply_recovery_repo_root_not_found"
    return repo_root, ""


def _deserialize_code_apply_recovery_files(
    repo_root: Path,
    content: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, str]], str]:
    raw_backups = content.get("backups")
    raw_planned = content.get("planned_files")
    if not isinstance(raw_backups, list) or not isinstance(raw_planned, list) or not raw_backups or len(raw_backups) != len(raw_planned):
        return [], [], "code_apply_recovery_backup_invalid"
    backups: list[dict[str, Any]] = []
    planned_files: list[dict[str, str]] = []
    seen_paths: set[str] = set()
    try:
        for backup_data, planned_data in zip(raw_backups, raw_planned):
            if not isinstance(backup_data, dict) or not isinstance(planned_data, dict):
                raise ValueError("code_apply_recovery_backup_invalid")
            relative_path = _safe_repo_relative_path(str(backup_data.get("path") or ""))
            planned_path = _safe_repo_relative_path(str(planned_data.get("path") or ""))
            if relative_path != planned_path or relative_path in seen_paths:
                raise ValueError("code_apply_recovery_path_invalid")
            seen_paths.add(relative_path)
            raw_content = b64decode(str(backup_data.get("content_b64") or "").encode("ascii"), validate=True)
            if sha256(raw_content).hexdigest() != str(backup_data.get("content_sha256") or ""):
                raise ValueError("code_apply_recovery_backup_digest_invalid")
            new_digest = str(planned_data.get("new_content_sha256") or "")
            if len(new_digest) != 64:
                raise ValueError("code_apply_recovery_patch_digest_invalid")
            backups.append(
                {
                    "path": _repo_child(repo_root, relative_path),
                    "existed": bool(backup_data.get("existed")),
                    "content": raw_content,
                }
            )
            planned_files.append({"path": relative_path, "new_content_sha256": new_digest})
    except Exception as exc:
        reason = str(exc).strip()
        return [], [], reason if reason.startswith("code_apply_") else "code_apply_recovery_backup_invalid"
    return backups, planned_files, ""


def _code_apply_recovery_file_state(
    repo_root: Path,
    *,
    backups: list[dict[str, Any]],
    planned_files: list[dict[str, str]],
) -> tuple[bool, str]:
    all_original = True
    try:
        for backup, planned in zip(backups, planned_files):
            destination = backup["path"]
            if not isinstance(destination, Path) or _relative_backup_path(repo_root, backup) != planned["path"]:
                return False, "code_apply_recovery_path_invalid"
            existed = bool(backup.get("existed"))
            if not destination.exists():
                if existed:
                    return False, f"code_apply_recovery_target_missing:{planned['path']}"
                continue
            if not destination.is_file():
                return False, f"code_apply_recovery_target_not_file:{planned['path']}"
            digest = sha256(destination.read_bytes()).hexdigest()
            original_digest = sha256(bytes(backup.get("content") or b"")).hexdigest()
            if existed and digest == original_digest:
                continue
            if not existed and digest == original_digest:
                return False, f"code_apply_recovery_unexpected_target:{planned['path']}"
            if digest == planned["new_content_sha256"]:
                all_original = False
                continue
            return False, f"code_apply_recovery_target_changed:{planned['path']}"
    except Exception:
        return False, "code_apply_recovery_target_unreadable"
    return all_original, ""


def _apply_code_patch_candidate(
    runtime: Any,
    candidate: RecordEnvelope,
    patch: dict[str, Any],
    *,
    scope: dict[str, Any] | ScopeRef | None,
    loop_id: str,
    eval_result: dict[str, Any],
    gate: dict[str, Any],
    automation_policy: dict[str, Any] | None = None,
    legacy_authority: object | None = None,
    require_transaction: bool = False,
) -> dict[str, Any]:
    if require_transaction and not _has_legacy_promotion_authority(candidate, legacy_authority):
        return {
            "ok": False,
            "blocked_reason": "code_evolution_v2_transaction_required",
            "promotion_target": "code_patch",
            "repo_mutated": False,
        }
    policy_summary = (
        dict(automation_policy)
        if isinstance(automation_policy, dict)
        else _machine_apply_policy(candidate)
    )
    policy_error = _code_patch_machine_policy_error(patch, policy_summary)
    if policy_error:
        return {
            "ok": False,
            "blocked_reason": policy_error,
            "promotion_target": "code_patch",
            "automation_policy": policy_summary,
            "repo_mutated": False,
        }
    if not _truthy(patch.get("apply_to_repo"), default=True):
        return {"ok": False, "blocked_reason": "code_patch_requires_apply_to_repo", "promotion_target": "code_patch"}
    repo_root = _code_repo_root(patch)
    if repo_root is None:
        return {"ok": False, "blocked_reason": "code_patch_repo_root_missing", "promotion_target": "code_patch"}
    if not repo_root.exists() or not repo_root.is_dir():
        return {"ok": False, "blocked_reason": "code_patch_repo_root_not_found", "promotion_target": "code_patch", "repo_root": str(repo_root)}
    file_updates = _file_updates(patch)
    if not file_updates:
        return {"ok": False, "blocked_reason": "code_patch_requires_file_updates", "promotion_target": "code_patch", "repo_root": str(repo_root)}
    contract_error = _code_patch_contract_error(patch, repo_root=repo_root, file_updates=file_updates)
    if contract_error:
        return {"ok": False, "blocked_reason": contract_error, "promotion_target": "code_patch", "repo_root": str(repo_root)}
    recovery = recover_incomplete_code_apply(runtime, scope=scope, repo_root=repo_root)
    gate_bundle = gate.get("gate_bundle") if isinstance(gate.get("gate_bundle"), dict) else {}
    if not _code_preflight_subject_matches(gate_bundle, repo_root=repo_root):
        return {
            "ok": False,
            "blocked_reason": "code_patch_subject_changed",
            "promotion_target": "code_patch",
            "repo_root": str(repo_root),
            "repo_mutated": False,
            "code_apply_recovery": recovery,
        }
    unfinished_transactions = _inflight_code_apply_transactions(
        runtime,
        repo_root=repo_root,
        include_quarantined=True,
    )
    if unfinished_transactions:
        return {
            "ok": False,
            "blocked_reason": "code_patch_recovery_required",
            "promotion_target": "code_patch",
            "repo_root": str(repo_root),
            "transaction_ids": [item.record_id for item in unfinished_transactions],
            "repo_mutated": False,
            "code_apply_recovery": recovery,
        }
    allowed_files = _allowed_files(patch, file_updates)
    timeout_seconds = _int_value(patch.get("timeout_seconds"), default=_int_value(gate_bundle.get("timeout_seconds"), default=300))
    timeout_seconds = max(1, timeout_seconds)
    prior_commit_sha = _current_commit_sha(repo_root, timeout_seconds=timeout_seconds)
    subject_state_digest = _code_patch_subject_state_digest(repo_root, subject_commit=prior_commit_sha)
    verification_commands = _normalize_commands(patch.get("verification_commands") or patch.get("verify_commands"))
    prepared, backups, error = _prepare_file_updates(repo_root, file_updates, allowed_files=allowed_files)
    if error:
        return {
            "ok": False,
            "blocked_reason": error,
            "promotion_target": "code_patch",
            "repo_root": str(repo_root),
            "applied_file_paths": [],
        }
    # The final non-mutating checks have now passed.  Transition a dynamic
    # implementation binding only here, immediately before the first write;
    # policy, repository, contract, and subject-state rejections above retain
    # their active binding without any stale transition side effect.
    binding_invalidation = invalidate_dynamic_binding_before_apply(
        runtime,
        code_patch=patch,
        scope=candidate.scope,
        opportunity_id=str(
            patch.get("source_opportunity_id")
            or candidate.meta.get("experiment_id")
            or candidate.record_id
        ),
    )
    if binding_invalidation.get("required") is True and binding_invalidation.get("ok") is not True:
        return {
            "ok": False,
            "blocked_reason": str(binding_invalidation.get("reason") or "dynamic_binding_invalidation_failed"),
            "promotion_target": "code_patch",
            "repo_root": str(repo_root),
            "repo_mutated": False,
            "binding_invalidation": binding_invalidation,
        }
    try:
        transaction = _begin_code_apply_transaction(
            runtime,
            candidate,
            patch,
            scope=scope,
            loop_id=loop_id,
            repo_root=repo_root,
            file_updates=file_updates,
            prepared=prepared,
            backups=backups,
            allowed_files=allowed_files,
            prior_commit_sha=prior_commit_sha,
            subject_state_digest=subject_state_digest,
            verification_commands=verification_commands,
            automation_policy=policy_summary,
        )
        _update_code_apply_transaction(runtime, transaction, stage="write_started")
    except Exception as exc:
        compensation = restore_dynamic_binding_after_unstarted_apply(
            runtime,
            scope=candidate.scope,
            invalidation=binding_invalidation,
            reason="code_apply_transaction_start_failed_before_write",
        )
        return {
            "ok": False,
            "blocked_reason": f"code_apply_transaction_start_failed:{type(exc).__name__}",
            "promotion_target": "code_patch",
            "repo_root": str(repo_root),
            "repo_mutated": False,
            "binding_invalidation": binding_invalidation,
            "binding_compensation": compensation,
        }
    applied, error = _write_prepared_file_updates(prepared, backups)
    rollback_evidence = _rollback_evidence(repo_root=repo_root, patch=patch, applied=applied, backups=backups, prior_commit_sha=prior_commit_sha, commit={})
    if error:
        rollback = _rollback_code_patch(
            repo_root=repo_root,
            patch=patch,
            backups=backups,
            timeout_seconds=timeout_seconds,
            phase="write",
            prior_commit_sha=prior_commit_sha,
        )
        rollback_state = _rollback_state(rollback)
        _complete_code_apply_rollback(
            runtime,
            transaction,
            rollback=rollback,
            reason="code_patch_write_failed",
        )
        _record_candidate_lifecycle(
            runtime,
            candidate,
            scope=scope,
            action_type=_rollback_action_type(rollback),
            rollback_command=_rollback_command_display(patch),
            reason="code_patch_write_failed",
            side_effect={"rollback": rollback, "rollback_evidence": rollback_evidence, **rollback_state},
        )
        compensation = (
            restore_dynamic_binding_after_unstarted_apply(
                runtime,
                scope=candidate.scope,
                invalidation=binding_invalidation,
                reason="code_patch_write_failed_before_mutation",
            )
            if not applied
            else {"ok": True, "status": "not_safe_after_partial_write"}
        )
        return {
            "ok": False,
            "blocked_reason": error,
            "promotion_target": "code_patch",
            "repo_root": str(repo_root),
            "applied_file_paths": [item["path"] for item in applied],
            "rollback_evidence": rollback_evidence,
            "rollback": rollback,
            "transaction_id": transaction.record_id,
            "binding_invalidation": binding_invalidation,
            "binding_compensation": compensation,
            **rollback_state,
        }
    _update_code_apply_transaction(runtime, transaction, stage="files_written")

    _update_code_apply_transaction(runtime, transaction, stage="verification_started")
    verification = _run_patch_commands(
        verification_commands,
        cwd=repo_root,
        timeout_seconds=timeout_seconds,
        phase="verify",
    )
    if not verification["ok"]:
        rollback = _rollback_code_patch(repo_root=repo_root, patch=patch, backups=backups, timeout_seconds=timeout_seconds, phase="verify")
        rollback_state = _rollback_state(rollback)
        _complete_code_apply_rollback(
            runtime,
            transaction,
            rollback=rollback,
            reason="code_patch_verification_failed",
            verification=verification,
        )
        _record_candidate_lifecycle(runtime, candidate, scope=scope, action_type=_rollback_action_type(rollback), test_result=verification, rollback_command=_rollback_command_display(patch), reason="code_patch_verification_failed", side_effect={"rollback": rollback, "rollback_evidence": rollback_evidence, **rollback_state})
        return {
            "ok": False,
            "blocked_reason": "code_patch_verification_failed",
            "promotion_target": "code_patch",
            "repo_root": str(repo_root),
            "applied_file_paths": [item["path"] for item in applied],
            "verification": verification,
            "rollback_evidence": rollback_evidence,
            "rollback": rollback,
            "transaction_id": transaction.record_id,
            **rollback_state,
        }

    _update_code_apply_transaction(runtime, transaction, stage="verification_passed", verification=verification)
    _update_code_apply_transaction(runtime, transaction, stage="commit_started", verification=verification)
    commit = _commit_repo_patch(
        repo_root,
        applied_paths=[item["path"] for item in applied],
        patch=patch,
        candidate=candidate,
        timeout_seconds=timeout_seconds,
        automation_policy=policy_summary,
    )
    rollback_evidence = _rollback_evidence(repo_root=repo_root, patch=patch, applied=applied, backups=backups, prior_commit_sha=prior_commit_sha, commit=commit)
    if not commit["ok"]:
        rollback = _rollback_code_patch(repo_root=repo_root, patch=patch, backups=backups, timeout_seconds=timeout_seconds, phase="commit", prior_commit_sha=prior_commit_sha)
        rollback_state = _rollback_state(rollback)
        _complete_code_apply_rollback(
            runtime,
            transaction,
            rollback=rollback,
            reason="code_patch_commit_failed",
            verification=verification,
            commit=commit,
        )
        _record_candidate_lifecycle(runtime, candidate, scope=scope, action_type=_rollback_action_type(rollback), test_result=verification, rollback_command=_rollback_command_display(patch), reason="code_patch_commit_failed", side_effect={"rollback": rollback, "rollback_evidence": rollback_evidence, "commit": commit, **rollback_state})
        return {
            "ok": False,
            "blocked_reason": "code_patch_commit_failed",
            "promotion_target": "code_patch",
            "repo_root": str(repo_root),
            "applied_file_paths": [item["path"] for item in applied],
            "verification": verification,
            "commit": commit,
            "rollback_evidence": rollback_evidence,
            "rollback": rollback,
            "transaction_id": transaction.record_id,
            **rollback_state,
        }
    _update_code_apply_transaction(runtime, transaction, stage="commit_completed", verification=verification, commit=commit)
    _record_candidate_lifecycle(
        runtime,
        candidate,
        scope=scope,
        action_type="applied",
        test_result=verification,
        commit_sha=str(commit.get("commit_sha") or ""),
        rollback_command=_rollback_command_display(patch),
        side_effect={"commit": commit, "repo_root": str(repo_root), "applied_file_paths": [item["path"] for item in applied]},
    )

    deployment: dict[str, Any] = {"ok": True, "skipped": True, "reports": []}
    post_deploy_health: dict[str, Any] = {"ok": True, "skipped": True, "reports": []}
    production_applied = False
    if _truthy(patch.get("deploy_to_production"), default=False):
        _update_code_apply_transaction(runtime, transaction, stage="deployment_started", verification=verification, commit=commit)
        deploy_commands = _deployment_commands(patch, repo_root)
        if not deploy_commands:
            rollback = _rollback_code_patch(repo_root=repo_root, patch=patch, backups=backups, timeout_seconds=timeout_seconds, phase="deploy", prior_commit_sha=prior_commit_sha)
            rollback_state = _rollback_state(rollback)
            _complete_code_apply_rollback(
                runtime,
                transaction,
                rollback=rollback,
                reason="code_patch_deployment_commands_missing",
                verification=verification,
                commit=commit,
            )
            _record_candidate_lifecycle(runtime, candidate, scope=scope, action_type=_rollback_action_type(rollback), test_result=verification, commit_sha=str(commit.get("commit_sha") or ""), rollback_command=_rollback_command_display(patch), reason="code_patch_deployment_commands_missing", side_effect={"rollback": rollback, "rollback_evidence": rollback_evidence, **rollback_state})
            return {
                "ok": False,
                "blocked_reason": "code_patch_deployment_commands_missing",
                "promotion_target": "code_patch",
                "repo_root": str(repo_root),
                "applied_file_paths": [item["path"] for item in applied],
                "verification": verification,
                "commit": commit,
                "rollback_evidence": rollback_evidence,
                "rollback": rollback,
                "transaction_id": transaction.record_id,
                **rollback_state,
            }
        deployment = _run_patch_commands(deploy_commands, cwd=repo_root, timeout_seconds=timeout_seconds, phase="deploy")
        rollback_evidence = _rollback_evidence(repo_root=repo_root, patch=patch, applied=applied, backups=backups, prior_commit_sha=prior_commit_sha, commit=commit, deployment=deployment)
        if not deployment["ok"]:
            rollback = _rollback_code_patch(repo_root=repo_root, patch=patch, backups=backups, timeout_seconds=timeout_seconds, phase="deploy", prior_commit_sha=prior_commit_sha)
            rollback_state = _rollback_state(rollback)
            _complete_code_apply_rollback(
                runtime,
                transaction,
                rollback=rollback,
                reason="code_patch_deployment_failed",
                verification=verification,
                commit=commit,
                deployment=deployment,
            )
            _record_candidate_lifecycle(runtime, candidate, scope=scope, action_type=_rollback_action_type(rollback), test_result=verification, commit_sha=str(commit.get("commit_sha") or ""), release_path=str(rollback_evidence.get("release_path") or ""), rollback_command=_rollback_command_display(patch), reason="code_patch_deployment_failed", side_effect={"rollback": rollback, "deployment": deployment, "rollback_evidence": rollback_evidence, **rollback_state})
            return {
                "ok": False,
                "blocked_reason": "code_patch_deployment_failed",
                "promotion_target": "code_patch",
                "repo_root": str(repo_root),
                "applied_file_paths": [item["path"] for item in applied],
                "verification": verification,
                "commit": commit,
                "deployment": deployment,
                "rollback_evidence": rollback_evidence,
                "rollback": rollback,
                "transaction_id": transaction.record_id,
                **rollback_state,
            }
        _update_code_apply_transaction(runtime, transaction, stage="deployment_completed", verification=verification, commit=commit, deployment=deployment)
        _record_candidate_lifecycle(
            runtime,
            candidate,
            scope=scope,
            action_type="deployed",
            test_result=verification,
            commit_sha=str(commit.get("commit_sha") or ""),
            release_path=str(rollback_evidence.get("release_path") or ""),
            rollback_command=_rollback_command_display(patch),
            side_effect={"deployment": deployment, "rollback_evidence": rollback_evidence},
        )
        _update_code_apply_transaction(runtime, transaction, stage="post_deploy_health_started", verification=verification, commit=commit, deployment=deployment)
        post_deploy_health = _run_patch_commands(_post_deploy_health_commands(patch), cwd=repo_root, timeout_seconds=timeout_seconds, phase="post_deploy_health")
        if not post_deploy_health["ok"] or post_deploy_health.get("skipped"):
            rollback = _rollback_code_patch(repo_root=repo_root, patch=patch, backups=backups, timeout_seconds=timeout_seconds, phase="post_deploy_health", prior_commit_sha=prior_commit_sha)
            rollback_state = _rollback_state(rollback)
            _complete_code_apply_rollback(
                runtime,
                transaction,
                rollback=rollback,
                reason="code_patch_post_deploy_health_failed",
                verification=verification,
                commit=commit,
                deployment=deployment,
            )
            _record_candidate_lifecycle(runtime, candidate, scope=scope, action_type=_rollback_action_type(rollback), test_result=verification, health_result=post_deploy_health, commit_sha=str(commit.get("commit_sha") or ""), release_path=str(rollback_evidence.get("release_path") or ""), rollback_command=_rollback_command_display(patch), reason="code_patch_post_deploy_health_failed", side_effect={"rollback": rollback, "deployment": deployment, "post_deploy_health": post_deploy_health, "rollback_evidence": rollback_evidence, **rollback_state})
            return {
                "ok": False,
                "blocked_reason": "code_patch_post_deploy_health_failed",
                "promotion_target": "code_patch",
                "repo_root": str(repo_root),
                "applied_file_paths": [item["path"] for item in applied],
                "verification": verification,
                "commit": commit,
                "deployment": deployment,
                "post_deploy_health": post_deploy_health,
                "rollback_evidence": rollback_evidence,
                "rollback": rollback,
                "transaction_id": transaction.record_id,
                **rollback_state,
            }
        _update_code_apply_transaction(runtime, transaction, stage="post_deploy_health_passed", verification=verification, commit=commit, deployment=deployment)
        _record_candidate_lifecycle(
            runtime,
            candidate,
            scope=scope,
            action_type="health_checked",
            test_result=verification,
            health_result=post_deploy_health,
            commit_sha=str(commit.get("commit_sha") or ""),
            release_path=str(rollback_evidence.get("release_path") or ""),
            rollback_command=_rollback_command_display(patch),
            side_effect={"deployment": deployment, "post_deploy_health": post_deploy_health, "rollback_evidence": rollback_evidence},
        )
        production_applied = True

    canary: dict[str, Any] = {"ok": True, "skipped": True, "status": "not_required", "reports": []}
    if production_applied:
        _update_code_apply_transaction(
            runtime,
            transaction,
            stage="canary_started",
            verification=verification,
            commit=commit,
            deployment=deployment,
        )
        canary = _run_code_patch_canary(
            runtime,
            candidate,
            scope=scope,
            repo_root=repo_root,
            patch=patch,
            verification=verification,
            commit=commit,
            rollback_evidence=rollback_evidence,
            backups=backups,
            prior_commit_sha=prior_commit_sha,
            timeout_seconds=timeout_seconds,
        )
        if not canary["ok"]:
            rollback = dict(canary.get("rollback") or {})
            rollback_state = _rollback_state(rollback)
            _complete_code_apply_rollback(
                runtime,
                transaction,
                rollback=rollback,
                reason="code_patch_canary_failed",
                verification=verification,
                commit=commit,
                deployment=deployment,
            )
            return {
                "ok": False,
                "blocked_reason": "code_patch_canary_failed",
                "promotion_target": "code_patch",
                "repo_root": str(repo_root),
                "applied_file_paths": [item["path"] for item in applied],
                "verification": verification,
                "commit": commit,
                "deployment": deployment,
                "post_deploy_health": post_deploy_health,
                "canary": canary,
                "rollback_evidence": rollback_evidence,
                "rollback": rollback,
                "transaction_id": transaction.record_id,
                **rollback_state,
            }
        _update_code_apply_transaction(
            runtime,
            transaction,
            stage="canary_completed",
            verification=verification,
            commit=commit,
            deployment=deployment,
        )

    _update_code_apply_transaction(
        runtime,
        transaction,
        stage="completed",
        status="completed",
        verification=verification,
        commit=commit,
        deployment=deployment,
    )
    record = append_learning_record_once(
        runtime,
        kind="learning_playbook",
        title=f"Direct code patch: {candidate.title}",
        summary=f"Applied direct code patch for {candidate.record_id}",
        scope=scope or candidate.scope,
        loop_id=loop_id,
        step_name="code_patch_apply",
        semantic_key=stable_semantic_key("direct_code_patch", candidate.record_id, [item["path"] for item in applied], commit.get("commit_sha")),
        authority_tier="L2",
        status="active",
        content={
            "candidate_id": candidate.record_id,
            "repo_root": str(repo_root),
            "applied_file_paths": [item["path"] for item in applied],
            "verification": verification,
            "commit": commit,
            "deployment": deployment,
            "post_deploy_health": post_deploy_health,
            "canary": canary,
            "rollback_evidence": rollback_evidence,
            "transaction_id": transaction.record_id,
            "eval_result": eval_result,
            "gate": gate,
            "production_applied": production_applied,
            "binding_invalidation": binding_invalidation,
        },
        meta={
            "candidate_id": candidate.record_id,
            "promotion_target": "code_patch",
            "repo_root": str(repo_root),
            "production_applied": production_applied,
            "commit_sha": str(commit.get("commit_sha") or ""),
            "prior_commit_sha": str(rollback_evidence.get("prior_commit_sha") or ""),
        },
    )
    return {
        "ok": True,
        "promotion_target": "code_patch",
        "adapter": "direct_repo_patch",
        "applied_artifact_ids": [record.record_id] + [item["path"] for item in applied],
        "repo_root": str(repo_root),
        "applied_file_paths": [item["path"] for item in applied],
        "repo_mutated": True,
        "verification": verification,
        "commit": commit,
        "deployment": deployment,
        "post_deploy_health": post_deploy_health,
        "canary": canary,
        "rollback_evidence": rollback_evidence,
        "transaction_id": transaction.record_id,
        "production_applied": production_applied,
        "binding_invalidation": binding_invalidation,
        "lifecycle_actions": [
            "applied",
            *([] if deployment.get("skipped") else ["deployed"]),
            *([] if post_deploy_health.get("skipped") else ["health_checked"]),
            *([] if canary.get("skipped") else ["shadow_observed", str(canary.get("status") or "")]),
        ],
    }


def _run_code_patch_canary(
    runtime: Any,
    candidate: RecordEnvelope,
    *,
    scope: dict[str, Any] | ScopeRef | None,
    repo_root: Path,
    patch: dict[str, Any],
    verification: dict[str, Any],
    commit: dict[str, Any],
    rollback_evidence: dict[str, Any],
    backups: list[dict[str, Any]],
    prior_commit_sha: str,
    timeout_seconds: int,
) -> dict[str, Any]:
    commands = _canary_commands(patch)
    if not commands:
        return {"ok": True, "skipped": True, "status": "not_required", "reports": []}
    required_observations = _bounded_int(patch.get("canary_required_observations"), default=3, minimum=1, maximum=20)
    rollback_threshold = _bounded_float(
        patch.get("canary_failure_rollback_threshold"),
        default=0.2,
        minimum=0.0,
        maximum=1.0,
    )
    active_threshold = _bounded_float(
        patch.get("canary_failure_active_threshold"),
        default=0.05,
        minimum=0.0,
        maximum=1.0,
    )
    reports: list[dict[str, Any]] = []
    observed_count = 0
    failure_count = 0
    release_path = str(rollback_evidence.get("release_path") or "")
    commit_sha = str(commit.get("commit_sha") or "")
    for index in range(required_observations):
        observation = _run_patch_commands(
            commands,
            cwd=repo_root,
            timeout_seconds=timeout_seconds,
            phase=f"canary:{index + 1}",
        )
        observed_count += 1
        if not observation.get("ok"):
            failure_count += 1
        failure_rate = round(min(1.0, max(0.0, failure_count / observed_count)), 6)
        observation["observed_count"] = observed_count
        observation["failure_rate"] = failure_rate
        reports.append(observation)
        _record_code_patch_canary_lifecycle(
            runtime,
            candidate,
            scope=scope,
            patch=patch,
            action_type="shadow_observed",
            verification=verification,
            health_result=observation,
            commit_sha=commit_sha,
            release_path=release_path,
            observed_count=observed_count,
            failure_rate=failure_rate,
            details={"observation_index": index + 1, "canary_status": "observing"},
        )
        if failure_rate >= rollback_threshold:
            rollback = _rollback_code_patch(
                repo_root=repo_root,
                patch=patch,
                backups=backups,
                timeout_seconds=timeout_seconds,
                phase="canary",
                prior_commit_sha=prior_commit_sha,
            )
            rollback_action = _rollback_action_type(rollback)
            _record_code_patch_canary_lifecycle(
                runtime,
                candidate,
                scope=scope,
                patch=patch,
                action_type=rollback_action,
                verification=verification,
                health_result=observation,
                commit_sha=commit_sha,
                release_path=release_path,
                observed_count=observed_count,
                failure_rate=failure_rate,
                reason="code_patch_canary_failure_rate_exceeded",
                details={"canary_status": rollback_action, "rollback": rollback},
            )
            return {
                "ok": False,
                "skipped": False,
                "status": rollback_action,
                "observed_count": observed_count,
                "failure_rate": failure_rate,
                "reports": reports,
                "rollback": rollback,
            }
    final_failure_rate = round(min(1.0, max(0.0, failure_count / max(observed_count, 1))), 6)
    if final_failure_rate <= active_threshold:
        _record_code_patch_canary_lifecycle(
            runtime,
            candidate,
            scope=scope,
            patch=patch,
            action_type="promoted_active",
            verification=verification,
            health_result=reports[-1] if reports else {},
            commit_sha=commit_sha,
            release_path=release_path,
            observed_count=observed_count,
            failure_rate=final_failure_rate,
            details={"canary_status": "promoted_active"},
        )
        return {
            "ok": True,
            "skipped": False,
            "status": "promoted_active",
            "observed_count": observed_count,
            "failure_rate": final_failure_rate,
            "reports": reports,
        }
    rollback = _rollback_code_patch(
        repo_root=repo_root,
        patch=patch,
        backups=backups,
        timeout_seconds=timeout_seconds,
        phase="canary_quarantine",
        prior_commit_sha=prior_commit_sha,
    )
    _record_code_patch_canary_lifecycle(
        runtime,
        candidate,
        scope=scope,
        patch=patch,
        action_type="quarantined",
        verification=verification,
        health_result=reports[-1] if reports else {},
        commit_sha=commit_sha,
        release_path=release_path,
        observed_count=observed_count,
        failure_rate=final_failure_rate,
        reason="code_patch_canary_failure_rate_above_active_threshold",
        details={"canary_status": "quarantined", "rollback": rollback},
    )
    return {
        "ok": False,
        "skipped": False,
        "status": "quarantined",
        "observed_count": observed_count,
        "failure_rate": final_failure_rate,
        "reports": reports,
        "rollback": rollback,
    }


def _record_code_patch_canary_lifecycle(
    runtime: Any,
    candidate: RecordEnvelope,
    *,
    scope: dict[str, Any] | ScopeRef | None,
    patch: dict[str, Any],
    action_type: str,
    verification: dict[str, Any],
    health_result: dict[str, Any],
    commit_sha: str,
    release_path: str,
    observed_count: int,
    failure_rate: float,
    reason: str = "",
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    patch_id = str(patch.get("id") or patch.get("patch_id") or candidate.content.get("experiment_id") or candidate.meta.get("experiment_id") or candidate.record_id)
    lifecycle_details = dict(details or {})
    if isinstance(lifecycle_details.get("rollback"), dict):
        lifecycle_details["rollback"] = {
            **dict(lifecycle_details["rollback"]),
            "candidate_id": candidate.record_id,
        }
    return record_lifecycle_event(
        runtime,
        scope=scope or candidate.scope,
        action_type=action_type,
        candidate_id=candidate.record_id,
        patch_id=patch_id,
        commit_sha=commit_sha,
        release_path=release_path,
        test_result=verification,
        health_result=health_result,
        rollback_command=_rollback_command_display(patch),
        observed_count=observed_count,
        failure_rate=failure_rate,
        source_opportunity={
            "candidate_id": candidate.record_id,
            "candidate_title": candidate.title,
            "promotion_target": "code_patch",
            "target_capability": str(candidate.meta.get("target_capability") or candidate.content.get("target_capability") or ""),
        },
        replay_report={"canary": health_result},
        reason=reason,
        details={
            "promotion_target": "code_patch",
            "target_capability": str(candidate.meta.get("target_capability") or candidate.content.get("target_capability") or ""),
            **lifecycle_details,
        },
        applied_artifact_id=commit_sha if action_type == "promoted_active" else "",
        budget_decision="ok" if action_type in {"shadow_observed", "promoted_active"} else "blocked",
    )


def _apply_playbook_candidate(
    runtime: Any,
    candidate: RecordEnvelope,
    patch: dict[str, Any],
    *,
    scope: dict[str, Any] | ScopeRef | None,
    loop_id: str,
    eval_result: dict[str, Any],
    gate: dict[str, Any],
) -> dict[str, Any]:
    record = append_learning_record_once(
        runtime,
        kind="learning_playbook",
        title=f"Activated playbook: {candidate.title}",
        summary=str(patch.get("summary") or candidate.summary),
        scope=scope or candidate.scope,
        loop_id=loop_id,
        step_name="promotion_apply",
        semantic_key=stable_semantic_key("activated_playbook", candidate.record_id, patch),
        authority_tier=str(candidate.meta.get("authority_tier") or candidate.content.get("authority_tier") or "L0"),
        status="active",
        content={"candidate_id": candidate.record_id, "patch": patch, "eval_result": eval_result, "gate": gate},
        meta={"candidate_id": candidate.record_id, "promotion_target": _promotion_target(candidate)},
    )
    return {"ok": True, "promotion_target": _promotion_target(candidate), "adapter": "learning_playbook", "applied_artifact_ids": [record.record_id]}


def _code_repo_root(patch: dict[str, Any]) -> Path | None:
    raw = str(patch.get("repo_root") or patch.get("repository_root") or os.environ.get("EIMEMORY_AUTONOMOUS_CODE_REPO") or "").strip()
    if not raw:
        default = Path("/dev-project/eimemory")
        if default.exists():
            raw = str(default)
        else:
            raw = os.getcwd()
    try:
        return Path(raw).expanduser().resolve()
    except OSError:
        return None


def _file_updates(patch: dict[str, Any]) -> list[dict[str, str]]:
    updates = patch.get("file_updates") or patch.get("files") or []
    if not isinstance(updates, list):
        return []
    normalized: list[dict[str, str]] = []
    for item in updates:
        if not isinstance(item, dict):
            continue
        path = str(item.get("path") or item.get("file") or "").strip()
        content = item.get("content")
        if not path or content is None:
            continue
        normalized.append({"path": path, "content": str(content)})
    return normalized


def _code_patch_machine_policy_error(patch: dict[str, Any], automation_policy: dict[str, Any]) -> str:
    """Bind each requested code side effect to a positive policy action."""
    if not bool(automation_policy.get("allow_apply")):
        return str(automation_policy.get("reason") or "automation_policy_local_apply_not_enabled")
    if _truthy(patch.get("commit_to_repo"), default=False) and not bool(automation_policy.get("allow_commit")):
        return "automation_policy_commit_not_enabled"
    if _truthy(patch.get("deploy_to_production"), default=False):
        if not bool(automation_policy.get("allow_commit")):
            return "automation_policy_commit_not_enabled"
        if not bool(automation_policy.get("allow_deployment")):
            return "automation_policy_deployment_not_enabled"
    return ""


def _code_patch_contract_error(patch: dict[str, Any], *, repo_root: Path, file_updates: list[dict[str, str]]) -> str:
    if not str(patch.get("repo_root") or patch.get("repository_root") or "").strip():
        return "code_patch_repo_root_missing"
    allowed_root = _allowed_code_repo_root()
    try:
        if repo_root.resolve() != allowed_root.resolve():
            return "code_patch_repo_root_not_allowed"
    except OSError:
        return "code_patch_repo_root_not_allowed"
    for update in file_updates:
        try:
            relative_path = _safe_repo_relative_path(str(update.get("path") or ""))
        except ValueError as exc:
            return str(exc)
        if _has_symlink_component(repo_root, relative_path):
            return f"code_patch_symlink_path_not_allowed:{relative_path}"
    if not _declared_allowed_files(patch):
        return "code_patch_requires_allowed_files"
    verification_commands = _normalize_commands(
        patch.get("verification_commands") or patch.get("verify_commands")
    )
    if not verification_commands:
        return "code_patch_requires_verification_commands"
    command_error = code_patch_verification_command_error(verification_commands)
    if command_error:
        return command_error
    if _truthy(patch.get("deploy_to_production"), default=False):
        if not _truthy(patch.get("commit_to_repo"), default=False):
            return "code_patch_requires_commit_to_repo"
        if not _rollback_commands(patch):
            return "code_patch_requires_rollback_plan"
    if _repo_has_dirty_worktree(repo_root):
        return "code_patch_repo_not_clean"
    return ""


def _allowed_code_repo_root() -> Path:
    raw = os.environ.get("EIMEMORY_AUTONOMOUS_CODE_REPO", "").strip() or "/dev-project/eimemory"
    return Path(raw).expanduser().resolve()


def _allowed_files(patch: dict[str, Any], file_updates: list[dict[str, str]]) -> list[str]:
    declared = _declared_allowed_files(patch)
    return declared or [str(item["path"]) for item in file_updates]


def _declared_allowed_files(patch: dict[str, Any]) -> list[str]:
    raw = patch.get("allowed_files") or patch.get("allowlist") or []
    if isinstance(raw, str):
        items = [raw]
    elif isinstance(raw, (list, tuple, set)):
        items = [str(item) for item in raw if str(item).strip()]
    else:
        items = []
    return items


def _prepare_file_updates(
    repo_root: Path,
    file_updates: list[dict[str, str]],
    *,
    allowed_files: list[str],
) -> tuple[list[dict[str, str]], list[dict[str, Any]], str]:
    """Read and validate every target before any filesystem mutation."""
    prepared: list[dict[str, str]] = []
    backups: list[dict[str, Any]] = []
    seen_paths: set[str] = set()
    try:
        for update in file_updates:
            relative_path = _safe_repo_relative_path(update["path"])
            if relative_path in seen_paths:
                raise ValueError(f"code_patch_duplicate_file_update:{relative_path}")
            seen_paths.add(relative_path)
            if not _path_allowed(relative_path, allowed_files):
                raise ValueError(f"code_patch_path_not_allowed:{relative_path}")
            if _has_symlink_component(repo_root, relative_path):
                raise ValueError(f"code_patch_symlink_path_not_allowed:{relative_path}")
            destination = _repo_child(repo_root, relative_path)
            backups.append(
                {
                    "path": destination,
                    "existed": destination.exists(),
                    "content": destination.read_bytes() if destination.exists() else b"",
                }
            )
            prepared.append(
                {
                    "path": relative_path,
                    "repo_root": str(repo_root),
                    "absolute_path": str(destination),
                    "content": str(update["content"]),
                }
            )
        return prepared, backups, ""
    except Exception as exc:
        return [], [], str(exc)


def _write_prepared_file_updates(
    prepared: list[dict[str, str]],
    backups: list[dict[str, Any]],
) -> tuple[list[dict[str, str]], str]:
    """Apply prevalidated updates, restoring the captured bytes on failure."""
    applied: list[dict[str, str]] = []
    try:
        for update in prepared:
            repo_root = Path(str(update["repo_root"]))
            relative_path = str(update["path"])
            if _has_symlink_component(repo_root, relative_path):
                raise ValueError(f"code_patch_symlink_path_not_allowed:{relative_path}")
            destination = _repo_child(repo_root, relative_path)
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(_file_update_written_bytes(update["content"]))
            applied.append({"path": str(update["path"]), "absolute_path": str(destination)})
        return applied, ""
    except Exception as exc:
        _restore_file_updates(backups)
        return applied, str(exc)


def _apply_file_updates(
    repo_root: Path,
    file_updates: list[dict[str, str]],
    *,
    allowed_files: list[str],
) -> tuple[list[dict[str, str]], list[dict[str, Any]], str]:
    """Compatibility wrapper used by isolated preflight execution."""
    prepared, backups, error = _prepare_file_updates(repo_root, file_updates, allowed_files=allowed_files)
    if error:
        return [], backups, error
    applied, error = _write_prepared_file_updates(prepared, backups)
    return applied, backups, error


def _file_update_written_bytes(content: str) -> bytes:
    """Match the platform newline behavior of the prior ``write_text`` path."""
    return str(content).replace("\n", os.linesep).encode("utf-8")


def _restore_file_updates(backups: list[dict[str, Any]]) -> None:
    for backup in reversed(backups):
        path = backup["path"]
        if bool(backup.get("existed")):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(bytes(backup.get("content") or b""))
        elif path.exists():
            path.unlink()


def _safe_repo_relative_path(value: str) -> str:
    raw = str(value or "").strip().replace("\\", "/")
    if not raw:
        raise ValueError("empty_code_patch_path")
    path = PurePosixPath(raw)
    first = path.parts[0] if path.parts else ""
    if path.is_absolute() or raw.startswith("//") or ":" in first:
        raise ValueError(f"absolute_code_patch_path:{raw}")
    if any(part in {"", ".", ".."} for part in path.parts):
        raise ValueError(f"unsafe_code_patch_path:{raw}")
    return "/".join(path.parts)


def _repo_child(repo_root: Path, relative_path: str) -> Path:
    root = repo_root.resolve()
    child = (root / Path(*PurePosixPath(relative_path).parts)).resolve()
    try:
        child.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"code_patch_path_escapes_repo:{relative_path}") from exc
    return child


def _has_symlink_component(repo_root: Path, relative_path: str) -> bool:
    """Reject links/reparse points before a relative update is resolved."""
    current = repo_root.resolve()
    for part in PurePosixPath(relative_path).parts:
        current = current / part
        try:
            entry = current.lstat()
        except FileNotFoundError:
            continue
        except OSError:
            return True
        reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
        attributes = getattr(entry, "st_file_attributes", 0)
        if stat.S_ISLNK(entry.st_mode) or bool(reparse_flag and attributes & reparse_flag):
            return True
    return False


def _repo_has_dirty_worktree(repo_root: Path) -> bool:
    if not (repo_root / ".git").exists():
        return False
    try:
        result = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=str(repo_root),
            text=True,
            capture_output=True,
            timeout=30,
            check=False,
        )
    except Exception:
        return True
    return result.returncode != 0 or bool(result.stdout.strip())


def _path_allowed(relative_path: str, allowed_files: list[str]) -> bool:
    normalized_allowed = [_safe_repo_relative_path(item) for item in allowed_files if str(item).strip()]
    return any(relative_path == item or fnmatch.fnmatch(relative_path, item) for item in normalized_allowed)


def _rollback_state(rollback: dict[str, Any]) -> dict[str, bool]:
    rolled_back = bool(rollback.get("ok"))
    return {"rolled_back": rolled_back, "rollback_failed": not rolled_back}


def _rollback_action_type(rollback: dict[str, Any]) -> str:
    return "rolled_back" if bool(rollback.get("ok")) else "rollback_failed"


def _rollback_code_patch(
    *,
    repo_root: Path,
    patch: dict[str, Any],
    backups: list[dict[str, Any]],
    timeout_seconds: int,
    phase: str,
    prior_commit_sha: str = "",
    reset_repo: bool = True,
) -> dict[str, Any]:
    _restore_file_updates(backups)
    repo_reset = (
        _reset_repo_to_commit(repo_root, prior_commit_sha=prior_commit_sha, timeout_seconds=timeout_seconds)
        if reset_repo
        else {"ok": True, "skipped": True, "reason": "recovery_head_matches_prior"}
    )
    commands = _rollback_commands(patch)
    command_report = _run_patch_commands(commands, cwd=repo_root, timeout_seconds=timeout_seconds, phase=f"rollback:{phase}") if commands else {"ok": True, "skipped": True, "reports": []}
    return {
        "ok": bool(repo_reset.get("ok")) and bool(command_report.get("ok")),
        "execution_type": "code_patch_rollback",
        "phase": str(phase),
        "file_restore": {"ok": True, "restored_count": len(backups)},
        "repo_reset": repo_reset,
        "command": _rollback_command_display(patch),
        "command_report": command_report,
    }


def _reset_repo_to_commit(repo_root: Path, *, prior_commit_sha: str, timeout_seconds: int) -> dict[str, Any]:
    sha = str(prior_commit_sha or "").strip()
    if not sha:
        return {"ok": True, "skipped": True, "reason": "prior_commit_missing"}
    if not (repo_root / ".git").exists():
        return {"ok": True, "skipped": True, "reason": "git_repo_missing"}
    verify = subprocess.run(
        ["git", "rev-parse", "--verify", f"{sha}^{{commit}}"],
        cwd=str(repo_root),
        text=True,
        capture_output=True,
        timeout=timeout_seconds,
        check=False,
    )
    if verify.returncode != 0:
        return {
            "ok": False,
            "skipped": False,
            "reason": "prior_commit_not_found",
            "stderr": (verify.stderr or "")[-4000:],
        }
    reset = _run_patch_commands(
        [["git", "reset", "--hard", sha]],
        cwd=repo_root,
        timeout_seconds=timeout_seconds,
        phase="rollback:git_reset",
    )
    clean = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=str(repo_root),
        text=True,
        capture_output=True,
        timeout=timeout_seconds,
        check=False,
    )
    return {
        "ok": bool(reset.get("ok")) and clean.returncode == 0 and not clean.stdout.strip(),
        "skipped": False,
        "prior_commit_sha": sha,
        "reports": list(reset.get("reports") or []),
        "dirty_after_reset": clean.stdout.strip(),
        "status_stderr": (clean.stderr or "")[-4000:],
    }


def _rollback_commands(patch: dict[str, Any]) -> list[str | list[str]]:
    plan = patch.get("rollback_plan") if isinstance(patch.get("rollback_plan"), dict) else {}
    return _normalize_commands(plan.get("commands") or patch.get("rollback_commands") or patch.get("rollback_command"))


def _rollback_command_display(patch: dict[str, Any]) -> str:
    commands = _rollback_commands(patch)
    if not commands:
        return ""
    return " && ".join(command if isinstance(command, str) else " ".join(command) for command in commands)


def _run_patch_commands(commands: Any, *, cwd: Path, timeout_seconds: int, phase: str) -> dict[str, Any]:
    normalized = _normalize_commands(commands)
    if not normalized and str(phase or "").startswith("verify"):
        return {
            "ok": False,
            "reports": [],
            "skipped": True,
            "error_type": "missing_required_commands",
        }
    reports: list[dict[str, Any]] = []
    for command in normalized:
        if isinstance(command, str):
            report = {
                "phase": phase,
                "command": command,
                "returncode": None,
                "stdout": "",
                "stderr": "shell string commands are not supported; provide argv JSON/list commands",
                "ok": False,
                "error_type": "unsupported_shell_command",
            }
            reports.append(report)
            return {"ok": False, "reports": reports}
        run_command = _resolve_patch_command(command)
        display = [str(part) for part in run_command]
        try:
            completed = _run_patch_subprocess(
                run_command,
                cwd=cwd,
                timeout_seconds=timeout_seconds,
                phase=phase,
            )
            report = {
                "phase": phase,
                "command": display,
                "returncode": completed.returncode,
                "stdout": (completed.stdout or "")[-4000:],
                "stderr": (completed.stderr or "")[-4000:],
                "ok": completed.returncode == 0,
            }
        except subprocess.TimeoutExpired as exc:
            report = {
                "phase": phase,
                "command": display,
                "returncode": None,
                "stdout": str(exc.stdout or "")[-4000:],
                "stderr": str(exc.stderr or "")[-4000:],
                "ok": False,
                "timeout": True,
            }
        except Exception as exc:
            report = {
                "phase": phase,
                "command": display,
                "returncode": None,
                "stdout": "",
                "stderr": str(exc),
                "ok": False,
                "error_type": type(exc).__name__,
            }
        reports.append(report)
        if not report["ok"]:
            return {"ok": False, "reports": reports}
    return {"ok": True, "reports": reports, "skipped": not bool(normalized)}


def _run_patch_subprocess(
    command: list[str],
    *,
    cwd: Path,
    timeout_seconds: int,
    phase: str,
) -> subprocess.CompletedProcess[str]:
    """Run a patch command while keeping verification caches out of the repo."""
    if not str(phase or "").startswith("verify"):
        return subprocess.run(
            command,
            cwd=str(cwd),
            text=True,
            capture_output=True,
            timeout=timeout_seconds,
            shell=False,
            check=False,
        )
    with tempfile.TemporaryDirectory(prefix="eimemory-code-verify-") as cache_root:
        environment = dict(os.environ)
        environment["PYTHONPYCACHEPREFIX"] = cache_root
        existing_pytest_opts = str(environment.get("PYTEST_ADDOPTS") or "").strip()
        if "no:cacheprovider" not in existing_pytest_opts:
            environment["PYTEST_ADDOPTS"] = " ".join(
                item for item in (existing_pytest_opts, "-p no:cacheprovider") if item
            )
        return subprocess.run(
            command,
            cwd=str(cwd),
            text=True,
            capture_output=True,
            timeout=timeout_seconds,
            shell=False,
            check=False,
            env=environment,
        )


def _resolve_patch_command(command: list[str]) -> list[str]:
    if not command:
        return command
    executable = str(command[0] or "")
    lower = executable.lower()
    if lower in {"python", "python.exe", "python3", "python3.exe"}:
        return [sys.executable, *[str(part) for part in command[1:]]]
    return [str(part) for part in command]


def _normalize_commands(commands: Any) -> list[str | list[str]]:
    if commands is None:
        return []
    if isinstance(commands, str):
        return [commands] if commands.strip() else []
    if not isinstance(commands, list):
        return []
    normalized: list[str | list[str]] = []
    for item in commands:
        if isinstance(item, str):
            if item.strip():
                normalized.append(item)
        elif isinstance(item, (list, tuple)) and item:
            normalized.append([str(part) for part in item])
    return normalized


def _normalize_env_commands(name: str) -> list[list[str]]:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return []
    if _is_argv_command(parsed):
        return [_coerce_argv_command(parsed)]
    if not isinstance(parsed, list):
        return []
    return [_coerce_argv_command(item) for item in parsed if _is_argv_command(item)]


def _is_argv_command(value: Any) -> bool:
    return isinstance(value, (list, tuple)) and bool(value) and all(
        not isinstance(part, (dict, list, tuple)) for part in value
    )


def _coerce_argv_command(value: Any) -> list[str]:
    return [str(part) for part in value]


def _commit_repo_patch(
    repo_root: Path,
    *,
    applied_paths: list[str],
    patch: dict[str, Any],
    candidate: RecordEnvelope,
    timeout_seconds: int,
    automation_policy: dict[str, Any],
) -> dict[str, Any]:
    if not _truthy(patch.get("commit_to_repo"), default=False):
        return {"ok": True, "skipped": True, "reason": "commit_disabled"}
    if not bool(automation_policy.get("allow_commit")):
        return {"ok": False, "reason": "automation_policy_commit_not_enabled"}
    if not (repo_root / ".git").exists():
        return {"ok": False, "reason": "git_repo_missing"}
    add_result = _run_patch_commands([["git", "add", "--", *applied_paths]], cwd=repo_root, timeout_seconds=timeout_seconds, phase="commit")
    if not add_result["ok"]:
        return {"ok": False, "reason": "git_add_failed", "reports": add_result["reports"]}
    diff_result = subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=str(repo_root), text=True, capture_output=True, timeout=timeout_seconds, check=False)
    if diff_result.returncode == 0:
        return {"ok": True, "skipped": True, "reason": "no_staged_changes", "reports": add_result["reports"]}
    message = str(patch.get("commit_message") or f"autonomous: apply code patch {candidate.record_id[:12]}")
    commit_result = _run_patch_commands([["git", "commit", "-m", message]], cwd=repo_root, timeout_seconds=timeout_seconds, phase="commit")
    if not commit_result["ok"]:
        return {"ok": False, "reason": "git_commit_failed", "reports": add_result["reports"] + commit_result["reports"]}
    sha_result = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(repo_root), text=True, capture_output=True, timeout=timeout_seconds, check=False)
    return {
        "ok": sha_result.returncode == 0,
        "commit_sha": sha_result.stdout.strip() if sha_result.returncode == 0 else "",
        "reports": add_result["reports"] + commit_result["reports"],
    }


def _deployment_commands(patch: dict[str, Any], _repo_root: Path) -> list[str | list[str]]:
    explicit = _normalize_commands(patch.get("deployment_commands") or patch.get("deploy_commands"))
    if explicit:
        return explicit
    env_commands = _normalize_env_commands("EIMEMORY_AUTONOMOUS_CODE_DEPLOY_COMMAND")
    if env_commands:
        return env_commands
    return []


def _post_deploy_health_commands(patch: dict[str, Any]) -> list[str | list[str]]:
    explicit = _normalize_commands(
        patch.get("post_deploy_health_commands")
        or patch.get("health_commands")
        or patch.get("smoke_commands")
    )
    if explicit:
        return explicit
    env_commands = _normalize_env_commands("EIMEMORY_AUTONOMOUS_CODE_HEALTH_COMMAND")
    if env_commands:
        return env_commands
    return [["curl", "-fsS", "http://127.0.0.1:8091/health"]]


def _canary_commands(patch: dict[str, Any]) -> list[str | list[str]]:
    explicit = _normalize_commands(patch.get("canary_commands") or patch.get("shadow_observe_commands"))
    if explicit:
        return explicit
    env_commands = _normalize_env_commands("EIMEMORY_AUTONOMOUS_CODE_CANARY_COMMAND")
    if env_commands:
        return env_commands
    return _post_deploy_health_commands(patch)


def _bounded_int(value: Any, *, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = int(default)
    return max(int(minimum), min(int(maximum), parsed))


def _bounded_float(value: Any, *, default: float, minimum: float, maximum: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        parsed = float(default)
    return max(float(minimum), min(float(maximum), parsed))


def _current_commit_sha(repo_root: Path, *, timeout_seconds: int) -> str:
    if not (repo_root / ".git").exists():
        return ""
    try:
        result = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(repo_root), text=True, capture_output=True, timeout=timeout_seconds, check=False)
    except Exception:
        return ""
    return result.stdout.strip() if result.returncode == 0 else ""


def _rollback_evidence(
    *,
    repo_root: Path,
    patch: dict[str, Any],
    applied: list[dict[str, str]],
    backups: list[dict[str, Any]],
    prior_commit_sha: str,
    commit: dict[str, Any],
    deployment: dict[str, Any] | None = None,
) -> dict[str, Any]:
    deployment = deployment or {}
    return {
        "repo_root": str(repo_root),
        "service_name": str(patch.get("service_name") or os.environ.get("EIMEMORY_AUTONOMOUS_CODE_SERVICE") or "eimemory-rpc.service"),
        "prior_commit_sha": prior_commit_sha,
        "new_commit_sha": str(commit.get("commit_sha") or ""),
        "release_path": str(patch.get("release_path") or _release_path_from_deployment(deployment) or ""),
        "rollback_method": "restore_file_backups_or_revert_commit_and_restart_service",
        "rollback_command": _rollback_command_display(patch),
        "file_backups": [
            {
                "path": str(item.get("path") or _relative_backup_path(repo_root, backup)),
                "existed": bool(backup.get("existed")),
            }
            for item, backup in zip(applied, backups)
        ],
    }


def _relative_backup_path(repo_root: Path, backup: dict[str, Any]) -> str:
    path = backup.get("path")
    if not isinstance(path, Path):
        return ""
    try:
        return path.resolve().relative_to(repo_root.resolve()).as_posix()
    except ValueError:
        return str(path)


def _release_path_from_deployment(deployment: dict[str, Any]) -> str:
    for report in deployment.get("reports") or []:
        if not isinstance(report, dict):
            continue
        for stream in (str(report.get("stdout") or ""), str(report.get("stderr") or "")):
            for line in stream.splitlines():
                if line.startswith("release="):
                    return line.split("=", 1)[1].strip()
    return ""


def _truthy(value: Any, *, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on", "y", "apply", "enabled"}


def _candidate_patch(runtime: Any, candidate: RecordEnvelope, *, scope: dict[str, Any] | ScopeRef | None) -> dict[str, Any]:
    content = candidate.content if isinstance(candidate.content, dict) else {}
    direct = content.get("candidate_patch") if isinstance(content.get("candidate_patch"), dict) else {}
    if direct:
        return dict(direct)
    experiment_id = str(content.get("experiment_id") or candidate.meta.get("experiment_id") or "")
    if experiment_id:
        experiment = runtime.store.get_by_id(experiment_id, scope=scope or candidate.scope)
        if experiment is not None and isinstance(experiment.content, dict):
            patch = experiment.content.get("candidate_patch")
            if isinstance(patch, dict):
                return dict(patch)
    return {
        "summary": str(content.get("summary") or candidate.summary),
        "target_capability": str(content.get("target_capability") or candidate.meta.get("target_capability") or ""),
        "policy": str(content.get("summary") or candidate.summary),
    }


def _candidate_target_capability(candidate: RecordEnvelope, patch: dict[str, Any]) -> str:
    content = candidate.content if isinstance(candidate.content, dict) else {}
    meta = candidate.meta if isinstance(candidate.meta, dict) else {}
    for value in (
        patch.get("target_capability"),
        content.get("target_capability"),
        meta.get("target_capability"),
    ):
        text = str(value or "").strip()
        if text:
            return text
    return ""


def _gate_bundle(candidate: RecordEnvelope, eval_result: dict[str, Any]) -> dict[str, Any]:
    for value in (
        eval_result.get("gate_bundle"),
        candidate.content.get("gate_bundle") if isinstance(candidate.content, dict) else None,
        (candidate.content.get("eval_result") or {}).get("gate_bundle") if isinstance(candidate.content, dict) and isinstance(candidate.content.get("eval_result"), dict) else None,
    ):
        if isinstance(value, dict):
            return dict(value)
    return {}


def _evidence_gate(gate_bundle: dict[str, Any], scores: dict[str, Any]) -> bool:
    evidence = gate_bundle.get("evidence")
    tiers = [str(item.get("tier") or "").upper() for item in evidence if isinstance(item, dict)] if isinstance(evidence, list) else []
    if any(tier in {"T0", "T1"} for tier in tiers):
        return True
    if sum(1 for tier in tiers if tier in {"T2", "T3"}) >= 2:
        return True
    return _score_value(scores, "evidence", default=0.0) >= 0.9


def _rollback_gate(gate_bundle: dict[str, Any]) -> bool:
    rollback = gate_bundle.get("rollback") if isinstance(gate_bundle.get("rollback"), dict) else {}
    return bool(rollback.get("executable") or rollback.get("available"))


def _canary_gate(gate_bundle: dict[str, Any]) -> bool:
    canary = gate_bundle.get("canary") if isinstance(gate_bundle.get("canary"), dict) else {}
    blast_radius = str(canary.get("blast_radius") or "").lower()
    return bool(canary.get("passed")) and blast_radius in {"single_scope", "single_workspace", "service_local", "low"}


def _prompt_safety_gate(gate_bundle: dict[str, Any]) -> bool:
    shadow = gate_bundle.get("prompt_shadow_eval") if isinstance(gate_bundle.get("prompt_shadow_eval"), dict) else {}
    injection = gate_bundle.get("prompt_injection_check") if isinstance(gate_bundle.get("prompt_injection_check"), dict) else {}
    if bool(shadow.get("notready")) or bool(injection.get("notready")):
        return False
    return bool(shadow.get("passed")) and bool(injection.get("passed"))


def _real_task_replay_gate(gate_bundle: dict[str, Any]) -> bool:
    report = gate_bundle.get("real_task_replay") or gate_bundle.get("replay_report") or gate_bundle.get("replay")
    if not isinstance(report, dict):
        return False
    if not bool(report.get("ok")):
        return False
    verdict = str(report.get("verdict") or "").strip().lower()
    sample_count = _int_value(report.get("sample_count") or report.get("case_count") or report.get("pass_count"), default=0)
    if verdict != "pass" or sample_count <= 0:
        return False
    pass_rate = _float_value(report.get("pass_rate"), default=0.0)
    threshold = _float_value(report.get("threshold"), default=0.6)
    return pass_rate >= threshold


def _promotion_target(candidate: RecordEnvelope) -> str:
    return str(candidate.meta.get("promotion_target") or candidate.content.get("promotion_target") or "").strip().lower()


def _scope_dict(scope: dict[str, Any] | ScopeRef | None) -> dict[str, Any]:
    if isinstance(scope, ScopeRef):
        return asdict(scope)
    return dict(scope or {})


def _event_type_for_capability(capability: str) -> str:
    value = capability.lower()
    if "routing" in value or "tool" in value:
        return "tool_routing"
    if "recall" in value or "memory" in value:
        return "memory_recall"
    if "code" in value:
        return "code_implementation"
    return "communication"


def _list_text(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value] if value.strip() else []
    if isinstance(value, (list, tuple, set)):
        return [str(item) for item in value if str(item).strip()]
    return []


def _record_candidate_lifecycle(
    runtime: Any,
    candidate: RecordEnvelope,
    *,
    scope: dict[str, Any] | ScopeRef | None,
    action_type: str,
    test_result: dict[str, Any] | None = None,
    health_result: dict[str, Any] | None = None,
    commit_sha: str = "",
    release_path: str = "",
    rollback_command: str = "",
    reason: str = "",
    side_effect: dict[str, Any] | None = None,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    patch = _candidate_patch(runtime, candidate, scope=scope)
    patch_id = str(patch.get("id") or patch.get("patch_id") or candidate.content.get("experiment_id") or candidate.meta.get("experiment_id") or candidate.record_id)
    side = dict(side_effect or {})
    if isinstance(side.get("rollback"), dict):
        side["rollback"] = {
            **dict(side["rollback"]),
            "candidate_id": candidate.record_id,
        }
    release = str(release_path or (side.get("rollback_evidence") or {}).get("release_path") or "")
    commit = str(commit_sha or (side.get("commit") or {}).get("commit_sha") or "")
    rollback = str(rollback_command or (side.get("rollback") or {}).get("command") or _rollback_command_display(patch))
    return record_lifecycle_event(
        runtime,
        scope=scope or candidate.scope,
        action_type=action_type,
        candidate_id=candidate.record_id,
        patch_id=patch_id,
        commit_sha=commit,
        release_path=release,
        test_result=test_result or side.get("verification") or {},
        health_result=health_result or side.get("post_deploy_health") or {},
        rollback_command=rollback,
        source_opportunity={
            "candidate_id": candidate.record_id,
            "candidate_title": candidate.title,
            "promotion_target": _promotion_target(candidate),
            "target_capability": str(candidate.meta.get("target_capability") or candidate.content.get("target_capability") or ""),
        },
        trust_report=dict(details or {}),
        replay_report=test_result or {},
        reason=reason,
        details={
            "promotion_target": _promotion_target(candidate),
            "target_capability": str(candidate.meta.get("target_capability") or candidate.content.get("target_capability") or ""),
            "side_effect": side,
            **dict(details or {}),
        },
        applied_artifact_id=str((side.get("applied_artifact_ids") or [""])[0] if isinstance(side.get("applied_artifact_ids"), list) and side.get("applied_artifact_ids") else ""),
        budget_decision="blocked" if action_type in {"gate_failed", "rolled_back", "rollback_failed", "quarantined"} else "ok",
    )


def _promotion_record(
    runtime: Any,
    candidate: RecordEnvelope,
    *,
    scope: dict[str, Any] | ScopeRef | None,
    loop_id: str,
    status: str,
    action: str,
    eval_result: dict[str, Any],
    health: dict[str, Any],
    gate: dict[str, Any] | None = None,
    side_effect: dict[str, Any] | None = None,
) -> str:
    semantic_key = stable_semantic_key("promotion", candidate.record_id, action, status)
    promotion_target = _promotion_target(candidate)
    target_capability = str(candidate.meta.get("target_capability") or candidate.content.get("target_capability") or "")
    record = append_learning_record_once(
        runtime,
        kind="promotion_request",
        title=f"Promotion {action}: {candidate.title}",
        summary=candidate.summary,
        scope=scope or candidate.scope,
        loop_id=loop_id,
        step_name="promotion",
        semantic_key=semantic_key,
        authority_tier=str(candidate.meta.get("authority_tier") or candidate.content.get("authority_tier") or "L0"),
        status=status,
        content={
            "candidate_id": candidate.record_id,
            "promotion_target": promotion_target,
            "target_capability": target_capability,
            "action": action,
            "eval_result": eval_result,
            "health": health,
            "gate": gate or {},
            "side_effect": side_effect or {},
            "rollback": candidate.content.get("rollback") or "disable candidate",
        },
        meta={
            "candidate_id": candidate.record_id,
            "promotion_target": promotion_target,
            "target_capability": target_capability,
            "action": action,
            "gate_ok": bool((gate or {"ok": status == "promoted"}).get("ok")),
            "side_effect_ok": bool((side_effect or {}).get("ok")),
        },
    )
    _ensure_promotion_rollout_ledger(runtime, promotion_record=record, candidate=candidate, scope=scope or candidate.scope)
    return record.record_id


def _ensure_promotion_rollout_ledger(
    runtime: Any,
    *,
    promotion_record: RecordEnvelope,
    candidate: RecordEnvelope | None = None,
    scope: dict[str, Any] | ScopeRef | None = None,
) -> dict[str, Any] | None:
    content = dict(promotion_record.content or {})
    action = str(content.get("action") or promotion_record.meta.get("action") or "").strip()
    if action == "dry_run":
        return None
    scope_ref = scope if isinstance(scope, ScopeRef) else ScopeRef.from_dict(scope or promotion_record.scope)
    existing_id = _existing_capability_rollout_ledger_id(runtime, scope=scope_ref, promotion_id=promotion_record.record_id)
    if existing_id:
        _attach_rollout_ledger_id(runtime, promotion_record, ledger_id=existing_id)
        return {"id": existing_id, "created": False}

    sqlite = getattr(getattr(runtime, "store", None), "sqlite", None)
    record_ledger = getattr(sqlite, "_record_policy_rollout_ledger", None)
    if not callable(record_ledger):
        return None

    candidate_id = str(content.get("candidate_id") or promotion_record.meta.get("candidate_id") or "").strip()
    if candidate is None and candidate_id:
        candidate = runtime.store.get_by_id(candidate_id, scope=scope_ref)
    gate = _jsonable(content.get("gate") if isinstance(content.get("gate"), dict) else {})
    side_effect = _jsonable(content.get("side_effect") if isinstance(content.get("side_effect"), dict) else {})
    eval_result = _jsonable(content.get("eval_result") if isinstance(content.get("eval_result"), dict) else {})
    health = _jsonable(content.get("health") if isinstance(content.get("health"), dict) else {})
    ledger_test_result = _promotion_ledger_test_result(eval_result=eval_result, side_effect=side_effect)
    ledger_health_result = _promotion_ledger_health_result(health=health, side_effect=side_effect)
    ledger_rollback_command = _promotion_ledger_rollback_command(side_effect=side_effect)
    applied_artifact_ids = _applied_artifact_ids(candidate=candidate, content=content, side_effect=side_effect)
    budget_decision = _capability_budget_decision(promotion_record.status, action)
    applied_pattern_id = applied_artifact_ids[0] if budget_decision == "ok" and applied_artifact_ids else ""
    reason = _promotion_ledger_reason(action=action, gate=gate, side_effect=side_effect)
    promotion_target = str(content.get("promotion_target") or promotion_record.meta.get("promotion_target") or (candidate.meta.get("promotion_target") if candidate else "") or "")
    target_capability = str(content.get("target_capability") or promotion_record.meta.get("target_capability") or (candidate.meta.get("target_capability") if candidate else "") or "")
    source_opportunity_id = candidate_id or promotion_record.record_id
    source_opportunity = _jsonable(
        {
            "opportunity_id": source_opportunity_id,
            "opportunity_type": CAPABILITY_ROLLOUT_ACTION,
            "candidate_id": candidate_id,
            "candidate_title": candidate.title if candidate is not None else promotion_record.title,
            "candidate_summary": candidate.summary if candidate is not None else promotion_record.summary,
            "experiment_id": str((candidate.content or {}).get("experiment_id") or (candidate.meta or {}).get("experiment_id") or "") if candidate is not None else "",
            "promotion_target": promotion_target,
            "target_capability": target_capability,
            "loop_id": str(content.get("loop_id") or promotion_record.meta.get("loop_id") or ""),
            "rollout_action": action,
        }
    )
    ledger = record_ledger(
        action_type=CAPABILITY_ROLLOUT_ACTION,
        scope=scope_ref,
        promotion_id=promotion_record.record_id,
        source_opportunity_id=source_opportunity_id,
        source_opportunity=source_opportunity,
        trust_report=_jsonable(
            {
                "ok": budget_decision == "ok" and bool(gate.get("ok", True)),
                "gate": gate,
                "health": ledger_health_result,
            }
        ),
        replay_report=_promotion_replay_report(eval_result=eval_result, gate=gate),
        is_auto=True,
        applied_pattern_id=applied_pattern_id,
        budget_decision=budget_decision,
        reason=reason,
        details=_jsonable(
            standardized_lifecycle_details(
                candidate_id=candidate_id,
                patch_id=str((candidate.content or {}).get("experiment_id") or (candidate.meta or {}).get("experiment_id") or candidate_id) if candidate is not None else candidate_id,
                commit_sha=str(side_effect.get("commit_sha") or (side_effect.get("commit") or {}).get("commit_sha") or ""),
                release_path=str((side_effect.get("rollback_evidence") or {}).get("release_path") or ""),
                test_result=ledger_test_result,
                health_result=ledger_health_result,
                rollback_command=ledger_rollback_command,
                observed_count=0,
                failure_rate=0.0,
                extra={
                "promotion_request_id": promotion_record.record_id,
                "candidate_id": candidate_id,
                "promotion_target": promotion_target,
                "target_capability": target_capability,
                "rollout_status": promotion_record.status,
                "rollout_action": action,
                "applied_artifact_ids": applied_artifact_ids,
                "gate": gate,
                "health": health,
                "side_effect": side_effect,
                "eval_result": eval_result,
                },
            )
        ),
    )
    _attach_rollout_ledger_id(runtime, promotion_record, ledger_id=str(ledger.get("id") or ""))
    return {**ledger, "created": True}


def _existing_capability_rollout_ledger_id(
    runtime: Any,
    *,
    scope: ScopeRef,
    promotion_id: str,
) -> str:
    sqlite = getattr(getattr(runtime, "store", None), "sqlite", None)
    conn = getattr(sqlite, "conn", None)
    if conn is not None:
        row = conn.execute(
            """
            SELECT id
            FROM policy_rollout_ledger
            WHERE tenant_id = ?
              AND agent_id = ?
              AND workspace_id = ?
              AND user_id = ?
              AND action_type = ?
              AND promotion_id = ?
            ORDER BY created_at DESC, id DESC
            LIMIT 1
            """,
            (
                scope.tenant_id,
                scope.agent_id,
                scope.workspace_id,
                scope.user_id,
                CAPABILITY_ROLLOUT_ACTION,
                str(promotion_id),
            ),
        ).fetchone()
        return str(row["id"] or "") if row is not None else ""
    try:
        for item in runtime.get_policy_rollout_ledger(scope=scope, action=CAPABILITY_ROLLOUT_ACTION, limit=200):
            if str(item.get("promotion_id") or "") == str(promotion_id):
                return str(item.get("id") or "")
    except Exception:
        return ""
    return ""


def _attach_rollout_ledger_id(runtime: Any, promotion_record: RecordEnvelope, *, ledger_id: str) -> None:
    if not ledger_id:
        return
    content = dict(promotion_record.content or {})
    meta = dict(promotion_record.meta or {})
    if content.get("rollout_ledger_id") == ledger_id and meta.get("rollout_ledger_id") == ledger_id:
        return
    content["rollout_ledger_id"] = ledger_id
    meta["rollout_ledger_id"] = ledger_id
    promotion_record.content = content
    promotion_record.meta = meta
    promotion_record.touch()
    runtime.store.rewrite(promotion_record)


def _capability_budget_decision(status: str, action: str) -> str:
    if str(action) in {"applied", "applied_shadow"} and str(status) in {"promoted", WATCH_STATUS}:
        return "ok"
    return "blocked"


def _promotion_ledger_reason(*, action: str, gate: dict[str, Any], side_effect: dict[str, Any]) -> str:
    if side_effect.get("blocked_reason"):
        return str(side_effect.get("blocked_reason") or "")
    blocked_reasons = gate.get("blocked_reasons")
    if isinstance(blocked_reasons, list) and blocked_reasons:
        return ",".join(str(item) for item in blocked_reasons if str(item).strip())
    if action == "automation_policy_blocked":
        return "automation_policy_apply_disabled"
    if action in {"gate_failed", "adapter_failed"}:
        return action
    return ""


def _promotion_ledger_test_result(*, eval_result: dict[str, Any], side_effect: dict[str, Any]) -> dict[str, Any]:
    verification = side_effect.get("verification")
    if isinstance(verification, dict) and verification:
        return verification
    return eval_result


def _promotion_ledger_health_result(*, health: dict[str, Any], side_effect: dict[str, Any]) -> dict[str, Any]:
    post_deploy_health = side_effect.get("post_deploy_health")
    if isinstance(post_deploy_health, dict) and post_deploy_health:
        return post_deploy_health
    canary = side_effect.get("canary")
    if isinstance(canary, dict) and canary and not bool(canary.get("skipped")):
        return canary
    return health


def _promotion_ledger_rollback_command(*, side_effect: dict[str, Any]) -> str:
    rollback = side_effect.get("rollback")
    if isinstance(rollback, dict) and rollback.get("command"):
        return str(rollback.get("command") or "")
    rollback_evidence = side_effect.get("rollback_evidence")
    if isinstance(rollback_evidence, dict) and rollback_evidence.get("rollback_command"):
        return str(rollback_evidence.get("rollback_command") or "")
    return ""


def _applied_artifact_ids(
    *,
    candidate: RecordEnvelope | None,
    content: dict[str, Any],
    side_effect: dict[str, Any],
) -> list[str]:
    raw = side_effect.get("applied_artifact_ids") or content.get("applied_artifact_ids") or []
    if not raw and candidate is not None:
        raw = candidate.meta.get("applied_artifact_ids") or []
    if isinstance(raw, str):
        items = [raw]
    elif isinstance(raw, (list, tuple, set)):
        items = [str(item) for item in raw if str(item).strip()]
    else:
        items = []
    return items


def _promotion_replay_report(*, eval_result: dict[str, Any], gate: dict[str, Any]) -> dict[str, Any]:
    gate_bundle = gate.get("gate_bundle") if isinstance(gate.get("gate_bundle"), dict) else {}
    for key in ("real_task_replay", "replay_report", "replay"):
        report = gate_bundle.get(key)
        if isinstance(report, dict):
            return _jsonable({"ok": bool(report.get("ok", True)), "source": key, key: report})
    return _jsonable(
        {
            "ok": str(eval_result.get("verdict") or "pass").lower() == "pass",
            "eval_result": eval_result,
        }
    )


def _jsonable(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(item) for item in value]
    return str(value)
