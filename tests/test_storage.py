import json
from pathlib import Path
import re
import sqlite3

import pytest

from eimemory.models.memory_edges import MemoryEdge
from eimemory.models.records import RecordEnvelope, ScopeRef, TimeRef
from eimemory.storage import sqlite_store as sqlite_store_module
from eimemory.storage.jsonl import (
    JsonlScanError,
    iter_jsonl_payloads,
    scan_jsonl_strict,
)
from eimemory.storage.runtime_store import RuntimeStore


def test_sqlite_commit_survives_jsonl_export_failure_and_retries(
    tmp_path, monkeypatch
) -> None:
    store = RuntimeStore(root=tmp_path)
    scope = ScopeRef(agent_id="main", workspace_id="outbox")
    record = RecordEnvelope.create(
        kind="memory",
        title="Durable outbox",
        summary="SQLite remains canonical while JSONL is unavailable.",
        scope=scope,
    )

    def fail_append(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(store.log, "append_payload", fail_append, raising=False)
    with pytest.raises(OSError, match="disk full"):
        store.append(record)

    assert store.sqlite.get_by_id(record.record_id, scope=scope) is not None
    assert len(store.sqlite.pending_exports(limit=10)) == 1

    monkeypatch.undo()
    assert store.flush_exports()["exported"] == 1
    assert store.sqlite.pending_exports(limit=10) == []


def test_rebuild_fails_closed_on_malformed_jsonl(tmp_path) -> None:
    store = RuntimeStore(root=tmp_path)
    scope = ScopeRef(agent_id="main", workspace_id="strict-rebuild")
    record = store.append(
        RecordEnvelope.create(
            kind="memory",
            title="Live row",
            summary="Must survive a corrupt recovery source.",
            scope=scope,
        )
    )
    with store.log.path.open("a", encoding="utf-8") as handle:
        handle.write("{malformed\n")

    report = store.rebuild_sqlite_from_jsonl(replace=True)

    assert report["ok"] is False
    assert report["replaced"] is False
    assert report["errors"][0]["line"] == 2
    assert store.get_by_id(record.record_id, scope=scope) is not None


def test_jsonl_rotates_into_bounded_segments_and_rebuilds_streaming(tmp_path) -> None:
    from eimemory.storage.jsonl import JsonlLog

    log = JsonlLog(tmp_path / "records.jsonl", max_segment_bytes=480)
    records = [
        {"record_id": f"rec_segmented_{index}", "summary": "x" * 360}
        for index in range(6)
    ]
    for record in records:
        log.append_payload(record)

    paths = log.segment_paths()
    assert len(paths) > 1
    assert all(
        len(path.read_text(encoding="utf-8").splitlines()) == 1 for path in paths
    )
    assert [entry.payload["record_id"] for entry in log.scan_strict()] == [
        record["record_id"] for record in records
    ]


def test_jsonl_resegments_oversized_legacy_archive_and_preserves_append_order(tmp_path) -> None:
    from eimemory.storage.jsonl import JsonlLog

    log = JsonlLog(
        tmp_path / "records.jsonl",
        max_segment_bytes=8_192,
        cleanup_grace_seconds=0,
    )
    scope = ScopeRef(agent_id="main", workspace_id="legacy-segments")
    records = [
        RecordEnvelope.create(
            kind="memory",
            title=f"Legacy {index}",
            summary="x" * 220,
            scope=scope,
        )
        for index in range(8)
    ]
    legacy = tmp_path / "records.00000001.jsonl"
    legacy.write_bytes(
        b"".join(
            (json.dumps(record.to_dict(), ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
            for record in records[:-1]
        )
    )
    log.path.write_bytes(
        (json.dumps(records[-1].to_dict(), ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
    )
    before = [entry.payload["record_id"] for entry in log.scan_strict()]

    first = log.resegment_oversized()
    second = log.resegment_oversized()
    appended = [
        RecordEnvelope.create(
            kind="memory",
            title=f"After migration {index}",
            summary="y" * 220,
            scope=scope,
        )
        for index in range(5)
    ]
    for record in appended:
        log.append_payload(record.to_dict())

    paths = log.segment_paths()
    assert first["changed"] is True
    assert first["oversized_segment_count"] == 1
    assert first["source_bytes"] == first["replacement_bytes"]
    assert first["source_sha256"] == first["replacement_sha256"]
    assert second["changed"] is False
    assert log.manifest_path.is_file()
    assert not legacy.exists()
    assert all(path.stat().st_size <= log.max_segment_bytes for path in paths)
    assert [entry.payload["record_id"] for entry in log.scan_strict()] == [
        *before,
        *(record.record_id for record in appended),
    ]
    assert json.loads(log.manifest_path.read_text(encoding="utf-8"))["pending_segment"] is None


def test_jsonl_recovers_rotation_after_active_file_was_moved(tmp_path) -> None:
    from eimemory.storage.jsonl import JsonlLog

    log = JsonlLog(tmp_path / "records.jsonl", max_segment_bytes=8_192)
    scope = ScopeRef(agent_id="main", workspace_id="pending-segment")
    moved = RecordEnvelope.create(
        kind="memory",
        title="Moved before manifest finalize",
        summary="recover the pending segment",
        scope=scope,
    )
    pending_name = f"records.segment-{'b' * 32}.jsonl"
    (tmp_path / pending_name).write_bytes(
        (json.dumps(moved.to_dict(), separators=(",", ":")) + "\n").encode("utf-8")
    )
    log.manifest_path.write_text(
        json.dumps(
            {
                "version": 1,
                "segments": [],
                "pending_segment": pending_name,
                "cleanup_pending": [],
            }
        ),
        encoding="utf-8",
    )
    appended = RecordEnvelope.create(
        kind="memory",
        title="Append after recovery",
        summary="new active segment",
        scope=scope,
    )

    log.append_payload(appended.to_dict())

    assert [entry.payload["record_id"] for entry in log.scan_strict()] == [
        moved.record_id,
        appended.record_id,
    ]
    manifest = json.loads(log.manifest_path.read_text(encoding="utf-8"))
    assert manifest["segments"] == [pending_name]
    assert manifest["pending_segment"] is None


def test_jsonl_cleanup_failure_is_retryable_without_duplicate_visibility(
    tmp_path, monkeypatch
) -> None:
    from eimemory.storage.jsonl import JsonlLog

    log = JsonlLog(
        tmp_path / "records.jsonl",
        max_segment_bytes=2_048,
        cleanup_grace_seconds=0,
    )
    legacy = tmp_path / "records.00000001.jsonl"
    rows = [
        json.dumps({"record_id": f"rec_{index}", "summary": "z" * 700}) + "\n"
        for index in range(5)
    ]
    legacy.write_text("".join(rows), encoding="utf-8")
    original_unlink = Path.unlink

    def fail_legacy_cleanup(path, *args, **kwargs):
        if path == legacy:
            raise PermissionError("simulated cleanup denial")
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_legacy_cleanup)
    first = log.resegment_oversized()
    visible_ids = [entry.payload["record_id"] for entry in log.scan_strict()]

    assert first["changed"] is True
    assert first["ok"] is False
    assert legacy.exists()
    assert visible_ids == [f"rec_{index}" for index in range(5)]
    assert json.loads(log.manifest_path.read_text(encoding="utf-8"))[
        "cleanup_pending"
    ] == [legacy.name]

    monkeypatch.setattr(Path, "unlink", original_unlink)
    second = log.resegment_oversized()

    assert second["changed"] is False
    assert second["ok"] is True
    assert not legacy.exists()
    assert json.loads(log.manifest_path.read_text(encoding="utf-8"))[
        "cleanup_pending"
    ] == []


def test_jsonl_new_migration_preserves_previous_cleanup_retry(
    tmp_path, monkeypatch
) -> None:
    from eimemory.storage.jsonl import JsonlLog

    log = JsonlLog(
        tmp_path / "records.jsonl",
        max_segment_bytes=2_048,
        cleanup_grace_seconds=0,
    )
    legacy = tmp_path / "records.00000001.jsonl"
    legacy.write_text(
        "".join(
            json.dumps({"record_id": f"old_{index}", "summary": "o" * 700}) + "\n"
            for index in range(5)
        ),
        encoding="utf-8",
    )
    original_unlink = Path.unlink

    def deny_old_source(path, *args, **kwargs):
        if path == legacy:
            raise PermissionError("old source remains busy")
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", deny_old_source)
    assert log.resegment_oversized()["ok"] is False
    log.path.write_text(
        "".join(
            json.dumps({"record_id": f"new_{index}", "summary": "n" * 700}) + "\n"
            for index in range(5)
        ),
        encoding="utf-8",
    )

    report = log.resegment_oversized()

    assert report["ok"] is False
    assert legacy.exists()
    assert json.loads(log.manifest_path.read_text(encoding="utf-8"))[
        "cleanup_pending"
    ] == [legacy.name]


def test_jsonl_rejects_a_single_row_over_the_hard_segment_limit(tmp_path) -> None:
    from eimemory.storage.jsonl import JsonlLog

    log = JsonlLog(tmp_path / "records.jsonl", max_segment_bytes=256)

    with pytest.raises(ValueError, match="single JSONL row exceeds segment limit"):
        log.append_payload({"record_id": "too_large", "summary": "x" * 300})

    assert not log.path.exists()


def test_jsonl_read_iterator_does_not_create_a_missing_parent(tmp_path) -> None:
    path = tmp_path / "read-only-probe" / "records.jsonl"

    assert list(iter_jsonl_payloads(path)) == []
    assert not path.parent.exists()


def test_jsonl_maintenance_removes_unreferenced_generation(tmp_path) -> None:
    from eimemory.storage.jsonl import JsonlLog

    log = JsonlLog(tmp_path / "records.jsonl", max_segment_bytes=2_048)
    orphan = tmp_path / f".records.segments-{'a' * 32}"
    orphan.mkdir()
    (orphan / "transaction.json").write_text(
        json.dumps(
            {
                "version": 1,
                "state": "staging",
                "log_name": "records.jsonl",
                "generation": orphan.name,
            }
        ),
        encoding="utf-8",
    )
    (orphan / "records.00000001.jsonl").write_text(
        json.dumps({"record_id": "orphan"}) + "\n",
        encoding="utf-8",
    )

    report = log.resegment_oversized()

    assert report["ok"] is True
    assert report["orphan_generation_count"] == 1
    assert not orphan.exists()


def test_jsonl_restores_a_missing_primary_manifest_from_backup(tmp_path) -> None:
    from eimemory.storage.jsonl import JsonlLog

    log = JsonlLog(
        tmp_path / "records.jsonl",
        max_segment_bytes=2_048,
        cleanup_grace_seconds=0,
    )
    legacy = tmp_path / "records.00000001.jsonl"
    legacy.write_text(
        "".join(
            json.dumps({"record_id": f"rec_{index}", "summary": "b" * 700}) + "\n"
            for index in range(5)
        ),
        encoding="utf-8",
    )
    expected = [f"rec_{index}" for index in range(5)]
    assert log.resegment_oversized()["ok"] is True
    assert log.manifest_backup_path.is_file()
    log.manifest_path.unlink()

    actual = [entry.payload["record_id"] for entry in log.scan_strict()]

    assert actual == expected
    assert log.manifest_path.is_file()


def test_jsonl_fails_closed_if_all_manifests_are_missing_for_active_generation(
    tmp_path,
) -> None:
    from eimemory.storage.jsonl import JsonlLog

    log = JsonlLog(
        tmp_path / "records.jsonl",
        max_segment_bytes=2_048,
        cleanup_grace_seconds=0,
    )
    legacy = tmp_path / "records.00000001.jsonl"
    legacy.write_text(
        "".join(
            json.dumps({"record_id": f"rec_{index}", "summary": "g" * 700}) + "\n"
            for index in range(5)
        ),
        encoding="utf-8",
    )
    assert log.resegment_oversized()["ok"] is True
    generation = next(tmp_path.glob(".records.segments-*"))
    log.manifest_path.unlink()
    log.manifest_backup_path.unlink()

    with pytest.raises(ValueError, match="manifest missing for activated generation"):
        log.resegment_oversized()

    assert generation.is_dir()
    assert list(generation.glob("*.jsonl"))


def test_jsonl_readers_bound_oversized_unterminated_rows(tmp_path) -> None:
    path = tmp_path / "records.jsonl"
    path.write_bytes(b'{"record_id":"' + (b"x" * 1_024))

    with pytest.raises(JsonlScanError, match="row exceeds size limit"):
        list(scan_jsonl_strict(path, max_row_bytes=256))
    assert list(iter_jsonl_payloads(path, max_row_bytes=256)) == []


def test_jsonl_rejects_cleanup_paths_not_owned_by_the_log(tmp_path) -> None:
    from eimemory.storage.jsonl import JsonlLog

    log = JsonlLog(tmp_path / "records.jsonl")
    sentinel = tmp_path / "sentinel.db"
    sentinel.write_bytes(b"must survive")
    log.manifest_path.write_text(
        json.dumps(
            {
                "version": 1,
                "segments": [],
                "pending_segment": None,
                "cleanup_pending": [sentinel.name],
                "cleanup_not_before": 0,
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="cleanup path is not owned by this log"):
        log.resegment_oversized(force_cleanup=True)

    assert sentinel.read_bytes() == b"must survive"


def test_jsonl_default_cleanup_grace_preserves_concurrent_reader_snapshot(
    tmp_path,
) -> None:
    from eimemory.storage.jsonl import JsonlLog

    log = JsonlLog(
        tmp_path / "records.jsonl",
        max_segment_bytes=2_048,
        cleanup_grace_seconds=3_600,
    )
    legacy = tmp_path / "records.00000001.jsonl"
    legacy.write_text(
        "".join(
            json.dumps({"record_id": f"rec_{index}", "summary": "r" * 700}) + "\n"
            for index in range(5)
        ),
        encoding="utf-8",
    )
    snapshot = log.segment_paths()

    migrated = log.resegment_oversized()

    assert migrated["ok"] is True
    assert migrated["cleanup_deferred_count"] == 1
    assert legacy.exists()
    assert [entry.payload["record_id"] for entry in scan_jsonl_strict(snapshot)] == [
        f"rec_{index}" for index in range(5)
    ]

    cleaned = log.resegment_oversized(force_cleanup=True)
    assert cleaned["ok"] is True
    assert not legacy.exists()


def test_strict_snapshot_fails_if_a_listed_segment_disappears(tmp_path) -> None:
    path = tmp_path / "records.00000001.jsonl"
    path.write_text(json.dumps({"record_id": "visible"}) + "\n", encoding="utf-8")
    snapshot = [path]
    path.unlink()

    with pytest.raises(FileNotFoundError, match="JSONL segment disappeared"):
        list(scan_jsonl_strict(snapshot))


def test_runtime_maintenance_reports_jsonl_resegmentation(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("EIMEMORY_JSONL_SEGMENT_MAX_BYTES", "2048")
    store = RuntimeStore(root=tmp_path)
    legacy = tmp_path / "records.00000001.jsonl"
    legacy.write_text(
        "".join(
            json.dumps({"record_id": f"rec_{index}", "summary": "m" * 700}) + "\n"
            for index in range(5)
        ),
        encoding="utf-8",
    )

    report = store.maintain_storage()

    assert report["ok"] is True
    assert report["jsonl_segments"]["changed"] is True
    assert report["jsonl_segments"]["largest_segment_bytes"] <= 2_048


def test_runtime_maintenance_resegments_auxiliary_jsonl_archives(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("EIMEMORY_JSONL_SEGMENT_MAX_BYTES", "2048")
    monkeypatch.setenv("EIMEMORY_JSONL_CLEANUP_GRACE_SECONDS", "0")
    store = RuntimeStore(root=tmp_path)
    legacy = tmp_path / "state" / "events.00000001.jsonl"
    legacy.write_text(
        "".join(
            json.dumps({"event_id": f"evt_{index}", "summary": "a" * 700}) + "\n"
            for index in range(5)
        ),
        encoding="utf-8",
    )

    report = store.maintain_storage()

    assert report["ok"] is True
    assert report["auxiliary_jsonl_segments"]["events"]["changed"] is True
    assert not legacy.exists()
    assert all(
        path.stat().st_size <= 2_048
        for path in store._auxiliary_log("events").segment_paths()
    )


def test_sqlite_runtime_uses_bounded_wal_and_disk_temp_pages(tmp_path) -> None:
    store = RuntimeStore(root=tmp_path)

    temp_store = int(store.sqlite.conn.execute("PRAGMA temp_store").fetchone()[0])
    auto_checkpoint = int(
        store.sqlite.conn.execute("PRAGMA wal_autocheckpoint").fetchone()[0]
    )
    journal_limit = int(
        store.sqlite.conn.execute("PRAGMA journal_size_limit").fetchone()[0]
    )

    assert temp_store == 1
    assert 1 <= auto_checkpoint <= 2_000
    assert 0 < journal_limit <= 67_108_864


def test_runtime_store_persists_and_searches_records(tmp_path) -> None:
    store = RuntimeStore(root=tmp_path)
    scope = ScopeRef(agent_id="main", workspace_id="demo")

    record = RecordEnvelope.create(
        kind="memory",
        title="OpenClaw recall",
        summary="Recall project memory before prompt build",
        scope=scope,
        tags=["openclaw"],
    )
    store.append(record)

    results = store.search(query="prompt build", kinds=["memory"], scope=scope, limit=5)

    assert len(results) == 1
    assert results[0].record_id == record.record_id


def test_runtime_store_rewrite_updates_kind_projection(tmp_path) -> None:
    store = RuntimeStore(root=tmp_path)
    scope = ScopeRef(agent_id="main", workspace_id="kind-transition")
    record = store.append(
        RecordEnvelope.create(
            kind="memory",
            title="Tool routing policy",
            summary="Promote this memory into a rule.",
            scope=scope,
        )
    )
    payload = record.to_dict()
    payload["kind"] = "rule"

    store.rewrite(RecordEnvelope.from_dict(payload))

    stored = store.sqlite.conn.execute(
        """
        SELECT records.kind AS record_kind,
               json_extract(records.payload_json, '$.kind') AS payload_kind,
               recall_index.kind AS index_kind
        FROM records
        JOIN recall_index USING (storage_key)
        WHERE records.record_id = ?
        """,
        (record.record_id,),
    ).fetchone()
    assert stored is not None
    assert (stored["record_kind"], stored["payload_kind"], stored["index_kind"]) == (
        "rule",
        "rule",
        "rule",
    )


def test_runtime_store_persists_scoped_memory_edges(tmp_path) -> None:
    store = RuntimeStore(root=tmp_path)
    scope = ScopeRef(agent_id="main", workspace_id="graph")
    first = RecordEnvelope.create(kind="memory", title="Deploy 1.5.1", summary="Release commit abc123.", scope=scope)
    second = RecordEnvelope.create(kind="memory", title="Health check", summary="8091 health ok.", scope=scope)
    store.append(first)
    store.append(second)

    edge = MemoryEdge.create(
        from_id=first.record_id,
        to_id=second.record_id,
        edge_type="temporal",
        confidence=0.7,
        evidence_id=second.record_id,
        scope=scope,
        reason="test",
    )
    store.upsert_memory_edge(edge)

    edges = store.list_memory_edges(scope=scope, edge_types=["temporal"], record_ids=[first.record_id], limit=5)

    assert [item.edge_id for item in edges] == [edge.edge_id]
    assert edges[0].from_id == first.record_id
    assert edges[0].to_id == second.record_id


def test_runtime_store_list_records_filters_by_updated_at_with_index(tmp_path) -> None:
    store = RuntimeStore(root=tmp_path)
    scope = ScopeRef(agent_id="main", workspace_id="ledger")
    old = store.append(
        RecordEnvelope.create(
            kind="capability_score",
            title="Old recall score",
            summary="old",
            scope=scope,
            meta={"capability": "memory.recall", "score": 0.3},
            content={"capability": "memory.recall", "score": 0.3},
        )
    )
    new = store.append(
        RecordEnvelope.create(
            kind="capability_score",
            title="New routing score",
            summary="new",
            scope=scope,
            meta={"capability": "tool.routing", "score": 0.8},
            content={"capability": "tool.routing", "score": 0.8},
        )
    )
    old.time.created_at = "2099-01-01T00:00:00+00:00"
    old.time.updated_at = "2099-01-01T00:00:00+00:00"
    new.time.created_at = "2099-01-02T00:00:00+00:00"
    new.time.updated_at = "2099-01-02T00:00:00+00:00"
    store.rewrite(old)
    store.rewrite(new)

    results = store.list_records(kinds=["capability_score"], scope=scope, since="2099-01-02", limit=10)
    index_names = {str(row["name"]) for row in store.sqlite.conn.execute("PRAGMA index_list(records)").fetchall()}

    assert [record.record_id for record in results] == [new.record_id]
    assert "idx_records_kind_scope_updated" in index_names


def test_runtime_store_creates_hot_path_records_indexes(tmp_path) -> None:
    store = RuntimeStore(root=tmp_path)
    index_names = {str(row["name"]) for row in store.sqlite.conn.execute("PRAGMA index_list(records)").fetchall()}

    assert "idx_records_kind_scope_status_updated" in index_names
    assert "idx_records_kind_scope_created" in index_names
    assert "idx_records_meta_session_id" in index_names


def test_runtime_store_counts_exact_scope_without_loading_record_payloads(tmp_path, monkeypatch) -> None:
    store = RuntimeStore(root=tmp_path)
    exact = ScopeRef(tenant_id="tenant-a", agent_id="hongtu", workspace_id="embodied", user_id="darrow")
    global_scope = ScopeRef(tenant_id="tenant-a", agent_id="hongtu", workspace_id="embodied", user_id="")
    other_user = ScopeRef(tenant_id="tenant-a", agent_id="hongtu", workspace_id="embodied", user_id="alice")
    for index, scope in enumerate((exact, exact, global_scope, other_user)):
        store.append(
            RecordEnvelope.create(
                kind="reflection",
                title=f"Exact-scope count {index}",
                summary="payload must not be decoded for a count",
                detail="x" * 100_000,
                scope=scope,
            )
        )

    monkeypatch.setattr(
        store.sqlite,
        "_record_from_payload_json",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("payload decoded")),
    )

    # Empty-user global records must never alias a user-specific exact scope.
    assert store.count_records_exact_scope(kinds=["reflection"], scope=exact) == 2
    assert store.count_records_exact_scope(kinds=["reflection"], scope=exact, status=" active ") == 2
    assert store.count_records_exact_scope(kinds=["reflection"], scope=exact, statuses=[" active "]) == 2


def test_runtime_store_lists_compact_capability_scores_without_returning_large_evidence_items(tmp_path) -> None:
    store = RuntimeStore(root=tmp_path)
    scope = ScopeRef(agent_id="hongtu", workspace_id="embodied", user_id="darrow")
    record = RecordEnvelope.create(
        kind="capability_score",
        title="Compact capability score",
        summary="large evidence remains canonical but not in readiness projections",
        scope=scope,
        source="eimemory.capability_ledger",
        meta={
            "capability": "",
            "score": 0.0,
            "score_sequence": 0,
            "regression_count": 0,
            "evidence_count": 3,
            "evidence_sources": ["outcome_trace"],
        },
        content={
            "capability": "memory.recall",
            "score": 0.9,
            "score_sequence": 7,
            "regression_count": 9,
            "evidence_record_ids": ["trace-1", "trace-2", "trace-3"],
            "evidence_sources": ["outcome_trace"],
            "evidence_items": [{"source_id": f"trace-{index}", "blob": "x" * 200_000} for index in range(3)],
        },
    )
    store.append(record)
    statements: list[str] = []
    store.sqlite.conn.set_trace_callback(statements.append)
    try:
        records = store.list_capability_scores_compact(scope=scope, limit=10)
    finally:
        store.sqlite.conn.set_trace_callback(None)

    assert [item.record_id for item in records] == [record.record_id]
    assert records[0].content["capability"] == "memory.recall"
    assert records[0].content["score"] == 0.0
    assert records[0].content["score_sequence"] == 0
    assert records[0].content["regression_count"] == 0
    assert records[0].content["evidence_record_ids"] == ["trace-1", "trace-2", "trace-3"]
    assert "evidence_items" not in records[0].content
    assert len(json.dumps(records[0].to_dict(), ensure_ascii=False)) < 20_000
    selects = [statement for statement in statements if statement.lstrip().upper().startswith(("SELECT", "WITH"))]
    assert len(selects) == 1
    assert "WITH selected_records AS" in selects[0]
    cte_body, marker, _outer_select = selects[0].partition(") SELECT")
    assert marker
    assert "payload_json" not in cte_body


def test_runtime_store_meta_value_query_limits_keys_before_loading_payloads(tmp_path) -> None:
    store = RuntimeStore(root=tmp_path)
    scope = ScopeRef(agent_id="hongtu", workspace_id="embodied", user_id="darrow")
    for index in range(4):
        store.append(
            RecordEnvelope.create(
                kind="reflection",
                title=f"Outcome trace {index}",
                detail="x" * 200_000,
                scope=scope,
                meta={"report_type": "outcome_trace"},
            )
        )
    statements: list[str] = []
    store.sqlite.conn.set_trace_callback(statements.append)
    try:
        records = store.list_records_by_meta_value(
            kinds=["reflection"],
            scope=scope,
            meta_key="report_type",
            meta_value="outcome_trace",
            limit=2,
        )
    finally:
        store.sqlite.conn.set_trace_callback(None)

    assert records is not None and len(records) == 2
    selects = [statement for statement in statements if statement.lstrip().upper().startswith(("SELECT", "WITH"))]
    assert len(selects) == 1
    statement = selects[0]
    assert "WITH selected_records AS" in statement
    cte_body, marker, outer_select = statement.partition(") SELECT")
    assert marker
    assert re.search(r"\bLIMIT\s+2\b", cte_body)
    assert "payload_json" not in cte_body
    assert "payload_json" in outer_select


def test_runtime_store_does_not_repeat_meta_key_backfill_after_migration(tmp_path, monkeypatch) -> None:
    store = RuntimeStore(root=tmp_path)
    scope = ScopeRef(agent_id="main", workspace_id="migration")
    for index in range(25):
        store.append(
            RecordEnvelope.create(
                kind="memory",
                title=f"Ordinary record {index}",
                summary="This record legitimately has no deduplication keys.",
                scope=scope,
            )
        )
    store.close()

    calls = 0
    original = sqlite_store_module._record_meta_keys_from_json

    def _tracked(meta_json: str) -> tuple[str, str]:
        nonlocal calls
        calls += 1
        return original(meta_json)

    monkeypatch.setattr(sqlite_store_module, "_record_meta_keys_from_json", _tracked)

    reopened = RuntimeStore(root=tmp_path)
    reopened.close()

    assert calls == 0


def test_applied_meta_key_migration_does_not_open_a_write_transaction_when_checked(tmp_path) -> None:
    store = RuntimeStore(root=tmp_path)

    def deny_transactions(action, _arg1, _arg2, _database, _trigger):
        if action == sqlite3.SQLITE_TRANSACTION:
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    store.sqlite.conn.set_authorizer(deny_transactions)
    try:
        report = store.sqlite.apply_storage_migrations(batch_size=1, offline=True)
    finally:
        store.sqlite.conn.set_authorizer(None)
        store.close()
    assert report["pending"] == []


def test_intent_pattern_payload_migration_is_marked_and_read_only_after_first_run(tmp_path) -> None:
    store = RuntimeStore(root=tmp_path)
    marker = store.sqlite.conn.execute(
        "SELECT 1 FROM schema_migrations WHERE migration_id = ?",
        (sqlite_store_module._INTENT_PATTERN_STATUS_MIGRATION,),
    ).fetchone()
    assert marker is not None

    def deny_pattern_updates(action, table, _column, _database, _trigger):
        if action == sqlite3.SQLITE_UPDATE and table == "intent_patterns":
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    store.sqlite.conn.set_authorizer(deny_pattern_updates)
    try:
        store.sqlite._migrate_intent_patterns_schema()
    finally:
        store.sqlite.conn.set_authorizer(None)
        store.close()


def test_completed_storage_schema_initialization_is_read_only_on_restart(tmp_path) -> None:
    store = RuntimeStore(root=tmp_path)

    def deny_writes(action, _arg1, _arg2, _database, _trigger):
        if action in {
            sqlite3.SQLITE_INSERT,
            sqlite3.SQLITE_UPDATE,
            sqlite3.SQLITE_DELETE,
            sqlite3.SQLITE_TRANSACTION,
        }:
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    store.sqlite.conn.set_authorizer(deny_writes)
    try:
        store.sqlite._init_db()
    finally:
        store.sqlite.conn.set_authorizer(None)
        store.close()


def test_completed_storage_schema_restart_restores_missing_default_pattern(tmp_path) -> None:
    store = RuntimeStore(root=tmp_path)
    row = store.sqlite.conn.execute(
        "SELECT id FROM intent_patterns ORDER BY id LIMIT 1"
    ).fetchone()
    assert row is not None
    pattern_id = str(row["id"])
    store.sqlite.conn.execute("DELETE FROM intent_patterns WHERE id = ?", (pattern_id,))
    store.sqlite.conn.commit()
    store.close()

    restarted = RuntimeStore(root=tmp_path)
    restored = restarted.sqlite.conn.execute(
        "SELECT 1 FROM intent_patterns WHERE id = ?",
        (pattern_id,),
    ).fetchone()
    restarted.close()

    assert restored is not None


def test_intent_pattern_payload_migration_normalizes_legacy_status_once(tmp_path) -> None:
    store = RuntimeStore(root=tmp_path)
    pattern_id = "legacy-pattern-status"
    payload = {
        "id": pattern_id,
        "pattern": "legacy status",
        "default_event_type": "legacy.status",
        "interpreted_intent": "normalize legacy status",
        "confidence": 0.5,
        "status": "unsupported",
    }
    now = "2026-07-20T00:00:00+00:00"
    store.sqlite.conn.execute(
        """
        INSERT INTO intent_patterns (
            id, pattern, default_event_type, interpreted_intent, confidence, status,
            tenant_id, agent_id, workspace_id, user_id, payload_json,
            last_rollback_reason, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            pattern_id,
            payload["pattern"],
            payload["default_event_type"],
            payload["interpreted_intent"],
            payload["confidence"],
            "unsupported",
            "default",
            "",
            "",
            "",
            json.dumps(payload),
            "",
            now,
            now,
        ),
    )
    store.sqlite.conn.execute(
        "DELETE FROM schema_migrations WHERE migration_id = ?",
        (sqlite_store_module._INTENT_PATTERN_STATUS_MIGRATION,),
    )
    store.sqlite.conn.commit()
    store.close()

    migrated = RuntimeStore(root=tmp_path)

    row = migrated.sqlite.conn.execute(
        "SELECT status, payload_json FROM intent_patterns WHERE id = ?",
        (pattern_id,),
    ).fetchone()
    marker = migrated.sqlite.conn.execute(
        "SELECT 1 FROM schema_migrations WHERE migration_id = ?",
        (sqlite_store_module._INTENT_PATTERN_STATUS_MIGRATION,),
    ).fetchone()
    migrated.close()

    assert row is not None
    assert row["status"] == "active"
    assert json.loads(row["payload_json"])["status"] == "active"
    assert marker is not None


def test_runtime_store_meta_key_migration_backfills_legacy_rows_once(tmp_path) -> None:
    store = RuntimeStore(root=tmp_path)
    scope = ScopeRef(agent_id="main", workspace_id="legacy-migration")
    record = store.append(
        RecordEnvelope.create(
            kind="memory",
            title="Legacy deduplication record",
            summary="Migration must project keys from meta JSON.",
            scope=scope,
            meta={"idempotency_key": "legacy-idem", "semantic_key": "legacy-semantic"},
        )
    )
    storage_key = store.sqlite._storage_key(record)
    store.sqlite.conn.execute(
        "UPDATE records SET idempotency_key = '', semantic_key = '' WHERE storage_key = ?",
        (storage_key,),
    )
    store.sqlite.conn.execute(
        "DELETE FROM schema_migrations WHERE migration_id = ?",
        (sqlite_store_module._RECORD_META_KEYS_MIGRATION,),
    )
    store.sqlite.conn.commit()
    store.close()

    migrated = RuntimeStore(root=tmp_path)
    assert sqlite_store_module._RECORD_META_KEYS_MIGRATION in migrated.sqlite.pending_storage_migrations()
    for _ in range(10):
        if sqlite_store_module._RECORD_META_KEYS_MIGRATION not in migrated.sqlite.pending_storage_migrations():
            break
        migrated.sqlite.apply_storage_migrations(batch_size=1, offline=True)
    row = migrated.sqlite.conn.execute(
        "SELECT idempotency_key, semantic_key FROM records WHERE storage_key = ?",
        (storage_key,),
    ).fetchone()
    marker = migrated.sqlite.conn.execute(
        "SELECT 1 FROM schema_migrations WHERE migration_id = ?",
        (sqlite_store_module._RECORD_META_KEYS_MIGRATION,),
    ).fetchone()
    migrated.close()

    assert row is not None
    assert (row["idempotency_key"], row["semantic_key"]) == ("legacy-idem", "legacy-semantic")
    assert marker is not None


def test_runtime_store_bulk_upserts_memory_edges_in_one_call(tmp_path) -> None:
    store = RuntimeStore(root=tmp_path)
    scope = ScopeRef(agent_id="main", workspace_id="graph")
    records = [
        store.append(RecordEnvelope.create(kind="memory", title=f"Record {index}", summary=f"Graph record {index}", scope=scope))
        for index in range(3)
    ]

    edges = store.upsert_memory_edges(
        [
            MemoryEdge.create(
                from_id=records[0].record_id,
                to_id=records[1].record_id,
                edge_type="semantic",
                confidence=0.5,
                evidence_id=records[1].record_id,
                scope=scope,
            ),
            MemoryEdge.create(
                from_id=records[1].record_id,
                to_id=records[2].record_id,
                edge_type="temporal",
                confidence=0.7,
                evidence_id=records[2].record_id,
                scope=scope,
            ),
        ]
    )

    stored = store.list_memory_edges(scope=scope, record_ids=[records[1].record_id], limit=10)

    assert len(edges) == 2
    assert {edge.edge_id for edge in stored} == {edge.edge_id for edge in edges}


def test_runtime_store_uses_wal_for_concurrent_learning_reads(tmp_path) -> None:
    store = RuntimeStore(root=tmp_path)

    journal_mode = str(store.sqlite.conn.execute("PRAGMA journal_mode").fetchone()[0]).lower()
    synchronous = int(store.sqlite.conn.execute("PRAGMA synchronous").fetchone()[0])

    assert journal_mode == "wal"
    assert synchronous <= 1


def test_runtime_store_rewrite_preserves_old_scope_when_new_write_fails(tmp_path, monkeypatch) -> None:
    store = RuntimeStore(root=tmp_path)
    old_scope = ScopeRef(agent_id="main", workspace_id="rewrite", user_id="old")
    new_scope = ScopeRef(agent_id="main", workspace_id="rewrite", user_id="new")
    original = store.append(
        RecordEnvelope.create(
            kind="memory",
            title="Rewrite transaction",
            summary="Old scoped record should survive failed move.",
            scope=old_scope,
        )
    )
    moved_payload = original.to_dict()
    moved_payload["scope"] = {
        "tenant_id": new_scope.tenant_id,
        "agent_id": new_scope.agent_id,
        "workspace_id": new_scope.workspace_id,
        "user_id": new_scope.user_id,
    }
    moved = RecordEnvelope.from_dict(moved_payload)

    def _fail_upsert(record, *, commit=True):
        raise RuntimeError("simulated upsert failure")

    monkeypatch.setattr(store.sqlite, "upsert", _fail_upsert)

    try:
        store.sqlite.rewrite(moved, previous_scope=old_scope)
    except RuntimeError:
        pass
    else:
        raise AssertionError("expected rewrite upsert failure")

    assert store.sqlite.get_by_id(original.record_id, scope=old_scope) is not None
    assert store.sqlite.get_by_id(original.record_id, scope=new_scope) is None


def test_runtime_store_search_skips_corrupt_payload_rows(tmp_path) -> None:
    store = RuntimeStore(root=tmp_path)
    scope = ScopeRef(agent_id="main", workspace_id="corrupt")
    good = store.append(
        RecordEnvelope.create(
            kind="memory",
            title="Healthy payload shared needle",
            summary="Healthy payload shared needle survives corrupt neighbors.",
            scope=scope,
            meta={"force_capture": True},
        )
    )
    bad = store.append(
        RecordEnvelope.create(
            kind="memory",
            title="Corrupt payload shared needle",
            summary="Healthy payload shared needle should not crash recall.",
            scope=scope,
            meta={"force_capture": True},
        )
    )
    storage_key = store.sqlite._storage_key(bad)
    store.sqlite.conn.execute(
        "UPDATE records SET payload_json = ? WHERE storage_key = ?",
        ("{}", storage_key),
    )
    store.sqlite.conn.commit()

    records, diagnostics = store.search_with_diagnostics(
        query="healthy payload shared needle",
        kinds=["memory"],
        scope=scope,
        limit=5,
    )

    assert all(record.record_id != bad.record_id for record in records)
    assert store.sqlite.get_by_id(good.record_id, scope=scope).record_id == good.record_id
    assert diagnostics["blocked_counts"]["corrupt_record"] == 1


def test_runtime_store_rebuilds_sqlite_business_tables_from_jsonl(tmp_path) -> None:
    store = RuntimeStore(root=tmp_path)
    scope = ScopeRef(agent_id="main", workspace_id="rebuild")
    memory = store.append(
        RecordEnvelope.create(
            kind="memory",
            title="Rebuild source",
            summary="Business table rebuild source memory",
            scope=scope,
        )
    )
    linked = store.append(
        RecordEnvelope.create(
            kind="memory",
            title="Rebuild linked",
            summary="Business table linked memory",
            scope=scope,
        )
    )
    event = store.record_event(
        {"user_phrase": "deploy safely", "event_type": "deploy", "interpreted_intent": "deploy with health check"},
        scope=scope,
    )
    outcome = store.record_outcome(event["id"], {"outcome": "good", "reason": "health ok"}, scope=scope)
    pattern = store.upsert_intent_pattern(
        {"pattern": "deploy safely", "default_event_type": "deploy", "interpreted_intent": "run gated deploy"},
        scope=scope,
    )
    edge = store.upsert_memory_edge(
        MemoryEdge.create(
            from_id=memory.record_id,
            to_id=linked.record_id,
            edge_type="causal",
            confidence=0.8,
            evidence_id=memory.record_id,
            scope=scope,
        )
    )
    ledger = store.sqlite._record_policy_rollout_ledger(
        action_type="promotion",
        scope=scope,
        promotion_id="promo_rebuild",
        source_opportunity_id="opp_rebuild",
        source_opportunity={"kind": "test"},
        trust_report={"ok": True},
        replay_report={"ok": True},
        is_auto=True,
        applied_pattern_id=pattern["id"],
        budget_decision="ok",
        reason="rebuild test",
        details={"event_id": event["id"]},
    )
    store.sqlite.conn.commit()

    report = store.rebuild_sqlite_from_jsonl(replace=True)

    assert report["ok"] is True, report
    assert store.get_by_id(memory.record_id, scope=scope).record_id == memory.record_id
    assert store.sqlite.conn.execute("SELECT COUNT(*) FROM events WHERE id = ?", (event["id"],)).fetchone()[0] == 1
    assert store.sqlite.conn.execute("SELECT COUNT(*) FROM event_outcomes WHERE id = ?", (outcome["id"],)).fetchone()[0] == 1
    assert store.sqlite.conn.execute("SELECT COUNT(*) FROM intent_patterns WHERE id = ?", (pattern["id"],)).fetchone()[0] == 1
    assert store.sqlite.conn.execute("SELECT COUNT(*) FROM policy_rollout_ledger WHERE id = ?", (ledger["id"],)).fetchone()[0] == 1
    assert store.list_memory_edges(scope=scope, record_ids=[memory.record_id], limit=5)[0].edge_id == edge.edge_id


def test_runtime_store_returns_active_policy_rules(tmp_path) -> None:
    store = RuntimeStore(root=tmp_path)
    scope = ScopeRef(agent_id="eibrain", workspace_id="robot")

    rule = RecordEnvelope.create(
        kind="rule",
        title="Prefer task context",
        summary="Use task context first for brain respond",
        scope=scope,
        status="active",
        meta={
            "task_type": "brain.respond",
            "retrieval_policy": {
                "route_hint": "task_context_first",
                "open_unknown_on_low_confidence": True,
            },
        },
    )
    store.append(rule)

    policy = store.get_active_policy(task_type="brain.respond", scope=scope)

    assert policy["retrieval_policy"]["route_hint"] == "task_context_first"
    assert policy["retrieval_policy"]["open_unknown_on_low_confidence"] is True


def test_runtime_store_hybrid_search_matches_semantic_overlap(tmp_path) -> None:
    store = RuntimeStore(root=tmp_path)
    scope = ScopeRef(agent_id="main", workspace_id="demo")

    store.append(
        RecordEnvelope.create(
            kind="memory",
            title="Concise assistant replies",
            summary="Respond briefly with compact answers for the operator",
            scope=scope,
        )
    )

    results = store.search(query="short concise responses", kinds=["memory"], scope=scope, limit=5)

    assert results
    assert results[0].title == "Concise assistant replies"


def test_runtime_store_quality_reranks_similar_memories(tmp_path) -> None:
    store = RuntimeStore(root=tmp_path)
    scope = ScopeRef(agent_id="main", workspace_id="demo")
    high_quality = RecordEnvelope.create(
        kind="memory",
        title="Core deployment preference",
        summary="OpenClaw memory deployments must keep durable gateway state.",
        scope=scope,
        meta={
            "quality": {
                "importance": 0.95,
                "confidence": 0.95,
                "freshness": 1.0,
                "reuse_potential": 0.95,
                "salience_score": 0.95,
                "quality_tier": "core",
                "capture_decision": "accept",
            }
        },
    )
    low_quality = RecordEnvelope.create(
        kind="memory",
        title="Candidate deployment note",
        summary="OpenClaw gateway memory deploy note.",
        scope=scope,
        meta={
            "quality": {
                "importance": 0.12,
                "confidence": 0.2,
                "freshness": 1.0,
                "reuse_potential": 0.1,
                "salience_score": 0.18,
                "quality_tier": "candidate",
                "capture_decision": "accept",
            }
        },
    )
    store.append(high_quality)
    store.append(low_quality)

    results, report = store.search_with_diagnostics(
        query="openclaw gateway memory deploy",
        kinds=["memory"],
        scope=scope,
        limit=2,
    )

    assert [record.title for record in results] == [
        "Core deployment preference",
        "Candidate deployment note",
    ]
    assert report["retrieval_mode"] == "recall_index_hybrid"
    assert report["scored_items"][0]["scoring_version"] == "memory_score.v1"
    assert report["scored_items"][0]["memory_score"]["schema_version"] == "memory_score.v1"
    assert "relevance" in report["scored_items"][0]["components"]
    assert report["scored_items"][0]["quality"]["salience_score"] == 0.95
    assert report["scored_items"][0]["final_score"] > report["scored_items"][1]["final_score"]


def test_runtime_store_living_boundary_repair_memory_reranks_similar_generic_memory(tmp_path) -> None:
    store = RuntimeStore(root=tmp_path)
    scope = ScopeRef(agent_id="main", workspace_id="demo")
    generic = RecordEnvelope.create(
        kind="memory",
        title="Communication constraint",
        summary="When deployment pressure rises, discuss communication boundaries with the operator.",
        scope=scope,
    )
    living = RecordEnvelope.create(
        kind="memory",
        title="Repair boundary preference",
        summary="When deployment pressure rises, discuss communication boundaries with the operator.",
        scope=scope,
        meta={
            "living_memory_v1": {
                "motive": {
                    "boundary_labels": ["communication boundary"],
                    "desire_labels": ["repair trust"],
                },
                "affective": {
                    "pressure": 0.8,
                    "frustration_repeat": True,
                    "trust_building": True,
                    "repair_needed": True,
                },
                "temporal": {"status": "active"},
            }
        },
    )
    store.append(generic)
    store.append(living)

    results, report = store.search_with_diagnostics(
        query="repair communication boundary",
        kinds=["memory"],
        scope=scope,
        limit=2,
        recall_filters={"living_task_context_terms": ["repair", "communication boundary"]},
    )

    assert [record.title for record in results] == [
        "Repair boundary preference",
        "Communication constraint",
    ]
    top_item = report["scored_items"][0]
    assert top_item["living_memory"]["affective"]["repair_needed"] is True
    assert top_item["living_score_adjustments"]["motive_match_boost"] > 0
    assert top_item["living_score_adjustments"]["affective_salience_boost"] > 0
    assert top_item["living_score_adjustments"]["total_adjustment"] > 0
    assert top_item["final_score"] > top_item["base_final_score"]


def test_runtime_store_living_expired_superseded_memory_is_penalized(tmp_path) -> None:
    store = RuntimeStore(root=tmp_path)
    scope = ScopeRef(agent_id="main", workspace_id="demo")
    current = RecordEnvelope.create(
        kind="memory",
        title="Current deployment identity",
        summary="OpenClaw deployment identity should use the current operator agreement.",
        scope=scope,
    )
    stale = RecordEnvelope.create(
        kind="memory",
        title="Expired deployment identity",
        summary="OpenClaw deployment identity should use the current operator agreement.",
        scope=scope,
        meta={
            "living_memory_v1": {
                "temporal": {
                    "valid_until": "2000-01-01T00:00:00Z",
                    "superseded": True,
                }
            }
        },
    )
    store.append(stale)
    store.append(current)

    results, report = store.search_with_diagnostics(
        query="current deployment identity",
        kinds=["memory"],
        scope=scope,
        limit=2,
    )

    assert [record.title for record in results] == [
        "Current deployment identity",
        "Expired deployment identity",
    ]
    stale_item = next(item for item in report["scored_items"] if item["title"] == "Expired deployment identity")
    assert stale_item["living_score_adjustments"]["stale_identity_penalty"] < 0
    assert stale_item["final_score"] < stale_item["base_final_score"]


def test_runtime_store_valid_until_stale_memory_does_not_win_by_exact_lexical_match(tmp_path) -> None:
    store = RuntimeStore(root=tmp_path)
    scope = ScopeRef(agent_id="main", workspace_id="demo")
    current = RecordEnvelope.create(
        kind="memory",
        title="Use latest operator agreement",
        summary="Use the latest operator agreement for current deployment guidance.",
        scope=scope,
    )
    stale = RecordEnvelope.create(
        kind="memory",
        title="Current deployment guidance exact",
        summary="Current deployment guidance exact.",
        scope=scope,
        meta={
            "living_memory_v1": {
                "temporal": {
                    "valid_until": "2000-01-01T00:00:00Z",
                }
            }
        },
    )
    store.append(stale)
    store.append(current)

    results, report = store.search_with_diagnostics(
        query="current deployment guidance exact",
        kinds=["memory"],
        scope=scope,
        limit=2,
    )

    assert [record.title for record in results] == [
        "Use latest operator agreement",
        "Current deployment guidance exact",
    ]
    stale_item = next(item for item in report["scored_items"] if item["title"] == "Current deployment guidance exact")
    assert stale_item["raw_lexical_score"] > stale_item["lexical_score"]
    assert stale_item["living_score_adjustments"]["stale_identity_penalty"] < 0
    assert stale_item["final_score"] < report["scored_items"][0]["final_score"]


def test_runtime_store_auto_enriched_living_labels_match_natural_query_terms(tmp_path) -> None:
    from eimemory.api.runtime import Runtime

    runtime = Runtime.create(root=tmp_path)
    scope = {"agent_id": "main", "workspace_id": "demo"}
    generic = runtime.memory.ingest(
        text="Prefer concise answers when writing operator updates.",
        memory_type="preference",
        title="Generic concise style",
        scope=scope,
        force_capture=True,
    )
    boundary = runtime.memory.ingest(
        text="Prefer concise answers. No fluff, get straight to the point.",
        memory_type="preference",
        title="No fluff concise style",
        scope=scope,
        force_capture=True,
    )

    results, report = runtime.store.search_with_diagnostics(
        query="no fluff concise style",
        kinds=["memory"],
        scope=scope,
        limit=2,
        recall_filters={"living_task_context_terms": ["no fluff"]},
    )

    assert results[0].record_id == boundary.record_id
    assert {item.record_id for item in results} == {boundary.record_id, generic.record_id}
    top_item = report["scored_items"][0]
    assert top_item["record_id"] == boundary.record_id
    assert top_item["living_score_adjustments"]["motive_match_boost"] > 0


def test_runtime_store_auto_enriched_pressure_contributes_to_affective_boost(tmp_path) -> None:
    from eimemory.api.runtime import Runtime

    runtime = Runtime.create(root=tmp_path)
    scope = {"agent_id": "main", "workspace_id": "demo"}
    urgent = runtime.memory.ingest(
        text="This is urgent and under pressure; reply before proceeding.",
        memory_type="preference",
        title="Urgent pressure preference",
        scope=scope,
        force_capture=True,
    )

    _, report = runtime.store.search_with_diagnostics(
        query="urgent pressure reply",
        kinds=["memory"],
        scope=scope,
        limit=1,
    )

    scored = report["scored_items"][0]
    assert scored["record_id"] == urgent.record_id
    assert scored["living_memory"]["affective"]["pressure"] == "elevated"
    assert scored["living_score_adjustments"]["affective_salience_boost"] > 0


def test_runtime_store_auto_enriched_let_go_memory_is_stale_penalized(tmp_path) -> None:
    from eimemory.api.runtime import Runtime

    runtime = Runtime.create(root=tmp_path)
    scope = {"agent_id": "main", "workspace_id": "demo"}
    stale = runtime.memory.ingest(
        text="Let go of the old deployment preference; it is no longer relevant.",
        memory_type="preference",
        title="Old deployment preference",
        scope=scope,
        force_capture=True,
    )
    current = runtime.memory.ingest(
        text="Use the current deployment preference for release work.",
        memory_type="preference",
        title="Current deployment preference",
        scope=scope,
        force_capture=True,
    )

    results, report = runtime.store.search_with_diagnostics(
        query="deployment preference",
        kinds=["memory"],
        scope=scope,
        limit=2,
    )

    assert results[0].record_id == current.record_id
    stale_item = next(item for item in report["scored_items"] if item["record_id"] == stale.record_id)
    assert stale_item["living_score_adjustments"]["stale_identity_penalty"] < 0


def test_runtime_store_quality_does_not_match_unrelated_memories(tmp_path) -> None:
    store = RuntimeStore(root=tmp_path)
    scope = ScopeRef(agent_id="main", workspace_id="demo")
    store.append(
        RecordEnvelope.create(
            kind="memory",
            title="Core deployment preference",
            summary="OpenClaw gateway memory deployments must keep durable state.",
            scope=scope,
            meta={
                "quality": {
                    "importance": 1.0,
                    "confidence": 1.0,
                    "freshness": 1.0,
                    "reuse_potential": 1.0,
                    "salience_score": 1.0,
                    "quality_tier": "core",
                    "capture_decision": "accept",
                }
            },
        )
    )

    results = store.search(query="banana smoothie recipe", kinds=["memory"], scope=scope, limit=5)

    assert results == []


def test_runtime_store_quality_boost_does_not_return_weakly_related_vector_noise(tmp_path) -> None:
    store = RuntimeStore(root=tmp_path)
    scope = ScopeRef(agent_id="main", workspace_id="demo")
    store.append(
        RecordEnvelope.create(
            kind="memory",
            title="OpenClaw agent outcome",
            summary="OpenClaw memory quality should improve useful agent recall.",
            scope=scope,
            meta={
                "quality": {
                    "importance": 0.95,
                    "confidence": 0.95,
                    "freshness": 1.0,
                    "reuse_potential": 0.95,
                    "salience_score": 0.95,
                    "quality_tier": "core",
                    "capture_decision": "accept",
                }
            },
        )
    )

    results = store.search(
        query="EIMEMORY_SOURCE_CANDIDATE_UNIQUE_1781951",
        kinds=["memory", "claim_card", "knowledge_page"],
        scope=scope,
        limit=5,
    )

    assert results == []


def test_runtime_store_excludes_rejected_records_from_search(tmp_path) -> None:
    store = RuntimeStore(root=tmp_path)
    scope = ScopeRef(agent_id="main", workspace_id="demo")
    store.append(
        RecordEnvelope.create(
            kind="memory",
            title="Rejected memory",
            summary="OpenClaw gateway memory deploy note.",
            scope=scope,
            status="rejected",
            meta={"quality": {"capture_decision": "reject", "salience_score": 0.0}},
        )
    )

    results = store.search(query="openclaw gateway", kinds=["memory"], scope=scope, limit=5)

    assert results == []


def test_runtime_store_scope_isolated_by_tenant_and_user(tmp_path) -> None:
    store = RuntimeStore(root=tmp_path)
    shared_agent_workspace = {"agent_id": "main", "workspace_id": "demo"}
    store.append(
        RecordEnvelope.create(
            kind="memory",
            title="Tenant A memory",
            summary="Only tenant A should see this",
            scope=ScopeRef(tenant_id="tenant-a", user_id="alice", **shared_agent_workspace),
        )
    )
    store.append(
        RecordEnvelope.create(
            kind="memory",
            title="Tenant B memory",
            summary="Only tenant B should see this",
            scope=ScopeRef(tenant_id="tenant-b", user_id="bob", **shared_agent_workspace),
        )
    )

    tenant_a_results = store.search(
        query="memory",
        kinds=["memory"],
        scope=ScopeRef(tenant_id="tenant-a", user_id="alice", **shared_agent_workspace),
        limit=5,
    )
    tenant_b_results = store.search(
        query="memory",
        kinds=["memory"],
        scope=ScopeRef(tenant_id="tenant-b", user_id="bob", **shared_agent_workspace),
        limit=5,
    )

    assert [record.title for record in tenant_a_results] == ["Tenant A memory"]
    assert [record.title for record in tenant_b_results] == ["Tenant B memory"]


def test_runtime_store_user_scope_can_see_global_memories_but_not_other_users(tmp_path) -> None:
    store = RuntimeStore(root=tmp_path)
    shared = {"tenant_id": "tenant-a", "agent_id": "main", "workspace_id": "demo"}
    store.append(
        RecordEnvelope.create(
            kind="memory",
            title="Global memory",
            summary="Shared global project memory",
            scope=ScopeRef(user_id="", **shared),
        )
    )
    store.append(
        RecordEnvelope.create(
            kind="memory",
            title="Alice memory",
            summary="Alice project memory",
            scope=ScopeRef(user_id="alice", **shared),
        )
    )
    store.append(
        RecordEnvelope.create(
            kind="memory",
            title="Bob memory",
            summary="Bob project memory",
            scope=ScopeRef(user_id="bob", **shared),
        )
    )

    results = store.search(
        query="project memory",
        kinds=["memory"],
        scope=ScopeRef(user_id="alice", **shared),
        limit=10,
    )

    titles = {record.title for record in results}
    assert "Global memory" in titles
    assert "Alice memory" in titles
    assert "Bob memory" not in titles


def test_runtime_store_empty_user_scope_does_not_wildcard_private_users(tmp_path) -> None:
    store = RuntimeStore(root=tmp_path)
    shared = {"tenant_id": "tenant-a", "agent_id": "main", "workspace_id": "demo"}
    store.append(
        RecordEnvelope.create(
            kind="memory",
            title="Global memory",
            summary="Shared global project memory",
            scope=ScopeRef(user_id="", **shared),
        )
    )
    store.append(
        RecordEnvelope.create(
            kind="memory",
            title="Alice memory",
            summary="Alice private project memory",
            scope=ScopeRef(user_id="alice", **shared),
        )
    )

    results = store.search(
        query="project memory",
        kinds=["memory"],
        scope=ScopeRef(user_id="", **shared),
        limit=10,
    )

    assert [record.title for record in results] == ["Global memory"]


def test_runtime_store_scopes_duplicate_record_ids_independently(tmp_path) -> None:
    store = RuntimeStore(root=tmp_path)
    shared_id = "stable_paper_id"
    tenant_a = ScopeRef(tenant_id="tenant-a", agent_id="main", workspace_id="repo")
    tenant_b = ScopeRef(tenant_id="tenant-b", agent_id="main", workspace_id="repo")
    for scope, title in [(tenant_a, "Tenant A paper"), (tenant_b, "Tenant B paper")]:
        store.append(
            RecordEnvelope(
                record_id=shared_id,
                kind="paper_source",
                status="active",
                title=title,
                summary=f"{title} summary",
                detail="",
                content={"text": f"{title} content"},
                tags=[],
                links=[],
                evidence=[],
                source="test",
                scope=scope,
                time=TimeRef(
                    created_at="2026-04-23T00:00:00+00:00",
                    updated_at="2026-04-23T00:00:00+00:00",
                    occurred_at="2026-04-23T00:00:00+00:00",
                ),
                provenance={},
                meta={},
            )
        )

    assert store.get_by_id(shared_id, scope=tenant_a).title == "Tenant A paper"
    assert store.get_by_id(shared_id, scope=tenant_b).title == "Tenant B paper"
    assert store.search(query="paper", kinds=["paper_source"], scope=tenant_a, limit=5)[0].title == "Tenant A paper"
    assert store.search(query="paper", kinds=["paper_source"], scope=tenant_b, limit=5)[0].title == "Tenant B paper"


def test_runtime_store_get_by_id_requires_matching_scope_when_provided(tmp_path) -> None:
    store = RuntimeStore(root=tmp_path)
    record = RecordEnvelope.create(
        kind="memory",
        title="Alice memory",
        summary="Alice private project memory",
        scope=ScopeRef(tenant_id="tenant-a", agent_id="main", workspace_id="demo", user_id="alice"),
    )
    store.append(record)

    assert store.get_by_id(
        record.record_id,
        scope=ScopeRef(tenant_id="tenant-a", agent_id="main", workspace_id="demo", user_id="bob"),
    ) is None


def test_runtime_store_list_records_uses_stable_tiebreaker_for_same_timestamp(tmp_path) -> None:
    store = RuntimeStore(root=tmp_path)
    scope = ScopeRef(agent_id="main", workspace_id="demo")
    same_time = TimeRef(
        created_at="2026-04-21T00:00:00+00:00",
        updated_at="2026-04-21T00:00:00+00:00",
        occurred_at="2026-04-21T00:00:00+00:00",
    )
    records = [
        RecordEnvelope(
            record_id=record_id,
            kind="memory",
            status="active",
            title=f"Stable record {record_id}",
            summary="Stable pagination memory record",
            detail="",
            content={"text": "Stable pagination memory record"},
            tags=[],
            links=[],
            evidence=[],
            source="test",
            scope=scope,
            time=same_time,
            provenance={},
            meta={},
        )
        for record_id in ["mem_a", "mem_b", "mem_c"]
    ]
    for record in records:
        store.append(record)

    first_page = store.list_records(scope=scope, limit=2, offset=0)
    second_page = store.list_records(scope=scope, limit=2, offset=2)

    paged_ids = [record.record_id for record in [*first_page, *second_page]]
    assert paged_ids == ["mem_c", "mem_b", "mem_a"]
    assert len(set(paged_ids)) == 3


def test_runtime_store_list_records_limits_keys_before_loading_bounded_payloads(tmp_path) -> None:
    store = RuntimeStore(root=tmp_path)
    scope = ScopeRef(agent_id="main", workspace_id="bounded-list")
    for index in range(4):
        store.append(
            RecordEnvelope.create(
                kind="memory",
                title=f"Bounded list {index}",
                summary="large payload must not enter the ORDER BY temporary table",
                detail="x" * 100_000,
                scope=scope,
            )
        )

    statements: list[str] = []
    store.sqlite.conn.set_trace_callback(statements.append)
    try:
        records = store.list_records(scope=scope, limit=2)
    finally:
        store.sqlite.conn.set_trace_callback(None)

    selects = [
        statement
        for statement in statements
        if statement.lstrip().upper().startswith(("SELECT", "WITH"))
    ]
    assert len(records) == 2
    assert len(selects) == 1
    statement = selects[0]
    assert "WITH selected_records AS" in statement
    assert "SELECT storage_key, updated_at, record_id FROM records" in statement
    assert "ORDER BY updated_at DESC, record_id DESC LIMIT 2 OFFSET 0" in statement
    assert (
        "SELECT selected_records.storage_key, records.source_id, records.payload_json"
        in statement
    )
    assert statement.index("LIMIT 2 OFFSET 0") < statement.index("payload_json")


def test_runtime_store_list_records_has_no_two_snapshot_payload_race(tmp_path) -> None:
    reader = RuntimeStore(root=tmp_path)
    writer = RuntimeStore(root=tmp_path)
    scope = ScopeRef(agent_id="main", workspace_id="snapshot-list")
    original = reader.append(
        RecordEnvelope.create(
            kind="memory",
            status="active",
            title="Snapshot record",
            summary="Concurrent rewrites must not change a selected page.",
            scope=scope,
        )
    )
    changed = RecordEnvelope.from_dict(original.to_dict())
    changed.status = "rejected"
    connection = reader.sqlite.conn

    class InterleavingConnection:
        rewritten = False

        def execute(self, statement, parameters=()):
            if (
                not self.rewritten
                and statement.lstrip().startswith(
                    "SELECT storage_key, payload_json FROM records WHERE storage_key IN"
                )
            ):
                self.rewritten = True
                writer.rewrite(changed)
            return connection.execute(statement, parameters)

        def __getattr__(self, name):
            return getattr(connection, name)

    interleaving = InterleavingConnection()
    reader.sqlite.conn = interleaving
    try:
        records = reader.list_records(scope=scope, status="active", limit=1)
    finally:
        reader.sqlite.conn = connection
        reader.close()
        writer.close()

    assert interleaving.rewritten is False
    assert [record.record_id for record in records] == [original.record_id]
    assert records[0].status == "active"


def test_sqlite_records_has_scope_updated_index(tmp_path) -> None:
    store = RuntimeStore(root=tmp_path)

    indexes = {
        str(row[1])
        for row in store.sqlite.conn.execute("PRAGMA index_list(records)").fetchall()
    }

    assert "idx_records_scope_updated" in indexes



def test_runtime_store_prefers_user_scoped_policy_over_newer_global_rule(tmp_path) -> None:
    store = RuntimeStore(root=tmp_path)
    scoped = ScopeRef(agent_id="eibrain", workspace_id="robot", user_id="alice")
    global_scope = ScopeRef(agent_id="eibrain", workspace_id="robot")

    specific_rule = RecordEnvelope.create(
        kind="rule",
        title="Alice policy",
        summary="Alice-specific retrieval policy",
        scope=scoped,
        status="active",
        meta={
            "task_type": "brain.respond",
            "retrieval_policy": {"route_hint": "user_specific"},
        },
    )
    global_rule = RecordEnvelope.create(
        kind="rule",
        title="Global policy",
        summary="Global retrieval policy",
        scope=global_scope,
        status="active",
        meta={
            "task_type": "brain.respond",
            "retrieval_policy": {"route_hint": "global_default"},
        },
    )
    store.append(specific_rule)
    global_rule.time.updated_at = "9999-12-31T23:59:59+00:00"
    store.append(global_rule)

    policy = store.get_active_policy(task_type="brain.respond", scope=scoped)

    assert policy["retrieval_policy"]["route_hint"] == "user_specific"


def test_runtime_store_search_with_lexical_diagnostics_prioritizes_project_memory(tmp_path) -> None:
    store = RuntimeStore(root=tmp_path)
    scope = ScopeRef(agent_id="main", workspace_id="project")
    memory = RecordEnvelope.create(
        kind="memory",
        title="UUMit 交付验收清单",
        summary="UUMit 外部订单 交付品质 海报 v2 验收清单。交付要求：按步骤逐项验收。",
        scope=scope,
        source="operator.correction",
    )
    knowledge_page = RecordEnvelope.create(
        kind="knowledge_page",
        title="SIREN 多模态推荐论文",
        summary="SIREN论文讨论多模态推荐与交付系统，但未涉及 UUMit 海报 v2。",
        scope=scope,
        source="eimemory.knowledge.compiler",
        meta={"page_type": "paper"},
    )
    store.append(memory)
    store.append(knowledge_page)

    results, report = store.search_with_diagnostics(
        query="UUMit 交付品质 海报 v2",
        kinds=["memory", "knowledge_page"],
        scope=scope,
        limit=5,
        recall_filters={
            "intent_name": "project_delivery",
            "memory_cube": "project",
            "preferred_kinds": ("memory", "rule", "raw_chunk", "reflection"),
            "suppressed_kinds": ("knowledge_page",),
            "kind_weights": {},
        },
    )

    assert len(results) == 2
    assert results[0].record_id == memory.record_id
    assert [item["kind"] for item in report["scored_items"]] == ["memory", "knowledge_page"]
    memory_signal = report["scored_items"][0]["lexical_signal"]
    knowledge_item = report["scored_items"][1]
    assert memory_signal["version_hits"] == ("v2",)
    assert "交付品质" in memory_signal["exact_phrase_hits"]
    assert memory_signal["entity_hits"] or memory_signal["token_hits"]
    assert knowledge_item["kind"] == "knowledge_page"
    assert knowledge_item["kind_intent_adjustment"] < 0
    assert knowledge_item["kind_intent_penalty"]
    assert report["scored_items"][0]["final_score"] > report["scored_items"][1]["final_score"]


def test_runtime_store_search_filters_claim_card_with_only_embedded_version_match_for_project_query(tmp_path) -> None:
    store = RuntimeStore(root=tmp_path)
    scope = ScopeRef(agent_id="main", workspace_id="project")
    memory = RecordEnvelope.create(
        kind="memory",
        title="外部订单交付验收规则",
        summary="以后外部订单先对需求清单逐条验收，再交付。",
        scope=scope,
        source="operator.correction",
        meta={"force_capture": True},
    )
    claim_card = RecordEnvelope.create(
        kind="claim_card",
        title='Our approach decouples the task and employs DSPy"s MIPROv2 optimizer',
        summary='Our approach decouples the task into modular stages and employs DSPy"s MIPROv2 optimizer.',
        scope=scope,
        source="eimemory.knowledge.claims",
    )
    store.append(memory)
    store.append(claim_card)

    results, report = store.search_with_diagnostics(
        query="UUMit 交付品质 海报 v2",
        kinds=["memory", "claim_card"],
        scope=scope,
        limit=5,
        recall_filters={
            "intent_name": "project_delivery",
            "memory_cube": "project",
            "preferred_kinds": ("memory", "rule", "raw_chunk", "reflection"),
            "suppressed_kinds": ("knowledge_page",),
            "kind_weights": {},
        },
    )

    assert [item.record_id for item in results] == [memory.record_id]
    assert all(item["record_id"] != claim_card.record_id for item in report["scored_items"])


def test_runtime_store_search_prefers_actionable_project_memory_over_tool_call_transcript(tmp_path) -> None:
    store = RuntimeStore(root=tmp_path)
    scope = ScopeRef(agent_id="main", workspace_id="project")
    tool_transcript = RecordEnvelope.create(
        kind="memory",
        title="OpenClaw agent outcome",
        summary=(
            '{"type":"toolCall","name":"message","arguments":{"message":"'
            "我没有交付实质内容；尝试取消时平台返回无权取消，所以提交透明说明。"
            '"}}'
        ),
        scope=scope,
        source="openclaw.agent_end",
        meta={
            "memory_type": "conversation",
            "quality": {
                "importance": 0.7,
                "salience_score": 0.7,
                "confidence": 0.62,
                "freshness": 1.0,
                "reuse_potential": 0.5,
                "capture_decision": "accept",
            },
        },
    )
    actionable_memory = RecordEnvelope.create(
        kind="memory",
        title="OpenClaw agent outcome",
        summary="已记到长期记忆。以后外部订单先对需求清单逐条验收，再交付。",
        scope=scope,
        source="openclaw.agent_end",
        meta={
            "memory_type": "conversation",
            "quality": {
                "importance": 0.52,
                "salience_score": 0.52,
                "confidence": 0.62,
                "freshness": 1.0,
                "reuse_potential": 0.38,
                "capture_decision": "accept",
            },
        },
    )
    store.append(tool_transcript)
    store.append(actionable_memory)

    results, report = store.search_with_diagnostics(
        query="UUMit 交付品质 海报 v2",
        kinds=["memory", "claim_card"],
        scope=scope,
        limit=5,
        recall_filters={
            "intent_name": "project_delivery",
            "memory_cube": "project",
            "preferred_kinds": ("memory", "rule", "raw_chunk", "reflection"),
            "suppressed_kinds": ("knowledge_page",),
            "kind_weights": {},
        },
    )

    assert [item.record_id for item in results] == [actionable_memory.record_id]
    scored = {item["record_id"]: item for item in report["scored_items"]}
    assert scored[actionable_memory.record_id]["actionable_intent_adjustment"] > 0
    assert tool_transcript.record_id not in scored

    evidence_results, evidence_report = store.search_with_diagnostics(
        query="UUMit 交付品质 海报 v2",
        kinds=["memory", "claim_card"],
        scope=scope,
        limit=5,
        recall_filters={
            "intent_name": "project_delivery",
            "memory_cube": "project",
            "preferred_kinds": ("memory", "rule", "raw_chunk", "reflection"),
            "suppressed_kinds": ("knowledge_page",),
            "include_evidence_only": True,
        },
    )

    assert [item.record_id for item in evidence_results[:2]] == [
        actionable_memory.record_id,
        tool_transcript.record_id,
    ]
    evidence_scored = {item["record_id"]: item for item in evidence_report["scored_items"]}
    assert evidence_scored[tool_transcript.record_id]["actionable_intent_adjustment"] < 0


def test_runtime_store_search_operator_preference_keeps_exact_style_memory_first(tmp_path) -> None:
    store = RuntimeStore(root=tmp_path)
    scope = ScopeRef(agent_id="main", workspace_id="operator")
    poetic_memory = RecordEnvelope.create(
        kind="memory",
        title="OpenClaw agent outcome",
        summary=(
            "凌晨三点的纸页还带着服务器的微温。鸿哥说研究不能只躺在摘要里。"
            "于是我把 paper 折成 workflow，把偏好压缩成规则，把“以后回复简洁”放进缓存。"
        ),
        scope=scope,
        source="openclaw.agent_end",
        meta={
            "memory_type": "conversation",
            "quality": {
                "importance": 0.72,
                "salience_score": 0.72,
                "confidence": 0.62,
                "freshness": 1.0,
                "reuse_potential": 0.5,
                "capture_decision": "accept",
            },
        },
    )
    style_memory = RecordEnvelope.create(
        kind="memory",
        title="Hongtu operator communication style",
        summary="鸿哥 沟通风格：极简、直接，讨厌废话；先给结论，少解释。",
        scope=scope,
        source="operator.correction",
        meta={
            "memory_type": "preference",
            "quality": {
                "importance": 0.6,
                "salience_score": 0.6,
                "confidence": 0.8,
                "freshness": 1.0,
                "reuse_potential": 0.7,
                "capture_decision": "accept",
            },
        },
    )
    store.append(poetic_memory)
    store.append(style_memory)

    results, report = store.search_with_diagnostics(
        query="鸿哥 沟通风格",
        kinds=["memory", "claim_card"],
        scope=scope,
        limit=5,
        recall_filters={
            "intent_name": "operator_preference",
            "memory_cube": "operator",
            "preferred_kinds": ("memory", "rule", "reflection"),
            "suppressed_kinds": ("knowledge_page",),
            "kind_weights": {},
        },
    )

    assert results[0].record_id == style_memory.record_id
    scored = {item["record_id"]: item for item in report["scored_items"]}
    assert scored[poetic_memory.record_id]["actionable_intent_adjustment"] == 0.0
    assert "actionable_preference" not in scored[poetic_memory.record_id]["actionable_intent_reasons"]


def test_runtime_store_recall_index_hides_operational_outcome_from_default_project_recall(tmp_path) -> None:
    store = RuntimeStore(root=tmp_path)
    scope = ScopeRef(agent_id="main", workspace_id="project")
    raw_outcome = RecordEnvelope.create(
        kind="memory",
        title="OpenClaw agent outcome",
        summary='{"type":"toolCall","name":"message","arguments":{"message":"UUMit 交付品质 海报 v2 过程日志"}}',
        scope=scope,
        source="openclaw.agent_end",
        meta={"memory_type": "conversation"},
    )
    actionable = RecordEnvelope.create(
        kind="memory",
        title="OpenClaw agent outcome",
        summary="已记到长期记忆。以后外部订单先对需求清单逐条验收，再交付。",
        scope=scope,
        source="openclaw.agent_end",
        meta={"memory_type": "conversation"},
    )
    store.append(raw_outcome)
    store.append(actionable)

    results, report = store.search_with_diagnostics(
        query="UUMit 交付品质 海报 v2",
        kinds=["memory", "claim_card"],
        scope=scope,
        limit=5,
        recall_filters={
            "intent_name": "project_delivery",
            "memory_cube": "project",
            "preferred_kinds": ("memory", "rule", "raw_chunk", "reflection"),
            "suppressed_kinds": ("knowledge_page",),
        },
    )

    assert results
    assert results[0].record_id == actionable.record_id
    assert all(item.record_id != raw_outcome.record_id for item in results)
    assert report["retrieval_mode"] == "recall_index_hybrid"
    assert report["candidate_count"] < 5


def test_runtime_store_recall_index_keeps_reflections_searchable_when_requested(tmp_path) -> None:
    store = RuntimeStore(root=tmp_path)
    scope = ScopeRef(agent_id="main", workspace_id="ops")
    reflection = RecordEnvelope.create(
        kind="reflection",
        title="Deployment report",
        summary="eimemory deployment report for release 898bd47.",
        scope=scope,
        source="eimemory.scheduler.nightly",
        meta={"report_type": "nightly"},
    )
    store.append(reflection)

    results = store.search(
        query="deployment report release",
        kinds=["reflection"],
        scope=scope,
        limit=5,
    )

    assert [item.record_id for item in results] == [reflection.record_id]


def test_runtime_store_recall_index_limits_candidates_before_rerank(tmp_path) -> None:
    store = RuntimeStore(root=tmp_path)
    scope = ScopeRef(agent_id="main", workspace_id="scale")
    target = RecordEnvelope.create(
        kind="memory",
        title="UUMit delivery acceptance rule",
        summary="UUMit 外部订单 交付品质 海报 v2 必须按需求清单逐条验收。",
        scope=scope,
        source="operator.correction",
        meta={"memory_type": "preference"},
    )
    store.append(target)
    for index in range(180):
        store.append(
            RecordEnvelope.create(
                kind="reflection",
                title=f"OpenClaw agent outcome {index}",
                summary=f"UUMit 交付品质 海报 v2 noisy operational report {index}.",
                scope=scope,
                source="openclaw.agent_end",
            )
        )

    results, report = store.search_with_diagnostics(
        query="UUMit 交付品质 海报 v2",
        kinds=["memory", "reflection"],
        scope=scope,
        limit=5,
        recall_filters={"intent_name": "project_delivery", "memory_cube": "project"},
    )

    assert results[0].record_id == target.record_id
    assert all(item.kind != "reflection" for item in results)
    assert report["candidate_count"] < 180


def test_runtime_store_recall_index_empty_existing_db_falls_back_without_startup_backfill(tmp_path) -> None:
    store = RuntimeStore(root=tmp_path)
    scope = ScopeRef(agent_id="main", workspace_id="fallback")
    target = RecordEnvelope.create(
        kind="memory",
        title="Fallback recall target",
        summary="Fallback recall keeps existing production databases searchable before offline index rebuild.",
        scope=scope,
        source="operator.correction",
        meta={"memory_type": "preference"},
    )
    store.append(target)
    store.sqlite.conn.execute("DELETE FROM recall_index")
    if store.sqlite._has_fts_table():
        store.sqlite.conn.execute("DELETE FROM recall_index_fts")
    store.sqlite.conn.commit()
    store.close()

    reopened = RuntimeStore(root=tmp_path)
    try:
        index_count = reopened.sqlite.conn.execute("SELECT COUNT(*) FROM recall_index").fetchone()[0]
        results, report = reopened.search_with_diagnostics(
            query="existing production searchable offline rebuild",
            kinds=["memory"],
            scope=scope,
            limit=5,
        )
    finally:
        reopened.close()

    assert index_count == 0
    assert results[0].record_id == target.record_id
    assert report["candidate_fallback"] == "legacy_scan"
    assert report["candidate_sources"]["legacy_scan"] >= 1


def test_runtime_store_search_with_knowledge_penalty_for_non_research_queries(tmp_path) -> None:
    store = RuntimeStore(root=tmp_path)
    scope = ScopeRef(agent_id="main", workspace_id="project")
    memory = RecordEnvelope.create(
        kind="memory",
        title="UUMit 交付记录",
        summary="UUMit 外部订单 交付品质 海报 v2",
        scope=scope,
    )
    knowledge_page = RecordEnvelope.create(
        kind="knowledge_page",
        title="Graphit-like 交付指标论文",
        summary="该论文讨论交付品质与指标。",
        scope=scope,
    )
    store.append(memory)
    store.append(knowledge_page)

    _, report = store.search_with_diagnostics(
        query="UUMit 交付品质 海报 v2",
        kinds=["memory", "knowledge_page"],
        scope=scope,
        limit=5,
        recall_filters={
            "intent_name": "project_delivery",
            "preferred_kinds": ("memory", "rule"),
            "suppressed_kinds": ("knowledge_page",),
            "kind_weights": {"knowledge_page": 0.72, "memory": 1.25},
        },
    )

    scored_items = report["scored_items"]
    knowledge_entry = next(item for item in scored_items if item["kind"] == "knowledge_page")
    assert knowledge_entry["kind_intent_adjustment"] < 0
    assert knowledge_entry["kind_intent_penalty"]


def test_runtime_store_search_does_not_report_kind_penalty_when_no_penalty_applied(tmp_path) -> None:
    store = RuntimeStore(root=tmp_path)
    scope = ScopeRef(agent_id="main", workspace_id="news")
    page = RecordEnvelope.create(
        kind="knowledge_page",
        title="AI 新闻页面",
        summary="AI 新闻摘要。",
        scope=scope,
        source="eimemory.news.digest",
    )
    store.append(page)

    _, report = store.search_with_diagnostics(
        query="AI 新闻",
        kinds=["knowledge_page"],
        scope=scope,
        limit=1,
        recall_filters={"intent_name": "news"},
    )

    assert report["scored_items"][0]["kind_intent_adjustment"] == 0.0
    assert report["scored_items"][0]["kind_intent_penalty"] == ""
