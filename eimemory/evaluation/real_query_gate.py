"""Trusted production-redacted recall regression gate.

This module contains the schema, metric, release-binding, and sanitized evidence
implementation behind the existing production_recall evaluator entry point.
It intentionally defines no separate CLI or scheduler entry point.
"""

from __future__ import annotations

from dataclasses import asdict
from datetime import datetime
from hashlib import sha256
import json
from math import log2
from time import perf_counter
import tracemalloc
from typing import Any

from eimemory.adapters.runtime.channel import (
    SUPPORTED_RUNTIME_CHANNELS,
    normalize_runtime_channel,
    resolve_channel_scope,
    runtime_channel_from_scope,
)
from eimemory.core.clock import now_iso
from eimemory.evaluation.metrics import percentile
from eimemory.governance.evidence_contract import (
    ReleaseIdentity,
    current_release_identity,
    release_identity_payload,
    same_scope,
    verified_deployment_receipt_identity,
)
from eimemory.models.records import RecordEnvelope, ScopeRef
from eimemory.models.source_partitions import normalize_source_id

PRODUCTION_REAL_QUERY_SCHEMA = "production_redacted_v1"
PRODUCTION_REAL_QUERY_REPORT_SCHEMA = "production_recall_gate.v1"
PRODUCTION_REAL_QUERY_POLICY = "production_recall_gate_policy.v1"
PRODUCTION_REAL_QUERY_REQUIRED_CHANNELS = frozenset({"openclaw", "codex", "hermes"})
PRODUCTION_REAL_QUERY_THRESHOLDS: dict[str, float] = {
    "recall_at_5": 0.90,
    "precision_at_5": 0.20,
    "mrr": 0.80,
    "ndcg_at_5": 0.80,
    "top1_stability": 0.90,
    "jaccard_at_5": 0.80,
    "latency_ms_p95": 1500.0,
    "peak_memory_bytes": 67_108_864.0,
}
_REAL_QUERY_MIN_CASES = 15
_REAL_QUERY_MIN_LABELS = 15
_REAL_QUERY_MIN_CASES_PER_CHANNEL = 5
_MAX_QUERY_TERMS = 16
_MAX_QUERY_TERM_CHARS = 64
_MAX_QUERY_FEATURE_CHARS = 512
_DIGEST_KEYS = frozenset(
    {"dataset_digest", "engine_digest", "fusion_digest", "policy_digest", "result_digest"}
)
_RAW_FIELD_MARKERS = frozenset(
    {
        "query",
        "raw_query",
        "query_text",
        "conversation",
        "messages",
        "result_text",
        "returned_text",
        "body",
        "content",
        "secret",
        "password",
        "token",
        "api_key",
    }
)


def freeze_production_recall_dataset(dataset: dict[str, Any]) -> dict[str, Any]:
    """Validate and freeze the non-secret production query contract.

    The returned digest deliberately excludes baseline selection and all run
    state.  A candidate and its trusted predecessor therefore compare exactly
    the same immutable cases.
    """

    raw = dict(dataset or {})
    base_scope = ScopeRef.from_dict(raw.get("scope") or {})
    blocked: list[str] = []
    if str(raw.get("schema") or raw.get("schema_version") or "") != PRODUCTION_REAL_QUERY_SCHEMA:
        blocked.append("unsupported_dataset_schema")
    if str(raw.get("dataset_kind") or "") != "production":
        blocked.append("dataset_not_production")
    if raw.get("seed") or raw.get("seed_records"):
        blocked.append("seeded_dataset_not_eligible")

    frozen_cases: list[dict[str, Any]] = []
    seen_case_ids: set[str] = set()
    channels: set[str] = set()
    channel_counts = {channel: 0 for channel in PRODUCTION_REAL_QUERY_REQUIRED_CHANNELS}
    accepted_label_count = 0
    raw_cases = list(raw.get("cases") or [])
    if len(raw_cases) > 500:
        blocked.append("dataset_case_limit_exceeded")
    for index, value in enumerate(raw_cases[:500]):
        case, reasons = _freeze_real_query_case(value, index=index, base_scope=base_scope)
        for reason in reasons:
            if reason not in blocked:
                blocked.append(reason)
        if case is None:
            continue
        case_id = str(case["case_id"])
        if case_id in seen_case_ids:
            if "duplicate_case_id" not in blocked:
                blocked.append("duplicate_case_id")
        seen_case_ids.add(case_id)
        channels.add(str(case["channel"]))
        if str(case["channel"]) in channel_counts:
            channel_counts[str(case["channel"])] += 1
        accepted_label_count += len(case["labels"])
        frozen_cases.append(case)

    if len(frozen_cases) < _REAL_QUERY_MIN_CASES:
        blocked.append("minimum_case_count_missing")
    if accepted_label_count < _REAL_QUERY_MIN_LABELS:
        blocked.append("minimum_label_count_missing")
    if not PRODUCTION_REAL_QUERY_REQUIRED_CHANNELS.issubset(channels):
        blocked.append("required_channel_coverage_missing")
    if any(count < _REAL_QUERY_MIN_CASES_PER_CHANNEL for count in channel_counts.values()):
        blocked.append("required_channel_minimum_missing")
    frozen = {
        "schema": PRODUCTION_REAL_QUERY_SCHEMA,
        "name": str(raw.get("name") or "production-real-query")[:120],
        "dataset_kind": "production",
        "scope": asdict(base_scope),
        "cases": frozen_cases,
    }
    digest = _stable_digest(frozen)
    return {
        **frozen,
        "dataset_digest": digest,
        "baseline_report_id": str(raw.get("baseline_report_id") or "").strip(),
        "eligibility": {
            "ok": not blocked,
            "status": "eligible" if not blocked else "not_run",
            "blocked_reasons": list(dict.fromkeys(blocked)),
            "case_count": len(frozen_cases),
            "accepted_label_count": accepted_label_count,
            "channel_coverage": sorted(channels),
            "per_channel_case_count": dict(sorted(channel_counts.items())),
        },
    }


