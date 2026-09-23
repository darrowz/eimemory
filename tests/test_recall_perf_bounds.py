"""PERF P0: FTS top-N equivalence safety net (no ranking semantics change)."""
from __future__ import annotations

from eimemory.models.records import RecordEnvelope, ScopeRef
from eimemory.storage.runtime_store import RuntimeStore


SCOPE = ScopeRef(tenant_id="default", agent_id="hongtu", workspace_id="perf-fts", user_id="darrow")


def _seed_varied_tiebreaks(store: RuntimeStore, n: int = 200) -> None:
    for i in range(n):
        store.append(
            RecordEnvelope.create(
                kind="memory",
                title=f"alpha deployment marker {i}",
                summary=f"alpha deployment marker {i}",
                scope=SCOPE,
                source="test.perf",
                content={"text": f"alpha deployment marker {i}"},
                meta={"force_capture": True},
            )
        )
    with store._lock:
        store.sqlite.execute(
            "UPDATE recall_index SET quality_score = 0.5 + (rowid % 30) * 0.01,"
            " updated_at = '2026-01-' || printf('%02d', 1 + (rowid % 28)) || 'T00:00:00Z'"
        )
        store.sqlite.commit()


def _fts_topn_rows(store: RuntimeStore, *, limit: int = 5) -> list[dict]:
    with store._lock:
        where, params = store.sqlite._recall_index_where(
            kinds=["memory"], scope=SCOPE, recall_filters={}, alias="i"
        )
        sql = (
            "SELECT i.storage_key AS storage_key, i.quality_score AS quality_score,"
            " i.updated_at AS updated_at, bm25(recall_index_fts) AS bm25_score"
            " FROM recall_index_fts"
            " JOIN recall_index i ON i.storage_key = recall_index_fts.storage_key"
            " WHERE recall_index_fts MATCH ? AND " + " AND ".join(where)
            + " ORDER BY bm25_score ASC, i.quality_score DESC, i.updated_at DESC LIMIT ?"
        )
        return [
            {
                "storage_key": str(row["storage_key"]),
                "quality_score": float(row["quality_score"] or 0.0),
                "updated_at": str(row["updated_at"] or ""),
                "bm25_score": float(row["bm25_score"]),
            }
            for row in store.sqlite.conn.execute(sql, ["alpha", *params, limit]).fetchall()
        ]


def test_fts_topn_is_stable_under_varied_tiebreaks(tmp_path) -> None:
    """BASELINE: lock FTS top-N ordering (rank → quality → updated_at)."""
    store = RuntimeStore(tmp_path)
    _seed_varied_tiebreaks(store)
    first = _fts_topn_rows(store, limit=5)
    second = _fts_topn_rows(store, limit=5)
    assert len(first) == 5
    assert [row["storage_key"] for row in first] == [row["storage_key"] for row in second]
    # Authoritative composite order: bm25 ASC, quality DESC, updated_at DESC.
    for left, right in zip(first, first[1:]):
        if left["bm25_score"] != right["bm25_score"]:
            assert left["bm25_score"] < right["bm25_score"]
        elif left["quality_score"] != right["quality_score"]:
            assert left["quality_score"] > right["quality_score"]
        else:
            assert left["updated_at"] >= right["updated_at"]
    # Snapshot for future lexical rewrites: same seed → same ordered keys.
    snapshot = [(row["bm25_score"], row["quality_score"], row["updated_at"]) for row in first]
    assert snapshot == [(row["bm25_score"], row["quality_score"], row["updated_at"]) for row in second]
    store.close()


def test_fts_rank_has_real_ties(tmp_path) -> None:
    """DOC: real corpora have pervasive rank ties — secondary keys decide top-N."""
    store = RuntimeStore(tmp_path)
    _seed_varied_tiebreaks(store, n=120)
    rows = _fts_topn_rows(store, limit=40)
    ranks = [row["bm25_score"] for row in rows]
    assert len(ranks) >= 10
    assert len(set(ranks)) < len(ranks)
    # Secondary keys actually vary inside the tied window.
    tied = [row for row in rows if row["bm25_score"] == ranks[0]]
    assert len({(row["quality_score"], row["updated_at"]) for row in tied}) > 1
    store.close()


