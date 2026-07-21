from __future__ import annotations

import time
import math
from collections import defaultdict
from dataclasses import asdict
from typing import Any

from eimemory.api.memory import MemoryAPI
from eimemory.evaluation.contracts import SUPPORTED_PHASES, normalize_memory_eval_suite
from eimemory.evaluation.metrics import (
    binary_pass_rate,
    mean_reciprocal_rank,
    percentile,
)
from eimemory.models.records import ScopeRef


def run_memory_eval_ci(
    runtime: Any,
    dataset: dict | list,
    *,
    emit_incidents: bool = False,
) -> dict[str, Any]:
    suite = normalize_memory_eval_suite(dataset)
    if emit_incidents:
        suite["emit_incidents"] = True

    scope_ref = ScopeRef.from_dict(suite["scope"])
    seeded_record_ids = _seed_records(runtime, suite)
    memory_api = MemoryAPI(runtime.store)

    samples: list[dict[str, Any]] = []
    incident_record_ids: list[str] = []
    for index, case in enumerate(suite["cases"]):
        started = time.perf_counter()
        sample = _run_case(runtime, memory_api, case, index=index, default_scope=scope_ref)
        sample["latency_ms"] = round((time.perf_counter() - started) * 1000, 3)
        samples.append(sample)
        if not sample["passed"] and suite["emit_incidents"]:
            incident = _emit_eval_incident(runtime, sample, suite)
            incident_record_ids.append(incident.record_id)

    pass_count = sum(1 for sample in samples if sample["passed"])
    fail_count = len(samples) - pass_count
    pass_rate = binary_pass_rate([bool(sample["passed"]) for sample in samples])
    threshold = float(suite["threshold"])
    latencies = [float(sample["latency_ms"]) for sample in samples]

    return {
        "ok": True,
        "schema_version": 2,
        "report_type": "memory_eval_ci",
        "name": str(suite["name"]),
        "scope": asdict(scope_ref),
        "seeded_record_ids": seeded_record_ids,
        "sample_count": len(samples),
        "pass_count": pass_count,
        "fail_count": fail_count,
        "pass_rate": pass_rate,
        "threshold": round(threshold, 3),
        "passed_threshold": pass_rate >= threshold,
        "phase_scores": _phase_scores(samples),
        "efficiency": {
            "latency_ms_avg": round(sum(latencies) / len(latencies), 3) if latencies else 0.0,
            "latency_ms_p95": percentile(latencies, 95),
            "case_count": len(samples),
        },
        "failures": [sample for sample in samples if not sample["passed"]],
        "incident_record_ids": incident_record_ids,
        "samples": samples,
    }


def _seed_records(runtime: Any, suite: dict[str, Any]) -> list[str]:
    scope_ref = ScopeRef.from_dict(suite["scope"])
    seeded_record_ids: list[str] = []
    for index, item in enumerate(suite.get("seed") or []):
        if not isinstance(item, dict):
            continue
        text = str(item.get("text") or item.get("summary") or "")
        title = str(item.get("title") or f"Memory CI seed {index + 1}")
        memory_type = str(item.get("memory_type") or item.get("type") or "fact")
        record = runtime.memory.ingest(
            text=text,
            memory_type=memory_type,
            title=title,
            scope=asdict(scope_ref),
            source=str(item.get("source") or "eimemory.eval.seed"),
            source_id=item["source_id"] if "source_id" in item else "default",
            tags=[str(tag) for tag in (item.get("tags") or [])],
            force_capture=bool(item.get("force_capture", True)),
            meta=dict(item.get("meta") or {}),
            content=dict(item.get("content") or {}),
            evidence=list(item.get("evidence") or []),
        )
        if record.status == "active":
            seeded_record_ids.append(record.record_id)
    return seeded_record_ids


def _run_case(
    runtime: Any,
    memory_api: MemoryAPI,
    case: Any,
    *,
    index: int,
    default_scope: ScopeRef,
) -> dict[str, Any]:
    if not isinstance(case, dict):
        return _invalid_case(index, default_scope, "invalid_case")

    phase = str(case.get("phase") or "usage").strip().lower()
    if phase not in SUPPORTED_PHASES:
        phase = "usage"

    if phase == "extraction":
        return _run_extraction_case(runtime, case, index=index, phase=phase, default_scope=default_scope)
    if phase == "update":
        return _run_update_case(memory_api, case, index=index, phase=phase, default_scope=default_scope)
    return _run_recall_case(memory_api, case, index=index, phase=phase, default_scope=default_scope)


