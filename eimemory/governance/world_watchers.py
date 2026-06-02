from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from hashlib import sha256
from pathlib import Path
from typing import Any

from eimemory.governance.learning_state import append_learning_record_once, stable_semantic_key
from eimemory.metadata import business_metadata
from eimemory.models.records import RecordEnvelope, ScopeRef

MAX_SIGNAL_TITLE_CHARS = 120
MAX_SIGNAL_SUMMARY_CHARS = 360
NOISE_SUMMARY_CHARS = 1200
NOISE_LINE_COUNT = 18


@dataclass(slots=True)
class SourceWatch:
    name: str
    kind: str
    query: str = ""
    enabled: bool = False
    dry_run: bool = True
    cadence: str = "daily"
    authority_tier: str = "L0"
    max_items: int = 20
    last_seen: str = ""
    dedupe_key: str = ""

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "SourceWatch":
        return cls(
            name=str(data.get("name") or data.get("kind") or "watch"),
            kind=str(data.get("kind") or ""),
            query=str(data.get("query") or ""),
            enabled=bool(data.get("enabled", False)),
            dry_run=bool(data.get("dry_run", True)),
            cadence=str(data.get("cadence") or "daily"),
            authority_tier=str(data.get("authority_tier") or "L0"),
            max_items=max(0, int(data.get("max_items") or 20)),
            last_seen=str(data.get("last_seen") or ""),
            dedupe_key=str(data.get("dedupe_key") or ""),
        )


def collect_world_signals(
    runtime: Any,
    *,
    scope: dict[str, Any] | ScopeRef | None = None,
    watches: list[SourceWatch | dict[str, Any]] | None = None,
    dry_run: bool = True,
    loop_id: str = "manual",
) -> dict[str, Any]:
    scope_ref = scope if isinstance(scope, ScopeRef) else ScopeRef.from_dict(scope)
    normalized = [watch if isinstance(watch, SourceWatch) else SourceWatch.from_dict(watch) for watch in (watches or default_watches())]
    signals: list[dict[str, Any]] = []
    persisted_ids: list[str] = []
    updated_ids: list[str] = []
    skipped_disabled = 0
    duplicate_count = 0
    existing_records = _existing_signal_records(runtime, scope=scope_ref)
    seen_hashes = set(existing_records)
    for watch in normalized:
        if not watch.enabled:
            skipped_disabled += 1
            continue
        watch_signals = _collect_watch(runtime, scope=scope_ref, watch=watch)[: watch.max_items]
        normalized_signals: list[dict[str, Any]] = []
        seen_local_hashes: set[str] = set()
        for raw_signal in watch_signals:
            signal = _normalize_signal(raw_signal, watch=watch)
            if _is_noise_signal(signal):
                continue
            local_hash = _signal_hash(watch, signal)
            if local_hash in seen_local_hashes:
                duplicate_count += 1
                continue
            seen_local_hashes.add(local_hash)
            normalized_signals.append(signal)
        for signal in normalized_signals:
            signal_hash = _signal_hash(watch, signal)
            if signal_hash in seen_hashes:
                duplicate_count += 1
                existing = existing_records.get(signal_hash)
                if existing is not None and not dry_run and not watch.dry_run:
                    _increment_repeat_count(runtime, existing, signal)
                    updated_ids.append(existing.record_id)
                continue
            seen_hashes.add(signal_hash)
            payload = {
                **signal,
                "watch_name": watch.name,
                "source_kind": watch.kind,
                "authority_tier": watch.authority_tier,
                "signal_hash": signal_hash,
                "evidence_tier": signal.get("evidence_tier") or _evidence_tier(watch.kind),
            }
            signals.append(payload)
            if not dry_run and not watch.dry_run:
                record = append_learning_record_once(
                    runtime,
                    kind="world_signal",
                    title=str(payload.get("title") or f"World signal: {watch.name}"),
                    summary=str(payload.get("summary") or ""),
                    scope=scope_ref,
                    loop_id=loop_id,
                    step_name="world_watch",
                    semantic_key=signal_hash,
                    authority_tier=watch.authority_tier,
                    status="candidate",
                    content={"signal": payload},
                    meta={
                        "watch_name": watch.name,
                        "source_kind": watch.kind,
                        "signal_type": payload.get("signal_type"),
                        "target_capability": payload.get("target_capability"),
                        "signal_hash": signal_hash,
                        "evidence_tier": payload.get("evidence_tier"),
                        "repeat_count": int(payload.get("repeat_count") or 1),
                    },
                )
                persisted_ids.append(record.record_id)
    return {
        "ok": True,
        "dry_run": bool(dry_run),
        "watch_count": len(normalized),
        "skipped_disabled_count": skipped_disabled,
        "signal_count": len(signals),
        "duplicate_count": duplicate_count,
        "persisted_record_ids": persisted_ids,
        "updated_record_ids": updated_ids,
        "signals": signals,
    }


