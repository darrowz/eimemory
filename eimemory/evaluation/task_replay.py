"""Real task replay runner for OpenClaw, UUMit, and eimemory history cases."""

from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
import tempfile
from time import perf_counter
from typing import Any

from eimemory.core.clock import now_iso
from eimemory.evaluation.metrics import binary_pass_rate, percentile
from eimemory.governance.evidence_contract import current_release_identity, release_identity_payload
from eimemory.models.records import RecordEnvelope, ScopeRef


def normalize_real_task_replay_dataset(dataset: dict | list) -> dict[str, Any]:
    raw = {"name": "real_task_replay", "cases": dataset} if isinstance(dataset, list) else dict(dataset)
    if not isinstance(raw, dict):
        raise ValueError("Real task replay dataset must be a JSON object or list")
    scope = asdict(ScopeRef.from_dict(raw.get("scope") or {}))
    return {
        "schema_version": "real_task_replay.v1",
        "name": str(raw.get("name") or "real_task_replay"),
        "threshold": _threshold(raw.get("threshold"), default=0.8),
        "scope": scope,
        "seed": [dict(item) for item in list(raw.get("seed") or raw.get("seed_records") or []) if isinstance(item, dict)],
        "cases": [dict(item) for item in list(raw.get("cases") or raw.get("samples") or []) if isinstance(item, dict)],
    }


def run_real_task_replay(
    runtime,
    dataset: dict | list,
    *,
    seed: bool = True,
    persist_report: bool = False,
) -> dict[str, Any]:
    normalized = normalize_real_task_replay_dataset(dataset)
    if seed and normalized["seed"]:
        with tempfile.TemporaryDirectory(prefix="eimemory-real-task-replay-") as temp_root:
            from eimemory.api.runtime import Runtime

            eval_runtime = Runtime.create(root=Path(temp_root))
            try:
                report = _run_on_runtime(eval_runtime, normalized=normalized)
            finally:
                eval_runtime.close()
    else:
        report = _run_on_runtime(runtime, normalized=normalized if seed else {**normalized, "seed": []})
    if persist_report:
        report_scope = ScopeRef.from_dict(normalized["scope"])
        release = current_release_identity(runtime, report_scope)
        record = runtime.store.append(_report_record(report, scope=report_scope, release=release))
        report = {**report, "persisted_record_id": record.record_id}
    return report


def _run_on_runtime(runtime, *, normalized: dict[str, Any]) -> dict[str, Any]:
    scope = ScopeRef.from_dict(normalized["scope"])
    seeded_record_ids = _seed_records(runtime, normalized["seed"], scope=scope)
    samples: list[dict[str, Any]] = []
    latencies: list[float] = []
    for index, case in enumerate(normalized["cases"]):
        started = perf_counter()
        sample = _run_case(runtime, case=case, index=index, default_scope=scope)
        sample["latency_ms"] = round((perf_counter() - started) * 1000.0, 3)
        latencies.append(float(sample["latency_ms"]))
        samples.append(sample)
    pass_rate = binary_pass_rate([bool(sample.get("passed")) for sample in samples])
    threshold = float(normalized["threshold"])
    verdict = "pass" if pass_rate >= threshold else "fail"
    return {
        "ok": True,
        "schema_version": "real_task_replay.v1",
        "report_type": "real_task_replay",
        "name": normalized["name"],
        "generated_at": now_iso(),
        "scope": asdict(scope),
        "seeded_record_ids": seeded_record_ids,
        "sample_count": len(samples),
        "pass_count": sum(1 for sample in samples if sample.get("passed")),
        "fail_count": sum(1 for sample in samples if not sample.get("passed")),
        "pass_rate": pass_rate,
        "threshold": threshold,
        "verdict": verdict,
        "latency_ms_avg": round(sum(latencies) / len(latencies), 3) if latencies else 0.0,
        "latency_ms_p95": percentile(latencies, 95),
        "failure_samples": [sample for sample in samples if not sample.get("passed")][:20],
        "samples": samples,
    }