def _run_extraction_case(
    runtime: Any,
    case: dict[str, Any],
    *,
    index: int,
    phase: str,
    default_scope: ScopeRef,
) -> dict[str, Any]:
    case_scope = ScopeRef.from_dict(case.get("scope") or asdict(default_scope))
    input_text = str(case.get("input_text") or "")
    if not input_text:
        return _invalid_case(index, case_scope, "missing_input_text", case=case)

    expect_memory_type = str(case.get("expect_memory_type") or case.get("memory_type") or "fact")
    expected_titles = _normalize_terms(case.get("expect_any_title"))
    expected_record_ids = _normalize_terms(case.get("expect_any_record_id"))
    expected_kinds = _normalize_terms(case.get("expect_any_kind"))
    expected_text = _normalize_terms(case.get("expect_any_text"))
    forbid_terms = _normalize_terms(case.get("forbid_any_text"))

    record = runtime.memory.ingest(
        text=input_text,
        memory_type=expect_memory_type,
        title=str(case.get("title") or case.get("case_id") or f"memory-eval-{index}"),
        scope=asdict(case_scope),
        source="eimemory.eval.ci",
        force_capture=bool(case.get("force_capture", True)),
    )

    returned_record_ids = [record.record_id]
    returned_titles = [record.title]
    returned_texts = [
        str(record.summary),
        str(record.detail),
        str(record.content.get("text") or ""),
        str(record.content.get("summary") or ""),
    ]

    expected_ok = True
    if expected_titles and record.title not in expected_titles:
        expected_ok = False
    if expected_record_ids and record.record_id not in expected_record_ids:
        expected_ok = False
    if expected_kinds and str(record.kind) not in expected_kinds:
        expected_ok = False
    if expected_text and not _text_contains_any(values=returned_texts, terms=expected_text):
        expected_ok = False
    if str(record.meta.get("memory_type") or record.content.get("memory_type") or "") != expect_memory_type:
        expected_ok = False

    hallucinated = bool(_text_contains_any(values=returned_texts, terms=forbid_terms))
    passed = record.status == "active" and expected_ok and not hallucinated

    sample: dict[str, Any] = {
        "index": index,
        "case_id": str(case.get("case_id") or case.get("id") or index),
        "phase": phase,
        "input_text": input_text,
        "scope": asdict(case_scope),
        "query": "",
        "task_context": {},
        "limit": int(case.get("limit") or 5),
        "expected_memory_type": expect_memory_type,
        "expected_titles": expected_titles,
        "expected_record_ids": expected_record_ids,
        "expected_kinds": expected_kinds,
        "expected_text": expected_text,
        "expected_current_text": _normalize_terms(case.get("expect_current_text")),
        "forbid_any_text": forbid_terms,
        "expected": {
            "memory_type": [expect_memory_type],
            "titles": expected_titles,
            "record_ids": expected_record_ids,
            "kinds": expected_kinds,
            "texts": expected_text,
        },
        "returned_record_ids": returned_record_ids,
        "returned_titles": returned_titles,
        "returned_texts": returned_texts,
        "returned_record_count": 1,
        "expected_rank": 1 if expected_ok else 0,
        "recall_at_k": 1.0 if expected_ok else 0.0,
        "precision_at_k": 1.0 if expected_ok else 0.0,
        "mrr": 1.0 if expected_ok else 0.0,
        "ndcg_at_k": 1.0 if expected_ok and expected_text else 0.0,
        "repair_hint": str(case.get("repair_hint") or ""),
        "hallucinated": hallucinated,
        "passed": passed,
    }
    if not passed:
        sample["failure_reason"] = "hallucination_detected" if hallucinated else "extraction_mismatch"
    return sample


