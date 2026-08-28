"""Machine-gated evolution from dynamic L5 capability gaps.

This orchestration layer has no compiled weak-capability list and no deferred
authorization state.  A profile-specific snapshot gap can become a bounded
experiment only when it has an exact revision/binding, a current knowledge
hypothesis, an explicitly selected evaluation case, independent evidence, and
the ordinary isolated code-application gates.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import asdict
from hashlib import sha256
import json
from typing import Any

from eimemory.capabilities.projector import CapabilityStateProjector
from eimemory.capabilities.registry import exact_runtime_scope
from eimemory.capabilities.contracts import CapabilityContractError, normalize_json_payload
from eimemory.evaluation.capability_catalog import (
    CapabilityEvaluationCatalog,
    CatalogResolutionError,
    resolve_application_capability_catalog,
)
from eimemory.governance.capability_hypotheses import (
    hypothesis_behavior_gate,
    list_capability_hypotheses,
    record_hypothesis_evaluation_artifact,
    record_hypothesis_experiment_feedback,
)
from eimemory.governance.capability_acceptance import run_capability_acceptance
from eimemory.governance.autonomous_evolution import run_autonomous_evolution
from eimemory.governance.code_automation_policy import (
    code_automation_policy_summary,
    machine_policy_context_from_mapping,
)
from eimemory.models.records import RecordEnvelope, ScopeRef


DYNAMIC_EVOLUTION_SCHEMA = "dynamic.capability_evolution.v1"
_MAX_WORK_ITEMS = 128


class DynamicCapabilityEvolutionError(ValueError):
    """A dynamic evolution request lacks a bounded, auditable dependency."""


def build_dynamic_capability_evolution_plan(
    runtime: Any,
    *,
    profile_key: str,
    runtime_scope: ScopeRef | Mapping[str, Any],
    capability_scope: str = "global",
    catalog: CapabilityEvaluationCatalog | None = None,
    max_candidates: int = 100,
    observation_limit: int = 500,
) -> dict[str, Any]:
    """Create a read-only plan from unresolved dynamic profile candidates."""

    scope = exact_runtime_scope(runtime_scope)
    if not str(profile_key or "").strip():
        raise DynamicCapabilityEvolutionError("profile_key is required")
    try:
        catalog = resolve_application_capability_catalog(catalog)
    except CatalogResolutionError as exc:
        raise DynamicCapabilityEvolutionError("evaluation_catalog_untrusted") from exc
    projector = CapabilityStateProjector(runtime.store)
    projection = projector.project(
        str(profile_key),
        runtime_scope=scope,
        capability_scope=capability_scope,
        max_candidates=max_candidates,
        observation_limit=observation_limit,
        persist=False,
    ).to_dict()
    cases = catalog.resolve_profile_cases(
        runtime,
        profile_key=str(profile_key),
        runtime_scope=scope,
        capability_scope=capability_scope,
        max_candidates=max_candidates,
    )
    case_index = _case_index(cases.get("cases") if isinstance(cases, Mapping) else ())
    work_items: list[dict[str, Any]] = []
    for blocked in projection.get("blocked") or ():
        if not isinstance(blocked, Mapping):
            continue
        capability_id = str(blocked.get("capability_id") or "")
        revision_id = str(blocked.get("capability_revision_id") or "")
        binding_id = str(blocked.get("provider_binding_id") or "")
        if not capability_id or not revision_id or not binding_id:
            # A gap without an exact executable target is a registry/profile
            # issue, not permission to infer a target implementation.
            work_items.append(
                _blocked_item(
                    blocked,
                    reason="gap_has_no_exact_revision_binding",
                    projection=projection,
                )
            )
            continue
        hypotheses = list_capability_hypotheses(
            runtime,
            runtime_scope=scope,
            capability_id=capability_id,
            capability_revision_id=revision_id,
            capability_scope=capability_scope,
            status="candidate",
            limit=2,
        )
        if len(hypotheses) != 1:
            work_items.append(
                _blocked_item(
                    blocked,
                    reason="hypothesis_missing_or_ambiguous",
                    projection=projection,
                    detail={"candidate_hypothesis_count": len(hypotheses)},
                )
            )
            continue
        target_cases = case_index.get((capability_id, revision_id, binding_id), ())
        if not target_cases:
            work_items.append(
                _blocked_item(
                    blocked,
                    reason="profile_selected_evaluation_case_missing",
                    projection=projection,
                    detail={"hypothesis_id": hypotheses[0].record_id},
                )
            )
            continue
        hypothesis = hypotheses[0]
        content = hypothesis.content if isinstance(hypothesis.content, Mapping) else {}
        try:
            normalized_bounds = normalize_json_payload(
                content.get("candidate_bounds") if isinstance(content.get("candidate_bounds"), Mapping) else {},
                field="dynamic_capability_evolution.hypothesis_candidate_bounds",
                reject_executable=True,
            )
        except CapabilityContractError:
            normalized_bounds = {}
        if (
            not str(content.get("link_id") or "").strip()
            or not str(content.get("link_digest") or "").strip()
            or not normalized_bounds
        ):
            work_items.append(
                _blocked_item(
                    blocked,
                    reason="hypothesis_context_or_bounds_invalid",
                    projection=projection,
                    detail={"hypothesis_id": hypothesis.record_id},
                )
            )
            continue
        item = {
            "work_item_id": _digest(
                {
                    "profile_key": str(profile_key),
                    "profile": projection.get("profile_digest"),
                    "watermark": projection.get("input_watermark"),
                    "capability_id": capability_id,
                    "revision_id": revision_id,
                    "binding_id": binding_id,
                    "hypothesis_id": hypothesis.record_id,
                    "case_ids": [str(case["artifact"].get("case_id") or "") for case in target_cases],
                }
            )[:40],
            "status": "ready_for_independent_evaluation",
            "reason": str(blocked.get("reason") or "profile_gap"),
            "capability_id": capability_id,
            "capability_revision_id": revision_id,
            "provider_binding_id": binding_id,
            "capability_scope": capability_scope,
            "profile_key": str(profile_key),
            "profile_id": str(projection.get("profile_id") or ""),
            "profile_digest": str(projection.get("profile_digest") or ""),
            "evidence_watermark": str(projection.get("input_watermark") or ""),
            "hypothesis_id": hypothesis.record_id,
            "hypothesis_link_id": str(content.get("link_id") or ""),
            "hypothesis_link_digest": str(content.get("link_digest") or ""),
            "expected_metric": dict(content.get("expected_metric") or {}),
            "candidate_bounds": normalized_bounds,
            "evaluation_cases": [dict(case) for case in target_cases],
            "machine_authorization": "independent_evidence_then_policy_gated_apply",
        }
        work_items.append(item)
    if len(work_items) > _MAX_WORK_ITEMS:
        raise DynamicCapabilityEvolutionError("profile gap count exceeds bounded evolution plan limit")
    work_items.sort(key=lambda item: (str(item.get("capability_id") or ""), str(item.get("work_item_id") or "")))
    material = {
        "schema": DYNAMIC_EVOLUTION_SCHEMA,
        "profile_key": str(profile_key),
        "capability_scope": capability_scope,
        "runtime_scope": asdict(scope),
        "projection_digest": str(projection.get("projection_digest") or ""),
        "input_watermark": str(projection.get("input_watermark") or ""),
        "catalog_status": str(cases.get("status") or "ready") if isinstance(cases, Mapping) else "invalid",
        "work_items": work_items,
    }
    return {**material, "plan_digest": _digest(material), "ok": bool(cases.get("ok", True)) if isinstance(cases, Mapping) else False}


def collect_dynamic_capability_independent_evidence(
    runtime: Any,
    *,
    profile_key: str,
    runtime_scope: ScopeRef | Mapping[str, Any],
    capability_scope: str = "global",
    catalog: CapabilityEvaluationCatalog | None = None,
    max_candidates: int = 100,
    observation_limit: int = 500,
    work_item_ids: Sequence[str] | None = None,
) -> dict[str, Any]:
    """Collect bounded, independently verified evidence for the live plan.

    The collector does not invent outcomes or select a historic capability
    cohort.  It asks the catalog to execute only the exact active
    profile/registry cases already attached to each plan item, persists the
    probe and its independent outcome trace, and returns references that can
    be consumed by :func:`execute_dynamic_capability_evolution`.
    """

    scope = exact_runtime_scope(runtime_scope)
    try:
        active_catalog = resolve_application_capability_catalog(catalog)
    except CatalogResolutionError as exc:
        raise DynamicCapabilityEvolutionError("evaluation_catalog_untrusted") from exc
    plan = build_dynamic_capability_evolution_plan(
        runtime,
        profile_key=profile_key,
        runtime_scope=scope,
        capability_scope=capability_scope,
        catalog=active_catalog,
        max_candidates=max_candidates,
        observation_limit=observation_limit,
    )
    requested_ids: set[str] | None = None
    if work_item_ids is not None:
        if isinstance(work_item_ids, (str, bytes)) or not isinstance(work_item_ids, Sequence):
            raise DynamicCapabilityEvolutionError("work_item_ids must be a bounded sequence")
        requested_ids = {str(value or "").strip() for value in work_item_ids if str(value or "").strip()}
        if len(requested_ids) > _MAX_WORK_ITEMS:
            raise DynamicCapabilityEvolutionError("work_item_ids exceeds bounded evolution plan limit")
    return _collect_independent_evidence_for_plan(
        runtime,
        plan=plan,
        profile_key=profile_key,
        scope=scope,
        capability_scope=capability_scope,
        catalog=active_catalog,
        requested_ids=requested_ids,
    )


def _collect_independent_evidence_for_plan(
    runtime: Any,
    *,
    plan: Mapping[str, Any],
    profile_key: str,
    scope: ScopeRef,
    capability_scope: str,
    catalog: CapabilityEvaluationCatalog,
    requested_ids: set[str] | None,
) -> dict[str, Any]:
    evidence: dict[str, dict[str, Any]] = {}
    results: list[dict[str, Any]] = []
    for item in plan.get("work_items") or ():
        if not isinstance(item, Mapping):
            continue
        work_item_id = str(item.get("work_item_id") or "")
        if requested_ids is not None and work_item_id not in requested_ids:
            continue
        if item.get("status") != "ready_for_independent_evaluation":
            results.append(
                {
                    "work_item_id": work_item_id,
                    "status": "skipped",
                    "reason": str(item.get("reason") or "plan_item_not_ready"),
                }
            )
            continue
        cases = _ordered_cases(item.get("evaluation_cases"))
        if not cases:
            results.append(
                {
                    "work_item_id": work_item_id,
                    "status": "blocked",
                    "reason": "plan_has_no_exact_evaluation_cases",
                }
            )
            continue
        try:
            context = _work_item_hypothesis_context(item, cases=cases)
        except DynamicCapabilityEvolutionError as exc:
            results.append({"work_item_id": work_item_id, "status": "blocked", "reason": str(exc)})
            continue
        case_ids = [str(case["artifact"].get("case_id") or "") for case in cases]
        acceptance = run_capability_acceptance(
            runtime,
            scope=scope,
            persist=True,
            execution_id=f"dynamic-evidence-{str(plan.get('plan_digest') or '')[:16]}-{work_item_id[:16]}",
            case_ids=case_ids,
            catalog=catalog,
            profile_key=str(profile_key),
            capability_scope=capability_scope,
            runtime_scope=scope,
            hypothesis_context_by_case={case_id: context for case_id in case_ids},
        )
        collected_cases: list[dict[str, Any]] = []
        for result in acceptance.get("results") or ():
            if not isinstance(result, Mapping):
                continue
            case_id = str(result.get("case_id") or "")
            required = ("probe_id", "trace_record_id", "execution_id")
            if not case_id or any(not result.get(field) for field in required):
                continue
            collected_cases.append(
                {
                    "case_id": case_id,
                    "evidence_ref": str(result.get("probe_record_id") or result.get("probe_id") or ""),
                    "probe_id": str(result.get("probe_id") or ""),
                    "trace_record_id": str(result.get("trace_record_id") or ""),
                    "execution_id": str(acceptance.get("execution_id") or ""),
                }
            )
        expected_case_ids = {str(case["artifact"].get("case_id") or "") for case in cases}
        collected_case_ids = {str(value.get("case_id") or "") for value in collected_cases}
        if expected_case_ids != collected_case_ids:
            results.append(
                {
                    "work_item_id": work_item_id,
                    "status": "blocked",
                    "reason": "independent_evidence_collection_incomplete",
                    "acceptance": acceptance,
                }
            )
            continue
        collected_cases.sort(key=lambda value: str(value["case_id"]))
        evidence[work_item_id] = {
            "cases": collected_cases,
            "collection_execution_id": str(acceptance.get("execution_id") or ""),
            "plan_digest": str(plan.get("plan_digest") or ""),
        }
        results.append(
            {
                "work_item_id": work_item_id,
                "status": "collected",
                "acceptance_ok": acceptance.get("ok") is True,
                "case_ids": [value["case_id"] for value in collected_cases],
                "trace_record_ids": [value["trace_record_id"] for value in collected_cases],
            }
        )
    material = {
        "schema": DYNAMIC_EVOLUTION_SCHEMA,
        "plan_digest": str(plan.get("plan_digest") or ""),
        "profile_key": str(profile_key),
        "capability_scope": capability_scope,
        "runtime_scope": asdict(scope),
        "results": results,
        "evidence": evidence,
    }
    return {
        **material,
        "ok": bool(evidence) and all(entry.get("status") != "blocked" for entry in results),
        "evidence_digest": _digest(material),
    }


def execute_dynamic_capability_evolution(
    runtime: Any,
    *,
    profile_key: str,
    runtime_scope: ScopeRef | Mapping[str, Any],
    independent_evidence: Mapping[str, Mapping[str, Any]] | None = None,
    auto_collect_independent_evidence: bool = True,
    auto_propose_code_patch: bool = True,
    capability_scope: str = "global",
    catalog: CapabilityEvaluationCatalog | None = None,
    candidate_opportunities: Mapping[str, Mapping[str, Any]] | None = None,
    apply: bool = True,
    max_apply: int = 1,
    max_candidates: int = 100,
    observation_limit: int = 500,
) -> dict[str, Any]:
    """Run selected evaluations and optional bounded code evolution.

    ``independent_evidence`` is keyed by work item ID and, when supplied,
    carries one durable probe/trace tuple for every exact selected case.  By
    default missing tuples are collected through the deterministic catalog.
    A derived evaluation artifact is created only after its execution digest
    is proven equal to that durable trace; it is not an independent authority.
    """

    scope = exact_runtime_scope(runtime_scope)
    try:
        catalog = resolve_application_capability_catalog(catalog)
    except CatalogResolutionError as exc:
        raise DynamicCapabilityEvolutionError("evaluation_catalog_untrusted") from exc
    plan = build_dynamic_capability_evolution_plan(
        runtime,
        profile_key=profile_key,
        runtime_scope=scope,
        capability_scope=capability_scope,
        catalog=catalog,
        max_candidates=max_candidates,
        observation_limit=observation_limit,
    )
    supplied_evidence = dict(independent_evidence or {})
    evidence_collection: dict[str, Any] | None = None
    if auto_collect_independent_evidence:
        pending_ids = [
            str(item.get("work_item_id") or "")
            for item in plan.get("work_items") or ()
            if isinstance(item, Mapping)
            and item.get("status") == "ready_for_independent_evaluation"
            and str(item.get("work_item_id") or "") not in supplied_evidence
        ]
        if pending_ids:
            evidence_collection = _collect_independent_evidence_for_plan(
                runtime,
                plan=plan,
                profile_key=profile_key,
                scope=scope,
                capability_scope=capability_scope,
                catalog=catalog,
                requested_ids=set(pending_ids),
            )
            collected = evidence_collection.get("evidence") if isinstance(evidence_collection.get("evidence"), Mapping) else {}
            supplied_evidence = {**dict(collected), **supplied_evidence}
    independent_evidence = supplied_evidence
    candidate_opportunities = candidate_opportunities or {}
    applied = 0
    results: list[dict[str, Any]] = []
    for item in plan["work_items"]:
        if item.get("status") != "ready_for_independent_evaluation":
            results.append({"work_item_id": item.get("work_item_id"), "status": "blocked", "reason": item.get("reason")})
            continue
        evidence = independent_evidence.get(str(item["work_item_id"]))
        if not isinstance(evidence, Mapping):
            results.append({"work_item_id": item["work_item_id"], "status": "blocked", "reason": "independent_evidence_missing"})
            continue
        selected_cases, evidence_error = _match_independent_evidence(item, evidence)
        if evidence_error:
            results.append({"work_item_id": item["work_item_id"], "status": "blocked", "reason": evidence_error})
            continue
        evaluated_cases: list[dict[str, Any]] = []
        evaluation_artifacts: list[RecordEnvelope] = []
        suite_error = ""
        for case, case_evidence in selected_cases:
            try:
                execution_result = catalog.execute_and_persist(
                    runtime,
                    artifact=case["artifact"],
                    target=case["target"],
                    runtime_scope=scope,
                    capability_scope=capability_scope,
                    evidence_ref=str(case_evidence["evidence_ref"]),
                    probe_id=str(case_evidence["probe_id"]),
                    trace_record_id=str(case_evidence["trace_record_id"]),
                    execution_id=str(case_evidence["execution_id"]),
                )
            except Exception as exc:  # Evidence/catalog failures are gap evidence, never a cycle crash.
                suite_error = f"catalog_evaluation_or_persistence_rejected:{type(exc).__name__}"
                break
            attested = (
                execution_result.get("independent_evidence")
                if isinstance(execution_result, Mapping)
                and isinstance(execution_result.get("independent_evidence"), Mapping)
                else {}
            )
            supplied_verifier = case_evidence.get("verifier") if isinstance(case_evidence.get("verifier"), Mapping) else None
            if supplied_verifier is not None and not _same_verifier(supplied_verifier, attested.get("verifier")):
                suite_error = "independent_verifier_does_not_match_durable_trace"
                break
            execution = execution_result.get("execution") if isinstance(execution_result, Mapping) else {}
            try:
                evaluation_artifact = record_hypothesis_evaluation_artifact(
                    runtime,
                    runtime_scope=scope,
                    hypothesis_id=str(item["hypothesis_id"]),
                    provider_binding_id=str(item["provider_binding_id"]),
                    execution=execution if isinstance(execution, Mapping) else {},
                    probe_id=str(case_evidence["probe_id"]),
                    trace_record_id=str(case_evidence["trace_record_id"]),
                    verifier={**dict(attested.get("verifier") or {}), "independent": True},
                    evaluation_run_id=str(getattr(execution_result.get("run"), "run_id", "")),
                    evaluation_observation_id=str(
                        getattr(execution_result.get("observation_receipt"), "entity_id", "")
                    ),
                    request_key=(
                        f"dynamic-capability-evaluation:{item['work_item_id']}:"
                        f"{str(case_evidence.get('case_id') or '')}:"
                        f"{str(case_evidence.get('probe_id') or '')}:"
                        f"{str(case_evidence.get('trace_record_id') or '')}:"
                        f"{str(execution.get('execution_digest') or '')}"
                    ),
                )
            except Exception as exc:
                suite_error = f"hypothesis_evaluation_artifact_rejected:{type(exc).__name__}"
                break
            evaluation_artifacts.append(evaluation_artifact)
            evaluated_cases.append(
                {
                    "case_id": str(case_evidence["case_id"]),
                    "execution": dict(execution) if isinstance(execution, Mapping) else {},
                    "evaluation_artifact_id": evaluation_artifact.record_id,
                    "evaluation_run_id": str(getattr(execution_result.get("run"), "run_id", "")),
                    "evaluation_observation_id": str(
                        getattr(execution_result.get("observation_receipt"), "entity_id", "")
                    ),
                    "trace_record_id": str(case_evidence["trace_record_id"]),
                    "verifier": {**dict(attested.get("verifier") or {}), "independent": True},
                }
            )
        if suite_error or len(evaluated_cases) != len(selected_cases):
            results.append(
                {
                    "work_item_id": item["work_item_id"],
                    "status": "blocked",
                    "reason": suite_error or "evaluation_suite_incomplete",
                    "evaluations": evaluated_cases,
                }
            )
            continue
        suite_passed = all(_execution_passed(entry.get("execution")) for entry in evaluated_cases)
        anchor = evaluated_cases[-1]
        try:
            feedback = record_hypothesis_experiment_feedback(
                runtime,
                runtime_scope=scope,
                hypothesis_id=str(item["hypothesis_id"]),
                artifact_type="evaluation",
                artifact_id=str(anchor["evaluation_artifact_id"]),
                verdict="pass" if suite_passed else "fail",
                verifier=dict(anchor["verifier"]),
                details={
                    "work_item_id": item["work_item_id"],
                    "evaluation_suite": True,
                    "case_ids": [str(entry["case_id"]) for entry in evaluated_cases],
                    "case_verdicts": [
                        "pass" if _execution_passed(entry.get("execution")) else "fail"
                        for entry in evaluated_cases
                    ],
                    "evaluation_artifact_ids": [entry["evaluation_artifact_id"] for entry in evaluated_cases],
                    "evidence_watermark": item["evidence_watermark"],
                },
                request_key=(
                    f"dynamic-capability-suite-feedback:{item['work_item_id']}:"
                    f"{_digest([entry['evaluation_artifact_id'] for entry in evaluated_cases])}"
                ),
            )
        except Exception as exc:
            results.append(
                {
                    "work_item_id": item["work_item_id"],
                    "status": "blocked",
                    "reason": "hypothesis_feedback_rejected",
                    "error_type": type(exc).__name__,
                    "evaluations": evaluated_cases,
                }
            )
            continue
        evidence_refresh = _refresh_dynamic_evidence_state(
            runtime,
            scope=scope,
            profile_key=profile_key,
            capability_scope=capability_scope,
            capability_id=str(item["capability_id"]),
            provider_binding_id=str(item["provider_binding_id"]),
            observation_limit=observation_limit,
        )
        if evidence_refresh.get("ok") is not True:
            results.append(
                {
                    "work_item_id": item["work_item_id"],
                    "status": "blocked",
                    "reason": str(evidence_refresh.get("reason") or "dynamic_evidence_projection_failed"),
                    "evaluations": evaluated_cases,
                    "feedback_id": feedback.record_id,
                    "evidence_refresh": evidence_refresh,
                }
            )
            continue
        gate = hypothesis_behavior_gate(
            runtime,
            runtime_scope=scope,
            hypothesis_id=str(item["hypothesis_id"]),
        )
        result: dict[str, Any] = {
            "work_item_id": item["work_item_id"],
            "status": "evaluated" if suite_passed and gate.get("allowed") else "blocked",
            "evaluations": evaluated_cases,
            "evaluation_artifact_ids": [artifact.record_id for artifact in evaluation_artifacts],
            "feedback_id": feedback.record_id,
            "suite_passed": suite_passed,
            "hypothesis_gate": gate,
            "evidence_refresh": evidence_refresh,
        }
        machine_policy = _trusted_machine_policy_for_item(runtime, item)
        result["automation_policy"] = machine_policy
        opportunity = candidate_opportunities.get(str(item["work_item_id"]))
        if opportunity is None and suite_passed and gate.get("allowed") and auto_propose_code_patch:
            if machine_policy.get("ok") is not True:
                proposed = None
                proposal_report = {
                    "status": "blocked",
                    "reason": str(machine_policy.get("reason") or "machine_policy_blocked"),
                    "automation_policy": machine_policy,
                }
                result.update(
                    {
                        "status": "blocked",
                        "reason": str(machine_policy.get("reason") or "machine_policy_blocked"),
                    }
                )
            else:
                proposed, proposal_report = _automatic_candidate_opportunity(
                    runtime,
                    scope=scope,
                    item=item,
                    gate=gate,
                    machine_policy_context=_machine_policy_context_for_item(item),
                )
                if proposed is None and str(proposal_report.get("status") or "") in {
                    "blocked",
                    "proposal_blocked",
                }:
                    result.update(
                        {
                            "status": "blocked",
                            "reason": str(proposal_report.get("reason") or "machine_proposal_blocked"),
                        }
                    )
            result["candidate_proposal"] = proposal_report
            opportunity = proposed
        if suite_passed and gate.get("allowed") and isinstance(opportunity, Mapping):
            if applied >= max(0, int(max_apply)):
                result.update({"status": "blocked", "reason": "machine_apply_budget_exhausted"})
            else:
                prepared, reason = _prepare_candidate_opportunity(
                    item,
                    opportunity,
                    gate,
                    automation_policy=machine_policy,
                )
                if prepared is None:
                    result.update({"status": "blocked", "reason": reason})
                else:
                    if prepared.get("opportunity_type") == "code_evolution_v2":
                        # Dynamic evaluation already supplied the independent
                        # catalog/hypothesis gates above.  Submit the strict
                        # provider proposal through the autonomous evolution
                        # helper, which delegates the actual candidate/effect
                        # ownership to promotion_manager.  Do not send this
                        # proposal through the legacy command-bearing gates.
                        from eimemory.governance.autonomous_evolution import (
                            _apply_safe_patch,
                            _safe_patch_from_opportunity,
                        )

                        strict_patch = _safe_patch_from_opportunity(
                            prepared,
                            scope=scope,
                        )
                        strict_patch["apply"] = bool(apply)
                        strict_result = _apply_safe_patch(
                            runtime,
                            strict_patch,
                            scope=asdict(scope),
                            legacy_compatibility=False,
                        )
                        strict_applied = bool(strict_result.get("applied"))
                        evolution = {
                            "schema": "autonomous_evolution.v1",
                            "ok": strict_applied,
                            "applied_count": 1 if strict_applied else 0,
                            "applied_patches": [strict_result] if strict_applied else [],
                            "blocked_patches": [] if strict_applied else [strict_result],
                            "dynamic_governance": {
                                "independent_suite_passed": True,
                                "hypothesis_gate": dict(gate),
                                "transaction_owner": "promotion_manager",
                            },
                        }
                    else:
                        evolution = run_autonomous_evolution(
                            runtime,
                            scope=scope,
                            opportunities=[prepared],
                            mine_events=False,
                            apply=bool(apply),
                            max_apply=1,
                            persist_report=True,
                        )
                    result["evolution"] = evolution
                    candidate_feedback = _record_dynamic_candidate_feedback(
                        runtime,
                        scope=scope,
                        item=item,
                        evolution=evolution,
                    )
                    result["candidate_feedback"] = candidate_feedback
                    if int(evolution.get("applied_count") or 0) > 0:
                        applied += 1
                        result["status"] = "applied"
                    else:
                        result["status"] = "blocked"
                        result["reason"] = "machine_rollout_gate_reject"
        results.append(result)
    material = {
        "schema": DYNAMIC_EVOLUTION_SCHEMA,
        "profile_key": str(profile_key),
        "capability_scope": capability_scope,
        "runtime_scope": asdict(scope),
        "plan_digest": plan["plan_digest"],
        "results": results,
        "evidence_collection": evidence_collection or {},
        "auto_collect_independent_evidence": bool(auto_collect_independent_evidence),
        "auto_propose_code_patch": bool(auto_propose_code_patch),
    }
    return {
        **material,
        "ok": all(item.get("status") in {"evaluated", "applied"} for item in results) if results else False,
        "applied_count": applied,
        "execution_digest": _digest(material),
    }


def _case_index(raw_cases: object) -> dict[tuple[str, str, str], tuple[dict[str, Any], ...]]:
    grouped: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    for raw in raw_cases if isinstance(raw_cases, Sequence) and not isinstance(raw_cases, (str, bytes)) else ():
        if not isinstance(raw, Mapping):
            continue
        artifact = raw.get("artifact") if isinstance(raw.get("artifact"), Mapping) else {}
        target = raw.get("target") if isinstance(raw.get("target"), Mapping) else {}
        key = (
            str(target.get("capability_id") or artifact.get("capability") or ""),
            str(target.get("capability_revision_id") or ""),
            str(target.get("provider_binding_id") or ""),
        )
        if all(key):
            grouped.setdefault(key, []).append({"artifact": dict(artifact), "target": dict(target)})
    return {key: tuple(sorted(values, key=lambda item: str(item["artifact"].get("case_id") or ""))) for key, values in grouped.items()}


def _ordered_cases(raw_cases: object) -> list[dict[str, Any]]:
    """Normalize one exact, bounded suite without inferred fallback cases."""

    values = raw_cases if isinstance(raw_cases, Sequence) and not isinstance(raw_cases, (str, bytes)) else ()
    cases: list[dict[str, Any]] = []
    for raw in values:
        if not isinstance(raw, Mapping):
            continue
        artifact = raw.get("artifact") if isinstance(raw.get("artifact"), Mapping) else None
        target = raw.get("target") if isinstance(raw.get("target"), Mapping) else None
        case_id = str((artifact or {}).get("case_id") or "").strip()
        if artifact is None or target is None or not case_id:
            continue
        cases.append({"artifact": dict(artifact), "target": dict(target)})
    case_ids = [str(case["artifact"].get("case_id") or "") for case in cases]
    if len(cases) > _MAX_WORK_ITEMS or len(case_ids) != len(set(case_ids)):
        return []
    return sorted(cases, key=lambda case: str(case["artifact"].get("case_id") or ""))


def _work_item_hypothesis_context(
    item: Mapping[str, Any],
    *,
    cases: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Build the sole provenance context permitted for a plan item's probes."""

    required = {
        "hypothesis_id": str(item.get("hypothesis_id") or "").strip(),
        "link_id": str(item.get("hypothesis_link_id") or "").strip(),
        "link_digest": str(item.get("hypothesis_link_digest") or "").strip(),
        "capability_id": str(item.get("capability_id") or "").strip(),
        "capability_revision_id": str(item.get("capability_revision_id") or "").strip(),
        "provider_binding_id": str(item.get("provider_binding_id") or "").strip(),
        "capability_scope": str(item.get("capability_scope") or "").strip(),
    }
    if not all(required.values()):
        raise DynamicCapabilityEvolutionError("plan_hypothesis_context_incomplete")
    bounds = item.get("candidate_bounds")
    try:
        normalized_bounds = normalize_json_payload(
            bounds if isinstance(bounds, Mapping) else {},
            field="dynamic_capability_evolution.candidate_bounds",
            reject_executable=True,
        )
    except CapabilityContractError as exc:
        raise DynamicCapabilityEvolutionError("plan_candidate_bounds_invalid") from exc
    if not normalized_bounds:
        raise DynamicCapabilityEvolutionError("plan_candidate_bounds_missing")
    replay_case_ids = [
        str((case.get("artifact") or {}).get("case_id") or "").strip()
        for case in cases
        if isinstance(case, Mapping)
    ]
    if not replay_case_ids or any(not value for value in replay_case_ids):
        raise DynamicCapabilityEvolutionError("plan_evaluation_case_ids_invalid")
    return {**required, "candidate_bounds": normalized_bounds, "replay_case_ids": sorted(replay_case_ids)}