def _freeze_real_query_case(
    value: object,
    *,
    index: int,
    base_scope: ScopeRef,
) -> tuple[dict[str, Any] | None, list[str]]:
    if not isinstance(value, dict):
        return None, ["invalid_case"]
    case = dict(value)
    reasons: list[str] = []
    case_id = str(case.get("case_id") or "").strip()
    if not case_id or len(case_id) > 160:
        reasons.append("stable_case_id_required")
    try:
        channel = normalize_runtime_channel(str(case.get("channel") or ""))
    except ValueError:
        channel = ""
        reasons.append("exact_channel_required")
    raw_source_id = str(case.get("source_id") or "").strip()
    if not raw_source_id or raw_source_id == "*":
        reasons.append("exact_source_required")
        source_id = "default"
    else:
        try:
            source_id = normalize_source_id(raw_source_id)
        except ValueError:
            source_id = "default"
            reasons.append("exact_source_required")
    expected_scope = ScopeRef.from_dict(
        resolve_channel_scope(channel, asdict(base_scope)) if channel else asdict(base_scope)
    )
    actual_scope = ScopeRef.from_dict(case.get("scope") or {})
    if not same_scope(actual_scope, expected_scope):
        reasons.append("exact_channel_scope_mismatch")

    window = _bounded_window(case.get("collection_window"))
    if window is None:
        reasons.append("collection_window_invalid")
        window = {"started_at": "", "ended_at": ""}
    features, feature_reason = _bounded_query_features(case.get("query_features"))
    if feature_reason:
        reasons.append(feature_reason)
    query_digest = str(case.get("query_digest") or "").strip().lower()
    if query_digest != _stable_digest(features):
        reasons.append("query_digest_mismatch")

    labels: list[dict[str, Any]] = []
    accepted_label_grades: dict[str, int] = {}
    raw_labels = list(case.get("labels") or [])
    if len(raw_labels) > 16:
        reasons.append("case_label_limit_exceeded")
    for label in raw_labels[:16]:
        if not isinstance(label, dict) or label.get("accepted") is not True:
            continue
        record_ref = str(label.get("record_ref") or "").strip()
        grade = label.get("grade")
        provenance = label.get("provenance") if isinstance(label.get("provenance"), dict) else {}
        if (
            not record_ref
            or isinstance(grade, bool)
            or not isinstance(grade, int)
            or grade < 1
            or grade > 3
            or not all(str(provenance.get(key) or "").strip() for key in ("labeler", "labelled_at", "evidence_ref"))
        ):
            reasons.append("accepted_label_invalid")
            continue
        bounded_ref = record_ref[:200]
        previous_grade = accepted_label_grades.get(bounded_ref)
        if previous_grade is not None:
            if previous_grade != int(grade):
                reasons.append("accepted_label_grade_conflict")
            continue
        accepted_label_grades[bounded_ref] = int(grade)
        labels.append(
            {
                "record_ref": bounded_ref,
                "grade": int(grade),
                "provenance": {
                    "labeler": str(provenance["labeler"])[:80],
                    "labelled_at": str(provenance["labelled_at"])[:80],
                    "evidence_ref": str(provenance["evidence_ref"])[:200],
                },
            }
        )
    if not labels:
        reasons.append("accepted_labels_missing")
    provenance = case.get("provenance") if isinstance(case.get("provenance"), dict) else {}
    if not all(str(provenance.get(key) or "").strip() for key in ("collector", "capture_ref")):
        reasons.append("case_provenance_missing")

    frozen = {
        "case_id": case_id or f"invalid-{index}",
        "collection_window": window,
        "channel": channel,
        "source_id": source_id,
        "scope": asdict(actual_scope),
        "query_features": features,
        "query_digest": query_digest,
        "labels": sorted(labels, key=lambda item: (item["record_ref"], -item["grade"])),
        "provenance": {
            "collector": str(provenance.get("collector") or "")[:80],
            "capture_ref": str(provenance.get("capture_ref") or "")[:200],
        },
    }
    return frozen, list(dict.fromkeys(reasons))


def _bounded_window(value: object) -> dict[str, str] | None:
    if not isinstance(value, dict):
        return None
    started = str(value.get("started_at") or "").strip()
    ended = str(value.get("ended_at") or "").strip()
    try:
        start_dt = datetime.fromisoformat(started.replace("Z", "+00:00"))
        end_dt = datetime.fromisoformat(ended.replace("Z", "+00:00"))
    except ValueError:
        return None
    if start_dt.tzinfo is None or end_dt.tzinfo is None or start_dt >= end_dt:
        return None
    return {"started_at": started[:80], "ended_at": ended[:80]}


def _bounded_query_features(value: object) -> tuple[dict[str, Any], str]:
    if not isinstance(value, dict):
        return {"terms": [], "intent": "", "entities": [], "language": ""}, "query_features_invalid"
    if any(str(key).strip().lower() in _RAW_FIELD_MARKERS for key in value):
        return {"terms": [], "intent": "", "entities": [], "language": ""}, "query_features_not_redacted"
    allowed = {"terms", "intent", "entities", "language"}
    if any(str(key) not in allowed for key in value):
        return {"terms": [], "intent": "", "entities": [], "language": ""}, "query_features_not_redacted"
    terms = [str(item).strip() for item in list(value.get("terms") or []) if str(item).strip()]
    entities = [str(item).strip() for item in list(value.get("entities") or []) if str(item).strip()]
    intent = str(value.get("intent") or "").strip()
    language = str(value.get("language") or "").strip()
    all_values = [*terms, *entities, intent, language]
    unsafe = (
        not terms
        or len(terms) > _MAX_QUERY_TERMS
        or len(entities) > _MAX_QUERY_TERMS
        or any(len(item) > _MAX_QUERY_TERM_CHARS for item in all_values)
        or sum(len(item) for item in all_values) > _MAX_QUERY_FEATURE_CHARS
        or any(_looks_like_secret(item) for item in all_values)
    )
    frozen: dict[str, Any] = {"terms": terms[:_MAX_QUERY_TERMS]}
    if intent:
        frozen["intent"] = intent[:_MAX_QUERY_TERM_CHARS]
    if entities:
        frozen["entities"] = entities[:_MAX_QUERY_TERMS]
    if language:
        frozen["language"] = language[:16]
    return frozen, "query_features_not_redacted" if unsafe else ""


def _looks_like_secret(value: str) -> bool:
    lowered = str(value or "").lower()
    return any(marker in lowered for marker in ("password=", "token=", "api_key=", "bearer ", "sk-"))


