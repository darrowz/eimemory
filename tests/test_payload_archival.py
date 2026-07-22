from __future__ import annotations

import json
import sqlite3

import pytest

from eimemory.adapters.openclaw.hooks import OpenClawMemoryHooks
from eimemory.api.runtime import Runtime
from eimemory.governance.capability_ledger import build_capability_ledger
from eimemory.models.records import RecallBundle, RecordEnvelope, ScopeRef
from eimemory.storage.payload_segments import PayloadSegmentError
from eimemory.storage.sqlite_store import SqliteRecordStore


SCOPE = ScopeRef(tenant_id="tenant", agent_id="agent", workspace_id="workspace", user_id="user")


def _large_record(kind: str, index: int = 0) -> RecordEnvelope:
    return RecordEnvelope.create(
        kind=kind,
        title=f"archive probe {index}",
        summary="bounded searchable summary",
        content={
            "capability": "memory.recall",
            "score": 0.91,
            "score_sequence": index + 1,
            "regression_count": 0,
            "evidence_record_ids": [f"evidence-{index}"],
            "report": {"samples": ["cold-payload-" + ("x" * 200_000)]},
        },
        scope=SCOPE,
        source="eimemory.capability_ledger",
        source_id="default",
        meta={"capability": "memory.recall", "score": 0.91, "score_sequence": index + 1},
    )


def test_future_large_payload_is_compact_in_sqlite_and_lazy_hydrated(tmp_path, monkeypatch) -> None:
    store = SqliteRecordStore(tmp_path / "state.sqlite", payload_archive_inline_bytes=1024)
    record = _large_record("capability_score")
    store.upsert(record)
    row = store.conn.execute(
        "SELECT length(payload_json),payload_pointer_json,payload_digest FROM records WHERE record_id=?",
        (record.record_id,),
    ).fetchone()
    assert int(row[0]) < 32_000
    assert json.loads(row[1])["digest"] == row[2]

    original_read = store.payload_segments.read
    monkeypatch.setattr(
        store.payload_segments,
        "read",
        lambda _pointer: (_ for _ in ()).throw(AssertionError("cold payload opened")),
    )
    compact = store.list_capability_scores_compact(scope=SCOPE, limit=10)
    recalled, _ = store.search_with_diagnostics(
        query="archive probe", kinds=["capability_score"], scope=SCOPE, limit=5
    )
    assert compact and recalled
    monkeypatch.setattr(store.payload_segments, "read", original_read)

    hydrated = store.get_by_id(record.record_id, scope=SCOPE)
    assert hydrated is not None
    assert hydrated.content == record.content
    store.close()


def test_lazy_hydration_fails_closed_and_records_observable_tamper(tmp_path) -> None:
    store = SqliteRecordStore(tmp_path / "state.sqlite", payload_archive_inline_bytes=1024)
    record = _large_record("recall_view")
    store.upsert(record)
    pointer = json.loads(store.conn.execute(
        "SELECT payload_pointer_json FROM records WHERE record_id=?", (record.record_id,)
    ).fetchone()[0])
    segment = store.payload_segments.root / pointer["segment"]
    segment.write_bytes(segment.read_bytes()[:-1] + b"!")

    with pytest.raises(PayloadSegmentError):
        store.get_by_id(record.record_id, scope=SCOPE)
    assert store.payload_segment_health()["failure_count"] == 1
    assert store.payload_segment_health()["last_error"]
    store.close()


def test_historical_archival_is_bounded_resumable_and_preserves_hot_window(tmp_path) -> None:
    db_path = tmp_path / "state.sqlite"
    legacy = SqliteRecordStore(db_path, archive_writes=False)
    records = [_large_record("capability_score", index) for index in range(5)]
    for record in records:
        legacy.upsert(record)
    before = legacy.plan_payload_archival(hot_window=1)
    assert before["eligible_count"] == 4

    reports = []
    while not legacy.payload_archival_complete():
        reports.append(legacy.apply_payload_archival_batch(batch_size=2, hot_window=1))
    assert all(report["processed"] <= 2 for report in reports)
    assert legacy.conn.execute(
        "SELECT COUNT(*) FROM records WHERE payload_pointer_json!=''"
    ).fetchone()[0] == 4
    newest = legacy.conn.execute(
        "SELECT payload_pointer_json FROM records ORDER BY updated_at DESC,record_id DESC LIMIT 1"
    ).fetchone()[0]
    assert newest == ""
    assert legacy.apply_payload_archival_batch(batch_size=2, hot_window=1)["processed"] == 0
    for record in records:
        assert legacy.get_by_id(record.record_id, scope=SCOPE).content == record.content
    legacy.close()


