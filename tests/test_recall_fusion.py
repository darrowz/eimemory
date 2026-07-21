from __future__ import annotations

from dataclasses import asdict
import json
import sqlite3

import pytest

from eimemory.api.memory import MemoryAPI
from eimemory.models.records import LinkRef, RecordEnvelope, ScopeRef
from eimemory.models.identity_aliases import IDENTITY_ALIASES_VERSION
from eimemory.retrieval.fusion import (
    FUSION_POLICY_VERSION,
    fuse_ranked_components,
    page_pool_key,
)
from eimemory.storage.runtime_store import RuntimeStore


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
    ("content", "expected_suffix"),
    [
        ({"page_id": "page-1"}, "page:page-1"),
        ({"parent_record_id": "parent-1"}, "parent:parent-1"),
        ({"source_document_id": "document-1"}, "document:document-1"),
        ({"session_id": "session-1", "source_event_id": "event-1"}, "raw:session-1:event-1"),
        ({}, "record:"),
    ],
)
def test_page_pool_key_has_hard_scope_source_namespace_and_priority(content: dict, expected_suffix: str) -> None:
    record = _record("chunk", content=content)
    key = page_pool_key(record)

    assert key.startswith("tenant-a\x1fopenclaw\x1fworkspace-a\x1fuser-a\x1falpha\x1f")
    if expected_suffix == "record:":
        assert key.endswith(f"record:{record.record_id}")
    else:
        assert key.endswith(expected_suffix)


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
    pooled = next(item for item in selected if item["page_key"].endswith("page:page-a"))
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
    ["missing_marker", "wrong_title_index", "wrong_alias_index", "title_column", "alias_column", "alias_table"],
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
    else:
        connection.execute("DROP TABLE recall_alias_index")
    connection.commit()
    connection.close()

    repaired = RuntimeStore(root)
    assert repaired.sqlite._recall_identity_physical_ready() is True
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
    repaired.close()


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
