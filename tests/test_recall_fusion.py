from __future__ import annotations

from dataclasses import asdict
import json
import sqlite3

import pytest

from eimemory.api.memory import MemoryAPI
from eimemory.models.records import LinkRef, RecordEnvelope, ScopeRef
from eimemory.models.identity_aliases import IDENTITY_ALIASES_VERSION, normalize_identity_text
from eimemory.retrieval.fusion import (
    FUSION_POLICY_VERSION,
    fuse_ranked_components,
    page_pool_key,
)
from eimemory.retrieval.contracts import CandidateBatch, CandidateHit, CandidateRef, ExactScope
from eimemory.retrieval.engine import GovernedRecallEngine
from eimemory.retrieval.postgres_sync import SQLiteProjectionReader
from eimemory.storage.runtime_store import RuntimeStore
from eimemory.storage.sqlite_store import SqliteRecordStore


SCOPE = ScopeRef(
    tenant_id="tenant-a",
    agent_id="openclaw",
    workspace_id="workspace-a",
    user_id="user-a",
)


def _record(
    title: str,
    *,
    source_id: str = "alpha",
    aliases: list[str] | None = None,
    scope: ScopeRef = SCOPE,
    content: dict | None = None,
    meta: dict | None = None,
) -> RecordEnvelope:
    return RecordEnvelope.create(
        kind="memory",
        title=title,
        summary=title,
        content={"text": title, **dict(content or {})},
        scope=scope,
        source="test",
        source_id=source_id,
        aliases=aliases,
        meta={
            "memory_type": "fact",
            "quality": {"capture_decision": "accept", "salience_score": 0.8},
            **dict(meta or {}),
        },
    )


def _apply_all_storage_migrations(store: RuntimeStore, *, batch_size: int = 2) -> list[dict]:
    reports: list[dict] = []
    for _ in range(100):
        if not store.sqlite.pending_storage_migrations():
            break
        reports.append(
            store.sqlite.apply_storage_migrations(batch_size=batch_size, offline=True)
        )
    assert store.sqlite.pending_storage_migrations() == []
    return reports


def test_rrf_is_bounded_deterministic_and_exposes_math() -> None:
    result = fuse_ranked_components(
        [
            ("keyword", ["b", "a"]),
            ("vector", ["a", "b"]),
        ],
        weights={"keyword": 2.0, "vector": 1.0},
        rrf_k=-100,
        limit=10_000,
    )

    assert result.policy_version == FUSION_POLICY_VERSION
    assert result.rrf_k == 1
    assert result.limit == 1000
    assert [item.record_id for item in result.items] == ["b", "a"]
    assert result.items[0].ranks == {"keyword": 1, "vector": 2}
    assert result.items[0].contributions == {
        "keyword": pytest.approx(1.0),
        "vector": pytest.approx(1.0 / 3.0),
    }


def test_rrf_ties_use_stable_record_id_and_reject_duplicate_components() -> None:
    result = fuse_ranked_components(
        [("keyword", ["record-z", "record-a"]), ("vector", ["record-a", "record-z"])],
        weights={"keyword": 1.0, "vector": 1.0},
        rrf_k=60,
    )
    assert [item.record_id for item in result.items] == ["record-a", "record-z"]
    with pytest.raises(ValueError, match="duplicate fusion component"):
        fuse_ranked_components([("keyword", ["a"]), ("keyword", ["b"])])
    with pytest.raises(ValueError, match="unsupported fusion component"):
        fuse_ranked_components([("body_dump", ["a"])])


def test_record_aliases_are_nfkc_casefold_bounded_and_do_not_trust_arbitrary_meta() -> None:
    record = _record(
        "Canonical",
        aliases=[" Ａｌｐｈａ ", "alpha", "Beta", "", *(f"alias-{i}" for i in range(100))],
        meta={"aliases": ["META-MUST-NOT-BECOME-AN-ALIAS"]},
    )
    restored = RecordEnvelope.from_dict(record.to_dict())

    assert restored.aliases[:2] == ["alpha", "beta"]
    assert restored.aliases_version == IDENTITY_ALIASES_VERSION
    assert len(restored.aliases) == 32
    assert "meta-must-not-become-an-alias" not in restored.aliases
    assert len(json.dumps(restored.to_dict(), sort_keys=True)) < 20_000
    payload = restored.to_dict()
    payload["aliases_version"] = "record_aliases.v999"
    with pytest.raises(ValueError, match="unsupported aliases_version"):
        RecordEnvelope.from_dict(payload)


def test_entity_legacy_alias_projection_is_explicit_per_kind() -> None:
    entity = RecordEnvelope.create(
        kind="entity_record",
        title="Model Context Protocol",
        content={"aliases": ["MCP", "ｍｃｐ"]},
        meta={"aliases": ["untrusted-meta"]},
        scope=SCOPE,
        source_id="alpha",
    )
    ordinary = _record("ordinary", content={"aliases": ["not-projected"]})

    assert entity.aliases == ["mcp"]
    assert ordinary.aliases == []


@pytest.mark.parametrize(
    ("content", "expected_type"),
    [
        ({"page_id": "page-1"}, "page"),
        ({"parent_record_id": "parent-1"}, "parent"),
        ({"source_document_id": "document-1"}, "document"),
        ({"session_id": "session-1", "source_event_id": "event-1"}, "raw"),
        ({}, "record"),
    ],
)
def test_page_pool_key_has_hard_scope_source_namespace_and_priority(content: dict, expected_type: str) -> None:
    record = _record("chunk", content=content)
    key = page_pool_key(record)
    assert key.startswith(f"page-pool.v1:{expected_type}:")
    assert len(key) < 100
    other_source = _record("chunk", source_id="beta", content=content)
    other_source.record_id = record.record_id
    assert page_pool_key(other_source) != key


