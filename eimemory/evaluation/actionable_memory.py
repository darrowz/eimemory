"""ActionableMemory smoke evaluator for recall/posture/query checks.

This evaluator focuses on a small deterministic dataset surface that exercises:

- recall quality for mixed memory/project/research intents
- posture synthesis quality from living-memory context
- contamination leakage checks for project queries

It does not alter core recall/posture implementations; it composes existing APIs
through thin evaluation-only adapters.
"""

from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
import tempfile
from typing import Any

from eimemory.core.clock import now_iso
from eimemory.models.records import RecordEnvelope, ScopeRef


def normalize_actionable_memory_dataset(dataset: dict | list) -> dict[str, Any]:
    if isinstance(dataset, list):
        raw = {"name": "actionable_memory", "cases": dataset}
    elif isinstance(dataset, dict):
        raw = dict(dataset)
    else:
        raise ValueError("ActionableMemory dataset must be a JSON object or list")

    scope = asdict(ScopeRef.from_dict(raw.get("scope") or {}))
    seed = [dict(item) for item in list(raw.get("seed") or raw.get("seed_records") or []) if isinstance(item, dict)]
    cases = [dict(item) for item in list(raw.get("cases") or raw.get("samples") or []) if isinstance(item, dict)]

    return {
        "schema_version": 1,
        "name": str(raw.get("name") or raw.get("dataset_name") or "actionable_memory_eval"),
        "scope": scope,
        "seed": seed,
        "cases": cases,
    }


def run_actionable_memory_eval(
    runtime,
    dataset: dict | list,
    *,
    persist_report: bool = False,
) -> dict[str, Any]:
    normalized = normalize_actionable_memory_dataset(dataset)
    dataset_scope = ScopeRef.from_dict(normalized["scope"])
    seed = list(normalized["seed"])
    cases = list(normalized["cases"])

    if seed:
        with tempfile.TemporaryDirectory(prefix="eimemory-actionable-eval-") as temp_root:
            from eimemory.api.runtime import Runtime

            eval_runtime = Runtime.create(root=Path(temp_root))
            try:
                report = _run_actionable_memory_eval_on_runtime(
                    eval_runtime,
                    normalized=normalized,
                    dataset_scope=dataset_scope,
                    seed=seed,
                    cases=cases,
                )
            finally:
                eval_runtime.close()
    else:
        report = _run_actionable_memory_eval_on_runtime(
            runtime,
            normalized=normalized,
            dataset_scope=dataset_scope,
            seed=seed,
            cases=cases,
        )

    if persist_report:
        record = _report_record(report, scope=dataset_scope)
        runtime.store.append(record)
        report["persisted"] = True
        report["persisted_record_id"] = record.record_id

    return report