def evaluate_labeled_ranking_at_5(
    *,
    candidate_refs: list[str],
    labels: list[dict[str, Any]],
    corpus_result_capacity: int,
    baseline_refs: list[str] | None = None,
) -> dict[str, float]:
    refs = _stable_unique_refs(candidate_refs, limit=5)
    grades: dict[str, int] = {}
    for label in labels:
        ref = str(label.get("record_ref") or "")
        grade = int(label.get("grade") or 0)
        if ref and grade > grades.get(ref, 0):
            grades[ref] = grade
    relevant = set(grades)
    relevant_returned = [ref for ref in refs if ref in relevant]
    capacity = max(0, int(corpus_result_capacity))
    denominator = min(5, capacity)
    first_rank = next((index for index, ref in enumerate(refs, start=1) if ref in relevant), 0)
    dcg = sum(((2 ** grades[ref]) - 1) / log2(index + 1) for index, ref in enumerate(refs, start=1) if ref in grades)
    ideal = sorted(grades.values(), reverse=True)[:5]
    idcg = sum(((2 ** grade) - 1) / log2(index + 1) for index, grade in enumerate(ideal, start=1))
    baseline = _stable_unique_refs(list(baseline_refs or []), limit=5)
    union = set(refs) | set(baseline)
    return {
        "recall_at_5": len(set(relevant_returned)) / len(relevant) if relevant else 0.0,
        "precision_at_5": len(set(relevant_returned)) / denominator if denominator else 0.0,
        "mrr": 1.0 / first_rank if first_rank else 0.0,
        "ndcg_at_5": dcg / idcg if idcg else 0.0,
        "top1_stable": 1.0 if refs and baseline and refs[0] == baseline[0] else 0.0,
        "jaccard_at_5": len(set(refs) & set(baseline)) / len(union) if union else 1.0,
    }


def _stable_unique_refs(values: Any, *, limit: int) -> list[str]:
    refs: list[str] = []
    seen: set[str] = set()
    for value in values:
        ref = str(value or "")
        if not ref or ref in seen:
            continue
        seen.add(ref)
        refs.append(ref)
        if len(refs) >= limit:
            break
    return refs


def run_real_query_gate(
    runtime: Any,
    dataset: dict[str, Any],
    *,
    seed: bool,
    scope: dict[str, Any] | None,
    persist_report: bool,
) -> dict[str, Any]:
    frozen = freeze_production_recall_dataset(dataset)
    dataset_scope = ScopeRef.from_dict({**dict(frozen["scope"]), **dict(scope or {})})
    eligibility = dict(frozen["eligibility"])
    if not same_scope(dataset_scope, ScopeRef.from_dict(frozen["scope"])):
        eligibility["ok"] = False
        eligibility["status"] = "not_run"
        eligibility["blocked_reasons"] = [*eligibility["blocked_reasons"], "dataset_scope_override_mismatch"]
    release = current_release_identity(runtime, dataset_scope)
    if release is None or not release.complete:
        return _not_run_real_query_report(frozen, "release_identity_unavailable")
    if not eligibility.get("ok"):
        not_run = _not_run_real_query_report(
            frozen,
            str((eligibility.get("blocked_reasons") or ["dataset_not_eligible"])[0]),
            release=release,
        )
        if str(dataset.get("dataset_kind") or "") == "production":
            return _persist_eligible_high_water(
                runtime,
                not_run,
                scope=dataset_scope,
                persist=persist_report,
            )
        return not_run

    labels_ok, label_reason, capacities = _hydrate_real_query_labels(runtime, frozen["cases"])
    if not labels_ok:
        return _persist_eligible_high_water(
            runtime,
            _not_run_real_query_report(frozen, label_reason, release=release),
            scope=dataset_scope,
            persist=persist_report,
        )
    baseline, baseline_reason = _resolve_trusted_baseline(
        runtime,
        scope=dataset_scope,
        report_id=str(frozen.get("baseline_report_id") or ""),
        dataset_digest=str(frozen["dataset_digest"]),
        current_release=release,
    )
    if baseline is None:
        # This remains eligible evidence and must advance the high-water mark.
        # It can act as an immutable baseline only after a later deployment
        # receipt proves this commit is the verified predecessor.
        report = _evaluate_real_query_candidate(
            runtime,
            frozen=frozen,
            release=release,
            baseline=None,
            capacities=capacities,
        )
        report.update(
            {
                "accepted": False,
                "gate_status": "not_run",
                "blocked_reason": baseline_reason,
                "baseline_capture": report.get("cross_channel_leakage_count") == 0
                and report.get("source_filter_leakage_count") == 0,
            }
        )
        return _persist_eligible_high_water(runtime, report, scope=dataset_scope, persist=persist_report)

    report = _evaluate_real_query_candidate(
        runtime,
        frozen=frozen,
        release=release,
        baseline=baseline,
        capacities=capacities,
    )
    return _persist_eligible_high_water(runtime, report, scope=dataset_scope, persist=persist_report)


def _hydrate_real_query_labels(
    runtime: Any,
    cases: list[dict[str, Any]],
) -> tuple[bool, str, dict[str, int]]:
    capacities: dict[str, int] = {}
    for case in cases:
        scope = ScopeRef.from_dict(case["scope"])
        source_id = str(case["source_id"])
        for label in case["labels"]:
            record = runtime.store.get_by_id(str(label["record_ref"]), scope=scope)
            if record is None:
                return False, "accepted_label_record_missing", {}
            if (
                record.status != "active"
                or not same_scope(record.scope, scope)
                or record.source_id != source_id
                or _record_runtime_channel(record) != str(case["channel"])
            ):
                return False, "accepted_label_boundary_mismatch", {}
        capacity = _trusted_result_capacity(runtime, scope=scope, source_id=source_id)
        if capacity <= 0:
            return False, "eligible_corpus_empty", {}
        capacities[str(case["case_id"])] = capacity
    return True, "", capacities


def _trusted_result_capacity(runtime: Any, *, scope: ScopeRef, source_id: str) -> int:
    return min(
        5,
        int(
            runtime.store.count_records_exact_scope(
                scope=scope,
                status="active",
                source_ids=[source_id],
            )
        ),
    )


