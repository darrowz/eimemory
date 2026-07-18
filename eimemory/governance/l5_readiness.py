from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from typing import Any

from eimemory.core.clock import now_iso
from eimemory.governance.capability_attribution import collect_capability_evidence
from eimemory.governance.capability_ledger import build_capability_ledger
from eimemory.governance.capability_replay_executor import validate_capability_replay_result
from eimemory.governance.capability_replay_packs import (
    MANIFEST_REPORT_TYPE,
    MANIFEST_SCHEMA_VERSION,
    capability_replay_case_ids,
    capability_replay_log_sequence_state,
    capability_replay_manifest_digest,
    capability_replay_member_digest,
)
from eimemory.governance.evidence_contract import (
    EvidenceRequirement,
    ReleaseIdentity,
    current_release_identity,
    resolve_evidence,
)
from eimemory.governance.learning_state import append_learning_record_once, stable_semantic_key
from eimemory.governance.rollout_lifecycle import is_executed_rollback_ledger_record
from eimemory.models.records import ScopeRef


READINESS_CAPABILITIES = [
    "memory.recall",
    "tool.routing",
    "knowledge.intake",
    "proactive.judgment",
    "search.discovery",
    "research.synthesis",
    "operations.uumit",
    "device.control",
    "safety.boundary",
]

STRONG_CAPABILITIES = {"memory.recall", "tool.routing", "knowledge.intake", "safety.boundary"}
WEAK_CAPABILITIES = {"search.discovery", "research.synthesis", "operations.uumit", "device.control"}


def readiness_gate_status(readiness: dict[str, Any]) -> str:
    """Return the only release-gate states backed by complete L5 evidence."""

    assessment = (
        readiness.get("latest_l5_assessment")
        if isinstance(readiness.get("latest_l5_assessment"), dict)
        else {}
    )
    live_gate = readiness.get("live_task_gate") if isinstance(readiness.get("live_task_gate"), dict) else {}
    replay = readiness.get("verified_replay") if isinstance(readiness.get("verified_replay"), dict) else {}
    common_verified = bool(
        readiness.get("ok") is True
        and assessment.get("trusted") is True
        and assessment.get("complete") is True
        and assessment.get("level") == "L5"
        and int(replay.get("executed_count") or 0) >= 10
        and not list(replay.get("weak_capabilities_missing") or [])
        and not dict(replay.get("manifest_rejection_reasons") or {})
    )
    if not common_verified:
        return ""
    score = readiness.get("readiness_score")
    if (
        readiness.get("current_stage") == "L5"
        and isinstance(score, (int, float))
        and not isinstance(score, bool)
        and float(score) == 1.0
        and live_gate.get("ok") is True
        and int(live_gate.get("current_deployment_verified_real_tasks") or 0) >= 10
    ):
        return "L5"
    if (
        readiness.get("current_stage") == "data_accumulating"
        and isinstance(score, (int, float))
        and not isinstance(score, bool)
        and float(score) == 0.9
        and live_gate.get("ok") is False
        and int(live_gate.get("current_deployment_operational_probes") or 0) >= 10
        and (
            int(live_gate.get("sample_deficit") or 0) > 0
            or int(live_gate.get("task_type_deficit") or 0) > 0
        )
    ):
        return "data_accumulating"
    return ""


def build_l5_readiness_report(
    runtime: Any,
    *,
    scope: dict[str, Any] | ScopeRef | None = None,
    persist: bool = False,
    limit: int = 500,
    loop_id: str = "l5_readiness",
) -> dict[str, Any]:
    """Build a read-only L5 readiness report from existing governance evidence."""

    scope_ref = scope if isinstance(scope, ScopeRef) else ScopeRef.from_dict(scope)
    release = current_release_identity(runtime, scope_ref, limit=limit)
    ledger = build_capability_ledger(runtime, scope=scope_ref, limit=limit, attribute_outcomes=False)
    hard_metrics = _safe_hard_metrics(runtime, scope=scope_ref, limit=limit)
    evidence_counts = _evidence_counts(runtime, scope=scope_ref, limit=limit)
    verified_replay = _verified_replay_summary(runtime, scope=scope_ref, limit=limit)
    latest_l5_assessment = _latest_l5_assessment(runtime, scope=scope_ref, release=release)
    weak_outcome_evidence = _weak_outcome_evidence(runtime, scope=scope_ref, limit=limit)
    capability_gaps = _capability_gaps(ledger, weak_outcome_evidence=weak_outcome_evidence)
    stage = _stage_for(
        ledger,
        hard_metrics,
        evidence_counts,
        capability_gaps,
        weak_outcome_evidence,
        verified_replay,
        latest_l5_assessment,
    )
    next_actions = _next_actions(
        stage,
        capability_gaps,
        evidence_counts,
        verified_replay=verified_replay,
        latest_l5_assessment=latest_l5_assessment,
    )
    report = {
        "ok": True,
        "report_type": "l5_readiness_report",
        "schema_version": "l5_readiness.v1",
        "generated_at": now_iso(),
        "scope": asdict(scope_ref),
        "current_stage": stage["stage"],
        "stage_label": stage["label"],
        "readiness_score": stage["readiness_score"],
        "stage_reason": stage["reason"],
        "done_when": stage["done_when"],
        "risk_boundary": stage["risk_boundary"],
        "evidence_counts": evidence_counts,
        "hard_metrics": hard_metrics.get("metrics", {}),
        "hard_metric_quality": hard_metrics.get("metric_quality", {}),
        "hard_metric_samples": hard_metrics.get("sample_counts", {}),
        "live_task_gate": stage["live_task_gate"],
        "verified_replay": verified_replay,
        "latest_l5_assessment": latest_l5_assessment,
        "weak_outcome_evidence": weak_outcome_evidence,
        "capability_gaps": capability_gaps,
        "next_actions": next_actions,
        "ledger": ledger,
        "persisted_record_id": "",
    }
    if persist:
        record = append_learning_record_once(
            runtime,
            kind="reflection",
            title="L5 readiness report",
            summary=f"{stage['stage']} readiness score {stage['readiness_score']}",
            scope=scope_ref,
            loop_id=loop_id,
            step_name="l5_readiness",
            semantic_key=stable_semantic_key("l5_readiness", scope_ref, stage["stage"], evidence_counts, capability_gaps),
            authority_tier="L0",
            status="active",
            content=report,
            meta={
                "report_type": "l5_readiness_report",
                "stage": stage["stage"],
                "readiness_score": stage["readiness_score"],
            },
            source="eimemory.l5_readiness",
        )
        report["persisted_record_id"] = record.record_id
    return report


