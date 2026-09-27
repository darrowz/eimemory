"""Version-neutral real-source evidence replayed by the current code."""

from __future__ import annotations

from collections import Counter
from typing import Any

from eimemory.evaluation.task_replay import (
    REAL_PROVENANCE_CONTRACT,
    validate_real_replay_source,
)
from eimemory.models.records import ScopeRef
from eimemory.runtime_identity import runtime_package_tree_digest


MIN_VERIFIED_REAL_REPLAY_SAMPLES = 10
MIN_VERIFIED_REAL_REPLAY_TASK_TYPES = 5
MIN_VERIFIED_REAL_REPLAY_PASS_RATE = 0.8


def build_verified_real_replay_summary(
    runtime,
    *,
    scope,
    limit: int = 500,
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
    if record is not None:
        report = record.content.get("report") if isinstance(record.content, dict) else {}
    else:
        report = {}
    if (
        not isinstance(report, dict)
        or report.get("real_provenance_contract") != REAL_PROVENANCE_CONTRACT
        or str(report.get("package_tree_digest") or "") != current_digest
    ):
        return _empty_summary(
            package_tree_digest=current_digest,
            reason="current_code_replay_missing",
        )
    accepted: list[dict[str, Any]] = []
    rejection_reasons: Counter[str] = Counter()
    seen_source_ids: set[str] = set()
    seen_terminal_evidence: set[str] = set()
    for raw_sample in list(report.get("samples") or []):
        if not isinstance(raw_sample, dict):
            rejection_reasons["malformed_sample"] += 1
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
        )
        if provenance.get("ok") is not True:
            rejection_reasons[str(provenance.get("reason") or "source_provenance_invalid")] += 1
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
    task_type_deficit = max(
        0,
        MIN_VERIFIED_REAL_REPLAY_TASK_TYPES - distinct_task_types,
    )
    pass_rate_deficit = max(0.0, MIN_VERIFIED_REAL_REPLAY_PASS_RATE - pass_rate)
    return {
        "ok": bool(
            sample_deficit == 0
            and task_type_deficit == 0
            and pass_rate_deficit == 0.0
        ),
        "reason": "" if sample_count else "verified_real_samples_missing",
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
        "minimum_task_types": MIN_VERIFIED_REAL_REPLAY_TASK_TYPES,
        "task_type_deficit": task_type_deficit,
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
        "minimum_task_types": MIN_VERIFIED_REAL_REPLAY_TASK_TYPES,
        "task_type_deficit": MIN_VERIFIED_REAL_REPLAY_TASK_TYPES,
        "rejection_reasons": {},
    }