def _match_independent_evidence(
    item: Mapping[str, Any],
    evidence: Mapping[str, Any],
) -> tuple[list[tuple[dict[str, Any], dict[str, Any]]], str]:
    """Require one durable evidence tuple for every exact selected case."""

    cases = _ordered_cases(item.get("evaluation_cases"))
    if not cases:
        return [], "plan_has_no_exact_evaluation_cases"
    expected = {str(case["artifact"].get("case_id") or ""): case for case in cases}
    raw_cases = evidence.get("cases")
    if raw_cases is None:
        raw_values: Sequence[object] = (evidence,)
    elif isinstance(raw_cases, Sequence) and not isinstance(raw_cases, (str, bytes)):
        raw_values = raw_cases
    else:
        return [], "independent_evidence_cases_invalid"
    if not raw_values or len(raw_values) > _MAX_WORK_ITEMS:
        return [], "independent_evidence_cases_invalid"
    evidence_by_case: dict[str, dict[str, Any]] = {}
    for raw in raw_values:
        if not isinstance(raw, Mapping):
            return [], "independent_evidence_case_invalid"
        case_id = str(raw.get("case_id") or "").strip()
        if not case_id or case_id not in expected or case_id in evidence_by_case:
            return [], "independent_evidence_case_selection_mismatch"
        required = ("evidence_ref", "probe_id", "trace_record_id", "execution_id")
        missing = [key for key in required if not str(raw.get(key) or "").strip()]
        if missing:
            return [], f"independent_evidence_fields_missing:{','.join(missing)}"
        normalized = {key: str(raw.get(key) or "").strip() for key in required}
        normalized["case_id"] = case_id
        if isinstance(raw.get("verifier"), Mapping):
            normalized["verifier"] = dict(raw["verifier"])
        evidence_by_case[case_id] = normalized
    if set(evidence_by_case) != set(expected):
        return [], "independent_evidence_suite_incomplete"
    return [(expected[case_id], evidence_by_case[case_id]) for case_id in sorted(expected)], ""


