"""Real task replay runner for OpenClaw, UUMit, and eimemory history cases."""

from __future__ import annotations

from dataclasses import asdict
from hashlib import sha256
import json
from pathlib import Path
import tempfile
from time import perf_counter
from typing import Any

from eimemory.adapters.runtime.service import AgentRuntimeMemoryService
from eimemory.core.clock import now_iso
from eimemory.core.ids import generate_record_id
from eimemory.evaluation.metrics import binary_pass_rate, percentile
from eimemory.experience.outcome import (
    OutcomeTraceBuildError,
    build_outcome_trace_record,
)
from eimemory.governance.capability_dashboard import _valid_runtime_task_evidence
from eimemory.governance.evidence_contract import (
    current_release_identity,
    release_identity_from_record,
    release_identity_payload,
)
from eimemory.models.records import RecordEnvelope, ScopeRef
from eimemory.runtime_identity import runtime_package_tree_digest


REAL_PROVENANCE_CONTRACT = "verified_real_replay.v1"
TRUSTED_REAL_TASK_SOURCES = frozenset(
    {
        "openclaw.agent_end",
        "openclaw.task_end",
        "codex.stop",
        "hermes.task_end",
    }
)


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
    catalog: Any | None = None,
    legacy_compatibility: bool = False,
) -> dict[str, Any]:
    normalized = normalize_real_task_replay_dataset(dataset)
    if seed and normalized["seed"]:
        with tempfile.TemporaryDirectory(prefix="eimemory-real-task-replay-") as temp_root:
            from eimemory.api.runtime import Runtime

            eval_runtime = Runtime.create(root=Path(temp_root))
            try:
                report = _run_on_runtime(
                    eval_runtime,
                    normalized=normalized,
                    catalog=catalog,
                    legacy_compatibility=legacy_compatibility,
                )
            finally:
                eval_runtime.close()
    else:
        report = _run_on_runtime(
            runtime,
            normalized=normalized if seed else {**normalized, "seed": []},
            catalog=catalog,
            legacy_compatibility=legacy_compatibility,
        )
    if persist_report:
        report_scope = ScopeRef.from_dict(normalized["scope"])
        release = current_release_identity(runtime, report_scope)
        record = runtime.store.append(_report_record(report, scope=report_scope, release=release))
        report = {**report, "persisted_record_id": record.record_id}
    return report


def _run_on_runtime(
    runtime,
    *,
    normalized: dict[str, Any],
    catalog: Any | None = None,
    legacy_compatibility: bool = False,
) -> dict[str, Any]:
    scope = ScopeRef.from_dict(normalized["scope"])
    seeded_record_ids = _seed_records(runtime, normalized["seed"], scope=scope)
    samples: list[dict[str, Any]] = []
    latencies: list[float] = []
    seen_terminal_evidence: set[str] = set()
    execution_id = generate_record_id("replay_result")
    for index, case in enumerate(normalized["cases"]):
        started = perf_counter()
        sample = _run_case(
            runtime,
            case=case,
            index=index,
            default_scope=scope,
            seen_terminal_evidence=seen_terminal_evidence,
            execution_id=execution_id,
            catalog=catalog,
            legacy_compatibility=legacy_compatibility,
        )
        sample["latency_ms"] = round((perf_counter() - started) * 1000.0, 3)
        latencies.append(float(sample["latency_ms"]))
        samples.append(sample)
    pass_rate = binary_pass_rate([bool(sample.get("passed")) for sample in samples])
    threshold = float(normalized["threshold"])
    verdict = "pass" if pass_rate >= threshold else "fail"
    verified_samples = [
        sample
        for sample in samples
        if sample.get("real_provenance_ok") is True and sample.get("executed") is True
    ]
    return {
        "ok": True,
        "schema_version": "real_task_replay.v1",
        "report_type": "real_task_replay",
        "real_provenance_contract": REAL_PROVENANCE_CONTRACT,
        "package_tree_digest": runtime_package_tree_digest(),
        "replay_execution_id": execution_id,
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
        "verified_real_sample_count": len(verified_samples),
        "verified_real_task_types": len(
            {
                str(sample.get("source_task_type") or "")
                for sample in verified_samples
                if str(sample.get("source_task_type") or "")
            }
        ),
        "failure_samples": [sample for sample in samples if not sample.get("passed")][:20],
        "samples": samples,
    }


