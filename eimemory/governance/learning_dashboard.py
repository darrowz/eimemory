from __future__ import annotations

from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from eimemory.core.clock import now_iso
from eimemory.governance.capability_ledger import build_capability_ledger
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
) -> dict[str, Any]:
    scope_ref = scope if isinstance(scope, ScopeRef) else ScopeRef.from_dict(scope)
    ensure_all_seeded(runtime, scope=scope_ref, loop_id="dashboard_seed")
    start = _week_start(week_start)
    ledger = build_capability_ledger(runtime, scope=scope_ref)
    daily = build_learning_daily_report(runtime, scope=scope_ref, persist=False)
    failures = _failure_breakdown(runtime, scope=scope_ref, since=start)
    markdown = _render_markdown(start=start, ledger=ledger, daily=daily, failures=failures)
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
            title=f"Autonomous learning weekly dashboard {start}",
            summary=f"Weekly dashboard for {len((ledger.get('capabilities') or {}))} capabilities.",
            scope=scope_ref,
            loop_id=f"weekly_dashboard_{start}",
            step_name="learning_dashboard",
            semantic_key=stable_semantic_key("learning_dashboard", start, scope_ref),
            authority_tier="L0",
            status="active",
            content={"report_type": "autonomous_learning_weekly_dashboard", "week_start": start, "markdown": markdown, "ledger": ledger, "failures": failures},
            meta={"report_type": "autonomous_learning_weekly_dashboard", "week_start": start, "capability_count": len(ledger.get("capabilities") or {})},
        )
        record_id = record.record_id
    return {
        "ok": True,
        "report_type": "autonomous_learning_weekly_dashboard",
        "week_start": start,
        "persisted": bool(persist),
        "persisted_record_id": record_id,
        "output_path": written_path,
        "output_error": output_error,
        "markdown": markdown,
        "ledger": ledger,
        "failure_breakdown": failures,
    }


def _week_start(value: str | None) -> str:
    if value:
        return value[:10]
    today = datetime.fromisoformat(now_iso().replace("Z", "+00:00")).date()
    monday = today - timedelta(days=today.weekday())
    return monday.isoformat()


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


def _render_markdown(*, start: str, ledger: dict[str, Any], daily: dict[str, Any], failures: dict[str, int]) -> str:
    lines = [
        f"# eimemory autonomous learning weekly dashboard ({start})",
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
