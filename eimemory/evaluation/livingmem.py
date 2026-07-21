"""Deterministic LivingMemEval adapter for living-memory metadata."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict
from datetime import datetime, timezone
from statistics import mean
from typing import Any

from eimemory.core.clock import now_iso
from eimemory.living.schema import LIVING_MEMORY_META_KEY, enrich_living_memory, get_living_memory_meta
from eimemory.models.records import RecordEnvelope, ScopeRef


METRIC_KEYS: tuple[str, ...] = (
    "temporal_accuracy",
    "motive_accuracy",
    "affective_grounding",
    "repair_recall",
    "stale_label_avoidance",
    "posture_accuracy",
)


def normalize_livingmem_dataset(dataset: dict | list) -> dict[str, Any]:
    if isinstance(dataset, list):
        raw = {"name": "livingmem", "cases": dataset}
    elif isinstance(dataset, dict):
        raw = dict(dataset)
    else:
        raise ValueError("LivingMemEval dataset must be a JSON object or list")
    scope = asdict(ScopeRef.from_dict(raw.get("scope") or {}))
    seed = [dict(item) for item in list(raw.get("seed") or raw.get("seed_records") or []) if isinstance(item, dict)]
    cases = [dict(item) for item in list(raw.get("cases") or raw.get("samples") or []) if isinstance(item, dict)]
    return {
        "schema_version": 1,
        "name": str(raw.get("name") or raw.get("dataset_name") or "livingmem"),
        "scope": scope,
        "seed": seed,
        "cases": cases,
    }


def run_livingmem_eval(
    runtime,
    dataset: dict | list,
    *,
    persist_report: bool = False,
) -> dict[str, Any]:
    normalized = normalize_livingmem_dataset(dataset)
    dataset_scope = ScopeRef.from_dict(normalized["scope"])
    seeded_records = _seed_records(runtime, normalized["seed"], default_scope=dataset_scope)
    records_by_seed_id = {
        seed_id: record
        for seed_id, record in seeded_records
        if seed_id
    }
    sample_reports: list[dict[str, Any]] = []

    for index, case in enumerate(normalized["cases"]):
        record = _case_record(case, index=index, seeded_records=seeded_records, records_by_seed_id=records_by_seed_id)
        sample_reports.append(_run_case(case, index=index, record=record))

    metric_values = {key: _metric_average(sample_reports, key) for key in METRIC_KEYS}
    pass_count = sum(1 for sample in sample_reports if sample["passed"])
    sample_count = len(sample_reports)
    report = {
        "ok": True,
        "schema_version": 1,
        "report_type": "livingmem_eval",
        "name": normalized["name"],
        "generated_at": now_iso(),
        "scope": asdict(dataset_scope),
        "sample_count": sample_count,
        "pass_rate": round(pass_count / sample_count, 3) if sample_count else 0.0,
        **metric_values,
        "seeded_record_ids": [record.record_id for _, record in seeded_records],
        "samples": sample_reports,
        "persisted": False,
        "persisted_record_id": "",
    }
    if persist_report:
        record = _report_record(report, scope=dataset_scope)
        runtime.store.append(record)
        report = {**report, "persisted": True, "persisted_record_id": record.record_id}
    return report


def _seed_records(runtime, seed: list[dict[str, Any]], *, default_scope: ScopeRef) -> list[tuple[str, RecordEnvelope]]:
    records: list[tuple[str, RecordEnvelope]] = []
    for index, item in enumerate(seed):
        text = str(item.get("text") or item.get("summary") or item.get("detail") or "")
        title = str(item.get("title") or f"LivingMemEval seed {index + 1}")
        meta = dict(item.get("meta") or {})
        living = meta.get(LIVING_MEMORY_META_KEY)
        if not isinstance(living, Mapping):
            meta[LIVING_MEMORY_META_KEY] = enrich_living_memory(
                {"text": text, "title": title, "summary": text, "meta": meta}
            )
        record = runtime.memory.ingest(
            text=text,
            memory_type=str(item.get("memory_type") or item.get("type") or "preference"),
            title=title,
            scope=dict(item.get("scope") or asdict(default_scope)),
            source=str(item.get("source") or "eimemory.livingmem.seed"),
            source_id=item["source_id"] if "source_id" in item else "default",
            tags=[str(tag) for tag in list(item.get("tags") or [])],
            force_capture=bool(item.get("force_capture", True)),
            meta=meta,
        )
        if record.status == "active":
            records.append((str(item.get("id") or item.get("seed_id") or index), record))
    return records


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
        except (IndexError, TypeError, ValueError):
            return None
    if index < len(seeded_records):
        return seeded_records[index][1]
    return None


def _run_case(case: dict[str, Any], *, index: int, record: RecordEnvelope | None) -> dict[str, Any]:
    if record is None:
        return {
            "index": index,
            "case_id": str(case.get("id") or case.get("case_id") or index),
            "record_id": "",
            "passed": False,
            "metrics": {},
            "error": "record_not_found",
        }
    living = get_living_memory_meta(record)
    metric_results = {
        "temporal_accuracy": _matches_expected(living.get("temporal"), case.get("expect_temporal")),
        "motive_accuracy": _matches_expected(living.get("motive"), case.get("expect_motive")),
        "affective_grounding": _matches_expected(living.get("affective"), case.get("expect_affective")),
        "repair_recall": _repair_matches(living, case),
        "stale_label_avoidance": _stale_matches(living, case),
        "posture_accuracy": _matches_expected(living.get("action_posture"), case.get("expect_posture")),
    }
    applicable = {key: value for key, value in metric_results.items() if value is not None}
    return {
        "index": index,
        "case_id": str(case.get("id") or case.get("case_id") or index),
        "record_id": record.record_id,
        "title": record.title,
        "passed": bool(applicable) and all(applicable.values()),
        "metrics": applicable,
        "living_memory": living,
    }


def _matches_expected(actual: Any, expected: Any) -> bool | None:
    if not isinstance(expected, Mapping):
        return None
    actual_map = actual if isinstance(actual, Mapping) else {}
    for key, expected_value in expected.items():
        actual_value = actual_map.get(key)
        if isinstance(expected_value, list):
            if list(actual_value or []) != expected_value:
                return False
        elif actual_value != expected_value:
            return False
    return True


def _repair_matches(living: Mapping[str, Any], case: dict[str, Any]) -> bool | None:
    if "expect_repair_needed" not in case:
        return None
    affective = living.get("affective") if isinstance(living.get("affective"), Mapping) else {}
    return bool(affective.get("repair_needed")) is bool(case.get("expect_repair_needed"))


def _stale_matches(living: Mapping[str, Any], case: dict[str, Any]) -> bool | None:
    if "expect_stale" not in case:
        return None
    return _is_stale(living) is bool(case.get("expect_stale"))


def _is_stale(living: Mapping[str, Any]) -> bool:
    temporal = living.get("temporal") if isinstance(living.get("temporal"), Mapping) else {}
    if str(temporal.get("temporal_distance") or "").strip().lower() == "stale":
        return True
    valid_until = str(temporal.get("valid_until") or "").strip()
    if not valid_until:
        return False
    try:
        parsed = datetime.fromisoformat(valid_until.replace("Z", "+00:00"))
    except ValueError:
        return False
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed < datetime.now(timezone.utc)


def _metric_average(samples: list[dict[str, Any]], key: str) -> float:
    values = [
        1.0 if bool(sample.get("metrics", {}).get(key)) else 0.0
        for sample in samples
        if key in sample.get("metrics", {})
    ]
    return round(mean(values), 3) if values else 0.0


def _report_record(report: dict[str, Any], *, scope: ScopeRef) -> RecordEnvelope:
    return RecordEnvelope.create(
        kind="reflection",
        title=f"LivingMemEval report: {report['name']}",
        summary=f"LivingMemEval pass_rate={report['pass_rate']}",
        scope=scope,
        source="eimemory.livingmem",
        content={"report": dict(report)},
        meta={
            "report_type": "livingmem_eval",
            "name": report["name"],
            "pass_rate": report["pass_rate"],
        },
    )


def _record_text(record_or_text: Any) -> str:
    if isinstance(record_or_text, str):
        return record_or_text
    if isinstance(record_or_text, Mapping):
        return " ".join(
            str(record_or_text.get(key) or "")
            for key in ("title", "summary", "detail", "text")
            if str(record_or_text.get(key) or "").strip()
        )
    return " ".join(
        str(getattr(record_or_text, key, "") or "")
        for key in ("title", "summary", "detail")
        if str(getattr(record_or_text, key, "") or "").strip()
    )