def _pragma_counts(store: RuntimeStore) -> dict[str, int]:
    counts: dict[str, int] = {}

    def tracer(sql: object) -> None:
        text = str(sql).strip().upper()
        if not text.startswith("PRAGMA"):
            return
        key = "PRAGMA"
        if "INDEX_LIST" in text and "RECALL_INDEX" in text:
            key = "PRAGMA index_list(recall_index)"
        elif "TABLE_INFO" in text and "RECALL_INDEX" in text:
            key = "PRAGMA table_info(recall_index)"
        elif text.startswith("PRAGMA"):
            key = text.split("(")[0].strip()
        counts[key] = counts.get(key, 0) + 1

    store._ensure_readers()
    for connection in (store.sqlite.conn, *(slot.store.conn for slot in store._readers)):
        connection.set_trace_callback(tracer)
    return counts


def test_recall_schema_pragma_bounded_after_ensure(tmp_path) -> None:
    """PERF P1 §3.1: single recall after ensure issues ≤2 recall_index PRAGMAs."""
    store = RuntimeStore(tmp_path)
    _seed_varied_tiebreaks(store, n=40)
    with store._lock:
        store.sqlite._ensure_recall_schema_once()
    counts = _pragma_counts(store)
    from eimemory.api.memory import MemoryAPI

    api = MemoryAPI(store=store)
    api.recall(
        query="alpha deployment",
        scope={
            "tenant_id": SCOPE.tenant_id,
            "agent_id": SCOPE.agent_id,
            "workspace_id": SCOPE.workspace_id,
            "user_id": SCOPE.user_id,
        },
        limit=5,
    )
    recall_index_pragmas = sum(
        n for key, n in counts.items() if "RECALL_INDEX" in key.upper() or "recall_index" in key
    )
    assert recall_index_pragmas <= 2, counts
    # Ignore busy_timeout PRAGMAs adjusted by the ≤3s recall deadline budget.
    schema_pragmas = {
        key: n for key, n in counts.items() if "BUSY_TIMEOUT" not in key.upper()
    }
    assert sum(schema_pragmas.values()) <= 2, schema_pragmas
    store.sqlite.conn.set_trace_callback(None)
    store.close()


def test_write_path_forces_schema_reverify(tmp_path) -> None:
    """PERF P1 §3.1: writes invalidate cache; force=True re-runs PRAGMA checks."""
    store = RuntimeStore(tmp_path)
    with store._lock:
        store.sqlite._ensure_recall_schema_once()
        assert store.sqlite._recall_schema_verified is True
    store.append(
        RecordEnvelope.create(
            kind="memory",
            title="write invalidate",
            summary="write invalidate",
            scope=SCOPE,
            source="test.perf",
            content={"text": "write invalidate alpha"},
            meta={"force_capture": True},
        )
    )
    assert store.sqlite._recall_schema_verified is False
    counts = _pragma_counts(store)
    with store._lock:
        store.sqlite._ensure_recall_schema_once(force=True)
    assert sum(counts.values()) > 0
    assert store.sqlite._recall_schema_verified is True
    store.sqlite.conn.set_trace_callback(None)
    store.close()


def test_pollution_gate_memoizes_index_document(tmp_path) -> None:
    """PERF P1 §3.2: compute count ≤ unique records; updated_at change invalidates."""
    from eimemory.api.memory import MemoryAPI
    from eimemory.recall import (
        build_recall_index_document,
        clear_recall_index_document_cache,
        recall_index_document_compute_count,
    )

    clear_recall_index_document_cache()
    store = RuntimeStore(tmp_path)
    records = []
    for i in range(8):
        records.append(
            store.append(
                RecordEnvelope.create(
                    kind="memory",
                    title=f"memo alpha {i}",
                    summary=f"memo alpha {i}",
                    scope=SCOPE,
                    source="test.perf",
                    content={"text": f"memo alpha body {i}"},
                    meta={"force_capture": True},
                )
            )
        )
    api = MemoryAPI(store=store)
    clear_recall_index_document_cache()
    before = recall_index_document_compute_count()
    # Simulate pollution gate walking the same records twice.
    for _ in range(2):
        api._apply_online_recall_pollution_gate(records, allow_operational_recall=False)
    after = recall_index_document_compute_count()
    assert after - before <= len(records)
    # updated_at change must miss the cache and recompute.
    target = records[0]
    target.time.updated_at = "2099-01-01T00:00:00Z"
    mid = recall_index_document_compute_count()
    doc = build_recall_index_document(target)
    assert doc.updated_at == "2099-01-01T00:00:00Z"
    assert recall_index_document_compute_count() == mid + 1
    store.close()
