from eimemory.api.runtime import Runtime
from eimemory.knowledge.views import build_claim_centered_view, build_mixed_view, build_page_centered_view
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
