from __future__ import annotations

import json
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
    "regression_watch",
    "learning_playbook",
]

REPORT_ITEM_CHARS = 160


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

    learned, noise_skipped_count = _summaries(today_records, kinds={"learning_goal", "world_signal"}, limit=5)
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
                "noise_skipped_count": noise_skipped_count,
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
        "noise_skipped_count": noise_skipped_count,
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


def _summaries(records: list, *, kinds: set[str], limit: int) -> tuple[list[str], int]:
    items: list[str] = []
    skipped = 0
    for record in records:
        if record.kind not in kinds:
            continue
        text, was_noise = _report_item(record)
        if was_noise:
            skipped += 1
            continue
        if text and _dedupe_key(text) not in {_dedupe_key(item) for item in items}:
            items.append(text)
        if len(items) >= limit:
            break
    return items, skipped


def _applied_promotions(records: list) -> list[str]:
    items = []
    for record in records:
        if record.kind != "promotion_request" or str(record.status or "") != "promoted":
            continue
        text, was_noise = _report_item(record, prefix_capability=False)
        if was_noise:
            continue
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
        text = f"{record.title}: {reason}"
        if _is_noisy_report_text(text):
            continue
        items.append(_one_line(text, limit=REPORT_ITEM_CHARS))
    return items[:5]


def _next_validation(records: list) -> list[str]:
    items = []
    for record in records:
        if record.kind in {"capability_candidate", "learning_playbook", "regression_watch"}:
            text, was_noise = _report_item(record)
            if was_noise:
                continue
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


def _report_item(record: Any, *, prefix_capability: bool = True) -> tuple[str, bool]:
    text, payload = _record_report_text(record)
    if _is_noisy_report_text(text):
        return "", True
    item = _one_line(text, limit=REPORT_ITEM_CHARS)
    if prefix_capability:
        capability = _record_capability(record, text, payload)
        if capability and not item.lower().startswith(f"{capability.lower()}:"):
            item = f"{capability}: {item}"
    return item, False


def _record_report_text(record: Any) -> tuple[str, dict[str, Any]]:
    content = record.content if isinstance(record.content, dict) else {}
    signal = content.get("signal") if isinstance(content.get("signal"), dict) else {}
    if signal:
        return str(signal.get("summary") or signal.get("title") or record.summary or record.title or ""), signal
    return str(record.summary or record.title or ""), {}


def _record_capability(record: Any, text: str, payload: dict[str, Any]) -> str:
    meta = record.meta if isinstance(record.meta, dict) else {}
    explicit = str(payload.get("target_capability") or meta.get("target_capability") or meta.get("capability") or "")
    return explicit or _classify_report_capability(text)


def _classify_report_capability(text: str) -> str:
    value = str(text or "").lower()
    if any(term in value for term in ("health", "timeout", "8091", "systemd", "gateway", "rpc", "端口", "超时", "健康")):
        return "ops.health"
    if any(term in value for term in ("recall", "ranking", "retrieve", "检索", "召回", "排序", "相关性")):
        return "memory.recall"
    if any(term in value for term in ("tool", "route", "routing", "hook", "工具", "路由")):
        return "tool.routing"
    if any(term in value for term in ("code", "patch", "diff", "test", "pytest", "traceback", "exception", "代码", "回归")):
        return "code.implementation"
    if any(term in value for term in ("prompt", "system prompt", "strategy", "policy", "策略")):
        return "policy.judgment"
    if any(term in value for term in ("source", "paper", "rss", "news", "论文", "新闻")):
        return "knowledge.intake"
    return "proactive.judgment"


def _is_noisy_report_text(text: str) -> bool:
    raw = str(text or "").strip()
    compact = " ".join(raw.lower().split())
    if not compact:
        return True
    if _looks_like_tool_message_json(raw):
        return True
    if "assistant:" in compact and "user:" in compact and len(compact) > 500:
        return True
    if compact.count("http") > 12:
        return True
    if len(compact) > 2400 and any(marker in compact for marker in ("toolcall", "arguments", "message", "system prompt")):
        return True
    return False


def _looks_like_tool_message_json(text: str) -> bool:
    value = str(text or "").strip()
    if not value.startswith("{"):
        return False
    try:
        payload = json.loads(value)
    except json.JSONDecodeError:
        compact = " ".join(value.lower().split())
        return '"type":"toolcall"' in compact or ("\"arguments\"" in compact and "\"message\"" in compact and len(compact) > 300)
    if not isinstance(payload, dict):
        return False
    payload_type = str(payload.get("type") or "").lower()
    name = str(payload.get("name") or "").lower()
    has_arguments = "arguments" in payload
    if payload_type in {"toolcall", "tool_call"} and has_arguments:
        return True
    if has_arguments and name in {"message", "send", "tool"}:
        return True
    return False


def _dedupe_key(text: str) -> str:
    return " ".join(str(text or "").lower().split())


def _one_line(text: str, *, limit: int = 180) -> str:
    value = " ".join(str(text or "").split())
    if len(value) > limit:
        value = value[: max(0, limit - 3)].rstrip() + "..."
    return value