def _run_actionable_memory_eval_on_runtime(
    runtime,
    *,
    normalized: dict[str, Any],
    dataset_scope: ScopeRef,
    seed: list[dict[str, Any]],
    cases: list[dict[str, Any]],
) -> dict[str, Any]:
    seeded_records, record_errors = _seed_records(runtime, seed, default_scope=dataset_scope)
    records_by_seed_id = {
        seed_id: record
        for seed_id, record in seeded_records
        if seed_id
    }

    sample_reports: list[dict[str, Any]] = []
    recall_count = 0
    posture_count = 0
    project_query_count = 0
    recall_pass_count = 0
    posture_pass_count = 0
    contamination_count = 0
    project_contamination_count = 0

    for index, case in enumerate(cases):
        case_type = str(case.get("case_type") or "recall").strip().lower()
        case_seed = _case_record(
            case,
            index=index,
            seeded_records=seeded_records,
            records_by_seed_id=records_by_seed_id,
        )
        case_sample_scope = ScopeRef.from_dict(case.get("scope") or normalized["scope"])
        case_sample = _run_case(
            runtime=runtime,
            case=case,
            index=index,
            case_type=case_type,
            case_seed=case_seed,
            dataset_scope=dataset_scope,
            case_scope=case_sample_scope,
        )
        sample_reports.append(case_sample)

        if case_type == "posture":
            posture_count += 1
            if case_sample.get("passed"):
                posture_pass_count += 1
        else:
            recall_count += 1
            if bool(case_sample.get("passed")):
                recall_pass_count += 1

        contamination = bool(case_sample.get("contamination_detected"))
        if contamination:
            contamination_count += 1
        if case.get("query_type") == "project" and contamination:
            project_contamination_count += 1
        if case.get("query_type") == "project":
            project_query_count += 1

    sample_count = len(sample_reports)
    pass_count = sum(1 for sample in sample_reports if sample.get("passed"))
    recall_topk_pass_rate = round(recall_pass_count / recall_count, 3) if recall_count else 0.0
    posture_pass_rate = round(posture_pass_count / posture_count, 3) if posture_count else 0.0
    contamination_rate = round(contamination_count / sample_count, 3) if sample_count else 0.0
    project_query_contamination_rate = (
        round(project_contamination_count / project_query_count, 3) if project_query_count else 0.0
    )
    pass_rate = round(pass_count / sample_count, 3) if sample_count else 0.0

    report: dict[str, Any] = {
        "ok": True,
        "schema_version": 1,
        "report_type": "actionable_memory_eval",
        "name": normalized["name"],
        "generated_at": now_iso(),
        "scope": asdict(dataset_scope),
        "seed_count": len(seeded_records),
        "seed_error_count": len(record_errors),
        "errors": record_errors,
        "sample_count": sample_count,
        "pass_count": pass_count,
        "pass_rate": pass_rate,
        "recall_topk_pass_rate": recall_topk_pass_rate,
        "posture_pass_rate": posture_pass_rate,
        "contamination_rate": contamination_rate,
        "project_query_contamination_rate": project_query_contamination_rate,
        "seeded_record_ids": [record.record_id for _, record in seeded_records],
        "samples": sample_reports,
        "persisted": False,
        "persisted_record_id": "",
        "sample_type_counts": {
            "recall": recall_count,
            "posture": posture_count,
        },
        "record_errors": record_errors,
    }

    return report


def _seed_records(
    runtime,
    seed: list[dict[str, Any]],
    *,
    default_scope: ScopeRef,
) -> tuple[list[tuple[str, RecordEnvelope]], list[dict[str, Any]]]:
    seeded_records: list[tuple[str, RecordEnvelope]] = []
    errors: list[dict[str, Any]] = []
    for index, item in enumerate(seed):
        if not isinstance(item, dict):
            errors.append({"phase": "seed", "index": index, "error": "invalid_seed"})
            continue
        seed_id = str(item.get("id") or item.get("seed_id") or index)
        kind = str(item.get("kind") or "memory").strip() or "memory"
        scope = ScopeRef.from_dict(item.get("scope") or asdict(default_scope))
        title = str(item.get("title") or f"ActionableMemoryEval seed {index + 1}")
        text = str(item.get("text") or item.get("summary") or item.get("detail") or "")
        source = str(item.get("source") or "eimemory.actionable_memory.seed")
        meta = dict(item.get("meta") or {})
        tags = [str(tag) for tag in list(item.get("tags") or [])]
        links = []
        evidence = [str(item) for item in list(item.get("evidence") or []) if str(item).strip()]

        try:
            if kind == "memory":
                record = runtime.memory.ingest(
                    text=text or title,
                    memory_type=str(item.get("memory_type") or item.get("type") or "preference"),
                    title=title,
                    scope=asdict(scope),
                    source=source,
                    tags=tags,
                    force_capture=bool(item.get("force_capture", True)),
                    meta=meta,
                )
            else:
                content = dict(item.get("content") or {})
                if text and "text" not in content:
                    content["text"] = text
                if not content.get("summary") and text:
                    content.setdefault("summary", text)
                content.update(_string_fields(item, ["summary", "detail"]))
                record = RecordEnvelope.create(
                    kind=kind,
                    title=title,
                    summary=str(item.get("summary") or text or title),
                    detail=str(item.get("detail") or text),
                    content=content,
                    tags=tags,
                    links=links,
                    evidence=evidence,
                    source=source,
                    scope=scope,
                    status=str(item.get("status") or "active"),
                    provenance=dict(item.get("provenance") or {}),
                    meta=meta,
                )
                runtime.store.append(record)
            if getattr(record, "status", "") != "rejected":
                seeded_records.append((seed_id, record))
        except Exception as exc:  # pragma: no cover - defensive eval boundary
            errors.append(
                {
                    "phase": "seed",
                    "index": index,
                    "id": seed_id,
                    "kind": kind,
                    "error": exc.__class__.__name__,
                    "detail": str(exc),
                }
            )
    return seeded_records, errors


