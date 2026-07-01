from __future__ import annotations

import json
import sys
import time
from dataclasses import asdict, dataclass
from hashlib import sha256
from pathlib import Path
from typing import Any

from eimemory.core.clock import now_iso
from eimemory.governance.learning_state import append_learning_record_once, stable_semantic_key
from eimemory.governance.goal_registry import derive_goal_signals, load_goal_registry
from eimemory.governance.memory_graph import build_incremental_memory_edges
from eimemory.governance.supervisor import persist_supervisor_summary, supervisor_summary
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
    seen_record_ids: tuple[str, ...] = ()

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
            seen_record_ids=tuple(str(item) for item in list(data.get("seen_record_ids") or []) if str(item)),
        )


def collect_world_signals(
    runtime: Any,
    *,
    scope: dict[str, Any] | ScopeRef | None = None,
    watches: list[SourceWatch | dict[str, Any]] | None = None,
    dry_run: bool = True,
    loop_id: str = "manual",
) -> dict[str, Any]:
    started = time.perf_counter()
    memory_start = _memory_peak_bytes()
    scope_ref = scope if isinstance(scope, ScopeRef) else ScopeRef.from_dict(scope)
    normalized = [_with_cursor(runtime, scope=scope_ref, watch=watch if isinstance(watch, SourceWatch) else SourceWatch.from_dict(watch)) for watch in (watches or default_watches())]
    signals: list[dict[str, Any]] = []
    persisted_ids: list[str] = []
    updated_ids: list[str] = []
    watcher_cursors: list[dict[str, str]] = []
    skipped_disabled = 0
    duplicate_count = 0
    seen_hashes: dict[str, RecordEnvelope | None] = {}
    for watch in normalized:
        if not watch.enabled:
            skipped_disabled += 1
            continue
        watch_signals = _collect_watch(runtime, scope=scope_ref, watch=watch)[: watch.max_items]
        high_watermark = _high_watermark_for_signals(runtime, scope=scope_ref, watch=watch, signals=watch_signals) or watch.last_seen
        seen_record_ids = _cursor_seen_record_ids(runtime, scope=scope_ref, watch=watch, signals=watch_signals, high_watermark=high_watermark)
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
            existing = seen_hashes.get(signal_hash) if signal_hash in seen_hashes else _existing_signal_record(runtime, scope=scope_ref, signal_hash=signal_hash)
            if signal_hash in seen_hashes or existing is not None:
                duplicate_count += 1
                seen_hashes[signal_hash] = existing
                if existing is not None and not dry_run and not watch.dry_run:
                    if _increment_repeat_count(runtime, existing, signal):
                        updated_ids.append(existing.record_id)
                continue
            seen_hashes[signal_hash] = None
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
        if not dry_run and not watch.dry_run:
            _save_watch_cursor(runtime, scope=scope_ref, watch=watch, high_watermark=high_watermark, seen_record_ids=seen_record_ids)
        watcher_cursors.append({"watch_name": watch.name, "kind": watch.kind, "last_seen": watch.last_seen, "high_watermark": high_watermark})
    edge_report = build_incremental_memory_edges(runtime, scope=scope_ref, dry_run=dry_run)
    memory_peak = max(memory_start, _memory_peak_bytes())
    duration_ms = int((time.perf_counter() - started) * 1000)
    produced_count = len(persisted_ids) + len(updated_ids) + int(edge_report.get("edge_count") or 0)
    summary = supervisor_summary(
        command="learn-watch",
        ok=True,
        duration_ms=duration_ms,
        memory_peak=int(memory_peak or 0),
        produced_count=produced_count,
        promoted_count=0,
        rolled_back_count=0,
    )
    if not dry_run:
        persist_supervisor_summary(runtime, scope=scope_ref, summary=summary)
    return {
        "ok": True,
        "dry_run": bool(dry_run),
        "watch_count": len(normalized),
        "skipped_disabled_count": skipped_disabled,
        "signal_count": len(signals),
        "duplicate_count": duplicate_count,
        "persisted_record_ids": persisted_ids,
        "updated_record_ids": updated_ids,
        "watcher_cursors": watcher_cursors,
        "duration_ms": duration_ms,
        "memory_peak": int(memory_peak or 0),
        "produced_count": produced_count,
        "promoted_count": 0,
        "rolled_back_count": 0,
        "edge_builder": edge_report,
        "supervisor_summary": summary,
        "signals": signals,
    }


