"""Version-neutral real-source evidence replayed by the current code."""

from __future__ import annotations

from collections import Counter
from dataclasses import asdict
from typing import Any

from eimemory.evaluation.task_replay import (
    REAL_PROVENANCE_CONTRACT,
    validate_real_replay_source,
)
from eimemory.models.records import ScopeRef
from eimemory.runtime_identity import runtime_package_tree_digest


MIN_VERIFIED_REAL_REPLAY_SAMPLES = 10
MIN_VERIFIED_REAL_REPLAY_PASS_RATE = 0.8

# A report with contradictory provenance cannot become trustworthy merely
# by dropping the damaged samples and retaining enough passing ones.
REAL_REPLAY_AUTHENTICITY_REASONS = frozenset({
    "source_record_unavailable_in_scope",
    "inactive_source_record",
    "untrusted_source_record",
    "invalid_outcome_trace_contract",
    "invalid_outcome_trace_payload",
    "outcome_trace_identity_mismatch",
    "dynamic_capability_catalog_required",
    "task_type_missing",
    "terminal_verifier_invalid",
    "terminal_evidence_invalid",
    "terminal_contract_digest_missing",
    "terminal_contract_digest_mismatch",
    "source_provenance_invalid",
    "source_evidence_digest_mismatch",
    "terminal_evidence_digest_mismatch",
    "source_task_type_mismatch",
    "malformed_sample",
    "malformed_verdict",
    "duplicate_terminal_evidence",
    "duplicate_source_record",
    "case_scope_mismatch",
    "source_record_id_missing",
    "sample_package_tree_digest_mismatch",
    "replay_execution_id_mismatch",
    "replay_report_identity_invalid",
    "verified_real_replay_authenticity_failed",
})