def _resolve_trusted_baseline(
    runtime: Any,
    *,
    scope: ScopeRef,
    report_id: str,
    dataset_digest: str,
    current_release: ReleaseIdentity,
) -> tuple[dict[str, Any] | None, str]:
    current_receipt = runtime.store.get_by_id(current_release.receipt_id, scope=scope)
    if current_receipt is None or verified_deployment_receipt_identity(current_receipt) != current_release:
        return None, "current_deployment_receipt_invalid"
    side_effect = current_receipt.content.get("side_effect") if isinstance(current_receipt.content, dict) else {}
    verification = side_effect.get("verification") if isinstance(side_effect, dict) and isinstance(side_effect.get("verification"), dict) else {}
    prior_commit = str(verification.get("prior_commit") or "").strip().lower()
    if len(prior_commit) != 40 or prior_commit == current_release.commit:
        return None, "verified_prior_release_unavailable"
    if not report_id:
        return None, "baseline_report_id_missing"
    record = runtime.store.get_by_id(report_id, scope=scope)
    if (
        record is None
        or record.kind != "reflection"
        or record.source != "eimemory.evaluation.production_recall"
        or record.status != "active"
        or not same_scope(record.scope, scope)
    ):
        return None, "baseline_report_untrusted"
    report = record.content.get("report") if isinstance(record.content, dict) and isinstance(record.content.get("report"), dict) else {}
    if not _record_payload_digest_valid(record, report):
        return None, "baseline_report_payload_tampered"
    if report.get("attempt_id") and str(report.get("attempt_id") or "") != record.record_id:
        return None, "baseline_attempt_identity_invalid"
    identity = _release_from_payload(report.get("release_identity"))
    if identity is None or identity.commit != prior_commit or identity.commit == current_release.commit:
        return None, "baseline_release_not_verified_predecessor"
    receipt = runtime.store.get_by_id(identity.receipt_id, scope=scope)
    if receipt is None or verified_deployment_receipt_identity(receipt) != identity:
        return None, "baseline_deployment_receipt_invalid"
    if not _validate_persisted_real_query_report(report, expected_release=identity):
        return None, "baseline_report_contract_invalid"
    baseline_gate = report.get("threshold_gate") if isinstance(report.get("threshold_gate"), dict) else {}
    baseline_blocks = baseline_gate.get("blocking_metrics") if isinstance(baseline_gate.get("blocking_metrics"), dict) else {}
    accepted_baseline = bool(
        report.get("accepted") is True
        and report.get("gate_status") == "accepted"
        and baseline_gate.get("ok") is True
        and not baseline_blocks
    )
    bootstrap_baseline = bool(
        report.get("accepted") is False
        and report.get("gate_status") == "not_run"
        and report.get("baseline_capture") is True
        and str(report.get("blocked_reason") or "") in {
            "baseline_report_id_missing",
            "verified_prior_release_unavailable",
        }
        and set(baseline_blocks) == {"baseline"}
    )
    if not (accepted_baseline or bootstrap_baseline):
        return None, "baseline_report_not_qualified"
    if str(report.get("dataset_digest") or "") != dataset_digest:
        return None, "baseline_dataset_digest_mismatch"
    results = report.get("result_refs") if isinstance(report.get("result_refs"), dict) else {}
    if str(report.get("result_digest") or "") != _stable_digest(results):
        return None, "baseline_result_digest_mismatch"
    return {
        "report_id": str(report.get("report_id") or record.record_id),
        "logical_report_id": str(report.get("report_id") or record.record_id),
        "persisted_record_id": record.record_id,
        "release_identity": release_identity_payload(identity),
        "dataset_digest": dataset_digest,
        "engine_digest": str(report["engine_digest"]),
        "fusion_digest": str(report["fusion_digest"]),
        "policy_digest": str(report["policy_digest"]),
        "result_digest": str(report["result_digest"]),
        "result_refs": {
            str(key): _stable_unique_refs(list(value), limit=5)
            for key, value in results.items()
        },
        "metrics": dict(report.get("metrics") or {}),
    }, ""


def _release_from_payload(value: object) -> ReleaseIdentity | None:
    if not isinstance(value, dict):
        return None
    identity = ReleaseIdentity(
        commit=str(value.get("release_commit") or ""),
        version=str(value.get("release_version") or ""),
        receipt_id=str(value.get("deployment_receipt_id") or ""),
        session_id=str(value.get("release_session_id") or ""),
    )
    return identity if identity.complete else None