def _run_update_case(
    memory_api: MemoryAPI,
    case: dict[str, Any],
    *,
    index: int,
    phase: str,
    default_scope: ScopeRef,
) -> dict[str, Any]:
    merged = dict(case)
    expected_current_text = _normalize_terms(merged.get("expect_current_text"))
    merged["expect_any_text"] = _merge_terms(_normalize_terms(merged.get("expect_any_text")), expected_current_text)
    sample = _run_recall_case(memory_api, merged, index=index, phase=phase, default_scope=default_scope)
    sample["expected_current_text"] = expected_current_text
    sample["case_note"] = "update phase uses recall + current state checks"
    return sample


def _run_recall_case(
    memory_api: MemoryAPI,
    case: dict[str, Any],
    *,
    index: int,
    phase: str,
    default_scope: ScopeRef,
) -> dict[str, Any]:
    case_scope = ScopeRef.from_dict(case.get("scope") or asdict(default_scope))
    query = str(case.get("query") or "")
    if not query.strip():
        return _invalid_case(index, case_scope, "empty_query", case=case)

    limit = _limit_value(case.get("limit"), default=5)
    task_context = dict(case.get("task_context") or {})
    expected_titles = _normalize_terms(case.get("expect_any_title"))
    expected_record_ids = _normalize_terms(case.get("expect_any_record_id"))
    expected_kinds = _normalize_terms(case.get("expect_any_kind"))
    expected_text = _normalize_terms(case.get("expect_any_text"))
    forbid_terms = _normalize_terms(case.get("forbid_any_text"))
    expected_current_text = _normalize_terms(case.get("expect_current_text"))

    bundle = memory_api.recall(
        query=query,
        scope=asdict(case_scope),
        task_context=task_context,
        limit=limit,
    )
    returned = list(bundle.items)
    returned_record_ids = [item.record_id for item in returned]
    returned_titles = [item.title for item in returned]
    returned_texts = _collect_record_texts(returned)

    expected_rank = _first_matching_rank(
        returned=returned,
        expected_record_ids=expected_record_ids,
        expected_titles=expected_titles,
        expected_kinds=expected_kinds,
        expected_text=expected_text,
        expected_current_text=expected_current_text,
    )
    expected_present = bool(expected_titles or expected_record_ids or expected_kinds or expected_text or expected_current_text)
    hallucinated = bool(_text_contains_any(values=returned_texts, terms=forbid_terms))
    sample_metrics = _rank_metrics(expected_rank=expected_rank, returned_count=len(returned), limit=limit, expected_present=expected_present)

    if expected_present:
        passed = expected_rank > 0 and not hallucinated
    else:
        passed = bool(returned) and not hallucinated

    sample: dict[str, Any] = {
        "index": index,
        "case_id": str(case.get("case_id") or case.get("id") or index),
        "phase": phase,
        "query": query,
        "scope": asdict(case_scope),
        "task_context": task_context,
        "limit": limit,
        "expected_titles": expected_titles,
        "expected_record_ids": expected_record_ids,
        "expected_kinds": expected_kinds,
        "expected_text": expected_text,
        "expected_current_text": expected_current_text,
        "forbid_any_text": forbid_terms,
        "expected": {
            "titles": expected_titles,
            "record_ids": expected_record_ids,
            "kinds": expected_kinds,
            "texts": expected_text,
            "current_text": expected_current_text,
        },
        "returned_record_ids": returned_record_ids,
        "returned_titles": returned_titles,
        "returned_texts": returned_texts,
        "returned_record_count": len(returned),
        "expected_rank": int(expected_rank),
        "recall_at_k": sample_metrics["recall_at_k"],
        "precision_at_k": sample_metrics["precision_at_k"],
        "ndcg_at_k": sample_metrics["ndcg_at_k"],
        "mrr": sample_metrics["mrr"],
        "repair_hint": str(case.get("repair_hint") or ""),
        "hallucinated": hallucinated,
        "passed": passed,
    }
    if not passed:
        sample["failure_reason"] = "hallucination_detected" if hallucinated else ("expectation_mismatch" if expected_present else "no_results")
    return sample