def _safe_hard_metrics(runtime: Any, *, scope: ScopeRef, limit: int) -> dict[str, Any]:
    try:
        from eimemory.governance.capability_dashboard import build_capability_dashboard_metrics

        return build_capability_dashboard_metrics(runtime, scope=scope, persist=False, limit=limit)
    except Exception as exc:
        return {"ok": False, "error": type(exc).__name__, "detail": str(exc), "metrics": {}, "sample_counts": {}}


def _evidence_counts(runtime: Any, *, scope: ScopeRef, limit: int) -> dict[str, int]:
    kinds = [
        "memory",
        "learning_loop",
        "learning_goal",
        "learning_eval",
        "replay_result",
        "capability_candidate",
        "promotion_request",
        "capability_score",
        "rl_transition",
        "regression_watch",
        "l5_world_model",
        "l5_strategic_roadmap",
        "l5_self_continuity",
        "l5_assessment",
        "l5_closed_loop",
    ]
    counts: dict[str, int] = {}
    for kind in kinds:
        try:
            counts[kind] = len(runtime.store.list_records(kinds=[kind], scope=scope, limit=limit))
        except Exception:
            counts[kind] = 0
    counts["promotion_applied"] = _count_status(runtime, scope=scope, kind="promotion_request", statuses={"promoted", "active", "deployed"}, limit=limit)
    counts["rollback_or_quarantine"] = _policy_rollback_count(runtime, scope=scope, limit=limit)
    return counts


def _count_status(runtime: Any, *, scope: ScopeRef, kind: str, statuses: set[str], limit: int) -> int:
    try:
        records = runtime.store.list_records(kinds=[kind], scope=scope, limit=limit)
    except Exception:
        return 0
    return sum(1 for record in records if str(record.status or "").lower() in statuses)


def _policy_rollback_count(runtime: Any, *, scope: ScopeRef, limit: int) -> int:
    getter = getattr(runtime, "get_policy_rollout_ledger", None)
    if not callable(getter):
        return 0
    try:
        records = getter(scope=scope, limit=max(0, int(limit)))
    except Exception:
        return 0
    return sum(1 for record in records if isinstance(record, dict) and is_executed_rollback_ledger_record(record))


