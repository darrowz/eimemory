"""Pure nightly result semantics, independent of Runtime and storage.

Job execution success and evaluation readiness are different claims. Known
non-actionable evidence waits stay non-fatal; explicit execution errors and
malformed present reports must not be promoted to success.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from hashlib import sha256
from math import isfinite

def _nightly_step(steps: list[dict], name: str, fn):
    """Run one nightly step; record success/failure without aborting the batch (EXT-05).

    SCH-01: missing ``ok`` or a non-dict/non-list result is unknown/failure — never coerce True.
    A bare list is normalized to ``{ok: True, items: list, count}`` so empty successful
    producers (e.g. replay with no datasets) do not false-fail aggregation.
    """
    try:
        result = fn()
        if isinstance(result, list):
            result = {"ok": True, "items": result, "reports": result, "count": len(result)}
        if not isinstance(result, dict):
            ok = False
            error = "step_result_not_dict"
            result = {"ok": False, "error": error, "raw_type": type(result).__name__}
        elif "ok" not in result:
            ok = False
            error = "step_ok_missing"
            result = {**result, "ok": False, "error": error}
        elif result.get("ok") is False:
            # Keep the evaluator's false bit. Only the scheduler's execution
            # outcome can be a non-fatal evidence wait; it is not acceptance.
            ok = _non_actionable_step_wait(name, result)
            error = "" if ok else str(result.get("error") or result.get("blocked_reason") or "step_reported_not_ok")
        elif result.get("ok") is True:
            # A successful wrapper cannot hide failed producer reports.
            ok = not (_has_report_failure(result) or _authority_boundary_failed(result))
            error = "" if ok else "nested_execution_failure"
            if not ok:
                result = {**result, "ok": False, "error": error}
        else:
            # Non-boolean ok is unknown → fail closed
            ok = False
            error = "step_ok_not_boolean"
            result = {**result, "ok": False, "error": error}
    except Exception as exc:  # noqa: BLE001 - nightly continues; report aggregates failures
        result = {"ok": False, "error": f"{type(exc).__name__}:{exc}"}
        ok = False
        error = f"{type(exc).__name__}"
    steps.append({"step": name, "ok": ok, "error": error,
                  "execution_ok": ok,
                  "evaluation_status": "awaiting_evidence" if ok and _non_actionable_step_wait(name, result) else
                                       "completed" if ok else "failed"})
    return result



NIGHTLY_NESTED_OK_ALLOWLIST = (
    "roi",
    "memory_quality",
    "memory_quality_repair",
    "source_expansion",
    "news_source_promotion",
    "external_collection",
    "paper_promotion",
    "operational_projection",
    "research_digest",
    "research_closure_review",
    "daily_brief",
    "rule_evolution",
    "autonomous_evolution",
    "autonomous_learning",
    "autonomous_learning_daily_report",
    "autonomous_learning_dashboard",
    "real_effect_daily_report",
    "real_effect_hypotheses",
    "real_effect_cycle",
    "l5_loop",
    "capability_v3_backfill",
    "capability_v3_dual_write",
    "l5_v3_shadow",
    "l5_v3_reconcile",
    "code_evolution",
    "capability_incubation",
    "capability_hypothesis_producer",
    "dynamic_capability_evolution",
    "outcome_evolution",
    "storage_maintenance",
    "promotion_watch_orphans",
    "memory_eval_ci",
    "production_recall",
    "recall_quality_gate",
    "quality_gap_intake",
    "judgment_evaluation",
    "source_discovery",
    "knowledge_refresh",
)


def _has_execution_failure(report: dict) -> bool:
    # A wait flag is not permission to suppress an independently reported
    # execution failure. Keep field semantics explicit, not substring guesses.
    return bool(report.get("error") or report.get("errors") or report.get("blocking_metrics"))


def _has_report_failure(report: dict, *, depth: int = 0) -> bool:
    """Inspect explicit report collections, not arbitrary nested business data.

    Empty lists and data-only items remain valid. An explicit false/non-boolean
    ok or error in a child report must not become success through list wrapping.
    Bound recursive wrappers and reject cycles rather than recurse forever.
    """
    if depth >= 16 or _has_execution_failure(report):
        return True
    for key in ("reports", "items"):
        children = report.get(key)
        if not isinstance(children, list):
            continue
        for child in children:
            if not isinstance(child, dict):
                continue
            if "ok" in child and child["ok"] is not True:
                return True
            if _has_report_failure(child, depth=depth + 1):
                return True
    return False


def _quality_wait_is_non_actionable(gate: dict) -> bool:
    """Known-item smoke cannot certify quality. That wait is not a job failure."""
    return (
        str(gate.get("blocked_reason") or "") == "recall_quality_evidence_incomplete"
        and not _has_report_failure(gate)
        and not _authority_boundary_failed(gate)
        and (not isinstance(gate.get("recall_quality_evidence"), dict)
             or gate["recall_quality_evidence"].get("execution_ok") is True)
        and gate.get("ok") is False
    )


def _l5_awaiting_evidence_is_non_actionable(nested: dict) -> bool:
    """Tip-safety not_ready / sample-starved L5 waits must not fail nightly exit."""
    prompt = nested.get("prompt_safety") if isinstance(nested.get("prompt_safety"), dict) else {}
    assessment = nested.get("assessment") if isinstance(nested.get("assessment"), dict) else {}
    if _authority_boundary_failed(nested) or any(_has_report_failure(part) for part in (nested, prompt, assessment)):
        return False
    if nested.get("awaiting_evidence") is True:
        return True
    if prompt.get("awaiting_evidence") is True or str(prompt.get("status") or "") == "not_ready":
        return True
    missing = assessment.get("missing_evidence") if isinstance(assessment.get("missing_evidence"), list) else []
    if missing and all(
        str(item).endswith(":awaiting_evidence")
        or str(item) in {
            "prompt_safety:awaiting_evidence",
        }
        for item in missing
    ):
        return True
    reason = str(nested.get("blocked_reason") or nested.get("l5_skipped_reason") or "")
    return reason in {
        "tip_safety_not_ready",
        "prompt_safety_not_ready",
        "awaiting_evidence",
    }


def _aggregate_nightly_ok(report: dict, step_reports: list[dict]) -> bool:
    """Top-level ok aggregates step_reports and critical nested ok fields (BC-01)."""
    # SCH-01: missing step ok is unknown → fail (do not default True)
    if step_reports and not all(isinstance(step, dict) and step.get("ok") is True for step in step_reports):
        return False
    for key in NIGHTLY_NESTED_OK_ALLOWLIST:
        nested = report.get(key)
        # A section absent from a partial report is not evidence of execution.
        # Preserve optional None sections, but reject malformed present reports.
        if nested is None:
            continue
        if not isinstance(nested, dict) or type(nested.get("ok")) is not bool:
            return False
        if nested["ok"] is True:
            if _has_report_failure(nested) or _authority_boundary_failed(nested):
                return False
            continue
        if _non_actionable_step_wait(key, nested):
            continue
        return False
    knowledge = report.get("knowledge")
    if isinstance(knowledge, dict):
        refresh_status = str(knowledge.get("refresh_status") or "ok")
        if refresh_status not in {"ok", "recovered"} and knowledge.get("retry_required"):
            # retry_required alone is informational; only fail when nested ok says so
            pass
    return True


_AUTHORITY_FAILURES = frozenset({
    "terminal_transaction_lineage_mismatch", "quality_repair_release_unbound",
    "current_lineage_incompatible", "pending_release_authority_mismatch",
    "release_source_mismatch", "release_source_validation_failed",
    "provider_binding_mismatch", "binding_mismatch", "implementation_digest_mismatch",
    "deployment_identity_unverified", "acceptance_runtime_not_current_immutable_release",
})


def _authority_boundary_failed(report: dict, *, depth: int = 0) -> bool:
    """Identity/source failures may never hide behind an awaiting-evidence bit."""
    if depth >= 8:
        return True
    values = [report.get(k) for k in ("error", "reason", "blocked_reason", "l5_skipped_reason")]
    for key in ("blocked_reasons", "missing_evidence"):
        raw = report.get(key)
        if isinstance(raw, list):
            values.extend(raw)
    if any(isinstance(v, str) and any(v == code or v.endswith(":" + code)
                                     for code in _AUTHORITY_FAILURES) for v in values):
        return True
    for key in ("source_validation", "release_source_validation", "release_lineage", "binding", "provider_binding"):
        item = report.get(key)
        if isinstance(item, dict) and item.get("ok") is False:
            return True
    return any(_authority_boundary_failed(report[key], depth=depth + 1)
               for key in ("assessment", "prompt_safety") if isinstance(report.get(key), dict))


def _non_actionable_step_wait(name: str, result: dict) -> bool:
    if _authority_boundary_failed(result) or _has_report_failure(result):
        return False
    if name == "recall_quality_gate":
        return _quality_wait_is_non_actionable(result)
    if name == "l5_loop":
        return _l5_awaiting_evidence_is_non_actionable(result)
    if name == "dynamic_capability_evolution":
        return _dynamic_evolution_awaiting_hypothesis_is_non_actionable(result)
    if name == "memory_eval_ci":
        return (result.get("ok") is True and result.get("execution_ok") is True
                and result.get("memory_benchmark_status") == "not_run"
                and result.get("memory_benchmark_accepted") is False
                and result.get("passed_threshold") is False
                and type(result.get("retrieval_case_count")) is int
                and result["retrieval_case_count"] == 0
                and result.get("blocked_reason") == "memory_eval_dataset_empty")
    if name == "production_recall":
        quality = result.get("quality_gate")
        # Only the evaluator can attest that execution completed. Missing this
        # field (e.g. legacy transport errors) remains a failure, not a guess.
        return (result.get("execution_ok") is True
                and isinstance(quality, dict) and _quality_wait_is_non_actionable(quality)
                and result.get("blocked_reason") == "recall_quality_evidence_incomplete")
    return False


_DYNAMIC_DIAGNOSTIC_REASONS = frozenset({
    "dynamic_capability_evolution_executor_unavailable",
    "dynamic_capability_evolution_execution_failed",
    "dynamic_capability_evolution_invalid_execution",
    "code_implementation_v2_provider_context_required",
    "hypothesis_context_or_bounds_invalid", "hypothesis_missing_or_ambiguous",
    "plan_has_no_exact_evaluation_cases",
    "independent_evidence_missing", "independent_evidence_cases_invalid",
    "independent_evidence_case_invalid", "independent_evidence_case_selection_mismatch",
    "independent_evidence_suite_incomplete",
    "independent_verifier_does_not_match_durable_trace",
    "evaluation_suite_incomplete", "hypothesis_feedback_rejected",
    "dynamic_evidence_projection_failed", "machine_policy_blocked",
    "machine_policy_runtime_resolver_missing", "machine_policy_resolver_failed",
    "machine_proposal_blocked", "machine_apply_budget_exhausted",
    "machine_rollout_gate_reject", "bounded_code_patch_missing",
    "candidate_revision_mismatch", "candidate_evidence_watermark_mismatch",
    "candidate_expected_metric_mismatch", "candidate_hypothesis_context_incomplete",
    "candidate_bounds_mismatch", "candidate_replay_case_ids_mismatch",
    "bounded_code_patch_requires_apply_to_repo",
    "bounded_code_patch_deployment_requires_commit", "machine_policy_action_unknown",
    "dynamic_patch_v2_machine_policy_unavailable", "dynamic_patch_policy_provider_mismatch",
    "dynamic_patch_target_not_supported_by_hermes_v2", "dynamic_patch_policy_incident_mismatch",
    "dynamic_patch_hypothesis_evidence_unavailable", "dynamic_patch_hypothesis_evidence_changed",
    "dynamic_patch_hypothesis_context_mismatch", "dynamic_patch_candidate_bounds_forbid_changes",
    "dynamic_patch_candidate_bounds_unavailable",
    "dynamic_patch_candidate_bounds_not_protected", "dynamic_patch_hypothesis_revoked",
    "dynamic_patch_independent_suite_missing", "dynamic_patch_independent_suite_mismatch",
    "dynamic_patch_independent_suite_incomplete", "dynamic_patch_independent_artifact_missing",
    "dynamic_patch_independent_artifact_invalid", "dynamic_patch_trusted_repository_unavailable",
    "dynamic_patch_repository_unavailable", "repository_release_identity_mismatch",
    "dynamic_patch_policy_allowed_files_not_protected", "dynamic_patch_repository_coordinates_unavailable",
    "dynamic_patch_policy_repository_mismatch", "dynamic_patch_policy_test_plan_mismatch",
    "dynamic_patch_policy_incident_digest_mismatch", "automation_policy_already_consumed",
    "repository_transaction_in_progress_or_quarantined", "dynamic_patch_transaction_already_exists",
    "dynamic_patch_provider_request_failed", "dynamic_patch_provider_unavailable",
    "dynamic_patch_provider_invalid_result", "dynamic_patch_authority_changed_during_proposal",
    "dynamic_patch_machine_context_mismatch",
    "dynamic_patch_independent_trace_missing", "dynamic_patch_independent_trace_invalid",
    "dynamic_patch_durable_context_missing", "dynamic_patch_durable_context_invalid",
    "dynamic_patch_durable_context_digest_mismatch", "dynamic_patch_known_maintenance_provenance_required",
    "dynamic_patch_durable_base_mismatch", "dynamic_patch_durable_evidence_changed",
    "dynamic_patch_durable_policy_mismatch",
    "dynamic_patch_durable_incident_mismatch",
    "dynamic_patch_durable_context_unavailable",
    "dynamic_patch_registry_unavailable", "dynamic_patch_registry_contract_mismatch",
    "dynamic_patch_candidate_policy_bounds_changed", "repository_identity_unavailable",
    "repository_worktree_not_clean", "repository_commit_invalid", "provider_unavailable",
    "policy_file_missing", "policy_file_unreadable", "policy_file_not_regular",
    "policy_symlink_rejected", "policy_permissions_invalid", "policy_owner_invalid",
    "policy_owner_check_unsupported", "policy_secure_open_unavailable", "policy_file_too_large",
    "policy_json_invalid", "policy_not_object", "policy_fields_unknown", "policy_schema_invalid",
    "policy_expired", "policy_not_yet_valid", "kill_switch_present",
})


def _diagnostic_count(value: object) -> int | None:
    return value if type(value) is int and 0 <= value <= 1_000_000 else None


def _diagnostic_bool(value: object) -> bool | None:
    return value if type(value) is bool else None


def _diagnostic_timestamp(value: object) -> str | None:
    if value == "":
        return ""
    if not isinstance(value, str) or len(value) > 80:
        return None
    try:
        timestamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if timestamp.tzinfo is None:
            return None
        return timestamp.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    except (ValueError, OverflowError):
        return None


def _diagnostic_code(value: object, allowed: set | frozenset) -> str:
    if value is None:
        return "not_reported"
    return value if isinstance(value, str) and value in allowed else "not_allowlisted"


def _diagnostic_ref(value: object, pattern: str) -> str | None:
    return value if isinstance(value, str) and re.fullmatch(pattern, value) else None


def _diagnostic_reason_counts(value: object, allowed: set | frozenset) -> dict | None:
    """Preserve missing counts and never echo a provider's arbitrary reason."""
    if not isinstance(value, dict):
        return None
    counts: dict[str, int] = {}
    for reason, raw_count in list(value.items())[:100]:
        count = _diagnostic_count(raw_count)
        if count is None:
            continue
        code = reason if isinstance(reason, str) and reason in allowed else "reason_not_allowlisted"
        counts[code] = counts.get(code, 0) + count
    return dict(sorted(counts.items()))


