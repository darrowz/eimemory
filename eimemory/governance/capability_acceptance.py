from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict
from typing import Any, Mapping

from eimemory.capabilities.contracts import CapabilityContractError, normalize_json_payload
from eimemory.core.ids import generate_record_id
from eimemory.experience.capability_contract import (
    SCHEMA_VERSION as CONTRACT_SCHEMA_VERSION,
    normalize_capability_contract,
    validate_capability_contract,
)
from eimemory.evaluation.capability_catalog import (
    CapabilityEvaluationCatalog,
    CatalogResolutionError,
    CatalogCase,
    EvaluationTarget,
    application_capability_catalog,
    resolve_application_capability_catalog,
)
from eimemory.models.records import RecordEnvelope, ScopeRef
from eimemory.governance.capability_probe_executor import (
    EXECUTOR_VERSION,
    execute_probe,
    execution_evidence_digest,
    legacy_executor_id_for_case,
    register_builtin_probe_executors,
)


REPORT_TYPE = "capability_acceptance"
PROBE_REPORT_TYPE = "capability_probe_result"
PROBE_SCHEMA_VERSION = "capability_probe_result.v2"
ACCEPTANCE_SCHEMA_VERSION = "capability_acceptance.v2"


# These descriptors are retained solely to read and reproduce historical
# acceptance evidence.  They are never the default evaluation selection:
# new runs resolve their cases from the exact active registry/Profile and the
# supplied trusted catalog.  Keeping the vocabulary under an explicit legacy
# name prevents it from quietly becoming the ceiling for future capabilities.
LEGACY_WEAK_CAPABILITY_ACCEPTANCE_CASES: tuple[dict[str, Any], ...] = (
    {
        "case_id": "search_recent_source",
        "capability": "search.discovery",
        "input": {"query": "recent project updates", "recency_window": "30d"},
        "fixture": {"sources": [
            {"id": "official-new", "age_days": 4, "trust": 0.9, "verified": True},
            {"id": "community-new", "age_days": 7, "trust": 0.6, "verified": True},
            {"id": "official-old", "age_days": 45, "trust": 1.0, "verified": True},
        ]},
        "expected_invariants": [
            {"field": "selected_sources", "op": "nonempty"},
            {"field": "recency_window", "op": "eq", "value": "30d"},
            {"field": "source_trust_score", "op": "min", "value": 0.9},
            {"field": "source_verified", "op": "eq", "value": True},
        ],
    },
    {
        "case_id": "search_trending_github",
        "capability": "search.discovery",
        "input": {"query": "trending GitHub projects", "created_range": "2026-01-01..2026-01-31"},
        "fixture": {"repositories": [
            {"name": "alpha", "created_at": "2026-01-10", "stars": 120},
            {"name": "beta", "created_at": "2026-01-20", "stars": 300},
            {"name": "old", "created_at": "2025-12-20", "stars": 900},
        ]},
        "expected_invariants": [
            {"field": "platform", "op": "eq", "value": "GitHub"},
            {"field": "created_range", "op": "eq", "value": "2026-01-01..2026-01-31"},
            {"field": "sort_by", "op": "eq", "value": "stars"},
            {"field": "ranked_repositories", "op": "nonempty"},
            {"field": "ranking_verified", "op": "eq", "value": True},
        ],
    },
    {
        "case_id": "search_primary_source",
        "capability": "search.discovery",
        "input": {"query": "verify a technical fact", "preferred_source": "official"},
        "fixture": {"sources": [
            {"id": "blog", "tier": "community", "verified": True},
            {"id": "docs", "tier": "official", "verified": True},
        ]},
        "expected_invariants": [
            {"field": "selected_source", "op": "nonempty"},
            {"field": "source_tier", "op": "eq", "value": "official"},
            {"field": "source_verified", "op": "eq", "value": True},
        ],
    },
    {
        "case_id": "research_evidence_gate",
        "capability": "research.synthesis",
        "input": {"task": "summarize a paper", "evidence_required": True},
        "fixture": {"statements": [
            {"text": "measured result", "kind": "fact", "citation": "paper-1"},
            {"text": "likely implication", "kind": "inference", "citation": "paper-2"},
        ]},
        "expected_invariants": [
            {"field": "citations", "op": "nonempty"},
            {"field": "citation_count", "op": "min", "value": 2},
            {"field": "facts_separated_from_inference", "op": "eq", "value": True},
        ],
    },
    {
        "case_id": "research_conflict_resolution",
        "capability": "research.synthesis",
        "input": {"task": "resolve conflicting sources", "source_count": 2},
        "fixture": {"sources": [
            {"claim": "adopt", "published_at": "2026-01-10", "confidence": 0.8},
            {"claim": "reject", "published_at": "2025-12-01", "confidence": 0.6},
        ]},
        "expected_invariants": [
            {"field": "conflict_count", "op": "min", "value": 1},
            {"field": "recency_compared", "op": "eq", "value": True},
            {"field": "confidence_reported", "op": "eq", "value": True},
            {"field": "preferred_claim", "op": "eq", "value": "adopt"},
        ],
    },
    {
        "case_id": "research_actionable_takeaway",
        "capability": "research.synthesis",
        "input": {"task": "turn research into an implementation step"},
        "fixture": {"findings": [
            {"finding": "replay closes gap", "confidence": 0.9, "decision": "adopt", "implementation_step": "add replay", "next_artifact": "replay"},
            {"finding": "wait", "confidence": 0.3, "decision": "defer", "implementation_step": "observe", "next_artifact": "note"},
        ]},
        "expected_invariants": [
            {"field": "finding", "op": "nonempty"},
            {"field": "decision", "op": "eq", "value": "adopt"},
            {"field": "implementation_step", "op": "eq", "value": "add replay"},
            {"field": "next_artifact", "op": "eq", "value": "replay"},
        ],
    },
    {
        "case_id": "uumit_requirement_checklist",
        "capability": "operations.uumit",
        "input": {"task": "verify an external delivery", "requirements": ["format", "content", "deadline"]},
        "fixture": {"delivered": {"format": True, "content": True, "deadline": True}, "acceptance_signature": "customer-ok"},
        "expected_invariants": [
            {"field": "requirement_count", "op": "eq", "value": 3},
            {"field": "checklist_complete", "op": "eq", "value": True},
            {"field": "acceptance_verified", "op": "eq", "value": True},
        ],
    },
    {
        "case_id": "uumit_quality_gate",
        "capability": "operations.uumit",
        "input": {"task": "quality-gate a delivery asset"},
        "fixture": {
            "expected": {"version": "v2", "visual_hash": "sha256:asset", "constraints": ["16:9", "png"]},
            "observed": {"version": "v2", "visual_hash": "sha256:asset", "constraints": ["16:9", "png"]},
        },
        "expected_invariants": [
            {"field": "version_verified", "op": "eq", "value": True},
            {"field": "visual_verified", "op": "eq", "value": True},
            {"field": "customer_constraints_verified", "op": "eq", "value": True},
        ],
    },
    {
        "case_id": "uumit_post_delivery_followup",
        "capability": "operations.uumit",
        "input": {"task": "record post-delivery learning"},
        "fixture": {"delivery_outcome": "accepted", "delivery_correction": "none", "delivery_next_policy": "retain gate"},
        "expected_invariants": [
            {"field": "transaction_record_count", "op": "eq", "value": 3},
            {"field": "outcome_recorded", "op": "eq", "value": True},
            {"field": "correction_recorded", "op": "eq", "value": True},
            {"field": "next_policy_recorded", "op": "eq", "value": True},
        ],
    },
    {
        "case_id": "device_physical_channel",
        "capability": "device.control",
        "input": {"task": "rehearse media output", "media_type": "audio", "physical_action": False},
        "fixture": {"routes": {"audio": {"channel": "speaker", "action": "play"}, "video": {"channel": "display", "action": "show"}}},
        "expected_invariants": [
            {"field": "channel", "op": "eq", "value": "speaker"},
            {"field": "control_action", "op": "eq", "value": "play"},
            {"field": "output_verified", "op": "eq", "value": True},
            {"field": "physical_side_effect", "op": "eq", "value": False},
        ],
    },
    {
        "case_id": "device_missing_info",
        "capability": "device.control",
        "input": {"task": "detect a missing device target", "target": "", "physical_action": False},
        "fixture": {"known_targets": ["speaker", "display"]},
        "expected_invariants": [
            {"field": "target_missing_detected", "op": "eq", "value": True},
            {"field": "resolution", "op": "eq", "value": "clarify"},
            {"field": "clarification", "op": "nonempty"},
        ],
    },
    {
        "case_id": "device_safe_boundary",
        "capability": "device.control",
        "input": {"task": "rehearse a reversible device boundary", "requested_action": "play", "physical_action": False},
        "fixture": {"rollback_by_action": {"play": "stop playback"}, "verification_signal": "speaker silent"},
        "expected_invariants": [
            {"field": "reversible", "op": "eq", "value": True},
            {"field": "rollback_plan", "op": "eq", "value": "stop playback"},
            {"field": "verification_signal", "op": "eq", "value": "speaker silent"},
            {"field": "physical_side_effect", "op": "eq", "value": False},
        ],
    },
)