def _verified_replay_summary(runtime: Any, *, scope: ScopeRef, limit: int) -> dict[str, Any]:
    records = _capability_replay_records(runtime, scope=scope, limit=limit)
    selected_records, manifest_record_ids, manifest_rejection_reasons = _latest_manifest_case_records(
        runtime,
        scope=scope,
        limit=limit,
    )
    by_capability = {
        capability: {
            "executed_count": 0,
            "pass_count": 0,
            "fail_count": 0,
            "not_run_count": 0,
            "pass_rate": 0.0,
            "distinct_evidence_count": 0,
        }
        for capability in sorted(WEAK_CAPABILITIES)
    }
    evidence_sources = {capability: set() for capability in WEAK_CAPABILITIES}
    pass_count = 0
    fail_count = 0
    not_run_count = 0
    observed_executed_count = sum(
        1
        for record in records
        if str(record.get("source", "") if isinstance(record, dict) else getattr(record, "source", "") or "").strip()
        == "eimemory.capability_replay"
        and str(_record_field(record, "report_type") or "").strip() == "capability_replay_pack"
        and str(_record_field(record, "verdict") or "").strip().lower() in {"pass", "fail"}
    )
    rejection_reasons: dict[str, int] = {}
    for record in selected_records:
        content = record.get("content") if isinstance(record, dict) else getattr(record, "content", None)
        content = content if isinstance(content, dict) else {}
        persisted_result = content.get("result") if isinstance(content.get("result"), dict) else {}
        case_payload = content.get("case") if isinstance(content.get("case"), dict) else {}
        verdict = str(persisted_result.get("verdict") or _record_field(record, "verdict") or "").strip().lower()
        capability = str(_record_field(record, "capability") or _record_field(record, "target_capability") or "").strip()
        case_id = str(case_payload.get("case_id") or _record_field(record, "case_id") or "").strip()
        report_type = str(_record_field(record, "report_type") or "").strip()
        source = str(record.get("source", "") if isinstance(record, dict) else getattr(record, "source", "") or "").strip()
        hit = persisted_result.get("hit") if "hit" in persisted_result else _record_field(record, "hit")
        trusted_replay = report_type == "capability_replay_pack" and source == "eimemory.capability_replay"
        if not trusted_replay:
            continue
        bucket = by_capability.get(capability)
        if bucket is None:
            continue
        evidence_source_id = str(persisted_result.get("evidence_source_id") or "").strip()
        if verdict == "pass":
            validation = validate_capability_replay_result(
                runtime,
                scope=scope,
                capability=capability,
                case_id=case_id,
                result=persisted_result,
            )
            if validation.get("ok") is not True:
                verdict = "fail"
                reason = str(validation.get("reason") or "invalid_contract_replay_result")
                rejection_reasons[reason] = rejection_reasons.get(reason, 0) + 1
        if verdict == "pass":
            pass_count += 1
            bucket["executed_count"] += 1
            bucket["pass_count"] += 1
            evidence_sources[capability].add(evidence_source_id)
        elif verdict == "fail":
            fail_count += 1
            bucket["executed_count"] += 1
            bucket["fail_count"] += 1
        elif verdict == "not_run":
            not_run_count += 1
            bucket["not_run_count"] += 1
    for bucket in by_capability.values():
        executed = int(bucket["executed_count"])
        bucket["pass_rate"] = round(int(bucket["pass_count"]) / executed, 3) if executed else 0.0
    for capability, bucket in by_capability.items():
        bucket["distinct_evidence_count"] = len(evidence_sources[capability])
    executed_count = pass_count + fail_count
    weak_capabilities_missing = [
        capability
        for capability, bucket in by_capability.items()
        if int(bucket["executed_count"]) < 3
        or float(bucket["pass_rate"]) < 0.8
        or int(bucket["distinct_evidence_count"]) < 3
    ]
    return {
        "observed_executed_count": observed_executed_count,
        "executed_count": executed_count,
        "pass_count": pass_count,
        "fail_count": fail_count,
        "not_run_count": not_run_count,
        "pass_rate": round(pass_count / executed_count, 3) if executed_count else 0.0,
        "minimum_executed": 10,
        "minimum_pass_rate": 0.8,
        "minimum_per_weak_capability": 3,
        "by_capability": by_capability,
        "weak_capabilities_missing": weak_capabilities_missing,
        "rejection_reasons": dict(sorted(rejection_reasons.items())),
        "manifest_record_ids": manifest_record_ids,
        "manifest_rejection_reasons": manifest_rejection_reasons,
    }


def _capability_replay_records(runtime: Any, *, scope: ScopeRef, limit: int) -> list[Any]:
    budget = max(1, int(limit))
    lookup = getattr(runtime.store, "list_records_by_meta_value", None)
    if callable(lookup):
        try:
            records = lookup(
                kinds=["replay_result"],
                scope=scope,
                meta_key="report_type",
                meta_value="capability_replay_pack",
                limit=budget,
            )
            if records is not None:
                return list(records)
        except Exception:
            pass
    try:
        return list(runtime.store.list_records(kinds=["replay_result"], scope=scope, limit=budget))
    except Exception:
        return []


def _latest_manifest_case_records(
    runtime: Any,
    *,
    scope: ScopeRef,
    limit: int,
) -> tuple[list[Any], dict[str, str], dict[str, str]]:
    high_water = _latest_manifest_high_water(runtime, scope=scope, limit=limit)
    log_state = capability_replay_log_sequence_state(runtime, scope=scope, capabilities=WEAK_CAPABILITIES)
    latest: dict[str, tuple[int, Any]] = {}
    for manifest in _capability_replay_manifest_records(runtime, scope=scope, limit=limit):
        content = manifest.get("content") if isinstance(manifest, dict) else getattr(manifest, "content", None)
        content = content if isinstance(content, dict) else {}
        capabilities = content.get("capabilities") if isinstance(content.get("capabilities"), list) else []
        sequences = content.get("sequence_by_capability") if isinstance(content.get("sequence_by_capability"), dict) else {}
        for capability in {str(value or "").strip() for value in capabilities} & WEAK_CAPABILITIES:
            try:
                sequence = int(sequences.get(capability) or 0)
            except (TypeError, ValueError):
                sequence = 0
            current = latest.get(capability)
            if current is None or sequence > current[0]:
                latest[capability] = (sequence, manifest)

    selected: list[Any] = []
    manifest_record_ids: dict[str, str] = {}
    rejection_reasons: dict[str, str] = {}
    for capability in sorted(WEAK_CAPABILITIES):
        entry = latest.get(capability)
        if entry is None:
            rejection_reasons[capability] = "manifest_missing"
            continue
        manifest = entry[1]
        manifest_record_ids[capability] = _record_id(manifest)
        capability_log_state = log_state.get(capability) or {}
        log_manifest_ids = set(capability_log_state.get("manifest_record_ids") or set())
        if len(log_manifest_ids) > 1:
            rejection_reasons[capability] = "manifest_sequence_collision"
            continue
        if int(capability_log_state.get("sequence") or 0) != int(entry[0] or 0):
            rejection_reasons[capability] = "manifest_log_high_water_mismatch"
            continue
        high_water_entry = high_water.get(capability) or {}
        expected_manifest_id = str(high_water_entry.get("manifest_record_id") or "")
        if not expected_manifest_id:
            rejection_reasons[capability] = "manifest_high_water_missing"
            continue
        if str(high_water_entry.get("status") or "") != "active" or str(high_water_entry.get("source") or "") != "eimemory.autonomous_learning":
            rejection_reasons[capability] = "manifest_high_water_status_invalid"
            continue
        if expected_manifest_id != _record_id(manifest):
            rejection_reasons[capability] = "manifest_high_water_mismatch"
            continue
        if int(high_water_entry.get("manifest_sequence") or 0) != int(entry[0] or 0):
            rejection_reasons[capability] = "manifest_high_water_sequence_mismatch"
            continue
        manifest_content = manifest.content if isinstance(manifest.content, dict) else {}
        if str(high_water_entry.get("execution_id") or "") != str(manifest_content.get("execution_id") or ""):
            rejection_reasons[capability] = "manifest_high_water_execution_mismatch"
            continue
        records, reason = _validated_manifest_members(runtime, manifest, scope=scope, capability=capability)
        if reason:
            rejection_reasons[capability] = reason
            continue
        selected.extend(records)
    return selected, manifest_record_ids, rejection_reasons


