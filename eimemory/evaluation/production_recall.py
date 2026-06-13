"""Production recall regression evaluator for eimemory.

This module intentionally keeps the runtime surface small and deterministic:

- load a dataset with one or more cases
- optionally seed a temporary in-memory runtime for isolated execution
- run recall for each case using the existing runtime API
- emit production-style metrics including latency and contamination rates
"""

from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
import tempfile
from time import perf_counter
from typing import Any

from eimemory.core.clock import now_iso
from eimemory.evaluation.metrics import mean_reciprocal_rank, percentile
from eimemory.metadata import business_metadata
from eimemory.models.records import RecordEnvelope, ScopeRef


RECALL_QUALITY_GATE_THRESHOLDS: dict[str, float] = {
    "hit_at_1": 0.70,
    "hit_at_5": 0.90,
    "false_recall_rate": 0.05,
    "forbidden_hit_rate": 0.05,
    "outcome_pollution_rate": 0.05,
    "reflection_pollution_rate": 0.05,
    "audit_pollution_rate": 0.05,
    "incident_pollution_rate": 0.05,
    "evolution_pollution_rate": 0.05,
    "stale_rule_pollution_rate": 0.05,
    "selected_record_pollution_rate": 0.05,
    "latency_ms_p95": 1500.0,
}

_MIN_GATE_METRICS = {"hit_at_1", "hit_at_5"}


def normalize_production_recall_dataset(dataset: dict | list) -> dict[str, Any]:
    if isinstance(dataset, list):
        raw = {"name": "production_recall", "cases": dataset}
    elif isinstance(dataset, dict):
        raw = dict(dataset)
    else:
        raise ValueError("Production recall dataset must be a JSON object or list")

    scope = asdict(ScopeRef.from_dict(raw.get("scope") or {}))
    seed = [dict(item) for item in list(raw.get("seed") or raw.get("seed_records") or []) if isinstance(item, dict)]
    cases = [dict(item) for item in list(raw.get("cases") or raw.get("samples") or []) if isinstance(item, dict)]

    return {
        "schema_version": 1,
        "name": str(raw.get("name") or raw.get("dataset_name") or "production_recall"),
        "scope": scope,
        "seed": seed,
        "cases": cases,
    }


def run_production_recall_eval(
    runtime,
    dataset: dict | list,
    *,
    seed: bool = True,
    scope: dict | None = None,
    persist_report: bool = False,
) -> dict[str, Any]:
    normalized = normalize_production_recall_dataset(dataset)
    dataset_scope = ScopeRef.from_dict({**dict(normalized["scope"]), **(scope or {})})
    seed_records = list(normalized["seed"])

    if seed and seed_records:
        with tempfile.TemporaryDirectory(prefix="eimemory-production-recall-") as temp_root:
            from eimemory.api.runtime import Runtime

            eval_runtime = Runtime.create(root=Path(temp_root))
            try:
                report = _run_production_recall_eval_on_runtime(
                    eval_runtime,
                    normalized=normalized,
                    dataset_scope=dataset_scope,
                    seed_records=seed_records,
                )
            finally:
                eval_runtime.close()
    else:
        report = _run_production_recall_eval_on_runtime(
            runtime,
            normalized=normalized,
            dataset_scope=dataset_scope,
            seed_records=seed_records if seed else [],
        )
    if persist_report:
        persisted = _persist_recall_quality_report(runtime, report, scope=dataset_scope)
        report = {
            **report,
            "persisted": bool(persisted),
            "persisted_record_id": persisted.record_id if persisted else "",
        }
    else:
        report = {**report, "persisted": False, "persisted_record_id": ""}
    return report


