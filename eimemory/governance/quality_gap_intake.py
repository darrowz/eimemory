from __future__ import annotations

from dataclasses import asdict
from hashlib import sha256
import json
from typing import Any, Mapping

from eimemory.core.clock import now_iso
from eimemory.models.records import RecordEnvelope, ScopeRef


QUALITY_GAP_SCHEMA = "eimemory.quality_gap.v1"
QUALITY_GAP_SOURCE = "eimemory.l5.quality_gap_intake"
_MAX_BLOCKING_METRICS = 32
_MAX_TEXT_CHARS = 240

_REPORT_CAPABILITIES = {
    "production_recall": "memory.recall",
    "recall_quality": "memory.recall",
    "memory_eval_ci": "memory.recall",
    "memory_quality": "memory.recall",
    "tool_routing": "tool.routing",
    "safety_replay": "safety.boundary",
    "channel_delivery": "channel.delivery",
}


def ingest_quality_gate_reports(
    runtime: Any,
    *,
    reports: Mapping[str, Mapping[str, Any] | None],
    scope: Mapping[str, Any] | ScopeRef | None,
) -> dict[str, Any]:
    """Turn machine quality-gate failures into deduplicated L5 learning gaps.

    This is deliberately an observation bridge, not a mutation engine.  It
    creates evidence that the existing autonomous-learning loop can consume;
    it never changes ACLs, runtime identity, release gates, or production
    policy directly.
    """

    scope_ref = scope if isinstance(scope, ScopeRef) else ScopeRef.from_dict(dict(scope or {}))
    created: list[str] = []
    deduplicated: list[str] = []
    resolved: list[str] = []
    ignored: list[str] = []
    findings: list[dict[str, Any]] = []

    for report_name, raw_report in reports.items():
        report = dict(raw_report or {}) if isinstance(raw_report, Mapping) else {}
        finding = _quality_finding(str(report_name), report)
        if finding is None:
            ignored.append(str(report_name))
            continue
        findings.append(finding)
        existing = _latest_gap(runtime, scope=scope_ref, semantic_key=finding["semantic_key"])

        if finding["gate_ok"]:
            if existing is not None and existing.status not in {"resolved", "closed"}:
                resolution = _resolution_record(finding, scope=scope_ref, resolves=existing)
                runtime.store.append(resolution)
                resolved.append(resolution.record_id)
            else:
                ignored.append(str(report_name))
            continue

        if existing is not None and existing.status not in {"resolved", "closed"}:
            if str(existing.meta.get("report_digest") or "") == finding["report_digest"]:
                deduplicated.append(existing.record_id)
                continue

        record = _gap_record(finding, scope=scope_ref, supersedes=existing)
        runtime.store.append(record)
        created.append(record.record_id)

    return {
        "ok": True,
        "report_type": "quality_gap_intake",
        "schema": QUALITY_GAP_SCHEMA,
        "created_count": len(created),
        "deduplicated_count": len(deduplicated),
        "resolved_count": len(resolved),
        "ignored_count": len(ignored),
        "created_record_ids": created,
        "deduplicated_record_ids": deduplicated,
        "resolved_record_ids": resolved,
        "ignored_reports": ignored,
        "findings": findings,
        "scope": asdict(scope_ref),
        "mutation_boundary": {
            "observation_records_only": True,
            "production_policy_changed": False,
            "acl_changed": False,
            "release_gate_changed": False,
        },
    }


def _quality_finding(report_name: str, report: dict[str, Any]) -> dict[str, Any] | None:
    gate = _gate(report)
    if not gate:
        return None
    gate_ok = gate.get("ok") is True
    raw_blocking = gate.get("blocking_metrics")
    blocking: Mapping[str, Any] = raw_blocking if isinstance(raw_blocking, Mapping) else {}
    normalized_blocking = {
        _bounded_text(key, 80): _normalize_metric(value)
        for key, value in list(sorted(blocking.items(), key=lambda item: str(item[0])))[:_MAX_BLOCKING_METRICS]
        if _bounded_text(key, 80)
    }
    blocked_reason = _bounded_text(
        gate.get("blocked_reason") or report.get("blocked_reason") or ("" if gate_ok else "quality_gate_failed"),
        _MAX_TEXT_CHARS,
    )
    capability = _REPORT_CAPABILITIES.get(report_name) or _bounded_text(
        report.get("target_capability") or report.get("capability") or "",
        120,
    )
    if not capability:
        return None
    semantic_key = f"quality-gap:{report_name}:{capability}"
    digest_payload = {
        "report_name": report_name,
        "capability": capability,
        "gate_ok": gate_ok,
        "blocked_reason": blocked_reason,
        "blocking_metrics": normalized_blocking,
    }
    return {
        **digest_payload,
        "semantic_key": semantic_key,
        "report_digest": _digest(digest_payload),
        "source_report_type": _bounded_text(report.get("report_type") or report_name, 120),
        "source_report_record_id": _bounded_text(report.get("persisted_record_id") or report.get("record_id") or "", 160),
        "source_sample_count": _bounded_int(report.get("sample_count"), minimum=0, maximum=1_000_000),
    }


def _gate(report: dict[str, Any]) -> dict[str, Any]:
    for key in ("quality_gate", "threshold_gate", "safety_gate", "isolation_gate", "gate"):
        value = report.get(key)
        if isinstance(value, Mapping):
            return dict(value)
    if "gate_ok" in report or "passed_threshold" in report:
        return {
            "ok": bool(report.get("gate_ok") or report.get("passed_threshold")),
            "blocked_reason": report.get("blocked_reason"),
            "blocking_metrics": report.get("blocking_metrics") or {},
        }
    return {}