def _diagnostic_refs(value: object, pattern: str, *, limit: int = 20) -> dict:
    if not isinstance(value, list):
        return {"refs": None, "truncated": None, "invalid_count": None}
    refs = []
    invalid = 0
    valid_count = 0
    for raw in value[:500]:
        ref = _diagnostic_ref(raw, pattern)
        if ref is None:
            invalid += 1
        else:
            valid_count += 1
            if len(refs) < limit and ref not in refs:
                refs.append(ref)
    return {"refs": refs, "truncated": len(value) > 500 or valid_count > limit,
            "invalid_count": invalid}


def _dynamic_proposal_diagnostics(value: object) -> dict:
    report = value if isinstance(value, dict) else {}
    result = {
        "schema": _diagnostic_code(report.get("schema"), {"dynamic_capability_patch_context.v1"}),
        "status": _diagnostic_code(report.get("status"), {"blocked", "proposal_blocked", "context_ready", "proposal_ready"}),
        "reason": _diagnostic_code(report.get("reason"), _DYNAMIC_DIAGNOSTIC_REASONS | {""}),
        "transaction_id": _diagnostic_ref(report.get("transaction_id"), r"dynamic-gap-[0-9a-f]{32}"),
        "incident_id": _diagnostic_ref(report.get("incident_id"), r"dynamic-gap-[0-9a-f]{32}"),
        "hypothesis_id": _diagnostic_ref(report.get("hypothesis_id"), r"capability_hypothesis_[0-9a-f]{32}"),
        "qualifying_feedback_id": _diagnostic_ref(report.get("qualifying_feedback_id"), r"capability_hypothesis_feedback_[0-9a-f]{32}"),
        "qualifies_for_product_completion": _diagnostic_bool(report.get("qualifies_for_product_completion")),
        "transaction_owner": _diagnostic_code(report.get("transaction_owner"), {"promotion_manager"}),
        "origin": _diagnostic_code(report.get("origin"), {"user_reported"}),
        **{key: _diagnostic_bool(report.get(key)) for key in (
            "known_before_detection", "prior_user_reported", "manual_bootstrap")},
    }
    artifacts = _diagnostic_refs(report.get("evaluation_artifact_ids"), r"capability_hypothesis_evaluation_[0-9a-f]{32}")
    result.update(evaluation_artifact_ids=artifacts["refs"],
                  evaluation_artifact_ids_truncated=artifacts["truncated"],
                  invalid_evaluation_artifact_id_count=artifacts["invalid_count"])
    return result


