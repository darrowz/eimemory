from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from collections.abc import Mapping
from typing import Any

from eimemory.core.clock import now_iso
from eimemory.evaluation.capability_catalog import (
    CapabilityEvaluationCatalog,
    CatalogResolutionError,
    resolve_application_capability_catalog,
)
from eimemory.governance.capability_attribution import collect_capability_evidence
from eimemory.governance.capability_ledger import build_capability_ledger
from eimemory.governance.capability_replay_executor import validate_capability_replay_result
from eimemory.governance.capability_replay_packs import (
    LEGACY_CORE_REPLAY_CAPABILITIES,
    MANIFEST_REPORT_TYPE,
    MANIFEST_SCHEMA_VERSION,
    SELECTION_CONTRACT_SCHEMA,
    capability_replay_case_ids,
    capability_replay_log_sequence_state,
    capability_replay_manifest_digest,
    capability_replay_member_digest,
    replay_selection_contract_digest,
)
from eimemory.governance.evidence_contract import (
    EvidenceRequirement,
    ReleaseIdentity,
    current_release_identity,
    release_identity_from_record,
    release_identity_payload,
    resolve_evidence,
    same_release_authority,
    same_scope,
)
from eimemory.governance.learning_state import append_learning_record_once, stable_semantic_key
from eimemory.governance.l5_maturity import apply_monotonic_maturity
from eimemory.governance.real_replay_gate import build_verified_real_replay_summary
from eimemory.governance.release_lineage import (
    current_release_lineage,
    evidence_release_for_domain,
)
from eimemory.governance.rollout_lifecycle import is_executed_rollback_ledger_record
from eimemory.models.records import ScopeRef


# Historical v2 taxonomy.  It remains available exclusively for durable
# report/replay compatibility; default readiness selection is resolved from
# the active capability Registry/Profile below.
LEGACY_READINESS_CAPABILITIES = [
    "memory.recall",
    "tool.routing",
    "knowledge.intake",
    "proactive.judgment",
    "search.discovery",
    "research.synthesis",
    "operations.uumit",
    "device.control",
    "safety.boundary",
]

LEGACY_STRONG_CAPABILITIES = {"memory.recall", "tool.routing", "knowledge.intake", "safety.boundary"}
LEGACY_WEAK_CAPABILITIES = {"search.discovery", "research.synthesis", "operations.uumit", "device.control"}


def _resolve_readiness_catalog(
    *,
    catalog: CapabilityEvaluationCatalog | None,
    legacy_compatibility: bool,
) -> tuple[CapabilityEvaluationCatalog | None, str, str]:
    """Resolve one catalog authority for a readiness calculation."""

    if legacy_compatibility:
        try:
            from eimemory.governance.capability_acceptance import ensure_legacy_evaluation_catalog

            return (
                ensure_legacy_evaluation_catalog(catalog, legacy_compatibility=True),
                "legacy_compatibility",
                "",
            )
        except Exception as exc:
            return None, "legacy_compatibility", f"legacy_evaluation_catalog_unavailable:{type(exc).__name__}"
    try:
        registered = resolve_application_capability_catalog(catalog)
    except CatalogResolutionError as exc:
        source = "explicit" if catalog is not None else "application_default"
        # Keep a resolver's stable provisioning reason observable to L5.  In
        # particular, ``catalog_not_configured`` is materially different from
        # a malformed caller-supplied catalog: it tells operators to install
        # the dynamic catalog, while still leaving this read-only report
        # fail-closed.
        reason = str(exc).strip()
        return None, source, reason or f"evaluation_catalog_untrusted:{type(exc).__name__}"
    return registered, "explicit" if catalog is not None else "application_default", ""


def _readiness_capability_selection(
    runtime: Any,
    *,
    scope: ScopeRef,
    runtime_scope: ScopeRef | Mapping[str, Any] | None,
    capability_scope: str,
    profile_key: str,
    at_time: str,
    catalog: CapabilityEvaluationCatalog | None,
    catalog_source: str,
    catalog_error: str,
    legacy_compatibility: bool,
) -> dict[str, Any]:
    """Resolve the capability cohort without inferring one from package/host.

    The v2 report can still read legacy evidence for migration comparison, but
    the normal path consumes only explicit Registry/Profile descriptors.  An
    empty or invalid dynamic selection is evidence of an unconfigured control
    plane, not a reason to revive a compiled vocabulary.
    """

    if legacy_compatibility:
        entries = [
            {
                "capability_id": capability_id,
                "risk_tier": "",
                "requirement": {
                    "min_evidence_count": 3,
                    "min_sample_count": 3,
                },
                "requires_outcome": capability_id in LEGACY_WEAK_CAPABILITIES,
                "planning_policy": {},
            }
            for capability_id in LEGACY_READINESS_CAPABILITIES
        ]
        return {
            "ok": True,
            "mode": "legacy_compatibility",
            "reason": "",
            "capabilities": entries,
            "capability_ids": list(LEGACY_READINESS_CAPABILITIES),
            "profile": {},
            "profile_key": "",
            "capability_scope": capability_scope,
            "runtime_scope": asdict(scope),
            "resolution_digest": "",
            "registry_watermark": "",
            "lifecycle_watermark": "",
        }

    if catalog is None:
        return {
            "ok": False,
            "mode": "dynamic",
            "reason": catalog_error or "evaluation_catalog_required",
            "capabilities": [],
            "capability_ids": [],
            "profile": {},
            "profile_key": str(profile_key or "").strip(),
            "capability_scope": capability_scope,
            "runtime_scope": asdict(scope),
            "evaluation_catalog": {
                "status": "blocked",
                "source": catalog_source,
                "reason": catalog_error or "evaluation_catalog_required",
            },
        }
    try:
        from eimemory.capabilities.consumer_views import dynamic_evaluation_view
        from eimemory.capabilities.registry import exact_runtime_scope

        exact_scope = exact_runtime_scope(runtime_scope if runtime_scope is not None else scope)
        if asdict(exact_scope) != asdict(scope):
            return {
                "ok": False,
                "mode": "dynamic",
                "reason": "dynamic_runtime_scope_mismatch",
                "capabilities": [],
                "capability_ids": [],
                "profile": {},
                "profile_key": str(profile_key or "").strip(),
                "capability_scope": capability_scope,
                "runtime_scope": asdict(exact_scope),
            }
        view = dynamic_evaluation_view(
            runtime,
            scope=exact_scope,
            capability_scope=capability_scope,
            profile_key=str(profile_key or "").strip(),
            catalog=catalog,
            at_time=at_time,
            max_cases=256,
        )
    except Exception as exc:
        return {
            "ok": False,
            "mode": "dynamic",
            "reason": f"dynamic_capability_selection_failed:{type(exc).__name__}",
            "capabilities": [],
            "capability_ids": [],
            "profile": {},
            "profile_key": str(profile_key or "").strip(),
            "capability_scope": capability_scope,
            "runtime_scope": asdict(scope),
        }

    if view.get("ok") is not True:
        return {
            "ok": False,
            "mode": "dynamic",
            "reason": str(view.get("reason") or "dynamic_evaluation_catalog_selection_blocked"),
            "capabilities": [],
            "capability_ids": [],
            "profile": dict(view.get("profile") or {}),
            "profile_key": str(profile_key or "").strip(),
            "capability_scope": capability_scope,
            "runtime_scope": dict(view.get("scope") or asdict(scope)),
            "evaluation_catalog": {
                "status": "blocked",
                "source": catalog_source,
                "reason": str(view.get("reason") or "dynamic_evaluation_catalog_selection_blocked"),
            },
        }
    capability_view = view.get("capability_view") if isinstance(view.get("capability_view"), Mapping) else {}
    entries = [
        dict(item)
        for item in capability_view.get("capabilities") or []
        if isinstance(item, Mapping) and str(item.get("capability_id") or "").strip()
    ]
    entries.sort(key=lambda item: str(item.get("capability_id") or ""))
    capability_ids = list(dict.fromkeys(str(item["capability_id"]) for item in entries))
    resolved_capabilities = {
        str((item.get("artifact") or {}).get("capability") or "").strip()
        for item in view.get("cases") or []
        if isinstance(item, Mapping) and isinstance(item.get("artifact"), Mapping)
    }
    missing_catalog_cases = sorted(set(capability_ids) - resolved_capabilities)
    if missing_catalog_cases:
        return {
            "ok": False,
            "mode": "dynamic",
            "reason": "evaluation_catalog_missing_profile_capability_cases",
            "capabilities": entries,
            "capability_ids": capability_ids,
            "profile": dict(view.get("profile") or capability_view.get("profile") or {}),
            "profile_key": str(profile_key or "").strip(),
            "capability_scope": capability_scope,
            "runtime_scope": dict(view.get("scope") or asdict(scope)),
            "evaluation_catalog": {
                "status": "blocked",
                "source": catalog_source,
                "reason": "evaluation_catalog_missing_profile_capability_cases",
                "missing_capabilities": missing_catalog_cases,
            },
        }
    return {
        "ok": bool(capability_ids),
        "mode": "dynamic",
        "reason": "" if capability_ids else "dynamic_capability_selection_empty",
        "capabilities": entries,
        "capability_ids": capability_ids,
        "profile": dict(view.get("profile") or capability_view.get("profile") or {}),
        "profile_key": str(profile_key or "").strip(),
        "capability_scope": capability_scope,
        "runtime_scope": dict(view.get("scope") or asdict(scope)),
        "resolution_digest": str(view.get("resolution_digest") or capability_view.get("resolution_digest") or ""),
        "registry_watermark": str(view.get("registry_watermark") or capability_view.get("registry_watermark") or ""),
        "lifecycle_watermark": str(view.get("lifecycle_watermark") or capability_view.get("lifecycle_watermark") or ""),
        "source": str(capability_view.get("source") or ""),
        "evaluation_catalog": {
            "status": "resolved",
            "source": catalog_source,
            "case_ids": sorted(
                str((item.get("artifact") or {}).get("case_id") or "")
                for item in view.get("cases") or []
                if isinstance(item, Mapping) and isinstance(item.get("artifact"), Mapping)
            ),
        },
    }


def _selection_requirement(entry: Mapping[str, Any] | object, field: str, default: int) -> int:
    requirement = entry.get("requirement") if isinstance(entry, Mapping) and isinstance(entry.get("requirement"), Mapping) else {}
    value = requirement.get(field, default)
    if isinstance(value, bool):
        return default
    try:
        numeric = int(value)
    except (TypeError, ValueError):
        return default
    return max(1, min(numeric, 10_000))