def test_indexed_identity_is_source_local_and_target_source_is_not_search_allowlist(tmp_path) -> None:
    store = RuntimeStore(tmp_path)
    alpha = store.append(_record("Alpha title", source_id="alpha", aliases=["shared alias"]))
    beta = store.append(_record("Beta title", source_id="beta", aliases=["shared alias"]))
    foreign = store.append(
        _record(
            "Foreign title",
            source_id="alpha",
            aliases=["shared alias"],
            scope=ScopeRef(
                tenant_id="tenant-b",
                agent_id="openclaw",
                workspace_id="workspace-a",
                user_id="user-a",
            ),
        )
    )

    bundle = MemoryAPI(store).recall(
        query="shared alias",
        scope=asdict(SCOPE),
        task_context={"source_ids": ["alpha", "beta"], "target_source_id": "alpha"},
        limit=5,
    )

    assert {item.record_id for item in bundle.items} == {alpha.record_id, beta.record_id}
    assert foreign.record_id not in {item.record_id for item in bundle.items}
    fusion = bundle.explanation["fusion"]
    assert fusion["create_safety"] == "exists"
    assert fusion["ambiguity_reasons"] == []
    by_id = {item["record_id"]: item for item in fusion["selected"]}
    assert by_id[alpha.record_id]["evidence"] == ["alias_hit"]
    assert by_id[alpha.record_id]["create_safety"] == "exists"
    assert by_id[beta.record_id]["create_safety"] == "unknown"
    store.close()


def test_conflicting_alias_in_target_source_is_probable_and_title_is_unique_exists(tmp_path) -> None:
    store = RuntimeStore(tmp_path)
    first = store.append(_record("First", aliases=["collision"]))
    second = store.append(_record("Second", aliases=["collision"]))
    titled = store.append(_record("Unique identity"))

    ambiguous = MemoryAPI(store).recall(
        query="collision",
        scope=asdict(SCOPE),
        task_context={"source_ids": ["alpha"], "target_source_id": "alpha"},
        limit=5,
    )
    exact = MemoryAPI(store).recall(
        query=" UNIQUE   IDENTITY ",
        scope=asdict(SCOPE),
        task_context={"source_ids": ["alpha"], "target_source_id": "alpha"},
        limit=5,
    )

    assert {item.record_id for item in ambiguous.items} == {first.record_id, second.record_id}
    assert ambiguous.explanation["fusion"]["create_safety"] == "probable"
    assert "ambiguous_identity" in ambiguous.explanation["fusion"]["ambiguity_reasons"]
    assert exact.items[0].record_id == titled.record_id
    assert exact.explanation["fusion"]["create_safety"] == "exists"
    assert "exact_title" in exact.explanation["fusion"]["selected"][0]["evidence"]
    assert "alias_hit" not in exact.explanation["fusion"]["selected"][0]["evidence"]
    store.close()


def test_omitted_target_source_is_unknown_even_with_strong_evidence(tmp_path) -> None:
    store = RuntimeStore(tmp_path)
    store.append(_record("Exact title", aliases=["exact alias"]))
    bundle = MemoryAPI(store).recall(
        query="exact alias",
        scope=asdict(SCOPE),
        task_context={"source_ids": ["alpha"]},
        limit=3,
    )
    assert bundle.explanation["fusion"]["create_safety"] == "unknown"
    assert "target_source_omitted" in bundle.explanation["fusion"]["ambiguity_reasons"]
    store.close()


def test_page_max_pool_keeps_diverse_pages_and_aggregates_chunk_diagnostics(tmp_path) -> None:
    store = RuntimeStore(tmp_path)
    chunks = [
        store.append(_record(f"long document marker {index}", content={"page_id": "page-a"}))
        for index in range(4)
    ]
    other = store.append(_record("long document marker other", content={"page_id": "page-b"}))

    bundle = MemoryAPI(store).recall(
        query="long document marker",
        scope=asdict(SCOPE),
        task_context={"source_ids": ["alpha"], "target_source_id": "alpha"},
        limit=2,
    )

    assert len(bundle.items) == 2
    assert {item.content["page_id"] for item in bundle.items} == {"page-a", "page-b"}
    selected = bundle.explanation["fusion"]["selected"]
    pooled_key = page_pool_key(chunks[0])
    pooled = next(item for item in selected if item["page_key"] == pooled_key)
    assert pooled["chunk_count"] == 4
    assert set(pooled["member_record_ids"]) == {item.record_id for item in chunks}
    assert other.record_id in {item["record_id"] for item in selected}
    assert bundle.explanation["fusion"]["pre_pool_count"] == 5
    assert bundle.explanation["fusion"]["post_pool_count"] == 2
    assert "long document marker" not in json.dumps(bundle.explanation["fusion"]).lower()
    store.close()


def test_rrf_order_change_from_legacy_weight_is_explicit_and_replay_stable(tmp_path) -> None:
    store = RuntimeStore(tmp_path)
    keyword = _record("intentional rrf marker", aliases=[])
    vector = _record("peripheral semantic record", aliases=[])
    store.append(keyword)
    store.append(vector)

    first = MemoryAPI(store).recall(
        query="intentional rrf marker",
        scope=asdict(SCOPE),
        task_context={"source_ids": ["alpha"], "target_source_id": "alpha"},
        limit=2,
    )
    second = MemoryAPI(store).recall(
        query="intentional rrf marker",
        scope=asdict(SCOPE),
        task_context={"source_ids": ["alpha"], "target_source_id": "alpha"},
        limit=2,
    )

    assert first.explanation["fusion"]["policy_version"] == FUSION_POLICY_VERSION
    assert first.explanation["fusion"]["ranking_change"] == "intentional_rrf_replaces_hand_weight_order"
    assert json.dumps(first.explanation["fusion"], sort_keys=True) == json.dumps(
        second.explanation["fusion"], sort_keys=True
    )
    store.close()


