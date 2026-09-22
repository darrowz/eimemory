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