def _latest_manifest_high_water(runtime: Any, *, scope: ScopeRef, limit: int) -> dict[str, dict[str, Any]]:
    try:
        records = runtime.store.list_records(kinds=["capability_score"], scope=scope, limit=max(1, int(limit)))
    except Exception:
        return {}
    latest: dict[str, tuple[int, dict[str, Any]]] = {}
    for record in records:
        if str(record.meta.get("kind") or "") != "capability_replay_pack":
            continue
        capability = str(record.meta.get("capability") or record.content.get("capability") or "").strip()
        if capability not in WEAK_CAPABILITIES:
            continue
        manifest_id = str(record.meta.get("manifest_record_id") or "").strip()
        try:
            score_sequence = int(record.meta.get("score_sequence") or record.content.get("score_sequence") or 0)
        except (TypeError, ValueError):
            score_sequence = 0
        try:
            manifest_sequence = int(record.meta.get("manifest_sequence") or 0)
        except (TypeError, ValueError):
            manifest_sequence = 0
        payload = {
            "manifest_record_id": manifest_id,
            "manifest_sequence": manifest_sequence,
            "execution_id": str(record.meta.get("replay_execution_id") or ""),
            "score_record_id": record.record_id,
            "score_sequence": score_sequence,
            "status": str(record.status or ""),
            "source": str(record.source or ""),
        }
        current = latest.get(capability)
        if current is None or score_sequence > current[0]:
            latest[capability] = (score_sequence, payload)
    return {capability: value[1] for capability, value in latest.items()}


def _capability_replay_manifest_records(runtime: Any, *, scope: ScopeRef, limit: int) -> list[Any]:
    budget = max(1, int(limit))
    lookup = getattr(runtime.store, "list_records_by_meta_value", None)
    if callable(lookup):
        try:
            records = lookup(
                kinds=["replay_result"],
                scope=scope,
                meta_key="report_type",
                meta_value=MANIFEST_REPORT_TYPE,
                limit=budget,
            )
            if records is not None:
                return list(records)
        except Exception:
            pass
    try:
        return [
            record
            for record in runtime.store.list_records(kinds=["replay_result"], scope=scope, limit=budget)
            if str(_record_field(record, "report_type") or "") == MANIFEST_REPORT_TYPE
        ]
    except Exception:
        return []