@pytest.mark.parametrize(
    "damage",
    [
        "missing_marker", "wrong_title_index", "wrong_alias_index", "title_column", "alias_column",
        "alias_table", "partial_title_index", "partial_alias_index", "alias_collation", "alias_primary_key",
        "title_wrong_type_nullable", "storage_key_not_primary",
    ],
)
def test_markered_identity_migration_repairs_physical_schema_and_backfills(tmp_path, damage: str) -> None:
    root = tmp_path / damage
    store = RuntimeStore(root)
    record = store.append(_record("Migration title", aliases=["migration alias"]))
    store.close()
    db_path = root / "state" / "eimemory.sqlite"
    connection = sqlite3.connect(db_path)
    if damage == "missing_marker":
        connection.execute("DELETE FROM schema_migrations WHERE migration_id = 'recall.identity_index.v1'")
    elif damage == "wrong_title_index":
        connection.execute("DROP INDEX idx_recall_title_exact")
        connection.execute("CREATE INDEX idx_recall_title_exact ON recall_index(title_normalized, storage_key)")
    elif damage == "wrong_alias_index":
        connection.execute("DROP INDEX idx_recall_alias_exact")
        connection.execute("CREATE INDEX idx_recall_alias_exact ON recall_alias_index(normalized_alias, storage_key)")
    elif damage == "title_column":
        connection.execute("ALTER TABLE recall_index RENAME COLUMN title_normalized TO legacy_title_normalized")
    elif damage == "alias_column":
        connection.execute("ALTER TABLE recall_alias_index RENAME COLUMN normalized_alias TO legacy_normalized_alias")
    elif damage == "partial_title_index":
        connection.execute("DROP INDEX idx_recall_title_exact")
        connection.execute(
            "CREATE INDEX idx_recall_title_exact ON recall_index("
            "tenant_id, agent_id, workspace_id, user_id, source_id, title_normalized, status, kind, storage_key"
            ") WHERE status = 'active'"
        )
    elif damage == "partial_alias_index":
        connection.execute("DROP INDEX idx_recall_alias_exact")
        connection.execute(
            "CREATE INDEX idx_recall_alias_exact ON recall_alias_index("
            "tenant_id, agent_id, workspace_id, user_id, source_id, normalized_alias, status, kind, storage_key"
            ") WHERE status = 'active'"
        )
    elif damage == "alias_collation":
        connection.execute("DROP INDEX idx_recall_alias_exact")
        connection.execute(
            "CREATE INDEX idx_recall_alias_exact ON recall_alias_index("
            "tenant_id, agent_id, workspace_id, user_id, source_id, normalized_alias COLLATE NOCASE, status, kind, storage_key)"
        )
    elif damage == "alias_primary_key":
        connection.execute("DROP TABLE recall_alias_index")
        connection.execute(
            "CREATE TABLE recall_alias_index ("
            "storage_key TEXT PRIMARY KEY, normalized_alias TEXT NOT NULL, alias_ordinal INTEGER NOT NULL, "
            "record_id TEXT NOT NULL, kind TEXT NOT NULL, status TEXT NOT NULL, source_id TEXT NOT NULL, "
            "tenant_id TEXT NOT NULL, agent_id TEXT NOT NULL, workspace_id TEXT NOT NULL, "
            "user_id TEXT NOT NULL)"
        )
    elif damage in {"title_wrong_type_nullable", "storage_key_not_primary"}:
        connection.execute("ALTER TABLE recall_index RENAME TO recall_index_legacy")
        storage_key_declaration = (
            "storage_key TEXT NOT NULL" if damage == "storage_key_not_primary" else "storage_key TEXT PRIMARY KEY"
        )
        title_declaration = (
            "title_normalized BLOB"
            if damage == "title_wrong_type_nullable"
            else "title_normalized TEXT NOT NULL DEFAULT ''"
        )
        connection.execute(
            "CREATE TABLE recall_index ("
            + storage_key_declaration
            + ", record_id TEXT NOT NULL, kind TEXT NOT NULL, status TEXT NOT NULL, "
            "source TEXT NOT NULL, source_id TEXT NOT NULL DEFAULT 'default', tenant_id TEXT NOT NULL, "
            "agent_id TEXT NOT NULL, workspace_id TEXT NOT NULL, user_id TEXT NOT NULL, lane TEXT NOT NULL, "
            "visibility TEXT NOT NULL, source_class TEXT NOT NULL, memory_type TEXT NOT NULL, "
            "projection_type TEXT NOT NULL, quality_score REAL NOT NULL DEFAULT 0.0, title_text TEXT NOT NULL, "
            + title_declaration
            + ", body_text TEXT NOT NULL, anchor_terms TEXT NOT NULL, updated_at TEXT NOT NULL)"
        )
        columns = (
            "storage_key, record_id, kind, status, source, source_id, tenant_id, agent_id, workspace_id, "
            "user_id, lane, visibility, source_class, memory_type, projection_type, quality_score, title_text, "
            "title_normalized, body_text, anchor_terms, updated_at"
        )
        connection.execute(f"INSERT INTO recall_index ({columns}) SELECT {columns} FROM recall_index_legacy")
        connection.execute("DROP TABLE recall_index_legacy")
        if damage == "storage_key_not_primary":
            connection.execute(
                "CREATE INDEX idx_recall_index_scope_source_updated ON recall_index("
                "tenant_id, agent_id, workspace_id, user_id, source_id, updated_at DESC, quality_score, "
                "status, lane, visibility, storage_key)"
            )
            connection.execute("CREATE INDEX idx_recall_index_storage_key ON recall_index(storage_key)")
            connection.execute(
                "CREATE INDEX idx_recall_title_exact ON recall_index("
                "tenant_id, agent_id, workspace_id, user_id, title_normalized, status, source_id, kind, storage_key)"
            )
            connection.execute(
                "CREATE INDEX idx_recall_title_exact_kind ON recall_index("
                "tenant_id, agent_id, workspace_id, user_id, title_normalized, status, kind, source_id, storage_key)"
            )
    else:
        connection.execute("DROP TABLE recall_alias_index")
    connection.commit()
    connection.close()

    repaired = RuntimeStore(root)
    assert "recall.identity_index.v1" in repaired.sqlite.pending_storage_migrations()
    _apply_all_storage_migrations(repaired)
    assert repaired.sqlite._recall_identity_physical_ready() is True
    storage_key_column = next(
        row for row in repaired.sqlite.conn.execute("PRAGMA table_info(recall_index)") if row["name"] == "storage_key"
    )
    assert int(storage_key_column["pk"]) == 1
    hits = repaired.sqlite.search_identity_candidates(
        query="migration alias",
        kinds=["memory"],
        scope=SCOPE,
        limit=5,
        recall_filters={"_exact_scope": True},
        source_ids=["alpha"],
    )
    assert [item["record_id"] for item in hits] == [record.record_id]
    assert hits[0]["evidence"] == ["alias_hit"]
    alias_pk = {
        row["name"]: int(row["pk"])
        for row in repaired.sqlite.conn.execute("PRAGMA table_info(recall_alias_index)")
    }
    assert alias_pk["storage_key"] == 1 and alias_pk["normalized_alias"] == 2
    for table, index_name in (
        ("recall_index", "idx_recall_title_exact"),
        ("recall_alias_index", "idx_recall_alias_exact"),
    ):
        index_row = next(
            row for row in repaired.sqlite.conn.execute(f"PRAGMA index_list({table})") if row["name"] == index_name
        )
        assert int(index_row["partial"]) == 0
        collations = [row["coll"] for row in repaired.sqlite.conn.execute(f"PRAGMA index_xinfo({index_name})") if row["key"]]
        assert set(collations) == {"BINARY"}
    repaired.append(_record("multi alias after repair", aliases=["multi one", "multi two"]))
    repaired.close()
    reopened = RuntimeStore(root)
    assert reopened.sqlite._recall_identity_physical_ready() is True