def _run_production_recall_eval_on_runtime(
    runtime,
    *,
    normalized: dict[str, Any],
    dataset_scope: ScopeRef,
    seed_records: list[dict[str, Any]],
) -> dict[str, Any]:
    seeded_records, seed_lookup, seed_errors = _seed_records(runtime, seed_records, default_scope=dataset_scope)

    sample_reports: list[dict[str, Any]] = []
    hit_at_1_scores: list[float] = []
    hit_at_k_scores: list[float] = []
    hit_at_5_scores: list[float] = []
    false_recall_count = 0
    forbidden_hit_count = 0
    reciprocal_ranks: list[int] = []
    latencies_ms: list[float] = []
    outcome_polluted_count = 0
    reflection_polluted_count = 0
    audit_polluted_count = 0
    incident_polluted_count = 0
    evolution_polluted_count = 0
    stale_rule_polluted_count = 0
    selected_record_polluted_count = 0
    policy_hit_scores: list[float] = []
    injection_withheld_rates: list[float] = []
    empty_count = 0

    for index, case in enumerate(normalized["cases"]):
        if not isinstance(case, dict):
            sample = _invalid_case(index, dataset_scope, "invalid_case")
        else:
            sample = _run_case(
                runtime=runtime,
                case=case,
                index=index,
                default_scope=dataset_scope,
                seed_lookup=seed_lookup,
            )
        sample_reports.append(sample)

        hit_at_1_scores.append(float(sample.get("hit_at_1") or 0.0))
        hit_at_k_scores.append(float(sample.get("hit_at_k") or 0.0))
        hit_at_5_scores.append(float(sample.get("hit_at_5") or 0.0))
        reciprocal_ranks.append(int(sample.get("rank") or 0))
        latencies_ms.append(float(sample.get("latency_ms") or 0.0))
        if sample.get("false_recall"):
            false_recall_count += 1
        if sample.get("forbid_hit"):
            forbidden_hit_count += 1
        if sample.get("outcome_polluted"):
            outcome_polluted_count += 1
        if sample.get("reflection_polluted"):
            reflection_polluted_count += 1
        if sample.get("audit_polluted"):
            audit_polluted_count += 1
        if sample.get("incident_polluted"):
            incident_polluted_count += 1
        if sample.get("evolution_polluted"):
            evolution_polluted_count += 1
        if sample.get("stale_rule_polluted"):
            stale_rule_polluted_count += 1
        if sample.get("selected_record_polluted"):
            selected_record_polluted_count += 1
        policy_hit_scores.append(float(sample.get("policy_hit") or 0.0))
        injection_withheld_rates.append(float(sample.get("injection_withheld_rate") or 0.0))
        if sample.get("empty"):
            empty_count += 1

    sample_count = len(sample_reports)
    pass_count = sum(1 for sample in sample_reports if bool(sample.get("passed")))
    report = {
        "ok": True,
        "schema_version": 2,
        "report_type": "recall_quality_report",
        "legacy_report_type": "production_recall_eval",
        "name": str(normalized["name"]),
        "generated_at": now_iso(),
        "scope": asdict(dataset_scope),
        "seeded": len(seed_records) > 0,
        "seeded_record_ids": [record.record_id for _, record in seeded_records],
        "seed_lookup": seed_lookup,
        "seed_error_count": len(seed_errors),
        "errors": seed_errors,
        "sample_count": sample_count,
        "pass_count": pass_count,
        "fail_count": max(0, sample_count - pass_count),
        "pass_rate": round(pass_count / sample_count, 3) if sample_count else 0.0,
        "hit_at_1": round(sum(hit_at_1_scores) / sample_count, 3) if sample_count else 0.0,
        "hit_at_k": round(sum(hit_at_k_scores) / sample_count, 3) if sample_count else 0.0,
        "hit_at_5": round(sum(hit_at_5_scores) / sample_count, 3) if sample_count else 0.0,
        "mrr": mean_reciprocal_rank([int(rank) for rank in reciprocal_ranks]) if sample_count else 0.0,
        "latency_ms_avg": round(sum(latencies_ms) / sample_count, 3) if sample_count else 0.0,
        "latency_ms_p95": percentile(latencies_ms, 95),
        "false_recall_rate": round(false_recall_count / sample_count, 3) if sample_count else 0.0,
        "forbidden_hit_rate": round(forbidden_hit_count / sample_count, 3) if sample_count else 0.0,
        "outcome_pollution_rate": round(outcome_polluted_count / sample_count, 3) if sample_count else 0.0,
        "reflection_pollution_rate": round(reflection_polluted_count / sample_count, 3) if sample_count else 0.0,
        "audit_pollution_rate": round(audit_polluted_count / sample_count, 3) if sample_count else 0.0,
        "incident_pollution_rate": round(incident_polluted_count / sample_count, 3) if sample_count else 0.0,
        "evolution_pollution_rate": round(evolution_polluted_count / sample_count, 3) if sample_count else 0.0,
        "stale_rule_pollution_rate": round(stale_rule_polluted_count / sample_count, 3) if sample_count else 0.0,
        "selected_record_pollution_rate": round(selected_record_polluted_count / sample_count, 3) if sample_count else 0.0,
        "policy_hit_rate": round(sum(policy_hit_scores) / sample_count, 3) if sample_count else 0.0,
        "injection_withheld_rate": round(sum(injection_withheld_rates) / sample_count, 3) if sample_count else 0.0,
        "empty_rate": round(empty_count / sample_count, 3) if sample_count else 0.0,
        "samples": sample_reports,
    }
    quality_gate = evaluate_production_recall_quality_gate(report)
    return {
        **report,
        "quality_gate": quality_gate,
        "passed_threshold": bool(quality_gate.get("ok")),
        "gate_ok": bool(quality_gate.get("ok")),
        "blocked_reason": "" if quality_gate.get("ok") else str(quality_gate.get("blocked_reason") or ""),
    }