def default_watches() -> list[SourceWatch]:
    return [
        SourceWatch(name="recent outcomes", kind="local_outcome_trace", enabled=True, dry_run=False),
        SourceWatch(name="recall gaps", kind="local_recall_gap", enabled=True, dry_run=False),
        SourceWatch(name="local eval", kind="local_eval", enabled=True, dry_run=False),
        SourceWatch(name="user goals", kind="user_goal_memory", enabled=True, dry_run=False),
        SourceWatch(name="repo scan", kind="local_repo", enabled=False, dry_run=True),
        SourceWatch(name="tool registry", kind="tool_registry", enabled=False, dry_run=True),
        SourceWatch(name="github releases", kind="github_releases", enabled=False, dry_run=True, authority_tier="L2"),
        SourceWatch(name="research feed", kind="research_feed", enabled=False, dry_run=True),
        SourceWatch(name="web search", kind="web_search", enabled=False, dry_run=True),
    ]


def _collect_watch(runtime: Any, *, scope: ScopeRef, watch: SourceWatch) -> list[dict[str, Any]]:
    if watch.kind == "local_outcome_trace":
        return _signals_from_outcomes(runtime, scope=scope, watch=watch)
    if watch.kind == "local_recall_gap":
        return _signals_from_records(runtime, scope=scope, kinds=["unknown"], watch=watch, signal_type="recall_gap")
    if watch.kind == "local_eval":
        return _signals_from_records(runtime, scope=scope, kinds=["replay_result"], watch=watch, signal_type="eval_result")
    if watch.kind == "user_goal_memory":
        return _signals_from_user_goals(runtime, scope=scope, watch=watch)
    if watch.kind == "local_repo":
        return _signals_from_repo(runtime, watch=watch)
    if watch.kind == "tool_registry":
        return [{"signal_type": "tool_registry", "title": "Tool registry dry-run", "summary": "Installed tool inventory watcher is configured but local inventory scan is disabled by default.", "confidence": 0.4}]
    return [{"signal_type": "disabled_public_adapter", "title": f"{watch.kind} disabled", "summary": "Public/network watcher adapter is present but disabled/dry-run safe.", "confidence": 0.2}]


def _signals_from_outcomes(runtime: Any, *, scope: ScopeRef, watch: SourceWatch) -> list[dict[str, Any]]:
    signals_by_key: dict[str, dict[str, Any]] = {}
    for record in runtime.store.list_records(kinds=["reflection"], scope=scope, limit=watch.max_items * 5):
        meta = business_metadata(record.meta)
        if str(meta.get("report_type") or "") != "outcome_trace":
            continue
        label = str(meta.get("primary_label") or "unknown")
        if label == "success":
            continue
        summary = record.summary or str(record.content.get("input_summary") or "")
        key = stable_semantic_key("bad_outcome_signal", label, _normalize_text(summary), _capability_from_label(label, summary))
        if key not in signals_by_key:
            signals_by_key[key] = {
                "record_id": record.record_id,
                "source_record_ids": [record.record_id],
                "signal_type": "bad_outcome",
                "title": f"Bad outcome: {label}",
                "summary": summary,
                "target_capability": _capability_from_label(label, summary),
                "repeat_count": 1,
                "confidence": 0.8,
                "impact": 0.75,
                "urgency": 0.7,
                "evidence_tier": "T0",
            }
        else:
            item = signals_by_key[key]
            item["repeat_count"] = int(item.get("repeat_count") or 1) + 1
            item["source_record_ids"] = [*list(item.get("source_record_ids") or []), record.record_id]
            item["impact"] = min(1.0, float(item.get("impact") or 0.75) + 0.05)
            item["urgency"] = min(1.0, float(item.get("urgency") or 0.7) + 0.05)
    return sorted(signals_by_key.values(), key=lambda item: (-int(item.get("repeat_count") or 1), str(item.get("title") or "")))