def _report_record(report: dict[str, Any], *, scope: ScopeRef, release: Any = None) -> RecordEnvelope:
    release_payload = release_identity_payload(release) if release is not None else {}
    persisted_report = _persistable_report(report)
    return RecordEnvelope.create(
        kind="replay_result",
        title="Real task replay report",
        summary=f"Real task replay {report['verdict']} pass_rate={report['pass_rate']}",
        scope=scope,
        source="eimemory.real_task_replay",
        content={
            "report": persisted_report,
            "evidence_class": "replay_execution",
            **release_payload,
        },
        meta={
            "report_type": "real_task_replay",
            "replay_source": "real_task_replay",
            "schema_version": "real_task_replay.v1",
            "name_digest": _stable_digest(str(report.get("name") or "")),
            "verdict": report["verdict"],
            "pass_rate": report["pass_rate"],
            "threshold": report["threshold"],
            "sample_count": report["sample_count"],
            "pass_count": report["pass_count"],
            "fail_count": report["fail_count"],
            "real_provenance_contract": str(report.get("real_provenance_contract") or ""),
            "package_tree_digest": str(report.get("package_tree_digest") or ""),
            "verified_real_sample_count": int(report.get("verified_real_sample_count") or 0),
            "verified_real_task_types": int(report.get("verified_real_task_types") or 0),
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
            source_id=item["source_id"] if "source_id" in item else "default",
            tags=[str(tag) for tag in list(item.get("tags") or [])],
            force_capture=bool(item.get("force_capture", True)),
            meta=dict(item.get("meta") or {}),
            content=dict(item.get("content") or {}),
        )
        if record.status == "active":
            record_ids.append(record.record_id)
    return record_ids


def _run_case(
    runtime,
    *,
    case: dict[str, Any],
    index: int,
    default_scope: ScopeRef,
    seen_terminal_evidence: set[str],
    execution_id: str,
    catalog: Any | None = None,
    legacy_compatibility: bool = False,
) -> dict[str, Any]:
    case_id = str(case.get("case_id") or case.get("id") or index)
    query = str(case.get("query") or case.get("input") or case.get("prompt") or "")
    requested_scope = ScopeRef.from_dict(case.get("scope") or asdict(default_scope))
    scope_mismatch = asdict(requested_scope) != asdict(default_scope)
    scope = default_scope
    provenance = (
        _invalid_provenance(
            str(case.get("source_record_id") or ""),
            "case_scope_mismatch",
        )
        if scope_mismatch
        else validate_real_replay_source(
            runtime,
            source_record_id=str(case.get("source_record_id") or ""),
            scope=scope,
            catalog=catalog,
            legacy_compatibility=legacy_compatibility,
        )
    )
    terminal_evidence_digest = str(
        provenance.get("terminal_evidence_digest") or ""
    )
    if provenance["ok"] and terminal_evidence_digest in seen_terminal_evidence:
        provenance = {
            **provenance,
            "ok": False,
            "reason": "duplicate_terminal_evidence",
        }
    elif provenance["ok"]:
        seen_terminal_evidence.add(terminal_evidence_digest)
    provenance_fields = {
        "source_record_id": str(provenance.get("source_record_id") or case.get("source_record_id") or ""),
        "source_evidence_digest": str(provenance.get("source_evidence_digest") or ""),
        "source_task_type": str(provenance.get("task_type") or ""),
        "terminal_evidence_digest": terminal_evidence_digest,
        "real_provenance_ok": provenance.get("ok") is True,
        "real_provenance_reason": str(provenance.get("reason") or ""),
        "replay_execution_id": execution_id,
        "package_tree_digest": runtime_package_tree_digest(),
    }
    if scope_mismatch:
        return {
            "index": index,
            "case_id": case_id,
            "executed": False,
            "passed": False,
            "failure_reason": "case_scope_mismatch",
            **provenance_fields,
        }
    if not query:
        return {
            "index": index,
            "case_id": case_id,
            "executed": False,
            "passed": False,
            "failure_reason": "empty_query",
            **provenance_fields,
        }
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
        "executed": True,
        "expected_ok": expected_ok,
        "negative_ok": negative_ok,
        "passed": bool(expected_ok and negative_ok),
        "failure_reason": "" if expected_ok and negative_ok else ("negative_text_hit" if not negative_ok else "expected_text_missing"),
        **provenance_fields,
    }