def _dynamic_evolution_awaiting_hypothesis_is_non_actionable(result: dict) -> bool:
    """Dynamic evolution with no candidate hypothesis anywhere is an input wait.

    The executor's ``ok: false`` verdict is kept; this only decides whether the
    scheduler run itself failed. It is a wait only when every work item is
    blocked on ``hypothesis_missing_or_ambiguous`` with exactly zero candidate
    hypotheses (nothing to evaluate, nothing applied, no error). An ambiguous
    pair, any other reason, an applied change, an execution error or an
    authority failure stays a failure.
    """
    if _authority_boundary_failed(result) or _has_execution_failure(result):
        return False
    if result.get("enabled") is not True or result.get("status") != "blocked":
        return False
    execution = result.get("execution")
    if not isinstance(execution, dict) or execution.get("ok") is not False:
        return False
    if _has_execution_failure(execution) or _authority_boundary_failed(execution):
        return False
    if int(execution.get("applied_count") or 0) != 0:
        return False
    results = execution.get("results")
    if not isinstance(results, list) or not results or len(results) > 500:
        return False
    return all(
        isinstance(item, dict)
        and item.get("status") == "blocked"
        and item.get("reason") == "hypothesis_missing_or_ambiguous"
        and type(item.get("candidate_hypothesis_count")) is int
        and item["candidate_hypothesis_count"] == 0
        for item in results
    )


def _dynamic_failure_diagnostics(report: object) -> dict:
    """Project only exact known codes; never copy payloads or exception text.

    Counts describe the first 500 results, not business acceptance. Missing or
    unrecognized reasons remain unknown, including exception-suffixed codes.
    """
    reason_counts: dict[str, int] = {}

    def add_reason(reason: object) -> None:
        code = (reason if isinstance(reason, str) and reason in _DYNAMIC_DIAGNOSTIC_REASONS
                else "reason_not_reported" if reason is None or reason == ""
                else "reason_not_allowlisted")
        reason_counts[code] = reason_counts.get(code, 0) + 1

    report = report if isinstance(report, dict) else {}
    execution = report.get("execution")
    execution = execution if isinstance(execution, dict) else {}
    for source in (report, execution):
        if source.get("reason"):
            add_reason(source["reason"])
    results = execution.get("results")
    results = results if isinstance(results, list) else []
    for item in results[:500]:
        if not isinstance(item, dict) or item.get("status") not in ("evaluated", "applied"):
            add_reason(item.get("reason") if isinstance(item, dict) else None)
    if not reason_counts:
        if not report:
            reason_counts["execution_report_missing"] = 1
        elif execution.get("results") == []:
            reason_counts["execution_results_empty"] = 1
        elif (_dynamic_context_reported(report) and report.get("ok") is True
              and execution.get("ok") is True):
            # Newly reported successful proposal contexts have no blocking
            # reason. Do not manufacture one just to populate this collection.
            pass
        else:
            reason_counts["reason_not_reported"] = 1
    candidate_counts = [item.get("candidate_hypothesis_count")
                        if isinstance(item, dict) and type(item.get("candidate_hypothesis_count")) is int
                        and 0 <= item["candidate_hypothesis_count"] <= 1_000_000 else None
                        for item in results[:500]]
    diagnostic = {"reason_counts": reason_counts, "result_count": min(len(results), 500),
            "results_truncated": len(results) > 500,
            "candidate_hypothesis_counts": candidate_counts,
            "candidate_hypothesis_counts_reported": sum(value is not None for value in candidate_counts)}
    proposals = [item for item in results[:500]
                 if isinstance(item, dict) and "candidate_proposal" in item]
    if proposals:
        diagnostic["patch_contexts"] = [{
            "work_item_id": _diagnostic_ref(item.get("work_item_id"), r"[0-9a-f]{40}"),
            **_dynamic_proposal_diagnostics(item["candidate_proposal"]),
        } for item in proposals[:20]]
        diagnostic["patch_context_count"] = len(proposals)
        diagnostic["patch_contexts_truncated"] = len(proposals) > 20 or len(results) > 500
    return diagnostic


def _dynamic_context_reported(report: object) -> bool:
    execution = report.get("execution") if isinstance(report, dict) else None
    results = execution.get("results") if isinstance(execution, dict) else None
    return isinstance(results, list) and any(
        isinstance(item, dict) and "candidate_proposal" in item for item in results[:500])


