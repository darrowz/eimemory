from __future__ import annotations

from dataclasses import asdict
from hashlib import sha256
import json
import re
from typing import Any, Mapping

from eimemory.core.clock import now_iso
from eimemory.models.records import RecordEnvelope, ScopeRef


QUALITY_GAP_SCHEMA = "eimemory.quality_gap.v1"
QUALITY_GAP_SOURCE = "eimemory.l5.quality_gap_intake"
_MAX_BLOCKING_METRICS = 32
_MAX_TEXT_CHARS = 240

# Compatibility mapping for historical report names, not a live capability catalog.
LEGACY_REPORT_CAPABILITIES = {
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
    ignored: list[str] = []

    pending_findings = verified_delivery_findings(runtime, scope=scope_ref)
    from eimemory.evaluation.semantic_relevance_monitor import monitor_channel_deliveries

    semantic_report, semantic_findings = monitor_channel_deliveries(runtime, scope=scope_ref)
    pending_findings.extend(semantic_findings)
    for report_name, raw_report in reports.items():
        # This detector's authority is the local delivery audit, not input JSON.
        if str(report_name).startswith(("recall_delivery:", "recall_semantic:")):
            ignored.append(str(report_name))
            continue
        report = dict(raw_report or {}) if isinstance(raw_report, Mapping) else {}
        finding = _quality_finding(str(report_name), report)
        if finding is None:
            ignored.append(str(report_name))
            continue
        pending_findings.append(finding)

    intake = _ingest_verified_findings(runtime, findings=pending_findings, scope=scope_ref)
    created = intake["created_record_ids"]
    deduplicated = intake["deduplicated_record_ids"]
    resolved = intake["resolved_record_ids"]
    ignored.extend(intake["ignored_reports"])
    findings = pending_findings

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
        "semantic_relevance": semantic_report,
        "scope": asdict(scope_ref),
        "mutation_boundary": {
            "observation_records_only": True,
            "production_policy_changed": False,
            "acl_changed": False,
            "release_gate_changed": False,
        },
    }


def _ingest_verified_findings(runtime: Any, *, findings: list[dict[str, Any]], scope: ScopeRef) -> dict[str, Any]:
    """Persist internal detector output; not an input/authorization API.

    Call only with freshly verified findings (including revalidated cached
    observations). Their exact owner is assigned by the detector, never the LLM.
    """
    created, deduplicated, resolved, ignored = [], [], [], []
    for finding in findings:
        report_name = finding["report_name"]
        # Only local detectors supply observation provenance. External report
        # JSON is normalized by _quality_finding and cannot set this scope.
        owner = finding.get("observation", {}).get("scope")
        scope_ref = ScopeRef.from_dict(owner) if owner is not None else scope
        existing = _latest_gap(runtime, scope=scope_ref, semantic_key=finding["semantic_key"])

        if finding["gate_ok"]:
            if existing is not None and existing.status not in {"resolved", "closed"}:
                resolution = _resolution_record(
                    finding,
                    scope=scope_ref,
                    resolves=existing,
                    release=_current_release_stamp(runtime, scope_ref),
                )
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

    return dict(created_record_ids=created, deduplicated_record_ids=deduplicated,
                resolved_record_ids=resolved, ignored_reports=ignored)