def _signals_from_records(
    runtime: Any,
    *,
    scope: ScopeRef,
    kinds: list[str],
    watch: SourceWatch,
    signal_type: str,
) -> list[dict[str, Any]]:
    return [
        {
            "record_id": record.record_id,
            "signal_type": signal_type,
            "title": record.title,
            "summary": record.summary,
            "target_capability": _classify_capability(f"{record.title} {record.summary}", fallback="memory.recall" if signal_type == "recall_gap" else "proactive.judgment"),
            "confidence": 0.65,
            "evidence_tier": _evidence_tier(watch.kind),
        }
        for record in runtime.store.list_records(kinds=kinds, scope=scope, limit=watch.max_items)
    ]


def _signals_from_user_goals(runtime: Any, *, scope: ScopeRef, watch: SourceWatch) -> list[dict[str, Any]]:
    memories = runtime.store.list_records(kinds=["memory"], scope=scope, limit=watch.max_items * 3)
    signals = []
    for record in memories:
        text = f"{record.title} {record.summary} {record.detail}".lower()
        if any(term in text for term in ("goal", "目标", "计划", "长期", "重要")):
            signals.append(
                {
                    "record_id": record.record_id,
                    "signal_type": "user_goal_memory",
                    "title": record.title,
                    "summary": record.summary,
                    "target_capability": _classify_capability(text, fallback="proactive.judgment"),
                    "confidence": 0.55,
                    "evidence_tier": "T2",
                }
            )
    return signals[: watch.max_items]


def _signals_from_repo(runtime: Any, *, watch: SourceWatch) -> list[dict[str, Any]]:
    root = Path(str(watch.query or Path.cwd()))
    if not root.exists() or not root.is_dir():
        root = Path.cwd()
    signals = []
    for path in list(root.rglob("*.py"))[:200]:
        if any(part in {".git", ".venv", "__pycache__"} for part in path.parts):
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        if "TODO" in text or "FIXME" in text:
            signals.append(
                {
                    "signal_type": "local_repo",
                    "title": f"Repo maintenance signal: {path.name}",
                    "summary": f"{path.as_posix()} contains TODO/FIXME markers",
                    "target_capability": "code.implementation",
                    "confidence": 0.5,
                    "evidence_tier": "T2",
                }
            )
        if len(signals) >= watch.max_items:
            break
    return signals


def _existing_signal_records(runtime: Any, *, scope: ScopeRef) -> dict[str, RecordEnvelope]:
    records_by_hash: dict[str, RecordEnvelope] = {}
    offset = 0
    while True:
        page = runtime.store.list_records(kinds=["world_signal"], scope=scope, limit=500, offset=offset)
        for record in page:
            value = str(record.meta.get("signal_hash") or record.content.get("signal_hash") or "")
            if value:
                records_by_hash[value] = record
        if len(page) < 500:
            break
        offset += len(page)
    return records_by_hash


def _signal_hash(watch: SourceWatch, signal: dict[str, Any]) -> str:
    if watch.dedupe_key and signal.get(watch.dedupe_key):
        seed = str(signal.get(watch.dedupe_key))
    else:
        seed = json.dumps(
            {
                "watch": watch.name,
                "kind": watch.kind,
                "signal_type": signal.get("signal_type"),
                "title": "" if signal.get("summary") else signal.get("title"),
                "summary": _normalize_text(str(signal.get("summary") or "")),
                "target_capability": signal.get("target_capability"),
            },
            sort_keys=True,
            ensure_ascii=False,
            default=str,
        )
    return sha256(seed.encode("utf-8")).hexdigest()[:24]