def evaluate_production_recall_quality_gate(
    report: dict[str, Any],
    *,
    thresholds: dict[str, float] | None = None,
) -> dict[str, Any]:
    limits = dict(RECALL_QUALITY_GATE_THRESHOLDS)
    if thresholds:
        limits.update({str(key): float(value) for key, value in thresholds.items()})

    blocking: dict[str, dict[str, Any]] = {}
    sample_count = int(report.get("sample_count") or 0)
    if sample_count <= 0:
        blocking["sample_count"] = {"actual": sample_count, "threshold": 1, "operator": ">="}

    for metric, threshold in limits.items():
        actual = float(report.get(metric) or 0.0)
        if metric in _MIN_GATE_METRICS:
            if actual < threshold:
                blocking[metric] = {"actual": actual, "threshold": threshold, "operator": ">="}
        elif actual > threshold:
            blocking[metric] = {"actual": actual, "threshold": threshold, "operator": "<="}

    ok = not blocking
    return {
        "ok": ok,
        "policy": "production_recall_pollution_gate",
        "blocked_reason": "" if ok else "recall_quality_gate_failed",
        "thresholds": limits,
        "blocking_metrics": blocking,
    }


def _run_case(
    *,
    runtime,
    case: dict[str, Any],
    index: int,
    default_scope: ScopeRef,
    seed_lookup: dict[str, str],
) -> dict[str, Any]:
    case_id = str(case.get("case_id") or case.get("id") or index)
    query = str(case.get("query") or "")
    if not query:
        return _invalid_case(index, default_scope, "empty_query")

    case_scope = ScopeRef.from_dict(case.get("scope") or asdict(default_scope))
    topk = _positive_int(case.get("topk"), default=5)
    task_context = dict(case.get("task_context") or {})

    start = perf_counter()
    bundle = runtime.memory.recall(query=query, scope=asdict(case_scope), task_context=task_context, limit=topk)
    latency_ms = (perf_counter() - start) * 1000.0

    returned = list(bundle.items)
    returned_record_ids = [item.record_id for item in returned]
    returned_titles = [item.title for item in returned]
    returned_kinds = [item.kind for item in returned]
    returned_sources = [str(item.source or "") for item in returned]
    returned_recall_lanes = [_record_recall_lane(item) for item in returned]
    returned_texts = _record_texts(returned)

    expected_record_ids = _expected_record_ids(case, seed_lookup=seed_lookup)
    expected_titles = _normalize_set(case.get("expected_titles") or case.get("expect_any_title"), lower=False)
    expected_text = _normalize_set(case.get("expected_text") or case.get("expect_any_text"), lower=True)
    forbid_kinds = _normalize_set(case.get("forbid_kinds") or case.get("forbid_any_kind"), lower=True)
    forbid_title_contains = _normalize_list(case.get("forbid_title_contains") or case.get("forbid_any_title"))
    forbid_source_contains = _normalize_list(case.get("forbid_source_contains") or case.get("forbid_any_source"))

    matched_rank = _first_matching_rank(
        returned,
        expected_record_ids=expected_record_ids,
        expected_titles=expected_titles,
        expected_text=expected_text,
    )
    has_expectation = bool(expected_record_ids or expected_titles or expected_text)
    if has_expectation:
        hit_at_1 = 1.0 if matched_rank == 1 else 0.0
        hit_at_k = 1.0 if matched_rank and matched_rank <= topk else 0.0
        hit_at_5 = 1.0 if matched_rank and matched_rank <= 5 else 0.0
        reciprocal_rank = round(1.0 / matched_rank, 3) if matched_rank else 0.0
    else:
        matched_rank = 1 if returned else 0
        hit_at_1 = 1.0 if matched_rank else 0.0
        hit_at_k = hit_at_1
        hit_at_5 = hit_at_1
        reciprocal_rank = 1.0 if returned else 0.0

    selected_records = [
        dict(item)
        for item in list((bundle.explanation.get("selected_records") or []))
        if isinstance(item, dict)
    ]
    policy_suggestions = [
        dict(item)
        for item in list((bundle.explanation.get("policy_suggestions") or []))
        if isinstance(item, dict)
    ]
    injection_plan = dict(bundle.explanation.get("injection_plan") or {})
    injection_lane_composition = dict(injection_plan.get("lane_composition") or {})
    injection_total = sum(int(value or 0) for value in injection_lane_composition.values())
    injection_withheld_rate = (
        round(int(injection_lane_composition.get("withheld") or 0) / injection_total, 3)
        if injection_total
        else 0.0
    )
    expected_policy_ids = _normalize_set(case.get("expected_policy_ids") or case.get("expect_any_policy_id"), lower=False)
    policy_hit = _policy_hit(policy_suggestions, expected_policy_ids=expected_policy_ids)
    reflection_returned = any(_is_reflection_record(item) for item in returned)
    reflection_allowed = _allows_reflection_results(
        query=query,
        task_context=task_context,
        expected_record_ids=expected_record_ids,
        returned=returned,
    )
    outcome_polluted = any(_is_outcome_pollution(item) for item in returned) and not reflection_allowed
    reflection_polluted = reflection_returned and not reflection_allowed
    audit_polluted = any(_record_recall_lane(item) == "audit_record" for item in returned) and not reflection_allowed
    incident_polluted = any(_record_recall_lane(item) == "incident_report" for item in returned) and not reflection_allowed
    evolution_polluted = any(_record_recall_lane(item) == "evolution_artifact" for item in returned) and not reflection_allowed
    stale_rule_polluted = any(_is_stale_rule_record(item) for item in returned)
    selected_record_polluted = any(_selected_record_is_polluted(item) for item in selected_records) and not reflection_allowed
    forbidden_by_case = any(
        _record_forbidden(
            record=item,
            forbid_kinds=forbid_kinds,
            forbid_title_contains=forbid_title_contains,
            forbid_source_contains=forbid_source_contains,
        )
        for item in returned
    )
    false_recall = bool(has_expectation and returned and not matched_rank)
    passed = (
        bool(matched_rank) if has_expectation else bool(returned)
    ) and not any(
        [
            forbidden_by_case,
            outcome_polluted,
            reflection_polluted,
            audit_polluted,
            incident_polluted,
            evolution_polluted,
            stale_rule_polluted,
            selected_record_polluted,
        ]
    )

    return {
        "index": index,
        "case_id": case_id,
        "query": query,
        "scope": asdict(case_scope),
        "topk": topk,
        "task_context": task_context,
        "expected_record_ids": sorted(expected_record_ids),
        "expected_titles": sorted(expected_titles),
        "expected_text": sorted(expected_text),
        "forbid_kinds": sorted(forbid_kinds),
        "forbid_title_contains": [item.lower() for item in forbid_title_contains],
        "forbid_source_contains": [item.lower() for item in forbid_source_contains],
        "latency_ms": round(latency_ms, 3),
        "returned_record_ids": returned_record_ids,
        "returned_titles": returned_titles,
        "returned_kinds": returned_kinds,
        "returned_sources": returned_sources,
        "returned_recall_lanes": returned_recall_lanes,
        "returned_texts": returned_texts,
        "returned_count": len(returned),
        "rank": matched_rank,
        "hit_at_1": hit_at_1,
        "hit_at_k": hit_at_k,
        "hit_at_5": hit_at_5,
        "reciprocal_rank": reciprocal_rank,
        "matched_expected": bool(matched_rank) if has_expectation else bool(returned),
        "empty": not bool(returned),
        "false_recall": bool(false_recall),
        "forbid_hit": bool(forbidden_by_case),
        "outcome_polluted": bool(outcome_polluted),
        "reflection_returned": bool(reflection_returned),
        "reflection_allowed": bool(reflection_allowed),
        "reflection_polluted": bool(reflection_polluted),
        "audit_polluted": bool(audit_polluted),
        "incident_polluted": bool(incident_polluted),
        "evolution_polluted": bool(evolution_polluted),
        "stale_rule_polluted": bool(stale_rule_polluted),
        "selected_record_polluted": bool(selected_record_polluted),
        "policy_hit": policy_hit,
        "policy_suggestion_ids": [str(item.get("id") or item.get("pattern_id") or "") for item in policy_suggestions],
        "injection_withheld_rate": injection_withheld_rate,
        "passed": bool(passed),
        "explanation": {
            "recall_profile": str(bundle.explanation.get("recall_profile") or ""),
            "retrieval_mode": str(bundle.explanation.get("retrieval_mode") or ""),
            "vector_hits": int(bundle.explanation.get("vector_hits") or 0),
            "recall_filters": dict(bundle.explanation.get("recall_filters") or {}),
            "selected_records": selected_records,
            "injection_plan": injection_plan,
        },
    }