def _hypothesis_producer_diagnostics(report: object) -> dict:
    """Known skip codes only; missing producer detail stays unknown."""
    if not isinstance(report, dict) or not report:
        return {"status": "not_reported"}
    allowed = {"knowledge_link_query_failed", "no_applicable_knowledge_link_for_gap_revision",
               "ambiguous_applicable_knowledge_links", "hypothesis_rejected",
               "existing_hypothesis_query_failed", "ambiguous_existing_gap_hypotheses",
               "existing_gap_hypothesis_revoked", "produced_hypothesis_revoked",
               "ambiguous_reviewed_knowledge_for_gap_revision"}
    skipped = report.get("skipped")
    reason_counts: dict[str, int] = {}
    for item in skipped[:500] if isinstance(skipped, list) else []:
        reason = item.get("reason") if isinstance(item, dict) else None
        code = reason if isinstance(reason, str) and reason in allowed else "reason_not_allowlisted"
        reason_counts[code] = reason_counts.get(code, 0) + 1
    statuses = {"disabled", "produced", "reused", "diagnosed", "no_gaps", "no_eligible_evidence", "not_configured"}
    status = report.get("status")
    diagnostic = {"status": status if isinstance(status, str) and status in statuses else "unknown",
            "skipped_reason_counts": reason_counts,
            "skipped_count": min(len(skipped), 500) if isinstance(skipped, list) else None,
            "skipped_truncated": len(skipped) > 500 if isinstance(skipped, list) else False}
    reviews = report.get("knowledge_link_reviews")
    if isinstance(reviews, list):
        diagnostic["knowledge_link_reviews"] = [_knowledge_review_diagnostics(item) for item in reviews[:20]]
        diagnostic["knowledge_link_review_count"] = min(len(reviews), 500)
        diagnostic["knowledge_link_reviews_truncated"] = len(reviews) > 20
    if "knowledge_link_reviews" in report or "reused" in report:
        diagnostic["created_count"] = len(report["created"]) if isinstance(report.get("created"), list) else None
        diagnostic["reused_count"] = len(report["reused"]) if isinstance(report.get("reused"), list) else None
        hypotheses = [item for name in ("created", "reused") for item in report.get(name, [])[:500]
                      if isinstance(item, dict)] if all(
                          isinstance(report.get(name, []), list) for name in ("created", "reused")) else []
        diagnostic["candidate_bounds"] = [{
            "hypothesis_id": _diagnostic_ref(item.get("hypothesis_id"), r"capability_hypothesis_[0-9a-f]{32}"),
            "status": _diagnostic_code(item.get("candidate_bounds_status"), {"evaluation_only", "bounded_write_candidate"}),
            "reason": _diagnostic_code(item.get("candidate_bounds_reason"), _DYNAMIC_DIAGNOSTIC_REASONS | {
                "", "no_registered_writable_candidate_bounds", "machine_policy_and_live_registry_bounds"}),
            "code_changes_authorized": _diagnostic_bool(item.get("code_changes_authorized")),
            "max_changes": _diagnostic_count(item.get("candidate_max_changes")),
        } for item in hypotheses[:20]]
        diagnostic["candidate_bounds_truncated"] = len(hypotheses) > 20
    return diagnostic


_KNOWLEDGE_REVIEW_REASONS = frozenset({
    "review_inventory_truncated", "review_inventory_unavailable", "gap_target_not_active_exact_scope",
    "knowledge_source_review_missing", "knowledge_source_not_approved", "knowledge_source_trust_not_verified",
    "knowledge_capability_context_invalid", "knowledge_capability_context_mismatch",
    "knowledge_capability_context_conflicting", "knowledge_not_active_exact_scope",
    "paper_source_not_found_exact_scope", "paper_source_id_missing", "paper_source_id_ambiguous",
    "knowledge_not_currently_applicable", "knowledge_canonical_anchor_missing",
    "knowledge_inventory_truncated", "knowledge_inventory_empty", "knowledge_evidence_unavailable",
    "knowledge_review_budget_exceeded", "knowledge_review_budget_deferred",
    "conflicting_applicability_reviews", "applicability_review_revoked", "applicability_review_input_oversized",
    "applicability_review_inputs_changed", "applicability_review_output_invalid",
    "applicability_review_quote_invalid", "applicability_review_quote_missing",
    "applicability_review_transport_unavailable", "knowledge_not_applicable_to_gap_revision",
    "ambiguous_reviewed_knowledge_for_gap_revision", "knowledge_review_unavailable",
    "knowledge_temporal_validity_invalid", "knowledge_temporal_state_not_current",
    "knowledge_temporal_validity_conflicting", "knowledge_environment_constraints_invalid",
    "knowledge_environment_constraints_conflicting", "applicability_review_input_digest_mismatch",
    "applicability_review_no_longer_current", "gap_revision_no_longer_current",
    "knowledge_inventory_enumeration_pending", "knowledge_inventory_changes_pending",
    "knowledge_inventory_authority_changed", "knowledge_inventory_review_incomplete",
    "knowledge_inventory_time_boundary_changed", "knowledge_inventory_record_changed",
    "knowledge_inventory_scan_state_invalid", "knowledge_inventory_restarted_after_change_gap",
    "knowledge_canonical_verification_unavailable",
    "knowledge_source_registry_unavailable",
    "inventory_exact_scope_required", "inventory_page_limit_invalid", "inventory_metadata_invalid",
    "inventory_watermark_invalid", "inventory_transition_invalid", "inventory_schema_invalid",
    "inventory_schema_requires_own_transaction", "inventory_change_history_gap",
    "inventory_watermark_unavailable", "inventory_write_requires_own_transaction",
    "inventory_scan_state_invalid", "inventory_scan_state_too_large", "inventory_scan_state_conflict",
    "inventory_manifest_kind_invalid", "inventory_review_status_invalid", "inventory_manifest_dirty_invalid",
    "inventory_applicability_invalid", "inventory_reason_invalid", "inventory_dependencies_invalid",
    "inventory_manifest_cursor_invalid", "inventory_changes_invalid", "inventory_change_scope_mismatch",
    "inventory_reason_counts_truncated", "inventory_review_lookup_invalid",
    "inventory_review_lookup_truncated", "inventory_review_payload_invalid",
    "inventory_input_digest_invalid",
})


def _knowledge_review_diagnostics(value: object) -> dict:
    report = value if isinstance(value, dict) else {}
    refs = _diagnostic_refs(report.get("review_receipt_ids"), r"knowledge_gap_review_[0-9a-f]{32}")
    work_items = _diagnostic_refs(report.get("work_item_ids"), r"[0-9a-f]{40}")
    reasons = report.get("reason_counts")
    links = report.get("links")
    diagnostic = {
        "schema": _diagnostic_code(report.get("schema"), {"knowledge.gap_applicability_review.v1"}),
        "status": _diagnostic_code(report.get("status"), {"linked", "scanning", "no_eligible_evidence", "not_linked", "blocked"}),
        "work_item_ids": work_items["refs"], "work_item_ids_truncated": work_items["truncated"],
        "invalid_work_item_id_count": work_items["invalid_count"],
        **{key: _diagnostic_count(report.get(key)) for key in (
            "eligible_count", "reviewed_count", "applicable_count", "completed_count", "deferred_count")},
        "reason_counts": _diagnostic_reason_counts(reasons, _KNOWLEDGE_REVIEW_REASONS),
        "reasons_truncated": len(reasons) > 100 if isinstance(reasons, dict) else None,
        "review_receipt_ids": refs["refs"], "review_receipt_ids_truncated": refs["truncated"],
        "invalid_review_receipt_id_count": refs["invalid_count"],
        "link_refs": [{
            "link_id": _diagnostic_ref(item.get("link_id"), r"knowledge_link_[0-9a-f]{32}"),
            "link_digest": _diagnostic_ref(item.get("link_digest"), r"[0-9a-f]{64}"),
            "review_receipt_id": _diagnostic_ref(item.get("review_receipt_id"), r"knowledge_gap_review_[0-9a-f]{32}"),
            **({"inventory_certificate_ref": _diagnostic_ref(item.get("inventory_certificate_ref"),
                r"knowledge_gap_review_[0-9a-f]{32}")} if "inventory_certificate_ref" in item else {}),
        } for item in links[:20] if isinstance(item, dict)] if isinstance(links, list) else None,
        "links_truncated": len(links) > 20 if isinstance(links, list) else None,
    }
    if "inventory" in report:
        inventory = report["inventory"] if isinstance(report["inventory"], dict) else {}
        diagnostic["inventory"] = {
            "schema": _diagnostic_code(inventory.get("schema"), {"knowledge.gap_inventory_scan.v1"}),
            **{key: _diagnostic_ref(inventory.get(key), r"[0-9a-f]{64}") for key in ("scan_key", "cursor_digest")},
            **{key: _diagnostic_bool(inventory.get(key)) for key in (
                "inventory_complete", "changes_complete", "complete")},
            **{key: _diagnostic_count(inventory.get(key)) for key in (
                "page_record_count", "revalidated_record_count", "manifest_record_count", "pending_count",
                "unknown_count")},
            **{key: inventory[key] if type(inventory.get(key)) is int and 0 <= inventory[key] < 2 ** 63
               else None for key in ("input_watermark", "current_watermark")},
            "next_transition": _diagnostic_timestamp(inventory.get("next_transition")),
            "certificate_ref": _diagnostic_ref(inventory.get("certificate_ref"), r"knowledge_gap_review_[0-9a-f]{32}"),
        }
    return diagnostic