def _execution_passed(execution: object) -> bool:
    if not isinstance(execution, Mapping):
        return False
    if execution.get("passed") is True:
        return True
    return str(execution.get("verdict") or "").strip().lower() in {"pass", "passed", "ok", "success"}


def _blocked_item(
    blocked: Mapping[str, Any],
    *,
    reason: str,
    projection: Mapping[str, Any],
    detail: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    material = {
        "capability_id": str(blocked.get("capability_id") or ""),
        "capability_revision_id": str(blocked.get("capability_revision_id") or ""),
        "provider_binding_id": str(blocked.get("provider_binding_id") or ""),
        "reason": reason,
        "projection_digest": str(projection.get("projection_digest") or ""),
    }
    return {
        "work_item_id": _digest(material)[:40],
        "status": "blocked",
        **material,
        "detail": dict(detail or {}),
    }


def _select_case(raw_cases: object, evidence: Mapping[str, Any]) -> dict[str, Any] | None:
    requested = str(evidence.get("case_id") or "")
    cases = [dict(item) for item in raw_cases if isinstance(item, Mapping)] if isinstance(raw_cases, Sequence) else []
    if requested:
        selected = [item for item in cases if str((item.get("artifact") or {}).get("case_id") or "") == requested]
        return selected[0] if len(selected) == 1 else None
    return cases[0] if len(cases) == 1 else None


def _machine_policy_context_for_item(item: Mapping[str, Any]) -> dict[str, str]:
    return machine_policy_context_from_mapping(
        {
            "profile_key": item.get("profile_key"),
            "capability_id": item.get("capability_id"),
            "capability_revision_id": item.get("capability_revision_id"),
            "capability_scope": item.get("capability_scope"),
            "provider_binding_id": item.get("provider_binding_id"),
        }
    )


def _trusted_machine_policy_for_item(runtime: Any, item: Mapping[str, Any]) -> dict[str, Any]:
    """Resolve policy only through the deployment-controlled resolver."""
    context = _machine_policy_context_for_item(item)
    resolver = getattr(runtime, "load_code_automation_policy", None)
    if not callable(resolver):
        return code_automation_policy_summary(
            {"ok": False, "status": "blocked", "reason": "machine_policy_runtime_resolver_missing"}
        )
    try:
        loaded = resolver(**context)
    except Exception:
        return code_automation_policy_summary(
            {"ok": False, "status": "blocked", "reason": "machine_policy_resolver_failed"}
        )
    return code_automation_policy_summary(loaded if isinstance(loaded, Mapping) else {})


def _prepare_candidate_opportunity(
    item: Mapping[str, Any],
    raw: Mapping[str, Any],
    gate: Mapping[str, Any],
    *,
    automation_policy: Mapping[str, Any],
) -> tuple[dict[str, Any] | None, str]:
    """Bind a code candidate to exact dynamic evidence before delegation."""

    strict_proposal = _strict_code_evolution_proposal(raw)
    if strict_proposal is not None:
        if str(item.get("capability_id") or "") != "code.implementation":
            return None, "code_evolution_v2_capability_mismatch"
        if str(item.get("capability_revision_id") or "") != "code.implementation:v8":
            return None, "code_evolution_v2_revision_mismatch"
        if str(item.get("provider_binding_id") or "") != "binding.hermes.code-implementation:v8":
            return None, "code_evolution_v2_binding_mismatch"
        provider = strict_proposal.get("provider") if isinstance(strict_proposal.get("provider"), Mapping) else {}
        expected_provider = {
            "capability_id": "code.implementation",
            "revision_id": "code.implementation:v8",
            "binding_id": "binding.hermes.code-implementation:v8",
        }
        if any(str(provider.get(key) or "") != value for key, value in expected_provider.items()):
            return None, "code_evolution_v2_provider_coordinates_mismatch"
        if str(strict_proposal.get("transaction_id") or "").strip() == "":
            return None, "code_evolution_v2_transaction_id_missing"
        if strict_proposal.get("proposal_only") is not True:
            return None, "code_evolution_v2_proposal_only_required"
        if not isinstance(strict_proposal.get("file_updates"), list):
            return None, "code_evolution_v2_file_updates_missing"
        if any(key in strict_proposal for key in ("commands", "verification_commands", "argv", "environment", "secrets")):
            return None, "code_evolution_v2_execution_authority_forbidden"
        return (
            {
                "opportunity_id": f"dynamic-capability-{item['work_item_id']}",
                "opportunity_type": "code_evolution_v2",
                "source": "eimemory.dynamic_capability_evolution",
                "risk_level": str(raw.get("risk_level") or "medium"),
                "policy_update": str(raw.get("summary") or "Dynamic capability code-evolution proposal"),
                "code_evolution_proposal": strict_proposal,
            },
            "",
        )

    code_patch = raw.get("code_patch") if isinstance(raw.get("code_patch"), Mapping) else {}
    if not code_patch:
        return None, "bounded_code_patch_missing"
    for key in ("repo_root", "allowed_files", "file_updates", "verification_commands"):
        if not code_patch.get(key):
            return None, f"bounded_code_patch_missing:{key}"
    declared_revision = str(raw.get("capability_revision_id") or item.get("capability_revision_id") or "")
    if declared_revision != str(item.get("capability_revision_id") or ""):
        return None, "candidate_revision_mismatch"
    if str(raw.get("evidence_watermark") or "") != str(item.get("evidence_watermark") or ""):
        return None, "candidate_evidence_watermark_mismatch"
    expected = raw.get("expected_metric") if isinstance(raw.get("expected_metric"), Mapping) else {}
    if expected != (item.get("expected_metric") if isinstance(item.get("expected_metric"), Mapping) else {}):
        return None, "candidate_expected_metric_mismatch"
    hypothesis_context = {
        "hypothesis_id": str(item.get("hypothesis_id") or ""),
        "link_id": str(item.get("hypothesis_link_id") or ""),
        "link_digest": str(item.get("hypothesis_link_digest") or ""),
        "capability_id": str(item.get("capability_id") or ""),
        "capability_revision_id": str(item.get("capability_revision_id") or ""),
        "provider_binding_id": str(item.get("provider_binding_id") or ""),
        "capability_scope": str(item.get("capability_scope") or ""),
    }
    if not all(hypothesis_context.values()):
        return None, "candidate_hypothesis_context_incomplete"
    supplied_context = raw.get("capability_hypothesis")
    if not isinstance(supplied_context, Mapping):
        supplied_context = code_patch.get("capability_hypothesis")
    if isinstance(supplied_context, Mapping):
        for key, value in hypothesis_context.items():
            if str(supplied_context.get(key) or "") and str(supplied_context.get(key) or "") != value:
                return None, f"candidate_hypothesis_{key}_mismatch"
    expected_bounds = dict(item.get("candidate_bounds") or {})
    supplied_bounds = raw.get("candidate_bounds") if isinstance(raw.get("candidate_bounds"), Mapping) else code_patch.get("candidate_bounds")
    if supplied_bounds is not None and dict(supplied_bounds) != expected_bounds:
        return None, "candidate_bounds_mismatch"
    selected_case_ids = sorted(
        {
            str((case.get("artifact") or {}).get("case_id") or "")
            for case in item.get("evaluation_cases") or ()
            if isinstance(case, Mapping) and str((case.get("artifact") or {}).get("case_id") or "")
        }
    )
    supplied_case_ids = raw.get("replay_case_ids")
    if not isinstance(supplied_case_ids, Sequence) or isinstance(supplied_case_ids, (str, bytes)):
        supplied_case_ids = code_patch.get("replay_case_ids")
    if supplied_case_ids is not None:
        normalized_supplied_case_ids = sorted({str(value or "") for value in supplied_case_ids if str(value or "")}) if isinstance(supplied_case_ids, Sequence) and not isinstance(supplied_case_ids, (str, bytes)) else []
        if normalized_supplied_case_ids != selected_case_ids:
            return None, "candidate_replay_case_ids_mismatch"
    requested_machine_actions, action_error = _requested_machine_actions(code_patch)
    if action_error:
        return None, action_error
    policy_error = _machine_policy_action_error(automation_policy, requested_machine_actions)
    if policy_error:
        return None, policy_error
    # Proposal and incident payloads are untrusted data.  Persist at most the
    # fixed summary derived by the environment loader; never carry a claimed
    # policy, review state, or requested action list into a promotion record.
    sanitized_code_patch = {
        key: value
        for key, value in dict(code_patch).items()
        if key
        not in {
            "automation_policy",
            "machine_policy",
            "policy",
            "requested_machine_actions",
            "operator_approval_path",
            "operator_approval",
            "approval",
            "approval_status",
            "review_status",
        }
    }
    return (
        {
            "opportunity_id": f"dynamic-capability-{item['work_item_id']}",
            "opportunity_type": "code_patch",
            "source": "eimemory.dynamic_capability_evolution",
            "risk_level": str(raw.get("risk_level") or "medium"),
            "policy_update": str(raw.get("summary") or "Dynamic capability improvement"),
            "source_event_payload": {
                "capability_id": item["capability_id"],
                "capability_revision_id": item["capability_revision_id"],
                "provider_binding_id": item["provider_binding_id"],
                "profile_key": item["profile_key"],
                "evidence_watermark": item["evidence_watermark"],
                "hypothesis_id": item["hypothesis_id"],
                "hypothesis_link_id": item["hypothesis_link_id"],
                "hypothesis_link_digest": item["hypothesis_link_digest"],
                "hypothesis_gate": dict(gate),
                "expected_metric": dict(item.get("expected_metric") or {}),
                "candidate_bounds": expected_bounds,
                "replay_case_ids": selected_case_ids,
            },
            "source_outcome_payload": {"independent_hypothesis_gate": dict(gate)},
            "code_patch": {
                **sanitized_code_patch,
                "target_capability": item["capability_id"],
                "capability_revision_id": item["capability_revision_id"],
                "provider_binding_id": item["provider_binding_id"],
                "profile_key": item["profile_key"],
                "capability_scope": item["capability_scope"],
                "evidence_watermark": item["evidence_watermark"],
                "expected_metric": dict(item.get("expected_metric") or {}),
                "candidate_bounds": expected_bounds,
                "replay_case_ids": selected_case_ids,
                "capability_hypothesis": hypothesis_context,
                # Bind the proposal to the independent evidence that was
                # current when it was generated.  Promotion rechecks this
                # exact feedback id, rather than trusting a candidate claim
                # or comparing volatile projector watermarks.
                "capability_hypothesis_gate": dict(gate),
                "automation_policy": code_automation_policy_summary(automation_policy),
                "requested_machine_actions": requested_machine_actions,
            },
        },
        "",
    )


def _strict_code_evolution_proposal(raw: Mapping[str, Any]) -> dict[str, Any] | None:
    """Extract only an already validated v2 proposal; never synthesize one."""

    for key in ("code_evolution_proposal", "proposal"):
        value = raw.get(key)
        if isinstance(value, Mapping) and str(value.get("schema_version") or "") == "code_implementation_proposal.v2":
            return dict(value)
    value = raw.get("code_patch")
    if isinstance(value, Mapping) and str(value.get("schema_version") or "") == "code_implementation_proposal.v2":
        return dict(value)
    return None


def _automatic_candidate_opportunity(
    runtime: Any,
    *,
    scope: ScopeRef,
    item: Mapping[str, Any],
    gate: Mapping[str, Any],
    machine_policy_context: Mapping[str, Any],
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    """Require a provider-owned v2 proposal context for automatic submission.

    Dynamic evaluation can establish that a hypothesis is worth investigating,
    but it cannot invent the transaction/base tree/request coordinates needed
    by the Hermes provider.  A strict proposal supplied by that provider is
    accepted by ``_prepare_candidate_opportunity`` below; the old generic
    proposer and its command-bearing payload are not an alternate route.
    """

    return None, {
        "status": "blocked",
        "reason": "code_implementation_v2_provider_context_required",
    }


def _requested_machine_actions(code_patch: Mapping[str, Any]) -> tuple[list[str], str]:
    """Require each machine effect to be an explicit boolean request."""

    if code_patch.get("apply_to_repo") is not True:
        return [], "bounded_code_patch_requires_apply_to_repo"
    actions = ["local_apply"]
    for field, action in (
        ("commit_to_repo", "commit"),
        ("deploy_to_production", "deployment"),
    ):
        value = code_patch.get(field, False)
        if not isinstance(value, bool):
            return [], f"bounded_code_patch_{field}_must_be_boolean"
        if value:
            actions.append(action)
    if "deployment" in actions and "commit" not in actions:
        return [], "bounded_code_patch_deployment_requires_commit"
    return actions, ""


def _machine_policy_action_error(
    automation_policy: Mapping[str, Any],
    requested_machine_actions: Sequence[str],
) -> str:
    """Reject side effects unless the trusted policy enables each one."""

    summary = code_automation_policy_summary(automation_policy)
    if summary.get("ok") is not True:
        return str(summary.get("reason") or "machine_policy_blocked")
    actions = summary.get("actions") if isinstance(summary.get("actions"), Mapping) else {}
    for action in requested_machine_actions:
        if action not in {"local_apply", "commit", "deployment"}:
            return "machine_policy_action_unknown"
        if actions.get(action) is not True:
            return f"machine_policy_{action}_not_enabled"
    return ""


def _bounded_text_list(value: object, *, maximum: int) -> list[str]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        return []
    result: list[str] = []
    for raw in value:
        text = str(raw or "").strip().replace("\\", "/")
        if not text or text in result:
            continue
        result.append(text)
        if len(result) > maximum:
            return []
    return result


def _digest(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return sha256(encoded.encode("utf-8")).hexdigest()


def _same_verifier(left: Mapping[str, Any], right: object) -> bool:
    """Require the caller's verifier identity to equal the durable trace one."""

    if not isinstance(right, Mapping):
        return False
    for key in ("id", "revision", "contract_digest"):
        if str(left.get(key) or "").strip() != str(right.get(key) or "").strip():
            return False
    return left.get("independent") is True


def _refresh_dynamic_evidence_state(
    runtime: Any,
    *,
    scope: ScopeRef,
    profile_key: str,
    capability_scope: str,
    capability_id: str,
    provider_binding_id: str,
    observation_limit: int,
) -> dict[str, Any]:
    """Project newly persisted independent evidence before any evolution step.

    The execution catalog owns the immutable run/observation write.  This
    bridge owns the subsequent bounded projection: a passing case cannot be
    used to create a patch opportunity until the exact Profile state and L5
    assessment have consumed it; a failing case receives the same treatment.
    """

    try:
        projector = CapabilityStateProjector(runtime.store)
        projection_result = projector.project_affected(
            str(profile_key),
            runtime_scope=scope,
            capability_scope=capability_scope,
            affected_capability_ids=[capability_id],
            max_candidates=100,
            observation_limit=max(1, min(int(observation_limit), 500)),
            persist=True,
        )
        projection = (
            projection_result.to_dict()
            if callable(getattr(projection_result, "to_dict", None))
            else dict(projection_result)
        )
    except Exception as exc:
        return {
            "ok": False,
            "reason": f"dynamic_evidence_projection_failed:{type(exc).__name__}",
            "capability_id": capability_id,
            "provider_binding_id": provider_binding_id,
        }
    try:
        from eimemory.governance.l5_assessment_v3 import build_l5_assessment_v3

        assessment = build_l5_assessment_v3(
            runtime,
            profile_key=str(profile_key),
            scope=scope,
            capability_scope=capability_scope,
            persist=True,
            max_candidates=100,
            observation_limit=max(1, min(int(observation_limit), 500)),
        )
    except Exception as exc:
        return {
            "ok": False,
            "reason": f"dynamic_evidence_l5_assessment_failed:{type(exc).__name__}",
            "capability_id": capability_id,
            "provider_binding_id": provider_binding_id,
            "projection": projection,
        }
    return {
        "ok": True,
        "capability_id": capability_id,
        "provider_binding_id": provider_binding_id,
        "projection": projection,
        "assessment": assessment,
    }


def _record_dynamic_candidate_feedback(
    runtime: Any,
    *,
    scope: ScopeRef,
    item: Mapping[str, Any],
    evolution: Mapping[str, Any],
) -> dict[str, Any]:
    """Record bounded-candidate evidence without turning it into self-grading.

    The candidate is created by the ordinary autonomous-evolution pipeline;
    this bridge accepts it only when the separately persisted isolated
    evaluator record proves generator/evaluator separation.  A successful
    repository mutation still leaves the old binding stale until a new exact
    advertisement arrives, so this feedback cannot inherit old L5 evidence.
    """

    candidate = _candidate_result_for_dynamic_opportunity(
        evolution,
        opportunity_id=f"dynamic-capability-{str(item.get('work_item_id') or '')}",
    )
    candidate_id = str(candidate.get("candidate_id") or "")
    if not candidate_id:
        return {"ok": False, "status": "skipped", "reason": "candidate_not_created"}
    isolated = candidate.get("isolated_evaluator") if isinstance(candidate.get("isolated_evaluator"), Mapping) else {}
    verifier = _isolated_evaluator_verifier(
        runtime,
        scope=scope,
        verdict_id=str(isolated.get("verdict_id") or ""),
    )
    if verifier is None:
        return {"ok": False, "status": "blocked", "reason": "isolated_verifier_not_independent", "candidate_id": candidate_id}
    verdict = "pass" if bool(candidate.get("applied")) else "fail"
    try:
        feedback = record_hypothesis_experiment_feedback(
            runtime,
            runtime_scope=scope,
            hypothesis_id=str(item.get("hypothesis_id") or ""),
            artifact_type="bounded_candidate",
            artifact_id=candidate_id,
            verdict=verdict,
            verifier=verifier,
            details={
                "work_item_id": str(item.get("work_item_id") or ""),
                "opportunity_id": str(candidate.get("opportunity_id") or ""),
                "promotion_id": str(candidate.get("promotion_id") or ""),
                "rollout_ledger_id": str(candidate.get("rollout_ledger_id") or ""),
                "binding_invalidation": (
                    dict(candidate.get("binding_invalidation") or {})
                    if isinstance(candidate.get("binding_invalidation"), Mapping)
                    else {}
                ),
            },
            request_key=f"dynamic-capability-candidate-feedback:{item.get('work_item_id')}:{candidate_id}",
        )
    except Exception as exc:
        return {
            "ok": False,
            "status": "blocked",
            "reason": "candidate_feedback_rejected",
            "error_type": type(exc).__name__,
            "candidate_id": candidate_id,
        }
    return {
        "ok": True,
        "status": "recorded",
        "candidate_id": candidate_id,
        "feedback_id": feedback.record_id,
        "verdict": verdict,
    }


def _candidate_result_for_dynamic_opportunity(
    evolution: Mapping[str, Any],
    *,
    opportunity_id: str,
) -> dict[str, Any]:
    for entry in evolution.get("applied_patches") or ():
        if isinstance(entry, Mapping) and str(entry.get("opportunity_id") or "") == opportunity_id:
            return dict(entry)
    for entry in evolution.get("blocked_patches") or ():
        if not isinstance(entry, Mapping) or str(entry.get("opportunity_id") or "") != opportunity_id:
            continue
        applied = entry.get("apply_result") if isinstance(entry.get("apply_result"), Mapping) else {}
        if applied:
            return dict(applied)
    return {}


def _isolated_evaluator_verifier(
    runtime: Any,
    *,
    scope: ScopeRef,
    verdict_id: str,
) -> dict[str, Any] | None:
    if not verdict_id:
        return None
    getter = getattr(getattr(runtime, "store", None), "get_by_id", None)
    if not callable(getter):
        return None
    try:
        verdict = getter(verdict_id, scope=scope)
    except Exception:
        return None
    if verdict is None or str(getattr(verdict, "kind", "") or "") != "evaluator_verdict":
        return None
    if str(getattr(verdict, "source", "") or "") != "eimemory.isolated_evaluator":
        return None
    content = verdict.content if isinstance(getattr(verdict, "content", None), Mapping) else {}
    roles = content.get("model_roles") if isinstance(content.get("model_roles"), Mapping) else {}
    generator = str(roles.get("generator_model") or "").strip()
    evaluator = str(roles.get("evaluator_model") or "").strip()
    if not generator or not evaluator or generator == evaluator:
        return None
    revision = str(content.get("schema_version") or "isolated_evaluator.v1")
    return {
        "id": "isolated_evaluator",
        "revision": revision,
        "contract_digest": _digest(
            {
                "verdict_id": verdict.record_id,
                "verdict": str(content.get("verdict") or ""),
                "packet_id": str(content.get("packet_id") or ""),
                "roles": {"generator_model": generator, "evaluator_model": evaluator},
                "blocked_reasons": list(content.get("blocked_reasons") or ()),
            }
        ),
        "independent": True,
    }


__all__ = [
    "DYNAMIC_EVOLUTION_SCHEMA",
    "DynamicCapabilityEvolutionError",
    "build_dynamic_capability_evolution_plan",
    "collect_dynamic_capability_independent_evidence",
    "execute_dynamic_capability_evolution",
]