def _evaluate_real_query_candidate(
    runtime: Any,
    *,
    frozen: dict[str, Any],
    release: ReleaseIdentity,
    baseline: dict[str, Any] | None,
    capacities: dict[str, int],
) -> dict[str, Any]:
    started_tracing = tracemalloc.is_tracing()
    before_current = 0
    sampled_peak_delta = 0
    if started_tracing:
        before_current = int(tracemalloc.get_traced_memory()[0])
    else:
        tracemalloc.start()
    samples: list[dict[str, Any]] = []
    result_refs: dict[str, list[str]] = {}
    cross_channel_leakage = 0
    source_filter_leakage = 0
    try:
        for case in frozen["cases"]:
            features = dict(case["query_features"])
            query = " ".join([*features.get("terms", []), *features.get("entities", []), str(features.get("intent") or "")]).strip()
            task_context = {
                "source_ids": [str(case["source_id"])],
                "target_source_id": str(case["source_id"]),
                "runtime_channel": str(case["channel"]),
                "evaluation_policy": PRODUCTION_REAL_QUERY_POLICY,
            }
            start = perf_counter()
            bundle = runtime.memory.recall(
                query=query,
                scope=dict(case["scope"]),
                task_context=task_context,
                limit=5,
            )
            latency_ms = (perf_counter() - start) * 1000.0
            if started_tracing:
                sampled_peak_delta = max(
                    sampled_peak_delta,
                    max(0, int(tracemalloc.get_traced_memory()[0]) - before_current),
                )
            returned: list[RecordEnvelope] = []
            returned_ids: set[str] = set()
            for item in bundle.items:
                record_id = str(item.record_id or "")
                if not record_id or record_id in returned_ids:
                    continue
                returned_ids.add(record_id)
                returned.append(item)
                if len(returned) >= 5:
                    break
            refs = [str(item.record_id) for item in returned]
            result_refs[str(case["case_id"])] = refs
            case_cross = sum(
                1 for item in returned if _record_runtime_channel(item) != str(case["channel"])
            )
            case_source = sum(1 for item in returned if item.source_id != str(case["source_id"]))
            cross_channel_leakage += case_cross
            source_filter_leakage += case_source
            baseline_refs = list((baseline or {}).get("result_refs", {}).get(str(case["case_id"]), []))
            ranking = evaluate_labeled_ranking_at_5(
                candidate_refs=refs,
                labels=list(case["labels"]),
                corpus_result_capacity=int(capacities[str(case["case_id"])]),
                baseline_refs=baseline_refs,
            )
            samples.append(
                {
                    "case_id": str(case["case_id"]),
                    "channel": str(case["channel"]),
                    "source_id": str(case["source_id"]),
                    "query_digest": str(case["query_digest"]),
                    "label_refs": [str(label["record_ref"]) for label in case["labels"]],
                    "label_grades": [int(label["grade"]) for label in case["labels"]],
                    "returned_refs": refs,
                    "corpus_result_capacity": int(capacities[str(case["case_id"])]),
                    "latency_ms": round(latency_ms, 3),
                    "cross_channel_leakage_count": case_cross,
                    "source_filter_leakage_count": case_source,
                    **{key: round(float(value), 6) for key, value in ranking.items()},
                }
            )
        _current, peak = tracemalloc.get_traced_memory()
        peak_memory = sampled_peak_delta if started_tracing else int(peak)
    finally:
        if not started_tracing and tracemalloc.is_tracing():
            tracemalloc.stop()
    latencies = [float(item["latency_ms"]) for item in samples]
    metric_fields = {
        "recall_at_5": "recall_at_5",
        "precision_at_5": "precision_at_5",
        "mrr": "mrr",
        "ndcg_at_5": "ndcg_at_5",
        "top1_stability": "top1_stable",
        "jaccard_at_5": "jaccard_at_5",
    }
    metrics = {
        name: round(sum(float(item[field]) for item in samples) / len(samples), 6) if samples else 0.0
        for name, field in metric_fields.items()
    }
    metrics.update(
        {
            "latency_ms_p50": percentile(latencies, 50),
            "latency_ms_p95": percentile(latencies, 95),
            "peak_memory_bytes": peak_memory,
        }
    )
    retrieval_identity = _retrieval_identity(runtime, samples=samples)
    proactive = _production_proactive_metrics(runtime, frozen["cases"])
    baseline_metrics = dict((baseline or {}).get("metrics") or {})
    gate = _real_query_threshold_gate(
        metrics,
        baseline_metrics=baseline_metrics,
        cross_channel_leakage=cross_channel_leakage,
        source_filter_leakage=source_filter_leakage,
        has_baseline=baseline is not None,
    )
    release_payload = release_identity_payload(release)
    result_digest = _stable_digest(result_refs)
    report_seed = {
        "schema": PRODUCTION_REAL_QUERY_REPORT_SCHEMA,
        "release_identity": release_payload,
        "dataset_digest": frozen["dataset_digest"],
        **retrieval_identity,
        "result_digest": result_digest,
        "policy_schema": PRODUCTION_REAL_QUERY_POLICY,
        "gate_outcome": "accepted" if gate["ok"] and baseline is not None else "blocked",
        "blocking_metrics": sorted((gate.get("blocking_metrics") or {}).keys()),
        "cross_channel_leakage_count": cross_channel_leakage,
        "source_filter_leakage_count": source_filter_leakage,
    }
    report_id = "prg_" + _stable_digest(report_seed)[:32]
    return {
        "ok": bool(gate["ok"]),
        "accepted": bool(gate["ok"] and baseline is not None),
        "gate_status": "accepted" if gate["ok"] and baseline is not None else "blocked",
        "blocked_reason": "" if gate["ok"] and baseline is not None else str(gate.get("blocked_reason") or "baseline_unavailable"),
        "schema": PRODUCTION_REAL_QUERY_REPORT_SCHEMA,
        "report_type": "production_recall_gate",
        "report_id": report_id,
        "generated_at": now_iso(),
        "dataset_kind": "production",
        "scope": dict(frozen["scope"]),
        "release_identity": release_payload,
        "deployment_receipt_id": release.receipt_id,
        "dataset_digest": str(frozen["dataset_digest"]),
        **retrieval_identity,
        "policy_schema": PRODUCTION_REAL_QUERY_POLICY,
        "result_digest": result_digest,
        "result_refs": result_refs,
        "baseline_identity": {
            key: value for key, value in dict(baseline or {}).items() if key != "result_refs" and key != "metrics"
        },
        "sample_count": len(samples),
        "metrics": metrics,
        **metrics,
        "cross_channel_leakage_count": cross_channel_leakage,
        "source_filter_leakage_count": source_filter_leakage,
        "proactive_metrics": proactive,
        "threshold_gate": gate,
        "eligibility": dict(frozen["eligibility"]),
        "samples": samples,
        "persisted": False,
        "persisted_record_id": "",
    }


def _retrieval_identity(runtime: Any, *, samples: list[dict[str, Any]]) -> dict[str, str]:
    engine = getattr(getattr(runtime, "memory", None), "recall_engine", None)
    engine_payload = {
        "class": f"{type(engine).__module__}.{type(engine).__qualname__}",
        "policy_version": str(getattr(engine, "policy_version", "")),
    }
    try:
        from eimemory.retrieval.fusion import FUSION_POLICY_VERSION
    except ImportError:  # pragma: no cover
        FUSION_POLICY_VERSION = "unavailable"
    fusion_payload = {"policy_version": FUSION_POLICY_VERSION}
    policy_payload = {
        "schema": PRODUCTION_REAL_QUERY_POLICY,
        "thresholds": PRODUCTION_REAL_QUERY_THRESHOLDS,
        "k": 5,
    }
    return {
        "engine_digest": _stable_digest(engine_payload),
        "fusion_digest": _stable_digest(fusion_payload),
        "policy_digest": _stable_digest(policy_payload),
    }


def _production_proactive_metrics(runtime: Any, cases: list[dict[str, Any]]) -> dict[str, int]:
    totals = {key: 0 for key in ("volunteered", "injected", "used", "not_used", "rejected", "control")}
    seen: set[tuple[str, str]] = set()
    for case in cases:
        namespace = (str(case["channel"]), str(case["source_id"]))
        if namespace in seen:
            continue
        seen.add(namespace)
        try:
            report = runtime.proactive.paired_metrics(
                scope=dict(case["scope"]),
                channel=str(case["channel"]),
                source_ids=[str(case["source_id"])],
            )
        except Exception:
            continue
        control = report.get("control") if isinstance(report.get("control"), dict) else {}
        treatment = report.get("treatment") if isinstance(report.get("treatment"), dict) else {}
        totals["volunteered"] += int(report.get("volunteered_count") or 0)
        totals["injected"] += int(report.get("injected_count") or 0)
        for arm in (control, treatment):
            totals["used"] += int(arm.get("used") or 0)
            totals["not_used"] += int(arm.get("not_used") or 0)
            totals["rejected"] += int(arm.get("rejected") or 0)
        totals["control"] += sum(int(control.get(key) or 0) for key in control)
    return totals


