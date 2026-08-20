"""Read-only comparison of legacy L5 output with the dynamic v3 assessment."""

from __future__ import annotations

from collections.abc import Mapping
from hashlib import sha256
import json
from typing import Any

from eimemory.governance.l5_assessment_v3 import build_l5_assessment_v3
from eimemory.models.records import ScopeRef


SHADOW_SCHEMA = "l5.shadow.v3"


def build_l5_v3_shadow(
    runtime: Any,
    *,
    profile_key: str,
    scope: ScopeRef | Mapping[str, Any],
    runtime_scope: ScopeRef | Mapping[str, Any] | None = None,
    capability_scope: str = "global",
    persist: bool = False,
    at_time: str = "",
    max_candidates: int = 100,
    observation_limit: int = 500,
    repo_root: str = "/dev-project/eimemory",
    catalog: Any | None = None,
) -> dict[str, Any]:
    """Compare v2/v3 semantics without modifying promotion or current L5 state."""

    from eimemory.governance.l5_readiness import build_l5_readiness_report

    v2 = build_l5_readiness_report(
        runtime,
        scope=_scope_dict(scope),
        persist=False,
        repo_root=repo_root,
        profile_key=profile_key,
        capability_scope=capability_scope,
        runtime_scope=runtime_scope,
        at_time=at_time,
        catalog=catalog,
        # Shadow's v2 side is a historical comparison baseline, never the
        # default capability selector used by live dynamic readers.
        legacy_compatibility=True,
    )
    v3 = build_l5_assessment_v3(
        runtime,
        profile_key=profile_key,
        scope=runtime_scope if runtime_scope is not None else scope,
        capability_scope=capability_scope,
        persist=False,
        at_time=at_time,
        max_candidates=max_candidates,
        observation_limit=observation_limit,
    )
    differences = _classify_differences(v2, v3)
    report = {
        "schema": SHADOW_SCHEMA,
        "ok": True,
        "persisted": False,
        "v2": _stable_summary(v2),
        "v3": _stable_summary(v3),
        "differences": differences,
    }
    report["shadow_digest"] = _digest(report)
    if persist:
        # Shadow persistence is intentionally a plain, explicitly requested
        # report stream.  It never writes an L5 v3 assessment, flips a reader,
        # or feeds a promotion decision.
        from eimemory.governance.learning_state import append_learning_record_once, stable_semantic_key

        scope_ref = scope if isinstance(scope, ScopeRef) else ScopeRef.from_dict(dict(scope))
        record = append_learning_record_once(
            runtime,
            kind="reflection",
            title=f"L5 v3 shadow: {profile_key}",
            summary="Read-only L5 v2/v3 semantic comparison",
            scope=scope_ref,
            loop_id="l5_v3_shadow",
            step_name="compare",
            semantic_key=stable_semantic_key("l5_v3_shadow", report["shadow_digest"]),
            authority_tier="L0",
            status="archived",
            content=report,
            meta={"report_type": SHADOW_SCHEMA, "shadow_digest": report["shadow_digest"]},
        )
        report["persisted"] = True
        report["record_id"] = record.record_id
    return report


def _classify_differences(v2: Mapping[str, Any], v3: Mapping[str, Any]) -> list[dict[str, str]]:
    categories: list[dict[str, str]] = []
    if str(v3.get("status") or "") == "blocked":
        categories.append(
            {
                "category": "evidence_mapping_gap",
                "detail": "v3 has no complete explicit revision/binding observation set",
            }
        )
    if v3.get("gaps"):
        categories.append(
            {
                "category": "profile_difference",
                "detail": "active dynamic profile has unresolved requirements",
            }
        )
    legacy_stage = str(v2.get("current_stage") or v2.get("observed_stage") or "")
    dynamic_stage = str(v3.get("loop_maturity") or "")
    if legacy_stage and dynamic_stage and legacy_stage != dynamic_stage:
        categories.append(
            {
                "category": "expected_taxonomy_removal",
                "detail": "v2 stage and dynamic loop maturity use different evidence models",
            }
        )
    adapter = v3.get("adapter_readiness")
    if isinstance(adapter, Mapping) and adapter.get("adapter_registry") == "unknown":
        categories.append(
            {
                "category": "adapter_difference",
                "detail": "no current provider advertisement is available for v3 adapter readiness",
            }
        )
    if not categories:
        categories.append({"category": "none", "detail": "no structural semantic difference detected"})
    return categories


def _stable_summary(value: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: value[key]
        for key in (
            "ok",
            "status",
            "current_stage",
            "observed_stage",
            "loop_maturity",
            "capability_readiness",
            "adapter_readiness",
            "deployment_assurance",
            "gaps",
            "projection",
        )
        if key in value
    }


def _scope_dict(scope: ScopeRef | Mapping[str, Any]) -> dict[str, str]:
    if isinstance(scope, ScopeRef):
        return {
            "tenant_id": scope.tenant_id,
            "agent_id": scope.agent_id,
            "workspace_id": scope.workspace_id,
            "user_id": scope.user_id,
        }
    return dict(scope)


def _digest(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return sha256(encoded.encode("utf-8")).hexdigest()


__all__ = ["SHADOW_SCHEMA", "build_l5_v3_shadow"]
