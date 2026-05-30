from eimemory.api.memory import MemoryAPI
from eimemory.api.runtime import Runtime
from eimemory.knowledge.views import build_claim_centered_view, build_mixed_view, build_page_centered_view, records_from_view
from eimemory.models.records import RecordEnvelope, ScopeRef


def test_claim_centered_view_prioritizes_claim_cards() -> None:
    claim = RecordEnvelope.create(
        kind="claim_card",
        title="Compact retrieval reduces prompt noise.",
        summary="Compact retrieval reduces prompt noise.",
        scope=ScopeRef(),
    )
    page = RecordEnvelope.create(
        kind="knowledge_page",
        title="Compact retrieval topic",
        summary="Topic page",
        scope=ScopeRef(),
    )

    view = build_claim_centered_view(claims=[claim], pages=[page])

    assert view.view_type == "claim_centered"
    assert view.items[0]["kind"] == "claim_card"


def test_page_centered_view_prioritizes_knowledge_pages() -> None:
    claim = RecordEnvelope.create(
        kind="claim_card",
        title="Compact retrieval reduces prompt noise.",
        summary="Compact retrieval reduces prompt noise.",
        scope=ScopeRef(),
    )
    page = RecordEnvelope.create(
        kind="knowledge_page",
        title="Compact retrieval topic",
        summary="Topic page",
        scope=ScopeRef(),
    )

    view = build_page_centered_view(claims=[claim], pages=[page])

    assert view.view_type == "page_centered"
    assert view.items[0]["kind"] == "knowledge_page"


