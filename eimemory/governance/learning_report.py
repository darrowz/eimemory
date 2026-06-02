from __future__ import annotations

from datetime import datetime
from typing import Any

from eimemory.core.clock import now_iso
from eimemory.governance.learning_state import append_learning_record_once, stable_semantic_key
from eimemory.models.records import ScopeRef


REPORT_KINDS = [
    "learning_loop",
    "world_signal",
    "learning_goal",
    "capability_candidate",
    "promotion_request",
    "capability_score",
    "regression_watch",
    "learning_playbook",
]


def build_learning_daily_report(
    runtime: Any,
    *,
    scope: dict[str, Any] | ScopeRef | None = None,
    report_date: str | None = None,
    persist: bool = True,
    limit: int = 300,
) -> dict[str, Any]:
    scope_ref = scope if isinstance(scope, ScopeRef) else ScopeRef.from_dict(scope)
    date = report_date or now_iso()[:10]
    records = _recent_learning_records(runtime, scope=scope_ref, limit=limit)
    today_records = [record for record in records if _record_date(record) == date]
    if not today_records:
        today_records = records[:50]

    learned = _summaries(today_records, kinds={"learning_goal", "world_signal"}, limit=5)
    applied = _applied_promotions(today_records)
    blocked = _blocked_promotions(today_records)
    next_validation = _next_validation(today_records)
    summary = _compact_report_summary(learned, applied, blocked, next_validation)

    record_id = ""
    if persist:
        record = append_learning_record_once(
            runtime,
            kind="reflection",
            title=f"Autonomous learning daily report {date}",
            summary=summary,
            scope=scope_ref,
            loop_id=f"daily_report_{date}",
            step_name="learning_daily_report",
            semantic_key=stable_semantic_key("learning_daily_report", date, scope_ref),
            authority_tier="L0",
            status="active",
            content={
                "report_type": "autonomous_learning_daily_report",
                "date": date,
                "learned": learned,
                "applied": applied,
                "blocked": blocked,
                "next_validation": next_validation,
            },
            meta={
                "report_type": "autonomous_learning_daily_report",
                "date": date,
                "learned_count": len(learned),
                "applied_count": len(applied),
                "blocked_count": len(blocked),
            },
        )
        record_id = record.record_id

    return {
        "ok": True,
        "report_type": "autonomous_learning_daily_report",
        "date": date,
        "persisted": bool(persist),
        "persisted_record_id": record_id,
        "learned": learned,
        "applied": applied,
        "blocked": blocked,
        "next_validation": next_validation,
        "summary": summary,
    }


def _recent_learning_records(runtime: Any, *, scope: ScopeRef, limit: int) -> list:
    records = []
    offset = 0
    page_size = min(500, max(1, int(limit or 300)))
    while len(records) < limit:
        page = runtime.store.list_records(kinds=REPORT_KINDS, scope=scope, limit=min(page_size, limit - len(records)), offset=offset)
        records.extend(page)
        if len(page) < page_size:
            break
        offset += len(page)
    return records


def _record_date(record: Any) -> str:
    raw = str(getattr(record, "time", None).created_at if getattr(record, "time", None) else "")
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).date().isoformat()
    except ValueError:
        return raw[:10]


def _summaries(records: list, *, kinds: set[str], limit: int) -> list[str]:
    items: list[str] = []
    for record in records:
        if record.kind not in kinds:
            continue
        text = _one_line(record.summary or record.title)
        if text and text not in items:
            items.append(text)
        if len(items) >= limit:
            break
    return items


def _applied_promotions(records: list) -> list[str]:
    items = []
    for record in records:
        if record.kind != "promotion_request" or str(record.status or "") != "promoted":
            continue
        text = _one_line(record.summary or record.title)
        target = str((record.content or {}).get("candidate_id") or record.meta.get("candidate_id") or "")
        items.append(f"{text} ({target})" if target else text)
    return items[:5]


def _blocked_promotions(records: list) -> list[str]:
    items = []
    for record in records:
        if record.kind != "promotion_request" or str(record.status or "") != "blocked":
            continue
        content = record.content if isinstance(record.content, dict) else {}
        side_effect = content.get("side_effect") if isinstance(content.get("side_effect"), dict) else {}
        reason = side_effect.get("blocked_reason") or ",".join((content.get("gate") or {}).get("blocked_reasons") or [])
        items.append(_one_line(f"{record.title}: {reason}"))
    return items[:5]


def _next_validation(records: list) -> list[str]:
    items = []
    for record in records:
        if record.kind in {"capability_candidate", "learning_playbook", "regression_watch"}:
            text = _one_line(record.summary or record.title)
            if text and text not in items:
                items.append(text)
        if len(items) >= 5:
            break
    if not items:
        items.append("Run the next nightly learning cycle and compare promotion/regression counts.")
    return items


def _compact_report_summary(learned: list[str], applied: list[str], blocked: list[str], next_validation: list[str]) -> str:
    return (
        f"Learned: {_join_short(learned)}; "
        f"Applied: {_join_short(applied)}; "
        f"Blocked: {_join_short(blocked)}; "
        f"Next: {_join_short(next_validation)}"
    )


def _join_short(items: list[str]) -> str:
    if not items:
        return "none"
    return " | ".join(items[:3])


def _one_line(text: str) -> str:
    value = " ".join(str(text or "").split())
    if len(value) > 180:
        value = value[:177].rstrip() + "..."
    return value