def _selection_replay_minimums(selection: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    minimums: dict[str, dict[str, Any]] = {}
    for entry in selection.get("capabilities") or []:
        if not isinstance(entry, Mapping):
            continue
        capability_id = str(entry.get("capability_id") or "").strip()
        if not capability_id:
            continue
        evidence_minimum = _selection_requirement(entry, "min_evidence_count", 3)
        sample_minimum = _selection_requirement(entry, "min_sample_count", evidence_minimum)
        minimums[capability_id] = {
            "minimum_executed": max(evidence_minimum, sample_minimum),
            "minimum_distinct_evidence": sample_minimum,
            "minimum_pass_rate": 0.8,
        }
    return minimums


def _selection_outcome_minimum(entry: Mapping[str, Any] | object) -> int:
    if not isinstance(entry, Mapping):
        return 0
    if entry.get("requires_outcome") is False:
        return 0
    requirement = entry.get("requirement") if isinstance(entry.get("requirement"), Mapping) else {}
    if "min_sample_count" in requirement:
        return _selection_requirement(entry, "min_sample_count", 0)
    return 3 if entry.get("requires_outcome") is True else 0


def _selection_priority(entry: Mapping[str, Any] | object) -> str:
    if not isinstance(entry, Mapping):
        return "medium"
    planning = entry.get("planning_policy") if isinstance(entry.get("planning_policy"), Mapping) else {}
    priority = planning.get("priority_weight")
    try:
        if not isinstance(priority, bool) and float(priority) >= 0.75:
            return "high"
    except (TypeError, ValueError):
        pass
    risk_tier = str(entry.get("risk_tier") or "").strip().lower()
    return "high" if risk_tier in {"high", "critical"} else "medium"


def _replay_missing(summary: Mapping[str, Any]) -> list[str]:
    for key in ("capabilities_missing", "weak_capabilities_missing", "core_capabilities_missing"):
        value = summary.get(key)
        if isinstance(value, list):
            return [str(item) for item in value if str(item)]
    return []


def _replay_missing_field_is_valid(summary: Mapping[str, Any]) -> bool:
    """Require an explicit list-valued replay-gap field before L5 promotion.

    ``_replay_missing`` intentionally returns an empty list for display when a
    malformed historical summary has no usable field.  The readiness gate must
    distinguish that presentation fallback from verified evidence: a missing
    field is unknown, never evidence of zero gaps.
    """

    return any(
        isinstance(summary.get(key), list)
        for key in ("capabilities_missing", "weak_capabilities_missing", "core_capabilities_missing")
    )


def readiness_gate_status(
    readiness: dict[str, Any],
    *,
    runtime: Any | None = None,
    scope: dict[str, Any] | ScopeRef | None = None,
    repo_root: str = "/dev-project/eimemory",
) -> str:
    """Return the only release-gate states backed by complete L5 evidence."""

    # The v2 gate has a fixed historical evidence contract.  Dynamic L5 is
    # evaluated by the v3 reader/assessment, so a registry-selected v2 report
    # must never be promoted through this compatibility gate.
    if (
        readiness.get("schema_version") != "l5_readiness.v2"
        or readiness.get("legacy_compatibility") is not True
        or runtime is None
    ):
        return ""
    scope_ref = scope if isinstance(scope, ScopeRef) else ScopeRef.from_dict(
        scope if scope is not None else readiness.get("scope")
    )
    exact_provider = getattr(runtime, "current_release_identity", None)
    exact_release = (
        exact_provider(scope=asdict(scope_ref), limit=500)
        if callable(exact_provider)
        else current_release_identity(runtime, scope_ref)
    )
    if exact_release is None:
        return ""
    release_identity = readiness.get("release_identity") if isinstance(readiness.get("release_identity"), dict) else {}
    if not same_release_authority(
        release_identity_from_record(release_identity),
        exact_release,
    ):
        return ""
    reported_lineage = (
        readiness.get("release_lineage")
        if isinstance(readiness.get("release_lineage"), dict)
        else {}
    )
    lineage_provider = getattr(runtime, "current_release_lineage", None)
    verified_lineage = (
        lineage_provider(
            scope=asdict(scope_ref),
            current_release=exact_release,
            repo_root=repo_root,
        )
        if callable(lineage_provider)
        else current_release_lineage(
            runtime,
            scope=scope_ref,
            current_release=exact_release,
            repo_root=repo_root,
            legacy_compatibility=True,
        )
    )
    if (
        verified_lineage.get("ok") is not True
        or verified_lineage.get("validated") is not True
        or verified_lineage.get("compatible") is not True
        or reported_lineage != verified_lineage
    ):
        return ""
    assessment = (
        readiness.get("latest_l5_assessment")
        if isinstance(readiness.get("latest_l5_assessment"), dict)
        else {}
    )
    live_gate = readiness.get("live_task_gate") if isinstance(readiness.get("live_task_gate"), dict) else {}
    reported_real_business_gate = (
        readiness.get("real_business_gate")
        if isinstance(readiness.get("real_business_gate"), dict)
        else {}
    )
    reported_real_replay = (
        reported_real_business_gate.get("real_replay")
        if isinstance(reported_real_business_gate.get("real_replay"), dict)
        else readiness.get("verified_real_replay")
        if isinstance(readiness.get("verified_real_replay"), dict)
        else {}
    )
    live_only_gate = _real_business_gate(live_gate, {})
    if live_only_gate.get("ok") is not True:
        replay_loader = getattr(
            getattr(runtime, "store", None),
            "latest_record_by_meta_value_exact_scope",
            None,
        )
        if not callable(replay_loader):
            return ""
        reported_real_replay = build_verified_real_replay_summary(
            runtime,
            scope=scope_ref,
            limit=500,
        )
    real_business_gate = _real_business_gate(live_gate, reported_real_replay)
    if reported_real_business_gate and (
        reported_real_business_gate.get("ok") is not real_business_gate.get("ok")
        or str(reported_real_business_gate.get("accepted_path") or "")
        != str(real_business_gate.get("accepted_path") or "")
    ):
        return ""
    recall_gate = (
        readiness.get("production_recall_gate")
        if isinstance(readiness.get("production_recall_gate"), dict)
        else {}
    )
    strict_state = (
        readiness.get("production_recall_strict_state")
        if isinstance(readiness.get("production_recall_strict_state"), dict)
        else {}
    )
    replay = readiness.get("verified_replay") if isinstance(readiness.get("verified_replay"), dict) else {}
    core_replay = (
        readiness.get("verified_core_replay")
        if isinstance(readiness.get("verified_core_replay"), dict)
        else {}
    )
    capability_gaps = readiness.get("capability_gaps")
    weak_missing = replay.get("weak_capabilities_missing")
    weak_manifest_rejections = replay.get("manifest_rejection_reasons")
    core_missing = core_replay.get("core_capabilities_missing")
    core_manifest_rejections = core_replay.get("manifest_rejection_reasons")
    common_verified = bool(
        readiness.get("ok") is True
        and isinstance(capability_gaps, list)
        and not capability_gaps
        and assessment.get("trusted") is True
        and assessment.get("complete") is True
        and assessment.get("level") == "L5"
        and int(replay.get("executed_count") or 0) >= 10
        and isinstance(weak_missing, list)
        and not weak_missing
        and isinstance(weak_manifest_rejections, dict)
        and not weak_manifest_rejections
        and int(core_replay.get("executed_count") or 0) >= len(LEGACY_CORE_REPLAY_CAPABILITIES) * 3
        and isinstance(core_missing, list)
        and not core_missing
        and isinstance(core_manifest_rejections, dict)
        and not core_manifest_rejections
        and isinstance(readiness.get("production_recall_gate"), dict)
        and readiness["production_recall_gate"].get("ok") is True
        and readiness["production_recall_gate"].get("status") == "accepted"
        and strict_state.get("ok") is True
        and strict_state.get("status") == "strict_activated"
        and bool(str(release_identity.get("release_commit") or ""))
        and str(strict_state.get("candidate_commit") or "")
        == str(
            recall_gate.get("evidence_release_commit")
            or release_identity.get("release_commit")
            or ""
        )
        and isinstance(readiness.get("storage_migrations"), dict)
        and readiness["storage_migrations"].get("ok") is True
        and readiness["storage_migrations"].get("pending") == []
    )
    if not common_verified:
        return ""
    score = readiness.get("observed_score", readiness.get("readiness_score"))
    observed_stage = readiness.get("observed_stage", readiness.get("current_stage"))
    if (
        observed_stage == "L5"
        and readiness.get("current_stage") == "L5"
        and isinstance(score, (int, float))
        and not isinstance(score, bool)
        and float(score) == 1.0
        and real_business_gate.get("ok") is True
    ):
        return "L5"
    return ""


def build_l5_readiness_report(
    runtime: Any,
    *,
    scope: dict[str, Any] | ScopeRef | None = None,
    persist: bool = False,
    limit: int = 500,
    loop_id: str = "l5_readiness",
    repo_root: str = "/dev-project/eimemory",
    profile_key: str = "",
    capability_scope: str = "global",
    runtime_scope: ScopeRef | Mapping[str, Any] | None = None,
    at_time: str = "",
    catalog: CapabilityEvaluationCatalog | None = None,
    legacy_compatibility: bool = False,
) -> dict[str, Any]:
    """Build a read-only L5 readiness report from existing governance evidence.

    The normal v2 reader path selects active capabilities from the registry or
    a named Profile.  The historical strong/weak/core cohort is available only
    for migration comparison through ``legacy_compatibility=True``.
    """

    scope_ref = scope if isinstance(scope, ScopeRef) else ScopeRef.from_dict(scope)
    active_catalog, catalog_source, catalog_error = _resolve_readiness_catalog(
        catalog=catalog,
        legacy_compatibility=legacy_compatibility,
    )
    capability_selection = _readiness_capability_selection(
        runtime,
        scope=scope_ref,
        runtime_scope=runtime_scope,
        capability_scope=capability_scope,
        profile_key=profile_key,
        at_time=at_time,
        catalog=active_catalog,
        catalog_source=catalog_source,
        catalog_error=catalog_error,
        legacy_compatibility=legacy_compatibility,
    )
    selected_capabilities = set(str(item) for item in capability_selection.get("capability_ids") or [] if str(item))
    replay_minimums = _selection_replay_minimums(capability_selection)
    replay_missing_field = "weak_capabilities_missing" if legacy_compatibility else "capabilities_missing"
    release = current_release_identity(runtime, scope_ref, limit=limit)
    release_lineage, evidence_releases = _resolve_readiness_release_lineage(
        runtime,
        scope=scope_ref,
        current_release=release,
        repo_root=repo_root,
        catalog=active_catalog,
        legacy_compatibility=legacy_compatibility,
    )
    channel_release = evidence_releases["channel.openclaw"]
    governance_release = evidence_releases["memory.governance"]
    bootstrap_pending = {"ok": False, "status": "not_run"}
    if release is not None:
        from eimemory.evaluation.real_query_gate import verify_current_bootstrap_data_pending

        try:
            bootstrap_pending = verify_current_bootstrap_data_pending(
                runtime,
                scope=scope_ref,
                release=release,
            )
        except Exception:
            bootstrap_pending = {"ok": False, "status": "not_run"}
    recall_release = (
        release
        if bootstrap_pending.get("ok") is True
        and bootstrap_pending.get("status") == "bootstrap_data_pending"
        else evidence_releases["memory.recall"]
    )
    ledger = build_capability_ledger(
        runtime,
        scope=scope_ref,
        limit=limit,
        attribute_outcomes=False,
        legacy_compatibility=legacy_compatibility,
    )
    hard_metrics = _safe_hard_metrics(
        runtime,
        scope=scope_ref,
        limit=limit,
        real_task_evidence_release=channel_release,
    )
    evidence_counts = _evidence_counts(runtime, scope=scope_ref, limit=limit)
    verified_replay = _verified_replay_summary(
        runtime,
        scope=scope_ref,
        limit=limit,
        capabilities=(LEGACY_WEAK_CAPABILITIES if legacy_compatibility else selected_capabilities),
        missing_field=replay_missing_field,
        release=governance_release,
        minimums=(None if legacy_compatibility else replay_minimums),
        catalog=active_catalog,
        legacy_compatibility=legacy_compatibility,
    )
    if legacy_compatibility:
        verified_core_replay = _verified_replay_summary(
            runtime,
            scope=scope_ref,
            limit=limit,
            capabilities=set(LEGACY_CORE_REPLAY_CAPABILITIES),
            missing_field="core_capabilities_missing",
            release=governance_release,
            minimums=None,
            catalog=active_catalog,
            legacy_compatibility=True,
        )
    else:
        # Dynamic selection has one registry-defined cohort, not invented
        # "weak" and "core" partitions.  Preserve the field only for readers
        # that still expect a second replay object during migration.
        verified_core_replay = {
            **verified_replay,
            "shared_dynamic_selection": True,
            "core_capabilities_missing": list(_replay_missing(verified_replay)),
        }
    verified_real_replay = build_verified_real_replay_summary(
        runtime,
        scope=scope_ref,
        limit=limit,
    )
    latest_l5_assessment = _annotate_release_evidence(
        _latest_l5_assessment(runtime, scope=scope_ref, release=governance_release),
        evidence_release=governance_release,
        current_release=release,
    )
    from eimemory.evaluation.production_recall import (
        verify_current_production_recall_gate,
        verify_current_production_recall_strict_state,
    )
    try:
        production_recall_gate = verify_current_production_recall_gate(
            runtime,
            scope=scope_ref,
            release=recall_release,
            limit=limit,
        )
    except Exception as exc:
        production_recall_gate = {
            "ok": False,
            "status": "not_run",
            "reason": f"production_recall_gate_error:{type(exc).__name__}",
            "record_id": "",
        }
    try:
        production_recall_strict_state = verify_current_production_recall_strict_state(
            runtime,
            scope=scope_ref,
            release=recall_release,
            gate_record_id=str(production_recall_gate.get("record_id") or ""),
        )
    except Exception as exc:
        production_recall_strict_state = {
            "ok": False,
            "status": "not_run",
            "reason": f"production_recall_strict_state_error:{type(exc).__name__}",
            "record_id": "",
        }
    if (
        production_recall_strict_state.get("ok") is True
        and production_recall_strict_state.get("status") == "strict_activated"
    ):
        strict_commit = str(production_recall_strict_state.get("candidate_commit") or "")
        gate_record_id = str(production_recall_gate.get("record_id") or "")
        strict_gate_record_id = str(production_recall_strict_state.get("gate_record_id") or "")
        if recall_release is None or strict_commit != recall_release.commit:
            production_recall_strict_state = {
                **production_recall_strict_state,
                "ok": False,
                "status": "blocked",
                "reason": "strict_state_commit_mismatch",
            }
        elif not gate_record_id or strict_gate_record_id != gate_record_id:
            production_recall_strict_state = {
                **production_recall_strict_state,
                "ok": False,
                "status": "blocked",
                "reason": "strict_gate_record_mismatch",
            }
    production_recall_gate = _annotate_release_evidence(
        production_recall_gate,
        evidence_release=recall_release,
        current_release=release,
    )
    production_recall_strict_state = _annotate_release_evidence(
        production_recall_strict_state,
        evidence_release=recall_release,
        current_release=release,
    )
    verified_replay = _annotate_release_evidence(
        verified_replay,
        evidence_release=governance_release,
        current_release=release,
    )
    verified_core_replay = _annotate_release_evidence(
        verified_core_replay,
        evidence_release=governance_release,
        current_release=release,
    )
    storage_migrations = _storage_migration_status(runtime)
    outcome_evidence = _capability_outcome_evidence(
        runtime,
        scope=scope_ref,
        limit=limit,
        capability_selection=capability_selection,
        catalog=active_catalog,
        legacy_compatibility=legacy_compatibility,
    )
    capability_gaps = _capability_gaps(
        ledger,
        outcome_evidence=outcome_evidence,
        capability_selection=capability_selection,
    )
    stage = _stage_for(
        ledger,
        hard_metrics,
        evidence_counts,
        capability_gaps,
        outcome_evidence,
        verified_replay,
        verified_core_replay,
        latest_l5_assessment,
        verified_real_replay,
        capability_selection=capability_selection,
        legacy_compatibility=legacy_compatibility,
    )
    stage = _apply_production_recall_gate(stage, production_recall_gate)
    stage = _apply_production_recall_strict_state_gate(stage, production_recall_strict_state)
    stage = _apply_storage_migration_gate(stage, storage_migrations)
    stage = _apply_release_lineage_gate(
        stage,
        release_lineage=release_lineage,
        current_release=release,
    )
    if not isinstance(stage.get("real_business_gate"), dict):
        stage = {
            **stage,
            "real_business_gate": _real_business_gate(
                stage.get("live_task_gate")
                if isinstance(stage.get("live_task_gate"), dict)
                else {},
                verified_real_replay,
            ),
        }
    observed_stage = str(stage["stage"])
    observed_score = float(stage["readiness_score"])
    maturity = apply_monotonic_maturity(
        runtime,
        scope=scope_ref,
        observed_stage=observed_stage,
        observed_score=observed_score,
        persist=persist,
        loop_id=loop_id,
    )
    next_actions = _next_actions(
        stage,
        capability_gaps,
        evidence_counts,
        verified_replay=verified_replay,
        latest_l5_assessment=latest_l5_assessment,
        production_recall_gate=production_recall_gate,
        production_recall_strict_state=production_recall_strict_state,
        legacy_compatibility=legacy_compatibility,
    )
    if storage_migrations.get("pending"):
        next_actions = [
            "Complete and verify all deferred storage migrations before claiming L5 or closing a release.",
            *next_actions,
        ]
    report = {
        "ok": True,
        "report_type": "l5_readiness_report",
        "schema_version": "l5_readiness.v2",
        "generated_at": now_iso(),
        "scope": asdict(scope_ref),
        "legacy_compatibility": bool(legacy_compatibility),
        "capability_selection": capability_selection,
        "evaluation_catalog": {
            "status": "resolved" if active_catalog is not None and not catalog_error else "blocked",
            "source": catalog_source,
            "reason": catalog_error,
            "legacy_compatibility": bool(legacy_compatibility),
        },
        "profile_key": str(profile_key or "").strip(),
        "capability_scope": capability_scope,
        "release_identity": release_identity_payload(release) if release is not None else {},
        "release_lineage": release_lineage,
        "observed_stage": observed_stage,
        "observed_score": observed_score,
        "current_stage": observed_stage,
        "stage_label": stage["label"],
        "readiness_score": observed_score,
        "stage_reason": stage["reason"],
        "done_when": stage["done_when"],
        "risk_boundary": stage["risk_boundary"],
        "evidence_counts": evidence_counts,
        "hard_metrics": hard_metrics.get("metrics", {}),
        "hard_metric_quality": hard_metrics.get("metric_quality", {}),
        "hard_metric_samples": hard_metrics.get("sample_counts", {}),
        "live_task_gate": stage["live_task_gate"],
        "verified_real_replay": verified_real_replay,
        "real_business_gate": stage["real_business_gate"],
        "verified_replay": verified_replay,
        "verified_core_replay": verified_core_replay,
        "latest_l5_assessment": latest_l5_assessment,
        "production_recall_gate": production_recall_gate,
        "production_recall_strict_state": production_recall_strict_state,
        "storage_migrations": storage_migrations,
        "outcome_evidence": outcome_evidence,
        # Retained only for consumers replaying a historical v2 report.
        "weak_outcome_evidence": outcome_evidence if legacy_compatibility else {},
        "capability_gaps": capability_gaps,
        "next_actions": next_actions,
        "release_validation": _release_validation(
            release=release,
            release_lineage=release_lineage,
            production_recall_gate=production_recall_gate,
            production_recall_strict_state=production_recall_strict_state,
            storage_migrations=storage_migrations,
        ),
        "accumulated_maturity": {
            "current_stage": maturity["current_stage"],
            "readiness_score": maturity["readiness_score"],
        },
        "maturity_transition": maturity["maturity_transition"],
        "maturity_checkpoint_record_id": maturity["maturity_checkpoint_record_id"],
        "downgrade_incident_id": maturity["downgrade_incident_id"],
        "regression_warning": maturity["regression_warning"],
        "ledger": ledger,
        "persisted_record_id": "",
    }
    if persist:
        record = append_learning_record_once(
            runtime,
            kind="reflection",
            title="L5 readiness report",
            summary=f"{observed_stage} readiness score {observed_score}",
            scope=scope_ref,
            loop_id=loop_id,
            step_name="l5_readiness",
            semantic_key=stable_semantic_key(
                "l5_readiness",
                scope_ref,
                observed_stage,
                maturity["current_stage"],
                evidence_counts,
                capability_gaps,
                capability_selection.get("resolution_digest"),
                capability_selection.get("registry_watermark"),
                bool(legacy_compatibility),
            ),
            authority_tier="L0",
            status="active",
            content=report,
            meta={
                "report_type": "l5_readiness_report",
                "stage": observed_stage,
                "observed_stage": observed_stage,
                "readiness_score": observed_score,
                "legacy_compatibility": bool(legacy_compatibility),
                "profile_key": str(profile_key or "").strip(),
                "capability_scope": capability_scope,
                "capability_resolution_digest": str(capability_selection.get("resolution_digest") or ""),
            },
            source="eimemory.l5_readiness",
        )
        report["persisted_record_id"] = record.record_id
    return report


def _resolve_readiness_release_lineage(
    runtime: Any,
    *,
    scope: ScopeRef,
    current_release: ReleaseIdentity | None,
    repo_root: str,
    catalog: CapabilityEvaluationCatalog | None,
    legacy_compatibility: bool,
) -> tuple[dict[str, Any], dict[str, ReleaseIdentity | None]]:
    domains = ("channel.openclaw", "memory.governance", "memory.recall")
    releases = {domain: current_release for domain in domains}
    if current_release is None:
        return {"ok": False, "error": "current_release_receipt_invalid"}, releases
    lineage = current_release_lineage(
        runtime,
        scope=scope,
        current_release=current_release,
        repo_root=repo_root,
        catalog=catalog,
        legacy_compatibility=legacy_compatibility,
    )
    if (
        not isinstance(lineage, dict)
        or lineage.get("ok") is not True
        or lineage.get("validated") is not True
        or lineage.get("compatible") is not True
    ):
        return (
            lineage
            if isinstance(lineage, dict)
            else {"ok": False, "error": "current_release_lineage_invalid"},
            releases,
        )
    for domain in domains:
        try:
            releases[domain] = evidence_release_for_domain(
                runtime,
                scope=scope,
                repo_root=repo_root,
                domain=domain,
                current_release=current_release,
                expected_record_id=str(lineage.get("record_id") or ""),
                catalog=catalog,
                legacy_compatibility=legacy_compatibility,
            )
        except (OSError, TypeError, ValueError) as exc:
            return {
                **lineage,
                "ok": False,
                "validated": False,
                "compatible": False,
                "error": "lineage_evidence_resolution_failed",
                "domain": domain,
                "detail": str(exc),
            }, {name: current_release for name in domains}
    return lineage, releases


def _annotate_release_evidence(
    report: dict[str, Any],
    *,
    evidence_release: ReleaseIdentity | None,
    current_release: ReleaseIdentity | None,
) -> dict[str, Any]:
    evidence_commit = evidence_release.commit if evidence_release is not None else ""
    current_commit = current_release.commit if current_release is not None else ""
    return {
        **report,
        "evidence_mode": (
            "lineage_inherited"
            if evidence_commit and current_commit and evidence_commit != current_commit
            else "current_release"
        ),
        "evidence_release_commit": evidence_commit,
        "current_release_commit": current_commit,
    }


def _apply_production_recall_gate(stage: dict[str, Any], gate: dict[str, Any]) -> dict[str, Any]:
    if gate.get("ok") is True and gate.get("status") == "accepted":
        return stage
    downgraded = dict(stage)
    downgraded["readiness_score"] = min(float(stage.get("readiness_score") or 0.0), 0.8)
    if str(stage.get("stage") or "") in {"L5", "data_accumulating"}:
        downgraded.update(
            {
                "stage": "L4.5",
                "label": "L5 evidence present; production recall gate incomplete",
                "reason": "The production real-query recall gate is not independently accepted for the current release.",
                "done_when": "Collect and label the required real queries, then pass the release-bound production recall gate.",
            }
        )
    return downgraded


def _apply_production_recall_strict_state_gate(
    stage: dict[str, Any],
    strict_state: dict[str, Any],
) -> dict[str, Any]:
    if strict_state.get("ok") is True and strict_state.get("status") == "strict_activated":
        return stage
    downgraded = dict(stage)
    downgraded["readiness_score"] = min(float(stage.get("readiness_score") or 0.0), 0.8)
    if str(stage.get("stage") or "") == "L5":
        downgraded.update(
            {
                "stage": "L4.5",
                "label": "L5 evidence present; production recall strict state missing",
                "reason": "The accepted production recall gate is not activated for the current release.",
                "done_when": "Activate and verify the accepted recall gate for the current release commit.",
            }
        )
    return downgraded


def _storage_migration_status(runtime: Any) -> dict[str, Any]:
    provider = getattr(getattr(getattr(runtime, "store", None), "sqlite", None), "pending_storage_migrations", None)
    if not callable(provider):
        return {"ok": False, "status": "unavailable", "pending": ["migration_status_unavailable"]}
    try:
        pending = provider()
    except Exception as exc:
        return {"ok": False, "status": "error", "pending": [f"migration_status_error:{type(exc).__name__}"]}
    if not isinstance(pending, list) or any(not isinstance(item, str) or not item for item in pending):
        return {"ok": False, "status": "invalid", "pending": ["migration_status_invalid"]}
    normalized = sorted(set(pending))
    return {"ok": not normalized, "status": "ready" if not normalized else "pending", "pending": normalized}


def _apply_storage_migration_gate(stage: dict[str, Any], migration: dict[str, Any]) -> dict[str, Any]:
    if migration.get("ok") is True and migration.get("pending") == []:
        return stage
    downgraded = dict(stage)
    downgraded["readiness_score"] = min(float(stage.get("readiness_score") or 0.0), 0.8)
    if str(stage.get("stage") or "") == "L5":
        downgraded.update(
            {
                "stage": "L4.5",
                "label": "L5 evidence present; storage migrations pending",
                "reason": "Deferred storage migrations must finish before L5 can be claimed.",
            }
        )
    return downgraded


def _apply_release_lineage_gate(
    stage: dict[str, Any],
    *,
    release_lineage: dict[str, Any],
    current_release: ReleaseIdentity | None,
) -> dict[str, Any]:
    lineage_current = (
        release_lineage.get("current_release")
        if isinstance(release_lineage.get("current_release"), dict)
        else {}
    )
    lineage_valid = bool(
        current_release is not None
        and release_lineage.get("ok") is True
        and release_lineage.get("validated") is True
        and release_lineage.get("compatible") is True
        and same_release_authority(
            ReleaseIdentity(
                commit=str(lineage_current.get("commit") or ""),
                version=str(lineage_current.get("version") or ""),
                receipt_id=str(lineage_current.get("receipt_id") or ""),
                session_id=str(lineage_current.get("session_id") or ""),
            ),
            current_release,
        )
    )
    if lineage_valid:
        return stage
    downgraded = dict(stage)
    downgraded["readiness_score"] = min(float(stage.get("readiness_score") or 0.0), 0.8)
    if str(stage.get("stage") or "") == "L5":
        downgraded.update(
            {
                "stage": "L4.5",
                "label": "L5 evidence present; release lineage incomplete",
                "reason": "The exact current release does not have a valid compatible runtime-recomputed lineage.",
                "done_when": "Record and revalidate authoritative gates for every changed production domain.",
            }
        )
    return downgraded


def _safe_hard_metrics(
    runtime: Any,
    *,
    scope: ScopeRef,
    limit: int,
    real_task_evidence_release: ReleaseIdentity | None = None,
) -> dict[str, Any]:
    try:
        from eimemory.governance.capability_dashboard import build_capability_dashboard_metrics

        return build_capability_dashboard_metrics(
            runtime,
            scope=scope,
            persist=False,
            limit=limit,
            real_task_evidence_release=real_task_evidence_release,
        )
    except Exception as exc:
        return {"ok": False, "error": type(exc).__name__, "detail": str(exc), "metrics": {}, "sample_counts": {}}


def _evidence_counts(runtime: Any, *, scope: ScopeRef, limit: int) -> dict[str, int]:
    kinds = [
        "memory",
        "learning_loop",
        "learning_goal",
        "learning_eval",
        "replay_result",
        "capability_candidate",
        "promotion_request",
        "capability_score",
        "rl_transition",
        "regression_watch",
        "l5_world_model",
        "l5_strategic_roadmap",
        "l5_self_continuity",
        "l5_assessment",
        "l5_closed_loop",
    ]
    counts: dict[str, int] = {}
    exact_counter = getattr(runtime.store, "count_records_exact_scope", None)
    for kind in kinds:
        try:
            if callable(exact_counter):
                counts[kind] = int(exact_counter(kinds=[kind], scope=scope))
            else:
                counts[kind] = sum(
                    1
                    for record in runtime.store.list_records(kinds=[kind], scope=scope, limit=limit)
                    if _record_has_exact_scope(record, scope)
                )
        except Exception:
            counts[kind] = 0
    counts["promotion_applied"] = _count_status(runtime, scope=scope, kind="promotion_request", statuses={"promoted", "active", "deployed"}, limit=limit)
    counts["rollback_or_quarantine"] = _policy_rollback_count(runtime, scope=scope, limit=limit)
    return counts


def _count_status(runtime: Any, *, scope: ScopeRef, kind: str, statuses: set[str], limit: int) -> int:
    exact_counter = getattr(runtime.store, "count_records_exact_scope", None)
    if callable(exact_counter):
        try:
            return int(exact_counter(kinds=[kind], scope=scope, statuses=sorted(statuses)))
        except Exception:
            return 0
    try:
        records = [
            record
            for record in runtime.store.list_records(kinds=[kind], scope=scope, limit=limit)
            if _record_has_exact_scope(record, scope)
        ]
    except Exception:
        return 0
    return sum(1 for record in records if str(record.status or "").lower() in statuses)


def _policy_rollback_count(runtime: Any, *, scope: ScopeRef, limit: int) -> int:
    getter = getattr(runtime, "get_policy_rollout_ledger", None)
    if not callable(getter):
        return 0
    try:
        records = getter(scope=scope, limit=max(0, int(limit)))
    except Exception:
        return 0
    return sum(1 for record in records if isinstance(record, dict) and is_executed_rollback_ledger_record(record))


def _verified_replay_summary(
    runtime: Any,
    *,
    scope: ScopeRef,
    limit: int,
    capabilities: set[str],
    missing_field: str,
    release: ReleaseIdentity | None,
    minimums: Mapping[str, Mapping[str, Any]] | None = None,
    catalog: CapabilityEvaluationCatalog | None = None,
    legacy_compatibility: bool = False,
) -> dict[str, Any]:
    records = _capability_replay_records(runtime, scope=scope, limit=limit)
    selected_records, manifest_record_ids, manifest_rejection_reasons = _latest_manifest_case_records(
        runtime,
        scope=scope,
        limit=limit,
        capabilities=capabilities,
        release=release,
        legacy_compatibility=legacy_compatibility,
    )
    by_capability = {
        capability: {
            "executed_count": 0,
            "pass_count": 0,
            "fail_count": 0,
            "not_run_count": 0,
            "pass_rate": 0.0,
            "distinct_evidence_count": 0,
        }
        for capability in sorted(capabilities)
    }
    evidence_sources = {capability: set() for capability in capabilities}
    pass_count = 0
    fail_count = 0
    not_run_count = 0
    observed_executed_count = sum(
        1
        for record in records
        if str(record.get("source", "") if isinstance(record, dict) else getattr(record, "source", "") or "").strip()
        == "eimemory.capability_replay"
        and str(_record_field(record, "report_type") or "").strip() == "capability_replay_pack"
        and str(_record_field(record, "capability") or "").strip() in capabilities
        and str(_record_field(record, "verdict") or "").strip().lower() in {"pass", "fail"}
    )
    rejection_reasons: dict[str, int] = {}
    for record in selected_records:
        content = record.get("content") if isinstance(record, dict) else getattr(record, "content", None)
        content = content if isinstance(content, dict) else {}
        persisted_result = content.get("result") if isinstance(content.get("result"), dict) else {}
        case_payload = content.get("case") if isinstance(content.get("case"), dict) else {}
        verdict = str(persisted_result.get("verdict") or _record_field(record, "verdict") or "").strip().lower()
        capability = str(_record_field(record, "capability") or _record_field(record, "target_capability") or "").strip()
        case_id = str(case_payload.get("case_id") or _record_field(record, "case_id") or "").strip()
        report_type = str(_record_field(record, "report_type") or "").strip()
        source = str(record.get("source", "") if isinstance(record, dict) else getattr(record, "source", "") or "").strip()
        hit = persisted_result.get("hit") if "hit" in persisted_result else _record_field(record, "hit")
        trusted_replay = report_type == "capability_replay_pack" and source == "eimemory.capability_replay"
        if not trusted_replay:
            continue
        bucket = by_capability.get(capability)
        if bucket is None:
            continue
        evidence_source_id = str(persisted_result.get("evidence_source_id") or "").strip()
        if verdict == "pass":
            if catalog is None:
                validation = {"ok": False, "reason": "evaluation_catalog_required"}
            else:
                validation = validate_capability_replay_result(
                    runtime,
                    scope=scope,
                    capability=capability,
                    case_id=case_id,
                    result=persisted_result,
                    catalog=catalog,
                    legacy_compatibility=legacy_compatibility,
                )
            if validation.get("ok") is not True:
                verdict = "fail"
                reason = str(validation.get("reason") or "invalid_contract_replay_result")
                rejection_reasons[reason] = rejection_reasons.get(reason, 0) + 1
        if verdict == "pass":
            pass_count += 1
            bucket["executed_count"] += 1
            bucket["pass_count"] += 1
            evidence_sources[capability].add(evidence_source_id)
        elif verdict == "fail":
            fail_count += 1
            bucket["executed_count"] += 1
            bucket["fail_count"] += 1
        elif verdict == "not_run":
            not_run_count += 1
            bucket["not_run_count"] += 1
    for bucket in by_capability.values():
        executed = int(bucket["executed_count"])
        bucket["pass_rate"] = round(int(bucket["pass_count"]) / executed, 3) if executed else 0.0
    for capability, bucket in by_capability.items():
        bucket["distinct_evidence_count"] = len(evidence_sources[capability])
    executed_count = pass_count + fail_count
    resolved_minimums = {
        capability: dict((minimums or {}).get(capability) or {})
        for capability in capabilities
    }
    capabilities_missing = []
    for capability, bucket in by_capability.items():
        configured = resolved_minimums.get(capability) or {}
        minimum_executed = _bounded_replay_minimum(configured.get("minimum_executed"), default=3)
        minimum_distinct = _bounded_replay_minimum(configured.get("minimum_distinct_evidence"), default=3)
        minimum_pass_rate = _bounded_pass_rate(configured.get("minimum_pass_rate"), default=0.8)
        bucket["minimum_executed"] = minimum_executed
        bucket["minimum_distinct_evidence"] = minimum_distinct
        bucket["minimum_pass_rate"] = minimum_pass_rate
        if (
            int(bucket["executed_count"]) < minimum_executed
            or float(bucket["pass_rate"]) < minimum_pass_rate
            or int(bucket["distinct_evidence_count"]) < minimum_distinct
        ):
            capabilities_missing.append(capability)
    return {
        "observed_executed_count": observed_executed_count,
        "executed_count": executed_count,
        "pass_count": pass_count,
        "fail_count": fail_count,
        "not_run_count": not_run_count,
        "pass_rate": round(pass_count / executed_count, 3) if executed_count else 0.0,
        "minimum_executed": sum(
            _bounded_replay_minimum((resolved_minimums.get(capability) or {}).get("minimum_executed"), default=3)
            for capability in capabilities
        ),
        "minimum_pass_rate": 0.8,
        "minimum_per_capability": 3 if not minimums else None,
        "minimums_by_capability": resolved_minimums,
        "by_capability": by_capability,
        missing_field: capabilities_missing,
        "rejection_reasons": dict(sorted(rejection_reasons.items())),
        "manifest_record_ids": manifest_record_ids,
        "manifest_rejection_reasons": manifest_rejection_reasons,
        "evaluation_catalog": {
            "status": "resolved" if catalog is not None else "blocked",
            "reason": "" if catalog is not None else "evaluation_catalog_required",
            "legacy_compatibility": bool(legacy_compatibility),
        },
    }


def _bounded_replay_minimum(value: Any, *, default: int) -> int:
    if isinstance(value, bool):
        return default
    try:
        numeric = int(value)
    except (TypeError, ValueError):
        return default
    return max(1, min(numeric, 10_000))


def _bounded_pass_rate(value: Any, *, default: float) -> float:
    if isinstance(value, bool):
        return default
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return default
    return max(0.0, min(numeric, 1.0))


def _capability_replay_records(runtime: Any, *, scope: ScopeRef, limit: int) -> list[Any]:
    budget = max(1, int(limit))
    lookup = getattr(runtime.store, "list_records_by_meta_value", None)
    if callable(lookup):
        try:
            records = lookup(
                kinds=["replay_result"],
                scope=scope,
                meta_key="report_type",
                meta_value="capability_replay_pack",
                limit=budget,
            )
            if records is not None:
                return [record for record in records if _record_has_exact_scope(record, scope)]
        except Exception:
            pass
    try:
        return [
            record
            for record in runtime.store.list_records(kinds=["replay_result"], scope=scope, limit=budget)
            if _record_has_exact_scope(record, scope)
        ]
    except Exception:
        return []


def _latest_manifest_case_records(
    runtime: Any,
    *,
    scope: ScopeRef,
    limit: int,
    capabilities: set[str],
    release: ReleaseIdentity | None,
    legacy_compatibility: bool = False,
) -> tuple[list[Any], dict[str, str], dict[str, str]]:
    high_water = _latest_manifest_high_water(
        runtime,
        scope=scope,
        limit=limit,
        capabilities=capabilities,
    )
    log_state = capability_replay_log_sequence_state(runtime, scope=scope, capabilities=capabilities)
    latest: dict[str, tuple[int, Any]] = {}
    for manifest in _capability_replay_manifest_records(runtime, scope=scope, limit=limit):
        content = manifest.get("content") if isinstance(manifest, dict) else getattr(manifest, "content", None)
        content = content if isinstance(content, dict) else {}
        manifest_capabilities = content.get("capabilities") if isinstance(content.get("capabilities"), list) else []
        sequences = content.get("sequence_by_capability") if isinstance(content.get("sequence_by_capability"), dict) else {}
        for capability in {str(value or "").strip() for value in manifest_capabilities} & capabilities:
            try:
                sequence = int(sequences.get(capability) or 0)
            except (TypeError, ValueError):
                sequence = 0
            current = latest.get(capability)
            if current is None or sequence > current[0]:
                latest[capability] = (sequence, manifest)

    selected: list[Any] = []
    manifest_record_ids: dict[str, str] = {}
    rejection_reasons: dict[str, str] = {}
    for capability in sorted(capabilities):
        entry = latest.get(capability)
        if entry is None:
            rejection_reasons[capability] = "manifest_missing"
            continue
        manifest = entry[1]
        manifest_record_ids[capability] = _record_id(manifest)
        capability_log_state = log_state.get(capability) or {}
        log_manifest_ids = set(capability_log_state.get("manifest_record_ids") or set())
        if len(log_manifest_ids) > 1:
            rejection_reasons[capability] = "manifest_sequence_collision"
            continue
        if int(capability_log_state.get("sequence") or 0) != int(entry[0] or 0):
            rejection_reasons[capability] = "manifest_log_high_water_mismatch"
            continue
        high_water_entry = high_water.get(capability) or {}
        expected_manifest_id = str(high_water_entry.get("manifest_record_id") or "")
        if not expected_manifest_id:
            rejection_reasons[capability] = "manifest_high_water_missing"
            continue
        if str(high_water_entry.get("status") or "") != "active" or str(high_water_entry.get("source") or "") != "eimemory.autonomous_learning":
            rejection_reasons[capability] = "manifest_high_water_status_invalid"
            continue
        if expected_manifest_id != _record_id(manifest):
            rejection_reasons[capability] = "manifest_high_water_mismatch"
            continue
        if int(high_water_entry.get("manifest_sequence") or 0) != int(entry[0] or 0):
            rejection_reasons[capability] = "manifest_high_water_sequence_mismatch"
            continue
        manifest_content = manifest.content if isinstance(manifest.content, dict) else {}
        if str(high_water_entry.get("execution_id") or "") != str(manifest_content.get("execution_id") or ""):
            rejection_reasons[capability] = "manifest_high_water_execution_mismatch"
            continue
        records, reason = _validated_manifest_members(
            runtime,
            manifest,
            scope=scope,
            capability=capability,
            release=release,
            legacy_compatibility=legacy_compatibility,
        )
        if reason:
            rejection_reasons[capability] = reason
            continue
        selected.extend(records)
    return selected, manifest_record_ids, rejection_reasons


def _latest_manifest_high_water(
    runtime: Any,
    *,
    scope: ScopeRef,
    limit: int,
    capabilities: set[str],
) -> dict[str, dict[str, Any]]:
    try:
        compact_loader = getattr(runtime.store, "list_capability_scores_compact", None)
        if callable(compact_loader):
            records = compact_loader(scope=scope, limit=max(1, int(limit)))
        else:
            records = runtime.store.list_records(
                kinds=["capability_score"],
                scope=scope,
                limit=max(1, int(limit)),
            )
    except Exception:
        return {}
    latest: dict[str, tuple[int, dict[str, Any]]] = {}
    for record in records:
        if not _record_has_exact_scope(record, scope):
            continue
        if str(record.meta.get("kind") or "") != "capability_replay_pack":
            continue
        capability = str(record.meta.get("capability") or record.content.get("capability") or "").strip()
        if capability not in capabilities:
            continue
        manifest_id = str(record.meta.get("manifest_record_id") or "").strip()
        try:
            score_sequence = int(record.meta.get("score_sequence") or record.content.get("score_sequence") or 0)
        except (TypeError, ValueError):
            score_sequence = 0
        try:
            manifest_sequence = int(record.meta.get("manifest_sequence") or 0)
        except (TypeError, ValueError):
            manifest_sequence = 0
        payload = {
            "manifest_record_id": manifest_id,
            "manifest_sequence": manifest_sequence,
            "execution_id": str(record.meta.get("replay_execution_id") or ""),
            "score_record_id": record.record_id,
            "score_sequence": score_sequence,
            "status": str(record.status or ""),
            "source": str(record.source or ""),
        }
        current = latest.get(capability)
        if current is None or score_sequence > current[0]:
            latest[capability] = (score_sequence, payload)
    return {capability: value[1] for capability, value in latest.items()}


def _capability_replay_manifest_records(runtime: Any, *, scope: ScopeRef, limit: int) -> list[Any]:
    budget = max(1, int(limit))
    lookup = getattr(runtime.store, "list_records_by_meta_value", None)
    if callable(lookup):
        try:
            records = lookup(
                kinds=["replay_result"],
                scope=scope,
                meta_key="report_type",
                meta_value=MANIFEST_REPORT_TYPE,
                limit=budget,
            )
            if records is not None:
                return [record for record in records if _record_has_exact_scope(record, scope)]
        except Exception:
            pass
    try:
        return [
            record
            for record in runtime.store.list_records(kinds=["replay_result"], scope=scope, limit=budget)
            if _record_has_exact_scope(record, scope)
            and str(_record_field(record, "report_type") or "") == MANIFEST_REPORT_TYPE
        ]
    except Exception:
        return []


def _manifest_selection_case_contract(
    *,
    content: Mapping[str, Any],
    meta: Mapping[str, Any],
    provenance: Mapping[str, Any],
    scope: ScopeRef,
    capability: str,
    legacy_compatibility: bool,
) -> tuple[list[str], dict[str, dict[str, str]], str]:
    """Validate the Profile/catalog contract frozen into a replay manifest.

    Old manifests are accepted only as explicitly legacy-shaped evidence.  New
    dynamic manifests must carry their exact Profile resolution, case targets,
    and thresholds so a release cannot downgrade the Profile requirement after
    the replay was recorded.
    """

    selection = content.get("selection_contract")
    if not isinstance(selection, Mapping) or not selection:
        # Historical manifests predate the selection contract and are only
        # useful to the explicit legacy compatibility reader.
        if not legacy_compatibility:
            return [], {}, "manifest_selection_contract_missing"
        return capability_replay_case_ids(capability, legacy_compatibility=True), {}, ""
    normalized = dict(selection)
    selection_digest = str(normalized.get("selection_contract_digest") or "").strip()
    if (
        str(normalized.get("schema") or "") != SELECTION_CONTRACT_SCHEMA
        or not selection_digest
        or replay_selection_contract_digest(normalized) != selection_digest
    ):
        return [], {}, "manifest_selection_contract_invalid"
    for container in (meta, provenance):
        if (
            str(container.get("selection_contract_schema") or "") != SELECTION_CONTRACT_SCHEMA
            or str(container.get("selection_contract_digest") or "") != selection_digest
            or str(container.get("selection_mode") or "") != str(normalized.get("mode") or "")
            or str(container.get("profile_key") or "") != str(normalized.get("profile_key") or "")
            or str(container.get("profile_id") or "") != str(normalized.get("profile_id") or "")
            or str(container.get("capability_scope") or "") != str(normalized.get("capability_scope") or "")
        ):
            return [], {}, "manifest_selection_contract_envelope_mismatch"
    runtime_scope = normalized.get("runtime_scope")
    if not isinstance(runtime_scope, Mapping) or dict(runtime_scope) != asdict(scope):
        return [], {}, "manifest_selection_scope_mismatch"
    capabilities = normalized.get("capabilities")
    if not isinstance(capabilities, list) or len({str(item or "").strip() for item in capabilities}) != len(capabilities):
        return [], {}, "manifest_selection_capabilities_invalid"
    if capability not in {str(item or "").strip() for item in capabilities}:
        return [], {}, "manifest_selection_capability_missing"
    expected_map = normalized.get("expected_case_ids")
    if not isinstance(expected_map, Mapping):
        return [], {}, "manifest_selection_expected_cases_missing"
    expected = [str(value or "").strip() for value in expected_map.get(capability) or []]
    if not expected or any(not value for value in expected) or len(set(expected)) != len(expected):
        return [], {}, "manifest_selection_expected_cases_invalid"
    mode = str(normalized.get("mode") or "")
    targets = normalized.get("case_targets")
    target_by_case: dict[str, dict[str, str]] = {}
    if mode == "legacy_compatibility":
        canonical_legacy = capability_replay_case_ids(capability, legacy_compatibility=True)
        if expected != canonical_legacy:
            return [], {}, "manifest_selection_legacy_cases_mismatch"
        return expected, target_by_case, ""
    if mode not in {"dynamic_profile", "dynamic_registry"}:
        return [], {}, "manifest_selection_mode_invalid"
    if mode == "dynamic_profile" and any(
        not str(normalized.get(field) or "").strip()
        for field in ("profile_key", "profile_id", "profile_digest", "resolution_digest")
    ):
        return [], {}, "manifest_selection_profile_identity_missing"
    if not isinstance(targets, Mapping) or not isinstance(targets.get(capability), list):
        return [], {}, "manifest_selection_targets_missing"
    for raw in targets.get(capability) or []:
        if not isinstance(raw, Mapping):
            return [], {}, "manifest_selection_target_invalid"
        row = {field: str(raw.get(field) or "").strip() for field in (
            "case_id",
            "capability_revision_id",
            "provider_binding_id",
            "eval_spec_id",
            "evaluation_case_digest",
        )}
        if any(not value for value in row.values()) or row["case_id"] in target_by_case:
            return [], {}, "manifest_selection_target_invalid"
        target_by_case[row["case_id"]] = row
    if list(sorted(target_by_case)) != list(expected):
        return [], {}, "manifest_selection_target_cases_mismatch"
    minimums = normalized.get("minimums_by_capability")
    if not isinstance(minimums, Mapping) or not isinstance(minimums.get(capability), Mapping):
        return [], {}, "manifest_selection_minimums_missing"
    return expected, target_by_case, ""


def _validated_manifest_members(
    runtime: Any,
    manifest: Any,
    *,
    scope: ScopeRef,
    capability: str,
    release: ReleaseIdentity | None,
    legacy_compatibility: bool,
) -> tuple[list[Any], str]:
    content = manifest.get("content") if isinstance(manifest, dict) else getattr(manifest, "content", None)
    content = content if isinstance(content, dict) else {}
    meta = manifest.get("meta") if isinstance(manifest, dict) else getattr(manifest, "meta", None)
    meta = meta if isinstance(meta, dict) else {}
    provenance = manifest.get("provenance") if isinstance(manifest, dict) else getattr(manifest, "provenance", None)
    provenance = provenance if isinstance(provenance, dict) else {}
    source = str(manifest.get("source", "") if isinstance(manifest, dict) else getattr(manifest, "source", "") or "")
    status = str(manifest.get("status", "") if isinstance(manifest, dict) else getattr(manifest, "status", "") or "")
    execution_id = str(content.get("execution_id") or "").strip()
    digest = str(content.get("manifest_digest") or "").strip()
    selection_contract = content.get("selection_contract") if isinstance(content.get("selection_contract"), Mapping) else {}
    if not _record_has_exact_scope(manifest, scope):
        return [], "manifest_scope_mismatch"
    if source != "eimemory.capability_replay":
        return [], "manifest_source_untrusted"
    if status != "active":
        return [], "manifest_status_invalid"
    if any(
        str(container.get("report_type") or "") != MANIFEST_REPORT_TYPE
        or str(container.get("schema_version") or "") != MANIFEST_SCHEMA_VERSION
        for container in (content, meta, provenance)
    ):
        return [], "manifest_schema_mismatch"
    if not execution_id or any(str(container.get("execution_id") or "").strip() != execution_id for container in (meta, provenance)):
        return [], "manifest_execution_id_missing_or_mismatched"
    if not digest or any(str(container.get("manifest_digest") or "").strip() != digest for container in (meta, provenance)):
        return [], "manifest_digest_mismatch"
    if capability_replay_manifest_digest(content) != digest:
        return [], "manifest_digest_mismatch"
    canonical_case_ids, expected_targets, selection_error = _manifest_selection_case_contract(
        content=content,
        meta=meta,
        provenance=provenance,
        scope=scope,
        capability=capability,
        legacy_compatibility=legacy_compatibility,
    )
    if selection_error:
        return [], selection_error
    if any(container.get("complete") is not True for container in (content, meta, provenance)):
        return [], "manifest_incomplete"
    if release is None:
        return [], "manifest_release_identity_mismatch"
    if any(
        not same_release_authority(
            release_identity_from_record(container),
            release,
        )
        for container in (content, meta, provenance)
    ):
        return [], "manifest_release_identity_mismatch"

    manifest_started = _record_created_at(manifest)
    manifest_finished = _record_updated_at(manifest)
    executed_at = _parse_timestamp(content.get("executed_at"))
    if manifest_started is None or manifest_finished is None:
        return [], "manifest_record_time_invalid"
    now = datetime.now(timezone.utc)
    if any(value > now + timedelta(minutes=5) for value in (manifest_started, manifest_finished)):
        return [], "manifest_time_in_future"
    if executed_at is None or executed_at > now + timedelta(minutes=5):
        return [], "manifest_time_in_future" if executed_at is not None else "manifest_time_invalid"
    if executed_at < manifest_started - timedelta(minutes=1) or executed_at > manifest_finished + timedelta(minutes=1):
        return [], "manifest_time_invalid"
    expected_map = content.get("expected_case_ids") if isinstance(content.get("expected_case_ids"), dict) else {}
    member_map = content.get("member_record_ids") if isinstance(content.get("member_record_ids"), dict) else {}
    member_digest_map = content.get("member_digests") if isinstance(content.get("member_digests"), dict) else {}
    expected_case_ids = [str(value or "").strip() for value in expected_map.get(capability) or []]
    member_ids = [str(value or "").strip() for value in member_map.get(capability) or []]
    member_digests = member_digest_map.get(capability) if isinstance(member_digest_map.get(capability), dict) else {}
    if expected_case_ids != canonical_case_ids:
        return [], "manifest_expected_cases_mismatch"
    if len(member_ids) != len(canonical_case_ids) or len(set(member_ids)) != len(member_ids):
        return [], "manifest_member_count_mismatch"
    manifest_evidence = [str(value or "").strip() for value in (manifest.get("evidence", []) if isinstance(manifest, dict) else getattr(manifest, "evidence", []) or [])]
    all_member_ids = [
        str(value or "").strip()
        for values in member_map.values()
        if isinstance(values, list)
        for value in values
    ]
    if sorted(manifest_evidence) != sorted(all_member_ids):
        return [], "manifest_evidence_members_mismatch"

    records: list[Any] = []
    seen_case_ids: set[str] = set()
    seen_probe_ids: set[str] = set()
    for member_id in member_ids:
        record = runtime.store.get_by_id(member_id, scope=scope)
        if record is None:
            return [], "manifest_member_missing"
        if not _record_has_exact_scope(record, scope):
            return [], "manifest_member_scope_mismatch"
        if str(member_digests.get(member_id) or "") != capability_replay_member_digest(record):
            return [], "manifest_member_digest_mismatch"
        record_content = record.content if isinstance(record.content, dict) else {}
        result = record_content.get("result") if isinstance(record_content.get("result"), dict) else {}
        verdict = str(result.get("verdict") or record_content.get("verdict") or "").strip().lower()
        case_payload = record_content.get("case") if isinstance(record_content.get("case"), dict) else {}
        case_id = str(case_payload.get("case_id") or record.meta.get("case_id") or "").strip()
        probe_id = str(result.get("probe_source_id") or "").strip()
        expected_target = expected_targets.get(case_id)
        expected_profile = (
            {
                field: str(selection_contract.get(field) or "")
                for field in ("profile_key", "profile_id", "profile_digest", "selection_contract_digest")
            }
            if expected_targets
            else {}
        )
        if (
            record.source != "eimemory.capability_replay"
            or record.kind != "replay_result"
            or record.status != "active"
            or str(record.meta.get("report_type") or "") != "capability_replay_pack"
            or str(record.meta.get("execution_id") or "").strip() != execution_id
            or str(record_content.get("execution_id") or "").strip() != execution_id
            or str(record.meta.get("executed_at") or "") != str(content.get("executed_at") or "")
            or str(record_content.get("executed_at") or "") != str(content.get("executed_at") or "")
            or str(record.meta.get("capability") or "") != capability
            or str(record_content.get("capability") or "") != capability
            or case_id not in canonical_case_ids
            or (
                expected_target is not None
                and any(
                    str(record_content.get(field) or record.meta.get(field) or "") != expected_target[field]
                    for field in (
                        "capability_revision_id",
                        "provider_binding_id",
                        "eval_spec_id",
                        "evaluation_case_digest",
                    )
                )
            )
            or (
                expected_profile
                and any(
                    str(record_content.get(field) or record.meta.get(field) or "") != value
                    for field, value in expected_profile.items()
                )
            )
        ):
            return [], "manifest_member_binding_mismatch"
        member_created = _record_created_at(record)
        if member_created is None:
            return [], "manifest_member_time_invalid"
        if member_created > now + timedelta(minutes=5):
            return [], "manifest_time_in_future"
        if member_created < manifest_started - timedelta(seconds=5) or member_created > manifest_finished + timedelta(seconds=5):
            return [], "manifest_member_time_invalid"
        if case_id in seen_case_ids:
            return [], "manifest_duplicate_case_id"
        seen_case_ids.add(case_id)
        if verdict == "pass":
            if not probe_id or probe_id in seen_probe_ids:
                return [], "manifest_probe_binding_invalid"
            seen_probe_ids.add(probe_id)
            probe = runtime.store.get_by_id(probe_id, scope=scope)
            if probe is None:
                return [], "manifest_probe_missing"
            if not _record_has_exact_scope(probe, scope):
                return [], "manifest_probe_scope_mismatch"
            if probe.kind != "replay_result" or probe.status != "active":
                return [], "manifest_probe_status_invalid"
            trace_record_id = str(result.get("trace_record_id") or "").strip()
            trace = runtime.store.get_by_id(trace_record_id, scope=scope)
            if trace is None or trace.kind != "reflection" or trace.status != "active":
                return [], "manifest_trace_status_invalid"
            if not _record_has_exact_scope(trace, scope):
                return [], "manifest_trace_scope_mismatch"
            probe_created = _record_created_at(probe)
            if probe_created is None:
                return [], "manifest_probe_time_invalid"
            if probe_created > now + timedelta(minutes=5):
                return [], "manifest_time_in_future"
            if probe_created < manifest_started - timedelta(minutes=15) or probe_created > manifest_finished + timedelta(seconds=5):
                return [], "manifest_probe_not_fresh"
        records.append(record)
    if seen_case_ids != set(canonical_case_ids):
        return [], "manifest_case_coverage_incomplete"
    return records, ""


def _record_id(record: Any) -> str:
    return str(record.get("record_id", "") if isinstance(record, dict) else getattr(record, "record_id", "") or "")


def _record_has_exact_scope(record: Any, scope: ScopeRef) -> bool:
    record_scope = record.get("scope") if isinstance(record, dict) else getattr(record, "scope", None)
    return same_scope(record_scope, scope)


def _record_created_at(record: Any) -> datetime | None:
    time_ref = record.get("time") if isinstance(record, dict) else getattr(record, "time", None)
    value = time_ref.get("created_at") if isinstance(time_ref, dict) else getattr(time_ref, "created_at", "")
    return _parse_timestamp(value)


def _record_updated_at(record: Any) -> datetime | None:
    time_ref = record.get("time") if isinstance(record, dict) else getattr(record, "time", None)
    value = time_ref.get("updated_at") if isinstance(time_ref, dict) else getattr(time_ref, "updated_at", "")
    return _parse_timestamp(value) or _record_created_at(record)


def _parse_timestamp(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def _latest_l5_assessment(
    runtime: Any,
    *,
    scope: ScopeRef,
    release: ReleaseIdentity | None = None,
) -> dict[str, Any]:
    current_release = release or current_release_identity(runtime, scope)
    records: list[Any] = []
    sqlite = getattr(getattr(runtime, "store", None), "sqlite", None)
    conn = getattr(sqlite, "conn", None)
    if conn is not None:
        try:
            rows = conn.execute(
                """
                SELECT record_id
                FROM records
                WHERE kind = 'l5_assessment'
                  AND source = 'eimemory.l5_loop'
                  AND tenant_id = ? AND agent_id = ? AND workspace_id = ? AND user_id = ?
                ORDER BY rowid DESC
                LIMIT 500
                """,
                (scope.tenant_id, scope.agent_id, scope.workspace_id, scope.user_id),
            ).fetchall()
            records = [runtime.store.get_by_id(str(row[0]), scope=scope) for row in rows]
            records = [record for record in records if record is not None]
        except Exception:
            records = []
    if not records:
        try:
            offset = 0
            while True:
                page = runtime.store.list_records(
                    kinds=["l5_assessment"],
                    scope=scope,
                    limit=100,
                    offset=offset,
                )
                records.extend(page)
                if len(page) < 100:
                    break
                offset += len(page)
        except Exception:
            records = []
    records = [record for record in records if _record_has_exact_scope(record, scope)]
    if not records:
        return {"present": False, "trusted": False, "complete": False, "level": "", "missing_evidence": [], "record_id": ""}
    if current_release is None:
        record = _global_l5_readiness_record(records)
        return {
            "present": True,
            "trusted": False,
            "complete": False,
            "assessment_id": str(_record_field(record, "assessment_id") or ""),
            "level": str(_record_field(record, "level") or ""),
            "missing_evidence": ["release_identity:unavailable"],
            "record_id": str(getattr(record, "record_id", "") or ""),
        }
    requirement = EvidenceRequirement(
        kinds=frozenset({"l5_assessment"}),
        sources=frozenset({"eimemory.l5_loop"}),
        statuses=frozenset({"active", "candidate"}),
        evidence_classes=frozenset({"structural"}),
    )
    release_records = [
        record
        for record in records
        if resolve_evidence(runtime, str(record.record_id or ""), requirement, scope, current_release).ok
    ]
    if not release_records:
        record = _global_l5_readiness_record(records)
        return {
            "present": True,
            "trusted": False,
            "complete": False,
            "assessment_id": str(_record_field(record, "assessment_id") or ""),
            "level": str(_record_field(record, "level") or ""),
            "missing_evidence": ["assessment:release_mismatch"],
            "record_id": str(getattr(record, "record_id", "") or ""),
        }
    record = _global_l5_readiness_record(release_records)
    missing = _record_field(record, "missing_evidence")
    missing_evidence = [str(item) for item in missing] if isinstance(missing, list) else []
    level = str(_record_field(record, "level") or "")
    source = str(record.get("source", "") if isinstance(record, dict) else getattr(record, "source", "") or "")
    trusted = (
        source == "eimemory.l5_loop"
        and str(_record_field(record, "report_type") or "") == "l5_assessment"
        and str(_record_field(record, "schema_version") or "") == "l5_closed_loop.v1"
    )
    complete = trusted and bool(_record_field(record, "complete")) and level == "L5" and not missing_evidence
    return {
        "present": True,
        "trusted": trusted,
        "complete": complete,
        "assessment_id": str(_record_field(record, "assessment_id") or ""),
        "level": level,
        "missing_evidence": missing_evidence,
        "record_id": str(getattr(record, "record_id", "") or ""),
    }


def _global_l5_readiness_record(records: list[Any]) -> Any:
    latest = records[0]
    if str(_record_field(latest, "activity_status") or "").strip().lower() not in {"idle", "no_change"}:
        return latest
    for record in records[1:]:
        if str(_record_field(record, "activity_status") or "").strip().lower() not in {"idle", "no_change"}:
            return record
    return latest


def _record_field(record: Any, key: str) -> Any:
    if isinstance(record, dict):
        if key in record:
            return record.get(key)
        payloads = (record.get("content"), record.get("meta"))
    else:
        payloads = (getattr(record, "content", None), getattr(record, "meta", None))
    for payload in payloads:
        if isinstance(payload, dict) and key in payload:
            return payload.get(key)
    return None


def _capability_gaps(
    ledger: dict[str, Any],
    *,
    outcome_evidence: dict[str, Any],
    capability_selection: Mapping[str, Any],
) -> list[dict[str, Any]]:
    capabilities = dict(ledger.get("capabilities") or {})
    outcome_counts = (
        outcome_evidence.get("counts")
        if isinstance(outcome_evidence.get("counts"), dict)
        else {}
    )
    selection_reason = str(capability_selection.get("reason") or "")
    entries = [item for item in capability_selection.get("capabilities") or [] if isinstance(item, Mapping)]
    if not entries:
        return [
            {
                "capability": "",
                "score": 0.0,
                "evidence_count": 0,
                "outcome_evidence_count": 0,
                "reason": selection_reason or "dynamic_capability_selection_empty",
                "priority": "high",
            }
        ]
    gaps: list[dict[str, Any]] = []
    for entry in entries:
        name = str(entry.get("capability_id") or "").strip()
        if not name:
            continue
        item = dict(capabilities.get(name) or {})
        score = float(item.get("score") or 0.0)
        evidence_count = int(item.get("evidence_count") or 0)
        # Ledger source labels are self-described metadata.  Only the
        # contract-verified evidence projection can satisfy an outcome
        # requirement; otherwise a legacy score could fabricate L5 evidence.
        outcome_count = max(0, int(outcome_counts.get(name) or 0))
        minimum_evidence = _selection_requirement(entry, "min_evidence_count", 3)
        minimum_outcomes = _selection_outcome_minimum(entry)
        if minimum_outcomes and outcome_count < minimum_outcomes:
            gaps.append(
                {
                    "capability": name,
                    "score": round(score, 3),
                    "evidence_count": evidence_count,
                    "outcome_evidence_count": outcome_count,
                    "reason": "insufficient_attributed_outcome_evidence",
                    "priority": _selection_priority(entry),
                    "minimum_evidence_count": minimum_evidence,
                    "minimum_outcome_evidence_count": minimum_outcomes,
                }
            )
            continue
        if score >= 0.7 and evidence_count >= minimum_evidence:
            continue
        gaps.append(
            {
                "capability": name,
                "score": round(score, 3),
                "evidence_count": evidence_count,
                "outcome_evidence_count": outcome_count,
                "reason": str(item.get("goal_gap_reason") or item.get("status") or "insufficient_evidence"),
                "priority": _selection_priority(entry),
                "minimum_evidence_count": minimum_evidence,
                "minimum_outcome_evidence_count": minimum_outcomes,
            }
        )
    return gaps


def _stage_for(
    ledger: dict[str, Any],
    hard_metrics: dict[str, Any],
    evidence_counts: dict[str, int],
    capability_gaps: list[dict[str, Any]],
    outcome_evidence: dict[str, Any],
    verified_replay: dict[str, Any],
    verified_core_replay: dict[str, Any],
    latest_l5_assessment: dict[str, Any],
    verified_real_replay: dict[str, Any] | None = None,
    *,
    capability_selection: Mapping[str, Any],
    legacy_compatibility: bool,
) -> dict[str, Any]:
    metrics = dict(hard_metrics.get("metrics") or {})
    metric_quality = dict(hard_metrics.get("metric_quality") or {})
    replay_count = int(verified_replay.get("executed_count") or 0)
    observed_replay_count = int(verified_replay.get("observed_executed_count") or replay_count)
    replay_pass_rate = float(verified_replay.get("pass_rate") or 0.0)
    replay_minimum = max(1, int(verified_replay.get("minimum_executed") or 1))
    readiness_replay_target = max(10, replay_minimum) if legacy_compatibility else replay_minimum
    l5_artifacts = sum(int(evidence_counts.get(kind) or 0) for kind in ("l5_world_model", "l5_strategic_roadmap", "l5_assessment", "l5_closed_loop"))
    promotion_count = int(evidence_counts.get("promotion_applied") or 0)
    rollback_count = int(evidence_counts.get("rollback_or_quarantine") or 0)
    selected_capabilities = {
        str(value or "").strip()
        for value in capability_selection.get("capability_ids") or []
        if str(value or "").strip()
    }
    selected_count = len(selected_capabilities)
    weak_gap_count = len(capability_gaps)
    maturity_ready_count = _ready_count(
        ledger,
        LEGACY_STRONG_CAPABILITIES if legacy_compatibility else selected_capabilities,
        capability_selection=capability_selection,
    )
    core_ready_count = _ready_count(
        ledger,
        set(LEGACY_READINESS_CAPABILITIES) if legacy_compatibility else selected_capabilities,
        capability_selection=capability_selection,
    )
    readiness_coverage = core_ready_count / max(selected_count, 1)
    gap_coverage = weak_gap_count / max(selected_count, 1)
    task_success = float(metrics.get("task_success_rate") or 0.0)
    recall_hit = float(metrics.get("recall_hit_rate") or 0.0)
    patch_success = float(metrics.get("patch_promotion_success_rate") or metrics.get("auto_patch_success_rate") or 0.0)
    patch_quality_ok = bool(
        (metric_quality.get("patch_promotion_success_rate") or metric_quality.get("auto_patch_success_rate") or {}).get("sufficient")
    )
    sample_counts = hard_metrics.get("sample_counts") if isinstance(hard_metrics.get("sample_counts"), dict) else {}
    real_task_evidence = (
        hard_metrics.get("real_task_evidence")
        if isinstance(hard_metrics.get("real_task_evidence"), dict)
        else {}
    )
    if real_task_evidence:
        verified_live_success = float(real_task_evidence.get("success_rate") or 0.0)
        verified_live_quality = {"sufficient": real_task_evidence.get("sufficient") is True}
        verified_live_samples = int(real_task_evidence.get("sample_count") or 0)
        verified_live_task_types = int(real_task_evidence.get("distinct_task_types") or 0)
    else:
        verified_live_success = float(metrics.get("current_deployment_verified_real_task_success_rate") or 0.0)
        verified_live_quality = metric_quality.get("current_deployment_verified_real_task_success_rate") or {}
        verified_live_samples = int(sample_counts.get("current_deployment_verified_real_tasks") or 0)
        verified_live_task_types = int(sample_counts.get("current_deployment_verified_real_task_types") or 0)
    operational_probes = int(sample_counts.get("current_deployment_operational_probes") or 0)
    live_task_gate = {
        "ok": bool(
            verified_live_quality.get("sufficient")
            and verified_live_success >= 0.8
            and verified_live_task_types >= 5
            and verified_live_samples >= 10
        ),
        "success_rate": verified_live_success,
        "sample_count": verified_live_samples,
        "minimum_samples": 10,
        "sample_deficit": max(0, 10 - verified_live_samples),
        "distinct_task_types": verified_live_task_types,
        "minimum_task_types": 5,
        "task_type_deficit": max(0, 5 - verified_live_task_types),
        "current_deployment_verified_real_tasks": verified_live_samples,
        "current_deployment_operational_probes": operational_probes,
        "evidence_mode": str(real_task_evidence.get("evidence_mode") or "current_release"),
        "evidence_release_commit": str(real_task_evidence.get("evidence_release_commit") or ""),
        "current_release_commit": str(real_task_evidence.get("current_release_commit") or ""),
    }
    real_business_gate = _real_business_gate(
        live_task_gate,
        verified_real_replay if isinstance(verified_real_replay, dict) else {},
    )
    outcome_evidence_ok = not outcome_evidence.get("missing")
    weak_missing = _replay_missing(verified_replay)
    weak_manifest_rejections = verified_replay.get("manifest_rejection_reasons")
    core_missing = _replay_missing(verified_core_replay)
    core_manifest_rejections = verified_core_replay.get("manifest_rejection_reasons")
    replay_gate_fields_valid = bool(
        _replay_missing_field_is_valid(verified_replay)
        and isinstance(weak_manifest_rejections, dict)
        and _replay_missing_field_is_valid(verified_core_replay)
        and isinstance(core_manifest_rejections, dict)
    )

    readiness_score = round(
        min(1.0, (readiness_coverage * 0.45) + (min(replay_count, readiness_replay_target) / readiness_replay_target * 0.2) + (min(l5_artifacts, 4) / 4 * 0.2) + (min(promotion_count, 5) / 5 * 0.15)),
        3,
    )
    if (
        capability_gaps
        or not outcome_evidence_ok
        or not patch_quality_ok
        or not replay_gate_fields_valid
        or weak_missing
        or weak_manifest_rejections
        or core_missing
        or core_manifest_rejections
        or not latest_l5_assessment.get("complete")
        or not real_business_gate["ok"]
    ):
        readiness_score = min(readiness_score, 0.8)
    common = {
        "readiness_score": readiness_score,
        "live_task_gate": live_task_gate,
        "real_business_gate": real_business_gate,
        "risk_boundary": "read-only reporting; no autonomous apply, deployment, external send, spend, deletion, or credential use.",
    }
    structural_ready = bool(
        l5_artifacts >= 4
        and not capability_gaps
        and outcome_evidence_ok
        and replay_gate_fields_valid
        and replay_count >= readiness_replay_target
        and replay_pass_rate >= 0.8
        and not weak_missing
        and not weak_manifest_rejections
        and int(verified_core_replay.get("executed_count") or 0) >= int(verified_core_replay.get("minimum_executed") or 0)
        and not core_missing
        and not core_manifest_rejections
        and latest_l5_assessment.get("complete") is True
        and promotion_count >= 1
        and rollback_count >= 1
        and patch_quality_ok
        and patch_success >= 0.8
    )
    if structural_ready and real_business_gate["ok"] and legacy_compatibility:
        return {
            **common,
            "readiness_score": 1.0,
            "stage": "L5",
            "label": "evidence-bound co-growth loop",
            "reason": "world model, roadmap, assessment, replay, promotion, rollback, and verified real-business evidence are all present.",
            "done_when": "Maintain zero missing L5 assessment evidence and keep either verified live-task or verified real-replay evidence above its threshold.",
        }
    if structural_ready:
        return {
            **common,
            "readiness_score": 0.8,
            "stage": "L4.5",
            "label": "dynamic evidence structure complete; v3 authority pending",
            "reason": (
                "The registry-selected evidence set is structurally complete, but the v2 reader cannot claim L5 "
                "for a dynamic capability cohort. Use the v3 projection and four-axis assessment."
                if not legacy_compatibility
                else "All structural and safety gates pass, but neither verified live tasks nor current-code replay of verified real tasks closes the business gate."
            ),
            "done_when": (
                "Run the v3 projection/assessment for this exact Profile and capability scope."
                if not legacy_compatibility
                else "Accumulate verified real tasks or replay at least ten unique real sources across five task types with pass rate >=0.8."
            ),
        }
    if l5_artifacts >= 2 and replay_count > 0 and (
        weak_gap_count <= 2 if legacy_compatibility else gap_coverage <= 0.5
    ):
        return {
            **common,
            "stage": "L4.5",
            "label": "self-growth reporting with registry-selected gaps closing",
            "reason": "L5 rehearsal artifacts exist, but one or more registry-selected evidence or production gates remain incomplete.",
            "done_when": "Complete replay, reversible promotion, and either verified live-task or verified real-replay business evidence.",
        }
    if (
        (maturity_ready_count >= 3 if legacy_compatibility else readiness_coverage > 0.0)
        and observed_replay_count > 0
        and (task_success > 0 or recall_hit > 0)
    ):
        return {
            **common,
            "stage": "L4",
            "label": "closed-loop learning with measurable outcomes",
            "reason": "registry-selected capabilities have ledger evidence and replay exists, but coverage is incomplete.",
            "done_when": "Autonomous cycles produce goal graph, replay dataset, promotion/block decision, and dashboard metrics every run.",
        }
    return {
        **common,
        "stage": "L3.5",
        "label": "early autonomous evolution with evidence gaps",
        "reason": "learning and candidate records may exist, but repeatable replay, L5 artifacts, and registry-selected capability evidence are not yet enough.",
        "done_when": "Add profile-backed replay packs and hard metrics for the active capability set without changing production behavior.",
    }


def _real_business_gate(
    live_task_gate: dict[str, Any],
    verified_real_replay: dict[str, Any],
) -> dict[str, Any]:
    live_sample_count = int(
        live_task_gate.get("current_deployment_verified_real_tasks")
        or live_task_gate.get("sample_count")
        or 0
    )
    live_ok = bool(
        live_task_gate.get("ok") is True
        and live_sample_count >= 10
    )
    replay_ok = bool(
        verified_real_replay.get("ok") is True
        and int(verified_real_replay.get("sample_count") or 0) >= 10
        and int(verified_real_replay.get("distinct_task_types") or 0) >= 5
        and float(verified_real_replay.get("pass_rate") or 0.0) >= 0.8
        and str(verified_real_replay.get("provenance_contract") or "")
        == "verified_real_replay.v1"
    )
    return {
        "ok": bool(live_ok or replay_ok),
        "accepted_path": "live_tasks" if live_ok else "real_replay" if replay_ok else "",
        "live_tasks": dict(live_task_gate),
        "real_replay": dict(verified_real_replay),
    }


def _release_validation(
    *,
    release: ReleaseIdentity | None,
    release_lineage: dict[str, Any],
    production_recall_gate: dict[str, Any],
    production_recall_strict_state: dict[str, Any],
    storage_migrations: dict[str, Any],
) -> dict[str, Any]:
    checks = {
        "release_identity": release is not None,
        "release_lineage": bool(
            release_lineage.get("ok") is True
            and release_lineage.get("validated") is True
            and release_lineage.get("compatible") is True
        ),
        "production_recall": bool(
            production_recall_gate.get("ok") is True
            and production_recall_gate.get("status") == "accepted"
        ),
        "production_recall_strict_state": bool(
            production_recall_strict_state.get("ok") is True
            and production_recall_strict_state.get("status") == "strict_activated"
        ),
        "storage_migrations": bool(
            storage_migrations.get("ok") is True
            and storage_migrations.get("pending") == []
        ),
    }
    return {
        "status": "verified" if all(checks.values()) else "unverified",
        "ok": all(checks.values()),
        "checks": checks,
    }


def _ready_count(
    ledger: dict[str, Any],
    capability_names: set[str],
    *,
    capability_selection: Mapping[str, Any],
) -> int:
    capabilities = dict(ledger.get("capabilities") or {})
    requirements = {
        str(entry.get("capability_id") or ""): entry
        for entry in capability_selection.get("capabilities") or []
        if isinstance(entry, Mapping)
    }
    total = 0
    for name in capability_names:
        item = dict(capabilities.get(name) or {})
        entry = requirements.get(name) or {}
        minimum_evidence = _selection_requirement(entry, "min_evidence_count", 3)
        if float(item.get("score") or 0.0) >= 0.7 and int(item.get("evidence_count") or 0) >= minimum_evidence:
            total += 1
    return total


def _capability_outcome_evidence(
    runtime: Any,
    *,
    scope: ScopeRef,
    limit: int,
    capability_selection: Mapping[str, Any],
    catalog: CapabilityEvaluationCatalog | None,
    legacy_compatibility: bool,
) -> dict[str, Any]:
    evidence_by_capability = collect_capability_evidence(
        runtime,
        scope=scope,
        limit=limit,
        catalog=catalog,
        legacy_compatibility=legacy_compatibility,
    )
    requirements = {
        str(entry.get("capability_id") or ""): entry
        for entry in capability_selection.get("capabilities") or []
        if isinstance(entry, Mapping) and str(entry.get("capability_id") or "").strip()
    }
    counts = {
        name: len(
            {
                str(item.get("source_id") or "")
                for item in evidence_by_capability.get(name, [])
                if item.get("contract_verified") is True and str(item.get("source_id") or "")
            }
        )
        for name in sorted(requirements)
    }
    return {
        "minimum_per_capability": None,
        "minimums_by_capability": {
            name: _selection_outcome_minimum(entry)
            for name, entry in requirements.items()
            if _selection_outcome_minimum(entry)
        },
        "counts": counts,
        "missing": [
            name
            for name, count in counts.items()
            if count < _selection_outcome_minimum(requirements[name])
        ],
    }


def _next_actions(
    stage: dict[str, Any],
    capability_gaps: list[dict[str, Any]],
    evidence_counts: dict[str, int],
    *,
    verified_replay: dict[str, Any],
    latest_l5_assessment: dict[str, Any],
    production_recall_gate: dict[str, Any],
    production_recall_strict_state: dict[str, Any],
    legacy_compatibility: bool,
) -> list[str]:
    actions = []
    if production_recall_gate.get("ok") is not True or production_recall_gate.get("status") != "accepted":
        reason = str(production_recall_gate.get("reason") or production_recall_gate.get("blocked_reason") or "not accepted")
        actions.append(
            f"Complete the production recall real-query gate ({reason}); L5 remains downgraded until independent verification accepts it."
        )
    if (
        production_recall_strict_state.get("ok") is not True
        or production_recall_strict_state.get("status") != "strict_activated"
    ):
        reason = str(production_recall_strict_state.get("reason") or "strict state missing")
        actions.append(
            f"Activate the accepted production recall gate for the current release ({reason}); L5 remains downgraded."
        )
    real_business_gate = (
        stage.get("real_business_gate")
        if isinstance(stage.get("real_business_gate"), dict)
        else {}
    )
    if not real_business_gate.get("ok"):
        actions.append(
            "Accumulate verified real user tasks or run a current-code replay of ten unique verified real sources across five task types with pass rate >=0.8; operational probes do not count."
        )
    if int(verified_replay.get("executed_count") or 0) < 5:
        actions.append("Execute replay packs from existing outcome traces before promoting new behavior; not_run records do not count.")
    for capability in _replay_missing(verified_replay)[:4]:
        actions.append(f"Execute the Profile-configured replay minimum for {capability} with pass rate >=0.8.")
    for gap in capability_gaps[:4]:
        capability = str(gap.get("capability") or "").strip()
        if capability:
            actions.append(f"Add replay-backed evidence for {capability} ({gap['reason']}).")
        else:
            actions.append(
                "Register or resolve an exact active capability Profile before evaluating readiness; no legacy cohort will be substituted."
            )
    if int(evidence_counts.get("l5_world_model") or 0) == 0:
        actions.append("Run or persist an L5 world-model report after the read-only readiness report is reviewed.")
    if stage["stage"] in {"L4", "L4.5"} and int(evidence_counts.get("rollback_or_quarantine") or 0) == 0:
        actions.append("Exercise a non-destructive rollback/quarantine rehearsal so reversibility is proven.")
    if not latest_l5_assessment.get("complete"):
        actions.append(
            "Complete an L5 assessment with zero missing evidence before claiming L5."
            if legacy_compatibility
            else "Complete the v3 projection and four-axis assessment for the exact active Profile before claiming L5."
        )
    return actions[:6] or ["Keep running readiness, replay, and dashboard reports; do not claim L5 unless assessment evidence is complete."]