def _run_case(
    *,
    runtime,
    case: dict[str, Any],
    index: int,
    case_type: str,
    case_seed: RecordEnvelope | None,
    dataset_scope: ScopeRef,
    case_scope: ScopeRef,
) -> dict[str, Any]:
    if case_type == "posture":
        return _run_posture_case(
            runtime=runtime,
            case=case,
            index=index,
            case_seed=case_seed,
            scope=case_scope,
            dataset_scope=dataset_scope,
        )
    return _run_recall_case(
        runtime=runtime,
        case=case,
        index=index,
        case_seed=case_seed,
        scope=case_scope,
        dataset_scope=dataset_scope,
    )


def _run_recall_case(
    *,
    runtime,
    case: dict[str, Any],
    index: int,
    case_seed: RecordEnvelope | None,
    scope: ScopeRef,
    dataset_scope: ScopeRef,
) -> dict[str, Any]:
    case_id = str(case.get("id") or case.get("case_id") or index)
    query = str(case.get("query") or "")
    if not query:
        return {
            "index": index,
            "case_id": case_id,
            "case_type": "recall",
            "query": "",
            "scope": asdict(scope),
            "seed_record_id": case_seed.record_id if case_seed else "",
            "passed": False,
            "error": "empty_query",
            "recall_topk": 0,
            "recall_topk_passed": False,
            "contamination_detected": False,
            "returned_record_ids": [],
            "returned_titles": [],
            "returned_kinds": [],
            "constraints": [],
            "query_type": str(case.get("query_type") or ""),
            "confidence": 0.0,
            "recall_profile": "",
            "retrieval_mode": "structured",
        }

    query_type = str(case.get("query_type") or "project").strip().lower()
    limit = _positive_int(case.get("limit"), default=5)
    task_context = dict(case.get("task_context") or {})
    task_context.setdefault("task_type", _query_task_type(query_type, default_task_type=case.get("task_type")))
    task_context.setdefault("recall_profile", str(case.get("recall_profile") or "balanced"))
    if query_type:
        task_context["query_type"] = query_type

    recall_bundle = runtime.memory.recall(
        query=query,
        scope=asdict(scope),
        task_context=task_context,
        limit=limit,
    )
    returned_records = list(recall_bundle.items)

    expected_titles = {str(item) for item in (case.get("expect_any_title") or []) if str(item).strip()}
    expected_kinds = {str(item).lower() for item in (case.get("expect_any_kind") or []) if str(item).strip()}
    expected_text = {str(item).lower() for item in (case.get("expect_any_text") or []) if str(item).strip()}
    expected_record_ids = {str(item) for item in (case.get("expect_any_record_id") or []) if str(item).strip()}
    forbid_titles = [str(item).strip().lower() for item in (case.get("forbid_any_title") or []) if str(item).strip()]
    forbid_kinds = {str(item).strip().lower() for item in (case.get("forbid_any_kind") or []) if str(item).strip()}

    scoring_records = _filter_records_for_eval(returned_records, expected_kinds=expected_kinds)
    contamination_detected = _detect_contamination(returned_records, forbid_titles=forbid_titles, forbid_kinds=forbid_kinds)
    if query_type == "project":
        contamination_detected = contamination_detected or _detect_project_contamination(returned_records, query=query, query_type=query_type)

    recall_pass = False
    rank = 0
    returned_record_ids = [record.record_id for record in returned_records]
    returned_titles = [record.title for record in returned_records]
    returned_kinds = [record.kind for record in returned_records]
    for rindex, record in enumerate(scoring_records, start=1):
        if not recall_pass:
            matches = _record_matches_expected(
                record,
                expected_titles=expected_titles,
                expected_record_ids=expected_record_ids,
                expected_kinds=expected_kinds,
                expected_text=expected_text,
            )
            if matches:
                rank = rindex
                recall_pass = True
    has_expectation = bool(expected_titles or expected_record_ids or expected_kinds or expected_text)
    if not has_expectation:
        recall_pass = bool(returned_records)
        rank = 1 if returned_records else 0
    recall_pass = bool(recall_pass and not contamination_detected)

    return {
        "index": index,
        "case_id": case_id,
        "case_type": "recall",
        "query": query,
        "query_type": query_type,
        "scope": asdict(scope),
        "dataset_scope": asdict(dataset_scope),
        "seed_record_id": case_seed.record_id if case_seed else "",
        "recall_topk": limit,
        "rank": rank,
        "passed": recall_pass,
        "recall_topk_passed": recall_pass,
        "contamination_detected": contamination_detected,
        "returned_record_ids": returned_record_ids,
        "returned_titles": returned_titles,
        "returned_kinds": returned_kinds,
        "expected_titles": sorted(expected_titles),
        "expected_record_ids": sorted(expected_record_ids),
        "expected_kinds": sorted(expected_kinds),
        "expected_text": sorted(expected_text),
        "forbid_titles": forbid_titles,
        "forbid_kinds": sorted(forbid_kinds),
        "confidence": float(recall_bundle.confidence),
        "recall_profile": str(recall_bundle.explanation.get("recall_profile") or ""),
        "retrieval_mode": str(recall_bundle.explanation.get("retrieval_mode") or ""),
        "vector_hits": int(recall_bundle.explanation.get("vector_hits") or 0),
    }