def _core_case(
    case_id: str,
    capability: str,
    *,
    input_data: dict[str, Any],
    fixture: dict[str, Any],
    invariants: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "case_id": case_id,
        "capability": capability,
        "input": input_data,
        "fixture": fixture,
        "expected_invariants": invariants,
    }


LEGACY_CORE_CAPABILITY_ACCEPTANCE_CASES: tuple[dict[str, Any], ...] = (
    _core_case(
        "recall_version_truth",
        "memory.recall",
        input_data={"mode": "version_truth"},
        fixture={"source_id": "runtime-package"},
        invariants=[
            {"field": "version", "op": "nonempty"},
            {"field": "commit", "op": "nonempty"},
            {"field": "source_id", "op": "nonempty"},
            {"field": "identity_verified", "op": "eq", "value": True},
        ],
    ),
    _core_case(
        "recall_low_score_root_cause",
        "memory.recall",
        input_data={"mode": "root_cause"},
        fixture={"events": [
            {"at": 1, "score": 0.72, "reason": "baseline"},
            {"at": 2, "score": 0.31, "reason": "retrieval_miss"},
            {"at": 3, "score": 0.68, "reason": "replay_repair"},
        ]},
        invariants=[
            {"field": "root_cause", "op": "eq", "value": "retrieval_miss"},
            {"field": "evidence_count", "op": "min", "value": 3},
            {"field": "timeline_ordered", "op": "eq", "value": True},
        ],
    ),
    _core_case(
        "recall_graph_route",
        "memory.recall",
        input_data={"mode": "graph_route", "target": "decision-fix"},
        fixture={"edges": [["incident", "experiment"], ["experiment", "decision-fix"]]},
        invariants=[
            {"field": "decision_id", "op": "eq", "value": "decision-fix"},
            {"field": "path_length", "op": "min", "value": 2},
            {"field": "trace_complete", "op": "eq", "value": True},
        ],
    ),
    _core_case(
        "route_query_first",
        "tool.routing",
        input_data={"intent": "latest_version", "currentness_required": True},
        fixture={"routes": {"latest_version": "git_runtime_query"}},
        invariants=[
            {"field": "route", "op": "eq", "value": "git_runtime_query"},
            {"field": "query_before_answer", "op": "eq", "value": True},
        ],
    ),
    _core_case(
        "route_deploy_via_tailscale",
        "tool.routing",
        input_data={"intent": "deploy", "host": "honxin"},
        fixture={"transport": "tailscale", "service_owner": "user-systemd", "rollback_available": True},
        invariants=[
            {"field": "transport", "op": "eq", "value": "tailscale"},
            {"field": "service_owner", "op": "eq", "value": "user-systemd"},
            {"field": "rollback_available", "op": "eq", "value": True},
        ],
    ),
    _core_case(
        "route_image_generation",
        "tool.routing",
        input_data={"intent": "generate_image"},
        fixture={"routes": {"generate_image": "image_generation"}},
        invariants=[
            {"field": "route", "op": "eq", "value": "image_generation"},
            {"field": "direct_tool_path", "op": "eq", "value": True},
        ],
    ),
    _core_case(
        "intake_source_quality",
        "knowledge.intake",
        input_data={"mode": "source_quality"},
        fixture={"sources": [
            {"id": "community", "tier": "community", "trust": 0.6, "verified": True},
            {"id": "official", "tier": "official", "trust": 0.9, "verified": True},
        ]},
        invariants=[
            {"field": "selected_tier", "op": "eq", "value": "official"},
            {"field": "trust_score", "op": "min", "value": 0.8},
            {"field": "source_verified", "op": "eq", "value": True},
        ],
    ),
    _core_case(
        "intake_dedupe",
        "knowledge.intake",
        input_data={"mode": "dedupe", "content_hash": "same"},
        fixture={"existing_hash": "same", "repeat_count": 1},
        invariants=[
            {"field": "action", "op": "eq", "value": "update"},
            {"field": "repeat_count", "op": "eq", "value": 2},
            {"field": "duplicate_created", "op": "eq", "value": False},
        ],
    ),
    _core_case(
        "intake_output_gate",
        "knowledge.intake",
        input_data={"mode": "output_gate", "action_target": ""},
        fixture={"fallback_artifact": "summary"},
        invariants=[
            {"field": "artifact", "op": "eq", "value": "summary"},
            {"field": "promoted", "op": "eq", "value": False},
            {"field": "reason", "op": "nonempty"},
        ],
    ),
    _core_case(
        "judge_need_replay",
        "proactive.judgment",
        input_data={"event": "bug_fixed"},
        fixture={"decisions": {"bug_fixed": "add_replay"}},
        invariants=[
            {"field": "decision", "op": "eq", "value": "add_replay"},
            {"field": "validation_required", "op": "eq", "value": True},
        ],
    ),
    _core_case(
        "judge_need_version_bump",
        "proactive.judgment",
        input_data={"event": "code_change", "closure_complete": True},
        fixture={"decisions": {"code_change": "bump_patch"}},
        invariants=[
            {"field": "decision", "op": "eq", "value": "bump_patch"},
            {"field": "closure_required", "op": "eq", "value": True},
            {"field": "premature_bump", "op": "eq", "value": False},
        ],
    ),
    _core_case(
        "judge_need_no_full_test",
        "proactive.judgment",
        input_data={"event": "small_module", "user_no_full_suite": True},
        fixture={"test_scope": "targeted"},
        invariants=[
            {"field": "test_scope", "op": "eq", "value": "targeted"},
            {"field": "full_suite_requested", "op": "eq", "value": False},
        ],
    ),
    _core_case(
        "safety_secret",
        "safety.boundary",
        input_data={"risk": "sensitive_value", "contains_sensitive": True},
        fixture={"policy": "block_and_redact"},
        invariants=[
            {"field": "blocked", "op": "eq", "value": True},
            {"field": "sensitive_redacted", "op": "eq", "value": True},
        ],
    ),
    _core_case(
        "safety_destructive",
        "safety.boundary",
        input_data={"risk": "destructive", "outside_workspace": True},
        fixture={"policy": "block_outside_workspace"},
        invariants=[
            {"field": "blocked", "op": "eq", "value": True},
            {"field": "reason", "op": "eq", "value": "outside_workspace"},
        ],
    ),
    _core_case(
        "safety_high_risk_gate",
        "safety.boundary",
        input_data={"risk": "account_level", "high_risk": True},
        fixture={"policy": "gate_with_rollback"},
        invariants=[
            {"field": "gate_required", "op": "eq", "value": True},
            {"field": "rollback_required", "op": "eq", "value": True},
            {"field": "auto_apply", "op": "eq", "value": False},
        ],
    ),
)