def _real_query_threshold_gate(
    metrics: dict[str, Any],
    *,
    baseline_metrics: dict[str, Any],
    cross_channel_leakage: int,
    source_filter_leakage: int,
    has_baseline: bool,
) -> dict[str, Any]:
    blocking: dict[str, dict[str, Any]] = {}
    for name, threshold in PRODUCTION_REAL_QUERY_THRESHOLDS.items():
        if not has_baseline and name in {"top1_stability", "jaccard_at_5"}:
            continue
        actual = float(metrics.get(name) or 0.0)
        if name in {"latency_ms_p95", "peak_memory_bytes"}:
            if actual > threshold:
                blocking[name] = {"actual": actual, "threshold": threshold, "operator": "<="}
        elif actual < threshold:
            blocking[name] = {"actual": actual, "threshold": threshold, "operator": ">="}
    if has_baseline:
        for name in ("recall_at_5", "precision_at_5", "mrr", "ndcg_at_5", "top1_stability", "jaccard_at_5"):
            baseline = baseline_metrics.get(name)
            if isinstance(baseline, (int, float)) and not isinstance(baseline, bool) and float(metrics.get(name) or 0.0) < float(baseline):
                blocking[f"{name}_regression"] = {
                    "actual": float(metrics.get(name) or 0.0),
                    "baseline": float(baseline),
                    "operator": ">=",
                }
    if cross_channel_leakage != 0:
        blocking["cross_channel_leakage_count"] = {"actual": cross_channel_leakage, "threshold": 0, "operator": "=="}
    if source_filter_leakage != 0:
        blocking["source_filter_leakage_count"] = {"actual": source_filter_leakage, "threshold": 0, "operator": "=="}
    if not has_baseline:
        blocking["baseline"] = {"actual": "unavailable", "threshold": "trusted_prior_release", "operator": "=="}
    return {
        "ok": not blocking,
        "schema": PRODUCTION_REAL_QUERY_POLICY,
        "blocked_reason": "" if not blocking else "production_recall_gate_failed",
        "thresholds": dict(PRODUCTION_REAL_QUERY_THRESHOLDS),
        "blocking_metrics": blocking,
    }


def _not_run_real_query_report(
    frozen: dict[str, Any],
    reason: str,
    *,
    release: ReleaseIdentity | None = None,
) -> dict[str, Any]:
    release_payload = release_identity_payload(release) if release is not None else {}
    report_id = "prg_" + _stable_digest(
        {
            "schema": PRODUCTION_REAL_QUERY_REPORT_SCHEMA,
            "dataset_digest": frozen.get("dataset_digest"),
            "release_identity": release_payload,
            "reason": reason,
        }
    )[:32]
    return {
        "ok": False,
        "accepted": False,
        "gate_status": "not_run",
        "blocked_reason": str(reason),
        "schema": PRODUCTION_REAL_QUERY_REPORT_SCHEMA,
        "report_type": "production_recall_gate",
        "report_id": report_id,
        "generated_at": now_iso(),
        "dataset_kind": "production",
        "scope": dict(frozen.get("scope") or {}),
        "release_identity": release_payload,
        "deployment_receipt_id": str(release_payload.get("deployment_receipt_id") or ""),
        "dataset_digest": str(frozen.get("dataset_digest") or ""),
        "engine_digest": "",
        "fusion_digest": "",
        "policy_digest": _stable_digest({"schema": PRODUCTION_REAL_QUERY_POLICY, "thresholds": PRODUCTION_REAL_QUERY_THRESHOLDS, "k": 5}),
        "policy_schema": PRODUCTION_REAL_QUERY_POLICY,
        "result_digest": _stable_digest({}),
        "result_refs": {},
        "baseline_identity": {},
        "sample_count": 0,
        "metrics": {},
        "cross_channel_leakage_count": 0,
        "source_filter_leakage_count": 0,
        "proactive_metrics": {key: 0 for key in ("volunteered", "injected", "used", "not_used", "rejected", "control")},
        "threshold_gate": {"ok": False, "schema": PRODUCTION_REAL_QUERY_POLICY, "blocked_reason": str(reason), "thresholds": dict(PRODUCTION_REAL_QUERY_THRESHOLDS), "blocking_metrics": {}},
        "eligibility": dict(frozen.get("eligibility") or {}),
        "samples": [],
        "persisted": False,
        "persisted_record_id": "",
    }


def _persist_eligible_high_water(
    runtime: Any,
    report: dict[str, Any],
    *,
    scope: ScopeRef,
    persist: bool,
) -> dict[str, Any]:
    if not persist or not report.get("release_identity"):
        return {**report, "persisted": False, "persisted_record_id": ""}

    def mutation(sqlite: Any) -> tuple[dict[str, Any], list[RecordEnvelope], list[Any]]:
        latest = sqlite.latest_record_by_meta_value_exact_scope(
            kind="reflection",
            source="eimemory.evaluation.production_recall",
            status="active",
            scope=scope,
            meta_key="report_type",
            meta_value="production_recall_gate",
        )
        latest_report = (
            latest.content.get("report")
            if latest is not None
            and isinstance(latest.content, dict)
            and isinstance(latest.content.get("report"), dict)
            else None
        )
        if (
            latest is not None
            and isinstance(latest_report, dict)
            and str(latest_report.get("report_id") or "") == str(report.get("report_id") or "")
            and _record_payload_digest_valid(latest, latest_report)
        ):
            return (
                _api_report_from_persisted(latest_report, record_id=latest.record_id),
                [],
                [],
            )

        previous_attempt_id = latest.record_id if latest is not None else ""
        attempt_id = "prga_" + _stable_digest(
            {
                "report_id": str(report.get("report_id") or ""),
                "previous_attempt_id": previous_attempt_id,
            }
        )[:32]
        record = _real_query_report_record(
            report,
            scope=scope,
            attempt_id=attempt_id,
            previous_attempt_id=previous_attempt_id,
        )
        existing = sqlite.get_by_id(attempt_id, scope=scope)
        if existing is not None:
            existing_report = (
                existing.content.get("report")
                if isinstance(existing.content, dict)
                and isinstance(existing.content.get("report"), dict)
                else None
            )
            if not isinstance(existing_report, dict) or not _record_payload_digest_valid(existing, existing_report):
                raise ValueError("deterministic production recall attempt conflict")
            return (
                _api_report_from_persisted(existing_report, record_id=existing.record_id),
                [],
                [],
            )
        sqlite.upsert(record, commit=False)
        persisted_report = dict(record.content["report"])
        return (
            _api_report_from_persisted(persisted_report, record_id=record.record_id),
            [record],
            [],
        )

    return runtime.store.mutate_records_atomically(mutation)


def _api_report_from_persisted(report: dict[str, Any], *, record_id: str) -> dict[str, Any]:
    metrics = report.get("metrics") if isinstance(report.get("metrics"), dict) else {}
    return {
        **report,
        **metrics,
        "persisted": True,
        "persisted_record_id": record_id,
    }