def _seed_records(
    runtime,
    seed_records: list[dict[str, Any]],
    *,
    default_scope: ScopeRef,
) -> tuple[list[tuple[str, RecordEnvelope]], dict[str, str], list[dict[str, Any]]]:
    seeded_records: list[tuple[str, RecordEnvelope]] = []
    seed_lookup: dict[str, str] = {}
    errors: list[dict[str, Any]] = []

    for index, item in enumerate(seed_records):
        if not isinstance(item, dict):
            errors.append({"phase": "seed", "index": index, "error": "invalid_seed_record"})
            continue
        seed_id = str(item.get("id") or item.get("seed_id") or str(index))
        kind = str(item.get("kind") or "memory").strip() or "memory"
        title = str(item.get("title") or f"Production recall seed {index + 1}")
        text = str(item.get("text") or item.get("summary") or item.get("detail") or "")
        source = str(item.get("source") or "eimemory.production_recall.seed")
        scope = ScopeRef.from_dict(item.get("scope") or asdict(default_scope))
        meta = dict(item.get("meta") or {})
        content = dict(item.get("content") or {})
        tags = [str(tag) for tag in list(item.get("tags") or [])]
        links = []

        if kind != "memory":
            content = dict(content)
            if text and "text" not in content:
                content["text"] = text
            summary = str(item.get("summary") or text or title)
            detail = str(item.get("detail") or text)
            try:
                record = RecordEnvelope.create(
                    kind=kind,
                    title=title,
                    summary=summary,
                    detail=detail,
                    content=content,
                    tags=tags,
                    links=links,
                    source=source,
                    scope=scope,
                    evidence=list(item.get("evidence") or []),
                    provenance=dict(item.get("provenance") or {}),
                    meta=meta,
                    status=str(item.get("status") or "active"),
                )
                runtime.store.append(record)
            except Exception as exc:  # pragma: no cover - defensive eval boundary
                errors.append(
                    {
                        "phase": "seed",
                        "index": index,
                        "seed_id": seed_id,
                        "error": exc.__class__.__name__,
                        "detail": str(exc),
                    }
                )
                continue
        else:
            memory_type = str(item.get("memory_type") or item.get("type") or "fact")
            try:
                record = runtime.memory.ingest(
                    text=text,
                    memory_type=memory_type,
                    title=title,
                    scope=asdict(scope),
                    source=source,
                    tags=tags,
                    force_capture=bool(item.get("force_capture", True)),
                    meta=meta,
                    content=content,
                    evidence=list(item.get("evidence") or []),
                )
            except Exception as exc:  # pragma: no cover - defensive eval boundary
                errors.append(
                    {
                        "phase": "seed",
                        "index": index,
                        "seed_id": seed_id,
                        "kind": kind,
                        "error": exc.__class__.__name__,
                        "detail": str(exc),
                    }
                )
                continue

        if record.status != "active":
            continue
        if seed_id not in seed_lookup:
            seed_lookup[seed_id] = record.record_id
        seeded_records.append((seed_id, record))

    return seeded_records, seed_lookup, errors