def test_restart_recreates_missing_fts_without_startup_payload_scan_then_defers_backfill(
    tmp_path, monkeypatch
) -> None:
    root = tmp_path / "missing-fts"
    store = RuntimeStore(root)
    record = store.append(_record("fts restart sentinel"))
    store.close()
    connection = sqlite3.connect(root / "state" / "eimemory.sqlite")
    connection.execute("DROP TABLE recall_index_fts")
    connection.commit()
    connection.close()

    monkeypatch.setattr(
        SqliteRecordStore,
        "_payload_dict_from_json",
        lambda _self, _payload: (_ for _ in ()).throw(
            AssertionError("startup decoded a historical payload")
        ),
    )
    reopened = RuntimeStore(root)

    assert reopened.sqlite._has_fts_table() is True
    assert "recall.identity_index.v1" in reopened.sqlite.pending_storage_migrations()
    assert reopened.sqlite.conn.execute(
        "SELECT 1 FROM recall_index_fts LIMIT 1"
    ).fetchone() is None
    monkeypatch.undo()
    _apply_all_storage_migrations(reopened)
    assert reopened.sqlite.conn.execute(
        "SELECT storage_key FROM recall_index_fts WHERE recall_index_fts MATCH ?",
        ('"fts"',),
    ).fetchone()[0] == reopened.sqlite._storage_key(record)
    reopened.close()


def test_restart_restores_missing_vector_alias_trigger_without_payload_scan(
    tmp_path, monkeypatch
) -> None:
    root = tmp_path / "missing-vector-trigger"
    store = RuntimeStore(root)
    SQLiteProjectionReader(store).snapshot_token()
    store.sqlite.conn.execute("DROP TRIGGER trg_recall_alias_vector_sync_insert")
    store.sqlite.conn.commit()
    store.close()

    monkeypatch.setattr(
        SqliteRecordStore,
        "_payload_dict_from_json",
        lambda _self, _payload: (_ for _ in ()).throw(
            AssertionError("startup decoded a historical payload")
        ),
    )
    reopened = RuntimeStore(root)
    trigger = reopened.sqlite.conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='trigger' "
        "AND name='trg_recall_alias_vector_sync_insert'"
    ).fetchone()
    reopened.close()

    assert trigger is not None
    reopened.close()


def test_exact_identity_queries_use_covering_indexes(tmp_path) -> None:
    store = RuntimeStore(tmp_path)
    store.append(_record("Indexed title", aliases=["indexed alias"]))
    title_plan = store.sqlite.conn.execute(
        "EXPLAIN QUERY PLAN SELECT storage_key FROM recall_index WHERE "
        "tenant_id = ? AND agent_id = ? AND workspace_id = ? AND user_id = ? AND source_id = ? "
        "AND title_normalized = ? AND status = ? AND kind = ? ORDER BY storage_key LIMIT 5",
        ("tenant-a", "openclaw", "workspace-a", "user-a", "alpha", "indexed title", "active", "memory"),
    ).fetchall()
    alias_plan = store.sqlite.conn.execute(
        "EXPLAIN QUERY PLAN SELECT storage_key FROM recall_alias_index WHERE "
        "tenant_id = ? AND agent_id = ? AND workspace_id = ? AND user_id = ? AND source_id = ? "
        "AND normalized_alias = ? AND status = ? AND kind = ? ORDER BY storage_key LIMIT 5",
        ("tenant-a", "openclaw", "workspace-a", "user-a", "alpha", "indexed alias", "active", "memory"),
    ).fetchall()
    assert any("COVERING INDEX idx_recall_title_exact" in row[3] for row in title_plan)
    assert any("COVERING INDEX idx_recall_alias_exact" in row[3] for row in alias_plan)
    store.close()


def test_default_explanation_exposes_all_versioned_component_weights(tmp_path) -> None:
    store = RuntimeStore(tmp_path)
    store.append(_record("component policy"))
    fusion = MemoryAPI(store).recall(
        query="component policy",
        scope=asdict(SCOPE),
        task_context={"source_ids": ["alpha"], "target_source_id": "alpha"},
        limit=1,
    ).explanation["fusion"]
    assert set(fusion["weights"]) == {
        "exact_title", "exact_alias", "keyword", "vector", "graph", "living", "usage"
    }
    assert fusion["rrf_k"] == 60
    store.close()


def test_target_source_outside_search_allowlist_stays_unknown(tmp_path) -> None:
    store = RuntimeStore(tmp_path)
    store.append(_record("target mismatch", source_id="alpha"))
    fusion = MemoryAPI(store).recall(
        query="target mismatch",
        scope=asdict(SCOPE),
        task_context={"source_ids": ["alpha"], "target_source_id": "beta"},
        limit=1,
    ).explanation["fusion"]
    assert fusion["create_safety"] == "unknown"
    assert fusion["ambiguity_reasons"] == ["target_source_not_searched"]
    store.close()


def test_graph_and_usage_components_are_actual_and_explainable(tmp_path) -> None:
    store = RuntimeStore(tmp_path)
    related = store.append(_record("unrelated linked context"))
    seed = _record("graph component seed")
    seed.links = [LinkRef(relation="related", target_kind="memory", target_id=related.record_id)]
    seed = store.append(seed)
    feedback = RecordEnvelope.create(
        kind="feedback",
        title="usage feedback",
        content={
            "report_type": "memory_usage_telemetry",
            "schema_version": "memory_usage_telemetry.v1",
            "used_record_ids": [related.record_id],
            "rejected_record_ids": [],
        },
        meta={"report_type": "memory_usage_telemetry"},
        scope=SCOPE,
        source_id="alpha",
    )
    store.append(feedback)

    bundle = MemoryAPI(store).recall(
        query="graph component seed",
        scope=asdict(SCOPE),
        task_context={"source_ids": ["alpha"], "target_source_id": "alpha"},
        limit=5,
    )
    by_id = {item["record_id"]: item for item in bundle.explanation["fusion"]["selected"]}
    assert "graph_path" in by_id[related.record_id]["evidence"]
    assert "graph" in by_id[related.record_id]["ranks"]
    assert "usage" in by_id[related.record_id]["ranks"]
    assert "graph_path" not in by_id[seed.record_id]["evidence"]
    store.close()