def _run_posture_case(
    *,
    runtime,
    case: dict[str, Any],
    index: int,
    case_seed: RecordEnvelope | None,
    scope: ScopeRef,
    dataset_scope: ScopeRef,
) -> dict[str, Any]:
    case_id = str(case.get("id") or case.get("case_id") or index)
    query = str(case.get("query") or "")
    if not query:
        return {
            "index": index,
            "case_id": case_id,
            "case_type": "posture",
            "query": "",
            "scope": asdict(scope),
            "seed_record_id": case_seed.record_id if case_seed else "",
            "passed": False,
            "error": "empty_query",
            "posture_profile_non_empty": False,
            "constraints_present": False,
            "constraints": [],
            "expected_constraints": [],
            "contamination_detected": False,
            "posture": {},
        }

    limit = _positive_int(case.get("limit"), default=5)
    report = runtime.recommend_action_posture(query, scope=asdict(scope), limit=limit)
    posture_profile = dict(report.get("profile") or {})
    constraints = [str(item) for item in (posture_profile.get("constraints") or []) if str(item)]
    expected_constraints = [str(item).strip() for item in (case.get("expected_constraints") or []) if str(item).strip()]
    constraints_present = all(constraint in constraints for constraint in expected_constraints)
    posture_profile_non_empty = bool(posture_profile.get("source_record_ids") or int(report.get("record_count") or 0) > 0)
    passed = bool(posture_profile_non_empty and constraints_present)
    return {
        "index": index,
        "case_id": case_id,
        "case_type": "posture",
        "query": query,
        "scope": asdict(scope),
        "dataset_scope": asdict(dataset_scope),
        "seed_record_id": case_seed.record_id if case_seed else "",
        "passed": passed,
        "posture_profile_non_empty": posture_profile_non_empty,
        "constraints_present": constraints_present,
        "constraints": constraints,
        "expected_constraints": expected_constraints,
        "contamination_detected": False,
        "posture": posture_profile,
        "posture_recommendation": str(posture_profile.get("recommended_action") or report.get("recommended_action") or ""),
        "posture_confidence": float(posture_profile.get("confidence") or 0.0),
        "posture_record_count": int(report.get("record_count") or 0),
        "posture_items": [dict(item) for item in list(report.get("items") or [])],
    }


