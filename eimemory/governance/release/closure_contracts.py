"""Fail-closed structural checks for release reports, not evidence authority.

These checks reject inconsistent summaries before existing record-integrity,
release-lineage and independent-evidence verifiers run. They do not turn a
caller-supplied dictionary, smoke probe, or diagnostic into trusted evidence.
"""
from __future__ import annotations

from hashlib import sha256
import math
import re
from typing import Any, Iterable

LIVE_ACCEPTANCE_CASE_IDS = (
    "store.sqlite_query", "store.scoped_record_read", "memory.store_search_read",
    "sources.registry_read", "governance.policy_ledger_read",
    "governance.skill_registry_read", "governance.dashboard_read",
    "governance.readiness_pure_read", "governance.replay_integrity",
    "deployment.identity",
)
LEGACY_RELEASE_CASE_IDS = (
    "search_recent_source", "search_trending_github", "search_primary_source",
    "research_evidence_gate", "research_conflict_resolution", "research_actionable_takeaway",
    "uumit_requirement_checklist", "uumit_quality_gate", "uumit_post_delivery_followup",
    "device_physical_channel", "device_missing_info", "device_safe_boundary",
)
_SHA40 = re.compile(r"[0-9a-f]{40}")
_SHA64 = re.compile(r"[0-9a-f]{64}")
_SAFE_CODE = re.compile(r"[A-Za-z0-9_.:-]{1,160}")