def _rank_metrics(*, expected_rank: int, returned_count: int, limit: int, expected_present: bool) -> dict[str, float]:
    if not expected_present:
        hit = returned_count > 0
        return {
            "recall_at_k": 1.0 if hit else 0.0,
            "precision_at_k": 1.0 if hit else 0.0,
            "ndcg_at_k": 1.0 if hit else 0.0,
            "mrr": 1.0 if hit else 0.0,
        }
    if expected_rank <= 0 or expected_rank > limit:
        return {"recall_at_k": 0.0, "precision_at_k": 0.0, "ndcg_at_k": 0.0, "mrr": 0.0}
    top_count = max(1, min(returned_count, limit))
    return {
        "recall_at_k": 1.0,
        "precision_at_k": round(1.0 / top_count, 3),
        "ndcg_at_k": round(1.0 / math.log2(expected_rank + 1), 3),
        "mrr": round(1.0 / expected_rank, 3),
    }


def _first_matching_rank(
    *,
    returned: list[Any],
    expected_record_ids: list[str],
    expected_titles: list[str],
    expected_kinds: list[str],
    expected_text: list[str],
    expected_current_text: list[str],
) -> int:
    for index, item in enumerate(returned, start=1):
        if _record_matches_expected(
            item,
            expected_record_ids=expected_record_ids,
            expected_titles=expected_titles,
            expected_kinds=expected_kinds,
            expected_text=expected_text,
            expected_current_text=expected_current_text,
        ):
            return index
    return 0


def _record_matches_expected(
    item: Any,
    *,
    expected_record_ids: list[str],
    expected_titles: list[str],
    expected_kinds: list[str],
    expected_text: list[str],
    expected_current_text: list[str],
) -> bool:
    if expected_record_ids and str(item.record_id) in expected_record_ids:
        return True
    if expected_titles and str(item.title) in expected_titles:
        return True
    if expected_kinds and str(item.kind) in expected_kinds:
        return True
    expected_text_terms = list(expected_text) + list(expected_current_text)
    if expected_text_terms and _text_contains_any(
        values=[
            str(item.title),
            str(item.summary),
            str(item.detail),
            str(item.content.get("text") or ""),
            str(item.content.get("summary") or ""),
            str(item.meta.get("memory_type") or ""),
            str(item.meta.get("current_text") or ""),
        ],
        terms=expected_text_terms,
    ):
        return True
    return False


def _collect_record_texts(items: list[Any]) -> list[str]:
    texts: list[str] = []
    for item in items:
        texts.append(str(item.title))
        texts.append(str(item.summary))
        texts.append(str(item.detail))
        texts.append(str(item.content.get("text") or ""))
        texts.append(str(item.content.get("summary") or ""))
        texts.append(str(item.meta.get("memory_type") or ""))
    return texts


def _text_contains_any(*, values: list[str], terms: list[str]) -> bool:
    haystack = " ".join(str(value).lower() for value in values)
    for term in terms:
        needle = str(term).strip().lower()
        if needle and needle in haystack:
            return True
    return False