def _increment_repeat_count(runtime: Any, record: RecordEnvelope, signal: dict[str, Any]) -> None:
    content = record.content if isinstance(record.content, dict) else {}
    payload = content.get("signal") if isinstance(content.get("signal"), dict) else {}
    current = max(1, int(payload.get("repeat_count") or record.meta.get("repeat_count") or 1))
    incoming = max(1, int(signal.get("repeat_count") or 1))
    payload["repeat_count"] = current + incoming
    payload["source_record_ids"] = sorted({*list(payload.get("source_record_ids") or []), *list(signal.get("source_record_ids") or []), str(signal.get("record_id") or "")} - {""})
    payload["impact"] = min(1.0, float(payload.get("impact") or 0.75) + 0.05)
    payload["urgency"] = min(1.0, float(payload.get("urgency") or 0.7) + 0.05)
    record.content = {**content, "signal": payload}
    record.meta["repeat_count"] = payload["repeat_count"]
    record.meta["last_seen_loop"] = str(signal.get("loop_id") or "")
    runtime.store.rewrite(record)


def _normalize_text(text: str) -> str:
    return " ".join(str(text or "").strip().lower().split())[:240]


def _normalize_signal(signal: dict[str, Any], *, watch: SourceWatch) -> dict[str, Any]:
    payload = dict(signal or {})
    title = _compact_text(str(payload.get("title") or f"World signal: {watch.name}"), limit=MAX_SIGNAL_TITLE_CHARS)
    raw_summary = str(payload.get("summary") or "")
    summary = _compact_text(raw_summary, limit=MAX_SIGNAL_SUMMARY_CHARS)
    target_capability = _classify_capability(
        f"{title} {raw_summary} {payload.get('signal_type') or ''}",
        fallback=str(payload.get("target_capability") or "proactive.judgment"),
    )
    payload["title"] = title
    payload["summary"] = summary
    payload["target_capability"] = target_capability
    payload["raw_summary_chars"] = len(raw_summary)
    payload["summary_truncated"] = len(summary) < len(raw_summary.strip())
    if raw_summary and (len(raw_summary) > NOISE_SUMMARY_CHARS or raw_summary.count("\n") > NOISE_LINE_COUNT):
        payload["noise_penalty"] = 0.25
        payload["confidence"] = round(max(0.1, float(payload.get("confidence") or 0.5) - 0.2), 3)
    return payload


def _compact_text(text: str, *, limit: int) -> str:
    normalized = " ".join(str(text or "").split())
    if len(normalized) <= limit:
        return normalized
    return normalized[: max(0, limit - 3)].rstrip() + "..."


def _is_noise_signal(signal: dict[str, Any]) -> bool:
    text = f"{signal.get('title') or ''} {signal.get('summary') or ''}".lower()
    if not text.strip():
        return True
    if "assistant:" in text and "user:" in text and len(text) > 500:
        return True
    if text.count("http") > 12:
        return True
    if float(signal.get("confidence") or 0.0) < 0.15:
        return True
    return False


def _classify_capability(text: str, *, fallback: str) -> str:
    value = str(text or "").lower()
    if any(term in value for term in ("health", "timeout", "8091", "service", "systemd", "gateway", "rpc", "端口", "超时", "健康")):
        return "ops.health"
    if any(term in value for term in ("recall", "ranking", "retrieve", "检索", "召回", "排序", "相关性")):
        return "memory.recall"
    if any(term in value for term in ("tool", "route", "routing", "hook", "工具", "路由")):
        return "tool.routing"
    if any(term in value for term in ("code", "patch", "diff", "test", "pytest", "traceback", "exception", "代码", "回归")):
        return "code.implementation"
    if any(term in value for term in ("prompt", "system prompt", "策略", "policy")):
        return "policy.judgment"
    if any(term in value for term in ("source", "paper", "rss", "news", "论文", "新闻")):
        return "knowledge.intake"
    return fallback or "proactive.judgment"


def _evidence_tier(kind: str) -> str:
    if kind == "local_outcome_trace":
        return "T0"
    if kind == "local_eval":
        return "T1"
    if kind.startswith("local") or kind in {"tool_registry", "user_goal_memory"}:
        return "T2"
    if kind.startswith("github"):
        return "T3"
    if kind.startswith("research"):
        return "T4"
    return "T5"


def _capability_from_label(label: str, text: str) -> str:
    value = f"{label} {text}".lower()
    if "tool" in value:
        return "tool.routing"
    if "stale" in value or "recall" in value:
        return "memory.recall"
    if "unsafe" in value or "risk" in value:
        return "safety.judgment"
    return "proactive.judgment"