def test_archival_batch_does_not_run_exact_full_table_plan(tmp_path, monkeypatch) -> None:
    store = SqliteRecordStore(tmp_path / "state.sqlite", archive_writes=False)
    for index in range(3):
        store.upsert(_large_record("capability_score", index))
    monkeypatch.setattr(
        store,
        "plan_payload_archival",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("full plan scan")),
    )

    report = store.apply_payload_archival_batch(batch_size=1, hot_window=1)

    assert report["processed"] == 1
    assert report["has_more"] is True
    store.close()


def test_archival_cas_conflict_does_not_advance_cursor(tmp_path, monkeypatch) -> None:
    db_path = tmp_path / "state.sqlite"
    store = SqliteRecordStore(db_path, archive_writes=False)
    record = _large_record("capability_score")
    store.upsert(record)
    original_append = store.payload_segments.append

    def concurrently_rewrite(payload):
        other = sqlite3.connect(db_path)
        try:
            changed = record.to_dict()
            changed["summary"] = "concurrent rewrite"
            other.execute(
                "UPDATE records SET payload_json=? WHERE record_id=?",
                (json.dumps(changed, ensure_ascii=False, sort_keys=True), record.record_id),
            )
            other.commit()
        finally:
            other.close()
        return original_append(payload)

    monkeypatch.setattr(store.payload_segments, "append", concurrently_rewrite)
    with pytest.raises(PayloadSegmentError, match="concurrently"):
        store.apply_payload_archival_batch(batch_size=1, hot_window=0)
    progress = store.conn.execute(
        "SELECT cursor FROM schema_migration_progress WHERE migration_id='records.payload_archive.v1'"
    ).fetchone()
    assert progress is None or progress[0] == ""
    store.close()


def test_payload_health_is_constant_time_and_deep_scan_is_explicit(tmp_path, monkeypatch) -> None:
    store = SqliteRecordStore(tmp_path / "state.sqlite", payload_archive_inline_bytes=1024)
    store.upsert(_large_record("capability_score"))
    monkeypatch.setattr(
        store.payload_segments,
        "archive_stats",
        lambda: (_ for _ in ()).throw(AssertionError("segment directory scan")),
    )
    monkeypatch.setattr(
        store.payload_segments,
        "orphan_report",
        lambda _digests: (_ for _ in ()).throw(AssertionError("pointer index scan")),
    )

    health = store.payload_segment_health()

    assert health["indexed_count"] == 1
    assert health["failure_count"] == 0
    store.close()


def test_storage_footprint_uses_only_pragmas_and_persisted_segment_stats(tmp_path) -> None:
    store = SqliteRecordStore(tmp_path / "state.sqlite", payload_archive_inline_bytes=1024)
    store.upsert(_large_record("capability_score"))
    statements: list[str] = []
    store.conn.set_trace_callback(statements.append)
    try:
        report = store.storage_footprint()
    finally:
        store.conn.set_trace_callback(None)

    assert report["sqlite_bytes"] > 0
    assert report["payload_segments"]["indexed_count"] == 1
    assert not any("FROM RECORDS" in statement.upper() for statement in statements)
    store.close()


def test_future_payload_hard_limit_rejects_before_sqlite_write(tmp_path) -> None:
    store = SqliteRecordStore(
        tmp_path / "state.sqlite",
        payload_archive_inline_bytes=128,
        payload_max_bytes=1024,
    )
    record = _large_record("capability_score")
    with pytest.raises(PayloadSegmentError, match="hard limit"):
        store.upsert(record)
    assert store.conn.execute("SELECT COUNT(*) FROM records").fetchone()[0] == 0
    store.close()