def validate_real_replay_source(
    runtime,
    *,
    source_record_id: str,
    scope: ScopeRef | dict[str, Any],
    catalog: Any | None = None,
    legacy_compatibility: bool = False,
) -> dict[str, Any]:
    scope_ref = scope if isinstance(scope, ScopeRef) else ScopeRef.from_dict(scope)
    source_record_id = str(source_record_id or "").strip()
    if not source_record_id:
        return _invalid_provenance("", "source_record_id_missing")
    record = runtime.store.get_by_id(source_record_id, scope=scope_ref)
    if record is None:
        return _invalid_provenance(source_record_id, "source_record_unavailable_in_scope")
    if record.status != "active":
        return _invalid_provenance(source_record_id, "inactive_source_record")
    if record.kind != "reflection" or record.source != "eimemory.experience.outcome_trace":
        return _invalid_provenance(source_record_id, "untrusted_source_record")
    content = record.content if isinstance(record.content, dict) else {}
    payload = content.get("payload") if isinstance(content.get("payload"), dict) else {}
    meta = record.meta if isinstance(record.meta, dict) else {}
    if (
        str(content.get("schema_version") or meta.get("schema_version") or "") != "outcome_trace.v1"
        or str(meta.get("report_type") or "") != "outcome_trace"
    ):
        return _invalid_provenance(source_record_id, "invalid_outcome_trace_contract")
    try:
        rebuilt = build_outcome_trace_record(
            payload,
            scope=scope_ref,
            catalog=catalog,
            legacy_compatibility=legacy_compatibility,
        )
    except OutcomeTraceBuildError as exc:
        return _invalid_provenance(
            source_record_id,
            _outcome_trace_build_failure_reason(exc),
        )
    trace_id = str(payload.get("trace_id") or "")
    idempotency_key = str(payload.get("idempotency_key") or "")
    business_meta = meta.get("business_meta")
    if (
        rebuilt.record.record_id != record.record_id
        or rebuilt.record.content != record.content
        or rebuilt.record.provenance != record.provenance
        or record.time.created_at != str(payload.get("recorded_at") or "")
        or str(meta.get("trace_id") or "") != trace_id
        or str(meta.get("idempotency_key") or "") != idempotency_key
        or str(meta.get("task_type") or "") != str(payload.get("task_type") or "")
        or str(record.provenance.get("report_type") or "") != "outcome_trace"
        or str(record.provenance.get("schema_version") or "") != "outcome_trace.v1"
        or str(record.provenance.get("trace_id") or "") != trace_id
        or str(record.provenance.get("idempotency_key") or "") != idempotency_key
        or not isinstance(business_meta, dict)
        or str(business_meta.get("report_type") or "") != "outcome_trace"
        or str(business_meta.get("schema_version") or "") != "outcome_trace.v1"
        or str(business_meta.get("trace_id") or "") != trace_id
        or str(business_meta.get("idempotency_key") or "") != idempotency_key
        or str(business_meta.get("task_type") or "") != str(payload.get("task_type") or "")
    ):
        return _invalid_provenance(source_record_id, "outcome_trace_identity_mismatch")
    terminal_source = str(payload.get("source") or "").strip()
    if terminal_source not in TRUSTED_REAL_TASK_SOURCES:
        return _invalid_provenance(source_record_id, "untrusted_terminal_source")
    outcome = payload.get("outcome") if isinstance(payload.get("outcome"), dict) else {}
    if outcome.get("rehearsal") is not False:
        return _invalid_provenance(source_record_id, "rehearsal_source")
    if outcome.get("success") is not True or str(outcome.get("status") or "").lower() not in {
        "success",
        "good",
        "succeeded",
        "completed",
        "ok",
        "passed",
    }:
        return _invalid_provenance(source_record_id, "unsuccessful_source")
    evidence_class = str(payload.get("evidence_class") or meta.get("evidence_class") or "")
    if evidence_class != "verified_real_task":
        return _invalid_provenance(source_record_id, "unverified_evidence_class")
    task_type = _normalized_task_type(payload.get("task_type") or meta.get("task_type"))
    if not task_type:
        return _invalid_provenance(source_record_id, "task_type_missing")
    verifier = payload.get("verifier") if isinstance(payload.get("verifier"), dict) else {}
    evidence_refs = verifier.get("evidence_refs")
    if not (
        verifier.get("passed") is True
        and str(verifier.get("method") or "") == terminal_source
        and isinstance(evidence_refs, list)
        and len(evidence_refs) == 1
        and bool(str(evidence_refs[0] or "").strip())
    ):
        return _invalid_provenance(source_record_id, "terminal_verifier_invalid")
    evidence_ref = str(evidence_refs[0]).strip()
    if not _valid_runtime_task_evidence(
        runtime,
        scope=scope_ref,
        evidence_ref=evidence_ref,
        method=terminal_source,
        trace_id=trace_id,
        session_id=str(payload.get("session_id") or ""),
        task_type=task_type,
        success=True,
        release=release_identity_from_record(record),
    ):
        return _invalid_provenance(source_record_id, "terminal_evidence_invalid")
    terminal_contract_digest = str(
        payload.get("terminal_contract_digest") or ""
    ).strip()
    if not terminal_contract_digest:
        return _invalid_provenance(
            source_record_id,
            "terminal_contract_digest_missing",
        )
    if not _terminal_contract_chain_valid(
        runtime,
        scope=scope_ref,
        evidence_ref=evidence_ref,
        trace_digest=terminal_contract_digest,
        trace_payload=payload,
    ):
        return _invalid_provenance(source_record_id, "terminal_contract_digest_mismatch")
    terminal_evidence_digest = _stable_digest(
        json.dumps(
            {
                "scope": asdict(scope_ref),
                "terminal_contract_digest": terminal_contract_digest,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    safe_projection = {
        "record_id": record.record_id,
        "scope": asdict(record.scope),
        "source": record.source,
        "terminal_source": terminal_source,
        "schema_version": "outcome_trace.v1",
        "task_type": task_type,
        "outcome_status": str(outcome.get("status") or "").lower(),
        "success": True,
        "rehearsal": False,
        "evidence_class": evidence_class,
        "terminal_contract_digest": terminal_contract_digest,
        "created_at": record.time.created_at,
    }
    digest = sha256(
        json.dumps(
            safe_projection,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    return {
        "ok": True,
        "source_record_id": record.record_id,
        "source_evidence_digest": digest,
        "terminal_evidence_digest": terminal_evidence_digest,
        "task_type": task_type,
        "reason": "",
    }


def _invalid_provenance(source_record_id: str, reason: str) -> dict[str, Any]:
    return {
        "ok": False,
        "source_record_id": source_record_id,
        "source_evidence_digest": "",
        "terminal_evidence_digest": "",
        "task_type": "",
        "reason": reason,
    }


def _outcome_trace_build_failure_reason(error: OutcomeTraceBuildError) -> str:
    """Keep dynamic-contract catalog absence observable and fail closed."""
    if "trusted capability catalog is required" in str(error):
        return "dynamic_capability_catalog_required"
    return "invalid_outcome_trace_payload"


def _normalized_task_type(value: Any) -> str:
    return ".".join(part for part in str(value or "").strip().lower().replace("_", ".").split(".") if part)


def _persistable_report(report: dict[str, Any]) -> dict[str, Any]:
    redacted_samples = []
    allowed = {
        "index",
        "executed",
        "passed",
        "failure_reason",
        "latency_ms",
        "source_record_id",
        "source_evidence_digest",
        "source_task_type",
        "terminal_evidence_digest",
        "real_provenance_ok",
        "real_provenance_reason",
        "replay_execution_id",
        "package_tree_digest",
    }
    for sample in list(report.get("samples") or []):
        if isinstance(sample, dict):
            redacted = {key: sample.get(key) for key in allowed if key in sample}
            if str(sample.get("case_id") or ""):
                redacted["case_id_digest"] = _stable_digest(str(sample["case_id"]))
            redacted_samples.append(redacted)
    persisted = {
        key: value
        for key, value in report.items()
        if key not in {"name", "samples", "failure_samples", "seeded_record_ids"}
    }
    persisted["name_digest"] = _stable_digest(str(report.get("name") or ""))
    persisted["samples"] = redacted_samples
    persisted["failure_samples"] = [
        sample for sample in redacted_samples if sample.get("passed") is not True
    ][:20]
    persisted["seeded_record_count"] = len(list(report.get("seeded_record_ids") or []))
    return persisted


def _terminal_contract_chain_valid(
    runtime,
    *,
    scope: ScopeRef,
    evidence_ref: str,
    trace_digest: str,
    trace_payload: dict[str, Any],
) -> bool:
    if not trace_digest:
        return False
    sqlite = getattr(getattr(runtime, "store", None), "sqlite", None)
    conn = getattr(sqlite, "conn", None)
    if conn is None:
        return False
    event_row = conn.execute(
        """SELECT payload_json FROM events
           WHERE id=? AND tenant_id=? AND agent_id=? AND workspace_id=? AND user_id=?
           LIMIT 1""",
        (
            evidence_ref,
            scope.tenant_id,
            scope.agent_id,
            scope.workspace_id,
            scope.user_id,
        ),
    ).fetchone()
    outcome_row = conn.execute(
        """SELECT payload_json FROM event_outcomes
           WHERE event_id=? AND tenant_id=? AND agent_id=? AND workspace_id=? AND user_id=?
           ORDER BY recorded_at DESC LIMIT 1""",
        (
            evidence_ref,
            scope.tenant_id,
            scope.agent_id,
            scope.workspace_id,
            scope.user_id,
        ),
    ).fetchone()
    if event_row is None or outcome_row is None:
        return False
    try:
        event = json.loads(str(event_row["payload_json"] or "{}"))
        outcome = json.loads(str(outcome_row["payload_json"] or "{}"))
    except (TypeError, json.JSONDecodeError):
        return False
    method = str(trace_payload.get("source") or "")
    channel, _, end_kind = method.partition(".")
    trace_outcome = (
        trace_payload.get("outcome")
        if isinstance(trace_payload.get("outcome"), dict)
        else {}
    )
    event_receipts = event.get("verification_receipts")
    receipt_ids = [
        str(receipt.get("receipt_id") or "")
        for receipt in event_receipts
        if isinstance(receipt, dict) and str(receipt.get("receipt_id") or "")
    ] if isinstance(event_receipts, list) else []
    recomputed_digest = AgentRuntimeMemoryService._terminal_contract_digest(
        {
            "channel": channel,
            "scope": asdict(scope),
            "end_kind": end_kind,
            "session_id": str(event.get("session_id") or ""),
            "event_id": str(event.get("run_id") or ""),
            "task_type": str(event.get("outcome_trace_task_type") or ""),
            "success": trace_outcome.get("success"),
            "rehearsal": bool(trace_outcome.get("rehearsal")),
            "verification": str(event.get("verification") or ""),
            "result": str(event.get("result") or ""),
            "receipt_ids": receipt_ids,
        }
    )
    return recomputed_digest == trace_digest and {
        trace_digest,
        str(event.get("terminal_contract_digest") or ""),
        str(outcome.get("terminal_contract_digest") or ""),
    } == {trace_digest}


def _stable_digest(value: str) -> str:
    return sha256(str(value or "").encode("utf-8")).hexdigest()


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