def test_recall_dedupes_repeated_knowledge_summaries_before_prompt_limit(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    scope = {"agent_id": "main", "workspace_id": "repo-x", "user_id": "darrow"}
    summary = (
        "SCOPE is a lightweight framework for air traffic control readback monitoring. "
        "It combines open-set classification with contextual examples and explains its corrections. "
        "The method improves safety-critical readback monitoring under low-latency constraints."
    )
    duplicate_pages = [
        RecordEnvelope.create(
            kind="knowledge_page",
            title=f"SCOPE duplicate {index}",
            summary=summary,
            scope=ScopeRef.from_dict(scope),
            meta={"quality": {"salience_score": 0.9}},
        )
        for index in range(8)
    ]
    distinct = RecordEnvelope.create(
        kind="knowledge_page",
        title="SCOPE deployment checks",
        summary="SCOPE monitoring also requires latency and deployment rollback checks.",
        scope=ScopeRef.from_dict(scope),
        meta={"quality": {"salience_score": 0.3}},
    )
    for record in [*duplicate_pages, distinct]:
        runtime.store.append(record)

    bundle = runtime.memory.recall(
        query="SCOPE monitoring",
        scope=scope,
        task_context={"task_type": "research", "recall_view": "page_centered"},
        limit=5,
    )

    repeated = [item for item in bundle.items if item.summary == summary]
    assert len(repeated) == 1
    assert distinct.record_id in {item.record_id for item in bundle.items}
    assert len({item.record_id for item in bundle.items}) == len(bundle.items)


def test_view_overscan_keeps_distinct_record_after_duplicate_run() -> None:
    scope = ScopeRef()
    summary = (
        "SCOPE is a lightweight framework for air traffic control readback monitoring. "
        "It combines open-set classification with contextual examples and explains its corrections."
    )
    duplicates = [
        RecordEnvelope.create(kind="knowledge_page", title=f"SCOPE duplicate {index}", summary=summary, scope=scope)
        for index in range(8)
    ]
    distinct = RecordEnvelope.create(
        kind="knowledge_page",
        title="SCOPE deployment checks",
        summary="SCOPE monitoring also requires latency and deployment rollback checks.",
        scope=scope,
    )
    ordered = [*duplicates, distinct]
    view = build_page_centered_view(claims=[], pages=ordered, query="SCOPE monitoring")

    old_path_items = MemoryAPI._dedupe_records(records_from_view(view, ordered, limit=5))
    overscanned_items = MemoryAPI._dedupe_records(records_from_view(view, ordered, limit=20))[:5]

    assert distinct.record_id not in {item.record_id for item in old_path_items}
    assert distinct.record_id in {item.record_id for item in overscanned_items}


def test_content_dedupe_keeps_distinct_preference_updates_with_same_prefix() -> None:
    prefix = "Operator communication style: concise, direct, no filler; lead with the conclusion, then evidence. "
    old = RecordEnvelope.create(
        kind="memory",
        title="Communication style",
        summary=f"{prefix}Old version: longer explanation is acceptable.",
        scope=ScopeRef(),
        meta={"memory_type": "preference", "quality": {"salience_score": 0.2}},
    )
    new = RecordEnvelope.create(
        kind="memory",
        title="Communication style",
        summary=f"{prefix}New version: default to the short answer.",
        scope=ScopeRef(),
        meta={"memory_type": "preference", "quality": {"salience_score": 0.9}},
    )

    deduped = MemoryAPI._dedupe_records([old, new])

    assert [item.record_id for item in deduped] == [old.record_id, new.record_id]


def test_content_dedupe_replaces_exact_duplicate_with_higher_quality_record() -> None:
    summary = "Operator communication style: concise, direct, no filler."
    old = RecordEnvelope.create(
        kind="memory",
        title="Communication style",
        summary=summary,
        scope=ScopeRef(),
        meta={"memory_type": "preference", "quality": {"salience_score": 0.2}},
    )
    new = RecordEnvelope.create(
        kind="memory",
        title="Communication style",
        summary=summary,
        scope=ScopeRef(),
        meta={"memory_type": "preference", "quality": {"salience_score": 0.9}},
    )

    deduped = MemoryAPI._dedupe_records([old, new])

    assert [item.record_id for item in deduped] == [new.record_id]


def test_content_dedupe_caps_knowledge_records_from_same_paper_source() -> None:
    paper_a_records = [
        RecordEnvelope.create(
            kind="knowledge_page",
            title=f"SCOPE page {index}",
            summary=f"SCOPE finding {index} about readback monitoring and latency controls.",
            scope=ScopeRef(),
            content={"source_ids": ["paper_scope"]},
            provenance={"paper_source_id": "paper_scope"},
        )
        for index in range(3)
    ]
    paper_b = RecordEnvelope.create(
        kind="knowledge_page",
        title="LexPath page",
        summary="LexPath retrieves legal statutes with intent-aware reranking.",
        scope=ScopeRef(),
        content={"source_ids": ["paper_lexpath"]},
        provenance={"paper_source_id": "paper_lexpath"},
    )

    deduped = MemoryAPI._dedupe_records([*paper_a_records, paper_b])

    paper_a_count = sum(1 for item in deduped if item.provenance.get("paper_source_id") == "paper_scope")
    assert paper_a_count == 2
    assert paper_b.record_id in {item.record_id for item in deduped}


def test_mixed_view_preserves_memory_without_execution_policy() -> None:
    memory = RecordEnvelope.create(
        kind="memory",
        title="Operator preference",
        summary="Use concise responses.",
        scope=ScopeRef(),
    )

    view = build_mixed_view(memories=[memory], claims=[], pages=[])

    assert view.view_type == "mixed"
    assert view.items[0]["kind"] == "memory"
    assert "execute" not in view.guidance.lower()


def test_memory_recall_routes_task_context_to_claim_centered_view(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    try:
        extraction = runtime.extract_paper_memory(
            {
                "paper_source_id": "paper_task_view",
                "title": "Task Memory",
                "abstract": "Compact retrieval improves embodied response quality.",
                "body": "Method: compact retrieval.",
            },
            scope={"agent_id": "agent-view", "workspace_id": "views"},
        )
        runtime.compile_paper_knowledge(
            extraction=extraction,
            scope={"agent_id": "agent-view", "workspace_id": "views"},
        )

        bundle = runtime.memory.recall(
            query="compact retrieval response quality",
            scope={"agent_id": "agent-view", "workspace_id": "views"},
            task_context={"task_type": "robot.reply"},
            limit=5,
        )

        assert bundle.explanation["recall_view"]["view_type"] == "claim_centered"
        assert bundle.explanation["recall_view"]["items"][0]["kind"] == "claim_card"
    finally:
        runtime.close()


def test_memory_recall_routes_research_context_to_page_centered_view(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    try:
        extraction = runtime.extract_paper_memory(
            {
                "paper_source_id": "paper_research_view",
                "title": "Research Memory",
                "abstract": "Knowledge pages improve long horizon research synthesis.",
                "body": "Method: page centered memory.",
            },
            scope={"agent_id": "agent-view", "workspace_id": "views"},
        )
        runtime.compile_paper_knowledge(
            extraction=extraction,
            scope={"agent_id": "agent-view", "workspace_id": "views"},
        )

        bundle = runtime.memory.recall(
            query="knowledge pages research synthesis",
            scope={"agent_id": "agent-view", "workspace_id": "views"},
            task_context={"intent": "research"},
            limit=5,
        )

        assert bundle.explanation["recall_view"]["view_type"] == "page_centered"
        assert bundle.explanation["recall_view"]["items"][0]["kind"] == "knowledge_page"
    finally:
        runtime.close()
