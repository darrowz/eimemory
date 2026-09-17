"""Performance regression guards for recall hot paths."""

from __future__ import annotations

from dataclasses import asdict

from eimemory.api.memory import MemoryAPI
from eimemory.models.records import RecordEnvelope, ScopeRef
from eimemory.storage.runtime_store import RuntimeStore


SCOPE = ScopeRef(
    tenant_id="tenant-a",
    agent_id="openclaw",
    workspace_id="workspace-a",
    user_id="user-a",
)


def _memory(title: str, *, source_id: str = "alpha") -> RecordEnvelope:
    return RecordEnvelope.create(
        kind="memory",
        title=title,
        summary=title,
        content={"text": title},
        scope=SCOPE,
        source="test",
        source_id=source_id,
        aliases=[],
        meta={"quality_status": "accepted", "quality_score": 1.0},
    )


def _rule(title: str, *, source_id: str = "alpha") -> RecordEnvelope:
    return RecordEnvelope.create(
        kind="rule",
        title=title,
        summary=title,
        content={"text": title},
        scope=SCOPE,
        source="test",
        source_id=source_id,
        aliases=[],
        meta={"quality_status": "accepted", "quality_score": 1.0},
    )


def test_explicit_kinds_without_rule_skips_active_rule_fanout(tmp_path) -> None:
    """PERF: smoke/identity recalls that constrain kinds must not list all active rules."""
    store = RuntimeStore(tmp_path)
    store.append(_memory("bounded kinds memory marker"))
    store.append(_rule("should not be fan-out listed"))

    original = store.list_records
    rule_list_calls: list[dict] = []

    def tracking_list_records(*args, **kwargs):
        kinds = kwargs.get("kinds") or (args[0] if args else None)
        if kinds == ["rule"] or kinds == ("rule",):
            rule_list_calls.append(dict(kwargs))
        return original(*args, **kwargs)

    store.list_records = tracking_list_records  # type: ignore[method-assign]
    bundle = MemoryAPI(store).recall(
        query="bounded kinds memory marker",
        scope=asdict(SCOPE),
        task_context={
            "source_ids": ["alpha"],
            "kinds": ["memory", "multimodal_memory", "knowledge_page", "claim_card"],
        },
        limit=5,
    )
    assert any(item.kind == "memory" for item in bundle.items)
    assert all(item.kind != "rule" for item in bundle.items)
    assert bundle.rules == []
    assert rule_list_calls == []
    store.close()


def test_identity_alias_query_uses_covering_index_without_temp_btree(tmp_path) -> None:
    """PERF: exact alias lookup must use covering alias index, not a TEMP B-TREE sort."""
    store = RuntimeStore(tmp_path)
    store.append(
        RecordEnvelope.create(
            kind="memory",
            title="plan title",
            summary="plan title",
            content={"text": "plan title"},
            scope=SCOPE,
            source="test",
            source_id="alpha",
            aliases=["plan alias"],
            meta={"quality_status": "accepted", "quality_score": 1.0},
        )
    )
    traced: list[str] = []
    with store._lock:
        store.sqlite.conn.set_trace_callback(traced.append)
        store.sqlite.search_identity_candidates(
            query="plan alias",
            kinds=["memory"],
            scope=SCOPE,
            limit=5,
            source_ids=["alpha"],
        )
        store.sqlite.conn.set_trace_callback(None)
        alias_sql = next(sql for sql in traced if "FROM recall_alias_index a" in sql)
        plan = store.sqlite.conn.execute("EXPLAIN QUERY PLAN " + alias_sql).fetchall()
        details = [str(row[3]) for row in plan]
        assert any("idx_recall_alias_exact" in detail for detail in details)
        assert not any("TEMP B-TREE" in detail for detail in details)
        assert not any(detail.startswith("SCAN ") and " INDEXED BY " not in detail for detail in details)
    store.close()


def test_embedding_cache_skips_oversized_inputs(monkeypatch) -> None:
    """PERF/correctness: oversized texts must not pollute the process LRU cache."""
    from eimemory.embeddings import local as local_embed

    local_embed._embed_text_cached.cache_clear()
    oversized = "x" * (local_embed.MAX_CACHED_TEXT_CHARS + 50)
    calls = {"uncached": 0}
    original = local_embed._embed_text_uncached

    def counting(text: str, size: int = local_embed.VECTOR_SIZE):
        calls["uncached"] += 1
        return original(text, size)

    monkeypatch.setattr(local_embed, "_embed_text_uncached", counting)
    local_embed.embed_text(oversized)
    local_embed.embed_text(oversized)
    assert calls["uncached"] == 2
    assert local_embed._embed_text_cached.cache_info().currsize == 0