def _normalize_terms(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    return []


def _merge_terms(*parts: list[str]) -> list[str]:
    seen: set[str] = set()
    merged: list[str] = []
    for part in parts:
        for item in part:
            if item in seen:
                continue
            seen.add(item)
            merged.append(item)
    return merged


def _limit_value(value: Any, *, default: int = 5) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return max(1, min(100, parsed))


def _invalid_case(index: int, scope: ScopeRef, failure_reason: str, case: dict[str, Any] | None = None) -> dict[str, Any]:
    base_case = dict(case or {})
    return {
        "index": index,
        "case_id": str(base_case.get("case_id") or base_case.get("id") or index),
        "phase": str(base_case.get("phase") or "usage"),
        "input_text": str(base_case.get("input_text") or ""),
        "query": str(base_case.get("query") or ""),
        "scope": asdict(scope),
        "task_context": dict(base_case.get("task_context") or {}),
        "limit": _limit_value(base_case.get("limit"), default=5),
        "expected_titles": _normalize_terms(base_case.get("expect_any_title")),
        "expected_record_ids": _normalize_terms(base_case.get("expect_any_record_id")),
        "expected_kinds": _normalize_terms(base_case.get("expect_any_kind")),
        "expected_text": _normalize_terms(base_case.get("expect_any_text")),
        "expected_current_text": _normalize_terms(base_case.get("expect_current_text")),
        "forbid_any_text": _normalize_terms(base_case.get("forbid_any_text")),
        "expected": {
            "titles": _normalize_terms(base_case.get("expect_any_title")),
            "record_ids": _normalize_terms(base_case.get("expect_any_record_id")),
            "kinds": _normalize_terms(base_case.get("expect_any_kind")),
            "texts": _normalize_terms(base_case.get("expect_any_text")),
            "current_text": _normalize_terms(base_case.get("expect_current_text")),
        },
        "returned_record_ids": [],
        "returned_titles": [],
        "returned_texts": [],
        "expected_rank": 0,
        "recall_at_k": 0.0,
        "precision_at_k": 0.0,
        "ndcg_at_k": 0.0,
        "mrr": 0.0,
        "repair_hint": str(base_case.get("repair_hint") or ""),
        "failure_reason": failure_reason,
        "hallucinated": False,
        "passed": False,
    }


def _phase_scores(samples: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for sample in samples:
        grouped[str(sample.get("phase") or "usage")].append(sample)

    scores: dict[str, dict[str, Any]] = {}
    for phase in sorted(SUPPORTED_PHASES):
        phase_samples = grouped.get(phase, [])
        sample_count = len(phase_samples)
        passed = [bool(sample.get("passed")) for sample in phase_samples]
        pass_rate = binary_pass_rate(passed) if phase_samples else 0.0
        hallucination_count = sum(1 for sample in phase_samples if bool(sample.get("hallucinated")))
        hallucination_rate = round(hallucination_count / sample_count, 3) if sample_count else 0.0

        expected_ranks = [
            int(sample.get("expected_rank") or 0)
            for sample in phase_samples
            if sample.get("expected_rank")
        ]
        if expected_ranks and sample_count:
            mrr = mean_reciprocal_rank([item for item in expected_ranks])
            recall = round(sum(1 for item in expected_ranks if item > 0) / sample_count, 3)
            precision = round(sum(1 for item in expected_ranks if item > 0) / sample_count, 3)
        else:
            mrr = 0.0
            recall = 0.0
            precision = 0.0

        scores[phase] = {
            "sample_count": sample_count,
            "pass_rate": pass_rate,
            "hallucination_rate": hallucination_rate,
            "mrr": mrr,
            "recall_at_k": recall,
            "precision_at_k": precision,
        }
    return scores


def _emit_eval_incident(runtime: Any, sample: dict[str, Any], suite: dict[str, Any]):
    return runtime.evolution.observe(
        signal_type="incident",
        payload={
            "title": f"Memory eval failure: {sample.get('case_id')}",
            "summary": sample.get("failure_reason") or "Memory evaluation sample failed.",
            "incident_type": "memory_eval_failure",
            "severity": "medium",
            "eval_failure": True,
            "eval_suite": suite.get("name", "memory_eval_ci"),
            "eval_case_id": sample.get("case_id", ""),
            "eval_phase": sample.get("phase", ""),
            "query": str(sample.get("query") or sample.get("input_text") or ""),
            "expected": dict(sample.get("expected") or {}),
            "returned_record_ids": list(sample.get("returned_record_ids") or []),
            "repair_hint": str(sample.get("repair_hint") or ""),
            "suggested_replay_dataset": [
                {
                    "id": sample.get("case_id"),
                    "query": str(sample.get("query") or sample.get("input_text") or ""),
                    "scope": sample.get("scope") or suite["scope"],
                    "task_context": dict(sample.get("task_context") or {}),
                    "expect_any_title": sample.get("expected_titles") or [],
                    "expect_any_record_id": sample.get("expected_record_ids") or [],
                    "expect_any_kind": sample.get("expected_kinds") or [],
                    "expect_any_text": sample.get("expected_text") or [],
                    "expect_current_text": sample.get("expected_current_text") or [],
                    "forbid_any_text": sample.get("forbid_any_text") or [],
                    "limit": int(sample.get("limit") or 5),
                }
            ],
        },
        scope=suite["scope"],
    )