def _record_matches_expected(
    record: RecordEnvelope,
    *,
    expected_titles: set[str],
    expected_record_ids: set[str],
    expected_kinds: set[str],
    expected_text: set[str],
) -> bool:
    if record.record_id in expected_record_ids:
        return True
    if record.title in expected_titles:
        return True
    if record.kind in expected_kinds:
        return True
    haystack = " ".join(
        [record.title, record.summary, record.detail, str(record.content.get("text") or ""), str(record.content.get("summary") or "")]
    ).lower()
    return any(term in haystack for term in expected_text) if expected_text else False


def _filter_records_for_eval(records: list[RecordEnvelope], *, expected_kinds: set[str]) -> list[RecordEnvelope]:
    if not expected_kinds:
        return list(records)
    filtered = [record for record in records if str(record.kind).lower() in expected_kinds]
    return filtered if filtered else list(records)


def _detect_project_contamination(returned_records: list[RecordEnvelope], *, query: str, query_type: str) -> bool:
    if query_type != "project":
        return False
    lowered_query = str(query or "").strip().lower()
    if not lowered_query:
        return False
    if "project" not in lowered_query and "项目" not in lowered_query and "交付" not in lowered_query:
        return False
    for record in returned_records:
        if record.kind != "knowledge_page":
            continue
        text = " ".join([record.title, record.summary, record.detail]).lower()
        if "siren" in text or "prism" in text:
            return True
    return False


def _detect_contamination(
    returned_records: list[RecordEnvelope],
    *,
    forbid_titles: list[str],
    forbid_kinds: set[str],
) -> bool:
    for record in returned_records:
        if str(record.kind).strip().lower() in forbid_kinds:
            return True
        haystack = f"{record.title} {record.summary} {record.detail}".lower()
        for term in forbid_titles:
            if term and term in haystack:
                return True
    return False


def _query_task_type(query_type: str, *, default_task_type: object) -> str:
    cleaned = str(query_type or "").strip().lower()
    if cleaned == "research":
        return "research"
    if cleaned == "project":
        return "project_delivery"
    if cleaned == "chat":
        return "chat.reply"
    if cleaned in {"preference", "style"}:
        return "chat.reply"
    return str(default_task_type or "")


def _case_record(
    case: dict[str, Any],
    *,
    index: int,
    seeded_records: list[tuple[str, RecordEnvelope]],
    records_by_seed_id: dict[str, RecordEnvelope],
) -> RecordEnvelope | None:
    seed_id = str(case.get("seed_id") or case.get("record_seed_id") or "")
    if seed_id and seed_id in records_by_seed_id:
        return records_by_seed_id[seed_id]
    if case.get("seed_index") is not None:
        try:
            return seeded_records[int(case["seed_index"])][1]
        except (IndexError, ValueError, TypeError):
            return None
    if index < len(seeded_records):
        return seeded_records[index][1]
    return None


def _positive_int(value: Any, *, default: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return max(1, min(1000, parsed))


def _report_record(report: dict[str, Any], *, scope: ScopeRef) -> RecordEnvelope:
    return RecordEnvelope.create(
        kind="reflection",
        title=f"ActionableMemoryEval report: {report['name']}",
        summary=f"ActionableMemoryEval pass_rate={report['pass_rate']}",
        scope=scope,
        source="eimemory.actionable_memory",
        content={"report": dict(report)},
        meta={
            "report_type": "actionable_memory_eval",
            "name": report["name"],
            "pass_rate": report["pass_rate"],
            "recall_topk_pass_rate": report["recall_topk_pass_rate"],
            "posture_pass_rate": report["posture_pass_rate"],
            "contamination_rate": report["contamination_rate"],
            "project_query_contamination_rate": report["project_query_contamination_rate"],
        },
    )


def _string_fields(payload: dict[str, Any], keys: list[str]) -> dict[str, str]:
    values: dict[str, str] = {}
    for key in keys:
        value = str(payload.get(key) or "")
        if value:
            values[key] = value
    return values