def _validated_manifest_members(
    runtime: Any,
    manifest: Any,
    *,
    scope: ScopeRef,
    capability: str,
) -> tuple[list[Any], str]:
    content = manifest.get("content") if isinstance(manifest, dict) else getattr(manifest, "content", None)
    content = content if isinstance(content, dict) else {}
    meta = manifest.get("meta") if isinstance(manifest, dict) else getattr(manifest, "meta", None)
    meta = meta if isinstance(meta, dict) else {}
    provenance = manifest.get("provenance") if isinstance(manifest, dict) else getattr(manifest, "provenance", None)
    provenance = provenance if isinstance(provenance, dict) else {}
    source = str(manifest.get("source", "") if isinstance(manifest, dict) else getattr(manifest, "source", "") or "")
    status = str(manifest.get("status", "") if isinstance(manifest, dict) else getattr(manifest, "status", "") or "")
    execution_id = str(content.get("execution_id") or "").strip()
    digest = str(content.get("manifest_digest") or "").strip()
    if source != "eimemory.capability_replay":
        return [], "manifest_source_untrusted"
    if status != "active":
        return [], "manifest_status_invalid"
    if any(
        str(container.get("report_type") or "") != MANIFEST_REPORT_TYPE
        or str(container.get("schema_version") or "") != MANIFEST_SCHEMA_VERSION
        for container in (content, meta, provenance)
    ):
        return [], "manifest_schema_mismatch"
    if not execution_id or any(str(container.get("execution_id") or "").strip() != execution_id for container in (meta, provenance)):
        return [], "manifest_execution_id_missing_or_mismatched"
    if not digest or any(str(container.get("manifest_digest") or "").strip() != digest for container in (meta, provenance)):
        return [], "manifest_digest_mismatch"
    if capability_replay_manifest_digest(content) != digest:
        return [], "manifest_digest_mismatch"
    if any(container.get("complete") is not True for container in (content, meta, provenance)):
        return [], "manifest_incomplete"

    manifest_started = _record_created_at(manifest)
    manifest_finished = _record_updated_at(manifest)
    executed_at = _parse_timestamp(content.get("executed_at"))
    if manifest_started is None or manifest_finished is None:
        return [], "manifest_record_time_invalid"
    now = datetime.now(timezone.utc)
    if any(value > now + timedelta(minutes=5) for value in (manifest_started, manifest_finished)):
        return [], "manifest_time_in_future"
    if executed_at is None or executed_at > now + timedelta(minutes=5):
        return [], "manifest_time_in_future" if executed_at is not None else "manifest_time_invalid"
    if executed_at < manifest_started - timedelta(minutes=1) or executed_at > manifest_finished + timedelta(minutes=1):
        return [], "manifest_time_invalid"
    expected_map = content.get("expected_case_ids") if isinstance(content.get("expected_case_ids"), dict) else {}
    member_map = content.get("member_record_ids") if isinstance(content.get("member_record_ids"), dict) else {}
    member_digest_map = content.get("member_digests") if isinstance(content.get("member_digests"), dict) else {}
    expected_case_ids = [str(value or "").strip() for value in expected_map.get(capability) or []]
    member_ids = [str(value or "").strip() for value in member_map.get(capability) or []]
    member_digests = member_digest_map.get(capability) if isinstance(member_digest_map.get(capability), dict) else {}
    canonical_case_ids = capability_replay_case_ids(capability)
    if expected_case_ids != canonical_case_ids:
        return [], "manifest_expected_cases_mismatch"
    if len(member_ids) != len(canonical_case_ids) or len(set(member_ids)) != len(member_ids):
        return [], "manifest_member_count_mismatch"
    manifest_evidence = [str(value or "").strip() for value in (manifest.get("evidence", []) if isinstance(manifest, dict) else getattr(manifest, "evidence", []) or [])]
    all_member_ids = [
        str(value or "").strip()
        for values in member_map.values()
        if isinstance(values, list)
        for value in values
    ]
    if sorted(manifest_evidence) != sorted(all_member_ids):
        return [], "manifest_evidence_members_mismatch"

    records: list[Any] = []
    seen_case_ids: set[str] = set()
    seen_probe_ids: set[str] = set()
    for member_id in member_ids:
        record = runtime.store.get_by_id(member_id, scope=scope)
        if record is None:
            return [], "manifest_member_missing"
        if str(member_digests.get(member_id) or "") != capability_replay_member_digest(record):
            return [], "manifest_member_digest_mismatch"
        record_content = record.content if isinstance(record.content, dict) else {}
        result = record_content.get("result") if isinstance(record_content.get("result"), dict) else {}
        verdict = str(result.get("verdict") or record_content.get("verdict") or "").strip().lower()
        case_payload = record_content.get("case") if isinstance(record_content.get("case"), dict) else {}
        case_id = str(case_payload.get("case_id") or record.meta.get("case_id") or "").strip()
        probe_id = str(result.get("probe_source_id") or "").strip()
        if (
            record.source != "eimemory.capability_replay"
            or record.kind != "replay_result"
            or record.status != "active"
            or str(record.meta.get("report_type") or "") != "capability_replay_pack"
            or str(record.meta.get("execution_id") or "").strip() != execution_id
            or str(record_content.get("execution_id") or "").strip() != execution_id
            or str(record.meta.get("executed_at") or "") != str(content.get("executed_at") or "")
            or str(record_content.get("executed_at") or "") != str(content.get("executed_at") or "")
            or str(record.meta.get("capability") or "") != capability
            or str(record_content.get("capability") or "") != capability
            or case_id not in canonical_case_ids
        ):
            return [], "manifest_member_binding_mismatch"
        member_created = _record_created_at(record)
        if member_created is None:
            return [], "manifest_member_time_invalid"
        if member_created > now + timedelta(minutes=5):
            return [], "manifest_time_in_future"
        if member_created < manifest_started - timedelta(seconds=5) or member_created > manifest_finished + timedelta(seconds=5):
            return [], "manifest_member_time_invalid"
        if case_id in seen_case_ids:
            return [], "manifest_duplicate_case_id"
        seen_case_ids.add(case_id)
        if verdict == "pass":
            if not probe_id or probe_id in seen_probe_ids:
                return [], "manifest_probe_binding_invalid"
            seen_probe_ids.add(probe_id)
            probe = runtime.store.get_by_id(probe_id, scope=scope)
            if probe is None:
                return [], "manifest_probe_missing"
            if probe.kind != "replay_result" or probe.status != "active":
                return [], "manifest_probe_status_invalid"
            trace_record_id = str(result.get("trace_record_id") or "").strip()
            trace = runtime.store.get_by_id(trace_record_id, scope=scope)
            if trace is None or trace.kind != "reflection" or trace.status != "active":
                return [], "manifest_trace_status_invalid"
            probe_created = _record_created_at(probe)
            if probe_created is None:
                return [], "manifest_probe_time_invalid"
            if probe_created > now + timedelta(minutes=5):
                return [], "manifest_time_in_future"
            if probe_created < manifest_started - timedelta(minutes=15) or probe_created > manifest_finished + timedelta(seconds=5):
                return [], "manifest_probe_not_fresh"
        records.append(record)
    if seen_case_ids != set(canonical_case_ids):
        return [], "manifest_case_coverage_incomplete"
    return records, ""