def test_mutated_aliases_are_normalized_before_payload_and_index_persist(tmp_path) -> None:
    store = RuntimeStore(tmp_path)
    record = _record("mutated aliases")
    record.aliases = [" ＭＵＴＡＴＥＤ ", "mutated", *(f"extra-{index}" for index in range(100))]
    stored = store.append(record)
    assert stored.aliases[0] == "mutated"
    assert len(stored.aliases) == 32
    payload = store.sqlite.conn.execute(
        "SELECT payload_json FROM records WHERE record_id = ?", (stored.record_id,)
    ).fetchone()[0]
    assert json.loads(payload)["aliases"] == stored.aliases
    assert store.sqlite.search_identity_candidates(
        query="mutated", kinds=["memory"], scope=SCOPE, limit=5, source_ids=["alpha"]
    )[0]["record_id"] == stored.record_id
    store.close()


def test_corrupt_alias_projection_cannot_forge_identity_evidence(tmp_path) -> None:
    store = RuntimeStore(tmp_path)
    record = store.append(_record("real identity", aliases=["real alias"]))
    store.sqlite.conn.execute(
        "UPDATE recall_alias_index SET normalized_alias = 'forged alias' WHERE record_id = ?",
        (record.record_id,),
    )
    store.sqlite.conn.commit()
    bundle = MemoryAPI(store).recall(
        query="forged alias",
        scope=asdict(SCOPE),
        task_context={"source_ids": ["alpha"], "target_source_id": "alpha"},
        limit=3,
    )
    assert bundle.items == []
    assert bundle.explanation["fusion"]["create_safety"] == "unknown"
    store.close()


def test_page_pool_diagnostics_are_bounded_but_aggregate_all_chunks(tmp_path) -> None:
    store = RuntimeStore(tmp_path)
    for index in range(70):
        store.append(_record(f"bounded pool marker {index}", content={"page_id": "one-page"}))
    bundle = MemoryAPI(store).recall(
        query="bounded pool marker",
        scope=asdict(SCOPE),
        task_context={"source_ids": ["alpha"], "target_source_id": "alpha"},
        limit=32,
    )
    diagnostic = bundle.explanation["fusion"]["selected"][0]
    assert diagnostic["chunk_count"] == 70
    assert len(diagnostic["member_record_ids"]) == 64
    assert diagnostic["member_record_ids_truncated"] == 6
    assert diagnostic["aggregate_score"] > diagnostic["score"]
    store.close()


def test_alias_identity_round_trips_jsonl_rebuild_and_recall_serialization(tmp_path) -> None:
    store = RuntimeStore(tmp_path)
    record = store.append(_record("rebuild identity", aliases=["rebuild alias"]))
    before = MemoryAPI(store).recall(
        query="rebuild alias",
        scope=asdict(SCOPE),
        task_context={"source_ids": ["alpha"], "target_source_id": "alpha"},
        limit=2,
    )
    assert store.rebuild_sqlite_from_jsonl(replace=True)["ok"] is True
    after = MemoryAPI(store).recall(
        query="rebuild alias",
        scope=asdict(SCOPE),
        task_context={"source_ids": ["alpha"], "target_source_id": "alpha"},
        limit=2,
    )
    assert [item.record_id for item in after.items] == [record.record_id]
    assert after.items[0].aliases == ["rebuild alias"]
    assert json.dumps(before.explanation["fusion"], sort_keys=True) == json.dumps(
        after.explanation["fusion"], sort_keys=True
    )
    store.close()


def test_keyword_exact_evidence_uses_token_boundaries_not_substrings() -> None:
    engine = object.__new__(__import__("eimemory.retrieval.engine", fromlist=["GovernedRecallEngine"]).GovernedRecallEngine)
    record = _record("educate retrieval")
    assert engine._keyword_exact_match("cat", record) is False
    assert engine._keyword_exact_match("retrieval", record) is True


def test_keyword_query_signal_is_analyzed_once_per_fusion(tmp_path, monkeypatch) -> None:
    from eimemory.retrieval import engine as engine_module

    store = RuntimeStore(tmp_path)
    for index in range(6):
        store.append(_record(f"Bounded fusion query record {index}"))
    original = engine_module.analyze_lexical_signal
    query_analysis_calls = 0

    def tracking_analysis(query, text, **kwargs):
        nonlocal query_analysis_calls
        if query == text == "bounded fusion query":
            query_analysis_calls += 1
        return original(query, text, **kwargs)

    monkeypatch.setattr(engine_module, "analyze_lexical_signal", tracking_analysis)
    MemoryAPI(store).recall(query="bounded fusion query", scope=asdict(SCOPE), limit=6)

    assert query_analysis_calls == 1
    store.close()


def test_overlong_identity_text_fails_closed_instead_of_prefix_colliding() -> None:
    assert normalize_identity_text("x" * 257) == ""
    record = _record("bounded identity", aliases=["x" * 256 + "a", "safe alias"])
    assert record.aliases == ["safe alias"]


def test_identity_index_cannot_bypass_quality_reject_hard_gate(tmp_path) -> None:
    store = RuntimeStore(tmp_path)
    rejected = _record("quality rejected identity", aliases=["rejected alias"])
    rejected.meta["quality"]["capture_decision"] = "reject"
    rejected.status = "active"
    store.append(rejected)
    bundle = MemoryAPI(store).recall(
        query="rejected alias",
        scope=asdict(SCOPE),
        task_context={"source_ids": ["alpha"], "target_source_id": "alpha"},
        limit=3,
    )
    assert bundle.items == []
    assert bundle.explanation["fusion"]["create_safety"] == "unknown"


@pytest.mark.parametrize(("vector_score", "expected"), [(0.13, "unknown"), (0.8, "probable")])
def test_create_safety_requires_strong_vector_evidence(tmp_path, vector_score: float, expected: str) -> None:
    store = RuntimeStore(tmp_path)
    record = store.append(_record("unrelated backend candidate"))

    class VectorSource:
        name = "vector-test"

        def search(self, request):
            return CandidateBatch(
                hits=(
                    CandidateHit(
                        ref=CandidateRef(
                            record_id=record.record_id,
                            scope=ExactScope.from_scope(record.scope),
                            source_id=record.source_id,
                        ),
                        source_rank=1,
                        source_score=vector_score,
                        component_hints={"vector_score": vector_score},
                    ),
                )
            )

    bundle = MemoryAPI(
        store,
        recall_engine=GovernedRecallEngine(store=store, candidate_source=VectorSource()),
    ).recall(
        query="no lexical overlap query",
        scope=asdict(SCOPE),
        task_context={"source_ids": ["alpha"], "target_source_id": "alpha"},
        limit=2,
    )
    assert bundle.explanation["fusion"]["create_safety"] == expected
    store.close()


