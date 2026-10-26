"""Durable, exact-scope review batches with a fixed per-cycle high water.

The cursor schedules work only. It never grants label authority. Receipts must
finish before compare-and-swap advances the cursor; interrupted batches are
safe to repeat using the existing immutable review receipts.
"""
from __future__ import annotations

from dataclasses import asdict
from typing import Any

from eimemory.models.records import RecordEnvelope, ScopeRef, now_iso
from eimemory.storage.runtime_store import _stamp_canonical_hongtu_identity
from .real_query_schema import _stable_digest

SCHEMA = "production_recall_review_progress.v1"
SOURCE = "eimemory.production_recall.review_progress"
_PENDING_SOURCE = "eimemory.production_recall.pending_case"


def _key(value: object) -> tuple[int, str] | None:
    if (isinstance(value, (list, tuple)) and len(value) == 2
            and type(value[0]) is int and 0 <= value[0] <= 2 ** 63 - 1
            and isinstance(value[1], str) and len(value[1]) <= 160):
        return value[0], value[1]
    return None


def load_review_batch(runtime: Any, *, scope: ScopeRef, limit: int) -> dict:
    cursor_id = "prrc_" + _stable_digest(asdict(scope))[:32]
    where = ("kind='evaluation_packet' AND source=? AND status='active' "
             "AND tenant_id=? AND agent_id=? AND workspace_id=? AND user_id=?")
    params = (_PENDING_SOURCE, scope.tenant_id, scope.agent_id, scope.workspace_id, scope.user_id)
    observed_at = now_iso()

    def read_window(db):
        saved = db.get_by_exact_ref(cursor_id, scope=scope, source_id="review_progress")
        saved_body = saved.content if saved is not None else None
        body = saved_body if isinstance(saved_body, dict) else {}
        valid = (saved is not None and saved.kind == "evaluation_packet" and saved.source == SOURCE
                 and saved.status == "active" and body.get("schema") == SCHEMA
                 and _key(body.get("after")) is not None and _key(body.get("high_water")) is not None)
        after = _key(body["after"]) if valid else None
        high = _key(body["high_water"]) if valid else None
        total = int(db.execute("SELECT COUNT(*) FROM records WHERE " + where, params).fetchone()[0])
        # SQLite's insertion row id orders same-timestamp arrivals. Validate the
        # bound record IDs before resuming: a rebuild/VACUUM/deletion may change
        # implicit row ids, in which case restart the cycle rather than skip.
        if high is not None and after is not None:
            for row_id, record_id in (after, high):
                if row_id == 0:
                    continue
                bound = db.execute("SELECT record_id FROM records WHERE " + where + " AND rowid=?",
                                   (*params, row_id)).fetchone()
                if bound is None or bound["record_id"] != record_id:
                    after = high = None
                    break
        open_count = body.get("open_review_count")
        if type(open_count) is not int or open_count < 0:
            open_count = None
        if high is None or after is None or after >= high:
            open_count = 0
            latest = db.execute("SELECT rowid,record_id FROM records WHERE " + where
                                + " ORDER BY rowid DESC LIMIT 1", params).fetchone()
            high = (int(latest["rowid"]), str(latest["record_id"])) if latest is not None else (0, "")
            after = (0, "")
        window = " AND rowid>? AND rowid<=?"
        rows = db.execute("SELECT rowid,record_id,source_id FROM records WHERE " + where + window
                          + " ORDER BY rowid LIMIT ?", (*params, after[0], high[0], limit)).fetchall()
        last = (int(rows[-1]["rowid"]), str(rows[-1]["record_id"])) if rows else high
        remaining = int(db.execute("SELECT COUNT(*) FROM records WHERE " + where + window,
                                   (*params, last[0], high[0])).fetchone()[0])
        newer = int(db.execute("SELECT COUNT(*) FROM records WHERE " + where
                               + " AND rowid>?", (*params, high[0])).fetchone()[0])
        return rows, last, high, total, remaining, newer, _stable_digest(saved_body) if saved is not None else None, open_count

    rows, last, high, total, remaining, newer, expected_digest, open_count = runtime.store.read_consistent(read_window)
    records = []
    for row in rows:
        record = runtime.store.get_by_exact_ref(str(row["record_id"]), scope=scope, source_id=str(row["source_id"]))
        if (record is None or record.source != _PENDING_SOURCE or record.scope != scope
                or record.kind != "evaluation_packet" or record.status != "active"):
            raise ValueError("review_batch_record_unavailable")
        records.append(record)
    return {"records": records, "cursor_id": cursor_id,
            "expected_digest": expected_digest,
            "next": {"schema": SCHEMA, "after": list(last), "high_water": list(high),
                     "open_review_count": open_count},
            "coverage": {"schema": SCHEMA, "boundary": "fixed_high_water_snapshot", "observed_at": observed_at,
                         "total_case_count": total, "batch_case_count": len(records),
                         "batch_uncovered_count": max(0, total - len(records)),
                         "cycle_remaining_count": remaining, "newer_case_count": newer,
                         "cycle_complete": remaining == 0 and newer == 0,
                         "cursor_persisted": False, "cursor_conflict": False}}


def complete_review_batch(runtime: Any, *, scope: ScopeRef, batch: dict) -> bool:
    cursor = RecordEnvelope.create(
        kind="evaluation_packet", title="Production recall review progress",
        summary="Scheduling cursor; never label or answer authority.",
        content=batch["next"], source=SOURCE, source_id="review_progress", scope=scope,
        status="active", meta={"report_type": SCHEMA})
    cursor.record_id = batch["cursor_id"]
    # Match native direct-write identity preparation without moving this
    # scheduling cursor out of the explicitly resolved review scope.
    _stamp_canonical_hongtu_identity(cursor)
    cursor.provenance["identity_scope_preserved"] = True

    def mutation(db):
        previous = db.get_by_exact_ref(cursor.record_id, scope=scope, source_id=cursor.source_id)
        digest = _stable_digest(previous.content) if previous is not None else None
        if digest != batch["expected_digest"]:
            return False, [], []
        db.upsert(cursor, commit=False)
        return True, [cursor], []

    return runtime.store.mutate_records_atomically(mutation)