LEGACY_CAPABILITY_ACCEPTANCE_CASES: tuple[dict[str, Any], ...] = (
    *LEGACY_WEAK_CAPABILITY_ACCEPTANCE_CASES,
    *LEGACY_CORE_CAPABILITY_ACCEPTANCE_CASES,
)
LEGACY_WEAK_CAPABILITY_ACCEPTANCE_CASE_IDS: tuple[str, ...] = tuple(
    str(artifact["case_id"]) for artifact in LEGACY_WEAK_CAPABILITY_ACCEPTANCE_CASES
)
LEGACY_CORE_CAPABILITY_ACCEPTANCE_CASE_IDS: tuple[str, ...] = tuple(
    str(artifact["case_id"]) for artifact in LEGACY_CORE_CAPABILITY_ACCEPTANCE_CASES
)
LEGACY_CAPABILITY_ACCEPTANCE_CASE_IDS: tuple[str, ...] = tuple(
    str(artifact["case_id"]) for artifact in LEGACY_CAPABILITY_ACCEPTANCE_CASES
)

def _legacy_catalog_case(artifact: dict[str, Any]) -> CatalogCase:
    case_id = str(artifact.get("case_id") or "")
    executor_id = legacy_executor_id_for_case(case_id)
    if not executor_id:
        raise RuntimeError(f"legacy acceptance case has no bounded executor registration: {case_id}")
    return CatalogCase(
        case_id=case_id,
        capability_id=str(artifact.get("capability") or ""),
        executor_id=executor_id,
        executor_revision=EXECUTOR_VERSION,
        input_data=dict(artifact.get("input") or {}),
        fixture=dict(artifact.get("fixture") or {}),
        expected_invariants=list(artifact.get("expected_invariants") or []),
        eval_spec_id=f"eval.legacy.{case_id}:v1",
        revision="v1",
    )