def _first_matching_rank(
    records: list[RecordEnvelope],
    *,
    expected_record_ids: set[str],
    expected_titles: set[str],
    expected_text: set[str],
) -> int:
    for index, record in enumerate(records, start=1):
        if record.record_id in expected_record_ids:
            return index
        if record.title in expected_titles:
            return index
        text = " ".join(
            [record.title, record.summary, record.detail, str(record.content.get("text") or ""), str(record.content.get("summary") or "")]
        ).lower()
        if any(term in text for term in expected_text):
            return index
    return 0


def _record_texts(records: list[RecordEnvelope]) -> list[str]:
    texts: list[str] = []
    for item in records:
        texts.append(item.summary)
        texts.append(item.detail)
        text = str(item.content.get("text") or "")
        if text:
            texts.append(text)
        summary = str(item.content.get("summary") or "")
        if summary:
            texts.append(summary)
    return texts


def _record_forbidden(
    *,
    record: RecordEnvelope,
    forbid_kinds: set[str],
    forbid_title_contains: list[str],
    forbid_source_contains: list[str],
) -> bool:
    if str(record.kind).lower() in forbid_kinds:
        return True
    title = str(record.title or "").lower()
    source = str(record.source or "").lower()
    if any(term in title for term in forbid_title_contains):
        return True
    if any(term in source for term in forbid_source_contains):
        return True
    return False


