"""Bounded reconciliation of legacy L5 output and dynamic L5 v3 shadows."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import asdict
from hashlib import sha256
import json
from typing import Any

from eimemory.capabilities.contracts import CapabilityContractError, normalize_opaque_id
from eimemory.capabilities.registry import CapabilityRegistryError, exact_runtime_scope
from eimemory.governance.l5_shadow import build_l5_v3_shadow
from eimemory.models.records import ScopeRef


RECONCILE_SCHEMA = "l5.v3.reconcile.v1"
_ACCEPTED_DIFFERENCES = frozenset(
    {
        "expected_taxonomy_removal",
        "evidence_mapping_gap",
        "profile_difference",
        "adapter_difference",
        "none",
    }
)


class L5V3ReconcileError(ValueError):
    """A shadow reconciliation request is unbounded or malformed."""


def reconcile_l5_v3(
    runtime: Any,
    *,
    profile_key: str,
    scopes: Sequence[ScopeRef | Mapping[str, Any]],
    capability_scope: str = "global",
    persist: bool = False,
    max_scopes: int = 32,
    at_time: str = "",
    max_candidates: int = 100,
    observation_limit: int = 500,
    repo_root: str = "/dev-project/eimemory",
) -> dict[str, Any]:
    """Run read-only L5 shadows and classify every structural difference.

    This does not switch a reader, promote a candidate, or mutate an L5 v3
    assessment.  Persisting writes only an archived reconciliation report.
    """

    try:
        profile = str(profile_key or "").strip()
        if not profile:
            raise L5V3ReconcileError("profile_key is required")
        normalized_capability_scope = normalize_opaque_id(
            capability_scope,
            field="capability_scope",
        )
        bounded_scopes = _bounded_int(max_scopes, field="max_scopes", maximum=32)
        bounded_candidates = _bounded_int(max_candidates, field="max_candidates", maximum=499)
        bounded_observations = _bounded_int(observation_limit, field="observation_limit", maximum=500)
        if isinstance(scopes, (str, bytes)) or not isinstance(scopes, Sequence):
            raise L5V3ReconcileError("scopes must be a sequence")
        if not 1 <= len(scopes) <= bounded_scopes:
            raise L5V3ReconcileError("scope count is outside the bounded reconciliation range")
        normalized_scopes = [exact_runtime_scope(scope) for scope in scopes]
    except (CapabilityContractError, CapabilityRegistryError, TypeError, ValueError, L5V3ReconcileError) as exc:
        return _failure_report(
            reason="invalid_l5_v3_reconcile_request",
            error=exc,
            profile_key=str(profile_key or ""),
            capability_scope=str(capability_scope or ""),
        )
    reports: list[dict[str, Any]] = []
    all_differences: list[dict[str, Any]] = []
    scope_failures: list[dict[str, Any]] = []
    for scope in normalized_scopes:
        try:
            shadow = build_l5_v3_shadow(
                runtime,
                profile_key=profile,
                scope=scope,
                capability_scope=normalized_capability_scope,
                persist=False,
                at_time=at_time,
                max_candidates=bounded_candidates,
                observation_limit=bounded_observations,
                repo_root=repo_root,
            )
            if not isinstance(shadow, Mapping):
                raise L5V3ReconcileError("shadow builder returned a non-object report")
            if not isinstance(shadow.get("v2"), Mapping) or not isinstance(shadow.get("v3"), Mapping):
                raise L5V3ReconcileError("shadow builder omitted a structured v2/v3 report")
        except Exception as exc:
            scope_failures.append(
                {
                    "scope": asdict(scope),
                    "error": type(exc).__name__,
                    "detail": str(exc)[:1_000],
                }
            )
            reports.append(
                {
                    "scope": asdict(scope),
                    "status": "failed",
                    "reason": "shadow_build_failed",
                    "differences": [],
                    "unclassified_difference_count": 0,
                }
            )
            continue
        differences = [
            dict(item)
            for item in shadow.get("differences") or ()
            if isinstance(item, Mapping)
        ]
        unclassified = [
            item for item in differences if str(item.get("category") or "") not in _ACCEPTED_DIFFERENCES
        ]
        scope_report = {
            "scope": asdict(scope),
            "status": "reconciled",
            "shadow_digest": str(shadow.get("shadow_digest") or ""),
            "v2": dict(shadow.get("v2") or {}),
            "v3": dict(shadow.get("v3") or {}),
            "differences": differences,
            "unclassified_difference_count": len(unclassified),
        }
        reports.append(scope_report)
        all_differences.extend(
            {"scope": asdict(scope), **item} for item in unclassified
        )
    material = {
        "schema": RECONCILE_SCHEMA,
        "profile_key": profile,
        "capability_scope": normalized_capability_scope,
        "at_time": at_time,
        "max_candidates": bounded_candidates,
        "observation_limit": bounded_observations,
        "shadow_mode": True,
        "reader_switched": False,
        "promotion_affected": False,
        "reports": reports,
    }
    reconcile_digest = _digest(material)
    report = {
        **material,
        "ok": not all_differences and not scope_failures,
        "status": "reconciled" if not all_differences and not scope_failures else "failed",
        "reason": (
            ""
            if not all_differences and not scope_failures
            else "unclassified_structural_difference" if all_differences else "shadow_scope_failed"
        ),
        "unclassified_differences": all_differences,
        "scope_failures": scope_failures,
        "reconcile_digest": reconcile_digest,
    }
    if persist:
        from eimemory.governance.learning_state import append_learning_record_once, stable_semantic_key

        # One exact scope is intentionally required for durable learning
        # records.  Multi-scope reports are cross-scope evidence, never a
        # substitute for an exact-scope decision record.
        if len(normalized_scopes) != 1:
            return {
                **report,
                "ok": False,
                "status": "blocked",
                "reason": "persisting_reconciliation_requires_exactly_one_runtime_scope",
            }
        scope = normalized_scopes[0]
        try:
            record = append_learning_record_once(
                runtime,
                kind="reflection",
                title=f"L5 v3 reconciliation: {profile}",
                summary=(
                    "L5 v3 shadow reconciliation completed with "
                    f"{len(all_differences)} unclassified differences and "
                    f"{len(scope_failures)} scope failures"
                ),
                scope=scope,
                loop_id="l5_v3_reconcile",
                step_name="shadow_compare",
                semantic_key=stable_semantic_key("l5_v3_reconcile", reconcile_digest),
                authority_tier="L0",
                status="archived",
                content=report,
                meta={"report_type": RECONCILE_SCHEMA, "reconcile_digest": reconcile_digest},
            )
            report["record_id"] = record.record_id
        except Exception as exc:
            return {
                **report,
                "ok": False,
                "status": "failed",
                "reason": "reconciliation_report_persist_failed",
                "error": type(exc).__name__,
                "detail": str(exc)[:1_000],
            }
    return report


def _bounded_int(value: object, *, field: str, maximum: int) -> int:
    if isinstance(value, bool):
        raise L5V3ReconcileError(f"{field} must be an integer")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise L5V3ReconcileError(f"{field} must be an integer") from exc
    if not 1 <= parsed <= maximum:
        raise L5V3ReconcileError(f"{field} must be from 1 to {maximum}")
    return parsed


def _failure_report(
    *,
    reason: str,
    error: Exception,
    profile_key: str,
    capability_scope: str,
) -> dict[str, Any]:
    return {
        "schema": RECONCILE_SCHEMA,
        "ok": False,
        "status": "blocked",
        "reason": reason,
        "error": type(error).__name__,
        "detail": str(error)[:1_000],
        "profile_key": profile_key,
        "capability_scope": capability_scope,
        "shadow_mode": True,
        "reader_switched": False,
        "promotion_affected": False,
        "reports": [],
        "unclassified_differences": [],
        "scope_failures": [],
    }


def _digest(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return sha256(encoded.encode("utf-8")).hexdigest()


__all__ = ["L5V3ReconcileError", "RECONCILE_SCHEMA", "reconcile_l5_v3"]