def test_same_page_alias_collision_remains_ambiguous_before_pooling(tmp_path) -> None:
    store = RuntimeStore(tmp_path)
    store.append(_record("collision one", aliases=["same identity"], content={"page_id": "same-page"}))
    store.append(_record("collision two", aliases=["same identity"], content={"page_id": "same-page"}))
    fusion = MemoryAPI(store).recall(
        query="same identity",
        scope=asdict(SCOPE),
        task_context={"source_ids": ["alpha"], "target_source_id": "alpha"},
        limit=3,
    ).explanation["fusion"]
    assert fusion["create_safety"] == "probable"
    assert fusion["ambiguity_reasons"] == ["ambiguous_identity"]
    assert fusion["selected"][0]["chunk_count"] == 2
    store.close()


def test_page_pool_key_is_collision_safe_for_long_and_raw_identifiers() -> None:
    long_left = _record("left", content={"page_id": "x" * 256 + "a"})
    long_right = _record("right", content={"page_id": "x" * 256 + "b"})
    raw_left = _record("raw-left", content={"session_id": "a:b", "source_event_id": "c"})
    raw_right = _record("raw-right", content={"session_id": "a", "source_event_id": "b:c"})
    assert page_pool_key(long_left) != page_pool_key(long_right)
    assert page_pool_key(raw_left) != page_pool_key(raw_right)


@pytest.mark.parametrize(
    ("kinds", "source_ids"),
    [
        (["memory"], ["alpha"]),
        (["memory"], ["alpha", "beta"]),
        (None, ["alpha"]),
        (None, ["alpha", "beta"]),
        (["memory"], None),
        (None, None),
    ],
)
def test_actual_alias_query_uses_covering_alias_index_without_temp_sort(
    tmp_path,
    kinds: list[str] | None,
    source_ids: list[str] | None,
) -> None:
    store = RuntimeStore(tmp_path)
    store.append(_record("plan title", aliases=["plan alias"]))
    traced: list[str] = []
    store.sqlite.conn.set_trace_callback(traced.append)
    store.sqlite.search_identity_candidates(
        query="plan alias", kinds=kinds, scope=SCOPE, limit=5, source_ids=source_ids
    )
    store.sqlite.conn.set_trace_callback(None)
    alias_sql = next(sql for sql in traced if "FROM recall_alias_index a" in sql)
    title_sql = next(sql for sql in traced if "FROM recall_index i INDEXED BY idx_recall_title_exact" in sql)
    for sql, expected_index in (
        (alias_sql, "idx_recall_alias_exact"),
        (title_sql, "idx_recall_title_exact"),
    ):
        plan = store.sqlite.conn.execute("EXPLAIN QUERY PLAN " + sql).fetchall()
        details = [str(row[3]) for row in plan]
        assert any(expected_index in detail for detail in details)
        assert not any("SCAN i" in detail or "TEMP B-TREE" in detail for detail in details)
    store.close()


def test_alias_query_uses_explicit_stable_order_before_bounded_limit(tmp_path) -> None:
    store = RuntimeStore(tmp_path)
    for index, source_id in enumerate(("beta", "alpha", "gamma", "alpha")):
        record = _record(f"stable alias {index}", source_id=source_id, aliases=["shared stable alias"])
        record.record_id = f"stable-{3 - index}"
        store.append(record)
    traced: list[str] = []
    store.sqlite.conn.set_trace_callback(traced.append)
    first = store.sqlite.search_identity_candidates(
        query="shared stable alias", kinds=["memory"], scope=SCOPE, limit=2, source_ids=None
    )
    second = store.sqlite.search_identity_candidates(
        query="shared stable alias", kinds=["memory"], scope=SCOPE, limit=2, source_ids=None
    )
    store.sqlite.conn.set_trace_callback(None)
    alias_sql = next(sql for sql in traced if "FROM recall_alias_index a" in sql)
    assert (
        "ORDER BY a.tenant_id, a.agent_id, a.workspace_id, a.user_id, a.normalized_alias, "
        "a.status, a.kind, a.source_id, a.storage_key"
    ) in alias_sql
    assert [(row["source_id"], row["storage_key"]) for row in first] == [
        (row["source_id"], row["storage_key"]) for row in second
    ]
    assert [row["source_id"] for row in first] == ["alpha", "alpha"]
    store.close()


def test_report_and_rule_promotions_cannot_bypass_hard_filters(tmp_path) -> None:
    store = RuntimeStore(tmp_path)
    report = RecordEnvelope.create(
        kind="reflection",
        title="blocked report",
        summary="blocked report",
        content={"report_type": "rule_evolution"},
        meta={"report_type": "rule_evolution"},
        scope=SCOPE,
        source="blocked.report",
        source_id="alpha",
    )
    report.record_id = "rule_evolution_blocked_probe"
    rule = RecordEnvelope.create(
        kind="rule",
        title="Communication style reply preference",
        summary="Communication style reply preference concise",
        scope=SCOPE,
        source="blocked.rules",
        source_id="alpha",
    )
    store.append(report)
    store.append(rule)
    memory = MemoryAPI(store)
    report_bundle = memory.recall(
        query="governance report rule_evolution_blocked_probe",
        scope=asdict(SCOPE),
        task_context={"source_ids": ["alpha"], "blocked_sources": ["blocked.report"]},
        limit=3,
    )
    rule_bundle = memory.recall(
        query="What is my reply style preference?",
        scope=asdict(SCOPE),
        task_context={"source_ids": ["alpha"], "blocked_sources": ["blocked.rules"]},
        limit=3,
    )
    assert report.record_id not in {item.record_id for item in report_bundle.items}
    assert rule.record_id not in {item.record_id for item in rule_bundle.items}
    assert report_bundle.explanation["fusion"]["selected"] == []
    assert rule_bundle.explanation["rule_recall_promoted_count"] == 0
    store.close()