def _policy_hit(policy_suggestions: list[dict[str, Any]], *, expected_policy_ids: set[str]) -> float:
    if not expected_policy_ids:
        return 1.0 if policy_suggestions else 0.0
    suggestion_ids = {
        str(item.get("id") or item.get("pattern_id") or item.get("record_id") or "")
        for item in policy_suggestions
    }
    return 1.0 if suggestion_ids & expected_policy_ids else 0.0


def _selected_record_is_polluted(item: dict[str, Any]) -> bool:
    lane = str(item.get("recall_lane") or item.get("lane") or "").strip().lower()
    kind = str(item.get("kind") or "").strip().lower()
    if lane in {"audit_record", "incident_report", "evolution_artifact", "run_log", "operational"}:
        return True
    return kind in {"reflection", "incident", "replay_result", "recall_view", "learning_eval"}


def _record_recall_lane(record: RecordEnvelope) -> str:
    meta = business_metadata(record.meta)
    content = record.content if isinstance(record.content, dict) else {}
    memory_type = str(meta.get("memory_type") or content.get("memory_type") or "").strip().lower()
    aliases = {
        "audit": "audit_record",
        "audit_record": "audit_record",
        "diagnostic": "audit_record",
        "incident": "incident_report",
        "incident_report": "incident_report",
        "log": "run_log",
        "run_log": "run_log",
        "runtime_log": "run_log",
        "evolution": "evolution_artifact",
        "evolution_artifact": "evolution_artifact",
        "preference": "user_preference",
        "user_preference": "user_preference",
        "rule": "system_rule",
        "system_rule": "system_rule",
        "fact": "durable_fact",
        "durable_fact": "durable_fact",
        "knowledge": "external_knowledge",
        "external_knowledge": "external_knowledge",
        "conversation": "task_context",
        "context": "task_context",
        "task_context": "task_context",
    }
    if memory_type in aliases:
        return aliases[memory_type]
    if record.kind == "rule":
        return "system_rule"
    if record.kind == "reflection":
        return _reflection_recall_lane(record)
    if record.kind in {"recall_view", "feedback"}:
        return "audit_record"
    if record.kind == "incident":
        return "incident_report"
    if record.kind in {"replay_result", "learning_eval", "capability_candidate", "promotion_request", "skill_candidate"}:
        return "evolution_artifact"
    if record.kind in {"knowledge_page", "claim_card", "paper_source", "paper_extract", "knowledge_unit"}:
        return "external_knowledge"
    if record.kind == "memory":
        return "durable_fact"
    return str(record.kind or "")