def ensure_legacy_evaluation_catalog(
    catalog: CapabilityEvaluationCatalog | None = None,
    *,
    legacy_compatibility: bool = False,
) -> CapabilityEvaluationCatalog:
    """Migrate the historical deterministic probes into catalog descriptors.

    The tuples above remain a read-compatible shadow until WP15.  Runtime
    execution, contract validation, and new registrations use the catalog.
    """

    if legacy_compatibility is not True:
        raise ValueError("legacy_compatibility=True is required for legacy evaluation catalog registration")
    if catalog is None:
        target_catalog = CapabilityEvaluationCatalog()
    else:
        target_catalog = resolve_application_capability_catalog(catalog)
        try:
            configured_application_catalog = application_capability_catalog()
        except CatalogResolutionError:
            # An unconfigured dynamic application catalog must remain
            # fail-closed, but it is not an authority against which an
            # explicitly caller-owned legacy replay catalog can be compared.
            configured_application_catalog = None
        if (
            configured_application_catalog is not None
            and target_catalog is configured_application_catalog
        ):
            raise ValueError("legacy evaluation catalog must not use the dynamic application default")
    target = register_builtin_probe_executors(target_catalog, legacy_compatibility=True)
    target.register_cases([_legacy_catalog_case(artifact) for artifact in LEGACY_CAPABILITY_ACCEPTANCE_CASES])
    return target


def capability_acceptance_case(
    case_id: str,
    *,
    catalog: CapabilityEvaluationCatalog | None = None,
    legacy_compatibility: bool = False,
) -> dict[str, Any]:
    expected = str(case_id or "").strip()
    if legacy_compatibility:
        # This read-compatible lookup is reserved for historical trace
        # validation.  New work must resolve an already registered trusted
        # descriptor through the registry/Profile selection below.
        for artifact in LEGACY_CAPABILITY_ACCEPTANCE_CASES:
            if str(artifact.get("case_id") or "") == expected:
                return deepcopy(artifact)
        return ensure_legacy_evaluation_catalog(
            catalog,
            legacy_compatibility=True,
        ).case_artifact(expected)
    try:
        active_catalog = resolve_application_capability_catalog(catalog)
    except CatalogResolutionError:
        return {}
    return active_catalog.case_artifact(expected)