def verified_delivery_findings(runtime: Any, *, scope: ScopeRef) -> list[dict[str, Any]]:
    """Detect duplicate delivery from the runtime audit, never a model verdict.

    This establishes redundant delivery only, not semantic off-topic relevance.
    The audit is historical delivery authority; the receipt and physical record
    must still resolve. Missing evidence remains diagnostic, never a passing gate.
    """
    from eimemory.adapters.runtime.channel import runtime_channel_from_scope
    from eimemory.governance.release.evidence_contract import (
        deployment_receipt_for_scope, verified_deployment_receipt_identity,
        release_identity_payload,
    )

    channel = runtime_channel_from_scope(scope) or "openclaw"
    if not callable(getattr(runtime.store, "locked", None)):
        return []
    # ponytail: bounded by existing 512-decision audit retention, no new queue.
    with runtime.store.locked() as sqlite:
        rows = sqlite.execute(
            "SELECT decision_id FROM proactive_decisions WHERE channel=? AND tenant_id=? "
            "AND agent_id=? AND workspace_id=? AND user_id=? AND release_bound=1 "
            "AND control_cohort=0 AND acceptance_generated=0 ORDER BY created_at DESC LIMIT 512",
            (channel, scope.tenant_id, scope.agent_id, scope.workspace_id, scope.user_id),
        ).fetchall()
        decisions = [sqlite.load_proactive_decision(row["decision_id"]) for row in rows]
    findings = []
    for decision in decisions:
        query_digest = decision["query_digest"]
        sources = decision["source_ids"]
        if (not re.fullmatch(r"[0-9a-f]{64}", query_digest)
                or query_digest == sha256(b"").hexdigest()
                or not decision["task_type"] or len(sources) != 1 or sources[0] in {"", "*"}):
            continue
        release = decision["release_identity"]
        receipt = deployment_receipt_for_scope(runtime, release["deployment_receipt_id"], scope)
        identity = verified_deployment_receipt_identity(receipt)
        if identity is None or release_identity_payload(identity) != release:
            continue
        delivered = [item for item in decision["items"] if item["ever_injected"]]
        refs = [item["record_id"] for item in delivered]
        if not refs or len(refs) == len(set(refs)):
            continue
        if any(item["source_id"] != sources[0] or not item["record_id"]
               or not re.fullmatch(r"[0-9a-f]{64}", item["render_digest"]) for item in delivered):
            continue
        if any(runtime.store.get_by_exact_ref(ref, scope=scope, source_id=sources[0]) is None
               for ref in set(refs)):
            continue
        identity_payload = {"scope": asdict(scope), "source_id": sources[0],
                            "query_digest": query_digest, "release_identity": release}
        finding = _quality_finding("recall_delivery:" + _digest(identity_payload), {
            "target_capability": "memory.recall", "report_type": "proactive_delivery_audit",
            "record_id": decision["decision_id"], "sample_count": 1,
            "quality_gate": {"ok": False, "blocked_reason": "verified_duplicate_delivery",
                             "blocking_metrics": {"duplicate_delivered_record":
                                                  {"actual": 1, "threshold": 0, "operator": "=="}}},
        })
        finding["observation"] = {**identity_payload, "decision_id": decision["decision_id"],
                                  "record_ids": sorted(set(refs)), "severity": "severe",
                                  "evidence_kind": "duplicate_delivered_record"}
        findings.append(finding)
    return findings


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
    capability = LEGACY_REPORT_CAPABILITIES.get(report_name) or _bounded_text(
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
    if "observation" in finding:
        content["source_report"]["observation"] = finding["observation"]
    return RecordEnvelope.create(
        kind="reflection",
        title=f"L5 quality gap: {finding['report_name']}",
        summary=summary,
        detail=json.dumps(content, ensure_ascii=False, sort_keys=True),
        content=content,
        scope=scope,
        source=QUALITY_GAP_SOURCE,
        source_id=finding.get("observation", {}).get("source_id", "default"),
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


def _current_release_stamp(runtime: Any, scope: ScopeRef) -> dict[str, str]:
    """Bind a passing gate to the release the process can actually verify."""

    try:
        from eimemory.governance.release.evidence_contract import current_release_identity

        identity = current_release_identity(runtime, scope)
    except (AttributeError, TypeError, ValueError, RuntimeError):
        return {}
    if identity is None or not identity.complete:
        return {}
    return {
        "release_commit": identity.commit,
        "deployment_receipt_id": identity.receipt_id,
    }


def _resolution_record(
    finding: dict[str, Any],
    *,
    scope: ScopeRef,
    resolves: RecordEnvelope,
    release: Mapping[str, str] | None = None,
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
            "release_commit": str((release or {}).get("release_commit") or ""),
            "deployment_receipt_id": str((release or {}).get("deployment_receipt_id") or ""),
        },
    )


def _latest_gap(runtime: Any, *, scope: ScopeRef, semantic_key: str) -> RecordEnvelope | None:
    records = [
        record
        for record in runtime.store.list_records_by_meta_value(
            kinds=["reflection"], scope=scope, meta_key="semantic_key", meta_value=semantic_key, limit=500
        )
        if record.source == QUALITY_GAP_SOURCE
        and record.scope == scope
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