_CONTROLLED_MEMORY_REASONS = frozenset({
    "authorized_source_ids_missing", "controlled_memory_store_unavailable", "controlled_memory_benchmark_empty",
    "controlled_memory_dataset_invalid", "controlled_memory_cases_not_evaluable", "controlled_case_invalid",
    "source_not_authorized", "source_record_inactive", "source_record_expired", "source_record_not_yet_valid",
    "source_record_invalid_temporal_binding", "source_record_operational", "source_record_missing",
    "source_query_material_empty", "source_case_binding_changed", "controlled_retrieval_unavailable",
    "source_record_capture_rejected", "source_record_episode_evidence", "source_record_evidence_only",
    "controlled_retrieval_incomplete", "returned_source_not_authorized_or_current", "known_item_missing",
    "memory_benchmark_channel_scope_invalid", "memory_benchmark_logical_user_channel_binding_missing",
    "memory_benchmark_bindings_missing", "memory_benchmark_bindings_unreadable",
    "memory_benchmark_channel_binding_ambiguous", "memory_benchmark_channel_binding_missing",
    "memory_benchmark_source_ids_invalid",
    "controlled_memory_cutoff_invalid", "source_not_stable_before_cutoff", "source_created_at_invalid",
})
_MEMORY_RECORD_REF = r"(?:mem|rec|digest|ktop|claim|kpage|knowledge_page|ku|knowledge_unit)_[0-9a-f]{8,64}"


def _memory_source_ref(value: object) -> dict:
    if not isinstance(value, str) or len(value) > 128 or re.fullmatch(r"[a-z0-9](?:[a-z0-9._-]*[a-z0-9])?", value) is None:
        return {"source_id": None}
    if value in {"hermes", "codex", "openclaw"}:
        return {"source_id": value}
    # Configured source IDs can contain member identities. The complete native
    # binding remains in the memory report; its nightly reference is opaque.
    return {"source_id_digest": sha256(value.encode("utf-8")).hexdigest()}


def _memory_binding_diagnostics(value: object) -> dict:
    binding = value if isinstance(value, dict) else {}
    return {"record_id": _diagnostic_ref(binding.get("record_id"), _MEMORY_RECORD_REF),
            "payload_digest": _diagnostic_ref(binding.get("payload_digest"), r"[0-9a-f]{64}"),
            **_memory_source_ref(binding.get("source_id"))}


def _controlled_retrieval_diagnostics(value: object) -> dict:
    """Keep measured stage counters, fixed codes and opaque source references."""
    from eimemory.retrieval.diagnostics import ADMISSION_DROPS, ENGINE_DROPS, compact_recall_diagnostics
    report = value if isinstance(value, dict) else {}
    statuses = {"unknown", "evidence_found", "no_evidence", "unavailable", "degraded", "ambiguous",
                "available", "not_run", "not_reported"}
    selector_drops = set(ADMISSION_DROPS) | {
        "grounding", "score", "top_score_margin", "exact_dominance", "durable_event_dominance",
        "recall_budget_exhausted", "candidate_hydration_timeout", "authority_unavailable", "authority_changed"}
    gate_drops = {"inactive_or_superseded", "stale_rule", "stale_memory", "external_knowledge_quarantined",
        "external_knowledge_untrusted", "agent_outcome", "tool_call", "diagnostic", "deployment",
        "episode_evidence", "operational", "raw", "evidence_only", "run_log", "audit_record",
        "incident_report", "evolution_artifact", "task_context", "source:blocked", "source:not_allowed",
        "memory_type:not_allowed", "organ:not_allowed", "intent_kind:suppressed", "recall_lane:not_allowed",
        "task_evidence_missing", "preference_attribute_missing"}
    engine_drops = set(ENGINE_DROPS) | {
        "query_scope_limit", "raw_source_not_allowed", "raw_ref_missing", "raw_scope_not_allowed",
        "raw_not_authoritative", "provider_over_limit", "preference_candidate_limit",
        "preference_collection_timeout", "preference_collection_unavailable", "scope_not_allowed",
        "source_not_allowed", "missing_or_corrupt_record", "ref_mismatch", "inactive_record", "quality_rejected",
        "kind_not_allowed", "pre_fusion_scope_source_rejected", "anchor_reserve_swap",
        *("relevance_selector:" + reason for reason in selector_drops)}

    def stage(raw: object, drops: set[str]) -> dict:
        raw = raw if isinstance(raw, dict) else {}
        projected = {key: _diagnostic_count(raw[key]) for key in (
            "input_count", "candidate_count", "candidate_limit", "selected_count", "retrieved_count",
            "search_limit", "query_scope_count") if key in raw}
        if "elapsed_ms" in raw:
            amount = raw["elapsed_ms"]
            projected["elapsed_ms"] = (round(min(amount, 1_000_000), 3)
                if (type(amount) is int or type(amount) is float and isfinite(amount)) and amount >= 0 else None)
        for key in ("status", "mode"):
            if key in raw:
                projected[key] = _diagnostic_code(raw[key], statuses if key == "status" else {
                    "enforced", "bypassed", "identity_lookup", "lightweight_evidence"})
        for key in ("drops", "dropped_reasons", "blocked_counts"):
            if key in raw:
                projected[key] = _diagnostic_reason_counts(raw[key], drops)
        return projected

    engine = stage(report.get("engine"), engine_drops)
    raw_engine = report.get("engine") if isinstance(report.get("engine"), dict) else {}
    if "source_names" in raw_engine:
        names = raw_engine["source_names"]
        engine["source_names"] = [_diagnostic_code(name, {"sqlite", "sqlite+postgres_vector",
            "SQLiteCandidateSource", "PostgresVectorCandidateSource", "other_configured_source"})
            for name in names[:8]] if isinstance(names, list) else None
    if "fallback_reason" in raw_engine:
        engine["fallback_reason"] = _diagnostic_code(raw_engine["fallback_reason"], {
            "legacy_scan", "candidate_source_fallback", "empty_source_allowlist"})
    if "fallback_error_codes" in raw_engine:
        engine["fallback_error_codes"] = _diagnostic_reason_counts(raw_engine["fallback_error_codes"], {
            "unknown", "disabled", "not_configured", "circuit_open", "index_lag_exceeded", "index_lag_unknown",
            "recall_budget_exhausted", "embedding_unavailable", "embedding_timeout", "postgres_unavailable",
            "postgres_timeout", "index_not_ready", "scope_not_indexed"})
    phases = report.get("pipeline")
    availability = report.get("source_availability")
    availability = availability if isinstance(availability, dict) else {}
    boundary = report.get("verifier_boundary")
    boundary = boundary if isinstance(boundary, dict) else {}
    boundary_counts = ("pool_candidate_count", "candidate_count", "visible_candidate_count", "visible_window_count",
        "candidate_text_chars", "visible_text_chars", "windowed_candidate_count", "calls", "model_selected_count",
        "answer_requirement_rejections", "quote_validation_rejections", "accepted_selection_count")
    partition = report.get("source_partition")
    partition = partition if isinstance(partition, dict) else {}
    sources = partition.get("source_ids")
    assistance = report.get("assistance")
    assistance = assistance if isinstance(assistance, dict) else {}
    sanitized_assistance = compact_recall_diagnostics({"engine_diagnostics": {},
        "relevance_selector": {"caller_assistance": assistance}}).get("caller_assistance", {})
    sanitized_assistance = {key: amount for key, amount in sanitized_assistance.items()
        if key not in {"proofs", "local_evidence"}
        and (type(assistance.get(key)) is int and _diagnostic_count(assistance[key]) is not None
             if type(amount) is int else True)}
    if assistance.get("status") == "not_reported":
        sanitized_assistance["status"] = "not_reported"
    result = {
        "schema": _diagnostic_code(report.get("schema"), {"retrieval_stage_diagnostics.v1"}),
        "retrieval_status": _diagnostic_code(report.get("retrieval_status"), statuses),
        "measurement_origin": _diagnostic_code(report.get("measurement_origin"), {
            "controlled_runtime_exception", "controlled_runtime_bundle"}),
        "engine": engine,
        "pipeline": [{"name": item["name"], **stage(item, gate_drops)} for item in phases[:8]
            if isinstance(item, dict) and isinstance(item.get("name"), str)
            and item["name"] in {"prepare", "retrieve", "graph_expand", "score_filter", "package"}]
            if isinstance(phases, list) else None,
        "selector": stage(report.get("selector"), selector_drops),
        "online_gate": stage(report.get("online_gate"), gate_drops),
        "verifier_boundary": {key: _diagnostic_count(boundary[key]) for key in boundary_counts if key in boundary},
        "source_availability": {
            "status": _diagnostic_code(availability.get("status"), {"available", "unavailable", "degraded", "unknown"}),
            **{key: _diagnostic_bool(availability.get(key)) for key in ("collection_complete", "fallback")},
            **{key: _diagnostic_count(availability.get(key)) for key in ("source_searches", "source_budget_exhausted")},
        },
        "source_partition": {
            "source_refs": [_memory_source_ref(source) for source in sources[:8]] if isinstance(sources, list) else None,
            "source_refs_truncated": len(sources) > 8 if isinstance(sources, list) else None,
            "target_source": _memory_source_ref(partition.get("target_source_id")),
            "exact_scope_only": _diagnostic_bool(partition.get("exact_scope_only")),
        },
        "assistance": sanitized_assistance,
        "empty_result_cause": _diagnostic_code(report.get("empty_result_cause"), {
            "returned_results", "candidate_collection_empty", "selector_rejected_all",
            "retrieval_unavailable_or_incomplete", "empty_origin_unknown"}),
    }
    if "status" in boundary:
        result["verifier_boundary"]["status"] = _diagnostic_code(boundary["status"], {"unknown"})
    if "reason" in boundary:
        result["verifier_boundary"]["reason"] = _diagnostic_code(boundary["reason"], {
            "unknown", "no_candidates", "candidate_evidence_empty", "model_no_selection", "reviewed_original_evidence",
            "answer_requirements_rejected", "caller_model_unavailable", "assistance_budget_exhausted",
            "caller_verification_failed", "caller_model_identity_changed", "authority_or_deadline_changed"})
    observed = result["source_availability"]
    if observed["status"] == "available" and not (
            observed["collection_complete"] is True and observed["fallback"] is False
            and observed["source_searches"] is not None and observed["source_searches"] > 0
            and observed["source_budget_exhausted"] == 0):
        observed["status"] = "unknown"
    if result["empty_result_cause"] == "candidate_collection_empty" and not (
            observed["status"] == "available" and engine.get("candidate_count") == 0):
        result["empty_result_cause"] = "empty_origin_unknown"
    selector = result["selector"]
    if result["empty_result_cause"] == "selector_rejected_all" and not (
            selector.get("input_count") is not None and selector["input_count"] > 0
            and selector.get("selected_count") == 0 and any((selector.get("dropped_reasons") or {}).values())):
        result["empty_result_cause"] = "empty_origin_unknown"
    if "target_trace" in report:
        from eimemory.retrieval.target_trace import safe_target_trace_report
        # The shared projection preserves source_trace only when this native
        # invocation recorded it; older traces acquire no source history.
        result["target_trace"] = safe_target_trace_report(report["target_trace"])
    return result