def _report_record(report: dict[str, Any], *, scope: ScopeRef, release: Any = None) -> RecordEnvelope:
    release_payload = release_identity_payload(release) if release is not None else {}
    return RecordEnvelope.create(
        kind="replay_result",
        title=f"Real task replay report: {report['name']}",
        summary=f"Real task replay {report['verdict']} pass_rate={report['pass_rate']}",
        scope=scope,
        source="eimemory.real_task_replay",
        content={
            "report": dict(report),
            "evidence_class": "replay_execution",
            **release_payload,
        },
        meta={
            "report_type": "real_task_replay",
            "replay_source": "real_task_replay",
            "schema_version": "real_task_replay.v1",
            "name": report["name"],
            "verdict": report["verdict"],
            "pass_rate": report["pass_rate"],
            "threshold": report["threshold"],
            "sample_count": report["sample_count"],
            "pass_count": report["pass_count"],
            "fail_count": report["fail_count"],
            "scope": asdict(scope),
            "evidence_class": "replay_execution",
            **release_payload,
        },
    )


def _seed_records(runtime, seed_records: list[dict[str, Any]], *, scope: ScopeRef) -> list[str]:
    record_ids: list[str] = []
    for index, item in enumerate(seed_records):
        text = str(item.get("text") or item.get("summary") or "")
        if not text:
            continue
        record = runtime.memory.ingest(
            text=text,
            memory_type=str(item.get("memory_type") or item.get("type") or "fact"),
            title=str(item.get("title") or f"Real task replay seed {index + 1}"),
            scope=asdict(ScopeRef.from_dict(item.get("scope") or asdict(scope))),
            source=str(item.get("source") or "eimemory.real_task_replay.seed"),
            tags=[str(tag) for tag in list(item.get("tags") or [])],
            force_capture=bool(item.get("force_capture", True)),
            meta=dict(item.get("meta") or {}),
            content=dict(item.get("content") or {}),
        )
        if record.status == "active":
            record_ids.append(record.record_id)
    return record_ids


def _run_case(runtime, *, case: dict[str, Any], index: int, default_scope: ScopeRef) -> dict[str, Any]:
    case_id = str(case.get("case_id") or case.get("id") or index)
    query = str(case.get("query") or case.get("input") or case.get("prompt") or "")
    if not query:
        return {"index": index, "case_id": case_id, "passed": False, "failure_reason": "empty_query"}
    scope = ScopeRef.from_dict(case.get("scope") or asdict(default_scope))
    task_context = {
        "task_type": str(case.get("task_type") or case.get("source_system") or "real_task_replay"),
        "source_system": str(case.get("source_system") or ""),
        **dict(case.get("task_context") or {}),
    }
    bundle = runtime.memory.recall(query=query, scope=asdict(scope), task_context=task_context, limit=int(case.get("limit") or 5))
    returned_text = "\n".join(
        " ".join(
            str(value or "")
            for value in (
                item.title,
                item.summary,
                item.detail,
                item.content.get("text") if isinstance(item.content, dict) else "",
                item.content.get("summary") if isinstance(item.content, dict) else "",
            )
        )
        for item in bundle.items
    ).lower()
    expected_text = _strings(case.get("expected_text") or case.get("expect_any_text"))
    negative_text = _strings(case.get("negative_expected_text") or case.get("forbid_any_text"))
    expected_ok = not expected_text or any(term.lower() in returned_text for term in expected_text)
    negative_ok = not any(term.lower() in returned_text for term in negative_text)
    return {
        "index": index,
        "case_id": case_id,
        "source_system": str(case.get("source_system") or ""),
        "query": query,
        "scope": asdict(scope),
        "task_context": task_context,
        "expected_text": expected_text,
        "negative_expected_text": negative_text,
        "returned_record_ids": [item.record_id for item in bundle.items],
        "returned_titles": [item.title for item in bundle.items],
        "returned_count": len(bundle.items),
        "expected_ok": expected_ok,
        "negative_ok": negative_ok,
        "passed": bool(expected_ok and negative_ok),
        "failure_reason": "" if expected_ok and negative_ok else ("negative_text_hit" if not negative_ok else "expected_text_missing"),
    }


def _strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value] if value.strip() else []
    return [str(item).strip() for item in list(value or []) if str(item).strip()]


def _threshold(value: Any, *, default: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return max(0.0, min(1.0, parsed))
