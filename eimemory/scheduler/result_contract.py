"""Pure nightly result semantics, independent of Runtime and storage.

Job execution success and evaluation readiness are different claims. Known
non-actionable evidence waits stay non-fatal; explicit execution errors and
malformed present reports must not be promoted to success.
"""
from __future__ import annotations

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
                  "evaluation_status": "awaiting_evidence" if ok and result.get("ok") is False else
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
    "daily_brief",
    "rule_evolution",
    "autonomous_evolution",
    "autonomous_learning",
    "autonomous_learning_daily_report",
    "autonomous_learning_dashboard",
    "l5_loop",
    "capability_v3_backfill",
    "capability_v3_dual_write",
    "l5_v3_shadow",
    "l5_v3_reconcile",
    "code_evolution",
    "capability_incubation",
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
})


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
        else:
            reason_counts["reason_not_reported"] = 1
    return {"reason_counts": reason_counts, "result_count": min(len(results), 500),
            "results_truncated": len(results) > 500}


def _auto_review_diagnostics(report: object) -> dict:
    """Bounded counts only: auto-reviewed vs human labels, never label content."""
    if not isinstance(report, dict) or not report:
        return {"status": "not_run"}
    def count(value: object) -> int:
        return value if type(value) is int and 0 <= value <= 1_000_000 else 0
    progress = report.get("dataset_progress") if isinstance(report.get("dataset_progress"), dict) else {}
    by_authority = progress.get("accepted_by_authority") if isinstance(progress.get("accepted_by_authority"), dict) else {}
    policy = report.get("policy") if isinstance(report.get("policy"), dict) else {}
    return {
        "status": str(report.get("status") or "unknown")[:40],
        "criteria_version": str(report.get("criteria_version") or "")[:80],
        "enabled": policy.get("enabled") is True,
        "accepted_count": count(report.get("accepted_count")),
        "pending_count": count(report.get("pending_count")),
        "rejected_count": count(report.get("rejected_count")),
        "accepted_cases_by_authority": {
            "human": count(by_authority.get("human")),
            "auto_review": count(by_authority.get("auto_review")),
        },
        "accepted_case_count": count(progress.get("accepted_case_count")),
        "required_case_count": count(progress.get("required_case_count")),
    }


def nightly_result_diagnostics(report: dict, steps: list[dict]) -> dict:
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
        "recall_quality_accepted": gate.get("ok") is True and gate.get("vacuous") is not True,
        "recall_quality_evidence": gate.get("recall_quality_evidence") or {},
        "recall_label_auto_review": _auto_review_diagnostics(report.get("production_recall_auto_review")),
        "release_acceptance": "not_evaluated_by_scheduler",
        "last_success_at_semantics": "current_run_not_historical_last_success",
        **({"dynamic_capability_evolution": _dynamic_failure_diagnostics(
            report.get("dynamic_capability_evolution"))}
           if "dynamic_capability_evolution" in failures else {}),
    }