def test_online_pollution_gate_precedes_create_safety(tmp_path) -> None:
    store = RuntimeStore(tmp_path)
    stale = _record("stale identity", aliases=["stale alias"])
    stale.meta["living_memory_v1"] = {"temporal": {"status": "expired"}}
    store.append(stale)
    bundle = MemoryAPI(store).recall(
        query="stale alias",
        scope=asdict(SCOPE),
        task_context={"source_ids": ["alpha"], "target_source_id": "alpha"},
        limit=3,
    )
    assert bundle.items == []
    assert bundle.explanation["online_recall_gate"]["blocked_counts"]["stale_memory"] == 1
    assert bundle.explanation["fusion"]["create_safety"] == "unknown"
    assert bundle.explanation["fusion"]["ambiguity_reasons"] == ["no_identity_evidence"]
    store.close()


def test_reflections_cannot_bypass_hard_or_online_recall_gates(tmp_path) -> None:
    store = RuntimeStore(tmp_path)
    reflection = RecordEnvelope.create(
        kind="reflection",
        title="blocked reflection marker",
        summary="blocked reflection marker",
        scope=SCOPE,
        source="blocked.reflection",
        source_id="alpha",
    )
    store.append(reflection)
    bundle = MemoryAPI(store).recall(
        query="blocked reflection marker",
        scope=asdict(SCOPE),
        task_context={"source_ids": ["alpha"], "blocked_sources": ["blocked.reflection"]},
        limit=3,
    )
    assert bundle.items == []
    assert bundle.reflections == []
    store.close()


def test_same_record_id_across_physical_scopes_preserves_logical_group_authority(tmp_path) -> None:
    store = RuntimeStore(tmp_path)
    main_scope = ScopeRef(tenant_id="default", agent_id="main", workspace_id="repo-x", user_id="darrow")
    canonical_scope = ScopeRef(tenant_id="default", agent_id="hongtu", workspace_id="embodied", user_id="darrow")
    main = _record("MAIN-PHYSICAL", scope=main_scope)
    canonical = _record("CANONICAL-ALIAS", scope=canonical_scope)
    main.record_id = canonical.record_id = "same_physical_id"
    store.append(main)
    store.append(canonical)
    bundle = MemoryAPI(store).recall(
        query="physical",
        scope=asdict(main_scope),
        task_context={"source_ids": ["alpha"]},
        limit=1,
    )
    assert [(item.title, item.scope.agent_id, item.scope.workspace_id) for item in bundle.items] == [
        ("MAIN-PHYSICAL", "main", "repo-x")
    ]
    store.close()


def test_corrupt_identity_source_ref_is_dropped_not_raised(tmp_path) -> None:
    store = RuntimeStore(tmp_path)
    record = store.append(_record("corrupt ref", aliases=["corrupt alias"]))
    store.sqlite.conn.execute(
        "UPDATE recall_index SET source_id = 'bad/source' WHERE record_id = ?", (record.record_id,)
    )
    store.sqlite.conn.commit()
    bundle = MemoryAPI(store).recall(query="corrupt alias", scope=asdict(SCOPE), limit=3)
    assert bundle.items == []
    store.close()


def test_long_document_pool_overfetches_past_profile_multiplier(tmp_path) -> None:
    store = RuntimeStore(tmp_path)
    other_record = _record("other", content={"page_id": "page-b"})
    other_record.summary = "diversity marker"
    other_record.content["text"] = "diversity marker"
    other_record.meta["quality"]["salience_score"] = 0.0
    other = store.append(other_record)
    for index in range(12):
        record = _record(f"diversity marker dominant page a {index}", content={"page_id": "page-a"})
        record.summary = "diversity marker"
        record.content["text"] = "diversity marker"
        record.meta["quality"]["salience_score"] = 1.0
        store.append(record)
    bundle = MemoryAPI(store).recall(
        query="diversity marker",
        scope=asdict(SCOPE),
        task_context={"source_ids": ["alpha"]},
        limit=2,
    )
    assert {item.content["page_id"] for item in bundle.items} == {"page-a", "page-b"}
    assert other.record_id in {
        member_id
        for selected in bundle.explanation["fusion"]["selected"]
        for member_id in selected["member_record_ids"]
    }
    store.close()


def test_extreme_naive_timestamp_is_stable_and_does_not_crash(tmp_path) -> None:
    store = RuntimeStore(tmp_path)
    record = _record("extreme timestamp")
    record.time.updated_at = "0001-01-01T00:00:00"
    store.append(record)
    first = MemoryAPI(store).recall(query="extreme timestamp", scope=asdict(SCOPE), limit=1)
    second = MemoryAPI(store).recall(query="extreme timestamp", scope=asdict(SCOPE), limit=1)
    assert [item.record_id for item in first.items] == [record.record_id]
    assert first.explanation["fusion"] == second.explanation["fusion"]
    store.close()


def test_partial_common_keyword_is_not_strong_create_evidence(tmp_path) -> None:
    store = RuntimeStore(tmp_path)
    candidate = store.append(_record("what happened elsewhere"))

    class KeywordSource:
        name = "keyword-test"

        def search(self, request):
            return CandidateBatch(
                hits=(CandidateHit(
                    ref=CandidateRef(candidate.record_id, ExactScope.from_scope(candidate.scope), candidate.source_id),
                    source_rank=1,
                    source_score=1.0,
                    component_hints={"lexical_score": 0.1},
                ),)
            )

    bundle = MemoryAPI(
        store, recall_engine=GovernedRecallEngine(store=store, candidate_source=KeywordSource())
    ).recall(
        query="what is target",
        scope=asdict(SCOPE),
        task_context={"source_ids": ["alpha"], "target_source_id": "alpha"},
        limit=2,
    )
    assert "keyword_exact" not in bundle.explanation["fusion"]["selected"][0]["evidence"]
    assert bundle.explanation["fusion"]["create_safety"] == "unknown"
    store.close()


def test_source_migration_skips_one_malformed_payload_without_startup_dos(tmp_path) -> None:
    root = tmp_path / "malformed-source-migration"
    store = RuntimeStore(root)
    candidate = _record("malformed source payload")
    candidate.kind = "knowledge_page"
    record = store.append(candidate)
    store.close()
    connection = sqlite3.connect(root / "state" / "eimemory.sqlite")
    connection.execute("DELETE FROM schema_migrations WHERE migration_id = 'records.source_partition.v1'")
    connection.execute("UPDATE records SET payload_json = '{' WHERE record_id = ?", (record.record_id,))
    connection.commit()
    connection.close()
    reopened = RuntimeStore(root)
    assert "records.source_partition.v1" in reopened.sqlite.pending_storage_migrations()
    assert reopened.sqlite.source_partition_migration_diagnostics == {}
    _apply_all_storage_migrations(reopened)
    assert reopened.sqlite.source_partition_migration_diagnostics["corrupt"] == 1
    reopened.close()


