"""Re-project the derived recall_index lane type of acceptance event memory.

1.14.49 classifies SAG event memory projected from closure/deploy acceptance
probes (task_type ``capability.acceptance`` / ``live.acceptance.*``) into the
``evolution_artifact`` recall lane via an effective memory type.  New writes
get that type in recall_index immediately; rows indexed by older releases
still carry ``event_trace`` and are only dropped after scoring.  This repair
updates that single derived recall_index column so the candidate pre-filter
excludes them too.

It never changes records, payload bytes or digests, never deletes anything,
previews by default (``apply=False``) and is reversible with ``revert=True``,
which restores the stored payload memory type in the same column.
"""
from __future__ import annotations

from hashlib import sha256
import json

from eimemory.contracts.recall_boundary import ACCEPTANCE_EVENT_MEMORY_TYPE, record_recall_memory_type
from eimemory.metadata import business_metadata
from eimemory.models.records import ScopeRef

from .inline_digest_repair import _channel_workspaces

SCHEMA = "recall_lane_memory_type_repair.v1"
_SOURCE_TYPES = ("event_trace",)
_TARGET_TYPES = (ACCEPTANCE_EVENT_MEMORY_TYPE,)


def repair_recall_lane_memory_types(store, *, scope, apply=False, revert=False, limit=5000):
    base = scope if isinstance(scope, ScopeRef) else ScopeRef.from_dict(scope)
    if not all((base.agent_id, base.workspace_id, base.user_id)):
        raise ValueError("recall_lane_repair_exact_owner_required")
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 5000:
        raise ValueError("recall_lane_repair_limit_invalid")
    workspaces = _channel_workspaces(base)
    current_types = _TARGET_TYPES if revert else _SOURCE_TYPES
    report = {
        "schema": SCHEMA, "mode": "revert" if revert else "forward", "applied": False,
        "scanned": 0, "eligible": 0, "repaired": 0, "changes": [], "unproven": [],
    }
    with store._lock:
        conn = store.sqlite.conn
        if conn.in_transaction:
            raise ValueError("recall_lane_repair_active_transaction")
        # The SQL only narrows the scan; eligibility is decided from the
        # normally hydrated (digest-checked) record below.
        rows = conn.execute(
            "SELECT r.*, ri.memory_type AS index_memory_type FROM recall_index ri "
            "JOIN records r ON r.storage_key = ri.storage_key "
            "WHERE r.status = 'active' AND r.tenant_id = ? AND r.agent_id = ? AND r.user_id = ? "
            "AND r.workspace_id IN (" + ",".join("?" for _ in workspaces) + ") "
            "AND LOWER(COALESCE(ri.memory_type, '')) IN (" + ",".join("?" for _ in current_types) + ") "
            "ORDER BY r.storage_key LIMIT ?",
            (base.tenant_id or "default", base.agent_id, base.user_id, *workspaces, *current_types, limit + 1),
        ).fetchall()
        if len(rows) > limit:
            raise ValueError("recall_lane_repair_scan_incomplete")
        changes = []
        for row in rows:
            report["scanned"] += 1
            try:
                record = store.sqlite._record_from_storage_row(row, hydrate=True)
            except Exception:
                record = None
            if record is None or not store.sqlite._record_matches_projection_row(record, row):
                report["unproven"].append(row["record_id"])
                continue
            meta = business_metadata(record.meta)
            content = record.content if isinstance(record.content, dict) else {}
            if revert:
                new_type = str(meta.get("memory_type") or content.get("memory_type") or "").strip().lower()
            else:
                new_type = record_recall_memory_type(meta, content, record.provenance).lower()
            old_type = str(row["index_memory_type"] or "")
            if not new_type or new_type == old_type.lower():
                continue
            changes.append((row, old_type, new_type))
            report["changes"].append({
                "storage_key": row["storage_key"], "record_id": row["record_id"],
                "task_type": str(meta.get("task_type") or content.get("task_type") or ""),
                "old_memory_type": old_type, "new_memory_type": new_type,
            })
        report["eligible"] = len(changes)
        report["plan_digest"] = sha256(json.dumps(report["changes"], sort_keys=True).encode()).hexdigest()
        if apply:
            with conn:
                for row, old_type, new_type in changes:
                    changed = conn.execute(
                        "UPDATE recall_index SET memory_type = ? WHERE storage_key = ? AND memory_type = ?",
                        (new_type, row["storage_key"], old_type),
                    ).rowcount
                    if changed != 1:
                        raise ValueError("recall_lane_repair_authority_changed")
            report.update(applied=True, repaired=len(changes))
    return report