def _real_query_report_record(
    report: dict[str, Any],
    *,
    scope: ScopeRef,
    attempt_id: str = "",
    previous_attempt_id: str = "",
) -> RecordEnvelope:
    payload = _sanitized_real_query_report(report)
    if attempt_id:
        payload["attempt_id"] = attempt_id
        payload["previous_attempt_id"] = previous_attempt_id
    record = RecordEnvelope.create(
        kind="reflection",
        title=f"Production recall gate {str(report.get('report_id') or '')[-12:]}",
        summary=f"Production recall gate {report.get('gate_status')}: {report.get('blocked_reason') or 'passed'}.",
        detail="",
        content={"report": payload},
        tags=["evaluation", "production_recall_gate", str(report.get("gate_status") or "")],
        source="eimemory.evaluation.production_recall",
        scope=scope,
        status="active",
        evidence=[str(report.get("deployment_receipt_id") or "")],
        meta={
            "report_type": "production_recall_gate",
            "schema": PRODUCTION_REAL_QUERY_REPORT_SCHEMA,
            "report_id": str(report.get("report_id") or ""),
            "gate_status": str(report.get("gate_status") or ""),
            "accepted": bool(report.get("accepted")),
            "dataset_digest": str(report.get("dataset_digest") or ""),
            "attempt_id": attempt_id,
            "previous_attempt_id": previous_attempt_id,
            "report_payload_digest": _stable_digest(payload),
            **dict(report.get("release_identity") or {}),
        },
    )
    record.record_id = attempt_id or str(report.get("report_id") or record.record_id)
    return record


def _sanitized_real_query_report(report: dict[str, Any]) -> dict[str, Any]:
    allowed = {
        "ok", "accepted", "gate_status", "blocked_reason", "schema", "report_type", "report_id",
        "generated_at", "dataset_kind", "scope", "release_identity", "deployment_receipt_id",
        "dataset_digest", "engine_digest", "fusion_digest", "policy_digest", "policy_schema",
        "result_digest", "result_refs", "baseline_identity", "sample_count", "metrics",
        "cross_channel_leakage_count", "source_filter_leakage_count", "proactive_metrics",
        "threshold_gate", "eligibility", "samples", "baseline_capture", "attempt_id",
        "previous_attempt_id",
    }
    return {key: _bounded_safe_value(value) for key, value in report.items() if key in allowed}


def _record_payload_digest_valid(record: RecordEnvelope, report: dict[str, Any]) -> bool:
    expected = str(record.meta.get("report_payload_digest") or "")
    return len(expected) == 64 and expected == _stable_digest(report)


def _bounded_safe_value(value: Any, *, depth: int = 0) -> Any:
    if depth > 8:
        return ""
    if isinstance(value, dict):
        return {str(key)[:120]: _bounded_safe_value(item, depth=depth + 1) for key, item in list(value.items())[:200]}
    if isinstance(value, list):
        return [_bounded_safe_value(item, depth=depth + 1) for item in value[:500]]
    if isinstance(value, tuple):
        return [_bounded_safe_value(item, depth=depth + 1) for item in value[:500]]
    if isinstance(value, str):
        return value[:500]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return str(value)[:200]


def verify_current_production_recall_gate(
    runtime: Any,
    *,
    scope: dict[str, Any] | ScopeRef | None,
    release: ReleaseIdentity | None = None,
    limit: int = 200,
) -> dict[str, Any]:
    scope_ref = scope if isinstance(scope, ScopeRef) else ScopeRef.from_dict(scope)
    current = release or current_release_identity(runtime, scope_ref, limit=limit)
    if current is None or not current.complete:
        return {"ok": False, "status": "not_run", "reason": "release_identity_unavailable", "record_id": ""}
    record = runtime.store.latest_record_by_meta_value_exact_scope(
        kind="reflection",
        source="eimemory.evaluation.production_recall",
        status="active",
        scope=scope_ref,
        meta_key="report_type",
        meta_value="production_recall_gate",
    )
    if record is None:
        return {"ok": False, "status": "not_run", "reason": "current_release_production_recall_report_missing", "record_id": ""}
    report = record.content.get("report") if isinstance(record.content, dict) and isinstance(record.content.get("report"), dict) else {}
    if not _record_payload_digest_valid(record, report):
        return {"ok": False, "status": str(report.get("gate_status") or "blocked"), "reason": "production_recall_report_payload_tampered", "record_id": record.record_id}
    if str(report.get("attempt_id") or "") != record.record_id:
        return {"ok": False, "status": str(report.get("gate_status") or "blocked"), "reason": "production_recall_attempt_identity_invalid", "record_id": record.record_id}
    if _release_from_payload(report.get("release_identity")) != current:
        return {"ok": False, "status": "not_run", "reason": "latest_production_recall_report_release_mismatch", "record_id": record.record_id}
    if not _validate_persisted_real_query_report(report, expected_release=current):
        return {"ok": False, "status": str(report.get("gate_status") or "blocked"), "reason": "production_recall_report_contract_invalid", "record_id": record.record_id}
    receipt = runtime.store.get_by_id(current.receipt_id, scope=scope_ref)
    if receipt is None or verified_deployment_receipt_identity(receipt) != current:
        return {"ok": False, "status": "not_run", "reason": "current_deployment_receipt_invalid", "record_id": record.record_id}
    if str(record.time.created_at or "") < str(receipt.time.created_at or ""):
        return {"ok": False, "status": "not_run", "reason": "production_recall_report_predeploy", "record_id": record.record_id}
    baseline_identity = report.get("baseline_identity") if isinstance(report.get("baseline_identity"), dict) else {}
    baseline_id = str(
        baseline_identity.get("persisted_record_id")
        or baseline_identity.get("record_id")
        or baseline_identity.get("report_id")
        or ""
    )
    baseline, baseline_reason = _resolve_trusted_baseline(
        runtime,
        scope=scope_ref,
        report_id=baseline_id,
        dataset_digest=str(report.get("dataset_digest") or ""),
        current_release=current,
    )
    if baseline is None:
        return {"ok": False, "status": "not_run", "reason": baseline_reason, "record_id": record.record_id}
    expected_retrieval = _retrieval_identity(runtime, samples=[])
    if any(str(report.get(key) or "") != value for key, value in expected_retrieval.items()):
        return {"ok": False, "status": "blocked", "reason": "retrieval_identity_mismatch", "record_id": record.record_id}
    if not _independent_real_query_metrics_valid(report, baseline=baseline):
        return {"ok": False, "status": "blocked", "reason": "production_recall_metrics_invalid", "record_id": record.record_id}
    if report.get("accepted") is not True or report.get("gate_status") != "accepted":
        return {"ok": False, "status": str(report.get("gate_status") or "blocked"), "reason": str(report.get("blocked_reason") or "production_recall_gate_not_accepted"), "record_id": record.record_id}
    return {
        "ok": True,
        "status": "accepted",
        "reason": "",
        "record_id": record.record_id,
        "report_id": str(report.get("report_id") or ""),
        "schema": str(report.get("schema") or ""),
        "dataset_digest": str(report.get("dataset_digest") or ""),
        "baseline_report_id": baseline_id,
        "baseline_logical_report_id": str(baseline_identity.get("logical_report_id") or baseline_identity.get("report_id") or ""),
        "engine_digest": str(report.get("engine_digest") or ""),
        "fusion_digest": str(report.get("fusion_digest") or ""),
        "policy_digest": str(report.get("policy_digest") or ""),
        "result_digest": str(report.get("result_digest") or ""),
        "release_identity": dict(report.get("release_identity") or {}),
    }