def build_verified_real_replay_summary(
    runtime,
    *,
    scope,
    limit: int = 500,
    catalog: Any | None = None,
) -> dict[str, Any]:
    scope_ref = scope if isinstance(scope, ScopeRef) else ScopeRef.from_dict(scope)
    current_digest = runtime_package_tree_digest()
    record = runtime.store.latest_record_by_meta_value_exact_scope(
        kind="replay_result",
        source="eimemory.real_task_replay",
        status="active",
        scope=scope_ref,
        meta_key="package_tree_digest",
        meta_value=current_digest,
    )
    if record is None:
        return _empty_summary(
            package_tree_digest=current_digest,
            reason="current_code_replay_missing",
        )
    report = record.content.get("report") if isinstance(record.content, dict) else {}
    report = report if isinstance(report, dict) else {}
    execution_id = report.get("replay_execution_id")
    if (
        report.get("ok") is not True
        or report.get("real_provenance_contract") != REAL_PROVENANCE_CONTRACT
        or report.get("package_tree_digest") != current_digest
        or report.get("schema_version") != "real_task_replay.v1"
        or report.get("report_type") != "real_task_replay"
        or report.get("scope") != asdict(scope_ref)
        or not isinstance(execution_id, str)
        or not execution_id.strip()
        or not isinstance(report.get("samples"), list)
    ):
        return {
            **_empty_summary(package_tree_digest=current_digest, reason="replay_report_identity_invalid"),
            "record_id": record.record_id,
        }
    accepted: list[dict[str, Any]] = []
    rejection_reasons: Counter[str] = Counter()
    seen_source_ids: set[str] = set()
    seen_terminal_evidence: set[str] = set()
    source_verification_lost = False
    for raw_sample in report["samples"]:
        if not isinstance(raw_sample, dict):
            rejection_reasons["malformed_sample"] += 1
            continue
        if raw_sample.get("package_tree_digest") != current_digest:
            rejection_reasons["sample_package_tree_digest_mismatch"] += 1
            continue
        if raw_sample.get("replay_execution_id") != execution_id:
            rejection_reasons["replay_execution_id_mismatch"] += 1
            continue
        source_record_id = str(raw_sample.get("source_record_id") or "")
        if not source_record_id:
            rejection_reasons["source_record_id_missing"] += 1
            continue
        if source_record_id in seen_source_ids:
            rejection_reasons["duplicate_source_record"] += 1
            continue
        seen_source_ids.add(source_record_id)
        provenance = validate_real_replay_source(
            runtime,
            source_record_id=source_record_id,
            scope=scope_ref,
            catalog=catalog,
        )
        if provenance.get("ok") is not True:
            rejection_reasons[str(provenance.get("reason") or "source_provenance_invalid")] += 1
            # A previously verified source that is now a rehearsal, failed,
            # or otherwise ineligible has regressed. Originally non-business
            # cases remain ordinary exclusions rather than corruption claims.
            source_verification_lost |= raw_sample.get("real_provenance_ok") is True
            continue
        if str(raw_sample.get("source_evidence_digest") or "") != str(
            provenance.get("source_evidence_digest") or ""
        ):
            rejection_reasons["source_evidence_digest_mismatch"] += 1
            continue
        if str(raw_sample.get("source_task_type") or "") != str(
            provenance.get("task_type") or ""
        ):
            rejection_reasons["source_task_type_mismatch"] += 1
            continue
        terminal_evidence_digest = str(
            provenance.get("terminal_evidence_digest") or ""
        )
        if str(raw_sample.get("terminal_evidence_digest") or "") != terminal_evidence_digest:
            rejection_reasons["terminal_evidence_digest_mismatch"] += 1
            continue
        if terminal_evidence_digest in seen_terminal_evidence:
            rejection_reasons["duplicate_terminal_evidence"] += 1
            continue
        seen_terminal_evidence.add(terminal_evidence_digest)
        if raw_sample.get("executed") is not True:
            rejection_reasons["not_run"] += 1
            continue
        if not isinstance(raw_sample.get("passed"), bool):
            rejection_reasons["malformed_verdict"] += 1
            continue
        accepted.append(
            {
                "source_record_id": source_record_id,
                "task_type": provenance["task_type"],
                "passed": raw_sample["passed"],
            }
        )
    sample_count = len(accepted)
    pass_count = sum(1 for sample in accepted if sample["passed"])
    fail_count = sample_count - pass_count
    pass_rate = pass_count / sample_count if sample_count else 0.0
    distinct_task_types = len({sample["task_type"] for sample in accepted})
    sample_deficit = max(0, MIN_VERIFIED_REAL_REPLAY_SAMPLES - sample_count)
    pass_rate_deficit = max(0.0, MIN_VERIFIED_REAL_REPLAY_PASS_RATE - pass_rate)
    authenticity_failed = source_verification_lost or bool(
        REAL_REPLAY_AUTHENTICITY_REASONS.intersection(rejection_reasons)
    )
    return {
        "ok": bool(
            sample_deficit == 0
            and pass_rate_deficit == 0.0
            and not authenticity_failed
        ),
        "reason": (
            "verified_real_replay_authenticity_failed" if authenticity_failed
            else "" if sample_count else "verified_real_samples_missing"
        ),
        "record_id": record.record_id,
        "replay_execution_id": str(report.get("replay_execution_id") or ""),
        "provenance_contract": REAL_PROVENANCE_CONTRACT,
        "package_tree_digest": current_digest,
        "sample_count": sample_count,
        "minimum_samples": MIN_VERIFIED_REAL_REPLAY_SAMPLES,
        "sample_deficit": sample_deficit,
        "pass_count": pass_count,
        "fail_count": fail_count,
        "pass_rate": pass_rate,
        "minimum_pass_rate": MIN_VERIFIED_REAL_REPLAY_PASS_RATE,
        "pass_rate_deficit": pass_rate_deficit,
        "distinct_task_types": distinct_task_types,
        "rejection_reasons": dict(sorted(rejection_reasons.items())),
    }


def _empty_summary(*, package_tree_digest: str, reason: str) -> dict[str, Any]:
    return {
        "ok": False,
        "reason": reason,
        "record_id": "",
        "replay_execution_id": "",
        "provenance_contract": REAL_PROVENANCE_CONTRACT,
        "package_tree_digest": package_tree_digest,
        "sample_count": 0,
        "minimum_samples": MIN_VERIFIED_REAL_REPLAY_SAMPLES,
        "sample_deficit": MIN_VERIFIED_REAL_REPLAY_SAMPLES,
        "pass_count": 0,
        "fail_count": 0,
        "pass_rate": 0.0,
        "minimum_pass_rate": MIN_VERIFIED_REAL_REPLAY_PASS_RATE,
        "pass_rate_deficit": MIN_VERIFIED_REAL_REPLAY_PASS_RATE,
        "distinct_task_types": 0,
        "rejection_reasons": {},
    }