def test_capability_ledger_never_hydrates_archived_evidence_body(tmp_path, monkeypatch) -> None:
    runtime = Runtime.create(root=tmp_path)
    runtime.store.sqlite.payload_archive_inline_bytes = 1024
    runtime.store.append(_large_record("capability_score"))
    monkeypatch.setattr(
        runtime.store.sqlite.payload_segments,
        "read",
        lambda _pointer: (_ for _ in ()).throw(AssertionError("cold payload opened")),
    )

    ledger = build_capability_ledger(runtime, scope=SCOPE, attribute_outcomes=False)

    assert ledger["capabilities"]["memory.recall"]["score"] == 0.91
    runtime.close()


def test_openclaw_policy_attribution_never_hydrates_archived_recall_view(
    tmp_path,
    monkeypatch,
) -> None:
    runtime = Runtime.create(root=tmp_path)
    runtime.store.sqlite.payload_archive_inline_bytes = 256
    hooks = OpenClawMemoryHooks(runtime)
    event = {
        "session_id": "session-cold-policy",
        "agent_id": SCOPE.agent_id,
        "workspace_id": SCOPE.workspace_id,
        "user_id": SCOPE.user_id,
        "tenant_id": SCOPE.tenant_id,
    }
    stored = hooks._audit_prompt_recall(
        event=event,
        bundle=RecallBundle(
            items=[],
            rules=[],
            reflections=[],
            confidence=0.9,
            next_action_hint="",
            explanation={
                "policy_suggestion_ids": ["policy-1"],
                "policy_sources": ["intent_pattern"],
                "matched_event_type": "browser_task",
                "selected_records": [
                    {
                        "record_id": "memory-1",
                        "kind": "memory",
                        "title": "deployment rule",
                        "source": "openclaw.message_received",
                        "recall_lane": "durable_fact",
                        "projection_type": "full",
                        "source_record_id": "source-1",
                    }
                ],
            },
        ),
        injected=False,
    )
    assert runtime.store.sqlite.conn.execute(
        "SELECT payload_pointer_json FROM records WHERE kind='recall_view' LIMIT 1"
    ).fetchone()[0]
    expected = hooks._normalize_policy_attribution(stored.content)
    monkeypatch.setattr(
        runtime.store.sqlite.payload_segments,
        "read",
        lambda _pointer: (_ for _ in ()).throw(AssertionError("cold payload opened")),
    )

    attribution = hooks._recall_audit_policy_attribution(event=event)

    assert attribution == expected
    runtime.close()


def test_recall_view_compact_projection_keeps_attribution_under_hard_limit(tmp_path) -> None:
    store = SqliteRecordStore(tmp_path / "state.sqlite", payload_archive_inline_bytes=256)
    selected = [
        {
            "record_id": f"memory-{index}",
            "kind": "memory",
            "title": "鏁版嵁" * 1_000,
            "source": "openclaw.message_received",
            "recall_lane": "durable_fact",
            "projection_type": "full",
            "source_record_id": f"source-{index}",
        }
        for index in range(100)
    ]
    record = RecordEnvelope.create(
        kind="recall_view",
        title="large recall audit",
        summary="large recall audit",
        scope=SCOPE,
        source="openclaw.before_prompt_build",
        content={
            "session_id": "session-large",
            "policy_suggestion_ids": ["policy-1"],
            "policy_sources": ["intent_pattern"],
            "matched_event_type": "browser_task",
            "selected_records": selected,
            "unbounded": "x" * 200_000,
        },
        meta={"session_id": "session-large"},
    )
    store.upsert(record)
    row = store.conn.execute(
        "SELECT payload_json FROM records WHERE record_id=?", (record.record_id,)
    ).fetchone()
    compact = json.loads(row[0])

    assert len(row[0].encode("utf-8")) <= 64 * 1024
    assert compact["content"]["session_id"] == "session-large"
    assert compact["content"]["policy_suggestion_ids"] == ["policy-1"]
    assert compact["content"]["policy_sources"] == ["intent_pattern"]
    assert compact["content"]["matched_event_type"] == "browser_task"
    assert compact["content"]["selected_records"]
    store.close()