def test_identical_content_across_sources_stays_independent_for_target_safety(tmp_path) -> None:
    store = RuntimeStore(tmp_path)
    alpha = store.append(_record("identical authority", source_id="alpha", aliases=["shared authority"]))
    beta = store.append(_record("identical authority", source_id="beta", aliases=["shared authority"]))
    bundle = MemoryAPI(store).recall(
        query="shared authority",
        scope=asdict(SCOPE),
        task_context={"source_ids": ["alpha", "beta"], "target_source_id": "alpha"},
        limit=5,
    )
    assert {item.record_id for item in bundle.items} == {alpha.record_id, beta.record_id}
    assert bundle.explanation["fusion"]["create_safety"] == "exists"
    store.close()


def test_rrf_intentionally_changes_a_fixed_legacy_hand_weight_order() -> None:
    legacy_scores = {"legacy-a": 0.95, "multi-signal-b": 0.80}
    legacy_order = sorted(legacy_scores, key=lambda record_id: (-legacy_scores[record_id], record_id))
    fused = fuse_ranked_components(
        [
            ("keyword", ["legacy-a", "multi-signal-b"]),
            ("vector", ["multi-signal-b"]),
            ("graph", ["multi-signal-b"]),
        ],
        rrf_k=60,
    )
    rrf_order = [item.record_id for item in fused.items]
    assert legacy_order == ["legacy-a", "multi-signal-b"]
    assert rrf_order == ["multi-signal-b", "legacy-a"]
    assert fused.items[0].contributions == {
        "graph": pytest.approx(1.0 / 61.0),
        "keyword": pytest.approx(2.0 / 62.0),
        "vector": pytest.approx(1.5 / 61.0),
    }


def test_usage_feedback_is_partitioned_by_exact_scope_and_source(tmp_path) -> None:
    store = RuntimeStore(tmp_path)
    main_scope = ScopeRef(tenant_id="default", agent_id="main", workspace_id="repo-x", user_id="darrow")
    canonical_scope = ScopeRef(tenant_id="default", agent_id="hongtu", workspace_id="embodied", user_id="darrow")
    alpha = _record("shared usage marker", source_id="alpha", scope=main_scope)
    beta = _record("shared usage marker", source_id="beta", scope=canonical_scope)
    alpha.record_id = beta.record_id = "shared-usage-id"
    store.append(alpha)
    store.append(beta)
    store.append(
        RecordEnvelope.create(
            kind="feedback",
            title="alpha usage feedback",
            content={"report_type": "memory_usage_telemetry", "used_record_ids": [alpha.record_id]},
            meta={"report_type": "memory_usage_telemetry"},
            scope=main_scope,
            source="test.usage",
            source_id="alpha",
        )
    )
    bundle = MemoryAPI(store).recall(
        query="shared usage marker",
        scope=asdict(main_scope),
        task_context={"source_ids": ["alpha", "beta"]},
        limit=2,
    )
    selected = {entry["source_id"]: entry for entry in bundle.explanation["fusion"]["selected"]}
    assert selected["alpha"]["contributions"].get("usage", 0.0) > 0.0
    assert selected["beta"]["contributions"].get("usage", 0.0) == 0.0
    store.close()


def test_fusion_token_preserves_legal_long_exact_refs(tmp_path) -> None:
    long_scope = ScopeRef(
        tenant_id="tenant-a",
        agent_id="openclaw",
        workspace_id="workspace-a",
        user_id="u" * 300,
    )
    store = RuntimeStore(tmp_path)
    record = store.append(_record("long exact ref marker", scope=long_scope))
    bundle = MemoryAPI(store).recall(
        query="long exact ref marker",
        scope=asdict(long_scope),
        task_context={"source_ids": ["alpha"]},
        limit=1,
    )
    assert [item.record_id for item in bundle.items] == [record.record_id]
    assert bundle.explanation["fusion"]["pre_pool_count"] == 1
    assert bundle.explanation["fusion"]["post_pool_count"] == 1
    store.close()


def test_knowledge_source_caps_are_partitioned_by_exact_authority() -> None:
    pages: list[RecordEnvelope] = []
    for index, source_id in enumerate(("alpha", "alpha", "beta")):
        pages.append(
            RecordEnvelope.create(
                kind="knowledge_page",
                title=f"page {index}",
                summary=f"page {index}",
                content={"paper_source_id": "paper-shared"},
                provenance={"paper_source_id": "paper-shared"},
                scope=SCOPE,
                source="test.paper",
                source_id=source_id,
            )
        )
    assert [(item.source_id, item.title) for item in MemoryAPI._dedupe_records(pages)] == [
        ("alpha", "page 0"),
        ("alpha", "page 1"),
        ("beta", "page 2"),
    ]


def test_scoring_explanation_is_keyed_by_exact_physical_ref(tmp_path) -> None:
    main_scope = ScopeRef(tenant_id="default", agent_id="main", workspace_id="repo-x", user_id="darrow")
    canonical_scope = ScopeRef(tenant_id="default", agent_id="hongtu", workspace_id="embodied", user_id="darrow")
    main = _record("main scoring marker", scope=main_scope)
    canonical = _record("canonical scoring marker", scope=canonical_scope)
    main.record_id = canonical.record_id = "shared-scoring-id"
    main.meta["quality"]["salience_score"] = 0.95
    canonical.meta["quality"]["salience_score"] = 0.05
    store = RuntimeStore(tmp_path)
    store.append(main)
    store.append(canonical)
    bundle = MemoryAPI(store).recall(
        query="scoring marker",
        scope=asdict(main_scope),
        task_context={"source_ids": ["alpha"]},
        limit=2,
    )
    scoring = {entry["title"]: entry for entry in bundle.explanation["scoring"]}
    assert scoring["main scoring marker"]["base_quality_score"] == pytest.approx(0.95)
    assert scoring["canonical scoring marker"]["base_quality_score"] == pytest.approx(0.05)
    store.close()
