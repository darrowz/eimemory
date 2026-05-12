from eimemory.api.runtime import Runtime
from eimemory.knowledge.compiler import compile_paper_knowledge
from eimemory.knowledge.extract import extract_paper_memory
from eimemory.models.knowledge_pages import KnowledgePage


def test_compile_paper_knowledge_creates_paper_and_topic_pages() -> None:
    result = compile_paper_knowledge(
        paper_source_id="paper_compiler",
        paper_title="Compact Retrieval for Embodied Agents",
        claims=["Compact retrieval reduces prompt noise."],
        entities=["Embodied agents", "Compact retrieval"],
    )
    page_types = {page.page_type for page in result.pages}

    assert "paper" in page_types
    assert "topic" in page_types
    assert all(isinstance(page, KnowledgePage) for page in result.pages)
    assert all(
        claim_id.startswith("claim_")
        for page in result.pages
        for claim_id in page.supporting_claim_ids
    )


def test_compiled_pages_keep_supporting_claim_ids_and_source_links() -> None:
    extraction = extract_paper_memory(
        paper_source_id="paper_compile_links",
        title="Hybrid Memory",
        abstract="Hybrid memory improves semantic recall.",
        body="Limitation: requires reliable source identity.",
    )
    result = compile_paper_knowledge(extraction=extraction)
    records = result.to_records(scope={"agent_id": "agent-compile"})

    assert records
    assert all(record.kind == "knowledge_page" for record in records)
    assert all(record.provenance["paper_source_id"] == "paper_compile_links" for record in records)
    assert any(record.content["supporting_claim_ids"] for record in records)
    assert all(
        any(link.target_id == "paper_compile_links" for link in record.links)
        for record in records
    )


def test_compile_paper_knowledge_filters_noisy_single_word_topic_pages() -> None:
    result = compile_paper_knowledge(
        paper_source_id="paper_noisy_topics",
        paper_title="GATHER Retrieval Framework",
        claims=[
            "GATHER improves retrieval for hyper-entity queries.",
            "GATHER improves retrieval for hyper-entity queries.",
        ],
        entities=["GATHER", "retrieval", "semantic", "interference", "Hyper Entity Queries"],
    )

    topic_titles = {page.title for page in result.pages if page.page_type == "topic"}
    paper_page = next(page for page in result.pages if page.page_type == "paper")

    assert "GATHER" in topic_titles
    assert "Hyper Entity Queries" in topic_titles
    assert "semantic" not in topic_titles
    assert "interference" not in topic_titles
    assert paper_page.summary.count("GATHER improves retrieval") == 1


def test_runtime_compile_paper_knowledge_persists_pages(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    try:
        extraction = runtime.extract_paper_memory(
            {
                "paper_source_id": "paper_runtime_compile",
                "title": "Semantic Memory",
                "abstract": "Semantic memory improves long horizon reuse.",
                "body": "Method: claim cards and knowledge pages.",
            },
            scope={"agent_id": "agent-runtime", "workspace_id": "compile"},
        )
        result = runtime.compile_paper_knowledge(
            extraction=extraction,
            scope={"agent_id": "agent-runtime", "workspace_id": "compile"},
        )

        pages = runtime.store.list_records(
            kinds=["knowledge_page"],
            scope={"agent_id": "agent-runtime", "workspace_id": "compile"},
        )
        assert result.pages
        assert pages
        assert any(page.content["page_type"] == "paper" for page in pages)
        assert any(page.content["page_type"] == "topic" for page in pages)
    finally:
        runtime.close()