def _controlled_query_generation_diagnostics(value: object) -> dict:
    report = value if isinstance(value, dict) else {}
    return {
        "material_field": _diagnostic_code(report.get("material_field"), {
            "title", "summary", "content_text", "unknown"}),
        "material_chars": _diagnostic_count(report.get("material_chars")),
        "query_chars": _diagnostic_count(report.get("query_chars")),
        "truncated": _diagnostic_bool(report.get("truncated")),
    }


def _controlled_memory_diagnostics(report: dict) -> dict:
    result = {
        "dataset_kind": _diagnostic_code(report.get("dataset_kind"), {"controlled_memory_benchmark"}),
        "dataset_schema": _diagnostic_code(report.get("dataset_schema"), {"controlled_memory_benchmark.v1"}),
        **{key: _diagnostic_bool(report.get(key)) for key in (
            "formal_business_evidence", "certifies_natural_queries", "certifies_no_answer",
            "controlled_benchmark_passed_threshold")},
        **{key: _diagnostic_count(report.get(key)) for key in (
            "evaluated_case_count", "not_evaluable_count", "sample_count", "pass_count", "fail_count")},
    }
    context = report.get("controlled_context")
    if isinstance(context, dict):
        result["controlled_context"] = {
            "status": _diagnostic_code(context.get("status"), {"blocked", "resolved"}),
            "channel": _diagnostic_code(context.get("channel"), {"hermes", "codex", "openclaw", ""}),
            "reason": _diagnostic_code(context.get("reason"), _CONTROLLED_MEMORY_REASONS | {""}),
        }
    generation = report.get("dataset_generation")
    if isinstance(generation, dict):
        raw_reasons = generation.get("skipped_reason_counts")
        raw_refs = generation.get("source_refs")
        result["dataset_generation"] = {
            "ok": _diagnostic_bool(generation.get("ok")),
            "reason": _diagnostic_code(generation.get("reason"), _CONTROLLED_MEMORY_REASONS | {""}),
            **{key: _diagnostic_count(generation.get(key)) for key in ("case_count", "scanned_count", "same_run_skipped_count")},
            "scan_truncated": _diagnostic_bool(generation.get("scan_truncated")),
            "selection_truncated": _diagnostic_bool(generation.get("selection_truncated")),
            "stable_before": _diagnostic_ref(generation.get("stable_before"),
                r"(?:\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z)?"),
            "skipped_reason_counts": _diagnostic_reason_counts(raw_reasons, _CONTROLLED_MEMORY_REASONS),
            "skipped_reasons_truncated": len(raw_reasons) > 100 if isinstance(raw_reasons, dict) else None,
            "source_refs": [{
                "record_id": _diagnostic_ref(item.get("record_id"), _MEMORY_RECORD_REF),
                "payload_digest": _diagnostic_ref(item.get("payload_digest"), r"[0-9a-f]{64}"),
            } for item in raw_refs[:20] if isinstance(item, dict)] if isinstance(raw_refs, list) else None,
            "source_refs_truncated": len(raw_refs) > 20 if isinstance(raw_refs, list) else None,
        }
    samples = report.get("samples")
    if isinstance(samples, list):
        result["case_results"] = [{
            "case_id": _diagnostic_ref(item.get("case_id"), r"controlled-memory-[0-9a-f]{24}"),
            "passed": _diagnostic_bool(item.get("passed")),
            "evaluable": _diagnostic_bool(item.get("evaluable")),
            "failure_reason": _diagnostic_code(item.get("failure_reason"), _CONTROLLED_MEMORY_REASONS),
            "expected_rank": _diagnostic_count(item.get("expected_rank")),
            "returned_record_count": _diagnostic_count(item.get("returned_record_count")),
        } for item in samples[:20] if isinstance(item, dict)]
        result["case_results_truncated"] = len(samples) > 20
        failures = [item for item in samples[:500] if isinstance(item, dict) and item.get("passed") is False]
        if any(any(key in item for key in ("source_binding", "retrieval_diagnostics", "query_generation"))
               for item in failures):
            result["failed_case_refs"] = [{
                "case_id": _diagnostic_ref(item.get("case_id"), r"controlled-memory-[0-9a-f]{24}"),
                "failure_reason": _diagnostic_code(item.get("failure_reason"), _CONTROLLED_MEMORY_REASONS),
                "evaluable": _diagnostic_bool(item.get("evaluable")),
                "returned_record_count": _diagnostic_count(item.get("returned_record_count")),
                "source_binding": _memory_binding_diagnostics(item.get("source_binding")),
                **({"query_generation": _controlled_query_generation_diagnostics(item["query_generation"])}
                   if "query_generation" in item else {}),
                **({"retrieval_diagnostics": _controlled_retrieval_diagnostics(item["retrieval_diagnostics"])}
                   if "retrieval_diagnostics" in item else {}),
            } for item in failures[:5]]
            result["failed_case_ref_count"] = len(failures)
            result["failed_case_refs_truncated"] = len(failures) > 5 or len(samples) > 500
    return result