def _reflection_recall_lane(record: RecordEnvelope) -> str:
    meta = business_metadata(record.meta)
    content = record.content if isinstance(record.content, dict) else {}
    report_type = str(meta.get("report_type") or record.provenance.get("report_type") or content.get("report_type") or "").strip().lower()
    haystack = " ".join([report_type, str(record.source or ""), str(record.title or "")]).lower()
    if any(marker in haystack for marker in ("audit", "before_prompt_build", "injection")):
        return "audit_record"
    if "incident" in haystack:
        return "incident_report"
    if "outcome_trace" in haystack or "run_log" in haystack:
        return "run_log"
    if report_type:
        return "evolution_artifact"
    return "audit_record"


def _is_stale_rule_record(record: RecordEnvelope) -> bool:
    if record.kind != "rule":
        return False
    if record.status not in {"active", "accepted"}:
        return True
    meta = business_metadata(record.meta)
    watch = meta.get("post_promotion_watch") if isinstance(meta, dict) else {}
    if isinstance(watch, dict) and str(watch.get("status") or "") in {"rolled_back", "quarantined", "rejected"}:
        return True
    return False


def _is_outcome_pollution(record: RecordEnvelope) -> bool:
    source = str(record.source or "").lower()
    title = str(record.title or "").lower()
    title_text = " ".join(
        [record.title, record.summary, record.detail, str(record.content.get("text") or ""), str(record.content.get("summary") or "")]
    ).lower()
    return (
        source == "openclaw.agent_end"
        or "openclaw.agent_end" in source
        or title == "openclaw agent outcome"
        or "openclaw agent outcome" in title_text
        or source == "agent_outcome"
        or "agent outcome" in title_text
        or "outcome" in source
    )


def _is_reflection_record(record: RecordEnvelope) -> bool:
    return str(record.kind) == "reflection"


def _allows_reflection_results(
    *,
    query: str,
    task_context: dict[str, Any],
    expected_record_ids: set[str],
    returned: list[RecordEnvelope],
) -> bool:
    if _is_report_query(query, task_context):
        return True
    if expected_record_ids and any(item.kind == "reflection" and item.record_id in expected_record_ids for item in returned):
        return True
    return False


def _is_report_query(query: str, task_context: dict[str, Any]) -> bool:
    haystack = f"{query} " + " ".join(
        str(task_context.get(key) or "")
        for key in ("intent", "goal", "task_type", "report_type", "recall_view")
    )
    lowered = haystack.lower()
    return any(
        marker in lowered
        for marker in (
            "report",
            "governance",
            "reflection",
            "rule evolution",
            "rule_evolution",
            "evolution",
            "复盘",
            "反思",
            "报告",
            "治理",
        )
    )