def _memory_peak_bytes() -> int:
    try:
        import resource
    except ImportError:
        return 0
    try:
        peak = int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
    except Exception:
        return 0
    if sys.platform == "darwin":
        return peak
    return peak * 1024


def default_watches() -> list[SourceWatch]:
    return [
        SourceWatch(name="recent outcomes", kind="local_outcome_trace", enabled=True, dry_run=False),
        SourceWatch(name="outcome weakness", kind="outcome_weakness", enabled=True, dry_run=False),
        SourceWatch(name="recall gaps", kind="local_recall_gap", enabled=True, dry_run=False),
        SourceWatch(name="local eval", kind="local_eval", enabled=True, dry_run=False),
        SourceWatch(name="user goals", kind="user_goal_memory", enabled=True, dry_run=False),
        SourceWatch(name="external intake summary", kind="external_intake_summary", enabled=True, dry_run=False),
        SourceWatch(name="local state", kind="local_state", enabled=True, dry_run=False),
        SourceWatch(name="goal registry gap", kind="goal_registry_gap", enabled=False, dry_run=True),
        SourceWatch(name="stale asset", kind="stale_asset", enabled=True, dry_run=False),
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
    if watch.kind == "external_intake_summary":
        return _signals_from_external_intake(runtime, scope=scope, watch=watch)
    if watch.kind == "local_state":
        return _signals_from_local_state(runtime, scope=scope, watch=watch)
    if watch.kind == "outcome_weakness":
        return _signals_from_outcome_weakness(runtime, scope=scope, watch=watch)
    if watch.kind == "goal_registry_gap":
        return _signals_from_goal_registry(runtime, scope=scope, watch=watch)
    if watch.kind == "stale_asset":
        return _signals_from_stale_assets(runtime, scope=scope, watch=watch)
    if watch.kind == "local_repo":
        return _signals_from_repo(runtime, watch=watch)
    if watch.kind == "tool_registry":
        return [{"signal_type": "tool_registry", "title": "Tool registry dry-run", "summary": "Installed tool inventory watcher is configured but local inventory scan is disabled by default.", "confidence": 0.4}]
    return [{"signal_type": "disabled_public_adapter", "title": f"{watch.kind} disabled", "summary": "Public/network watcher adapter is present but disabled/dry-run safe.", "confidence": 0.2}]


def _signals_from_outcomes(runtime: Any, *, scope: ScopeRef, watch: SourceWatch) -> list[dict[str, Any]]:
    signals_by_key: dict[str, dict[str, Any]] = {}
    for record in runtime.store.list_records(kinds=["reflection"], scope=scope, limit=watch.max_items * 5):
        if not _record_after_cursor(record, watch):
            continue
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
        if _record_after_cursor(record, watch)
    ]


def _signals_from_user_goals(runtime: Any, *, scope: ScopeRef, watch: SourceWatch) -> list[dict[str, Any]]:
    memories = runtime.store.list_records(kinds=["memory"], scope=scope, limit=watch.max_items * 3)
    signals = []
    for record in memories:
        if not _record_after_cursor(record, watch):
            continue
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


