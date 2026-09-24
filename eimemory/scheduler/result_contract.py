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
            ok = False
            error = str(result.get("error") or result.get("blocked_reason") or "step_reported_not_ok")
        elif result.get("ok") is True:
            ok = True
            error = ""
        else:
            # Non-boolean ok is unknown → fail closed
            ok = False
            error = "step_ok_not_boolean"
            result = {**result, "ok": False, "error": error}
    except Exception as exc:  # noqa: BLE001 - nightly continues; report aggregates failures
        result = {"ok": False, "error": f"{type(exc).__name__}:{exc}"}
        ok = False
        error = f"{type(exc).__name__}"
    steps.append({"step": name, "ok": ok, "error": error})
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


def _quality_wait_is_non_actionable(gate: dict) -> bool:
    """Known-item smoke cannot certify quality. That wait is not a job failure."""
    return (
        str(gate.get("blocked_reason") or "") == "recall_quality_evidence_incomplete"
        and not _has_execution_failure(gate)
        and gate.get("ok") is False
    )


def _l5_awaiting_evidence_is_non_actionable(nested: dict) -> bool:
    """Tip-safety not_ready / sample-starved L5 waits must not fail nightly exit."""
    prompt = nested.get("prompt_safety") if isinstance(nested.get("prompt_safety"), dict) else {}
    assessment = nested.get("assessment") if isinstance(nested.get("assessment"), dict) else {}
    if any(_has_execution_failure(part) for part in (nested, prompt, assessment)):
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
            "terminal_transaction_lineage_mismatch",
            "quality_repair_release_unbound",
        }
        for item in missing
    ):
        return True
    reason = str(nested.get("blocked_reason") or nested.get("l5_skipped_reason") or "")
    return reason in {
        "tip_safety_not_ready",
        "prompt_safety_not_ready",
        "terminal_transaction_lineage_mismatch",
        "quality_repair_release_unbound",
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
            continue
        if key == "recall_quality_gate" and _quality_wait_is_non_actionable(nested):
            continue
        if key == "l5_loop" and _l5_awaiting_evidence_is_non_actionable(nested):
            continue
        return False
    knowledge = report.get("knowledge")
    if isinstance(knowledge, dict):
        refresh_status = str(knowledge.get("refresh_status") or "ok")
        if refresh_status not in {"ok", "recovered"} and knowledge.get("retry_required"):
            # retry_required alone is informational; only fail when nested ok says so
            pass
    return True
