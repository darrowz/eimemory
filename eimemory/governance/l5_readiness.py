from __future__ import annotations

from dataclasses import asdict
from typing import Any

from eimemory.core.clock import now_iso
from eimemory.governance.capability_attribution import collect_capability_evidence
from eimemory.governance.capability_ledger import build_capability_ledger
from eimemory.governance.capability_replay_executor import validate_capability_replay_result
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
    ledger = build_capability_ledger(runtime, scope=scope_ref, limit=limit, attribute_outcomes=True)
    hard_metrics = _safe_hard_metrics(runtime, scope=scope_ref, limit=limit)
    evidence_counts = _evidence_counts(runtime, scope=scope_ref, limit=limit)
    verified_replay = _verified_replay_summary(runtime, scope=scope_ref, limit=limit)
    latest_l5_assessment = _latest_l5_assessment(runtime, scope=scope_ref)
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
    rejection_reasons: dict[str, int] = {}
    for record in _latest_capability_case_records(records):
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
        bucket = by_capability.get(capability)
        if verdict == "pass":
            pass_count += 1
            if bucket is not None:
                bucket["executed_count"] += 1
                bucket["pass_count"] += 1
                evidence_sources[capability].add(evidence_source_id)
        elif verdict == "fail":
            fail_count += 1
            if bucket is not None:
                bucket["executed_count"] += 1
                bucket["fail_count"] += 1
        elif verdict == "not_run":
            not_run_count += 1
            if bucket is not None:
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


def _latest_capability_case_records(records: list[Any]) -> list[Any]:
    latest: dict[tuple[str, str], tuple[tuple[str, str, str], Any]] = {}
    for record in records:
        report_type = str(_record_field(record, "report_type") or "").strip()
        source = str(record.get("source", "") if isinstance(record, dict) else getattr(record, "source", "") or "").strip()
        if report_type != "capability_replay_pack" or source != "eimemory.capability_replay":
            continue
        capability = str(_record_field(record, "capability") or _record_field(record, "target_capability") or "").strip()
        case_id = str(_record_field(record, "case_id") or "").strip()
        record_id = str(record.get("record_id", "") if isinstance(record, dict) else getattr(record, "record_id", "") or "")
        if not case_id:
            case_id = record_id
        record_time = getattr(record, "time", None)
        fallback_time = str(getattr(record_time, "updated_at", "") or getattr(record_time, "created_at", "") or "")
        sort_key = (
            str(_record_field(record, "executed_at") or fallback_time),
            str(_record_field(record, "execution_id") or ""),
            record_id,
        )
        key = (capability, case_id)
        current = latest.get(key)
        if current is None or sort_key > current[0]:
            latest[key] = (sort_key, record)
    return [item[1] for item in latest.values()]


def _latest_l5_assessment(runtime: Any, *, scope: ScopeRef) -> dict[str, Any]:
    try:
        records = runtime.store.list_records(kinds=["l5_assessment"], scope=scope, limit=1)
    except Exception:
        records = []
    if not records:
        return {"present": False, "trusted": False, "complete": False, "level": "", "missing_evidence": [], "record_id": ""}
    record = records[0]
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
        "level": level,
        "missing_evidence": missing_evidence,
        "record_id": str(getattr(record, "record_id", "") or ""),
    }


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
    ):
        readiness_score = min(readiness_score, 0.8)
    common = {
        "readiness_score": readiness_score,
        "risk_boundary": "read-only reporting; no autonomous apply, deployment, external send, spend, deletion, or credential use.",
    }
    if (
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
    ):
        return {
            **common,
            "readiness_score": 1.0,
            "stage": "L5",
            "label": "evidence-bound co-growth loop",
            "reason": "world model, roadmap, assessment, replay, promotion, and rollback evidence are all present.",
            "done_when": "Maintain zero missing L5 assessment evidence across repeated cycles and keep weak capabilities active.",
        }
    if l5_artifacts >= 2 and replay_count >= 5 and weak_gap_count <= 2:
        return {
            **common,
            "stage": "L4.5",
            "label": "self-growth reporting with most weak gaps closing",
            "reason": "L5 artifacts exist, but repeated closed-loop promotion and rollback evidence is not complete.",
            "done_when": "Each weak capability has replay-backed score >=0.7 and at least one reversible promotion path.",
        }
    if strong_ready_count >= 3 and replay_count >= 3 and (task_success > 0 or recall_hit > 0):
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