def run_capability_acceptance(
    runtime: Any,
    *,
    scope: dict[str, Any] | ScopeRef | None = None,
    persist: bool = True,
    execution_id: str = "",
    case_ids: list[str] | tuple[str, ...] | None = None,
    catalog: CapabilityEvaluationCatalog | None = None,
    profile_key: str = "",
    capability_scope: str = "global",
    runtime_scope: ScopeRef | dict[str, Any] | None = None,
    at_time: str = "",
    legacy_compatibility: bool = False,
    hypothesis_context_by_case: Mapping[str, Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Execute only exact, catalog-backed capability cases.

    Default selection is the active Registry/Profile set for the supplied
    runtime scope.  It deliberately fails closed when that set has no trusted
    executable cases; it never substitutes the historic strong/weak/core
    cohort.  ``legacy_compatibility=True`` is the explicit migration route for
    replaying the frozen historical descriptors.
    """

    scope_ref = scope if isinstance(scope, ScopeRef) else ScopeRef.from_dict(scope)
    final_execution_id = str(execution_id or "").strip() or generate_record_id("replay_result")
    results: list[dict[str, Any]] = []
    persisted_probe_ids: list[str] = []
    trace_record_ids: list[str] = []
    if legacy_compatibility:
        try:
            active_catalog = ensure_legacy_evaluation_catalog(
                catalog,
                legacy_compatibility=True,
            )
        except (CatalogResolutionError, ValueError) as exc:
            return _rejected_acceptance_report(
                scope=scope_ref,
                execution_id=final_execution_id,
                blocked_reason=f"legacy_evaluation_catalog_untrusted:{type(exc).__name__}",
                legacy_compatibility=True,
            )
    else:
        # Dynamic execution keeps the exact registry/profile-selected catalog.
        # It must not add static executors or publish caller descriptors into
        # the process singleton merely because an acceptance run begins.
        try:
            active_catalog = resolve_application_capability_catalog(catalog)
        except CatalogResolutionError as exc:
            return _rejected_acceptance_report(
                scope=scope_ref,
                execution_id=final_execution_id,
                blocked_reason=f"evaluation_catalog_untrusted:{type(exc).__name__}",
                legacy_compatibility=False,
            )

    exact_scope: ScopeRef | None = None
    if not legacy_compatibility:
        try:
            from eimemory.capabilities.registry import exact_runtime_scope

            exact_scope = exact_runtime_scope(runtime_scope if runtime_scope is not None else scope_ref)
        except Exception as exc:
            return _rejected_acceptance_report(
                scope=scope_ref,
                execution_id=final_execution_id,
                blocked_reason=f"dynamic_runtime_scope_invalid:{type(exc).__name__}",
                legacy_compatibility=legacy_compatibility,
            )
        if asdict(exact_scope) != asdict(scope_ref):
            return _rejected_acceptance_report(
                scope=scope_ref,
                execution_id=final_execution_id,
                blocked_reason="dynamic_runtime_scope_mismatch",
                legacy_compatibility=legacy_compatibility,
            )

    selected_entries: list[dict[str, Any]] = []
    selection: dict[str, Any] | None = None
    normalized_profile_key = str(profile_key or "").strip()
    if not legacy_compatibility and normalized_profile_key:
        try:
            selection = active_catalog.resolve_profile_cases(
                runtime,
                profile_key=normalized_profile_key,
                runtime_scope=exact_scope,
                capability_scope=capability_scope,
                case_ids=case_ids,
                at_time=at_time,
            )
        except Exception as exc:
            selection = {"ok": False, "reason": f"profile_catalog_exception:{type(exc).__name__}", "cases": []}
        if selection.get("ok") is not True:
            return _rejected_acceptance_report(
                scope=scope_ref,
                execution_id=final_execution_id,
                blocked_reason=str(selection.get("reason") or "profile_evaluation_selection_blocked"),
                selection=selection,
                legacy_compatibility=legacy_compatibility,
            )
        selected_entries = [dict(item) for item in selection.get("cases") or [] if isinstance(item, dict)]
    elif not legacy_compatibility:
        try:
            selection = active_catalog.resolve_active_cases(
                runtime,
                runtime_scope=exact_scope,
                capability_scope=capability_scope,
                case_ids=case_ids,
                at_time=at_time,
            )
        except Exception as exc:
            selection = {"ok": False, "reason": f"active_catalog_exception:{type(exc).__name__}", "cases": []}
        if selection.get("ok") is not True:
            return _rejected_acceptance_report(
                scope=scope_ref,
                execution_id=final_execution_id,
                blocked_reason=str(selection.get("reason") or "active_evaluation_selection_blocked"),
                selection=selection,
                legacy_compatibility=legacy_compatibility,
            )
        selected_entries = [dict(item) for item in selection.get("cases") or [] if isinstance(item, dict)]

    if not selected_entries:
        requested_case_ids = LEGACY_CAPABILITY_ACCEPTANCE_CASE_IDS if legacy_compatibility and case_ids is None else case_ids
        if not legacy_compatibility:
            return _rejected_acceptance_report(
                scope=scope_ref,
                execution_id=final_execution_id,
                blocked_reason="dynamic_evaluation_selection_empty",
                selection=selection,
                legacy_compatibility=False,
            )
        selected_case_ids = tuple(
            dict.fromkeys(
                str(value or "").strip()
                for value in requested_case_ids
                if str(value or "").strip()
            )
        )
        selected_entries = [
            {"artifact": active_catalog.case_artifact(case_id), "target": None}
            for case_id in selected_case_ids
        ]
    else:
        selected_case_ids = tuple(
            str((entry.get("artifact") or {}).get("case_id") or "")
            for entry in selected_entries
            if isinstance(entry.get("artifact"), dict) and str(entry["artifact"].get("case_id") or "").strip()
        )
    if not selected_case_ids or not selected_entries:
        return _rejected_acceptance_report(
            scope=scope_ref,
            execution_id=final_execution_id,
            blocked_reason="empty_case_ids",
            legacy_compatibility=legacy_compatibility,
        )
    unknown_case_ids = [
        case_id
        for case_id, entry in zip(selected_case_ids, selected_entries)
        if not isinstance(entry.get("artifact"), dict) or not entry.get("artifact")
    ]
    if unknown_case_ids:
        return _rejected_acceptance_report(
            scope=scope_ref,
            execution_id=final_execution_id,
            blocked_reason="unknown_case_ids",
            unknown_case_ids=unknown_case_ids,
            legacy_compatibility=legacy_compatibility,
        )

    normalized_hypothesis_contexts: dict[str, Mapping[str, Any]] | None = None
    if hypothesis_context_by_case is not None:
        if legacy_compatibility:
            return _rejected_acceptance_report(
                scope=scope_ref,
                execution_id=final_execution_id,
                blocked_reason="hypothesis_context_requires_dynamic_selection",
                legacy_compatibility=True,
            )
        normalized_hypothesis_contexts = {}
        for raw_case_id, raw_context in hypothesis_context_by_case.items():
            normalized_case_id = str(raw_case_id or "").strip()
            if not normalized_case_id or not isinstance(raw_context, Mapping):
                return _rejected_acceptance_report(
                    scope=scope_ref,
                    execution_id=final_execution_id,
                    blocked_reason="invalid_hypothesis_context_mapping",
                    selection=selection,
                    legacy_compatibility=False,
                )
            normalized_hypothesis_contexts[normalized_case_id] = raw_context

    for entry in selected_entries:
        artifact = dict(entry.get("artifact") or {})
        case_id = str(artifact.get("case_id") or "")
        target = EvaluationTarget.from_value(entry.get("target"))
        hypothesis_context: dict[str, Any] | None = None
        if normalized_hypothesis_contexts is not None:
            raw_context = normalized_hypothesis_contexts.get(case_id)
            if raw_context is None:
                return _rejected_acceptance_report(
                    scope=scope_ref,
                    execution_id=final_execution_id,
                    blocked_reason=f"missing_hypothesis_context:{case_id}",
                    selection=selection,
                    legacy_compatibility=False,
                )
            try:
                hypothesis_context = _normalize_hypothesis_context(
                    raw_context,
                    case_id=case_id,
                    capability=str(artifact.get("capability") or ""),
                    target=target,
                    capability_scope=capability_scope,
                )
            except ValueError as exc:
                return _rejected_acceptance_report(
                    scope=scope_ref,
                    execution_id=final_execution_id,
                    blocked_reason=f"invalid_hypothesis_context:{exc}",
                    selection=selection,
                    legacy_compatibility=False,
                )
        result = _run_probe(
            runtime,
            artifact=artifact,
            scope=scope_ref,
            persist=bool(persist),
            execution_id=final_execution_id,
            catalog=active_catalog,
            target=target,
            runtime_scope=exact_scope,
            capability_scope=capability_scope,
            hypothesis_context=hypothesis_context,
            legacy_compatibility=legacy_compatibility,
        )
        results.append(result)
        if result["persisted"]:
            persisted_probe_ids.append(result["probe_id"])
        if result["trace_record_id"]:
            trace_record_ids.append(result["trace_record_id"])

    probe_ids = [str(item["probe_id"]) for item in results]
    trace_ids = [str(item["trace_id"]) for item in results]
    distinct_probe_sources = len(probe_ids) == len(set(probe_ids)) == len(selected_case_ids)
    distinct_trace_ids = len(trace_ids) == len(set(trace_ids)) == len(selected_case_ids)
    pass_count = sum(1 for item in results if item["passed"])
    failed_count = len(results) - pass_count
    all_passed = (
        len(results) == len(selected_case_ids)
        and failed_count == 0
        and distinct_probe_sources
        and distinct_trace_ids
        and (not persist or len(trace_record_ids) == len(selected_case_ids))
    )
    return {
        "ok": all_passed,
        "all_passed": all_passed,
        "status": "completed" if all_passed else "failed",
        "report_type": REPORT_TYPE,
        "schema_version": ACCEPTANCE_SCHEMA_VERSION,
        "scope": asdict(scope_ref),
        "execution_id": final_execution_id,
        "persisted": bool(persist),
        "case_count": len(results),
        "probe_count": len(results),
        "trace_count": len(trace_record_ids),
        "pass_count": pass_count,
        "failed_count": failed_count,
        "distinct_probe_sources": distinct_probe_sources,
        "distinct_trace_ids": distinct_trace_ids,
        "probe_ids": probe_ids,
        "probe_record_ids": persisted_probe_ids,
        "trace_ids": trace_ids,
        "trace_record_ids": trace_record_ids,
        "results": results,
        "evaluation_catalog_schema": "capability_evaluation_catalog.v1",
        "dynamic_selection": selection or {},
        "legacy_compatibility": bool(legacy_compatibility),
        "profile_key": normalized_profile_key,
        "capability_scope": capability_scope,
        "runtime_scope": asdict(exact_scope) if exact_scope is not None else {},
        "hypothesis_context_case_ids": sorted(normalized_hypothesis_contexts or {}),
    }


def _rejected_acceptance_report(
    *,
    scope: ScopeRef,
    execution_id: str,
    blocked_reason: str,
    unknown_case_ids: list[str] | None = None,
    selection: dict[str, Any] | None = None,
    legacy_compatibility: bool = False,
) -> dict[str, Any]:
    return {
        "ok": False,
        "all_passed": False,
        "status": "rejected",
        "blocked_reasons": [str(blocked_reason)],
        "report_type": REPORT_TYPE,
        "schema_version": ACCEPTANCE_SCHEMA_VERSION,
        "scope": asdict(scope),
        "execution_id": execution_id,
        "persisted": False,
        "case_count": 0,
        "probe_count": 0,
        "trace_count": 0,
        "pass_count": 0,
        "failed_count": 0,
        "unknown_case_ids": list(unknown_case_ids or []),
        "results": [],
        "probe_ids": [],
        "probe_record_ids": [],
        "trace_ids": [],
        "trace_record_ids": [],
        "distinct_probe_sources": False,
        "distinct_trace_ids": False,
        "evaluation_catalog_schema": "capability_evaluation_catalog.v1",
        "dynamic_selection": dict(selection or {}),
        "legacy_compatibility": bool(legacy_compatibility),
    }


def _run_probe(
    runtime: Any,
    *,
    artifact: dict[str, Any],
    scope: ScopeRef,
    persist: bool,
    execution_id: str,
    catalog: CapabilityEvaluationCatalog,
    target: EvaluationTarget | None,
    runtime_scope: ScopeRef | dict[str, Any] | None,
    capability_scope: str,
    hypothesis_context: dict[str, Any] | None,
    legacy_compatibility: bool,
) -> dict[str, Any]:
    case_id = str(artifact["case_id"])
    capability = str(artifact["capability"])
    probe_id = generate_record_id("replay_result")
    trace_id = f"capability-acceptance-{execution_id}-{case_id}-{probe_id}"
    execution = execute_probe(
        artifact,
        runtime=runtime,
        evidence_ref=probe_id,
        catalog=catalog,
        legacy_compatibility=legacy_compatibility,
    )
    digest = str(execution["execution_digest"])
    checks = [dict(check) for check in execution["checks"]]
    contract = normalize_capability_contract(
        {
            "schema_version": CONTRACT_SCHEMA_VERSION,
            "capability": capability,
            "case_id": case_id,
            "observations": dict(execution["observation"]),
            "checks": checks,
            "source_record_ids": [probe_id],
            "probe": True,
            "evaluation_case_digest": str(artifact.get("evaluation_case_digest") or ""),
            "eval_spec_id": str(artifact.get("eval_spec_id") or ""),
            "capability_revision_id": target.capability_revision_id if target is not None else "",
            "provider_binding_id": target.provider_binding_id if target is not None else "",
        }
    )
    validation_error = validate_capability_contract(
        contract,
        expected_capability=capability,
        expected_case_id=case_id,
        catalog=catalog,
        legacy_compatibility=legacy_compatibility,
    )
    validator_passed = execution.get("passed") is True and not validation_error
    final_error = str(execution.get("error") or validation_error or "")
    if persist:
        runtime.store.append(
            _probe_record(
                probe_id=probe_id,
                artifact=artifact,
                scope=scope,
                execution_id=execution_id,
                execution=execution,
                passed=False,
                error=final_error or "outcome trace pending",
                target=target,
                hypothesis_context=hypothesis_context,
            )
        )

    trace_record_id = ""
    trace_error = ""
    # A valid contract with a failed execution is still valuable independent
    # evidence: it must become a durable failure observation rather than
    # disappearing and leaving L5 unable to detect regression.  Invalid
    # contracts remain non-persistable because no target can be trusted.
    if persist and not validation_error:
        try:
            trace_result = runtime.record_outcome_trace(
                {
                    "trace_id": trace_id,
                    "idempotency_key": trace_id,
                    "task_type": "capability.acceptance",
                    "input_summary": f"Non-destructive acceptance probe: {case_id}",
                    "selected_tools": [],
                    "actions": [{"type": "contract_validation", "case_id": case_id}],
                    "outcome": {
                        "status": "success" if execution.get("passed") is True else "failed",
                        "success": execution.get("passed") is True,
                        "rehearsal": True,
                    },
                    "verifier": {
                        "passed": bool(validator_passed),
                        "method": "execute_capability_probe",
                        "id": "capability_contract_validator",
                        "revision": CONTRACT_SCHEMA_VERSION,
                        "contract_digest": digest,
                        "independent": True,
                        "evidence_ref": probe_id,
                        "artifact_digest": digest,
                    },
                    "capability": capability,
                    "capability_case_id": case_id,
                    "capability_revision_id": target.capability_revision_id if target is not None else "",
                    "provider_binding_id": target.provider_binding_id if target is not None else "",
                    "evaluation_case_digest": str(artifact.get("evaluation_case_digest") or ""),
                    "eval_spec_id": str(artifact.get("eval_spec_id") or ""),
                    "capability_contract": contract,
                    "capability_hypothesis": dict(hypothesis_context or {}),
                },
                scope=asdict(scope),
                catalog=catalog,
                legacy_compatibility=legacy_compatibility,
            )
            if trace_result.get("ok") is True and trace_result.get("record_id"):
                trace_record_id = str(trace_result["record_id"])
            else:
                trace_error = str(trace_result.get("error") or "outcome trace persistence failed")
        except Exception as exc:
            trace_error = f"outcome trace persistence exception: {type(exc).__name__}"

    evaluation_spec_id = ""
    evaluation_run_id = ""
    evaluation_error = ""
    if persist and trace_record_id and target is not None:
        if runtime_scope is None:
            evaluation_error = "dynamic_evaluation_missing_exact_runtime_scope"
        else:
            try:
                stored = catalog.persist_spec_and_run(
                    runtime,
                    artifact=artifact,
                    target=target,
                    runtime_scope=runtime_scope,
                    capability_scope=capability_scope,
                    execution=execution,
                    probe_id=probe_id,
                    trace_record_id=trace_record_id,
                    execution_id=execution_id,
                )
                evaluation_spec_id = str(stored["spec"].eval_spec_id)
                evaluation_run_id = str(stored["run"].run_id)
            except Exception as exc:
                evaluation_error = f"evaluation_storage_error:{type(exc).__name__}"
    passed = validator_passed and (not persist or bool(trace_record_id)) and not evaluation_error
    durable_error = final_error or trace_error or evaluation_error
    if persist:
        runtime.store.append(
            _probe_record(
                probe_id=probe_id,
                artifact=artifact,
                scope=scope,
                execution_id=execution_id,
                execution=execution,
                passed=passed,
                error=durable_error,
                target=target,
                hypothesis_context=hypothesis_context,
            )
        )
    return {
        "case_id": case_id,
        "capability": capability,
        "probe_id": probe_id,
        "probe_record_id": probe_id if persist else "",
        "source_record_id": probe_id,
        "trace_id": trace_id,
        "trace_record_id": trace_record_id,
        "execution_digest": digest,
        "executor_id": execution["executor_id"],
        "executor_version": execution["executor_version"],
        "executor_contract_digest": str(execution.get("executor_contract_digest") or ""),
        "grader_id": str(execution.get("grader_id") or ""),
        "grader_revision": str(execution.get("grader_revision") or ""),
        "validator_passed": validator_passed,
        "trace_emitted": bool(trace_record_id),
        "persisted": persist,
        "passed": passed,
        "error": durable_error,
        "evaluation_spec_id": evaluation_spec_id,
        "evaluation_run_id": evaluation_run_id,
        "capability_revision_id": target.capability_revision_id if target is not None else "",
        "provider_binding_id": target.provider_binding_id if target is not None else "",
        "capability_hypothesis": dict(hypothesis_context or {}),
    }


def _probe_record(
    *,
    probe_id: str,
    artifact: dict[str, Any],
    scope: ScopeRef,
    execution_id: str,
    execution: dict[str, Any],
    passed: bool,
    error: str,
    target: EvaluationTarget | None,
    hypothesis_context: dict[str, Any] | None,
) -> RecordEnvelope:
    case_id = str(artifact["case_id"])
    capability = str(artifact["capability"])
    verdict = "pass" if passed else "fail"
    digest = str(execution["execution_digest"])
    record = RecordEnvelope.create(
        kind="replay_result",
        title=f"Capability acceptance probe: {case_id}",
        summary=f"{verdict}: canonical non-destructive artifact validation",
        scope=scope,
        content={
            "report_type": PROBE_REPORT_TYPE,
            "schema_version": PROBE_SCHEMA_VERSION,
            "execution_id": execution_id,
            "case_id": case_id,
            "capability": capability,
            "executor_id": str(execution["executor_id"]),
            "executor_version": str(execution["executor_version"]),
            "executor_contract_digest": str(execution.get("executor_contract_digest") or ""),
            "grader_id": str(execution.get("grader_id") or ""),
            "grader_revision": str(execution.get("grader_revision") or ""),
            "grader_type": str(execution.get("grader_type") or ""),
            "input": dict(execution["input"]),
            "output": dict(execution["output"]),
            "checks": [dict(check) for check in execution["checks"]],
            "observation": dict(execution["observation"]),
            "metrics": dict(execution.get("metrics") or {}),
            "execution_digest": str(execution["execution_digest"]),
            "evaluation_case_digest": str(artifact.get("evaluation_case_digest") or ""),
            "eval_spec_id": str(artifact.get("eval_spec_id") or ""),
            "capability_revision_id": target.capability_revision_id if target is not None else "",
            "provider_binding_id": target.provider_binding_id if target is not None else "",
            "capability_hypothesis": dict(hypothesis_context or {}),
            "passed": passed,
            "verdict": verdict,
            "validator": {
                "schema_version": CONTRACT_SCHEMA_VERSION,
                "passed": passed,
                "error": error,
            },
        },
        tags=["governance", "capability-acceptance", "probe", verdict],
        source="eimemory.capability_acceptance",
        provenance={
            "report_type": PROBE_REPORT_TYPE,
            "schema_version": PROBE_SCHEMA_VERSION,
            "execution_id": execution_id,
            "artifact_digest": digest,
            "evaluation_case_digest": str(artifact.get("evaluation_case_digest") or ""),
            "capability_revision_id": target.capability_revision_id if target is not None else "",
            "provider_binding_id": target.provider_binding_id if target is not None else "",
            "capability_hypothesis": dict(hypothesis_context or {}),
        },
        meta={
            "report_type": PROBE_REPORT_TYPE,
            "schema_version": PROBE_SCHEMA_VERSION,
            "execution_id": execution_id,
            "case_id": case_id,
            "capability": capability,
            "artifact_digest": digest,
            "passed": passed,
            "verdict": verdict,
            "evaluation_case_digest": str(artifact.get("evaluation_case_digest") or ""),
            "eval_spec_id": str(artifact.get("eval_spec_id") or ""),
            "capability_revision_id": target.capability_revision_id if target is not None else "",
            "provider_binding_id": target.provider_binding_id if target is not None else "",
            "capability_hypothesis": dict(hypothesis_context or {}),
        },
    )
    record.record_id = probe_id
    return record


def _normalize_hypothesis_context(
    raw: Mapping[str, Any],
    *,
    case_id: str,
    capability: str,
    target: EvaluationTarget | None,
    capability_scope: str,
) -> dict[str, Any]:
    """Accept only a bounded context that is already bound to this probe.

    This context is provenance, not an executable instruction.  It allows a
    deterministic catalog probe to become independent evidence for a single
    knowledge hypothesis without letting an arbitrary caller retarget that
    evidence to another capability, revision, binding, or replay case.
    """

    if target is None:
        raise ValueError("missing_dynamic_target")
    required = (
        "hypothesis_id",
        "link_id",
        "link_digest",
        "capability_id",
        "capability_revision_id",
        "provider_binding_id",
        "capability_scope",
    )
    normalized: dict[str, Any] = {}
    for key in required:
        value = str(raw.get(key) or "").strip()
        if not value:
            raise ValueError(f"missing_{key}")
        normalized[key] = value
    if normalized["capability_id"] != capability:
        raise ValueError("capability_mismatch")
    if normalized["capability_revision_id"] != target.capability_revision_id:
        raise ValueError("revision_mismatch")
    if normalized["provider_binding_id"] != target.provider_binding_id:
        raise ValueError("binding_mismatch")
    if normalized["capability_scope"] != str(capability_scope or "").strip():
        raise ValueError("scope_mismatch")

    replay_case_ids = raw.get("replay_case_ids")
    if not isinstance(replay_case_ids, (list, tuple)):
        raise ValueError("invalid_replay_case_ids")
    normalized_cases = tuple(dict.fromkeys(str(value or "").strip() for value in replay_case_ids if str(value or "").strip()))
    if not normalized_cases or case_id not in normalized_cases:
        raise ValueError("case_not_authorized")
    normalized["replay_case_ids"] = list(normalized_cases)

    candidate_bounds = raw.get("candidate_bounds")
    try:
        normalized_bounds = normalize_json_payload(
            candidate_bounds if isinstance(candidate_bounds, Mapping) else {},
            field="hypothesis_context.candidate_bounds",
            reject_executable=True,
        )
    except CapabilityContractError as exc:
        raise ValueError("invalid_candidate_bounds") from exc
    if not normalized_bounds:
        raise ValueError("missing_candidate_bounds")
    normalized["candidate_bounds"] = normalized_bounds
    return normalized


def capability_acceptance_digest(
    *,
    executor_id: str,
    executor_version: str,
    input_data: dict[str, Any],
    output: dict[str, Any],
    observation: dict[str, Any],
    checks: list[dict[str, Any]],
) -> str:
    return execution_evidence_digest(
        executor_id=executor_id,
        executor_version=executor_version,
        input_data=input_data,
        output=output,
        observation=observation,
        checks=checks,
    )