def _gap_record(
    finding: dict[str, Any],
    *,
    scope: ScopeRef,
    supersedes: RecordEnvelope | None,
) -> RecordEnvelope:
    capability = str(finding["capability"])
    metric_names = list(finding["blocking_metrics"])
    summary = (
        f"{finding['report_name']} quality gate failed for {capability}: "
        + (", ".join(metric_names) if metric_names else str(finding["blocked_reason"]))
    )
    content = {
        "schema": QUALITY_GAP_SCHEMA,
        "semantic_key": finding["semantic_key"],
        "target_capability": capability,
        "miss": summary,
        "fix": "Generate a bounded candidate, replay it offline, and require safety/isolation/quality gates before shadow observation.",
        "success_criteria": {
            "source_quality_gate_passes": True,
            "cross_scope_leakage_count": 0,
            "no_acl_or_identity_relaxation": True,
            "promotion_requires_replay_and_shadow_evidence": True,
        },
        "blocking_metrics": finding["blocking_metrics"],
        "source_report": {
            "name": finding["report_name"],
            "type": finding["source_report_type"],
            "record_id": finding["source_report_record_id"],
            "digest": finding["report_digest"],
            "sample_count": finding["source_sample_count"],
        },
        "candidate_boundary": {
            "allowed": ["ranking", "query_rewrite", "budgets", "record_lane_weights", "reranking", "decay_thresholds"],
            "forbidden": ["tenant_acl", "user_acl", "channel_acl", "runtime_identity", "audit_deletion", "release_gate", "fail_closed"],
        },
    }
    if supersedes is not None:
        content["supersedes_gap_id"] = supersedes.record_id
    return RecordEnvelope.create(
        kind="reflection",
        title=f"L5 quality gap: {finding['report_name']}",
        summary=summary,
        detail=json.dumps(content, ensure_ascii=False, sort_keys=True),
        content=content,
        scope=scope,
        source=QUALITY_GAP_SOURCE,
        status="active",
        tags=["l5", "quality_gap", capability],
        meta={
            "schema": QUALITY_GAP_SCHEMA,
            "report_type": "quality_gap",
            "semantic_key": finding["semantic_key"],
            "report_digest": finding["report_digest"],
            "target_capability": capability,
            "capability": capability,
            "tag": capability,
            "miss": summary,
            "fix": content["fix"],
            "is_failure": True,
            "authority_tier": "L1",
            "supersedes_gap_id": supersedes.record_id if supersedes is not None else "",
        },
    )


def _resolution_record(
    finding: dict[str, Any],
    *,
    scope: ScopeRef,
    resolves: RecordEnvelope,
) -> RecordEnvelope:
    observed_at = now_iso()
    content = {
        "schema": QUALITY_GAP_SCHEMA,
        "semantic_key": finding["semantic_key"],
        "target_capability": finding["capability"],
        "resolves_gap_id": resolves.record_id,
        "resolution": {
            "status": "passed",
            "report_digest": finding["report_digest"],
            "observed_at": observed_at,
        },
    }
    return RecordEnvelope.create(
        kind="reflection",
        title=f"L5 quality gap resolved: {finding['report_name']}",
        summary=f"{finding['report_name']} quality gate passed for {finding['capability']}.",
        detail=json.dumps(content, ensure_ascii=False, sort_keys=True),
        content=content,
        scope=scope,
        source=QUALITY_GAP_SOURCE,
        status="resolved",
        tags=["l5", "quality_gap", "resolved", str(finding["capability"])],
        meta={
            "schema": QUALITY_GAP_SCHEMA,
            "report_type": "quality_gap_resolution",
            "semantic_key": finding["semantic_key"],
            "report_digest": finding["report_digest"],
            "target_capability": finding["capability"],
            "capability": finding["capability"],
            "is_failure": False,
            "resolved_at": observed_at,
            "resolves_gap_id": resolves.record_id,
        },
    )


def _latest_gap(runtime: Any, *, scope: ScopeRef, semantic_key: str) -> RecordEnvelope | None:
    records = [
        record
        for record in runtime.store.list_records(kinds=["reflection"], scope=scope, limit=500)
        if record.source == QUALITY_GAP_SOURCE
        and str(record.meta.get("semantic_key") or record.content.get("semantic_key") or "") == semantic_key
    ]
    if not records:
        return None
    superseded_ids = {
        str(reference)
        for record in records
        for reference in (
            record.content.get("supersedes_gap_id"),
            record.content.get("resolves_gap_id"),
        )
        if str(reference or "").strip()
    }
    tips = [record for record in records if record.record_id not in superseded_ids]
    return tips[0] if tips else records[0]


def _normalize_metric(value: Any) -> dict[str, Any]:
    raw = dict(value) if isinstance(value, Mapping) else {"actual": value}
    return {
        key: _bounded_scalar(raw.get(key))
        for key in ("actual", "threshold", "operator")
        if key in raw
    }


def _bounded_scalar(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return _bounded_text(value, 120)


def _bounded_text(value: Any, limit: int) -> str:
    return " ".join(str(value or "").split())[: max(0, int(limit))]


def _bounded_int(value: Any, *, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = minimum
    return max(minimum, min(maximum, parsed))


def _digest(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return sha256(payload.encode("utf-8")).hexdigest()
