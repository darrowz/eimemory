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
from eimemory.models.records import RecordEnvelope, ScopeRef


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
    reciprocal_ranks: list[int] = []
    latencies_ms: list[float] = []
    outcome_polluted_count = 0
    reflection_polluted_count = 0
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
        reciprocal_ranks.append(int(sample.get("rank") or 0))
        latencies_ms.append(float(sample.get("latency_ms") or 0.0))
        if sample.get("outcome_polluted"):
            outcome_polluted_count += 1
        if sample.get("reflection_polluted"):
            reflection_polluted_count += 1
        if sample.get("empty"):
            empty_count += 1

    sample_count = len(sample_reports)
    return {
        "ok": True,
        "schema_version": 1,
        "report_type": "production_recall_eval",
        "name": str(normalized["name"]),
        "generated_at": now_iso(),
        "scope": asdict(dataset_scope),
        "seeded": len(seed_records) > 0,
        "seeded_record_ids": [record.record_id for _, record in seeded_records],
        "seed_lookup": seed_lookup,
        "seed_error_count": len(seed_errors),
        "errors": seed_errors,
        "sample_count": sample_count,
        "hit_at_1": round(sum(hit_at_1_scores) / sample_count, 3) if sample_count else 0.0,
        "hit_at_k": round(sum(hit_at_k_scores) / sample_count, 3) if sample_count else 0.0,
        "mrr": mean_reciprocal_rank([int(rank) for rank in reciprocal_ranks]) if sample_count else 0.0,
        "latency_ms_avg": round(sum(latencies_ms) / sample_count, 3) if sample_count else 0.0,
        "latency_ms_p95": percentile(latencies_ms, 95),
        "outcome_pollution_rate": round(outcome_polluted_count / sample_count, 3) if sample_count else 0.0,
        "reflection_pollution_rate": round(reflection_polluted_count / sample_count, 3) if sample_count else 0.0,
        "empty_rate": round(empty_count / sample_count, 3) if sample_count else 0.0,
        "samples": sample_reports,
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
        reciprocal_rank = round(1.0 / matched_rank, 3) if matched_rank else 0.0
    else:
        matched_rank = 1 if returned else 0
        hit_at_1 = 1.0 if matched_rank else 0.0
        hit_at_k = hit_at_1
        reciprocal_rank = 1.0 if returned else 0.0

    outcome_polluted = any(_is_outcome_pollution(item) for item in returned)
    reflection_polluted = any(_is_reflection_record(item) for item in returned)
    forbidden_by_case = any(
        _record_forbidden(
            record=item,
            forbid_kinds=forbid_kinds,
            forbid_title_contains=forbid_title_contains,
            forbid_source_contains=forbid_source_contains,
        )
        for item in returned
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
        "returned_texts": returned_texts,
        "returned_count": len(returned),
        "rank": matched_rank,
        "hit_at_1": hit_at_1,
        "hit_at_k": hit_at_k,
        "reciprocal_rank": reciprocal_rank,
        "matched_expected": bool(matched_rank) if has_expectation else bool(returned),
        "empty": not bool(returned),
        "forbid_hit": bool(forbidden_by_case),
        "outcome_polluted": bool(outcome_polluted),
        "reflection_polluted": bool(reflection_polluted),
        "explanation": {
            "recall_profile": str(bundle.explanation.get("recall_profile") or ""),
            "retrieval_mode": str(bundle.explanation.get("retrieval_mode") or ""),
            "vector_hits": int(bundle.explanation.get("vector_hits") or 0),
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