def _signals_from_external_intake(runtime: Any, *, scope: ScopeRef, watch: SourceWatch) -> list[dict[str, Any]]:
    kinds = ["knowledge_candidate", "paper_source", "paper_extract", "knowledge_page", "claim_card", "source_watch", "news"]
    records = runtime.store.list_records(kinds=kinds, scope=scope, limit=max(1, watch.max_items) * 4)
    by_kind: dict[str, list[Any]] = {}
    for record in records:
        if not _record_after_cursor(record, watch):
            continue
        if _is_internal_watch_record(record):
            continue
        by_kind.setdefault(record.kind, []).append(record)
    signals = []
    for kind, items in sorted(by_kind.items()):
        if not items:
            continue
        sample = " | ".join(_compact_text(item.summary or item.title, limit=90) for item in items[:3] if (item.summary or item.title))
        signals.append(
            {
                "signal_type": "external_intake_summary",
                "title": f"External intake updated: {kind}",
                "summary": f"{len(items)} recent {kind} records. {sample}",
                "target_capability": "research.synthesis" if kind in {"paper_source", "paper_extract", "knowledge_page", "claim_card"} else "knowledge.intake",
                "confidence": 0.62,
                "impact": 0.55,
                "urgency": 0.35,
                "evidence_tier": "T3",
                "source_record_ids": [item.record_id for item in items[:10]],
            }
        )
        if len(signals) >= watch.max_items:
            break
    return signals


def _is_internal_watch_record(record: RecordEnvelope) -> bool:
    meta = business_metadata(record.meta)
    return str(meta.get("report_type") or "") in {"memory_graph_cursor", "supervisor_run"}


def _signals_from_local_state(runtime: Any, *, scope: ScopeRef, watch: SourceWatch) -> list[dict[str, Any]]:
    loops = runtime.store.list_records(kinds=["learning_loop"], scope=scope, limit=20)
    regressions = runtime.store.list_records(kinds=["regression_watch"], scope=scope, limit=20)
    blocked = [record for record in runtime.store.list_records(kinds=["promotion_request"], scope=scope, limit=50) if str(record.status or "") == "blocked"]
    signals = []
    active = [loop for loop in loops if str(loop.status or "") in {"running", "collecting", "researching", "experimenting", "evaluating", "promoting"}]
    if active:
        signals.append(
            {
                "signal_type": "local_state",
                "title": "Active autonomous learning loop is still open",
                "summary": f"{len(active)} active learning loop(s) need completion or force handling.",
                "target_capability": "ops.health",
                "confidence": 0.7,
                "impact": 0.65,
                "urgency": 0.7,
                "evidence_tier": "T1",
                "source_record_ids": [item.record_id for item in active[:5]],
            }
        )
    if regressions:
        signals.append(
            {
                "signal_type": "local_state",
                "title": "Recent regression watch activity",
                "summary": f"{len(regressions)} regression watch records exist; review before further promotion.",
                "target_capability": "safety.boundary",
                "confidence": 0.65,
                "impact": 0.65,
                "urgency": 0.55,
                "evidence_tier": "T1",
                "source_record_ids": [item.record_id for item in regressions[:5]],
            }
        )
    if blocked:
        signals.append(
            {
                "signal_type": "local_state",
                "title": "Blocked promotions need review",
                "summary": f"{len(blocked)} blocked promotion request(s) are available for proactive follow-up.",
                "target_capability": "proactive.judgment",
                "confidence": 0.68,
                "impact": 0.6,
                "urgency": 0.5,
                "evidence_tier": "T1",
                "source_record_ids": [item.record_id for item in blocked[:5]],
            }
        )
    if not signals:
        signals.append(
            {
                "signal_type": "local_state",
                "title": "Local autonomous learning state is quiet",
                "summary": "No active loops, regressions, or blocked promotions were found in the local store.",
                "target_capability": "ops.health",
                "confidence": 0.35,
                "impact": 0.2,
                "urgency": 0.1,
                "evidence_tier": "T2",
            }
        )
    return signals[: watch.max_items]