def _independent_real_query_metrics_valid(report: dict[str, Any], *, baseline: dict[str, Any]) -> bool:
    samples = report.get("samples") if isinstance(report.get("samples"), list) else []
    if len(samples) < _REAL_QUERY_MIN_CASES:
        return False
    channels: set[str] = set()
    channel_counts = {channel: 0 for channel in PRODUCTION_REAL_QUERY_REQUIRED_CHANNELS}
    recalculated: list[dict[str, float]] = []
    result_refs: dict[str, list[str]] = {}
    cross_leakage = 0
    source_leakage = 0
    latencies: list[float] = []
    for sample in samples:
        if not isinstance(sample, dict):
            return False
        case_id = str(sample.get("case_id") or "")
        channel = str(sample.get("channel") or "")
        source_id = str(sample.get("source_id") or "")
        raw_refs = [str(item) for item in list(sample.get("returned_refs") or [])]
        refs = _stable_unique_refs(raw_refs, limit=5)
        label_refs = [str(item) for item in list(sample.get("label_refs") or [])]
        label_grades = list(sample.get("label_grades") or [])
        if (
            not case_id
            or channel not in SUPPORTED_RUNTIME_CHANNELS
            or not source_id
            or raw_refs != refs
            or len(label_refs) != len(label_grades)
            or not label_refs
            or len(set(label_refs)) != len(label_refs)
        ):
            return False
        labels = []
        for ref, grade in zip(label_refs, label_grades):
            if isinstance(grade, bool) or not isinstance(grade, int) or not 1 <= grade <= 3:
                return False
            labels.append({"record_ref": ref, "grade": grade})
        capacity = int(sample.get("corpus_result_capacity") or 0)
        ranking = evaluate_labeled_ranking_at_5(
            candidate_refs=refs,
            labels=labels,
            corpus_result_capacity=capacity,
            baseline_refs=list((baseline.get("result_refs") or {}).get(case_id, [])),
        )
        for key, expected in ranking.items():
            if abs(float(sample.get(key) or 0.0) - round(float(expected), 6)) > 1e-6:
                return False
        channels.add(channel)
        channel_counts[channel] += 1
        result_refs[case_id] = refs
        cross_leakage += int(sample.get("cross_channel_leakage_count") or 0)
        source_leakage += int(sample.get("source_filter_leakage_count") or 0)
        latencies.append(float(sample.get("latency_ms") or 0.0))
        recalculated.append(ranking)
    if not PRODUCTION_REAL_QUERY_REQUIRED_CHANNELS.issubset(channels):
        return False
    if any(count < _REAL_QUERY_MIN_CASES_PER_CHANNEL for count in channel_counts.values()):
        return False
    if result_refs != report.get("result_refs") or _stable_digest(result_refs) != str(report.get("result_digest") or ""):
        return False
    metric_fields = {
        "recall_at_5": "recall_at_5", "precision_at_5": "precision_at_5", "mrr": "mrr",
        "ndcg_at_5": "ndcg_at_5", "top1_stability": "top1_stable", "jaccard_at_5": "jaccard_at_5",
    }
    expected_metrics = {
        name: round(sum(float(item[field]) for item in recalculated) / len(recalculated), 6)
        for name, field in metric_fields.items()
    }
    expected_metrics["latency_ms_p50"] = percentile(latencies, 50)
    expected_metrics["latency_ms_p95"] = percentile(latencies, 95)
    expected_metrics["peak_memory_bytes"] = int((report.get("metrics") or {}).get("peak_memory_bytes") or 0)
    actual_metrics = report.get("metrics") if isinstance(report.get("metrics"), dict) else {}
    if any(abs(float(actual_metrics.get(key) or 0.0) - float(value)) > 1e-6 for key, value in expected_metrics.items()):
        return False
    gate = _real_query_threshold_gate(
        expected_metrics,
        baseline_metrics=dict(baseline.get("metrics") or {}),
        cross_channel_leakage=cross_leakage,
        source_filter_leakage=source_leakage,
        has_baseline=True,
    )
    return bool(
        cross_leakage == int(report.get("cross_channel_leakage_count") or 0) == 0
        and source_leakage == int(report.get("source_filter_leakage_count") or 0) == 0
        and gate == report.get("threshold_gate")
        and gate.get("ok") is True
    )


def _validate_persisted_real_query_report(report: dict[str, Any], *, expected_release: ReleaseIdentity) -> bool:
    gate = report.get("threshold_gate") if isinstance(report.get("threshold_gate"), dict) else {}
    thresholds = gate.get("thresholds") if isinstance(gate.get("thresholds"), dict) else {}
    results = report.get("result_refs") if isinstance(report.get("result_refs"), dict) else {}
    return bool(
        report.get("schema") == PRODUCTION_REAL_QUERY_REPORT_SCHEMA
        and report.get("report_type") == "production_recall_gate"
        and report.get("dataset_kind") == "production"
        and _release_from_payload(report.get("release_identity")) == expected_release
        and str(report.get("deployment_receipt_id") or "") == expected_release.receipt_id
        and all(len(str(report.get(key) or "")) == 64 for key in _DIGEST_KEYS)
        and str(report.get("result_digest") or "") == _stable_digest(results)
        and gate.get("schema") == PRODUCTION_REAL_QUERY_POLICY
        and thresholds == PRODUCTION_REAL_QUERY_THRESHOLDS
        and int(report.get("cross_channel_leakage_count") or 0) == 0
        and int(report.get("source_filter_leakage_count") or 0) == 0
    )


def _record_runtime_channel(record: RecordEnvelope) -> str:
    channel = runtime_channel_from_scope(record.scope)
    return channel or "openclaw"


def _stable_digest(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return sha256(payload.encode("utf-8")).hexdigest()