def _record_id(record: Any) -> str:
    return str(record.get("record_id", "") if isinstance(record, dict) else getattr(record, "record_id", "") or "")


def _record_created_at(record: Any) -> datetime | None:
    time_ref = record.get("time") if isinstance(record, dict) else getattr(record, "time", None)
    value = time_ref.get("created_at") if isinstance(time_ref, dict) else getattr(time_ref, "created_at", "")
    return _parse_timestamp(value)


def _record_updated_at(record: Any) -> datetime | None:
    time_ref = record.get("time") if isinstance(record, dict) else getattr(record, "time", None)
    value = time_ref.get("updated_at") if isinstance(time_ref, dict) else getattr(time_ref, "updated_at", "")
    return _parse_timestamp(value) or _record_created_at(record)


def _parse_timestamp(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def _latest_l5_assessment(
    runtime: Any,
    *,
    scope: ScopeRef,
    release: ReleaseIdentity | None = None,
) -> dict[str, Any]:
    current_release = release or current_release_identity(runtime, scope)
    records: list[Any] = []
    sqlite = getattr(getattr(runtime, "store", None), "sqlite", None)
    conn = getattr(sqlite, "conn", None)
    if conn is not None:
        try:
            rows = conn.execute(
                """
                SELECT record_id
                FROM records
                WHERE kind = 'l5_assessment'
                  AND source = 'eimemory.l5_loop'
                  AND tenant_id = ? AND agent_id = ? AND workspace_id = ? AND user_id = ?
                ORDER BY rowid DESC
                LIMIT 500
                """,
                (scope.tenant_id, scope.agent_id, scope.workspace_id, scope.user_id),
            ).fetchall()
            records = [runtime.store.get_by_id(str(row[0]), scope=scope) for row in rows]
            records = [record for record in records if record is not None]
        except Exception:
            records = []
    if not records:
        try:
            offset = 0
            while True:
                page = runtime.store.list_records(
                    kinds=["l5_assessment"],
                    scope=scope,
                    limit=100,
                    offset=offset,
                )
                records.extend(page)
                if len(page) < 100:
                    break
                offset += len(page)
        except Exception:
            records = []
    if not records:
        return {"present": False, "trusted": False, "complete": False, "level": "", "missing_evidence": [], "record_id": ""}
    if current_release is None:
        record = _global_l5_readiness_record(records)
        return {
            "present": True,
            "trusted": False,
            "complete": False,
            "assessment_id": str(_record_field(record, "assessment_id") or ""),
            "level": str(_record_field(record, "level") or ""),
            "missing_evidence": ["release_identity:unavailable"],
            "record_id": str(getattr(record, "record_id", "") or ""),
        }
    requirement = EvidenceRequirement(
        kinds=frozenset({"l5_assessment"}),
        sources=frozenset({"eimemory.l5_loop"}),
        statuses=frozenset({"active", "candidate"}),
        evidence_classes=frozenset({"structural"}),
    )
    release_records = [
        record
        for record in records
        if resolve_evidence(runtime, str(record.record_id or ""), requirement, scope, current_release).ok
    ]
    if not release_records:
        record = _global_l5_readiness_record(records)
        return {
            "present": True,
            "trusted": False,
            "complete": False,
            "assessment_id": str(_record_field(record, "assessment_id") or ""),
            "level": str(_record_field(record, "level") or ""),
            "missing_evidence": ["assessment:release_mismatch"],
            "record_id": str(getattr(record, "record_id", "") or ""),
        }
    record = _global_l5_readiness_record(release_records)
    missing = _record_field(record, "missing_evidence")
    missing_evidence = [str(item) for item in missing] if isinstance(missing, list) else []
    level = str(_record_field(record, "level") or "")
    source = str(record.get("source", "") if isinstance(record, dict) else getattr(record, "source", "") or "")
    trusted = (
        source == "eimemory.l5_loop"
        and str(_record_field(record, "report_type") or "") == "l5_assessment"
        and str(_record_field(record, "schema_version") or "") == "l5_closed_loop.v1"
    )
    complete = trusted and bool(_record_field(record, "complete")) and level == "L5" and not missing_evidence
    return {
        "present": True,
        "trusted": trusted,
        "complete": complete,
        "assessment_id": str(_record_field(record, "assessment_id") or ""),
        "level": level,
        "missing_evidence": missing_evidence,
        "record_id": str(getattr(record, "record_id", "") or ""),
    }


def _global_l5_readiness_record(records: list[Any]) -> Any:
    latest = records[0]
    if str(_record_field(latest, "activity_status") or "").strip().lower() not in {"idle", "no_change"}:
        return latest
    for record in records[1:]:
        if str(_record_field(record, "activity_status") or "").strip().lower() not in {"idle", "no_change"}:
            return record
    return latest


def _record_field(record: Any, key: str) -> Any:
    if isinstance(record, dict):
        if key in record:
            return record.get(key)
        payloads = (record.get("content"), record.get("meta"))
    else:
        payloads = (getattr(record, "content", None), getattr(record, "meta", None))
    for payload in payloads:
        if isinstance(payload, dict) and key in payload:
            return payload.get(key)
    return None


def _capability_gaps(
    ledger: dict[str, Any],
    *,
    weak_outcome_evidence: dict[str, Any],
) -> list[dict[str, Any]]:
    capabilities = dict(ledger.get("capabilities") or {})
    weak_outcome_counts = (
        weak_outcome_evidence.get("counts")
        if isinstance(weak_outcome_evidence.get("counts"), dict)
        else {}
    )
    gaps = []
    for name in READINESS_CAPABILITIES:
        item = dict(capabilities.get(name) or {})
        score = float(item.get("score") or 0.0)
        evidence_count = int(item.get("evidence_count") or 0)
        outcome_count = (
            int(weak_outcome_counts.get(name) or 0)
            if name in WEAK_CAPABILITIES
            else _outcome_evidence_count(item)
        )
        if name in WEAK_CAPABILITIES and outcome_count < 3:
            gaps.append(
                {
                    "capability": name,
                    "score": round(score, 3),
                    "evidence_count": evidence_count,
                    "outcome_evidence_count": outcome_count,
                    "reason": "insufficient_attributed_outcome_evidence",
                    "priority": "high",
                }
            )
            continue
        if score >= 0.7 and evidence_count >= 3:
            continue
        gaps.append(
            {
                "capability": name,
                "score": round(score, 3),
                "evidence_count": evidence_count,
                "outcome_evidence_count": outcome_count,
                "reason": str(item.get("goal_gap_reason") or item.get("status") or "insufficient_evidence"),
                "priority": "high" if name in WEAK_CAPABILITIES else "medium",
            }
        )
    return gaps


def _stage_for(
    ledger: dict[str, Any],
    hard_metrics: dict[str, Any],
    evidence_counts: dict[str, int],
    capability_gaps: list[dict[str, Any]],
    weak_outcome_evidence: dict[str, Any],
    verified_replay: dict[str, Any],
    latest_l5_assessment: dict[str, Any],
) -> dict[str, Any]:
    metrics = dict(hard_metrics.get("metrics") or {})
    metric_quality = dict(hard_metrics.get("metric_quality") or {})
    replay_count = int(verified_replay.get("executed_count") or 0)
    observed_replay_count = int(verified_replay.get("observed_executed_count") or replay_count)
    replay_pass_rate = float(verified_replay.get("pass_rate") or 0.0)
    l5_artifacts = sum(int(evidence_counts.get(kind) or 0) for kind in ("l5_world_model", "l5_strategic_roadmap", "l5_assessment", "l5_closed_loop"))
    promotion_count = int(evidence_counts.get("promotion_applied") or 0)
    rollback_count = int(evidence_counts.get("rollback_or_quarantine") or 0)
    weak_gap_count = sum(1 for gap in capability_gaps if gap["capability"] in WEAK_CAPABILITIES)
    strong_ready_count = _ready_count(ledger, STRONG_CAPABILITIES)
    core_ready_count = _ready_count(ledger, set(READINESS_CAPABILITIES))
    task_success = float(metrics.get("task_success_rate") or 0.0)
    recall_hit = float(metrics.get("recall_hit_rate") or 0.0)
    patch_success = float(metrics.get("patch_promotion_success_rate") or metrics.get("auto_patch_success_rate") or 0.0)
    patch_quality_ok = bool(
        (metric_quality.get("patch_promotion_success_rate") or metric_quality.get("auto_patch_success_rate") or {}).get("sufficient")
    )
    verified_live_success = float(metrics.get("current_deployment_verified_real_task_success_rate") or 0.0)
    verified_live_quality = metric_quality.get("current_deployment_verified_real_task_success_rate") or {}
    sample_counts = hard_metrics.get("sample_counts") if isinstance(hard_metrics.get("sample_counts"), dict) else {}
    verified_live_samples = int(sample_counts.get("current_deployment_verified_real_tasks") or 0)
    verified_live_task_types = int(sample_counts.get("current_deployment_verified_real_task_types") or 0)
    operational_probes = int(sample_counts.get("current_deployment_operational_probes") or 0)
    live_task_gate = {
        "ok": bool(
            verified_live_quality.get("sufficient")
            and verified_live_success >= 0.8
            and verified_live_task_types >= 5
            and verified_live_samples >= 10
        ),
        "success_rate": verified_live_success,
        "sample_count": verified_live_samples,
        "minimum_samples": 10,
        "sample_deficit": max(0, 10 - verified_live_samples),
        "distinct_task_types": verified_live_task_types,
        "minimum_task_types": 5,
        "task_type_deficit": max(0, 5 - verified_live_task_types),
        "current_deployment_verified_real_tasks": verified_live_samples,
        "current_deployment_operational_probes": operational_probes,
    }
    weak_outcome_ok = not weak_outcome_evidence.get("missing")

    readiness_score = round(
        min(1.0, (core_ready_count / len(READINESS_CAPABILITIES) * 0.45) + (min(replay_count, 10) / 10 * 0.2) + (min(l5_artifacts, 4) / 4 * 0.2) + (min(promotion_count, 5) / 5 * 0.15)),
        3,
    )
    if (
        not weak_outcome_ok
        or not patch_quality_ok
        or verified_replay.get("weak_capabilities_missing")
        or not latest_l5_assessment.get("complete")
        or not live_task_gate["ok"]
    ):
        readiness_score = min(readiness_score, 0.8)
    common = {
        "readiness_score": readiness_score,
        "live_task_gate": live_task_gate,
        "risk_boundary": "read-only reporting; no autonomous apply, deployment, external send, spend, deletion, or credential use.",
    }
    structural_ready = bool(
        l5_artifacts >= 4
        and weak_gap_count == 0
        and weak_outcome_ok
        and replay_count >= 10
        and replay_pass_rate >= 0.8
        and not verified_replay.get("weak_capabilities_missing")
        and latest_l5_assessment.get("complete") is True
        and promotion_count >= 1
        and rollback_count >= 1
        and patch_quality_ok
        and patch_success >= 0.8
    )
    if structural_ready and live_task_gate["ok"]:
        return {
            **common,
            "readiness_score": 1.0,
            "stage": "L5",
            "label": "evidence-bound co-growth loop",
            "reason": "world model, roadmap, assessment, replay, promotion, rollback, and verified live task evidence are all present.",
            "done_when": "Maintain zero missing L5 assessment evidence and keep verified live task success at or above 0.8.",
        }
    if structural_ready:
        return {
            **common,
            "readiness_score": 0.9,
            "stage": "data_accumulating",
            "label": "release structure complete; real-task evidence accumulating",
            "reason": "All structural and safety gates pass, but the current release has not accumulated ten verified real tasks across five task types.",
            "done_when": "Accumulate the remaining current-release verified real tasks; operational probes do not count as user-task evidence.",
        }
    if l5_artifacts >= 2 and replay_count >= 5 and weak_gap_count <= 2:
        return {
            **common,
            "stage": "L4.5",
            "label": "self-growth reporting with most weak gaps closing",
            "reason": "L5 rehearsal artifacts exist, but one or more production evidence gates remain incomplete.",
            "done_when": "Complete replay, reversible promotion, and at least ten current-deployment verified live tasks across five task types.",
        }
    if strong_ready_count >= 3 and observed_replay_count >= 3 and (task_success > 0 or recall_hit > 0):
        return {
            **common,
            "stage": "L4",
            "label": "closed-loop learning with measurable outcomes",
            "reason": "core capabilities have ledger evidence and replay exists, but weak capability coverage is incomplete.",
            "done_when": "Autonomous cycles produce goal graph, replay dataset, promotion/block decision, and dashboard metrics every run.",
        }
    return {
        **common,
        "stage": "L3.5",
        "label": "early autonomous evolution with evidence gaps",
        "reason": "learning and candidate records may exist, but repeatable replay, L5 artifacts, and weak capability evidence are not yet enough.",
        "done_when": "Add readiness report, replay packs, and hard metrics for weak capabilities without changing production behavior.",
    }


def _ready_count(ledger: dict[str, Any], capability_names: set[str]) -> int:
    capabilities = dict(ledger.get("capabilities") or {})
    total = 0
    for name in capability_names:
        item = dict(capabilities.get(name) or {})
        if float(item.get("score") or 0.0) >= 0.7 and int(item.get("evidence_count") or 0) >= 3:
            total += 1
    return total


def _weak_outcome_evidence(runtime: Any, *, scope: ScopeRef, limit: int) -> dict[str, Any]:
    evidence_by_capability = collect_capability_evidence(runtime, scope=scope, limit=limit)
    counts = {
        name: len(
            {
                str(item.get("source_id") or "")
                for item in evidence_by_capability.get(name, [])
                if item.get("contract_verified") is True and str(item.get("source_id") or "")
            }
        )
        for name in sorted(WEAK_CAPABILITIES)
    }
    return {
        "minimum_per_capability": 3,
        "counts": counts,
        "missing": [name for name, count in counts.items() if count < 3],
    }


def _outcome_evidence_count(item: dict[str, Any]) -> int:
    source_counts = item.get("evidence_source_counts") if isinstance(item.get("evidence_source_counts"), dict) else {}
    return int(source_counts.get("event_outcome") or 0) + int(source_counts.get("outcome_trace") or 0)


def _next_actions(
    stage: dict[str, Any],
    capability_gaps: list[dict[str, Any]],
    evidence_counts: dict[str, int],
    *,
    verified_replay: dict[str, Any],
    latest_l5_assessment: dict[str, Any],
) -> list[str]:
    actions = []
    live_task_gate = stage.get("live_task_gate") if isinstance(stage.get("live_task_gate"), dict) else {}
    if not live_task_gate.get("ok"):
        actions.append(
            "Accumulate current-release real user tasks; L5 requires ten verified non-rehearsal outcomes across five task types with success rate >=0.8, and operational probes do not count."
        )
    if int(verified_replay.get("executed_count") or 0) < 5:
        actions.append("Execute replay packs from existing outcome traces before promoting new behavior; not_run records do not count.")
    for capability in list(verified_replay.get("weak_capabilities_missing") or [])[:4]:
        actions.append(f"Execute at least three replays for {capability} with pass rate >=0.8.")
    for gap in capability_gaps[:4]:
        actions.append(f"Add replay-backed evidence for {gap['capability']} ({gap['reason']}).")
    if int(evidence_counts.get("l5_world_model") or 0) == 0:
        actions.append("Run or persist an L5 world-model report after the read-only readiness report is reviewed.")
    if stage["stage"] in {"L4", "L4.5"} and int(evidence_counts.get("rollback_or_quarantine") or 0) == 0:
        actions.append("Exercise a non-destructive rollback/quarantine rehearsal so reversibility is proven.")
    if not latest_l5_assessment.get("complete"):
        actions.append("Complete an L5 assessment with zero missing evidence before claiming L5.")
    return actions[:6] or ["Keep running readiness, replay, and dashboard reports; do not claim L5 unless assessment evidence is complete."]