def _signals_from_outcome_weakness(runtime: Any, *, scope: ScopeRef, watch: SourceWatch) -> list[dict[str, Any]]:
    signals = _signals_from_outcomes(runtime, scope=scope, watch=watch)
    table_signals = _signals_from_event_outcomes(runtime, scope=scope, watch=watch)
    grouped: dict[str, dict[str, Any]] = {}
    for signal in [*signals, *table_signals]:
        key = stable_semantic_key(signal.get("target_capability"), signal.get("summary"))
        if key not in grouped:
            grouped[key] = signal
            continue
        grouped[key]["repeat_count"] = int(grouped[key].get("repeat_count") or 1) + int(signal.get("repeat_count") or 1)
        grouped[key]["source_record_ids"] = sorted({*list(grouped[key].get("source_record_ids") or []), *list(signal.get("source_record_ids") or [])})
    return sorted(grouped.values(), key=lambda item: (-int(item.get("repeat_count") or 1), str(item.get("title") or "")))[: watch.max_items]


def _signals_from_event_outcomes(runtime: Any, *, scope: ScopeRef, watch: SourceWatch) -> list[dict[str, Any]]:
    store = getattr(runtime, "store", None)
    conn = getattr(store, "conn", None) or getattr(getattr(store, "sqlite", None), "conn", None)
    if conn is None:
        return []
    try:
        since_clause = "AND o.recorded_at > ?" if watch.last_seen else ""
        params: tuple[Any, ...] = (scope.tenant_id, scope.agent_id, scope.workspace_id, scope.user_id, watch.last_seen, max(1, watch.max_items) * 5) if watch.last_seen else (scope.tenant_id, scope.agent_id, scope.workspace_id, scope.user_id, max(1, watch.max_items) * 5)
        rows = conn.execute(
            f"""
            SELECT e.id AS event_id, e.payload_json AS event_payload, o.payload_json AS outcome_payload, o.outcome
            FROM event_outcomes o
            LEFT JOIN events e ON e.id = o.event_id
            WHERE o.tenant_id = ?
              AND o.agent_id = ?
              AND o.workspace_id = ?
              AND o.user_id = ?
              {since_clause}
            ORDER BY o.recorded_at DESC
            LIMIT ?
            """,
            params,
        ).fetchall()
    except Exception:
        return []
    signals = []
    for row in rows:
        outcome = _json_dict(row["outcome_payload"])
        event = _json_dict(row["event_payload"])
        outcome_name = str(outcome.get("outcome") or row["outcome"] or "")
        correction = str(outcome.get("correction_from_user") or outcome.get("policy_update") or "")
        if outcome_name != "bad" and not correction:
            continue
        summary = correction or str(outcome.get("reason") or event.get("lesson") or event.get("user_phrase") or "bad outcome")
        signals.append(
            {
                "record_id": str(row["event_id"] or ""),
                "source_record_ids": [str(row["event_id"] or "")],
                "signal_type": "outcome_weakness",
                "title": f"Outcome weakness: {event.get('event_type') or outcome_name or 'bad'}",
                "summary": summary,
                "target_capability": _classify_capability(f"{event.get('event_type')} {event.get('user_phrase')} {summary}", fallback="proactive.judgment"),
                "repeat_count": 1,
                "confidence": 0.82 if correction else 0.72,
                "impact": 0.78,
                "urgency": 0.7,
                "evidence_tier": "T0",
            }
        )
    return signals


def _signals_from_goal_registry(runtime: Any, *, scope: ScopeRef, watch: SourceWatch) -> list[dict[str, Any]]:
    from eimemory.governance.capability_ledger import build_capability_ledger

    registry = load_goal_registry()
    ledger = build_capability_ledger(runtime, scope=scope)
    return derive_goal_signals(registry, capability_scores=ledger.get("capabilities") or {}, limit=watch.max_items)