def _memory_benchmark_diagnostics(report: object) -> dict:
    if not isinstance(report, dict) or not report:
        return {"status": "not_reported"}
    def code(value: object, allowed: set[str]) -> str:
        if value is None:
            return "not_reported"
        return value if isinstance(value, str) and value in allowed else "not_allowlisted"
    reasons = {"", "memory_eval_dataset_empty", "memory_eval_dataset_generation_failed",
               "run_memory_eval_ci_unavailable"} | _CONTROLLED_MEMORY_REASONS
    count = report.get("retrieval_case_count")
    diagnostic = {
        "status": code(report.get("memory_benchmark_status"), {"not_run", "evaluated"}),
        "dataset_source": code(report.get("dataset_source"), {"env", "conventional_path", "replay_dataset", "none", "controlled_records"}),
        "retrieval_case_count": count if type(count) is int and 0 <= count <= 1_000_000 else None,
        "eval_skipped_reason": code(report.get("eval_skipped_reason"), reasons),
        "blocked_reason": code(report.get("blocked_reason"), reasons),
    }
    if report.get("dataset_source") == "controlled_records" or "controlled_context" in report:
        diagnostic.update(_controlled_memory_diagnostics(report))
    return diagnostic


def _auto_review_diagnostics(report: object) -> dict:
    """Bounded counters and native review references, never label content."""
    from .review_references import review_reason_references
    from eimemory.evaluation.production_query_review_reasons import (
        PRODUCTION_REVIEW_REASON_CODES, REASON_CATALOG_VERSION,
    )
    if not isinstance(report, dict) or not report:
        return {"status": "not_run"}
    def count(value: object) -> int:
        return value if type(value) is int and 0 <= value <= 1_000_000 else 0
    progress = report.get("dataset_progress") if isinstance(report.get("dataset_progress"), dict) else {}
    by_authority = progress.get("accepted_by_authority") if isinstance(progress.get("accepted_by_authority"), dict) else {}
    policy = report.get("policy") if isinstance(report.get("policy"), dict) else {}
    reason_buckets = report.get("reason_counts")
    reasons = reason_buckets.get("not_passed") if isinstance(reason_buckets, dict) else None
    reason_counts: dict[str, int] = {}
    for reason, value in list(reasons.items())[:100] if isinstance(reasons, dict) else []:
        code = (reason if isinstance(reason, str) and reason in PRODUCTION_REVIEW_REASON_CODES
                else "reason_not_allowlisted")
        reason_counts[code] = reason_counts.get(code, 0) + count(value)
    return {
        "status": str(report.get("status") or "unknown")[:40],
        "review_contract": (report.get("review_contract")
                            if report.get("review_contract") == "production_recall_review_conclusion.v1" else "legacy"),
        **{key: report[key] if type(report.get(key)) is int and 0 <= report[key] <= 1_000_000 else None
           for key in ("reviewed_count", "passed_count", "not_passed_count")},
        "not_passed_reason_counts": reason_counts if isinstance(reasons, dict) else None,
        "not_passed_review_references": review_reason_references(report),
        "reason_catalog_version": REASON_CATALOG_VERSION,
        "not_passed_reasons_truncated": len(reasons) > 100 if isinstance(reasons, dict) else None,
        "criteria_version": str(report.get("criteria_version") or "")[:80],
        "enabled": policy.get("enabled") is True,
        "accepted_count": count(report.get("accepted_count")),
        "pending_count": (count(report["pending_count"]) if report.get("pending_count") is not None else None),
        "open_review_count": (count(report["open_review_count"]) if report.get("open_review_count") is not None else None),
        "coverage": _review_coverage_diagnostics(report.get("coverage")),
        "closed_not_evaluable_count": count(report.get("closed_not_evaluable_count")),
        "closed_without_label_count": count(report.get("closed_without_label_count")),
        "rejected_count": count(report.get("rejected_count")),
        "accepted_cases_by_authority": {
            "human": count(by_authority.get("human")),
            "auto_review": count(by_authority.get("auto_review")),
        },
        "accepted_case_count": count(progress.get("accepted_case_count")),
        "required_case_count": count(progress.get("required_case_count")),
    }


def _review_coverage_diagnostics(value: object) -> dict:
    if not isinstance(value, dict) or value.get("schema") != "production_recall_review_progress.v1":
        return {"status": "not_reported"}
    return {"schema": value["schema"],
            "boundary": "fixed_high_water_snapshot" if value.get("boundary") == "fixed_high_water_snapshot" else "unknown",
            "count_scope": "reviewed_batch" if value.get("count_scope") == "reviewed_batch" else "unknown",
            "cycle_complete": value.get("cycle_complete") if type(value.get("cycle_complete")) is bool else None,
            **{key: _diagnostic_count(value.get(key)) for key in (
                "total_case_count", "batch_uncovered_count", "cycle_remaining_count", "newer_case_count")}}


def _semantic_monitor_diagnostics(report: object) -> dict:
    """Bounded per-surface/per-channel observation counts; never query text."""
    if not isinstance(report, dict) or not report:
        return {"status": "not_run"}
    from eimemory.evaluation.semantic_relevance_monitor import SKIP_REASONS
    def count(value: object) -> int:
        return value if type(value) is int and 0 <= value <= 1_000_000 else 0
    def bounded(value: object, keys: tuple[str, ...]) -> dict:
        source = value if isinstance(value, dict) else {}
        return {key: count(source.get(key)) for key in keys if count(source.get(key))}
    return {
        "status": str(report.get("status") or "unknown")[:40],
        "new_count": count(report.get("new_count")),
        "reused_count": count(report.get("reused_count")),
        "deferred_count": count(report.get("deferred_count")),
        "skipped_count": count(report.get("skipped_count")),
        "skip_reason_counts": bounded(report.get("skip_reason_counts"), SKIP_REASONS),
        "provider_calls": count(report.get("provider_calls")),
        "verdict_counts": bounded(report.get("verdict_counts"), ("relevant", "mixed", "off_topic", "unknown")),
        "not_applicable_counts": bounded(report.get("not_applicable_counts"), ("empty_delivery",)),
        "by_surface": bounded(report.get("by_surface"), ("memory.recall", "research.task")),
        "by_channel": bounded(report.get("by_channel"), ("openclaw", "codex", "hermes")),
    }


