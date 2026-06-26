from __future__ import annotations

from dataclasses import asdict
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from eimemory.core.clock import now_iso
from eimemory.governance.capability_ledger import build_capability_ledger
from eimemory.governance.capability_dashboard import build_capability_dashboard_metrics
from eimemory.governance.capability_seeding import SEEDED_CAPABILITIES, ensure_all_seeded
from eimemory.governance.learning_report import build_learning_daily_report
from eimemory.governance.learning_state import append_learning_record_once, stable_semantic_key
from eimemory.models.records import ScopeRef


def build_weekly_dashboard(
    runtime: Any,
    *,
    scope: dict[str, Any] | ScopeRef | None = None,
    week_start: str | None = None,
    persist: bool = True,
    output_path: str | Path | None = None,
    weekly: bool = False,
) -> dict[str, Any]:
    scope_ref = scope if isinstance(scope, ScopeRef) else ScopeRef.from_dict(scope)
    ensure_all_seeded(runtime, scope=scope_ref, loop_id="dashboard_seed")
    period_type = "weekly" if weekly else "daily"
    start = _week_start(week_start) if weekly else _day_start(week_start)
    ledger = build_capability_ledger(runtime, scope=scope_ref)
    daily = build_learning_daily_report(runtime, scope=scope_ref, persist=False)
    failures = _failure_breakdown(runtime, scope=scope_ref, since=start)
    activity = _activity_breakdown(runtime, scope=scope_ref, since=start)
    promotion_statuses = _promotion_statuses(runtime, scope=scope_ref)
    roi = _safe_roi(runtime, scope=scope_ref)
    module_status = _module_status(runtime, scope=scope_ref)
    hard_metrics = build_capability_dashboard_metrics(runtime, scope=scope_ref, persist=False)
    markdown = _render_markdown(
        start=start,
        period_type=period_type,
        ledger=ledger,
        daily=daily,
        failures=failures,
        activity=activity,
        roi=roi,
        module_status=module_status,
        hard_metrics=hard_metrics,
    )
    output_error: str | dict[str, str] = ""
    written_path = ""
    if output_path:
        target = Path(output_path)
        written_path = str(target)
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(markdown, encoding="utf-8")
        except OSError as exc:
            output_error = {"type": exc.__class__.__name__, "detail": str(exc)}
    record_id = ""
    if persist:
        record = append_learning_record_once(
            runtime,
            kind="reflection",
            title=f"Autonomous learning {period_type} dashboard {start}",
            summary=f"{period_type.title()} dashboard for {len((ledger.get('capabilities') or {}))} capabilities.",
            scope=scope_ref,
            loop_id=f"{period_type}_dashboard_{start}",
            step_name="learning_dashboard",
            semantic_key=stable_semantic_key("learning_dashboard", period_type, start, scope_ref),
            authority_tier="L0",
            status="active",
            content={
                "report_type": f"autonomous_learning_{period_type}_dashboard",
                "period_type": period_type,
                "period_start": start,
                "markdown": markdown,
                "ledger": ledger,
                "failures": failures,
                "activity": activity,
                "promotion_statuses": promotion_statuses,
                "roi": roi,
                "module_status": module_status,
                "hard_metrics": hard_metrics,
            },
            meta={"report_type": f"autonomous_learning_{period_type}_dashboard", "period_type": period_type, "period_start": start, "capability_count": len(ledger.get("capabilities") or {})},
        )
        record_id = record.record_id
    return {
        "ok": True,
        "report_type": f"autonomous_learning_{period_type}_dashboard",
        "period_type": period_type,
        "period_start": start,
        "week_start": start,
        "persisted": bool(persist),
        "persisted_record_id": record_id,
        "output_path": written_path,
        "output_error": output_error,
        "markdown": markdown,
        "ledger": ledger,
        "failure_breakdown": failures,
        "activity": activity,
        "promotion_statuses": promotion_statuses,
        "roi": roi,
        "module_status": module_status,
        "hard_metrics": hard_metrics,
    }


def _week_start(value: str | None) -> str:
    if value:
        return value[:10]
    today = datetime.fromisoformat(now_iso().replace("Z", "+00:00")).date()
    monday = today - timedelta(days=today.weekday())
    return monday.isoformat()


def _day_start(value: str | None) -> str:
    if value:
        return value[:10]
    return datetime.fromisoformat(now_iso().replace("Z", "+00:00")).date().isoformat()