def _signals_from_stale_assets(runtime: Any, *, scope: ScopeRef, watch: SourceWatch) -> list[dict[str, Any]]:
    records = runtime.store.list_records(kinds=["capability_candidate", "learning_playbook", "rule"], scope=scope, limit=max(1, watch.max_items) * 5)
    signals = []
    for record in records:
        if not _record_after_cursor(record, watch):
            continue
        meta = business_metadata(record.meta)
        if str(record.status or "") in {"disabled", "rejected"}:
            continue
        if meta.get("last_replay_at") or meta.get("verified_at") or meta.get("regression_checked_at"):
            continue
        signals.append(
            {
                "record_id": record.record_id,
                "signal_type": "stale_asset",
                "title": f"Replay needed: {record.title}",
                "summary": f"{record.kind} {record.record_id} has no recent replay or verification metadata.",
                "target_capability": str(meta.get("target_capability") or meta.get("capability") or "proactive.judgment"),
                "confidence": 0.58,
                "impact": 0.5,
                "urgency": 0.42,
                "evidence_tier": "T2",
                "source_record_ids": [record.record_id],
            }
        )
        if len(signals) >= watch.max_items:
            break
    return signals


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


def _existing_signal_record(runtime: Any, *, scope: ScopeRef, signal_hash: str) -> RecordEnvelope | None:
    value = str(signal_hash or "").strip()
    if not value:
        return None
    list_by_meta = getattr(runtime.store, "list_records_by_meta_value", None)
    if callable(list_by_meta):
        records = list_by_meta(
            kinds=["world_signal"],
            scope=scope,
            meta_key="signal_hash",
            meta_value=value,
            limit=1,
        )
        if records:
            return records[0]
        if records == []:
            return None
    offset = 0
    while True:
        page = runtime.store.list_records(kinds=["world_signal"], scope=scope, limit=500, offset=offset)
        for record in page:
            existing_hash = str(record.meta.get("signal_hash") or record.content.get("signal_hash") or "")
            if existing_hash == value:
                return record
        if len(page) < 500:
            return None
        offset += len(page)


def _with_cursor(runtime: Any, *, scope: ScopeRef, watch: SourceWatch) -> SourceWatch:
    cursor = _load_watch_cursor(runtime, scope=scope, watch=watch)
    if not cursor:
        return watch
    return SourceWatch(
        name=watch.name,
        kind=watch.kind,
        query=watch.query,
        enabled=watch.enabled,
        dry_run=watch.dry_run,
        cadence=watch.cadence,
        authority_tier=watch.authority_tier,
        max_items=watch.max_items,
        last_seen=str(cursor.get("high_watermark") or cursor.get("last_seen") or watch.last_seen),
        dedupe_key=watch.dedupe_key,
        seen_record_ids=tuple(str(item) for item in list(cursor.get("seen_record_ids") or []) if str(item)),
    )


def _load_watch_cursor(runtime: Any, *, scope: ScopeRef, watch: SourceWatch) -> dict[str, Any]:
    for record in runtime.store.list_records(kinds=["reflection"], scope=scope, limit=200):
        meta = record.meta if isinstance(record.meta, dict) else {}
        content = record.content if isinstance(record.content, dict) else {}
        if str(meta.get("report_type") or content.get("report_type") or "") != "world_watch_cursor":
            continue
        if str(meta.get("watch_name") or content.get("watch_name") or "") == watch.name and str(meta.get("kind") or content.get("kind") or "") == watch.kind:
            return dict(content)
    return {}


def _save_watch_cursor(
    runtime: Any,
    *,
    scope: ScopeRef,
    watch: SourceWatch,
    high_watermark: str,
    seen_record_ids: set[str],
) -> RecordEnvelope:
    record = append_learning_record_once(
        runtime,
        kind="reflection",
        title=f"World watch cursor: {watch.name}",
        summary=f"{watch.name} high watermark {high_watermark}",
        scope=scope,
        loop_id="learn_watch_cursor",
        step_name="world_watch_cursor",
        semantic_key=stable_semantic_key("world_watch_cursor", watch.name, watch.kind, scope),
        authority_tier="L0",
        status="active",
        content={
            "report_type": "world_watch_cursor",
            "watch_name": watch.name,
            "kind": watch.kind,
            "last_seen": watch.last_seen,
            "high_watermark": high_watermark,
            "seen_record_ids": sorted(seen_record_ids),
        },
        meta={"report_type": "world_watch_cursor", "watch_name": watch.name, "kind": watch.kind},
    )
    record.content = {
        "report_type": "world_watch_cursor",
        "watch_name": watch.name,
        "kind": watch.kind,
        "last_seen": watch.last_seen,
        "high_watermark": high_watermark,
        "seen_record_ids": sorted(seen_record_ids),
    }
    record.meta = {**dict(record.meta or {}), "report_type": "world_watch_cursor", "watch_name": watch.name, "kind": watch.kind}
    record.touch()
    return runtime.store.rewrite(record)