def _object(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


def _integer(value: Any, *, minimum: int = 0) -> bool:
    return type(value) is int and value >= minimum


def _ratio(value: Any) -> bool:
    if type(value) not in (int, float):
        return False
    try:
        return math.isfinite(value) and 0.0 <= value <= 1.0
    except OverflowError:
        return False


def _name(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip()) and value == value.strip()


def _rows(value: Any) -> bool:
    return isinstance(value, list) and bool(value) and all(isinstance(row, dict) for row in value)


def _unique(rows: list[dict], key: str) -> bool:
    values = [row.get(key) for row in rows]
    return all(_name(value) for value in values) and len(set(values)) == len(values)


def acceptance_report_ok(
    report: Any, *, expected_count: int | None,
    expected_case_ids: Iterable[str] | None = None,
    require_persisted: bool = False,
) -> bool:
    report = _object(report)
    count = report.get("case_count")
    rows = report.get("results")
    if not (
        report.get("ok") is True and report.get("all_passed") is True
        and _integer(count, minimum=1)
        and (expected_count is None or count == expected_count)
        and _integer(report.get("pass_count")) and report["pass_count"] == count
        and _integer(report.get("failed_count")) and report["failed_count"] == 0
        and report.get("distinct_probe_sources") is True
        and report.get("distinct_trace_ids") is True
        and _name(report.get("execution_id")) and _rows(rows) and len(rows) == count
        and type(report.get("persisted")) is bool
    ):
        return False
    if not all(_unique(rows, key) for key in ("case_id", "probe_id", "trace_id")):
        return False
    if expected_case_ids is not None and {r["case_id"] for r in rows} != set(expected_case_ids):
        return False
    if any(
        r.get("passed") is not True or r.get("validator_passed") is not True
        or r.get("error") not in (None, "") or not _name(r.get("capability"))
        or r.get("persisted") is not report["persisted"]
        for r in rows
    ):
        return False
    if require_persisted and report["persisted"] is not True:
        return False
    if report["persisted"]:
        if not all(_unique(rows, k) for k in ("probe_record_id", "trace_record_id")):
            return False
        if any(r["probe_record_id"] != r["probe_id"] or r.get("trace_emitted") is not True for r in rows):
            return False
        if not _integer(report.get("trace_count")) or report["trace_count"] != count:
            return False
    return True


def acceptance_failure_details(report: Any, *, maximum: int = 32) -> dict:
    """Emit phase, safe identifiers and error fingerprints; never raw errors."""
    report = _object(report)
    rows = report.get("results") if isinstance(report.get("results"), list) else []
    failures: list[dict] = []
    for row in rows:
        if not isinstance(row, dict):
            failures.append({"phase": "report_contract", "reason_code": "malformed_result"})
            continue
        if row.get("passed") is True and not row.get("error"):
            continue
        if row.get("validator_passed") is not True:
            phase = "execution_or_contract"
        elif row.get("persisted") is True and not row.get("trace_record_id"):
            phase = "trace_persistence"
        elif row.get("capability_revision_id") and not row.get("evaluation_run_id"):
            phase = "evaluation_persistence"
        else:
            phase = "report_contract"
        error = str(row.get("error") or "")
        item = {"phase": phase, "reason_code": phase + "_failed"}
        for field in ("case_id", "capability", "probe_record_id", "trace_record_id"):
            value = row.get(field)
            if isinstance(value, str) and _SAFE_CODE.fullmatch(value):
                item[field] = value
        if error:
            item["error_sha256"] = sha256(error.encode("utf-8", errors="replace")).hexdigest()
        failures.append(item)
    if not failures:
        failures.append({"phase": "selection_or_report_contract", "reason_code": "acceptance_contract_invalid"})
    limit = max(1, min(128, int(maximum)))
    details = {"failure_count": len(failures), "failures": failures[:limit], "truncated": len(failures) > limit}
    selection_reasons = report.get("blocked_reasons")
    if isinstance(selection_reasons, list) and selection_reasons:
        reason = str(selection_reasons[0])
        allowed = {
            "evaluation_catalog_required", "dynamic_runtime_scope_mismatch",
            "dynamic_evaluation_selection_empty", "empty_case_ids", "unknown_case_ids",
            "hypothesis_context_requires_dynamic_selection", "invalid_hypothesis_context_mapping",
            "profile_evaluation_selection_blocked", "active_evaluation_selection_blocked",
        }
        details["selection_reason_code"] = reason if reason in allowed else "evaluation_selection_failed"
        details["selection_error_sha256"] = sha256(reason.encode("utf-8", errors="replace")).hexdigest()
    return details


def replay_summary_ok(summary: Any, *, minimum_pass_rate: float = 0.8) -> bool:
    summary = _object(summary)
    executed, passed, failed = (summary.get(k) for k in ("executed_count", "pass_count", "fail_count"))
    rate = summary.get("pass_rate")
    return bool(
        _integer(executed, minimum=1) and _integer(passed) and _integer(failed)
        and passed + failed == executed and _ratio(rate) and _ratio(minimum_pass_rate)
        and passed / executed >= minimum_pass_rate
        and abs(rate - round(passed / executed, 3)) <= 0.001
    )


def capability_replay_report_gate(
    report: Any, *, expected_capabilities: list[str] | None, reason_prefix: str,
) -> dict:
    report = _object(report)
    packs = report.get("packs")
    expected = sorted(set(expected_capabilities or []))
    blocked: list[str] = []
    not_executed: set[str] = set()
    failed: set[str] = set()
    duplicate: set[str] = set()
    global_sources: set[str] = set()
    if report.get("ok") is not True or not _rows(packs):
        blocked.append(f"{reason_prefix}_invalid")
        packs = []
    elif not _unique(packs, "capability") or (expected and sorted(p["capability"] for p in packs) != expected):
        blocked.append(f"{reason_prefix}_invalid")
    for pack in packs:
        capability = str(pack.get("capability") or "")
        cases, results = pack.get("cases"), pack.get("case_results")
        if not (
            _rows(cases) and _rows(results) and len(cases) == len(results)
            and _unique(cases, "case_id") and _unique(results, "case_id")
            and {c["case_id"] for c in cases} == {r["case_id"] for r in results}
            and all(r.get("verdict") in ("pass", "fail") for r in results)
        ):
            not_executed.add(capability)
            continue
        thresholds = [c.get("threshold", 0.8) for c in cases]
        rate = pack.get("pass_rate")
        if not all(_ratio(t) for t in thresholds) or not _ratio(rate):
            failed.add(capability)
        else:
            passed = sum(r["verdict"] == "pass" for r in results)
            computed = passed / len(results)
            # Test the unrounded value against the threshold. Rounding a
            # 0.7996 result to 0.800 must not turn a failure into a pass.
            if computed < max(0.8, *thresholds) or abs(rate - round(computed, 3)) > 0.001:
                failed.add(capability)
        if not _unique(results, "evidence_source_id"):
            duplicate.add(capability)
        else:
            sources = {r["evidence_source_id"] for r in results}
            if sources & global_sources:
                duplicate.add(capability)
            global_sources.update(sources)
    for values, suffix in ((not_executed, "not_executed"), (failed, "failed"), (duplicate, "evidence_not_distinct")):
        if values:
            blocked.append(f"{reason_prefix}_{suffix}")
    return {
        "ok": not blocked, "blocked_reasons": list(dict.fromkeys(blocked)),
        "not_executed_capabilities": sorted(not_executed),
        "failed_capabilities": sorted(failed),
        "duplicate_evidence_capabilities": sorted(duplicate),
        "expected_capabilities": expected,
    }


def live_acceptance_report_ok(report: Any, *, receipt: Any) -> bool:
    report, receipt = _object(report), _object(receipt)
    deployment = _object(report.get("deployment"))
    commit, receipt_id, path = (receipt.get(k) for k in ("commit", "promotion_request_id", "release_path"))
    session = receipt.get("release_session_id")
    count = len(LIVE_ACCEPTANCE_CASE_IDS)
    rows = report.get("cases")
    if not (
        report.get("ok") is True and isinstance(commit, str) and _SHA40.fullmatch(commit)
        and _name(receipt_id) and _name(path) and _name(session)
        and deployment.get("commit") == commit
        and deployment.get("promotion_request_id") == receipt_id
        and deployment.get("release_path") == path
        and deployment.get("release_session_id") == session
        and all(type(report.get(k)) is int and report[k] == n for k, n in (
            ("case_count", count), ("pass_count", count), ("fail_count", 0), ("distinct_task_types", count)
        ))
        and _rows(rows) and len(rows) == count
    ):
        return False
    if not all(_unique(rows, k) for k in ("case_id", "record_id", "trace_id", "task_type")):
        return False
    if {r["case_id"] for r in rows} != set(LIVE_ACCEPTANCE_CASE_IDS):
        return False
    return all(
        r.get("passed") is True and r.get("trace_persisted") is True
        and r["task_type"] == f"live.acceptance.{r['case_id']}"
        and isinstance(r.get("observation_digest"), str) and _SHA64.fullmatch(r["observation_digest"])
        and r["trace_id"] == f"live-acceptance:{commit}:{r['case_id']}:{r['observation_digest'][:12]}"
        for r in rows
    )


def legacy_release_replay_ok(report: Any) -> bool:
    """Validate the historic release cohort without granting it dynamic L5 authority."""
    report = _object(report)
    if report.get("ok") is not True or report.get("legacy_compatibility") is not True:
        return False
    acceptance = report.get("capability_acceptance")
    if not acceptance_report_ok(
        acceptance, expected_count=len(LEGACY_RELEASE_CASE_IDS),
        expected_case_ids=LEGACY_RELEASE_CASE_IDS, require_persisted=True,
    ):
        return False
    replay = _object(report.get("capability_replay") or report.get("weak_capability_replay"))
    accepted = {r["case_id"]: r for r in acceptance["results"]}
    capabilities = sorted({r["capability"] for r in accepted.values()})
    if not capability_replay_report_gate(
        replay, expected_capabilities=capabilities, reason_prefix="replay",
    )["ok"]:
        return False
    if not _name(replay.get("manifest_record_id")):
        return False
    ids = replay.get("persisted_replay_ids")
    count = len(accepted)
    if not (type(replay.get("persisted_replay_count")) is int
            and replay["persisted_replay_count"] == count
            and isinstance(ids, list) and len(ids) == count
            and all(_name(value) for value in ids) and len(set(ids)) == count):
        return False
    for pack in replay["packs"]:
        expected = {key for key, row in accepted.items() if row["capability"] == pack["capability"]}
        if {c["case_id"] for c in pack["cases"]} != expected:
            return False
        for row in pack["case_results"]:
            original = accepted[row["case_id"]]
            if (row.get("probe_source_id") != original["probe_record_id"]
                    or row.get("trace_record_id") != original["trace_record_id"]
                    or row.get("trace_id") != original["trace_id"]):
                return False
    return True


def channel_wait_report_ok(report: Any) -> bool:
    """A channel wait is admissible only after successful, bound upstream gates.

    This is not closure certification. It never changes the source report's
    ok/closure_complete bits, and never accepts a failed replay bootstrap.
    """
    report = _object(report)
    deployment = _object(report.get("deployment"))
    receipt = _object(report.get("deployment_receipt"))
    replay = _object(report.get("replay_bootstrap"))
    recall = _object(report.get("production_recall_gate"))
    storage = _object(report.get("storage_migrations"))
    channel = _object(report.get("channel_acceptance"))
    if not (
        report.get("report_type") == "l5_release_closure"
        and report.get("ok") is False and report.get("closure_complete") is False
        and report.get("data_accumulating") is False
        and report.get("blocked_stage") == "channel_acceptance"
        and report.get("blocked_reason") == "current_release_channel_receipt_not_found"
        and channel.get("ok") is False
        and (channel.get("reason") or channel.get("error")) == "current_release_channel_receipt_not_found"
        and receipt.get("ok") is True and storage.get("ok") is True
        and all(deployment.get(k) == receipt.get(k) for k in ("commit", "promotion_request_id", "release_path"))
        and live_acceptance_report_ok(report.get("live_acceptance"), receipt=receipt)
        and replay.get("ok") is True
        and replay.get("legacy_compatibility") is True
        and _object(report.get("pending_checkpoint")).get("ok") is True
        and _object(report.get("pending_checkpoint")).get("status") in {
            "waiting_for_channel_acceptance", "revalidating",
        }
        and ("post_write_reconcile" not in report
             or _object(report["post_write_reconcile"]).get("ok") is True)
    ):
        return False
    if not legacy_release_replay_ok(replay):
        return False
    if recall.get("ok") is True and recall.get("status") == "accepted":
        strict = _object(report.get("production_recall_strict_state"))
        return bool(strict.get("ok") is True and strict.get("status") == "strict_activated"
                    and strict.get("candidate_commit") == receipt.get("commit"))
    pending = _object(recall.get("bootstrap"))
    identity = _object(pending.get("release_identity"))
    return bool(
        recall.get("status") == "data_accumulating"
        and pending.get("ok") is True and pending.get("status") == "bootstrap_data_pending"
        and _name(pending.get("record_id"))
        and identity.get("release_commit") == receipt.get("commit")
        and identity.get("deployment_receipt_id") == receipt.get("promotion_request_id")
        and identity.get("release_session_id") == receipt.get("release_session_id")
    )