def _failure_breakdown(runtime: Any, *, scope: ScopeRef, since: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for kind in ["world_signal", "weakness", "regression_watch", "promotion_request", "reflection"]:
        for record in runtime.store.list_records(kinds=[kind], scope=scope, limit=500):
            if _record_date(record) < since:
                continue
            capability = str(record.meta.get("target_capability") or record.meta.get("capability") or _classify(record.summary or record.title))
            status = str(record.status or "")
            if kind in {"weakness", "regression_watch"} or status in {"blocked", "failed"} or "fail" in (record.summary or "").lower():
                counts[capability] = counts.get(capability, 0) + 1
    return counts


def _activity_breakdown(runtime: Any, *, scope: ScopeRef, since: str) -> dict[str, Any]:
    counts = {
        "signals": 0,
        "candidates": 0,
        "promotions": 0,
        "rollbacks": 0,
        "replay_pass_rate": 0.0,
        "replay_pass_count": 0,
        "replay_fail_count": 0,
    }
    for record in runtime.store.list_records(kinds=["world_signal"], scope=scope, limit=500):
        if _record_date(record) >= since:
            counts["signals"] += 1
    for record in runtime.store.list_records(kinds=["capability_candidate"], scope=scope, limit=500):
        if _record_date(record) >= since:
            counts["candidates"] += 1
    for record in runtime.store.list_records(kinds=["promotion_request"], scope=scope, limit=500):
        if _record_date(record) < since:
            continue
        if str(record.status or "") == "promoted":
            counts["promotions"] += 1
        if str(record.status or "") in {"rolled_back", "quarantined"}:
            counts["rollbacks"] += 1
    replays = []
    for record in runtime.store.list_records(kinds=["replay_result"], scope=scope, limit=500):
        if _record_date(record) >= since and str(record.meta.get("report_type") or record.content.get("report_type") or "") != "proactive_replay_dataset":
            replays.append(record)
    counts["replay_pass_count"] = sum(1 for item in replays if str(item.meta.get("verdict") or item.content.get("verdict") or "").lower() in {"pass", "passed", "success"})
    counts["replay_fail_count"] = sum(1 for item in replays if str(item.meta.get("verdict") or item.content.get("verdict") or "").lower() in {"fail", "failed", "failure"})
    total = counts["replay_pass_count"] + counts["replay_fail_count"]
    counts["replay_pass_rate"] = round(counts["replay_pass_count"] / total, 3) if total else 0.0
    return counts


def _promotion_statuses(runtime: Any, *, scope: ScopeRef, limit: int = 50) -> list[dict[str, Any]]:
    statuses: list[dict[str, Any]] = []
    for record in runtime.store.list_records(kinds=["promotion_request"], scope=scope, limit=limit):
        content = record.content if isinstance(record.content, dict) else {}
        meta = record.meta if isinstance(record.meta, dict) else {}
        candidate_id = str(content.get("candidate_id") or meta.get("candidate_id") or "")
        candidate = runtime.store.get_by_id(candidate_id, scope=scope) if candidate_id else None
        watch = dict((candidate.meta or {}).get("post_promotion_watch") or {}) if candidate is not None else {}
        statuses.append(
            {
                "promotion_request_id": record.record_id,
                "candidate_id": candidate_id,
                "status": str((candidate.status if candidate is not None else record.status) or record.status),
                "promotion_target": str(content.get("promotion_target") or meta.get("promotion_target") or ""),
                "target_capability": str(content.get("target_capability") or meta.get("target_capability") or ""),
                "action": str(content.get("action") or meta.get("action") or ""),
                "observed_count": int(watch.get("observed_count") or 0),
                "failure_rate": float(watch.get("failure_rate") or 0.0),
                "rollout_ledger_id": str(content.get("rollout_ledger_id") or meta.get("rollout_ledger_id") or ""),
                "updated_at": str(record.time.updated_at or record.time.created_at or ""),
            }
        )
    return statuses


def _safe_roi(runtime: Any, *, scope: ScopeRef) -> dict[str, Any]:
    try:
        return dict(runtime.evolution.build_roi_report(scope=asdict(scope)))
    except Exception as exc:
        return {"ok": False, "error": type(exc).__name__, "detail": str(exc), "roi_components": {}}


def _render_markdown(
    *,
    start: str,
    period_type: str,
    ledger: dict[str, Any],
    daily: dict[str, Any],
    failures: dict[str, int],
    activity: dict[str, Any],
    roi: dict[str, Any],
    module_status: dict[str, Any],
    hard_metrics: dict[str, Any],
) -> str:
    metrics = dict(hard_metrics.get("metrics") or {})
    lines = [
        f"# eimemory autonomous learning {period_type} dashboard ({start})",
        "",
        "## Autonomy Summary",
        "",
        f"- Signals: {int(activity.get('signals') or 0)}",
        f"- Candidates: {int(activity.get('candidates') or 0)}",
        f"- Promotions: {int(activity.get('promotions') or 0)}",
        f"- Rollbacks/quarantines: {int(activity.get('rollbacks') or 0)}",
        f"- Replay pass rate: {float(activity.get('replay_pass_rate') or 0.0):.3f}",
        f"- ROI signal: {float(roi.get('roi_signal') or 0.0):.3f}",
        "",
        "## Module Activation",
        "",
        "| Module | Enabled | Evidence |",
        "| --- | ---: | --- |",
        *_module_status_lines(module_status),
        "",
        "## Hard Metrics",
        "",
        f"- Recall hit rate: {float(metrics.get('recall_hit_rate') or 0.0):.3f}",
        f"- User correction rate: {float(metrics.get('user_correction_rate') or 0.0):.3f}",
        f"- Task success rate: {float(metrics.get('task_success_rate') or 0.0):.3f}",
        f"- Auto patch success rate: {float(metrics.get('auto_patch_success_rate') or 0.0):.3f}",
        f"- Rollbacks/quarantines: {int(metrics.get('rollback_count') or 0)}",
        f"- Skill reuse count: {int(metrics.get('skill_reuse_count') or 0)}",
        "",
        "## ROI Components",
        "",
        *_roi_component_lines(roi),
        "",
        "## Capability Ledger",
        "",
        "| Capability | Score | Average | Trend | Evidence | Regressions | Failures |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    caps = dict(ledger.get("capabilities") or {})
    for cap in SEEDED_CAPABILITIES:
        item = dict(caps.get(cap) or {})
        lines.append(
            f"| {cap} | {float(item.get('score') or 0.0):.3f} | {float(item.get('average') or 0.0):.3f} | "
            f"{float(item.get('trend') or 0.0):.3f} | {int(item.get('evidence_count') or 0)} | "
            f"{int(item.get('regression_count') or 0)} | {int(failures.get(cap) or 0)} |"
        )
    lines.extend(
        [
            "",
            "## This Week",
            "",
            f"- Learned: {_join(daily.get('learned') or [])}",
            f"- Applied: {_join(daily.get('applied') or [])}",
            f"- Blocked: {_join(daily.get('blocked') or [])}",
            f"- Next validation: {_join(daily.get('next_validation') or [])}",
            "",
            "## Failure Focus",
            "",
        ]
    )
    if failures:
        for cap, count in sorted(failures.items(), key=lambda item: (-item[1], item[0]))[:8]:
            lines.append(f"- {cap}: {count}")
    else:
        lines.append("- none")
    return "\n".join(lines) + "\n"


def _module_status(runtime: Any, *, scope: ScopeRef) -> dict[str, Any]:
    sources = _safe_sources(runtime)
    enabled_sources = [source for source in sources if bool(getattr(source, "enabled", False))]
    return {
        "external_collection": {
            "enabled": callable(getattr(runtime, "collect_external_sources", None)),
            "evidence": f"{len(enabled_sources)} enabled source(s)",
        },
        "paper_intake": {
            "enabled": all(callable(getattr(runtime, name, None)) for name in ("ingest_paper_source", "promote_collected_paper_candidates")),
            "evidence": f"{_count_records(runtime, scope, ['paper_source', 'paper_extract'])} paper record(s)",
        },
        "autonomous_learning": {
            "enabled": callable(getattr(runtime, "run_autonomy_cycle", None)) and callable(getattr(runtime, "run_autonomous_learning_cycle", None)),
            "evidence": f"{_count_records(runtime, scope, ['learning_loop'])} loop record(s)",
        },
        "autonomous_evolution": {
            "enabled": callable(getattr(runtime, "run_autonomous_evolution", None)),
            "evidence": f"{_count_records(runtime, scope, ['capability_candidate', 'promotion_request'])} evolution record(s)",
        },
        "code_sandbox": {
            "enabled": callable(getattr(runtime, "run_code_sandbox", None)) and callable(getattr(runtime, "propose_code_patch", None)),
            "evidence": f"{_count_records(runtime, scope, ['reflection'])} reflection/report record(s)",
        },
        "knowledge_ingest": {
            "enabled": callable(getattr(runtime, "ingest_knowledge_source", None)),
            "evidence": f"{_count_records(runtime, scope, ['knowledge_unit'])} knowledge unit(s)",
        },
        "skill_candidates": {
            "enabled": callable(getattr(runtime, "extract_skill_candidates", None)),
            "evidence": _skill_candidate_evidence(runtime, scope),
        },
        "autonomy_goal_queue": {
            "enabled": callable(getattr(runtime, "build_autonomy_goal_queue", None)),
            "evidence": f"{_count_records(runtime, scope, ['autonomy_goal_queue'])} queue record(s)",
        },
    }


def _module_status_lines(module_status: dict[str, Any]) -> list[str]:
    labels = {
        "external_collection": "External collection",
        "paper_intake": "Paper intake",
        "autonomous_learning": "Autonomous learning",
        "autonomous_evolution": "Autonomous evolution",
        "code_sandbox": "Code sandbox",
        "knowledge_ingest": "Knowledge ingest",
        "skill_candidates": "Skill candidates",
        "autonomy_goal_queue": "Autonomy goal queue",
    }
    lines: list[str] = []
    for key, label in labels.items():
        item = dict(module_status.get(key) or {})
        enabled = "yes" if item.get("enabled") else "no"
        evidence = str(item.get("evidence") or "")
        lines.append(f"| {label} | {enabled} | {evidence} |")
    return lines


def _safe_sources(runtime: Any) -> list[Any]:
    try:
        sources = getattr(runtime, "sources", None)
        return list(sources.list_sources()) if sources is not None else []
    except Exception:
        return []


def _count_records(runtime: Any, scope: ScopeRef, kinds: list[str]) -> int:
    try:
        return len(runtime.store.list_records(kinds=kinds, scope=scope, limit=500))
    except Exception:
        return 0


def _skill_candidate_evidence(runtime: Any, scope: ScopeRef) -> str:
    try:
        records = runtime.store.list_records(kinds=["skill_candidate"], scope=scope, limit=500)
    except Exception:
        records = []
    statuses: dict[str, int] = {}
    for record in records:
        status = str(record.meta.get("status") or record.status or "candidate")
        statuses[status] = statuses.get(status, 0) + 1
    if not statuses:
        return "0 candidate(s)"
    return ", ".join(f"{status}={count}" for status, count in sorted(statuses.items()))


def _roi_component_lines(roi: dict[str, Any]) -> list[str]:
    components = dict(roi.get("roi_components") or {})
    if not components:
        return ["- none"]
    lines = []
    for key in ("service_stability", "learning_quality", "business_value"):
        item = dict(components.get(key) or {})
        lines.append(f"- {key}: score={float(item.get('score') or 0.0):.3f}, signal={float(item.get('signal') or 0.0):.3f}")
    return lines


def _join(items: list[Any]) -> str:
    values = [str(item) for item in items[:3] if str(item).strip()]
    return " | ".join(values) if values else "none"


def _record_date(record: Any) -> str:
    raw = str(getattr(record, "time", None).created_at if getattr(record, "time", None) else "")
    return raw[:10] if raw else date.min.isoformat()


def _classify(text: str) -> str:
    value = str(text or "").lower()
    if any(term in value for term in ("recall", "检索", "召回")):
        return "search.discovery"
    if any(term in value for term in ("code", "patch", "test", "pytest", "代码")):
        return "code.implementation"
    if "uumit" in value:
        return "operations.uumit"
    if any(term in value for term in ("office", "daily", "日报", "任务")):
        return "office.daily_task"
    if any(term in value for term in ("device", "audio", "播放", "设备")):
        return "device.control"
    if any(term in value for term in ("paper", "research", "论文")):
        return "research.synthesis"
    if any(term in value for term in ("safety", "risk", "rollback", "边界")):
        return "safety.boundary"
    return "proactive.judgment"