def _record_after_cursor(record: RecordEnvelope, watch: SourceWatch) -> bool:
    if not watch.last_seen:
        return True
    timestamp = str(record.time.updated_at or record.time.created_at or "")
    if not timestamp:
        return False
    if timestamp > watch.last_seen:
        return True
    if timestamp == watch.last_seen and record.record_id not in set(watch.seen_record_ids):
        return True
    return False


def _high_watermark_for_signals(
    runtime: Any,
    *,
    scope: ScopeRef,
    watch: SourceWatch,
    signals: list[dict[str, Any]],
) -> str:
    timestamps: list[str] = []
    for signal in signals:
        for record_id in _signal_source_ids(signal):
            record = runtime.store.get_by_id(record_id, scope=scope)
            if record is None:
                continue
            timestamp = str(record.time.updated_at or record.time.created_at or "")
            if timestamp:
                timestamps.append(timestamp)
    if timestamps:
        return max(timestamps)
    return watch.last_seen


def _cursor_seen_record_ids(
    runtime: Any,
    *,
    scope: ScopeRef,
    watch: SourceWatch,
    signals: list[dict[str, Any]],
    high_watermark: str,
) -> set[str]:
    seen = set(watch.seen_record_ids)
    if not high_watermark:
        return seen
    for signal in signals:
        for record_id in _signal_source_ids(signal):
            record = runtime.store.get_by_id(record_id, scope=scope)
            if record is None:
                continue
            timestamp = str(record.time.updated_at or record.time.created_at or "")
            if timestamp == high_watermark:
                seen.add(record.record_id)
    return seen


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


def _increment_repeat_count(runtime: Any, record: RecordEnvelope, signal: dict[str, Any]) -> bool:
    content = record.content if isinstance(record.content, dict) else {}
    payload = content.get("signal") if isinstance(content.get("signal"), dict) else {}
    current = max(1, int(payload.get("repeat_count") or record.meta.get("repeat_count") or 1))
    incoming = max(1, int(signal.get("repeat_count") or 1))
    current_source_ids = {str(item) for item in list(payload.get("source_record_ids") or []) if str(item)}
    incoming_source_ids = _signal_source_ids(signal)
    if incoming_source_ids and incoming_source_ids.issubset(current_source_ids):
        return False
    if not incoming_source_ids and not signal.get("record_id"):
        return False
    merged_source_ids = sorted(current_source_ids | incoming_source_ids)
    payload["repeat_count"] = max(current, incoming, len(merged_source_ids) if merged_source_ids else 1)
    payload["source_record_ids"] = merged_source_ids
    payload["impact"] = min(1.0, float(payload.get("impact") or 0.75) + 0.05)
    payload["urgency"] = min(1.0, float(payload.get("urgency") or 0.7) + 0.05)
    record.content = {**content, "signal": payload}
    record.meta["repeat_count"] = payload["repeat_count"]
    record.meta["last_seen_loop"] = str(signal.get("loop_id") or "")
    runtime.store.rewrite(record)
    return True


def _signal_source_ids(signal: dict[str, Any]) -> set[str]:
    values = [*list(signal.get("source_record_ids") or []), str(signal.get("record_id") or "")]
    return {str(item) for item in values if str(item)}


def _normalize_text(text: str) -> str:
    return " ".join(str(text or "").strip().lower().split())[:240]


def _json_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    try:
        payload = json.loads(str(value or "{}"))
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


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
