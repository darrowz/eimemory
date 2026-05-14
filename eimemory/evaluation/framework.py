"""Deterministic evaluation runner for eimemory capabilities."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from eimemory.api.memory import MemoryAPI
from eimemory.core.clock import now_iso
from eimemory.models.records import ScopeRef


def run_evaluation(
    runtime,
    dataset: dict | list,
    *,
    scope: dict | None = None,
    task_type: str = "",
    profile: str = "balanced",
    seed: bool = True,
) -> dict:
    """Run a recall-focused evaluation dataset against a runtime.

    The initial framework intentionally keeps the contract small: seed optional
    memories, run recall cases, and report hit-rate/MRR/precision diagnostics.
    """

    normalized = _normalize_dataset(dataset)
    scope_ref = ScopeRef.from_dict(scope or normalized.get("scope") or {})
    default_task_type = str(task_type or normalized.get("task_type") or "")
    default_profile = _normalize_profile(profile or normalized.get("profile") or "balanced")
    seed_records = list(normalized.get("seed") or normalized.get("seed_records") or [])
    cases = list(normalized.get("cases") or normalized.get("samples") or [])
    seeded_record_ids: list[str] = []
    errors: list[dict[str, Any]] = []

    if seed:
        for index, item in enumerate(seed_records):
            if not isinstance(item, dict):
                errors.append({"phase": "seed", "index": index, "error": "invalid_seed"})
                continue
            try:
                record = runtime.memory.ingest(
                    text=str(item.get("text") or item.get("summary") or ""),
                    memory_type=str(item.get("memory_type") or item.get("type") or "fact"),
                    title=str(item.get("title") or f"Eval seed {index + 1}"),
                    scope=dict(item.get("scope") or asdict(scope_ref)),
                    source=str(item.get("source") or "eimemory.eval.seed"),
                    tags=[str(tag) for tag in (item.get("tags") or [])],
                    force_capture=bool(item.get("force_capture", True)),
                )
                if record.status == "active":
                    seeded_record_ids.append(record.record_id)
            except Exception as exc:  # pragma: no cover - defensive eval boundary
                errors.append({"phase": "seed", "index": index, "error": exc.__class__.__name__, "detail": str(exc)})

    memory_api = MemoryAPI(runtime.store)
    sample_reports: list[dict[str, Any]] = []
    hit_count = 0
    reciprocal_ranks: list[float] = []
    precisions: list[float] = []

    for index, case in enumerate(cases):
        if not isinstance(case, dict):
            sample = _invalid_case(index, scope_ref, default_task_type, "invalid_case")
            sample_reports.append(sample)
            continue
        sample = _run_recall_case(
            memory_api,
            case,
            index=index,
            default_scope=scope_ref,
            default_task_type=default_task_type,
            default_profile=default_profile,
        )
        sample_reports.append(sample)
        if sample["hit"]:
            hit_count += 1
        reciprocal_ranks.append(float(sample["reciprocal_rank"]))
        if sample["precision_at_k"] is not None:
            precisions.append(float(sample["precision_at_k"]))

    sample_count = len(sample_reports)
    miss_count = sample_count - hit_count
    pass_rate = round(hit_count / sample_count, 3) if sample_count else 0.0
    mrr = round(sum(reciprocal_ranks) / sample_count, 3) if sample_count else 0.0
    precision_at_k = round(sum(precisions) / len(precisions), 3) if precisions else 0.0
    misses = [sample for sample in sample_reports if not sample["hit"]]

    return {
        "ok": True,
        "schema_version": 1,
        "name": str(normalized.get("name") or "evaluation"),
        "generated_at": now_iso(),
        "scope": asdict(scope_ref),
        "task_type": default_task_type,
        "profile": default_profile,
        "seeded": bool(seed),
        "seeded_record_ids": seeded_record_ids,
        "seed_error_count": len(errors),
        "errors": errors,
        "sample_count": sample_count,
        "hit_count": hit_count,
        "miss_count": miss_count,
        "pass_rate": pass_rate,
        "mrr": mrr,
        "precision_at_k": precision_at_k,
        "misses": misses,
        "samples": sample_reports,
    }


def run_memory_eval_ci(
    runtime,
    dataset: dict | list,
    *,
    emit_incidents: bool = False,
) -> dict:
    """Run the benchmark-style memory evaluation CI dataset."""
    from eimemory.evaluation.benchmarks import run_memory_eval_ci as run_memory_eval_ci_impl

    return run_memory_eval_ci_impl(runtime, dataset, emit_incidents=emit_incidents)


def _normalize_dataset(dataset: dict | list) -> dict:
    if isinstance(dataset, list):
        return {"name": "list_dataset", "cases": dataset}
    if isinstance(dataset, dict):
        return dict(dataset)
    raise ValueError("evaluation dataset must be a JSON object or list")


def _run_recall_case(
    memory_api: MemoryAPI,
    case: dict,
    *,
    index: int,
    default_scope: ScopeRef,
    default_task_type: str,
    default_profile: str,
) -> dict:
    query = str(case.get("query") or "")
    sample_scope = dict(case.get("scope") or asdict(default_scope))
    task_context = dict(case.get("task_context") or {})
    task_type = str(case.get("task_type") or task_context.get("task_type") or default_task_type)
    if task_type and not str(task_context.get("task_type") or ""):
        task_context["task_type"] = task_type
    profile = _normalize_profile(case.get("profile") or default_profile)
    task_context.setdefault("recall_profile", profile)
    limit = _positive_int(case.get("limit"), default=5)
    expected_titles = {str(item) for item in (case.get("expect_any_title") or []) if str(item)}
    expected_ids = {str(item) for item in (case.get("expect_any_record_id") or []) if str(item)}
    expected_kinds = {str(item) for item in (case.get("expect_any_kind") or []) if str(item)}
    expected_terms = {str(item).lower() for item in (case.get("expect_any_text") or []) if str(item)}
    if not query:
        return _invalid_case(index, ScopeRef.from_dict(sample_scope), task_type, "empty_query")

    bundle = memory_api.recall(query=query, scope=sample_scope, task_context=task_context, limit=limit)
    returned = list(bundle.items)
    if case.get("kinds"):
        allowed_kinds = {str(item) for item in case.get("kinds") or [] if str(item)}
        returned = [item for item in returned if item.kind in allowed_kinds]
    ranks = [
        rank
        for rank, item in enumerate(returned, start=1)
        if _record_matches_expected(
            item,
            expected_titles=expected_titles,
            expected_ids=expected_ids,
            expected_kinds=expected_kinds,
            expected_terms=expected_terms,
        )
    ]
    has_expectation = bool(expected_titles or expected_ids or expected_kinds or expected_terms)
    hit = bool(ranks) if has_expectation else bool(returned)
    reciprocal_rank = round(1.0 / ranks[0], 3) if ranks else 0.0
    precision_at_k = round(len(ranks) / len(returned), 3) if returned and has_expectation else None
    return {
        "index": index,
        "case_id": str(case.get("id") or case.get("case_id") or index),
        "query": query,
        "scope": sample_scope,
        "task_type": task_type,
        "profile": profile,
        "hit": hit,
        "reciprocal_rank": reciprocal_rank,
        "precision_at_k": precision_at_k,
        "expected_titles": sorted(expected_titles),
        "expected_record_ids": sorted(expected_ids),
        "expected_kinds": sorted(expected_kinds),
        "expected_text": sorted(expected_terms),
        "returned_record_ids": [item.record_id for item in returned],
        "returned_titles": [item.title for item in returned],
        "returned_kinds": [item.kind for item in returned],
        "confidence": bundle.confidence,
        "recall_profile": str(bundle.explanation.get("recall_profile") or profile),
        "retrieval_mode": str(bundle.explanation.get("retrieval_mode") or ""),
        "vector_hits": int(bundle.explanation.get("vector_hits") or 0),
    }


def _invalid_case(index: int, scope: ScopeRef, task_type: str, error: str) -> dict:
    return {
        "index": index,
        "case_id": str(index),
        "query": "",
        "scope": asdict(scope),
        "task_type": task_type,
        "profile": "balanced",
        "hit": False,
        "reciprocal_rank": 0.0,
        "precision_at_k": 0.0,
        "expected_titles": [],
        "expected_record_ids": [],
        "expected_kinds": [],
        "expected_text": [],
        "returned_record_ids": [],
        "returned_titles": [],
        "returned_kinds": [],
        "confidence": 0.0,
        "recall_profile": "balanced",
        "retrieval_mode": "",
        "vector_hits": 0,
        "error": error,
    }


def _record_matches_expected(
    item,
    *,
    expected_titles: set[str],
    expected_ids: set[str],
    expected_kinds: set[str],
    expected_terms: set[str],
) -> bool:
    if item.record_id in expected_ids:
        return True
    if item.title in expected_titles:
        return True
    if item.kind in expected_kinds:
        return True
    text = " ".join([item.title, item.summary, item.detail]).lower()
    return any(term in text for term in expected_terms)


def _normalize_profile(value: object) -> str:
    profile = str(value or "balanced").strip().lower()
    return profile if profile in {"precision", "balanced", "exploratory"} else "balanced"


def _positive_int(value: object, *, default: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return max(1, min(1000, parsed))
