from __future__ import annotations

from datetime import datetime
from typing import Any

from eimemory.core.clock import now_iso
from eimemory.governance.learning_state import append_learning_record_once, stable_semantic_key
from eimemory.models.records import ScopeRef


LEARNING_KINDS = [
    "world_signal",
    "source_watch",
    "capability_model",
    "weakness",
    "learning_goal",
    "research_task",
    "research_note",
    "learning_experiment",
    "learning_eval",
    "capability_candidate",
    "promotion_request",
    "regression_watch",
    "learning_playbook",
]

# Capability scores are immutable evidence-ledger entries.  Retention must not
# materialize or rewrite their potentially large canonical payloads.


def compact_learning_records(
    runtime: Any,
    *,
    scope: dict[str, Any] | ScopeRef | None = None,
    loop_id: str = "manual",
    max_records: int = 500,
    dry_run: bool = True,
) -> dict[str, Any]:
    scope_ref = scope if isinstance(scope, ScopeRef) else ScopeRef.from_dict(scope)
    records = _list_learning_records(runtime, scope=scope_ref, max_records=max_records)
    expired = [record for record in records if _is_expired(record)]
    duplicate_signals = _duplicate_world_signals(records)
    affected = _unique_records(expired + duplicate_signals)
    if not dry_run:
        for record in affected:
            record.status = "disabled"
            record.meta["retention_disabled_at"] = now_iso()
            runtime.store.rewrite(record)
        if affected:
            append_learning_record_once(
                runtime,
                kind="learning_playbook",
                title="Learning retention compaction",
                summary=f"Compacted {len(affected)} stale or duplicate learning records",
                scope=scope_ref,
                loop_id=loop_id,
                step_name="retention",
                semantic_key=stable_semantic_key("retention", [item.record_id for item in affected]),
                authority_tier="L0",
                status="active",
                content={"disabled_record_ids": [item.record_id for item in affected]},
                meta={"disabled_count": len(affected)},
            )
    return {"ok": True, "dry_run": bool(dry_run), "expired_count": len(expired), "duplicate_count": len(duplicate_signals), "disabled_count": 0 if dry_run else len(affected)}


def _list_learning_records(runtime: Any, *, scope: ScopeRef, max_records: int) -> list:
    records = []
    offset = 0
    page_size = min(500, max(1, int(max_records or 500)))
    while len(records) < max_records:
        page = runtime.store.list_records(
            kinds=LEARNING_KINDS,
            scope=scope,
            limit=min(page_size, max_records - len(records)),
            offset=offset,
        )
        records.extend(page)
        if len(page) < page_size:
            break
        offset += len(page)
    return records


def _is_expired(record) -> bool:
    raw = str(record.meta.get("expires_at") or record.content.get("expires_at") or "").strip()
    if not raw:
        return False
    try:
        expires = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        now = datetime.fromisoformat(now_iso())
    except ValueError:
        return False
    return expires < now


def _duplicate_world_signals(records) -> list:
    seen: set[str] = set()
    duplicates = []
    for record in records:
        if record.kind != "world_signal":
            continue
        key = str(record.meta.get("signal_hash") or "")
        if not key:
            continue
        if key in seen:
            duplicates.append(record)
        else:
            seen.add(key)
    return duplicates


def _unique_records(records) -> list:
    seen: set[str] = set()
    unique = []
    for record in records:
        if record.record_id in seen:
            continue
        seen.add(record.record_id)
        unique.append(record)
    return unique