def _persist_recall_quality_report(runtime: Any, report: dict[str, Any], *, scope: ScopeRef) -> RecordEnvelope | None:
    try:
        record = _recall_quality_report_record(report, scope=scope)
        return runtime.store.append(record)
    except Exception:  # pragma: no cover - report persistence must not break eval
        return None


def _recall_quality_report_record(report: dict[str, Any], *, scope: ScopeRef) -> RecordEnvelope:
    report_payload = {
        key: value
        for key, value in dict(report).items()
        if key not in {"persisted", "persisted_record_id"}
    }
    name = str(report.get("name") or "production_recall")
    summary = (
        f"Recall quality {name}: "
        f"hit@1={float(report.get('hit_at_1') or 0.0):.3f}, "
        f"hit@5={float(report.get('hit_at_5') or 0.0):.3f}, "
        f"p95={float(report.get('latency_ms_p95') or 0.0):.1f}ms, "
        f"gate={'ok' if (report.get('quality_gate') or {}).get('ok') else 'blocked'}"
    )
    return RecordEnvelope.create(
        kind="reflection",
        title=f"Recall quality report: {name}",
        summary=summary,
        detail=summary,
        content={"report": report_payload},
        tags=["evaluation", "recall_quality_report", "production_recall"],
        source="eimemory.evaluation.production_recall",
        scope=scope,
        meta={
            "report_type": "recall_quality_report",
            "legacy_report_type": "production_recall_eval",
            "sample_count": int(report.get("sample_count") or 0),
            "hit_at_1": float(report.get("hit_at_1") or 0.0),
            "hit_at_5": float(report.get("hit_at_5") or 0.0),
            "latency_ms_p95": float(report.get("latency_ms_p95") or 0.0),
            "quality_gate_ok": bool((report.get("quality_gate") or {}).get("ok")),
        },
        provenance={
            "report_type": "recall_quality_report",
            "legacy_report_type": "production_recall_eval",
        },
    )


def _expected_record_ids(case: dict[str, Any], *, seed_lookup: dict[str, str]) -> set[str]:
    expected_record_ids = {
        _map_seed_reference(value, seed_lookup=seed_lookup)
        for value in _normalize_list(case.get("expected_record_ids") or case.get("expect_any_record_id"))
    }
    return {value for value in expected_record_ids if value}


def _map_seed_reference(value: str, *, seed_lookup: dict[str, str]) -> str:
    if not value:
        return ""
    return str(seed_lookup.get(value, value))


def _normalize_set(value: Any, *, lower: bool) -> set[str]:
    return {str(item).strip().lower() if lower else str(item).strip() for item in _normalize_list(value)}


def _normalize_list(value: Any) -> list[str]:
    return [str(item).strip() for item in list(value or []) if str(item).strip()]


def _invalid_case(index: int, scope: ScopeRef, error: str) -> dict[str, Any]:
    return {
        "index": index,
        "case_id": str(index),
        "query": "",
        "scope": asdict(scope),
        "topk": 0,
        "task_context": {},
        "expected_record_ids": [],
        "expected_titles": [],
        "expected_text": [],
        "forbid_kinds": [],
        "forbid_title_contains": [],
        "forbid_source_contains": [],
        "latency_ms": 0.0,
        "returned_record_ids": [],
        "returned_titles": [],
        "returned_kinds": [],
        "returned_sources": [],
        "returned_texts": [],
        "returned_count": 0,
        "rank": 0,
        "hit_at_1": 0.0,
        "hit_at_k": 0.0,
        "reciprocal_rank": 0.0,
        "matched_expected": False,
        "empty": True,
        "forbid_hit": False,
        "outcome_polluted": False,
        "reflection_returned": False,
        "reflection_allowed": False,
        "reflection_polluted": False,
        "error": error,
        "explanation": {},
    }


def _positive_int(value: object, *, default: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return max(1, min(1000, parsed))