_COLLECTION_DIAGNOSTIC_ERRORS = frozenset({
    "fetch failed",
    "unsafe fetch URL",
    "missing source URI",
    "invalid GitHub URL",
    "collect_external_sources_unavailable",
    "collection_failed",
})


def _external_collection_diagnostics(report: object) -> dict:
    """Count allowlisted collection errors. Never copy URLs or exception text."""
    errors = report.get("errors") if isinstance(report, dict) else None
    counts: dict[str, int] = {}
    if isinstance(errors, list):
        for item in errors[:50]:
            raw = item.get("error") if isinstance(item, dict) else ""
            code = raw if isinstance(raw, str) and raw in _COLLECTION_DIAGNOSTIC_ERRORS else "reason_not_allowlisted"
            counts[code] = counts.get(code, 0) + 1
    return {"error_codes": counts}


def nightly_result_diagnostics(report: dict, steps: list[dict]) -> dict:
    from .collection_diagnostics import production_collection_diagnostics
    from eimemory.scheduler.research_review_diagnostics import (
        research_provider_diagnostics_reported, research_review_diagnostics,
    )
    failures = []
    waits = []
    for step in steps:
        if not isinstance(step, dict) or step.get("ok") is not True:
            failures.append(str(step.get("step") or "unknown") if isinstance(step, dict) else "invalid_step")
        elif step.get("evaluation_status") == "awaiting_evidence":
            waits.append(str(step.get("step") or "unknown"))
    for name in NIGHTLY_NESTED_OK_ALLOWLIST:
        nested = report.get(name)
        if nested is None:
            continue
        if not isinstance(nested, dict) or type(nested.get("ok")) is not bool:
            failures.append(name)
        elif _non_actionable_step_wait(name, nested):
            waits.append(name)
        elif nested["ok"] is not True or _has_report_failure(nested) or _authority_boundary_failed(nested):
            failures.append(name)
    failures = list(dict.fromkeys(failures))
    gate = report.get("recall_quality_gate")
    gate = gate if isinstance(gate, dict) else {}
    execution_ok = _aggregate_nightly_ok(report, steps)
    return {
        "schema": "nightly_execution_diagnostics.v1",
        "execution_ok": execution_ok,
        "first_failed_step": failures[0] if failures else "",
        "failed_steps": failures,
        "evidence_waits": list(dict.fromkeys(waits)),
        "recall_quality_accepted": _formal_recall_accepted(report.get("recall_quality"), gate),
        "recall_quality_evidence": gate.get("recall_quality_evidence") or {},
        "memory_benchmark_status": (report.get("memory_eval_ci") or {}).get("memory_benchmark_status", "not_run")
            if isinstance(report.get("memory_eval_ci"), dict) else "not_run",
        "memory_benchmark_accepted": isinstance(report.get("memory_eval_ci"), dict)
            and report["memory_eval_ci"].get("memory_benchmark_status") == "evaluated"
            and report["memory_eval_ci"].get("memory_benchmark_accepted") is True,
        "recall_label_auto_review": _auto_review_diagnostics(report.get("production_recall_auto_review")),
        "production_recall_collection": production_collection_diagnostics(
            report.get("production_recall_collection"), report.get("production_recall_auto_review")),
        "memory_benchmark": _memory_benchmark_diagnostics(report.get("memory_eval_ci")),
        "capability_hypothesis_producer": _hypothesis_producer_diagnostics(report.get("capability_hypothesis_producer")),
        "recall_semantic_relevance": _semantic_monitor_diagnostics(report.get("semantic_relevance_monitor")),
        "production_recall_smoke": _production_recall_smoke_diagnostics(report.get("production_recall")),
        "recall_quality_per_agent": _per_agent_diagnostics(report.get("recall_quality_per_agent")),
        "release_acceptance": "not_evaluated_by_scheduler",
        "last_success_at_semantics": "current_run_not_historical_last_success",
        **({"dynamic_capability_evolution": _dynamic_failure_diagnostics(
            report.get("dynamic_capability_evolution"))}
           if "dynamic_capability_evolution" in failures or "dynamic_capability_evolution" in waits
              or _dynamic_context_reported(report.get("dynamic_capability_evolution")) else {}),
        **({"external_collection": _external_collection_diagnostics(
            report.get("external_collection"))}
           if "external_collection" in failures else {}),
        **({"research_closure_review": research_review_diagnostics(
            report.get("research_closure_review"))}
           if "research_closure_review" in failures
              or research_provider_diagnostics_reported(report.get("research_closure_review")) else {}),
    }


def _per_agent_diagnostics(value: object) -> dict:
    if not isinstance(value, dict) or not isinstance(value.get("agents"), dict):
        return {"status": "not_run"}
    agents = {}
    for agent, verdict in sorted(value["agents"].items()):
        verdict = verdict if isinstance(verdict, dict) else {}
        agents[str(agent)[:64]] = {key: verdict.get(key) for key in (
            "ok", "blocked_reason", "sample_count", "evaluation_contract", "label_trust_accepted",
            "false_recall_rate", "recall_at_k", "mrr") if key in verdict}
    return {"status": "evaluated", "rule": value.get("rule"), "discovery": value.get("discovery"),
            "agents": agents, "combined": value.get("combined")}


def _production_recall_smoke_diagnostics(value: object) -> dict:
    """Post-run recall metrics measured by this nightly's own known-item smoke eval.

    Diagnostic only: the scheduler does not perform release acceptance, and
    these numbers never replace the formal recall-quality gate.
    """
    if not isinstance(value, dict) or not value:
        return {"status": "not_run"}
    if value.get("eval_skipped_reason") or value.get("error"):
        return {"status": "not_evaluated",
                "reason": str(value.get("eval_skipped_reason") or value.get("error") or "")[:120]}
    gate = value.get("quality_gate") if isinstance(value.get("quality_gate"), dict) else {}
    metrics = {}
    for key in ("false_recall_rate", "recall_at_k", "precision_at_k", "mrr", "false_recall_count"):
        number = value.get(key)
        if type(number) in (int, float) and not isinstance(number, bool):
            metrics[key] = number
    sample_count = value.get("sample_count")
    return {
        "status": "evaluated",
        "evaluation_contract": str(value.get("evaluation_contract") or "")[:80],
        "sample_count": sample_count if type(sample_count) is int else None,
        **metrics,
        "false_recall_threshold": (gate.get("thresholds") or {}).get("false_recall_rate")
            if isinstance(gate.get("thresholds"), dict) else None,
        "gate_ok": gate.get("ok") is True,
        "gate_blocked_reason": str(gate.get("blocked_reason") or "")[:120],
        "persisted_record_id": str(value.get("persisted_record_id") or "")[:80],
    }


def _formal_recall_accepted(quality: object, gate: dict) -> bool:
    from eimemory.evaluation.real_query_schema import (
        PRODUCTION_REAL_QUERY_REPORT_SCHEMA, PRODUCTION_REAL_QUERY_POLICY,
    )
    quality = quality if isinstance(quality, dict) else {}
    evidence = gate.get("recall_quality_evidence")
    evidence = evidence if isinstance(evidence, dict) else {}
    return (quality.get("schema") == PRODUCTION_REAL_QUERY_REPORT_SCHEMA
            and quality.get("policy_schema") == PRODUCTION_REAL_QUERY_POLICY
            and quality.get("dataset_kind") == "production"
            and quality.get("accepted") is True and quality.get("gate_status") == "accepted"
            and quality.get("persisted") is True
            and gate.get("ok") is True and gate.get("vacuous") is not True
            and evidence.get("evaluation_contract") == PRODUCTION_REAL_QUERY_REPORT_SCHEMA
            and evidence.get("formal_business_evidence") is True
            and evidence.get("label_trust_accepted") is True
            and evidence.get("release_authority_verified") is True
            and bool(quality.get("persisted_record_id"))
            and evidence.get("authority_record_id") == quality["persisted_record_id"]
            and quality.get("quality_gate") == gate)
