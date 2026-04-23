from eimemory.api.runtime import Runtime
from eimemory.knowledge.synthesis import build_research_digest
from eimemory.scheduler.jobs import run_nightly_jobs


def test_build_research_digest_summarizes_recent_paper_knowledge(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path / "runtime")
    scope = {"agent_id": "main", "workspace_id": "papers"}
    try:
        source = runtime.ingest_paper_source(
            {
                "source_kind": "url",
                "canonical_url": "https://example.test/papers/digest-a",
                "title": "Agent Memory Retrieval",
                "abstract": "Memory retrieval improves long horizon agent performance.",
                "published_at": "2026-04-20",
                "metadata": {"categories": ["cs.AI", "memory"]},
            },
            scope=scope,
        )
        extraction = runtime.extract_paper_memory(
            {
                "paper_source_id": source.record_id,
                "title": "Agent Memory Retrieval",
                "abstract": "Memory retrieval improves long horizon agent performance.",
                "body": "Limitation: agents may mishandle stale claims?",
                "metadata": {"categories": ["cs.AI", "memory"]},
                "provenance": {"published_at": "2026-04-20"},
            },
            scope=scope,
        )
        runtime.compile_paper_knowledge(extraction=extraction, scope=scope)
        papers = runtime.store.list_records(kinds=["paper_source"], scope=scope, limit=10)
        claims = runtime.store.list_records(kinds=["claim_card"], scope=scope, limit=10)
        pages = runtime.store.list_records(kinds=["knowledge_page"], scope=scope, limit=10)

        digest = build_research_digest(
            paper_sources=papers,
            claim_cards=claims,
            knowledge_pages=pages,
            limit=3,
            digest_date="2026-04-23",
        )

        assert digest["ok"] is True
        assert digest["paper_count"] == 1
        assert digest["top_papers"][0]["title"] == "Agent Memory Retrieval"
        assert digest["themes"][0]["name"] in {"memory", "cs.AI"}
        assert any("Memory retrieval improves" in claim["text"] for claim in digest["notable_claims"])
        assert any("stale claims" in question for question in digest["open_questions"])
        assert digest["summary"]
    finally:
        runtime.close()


def test_runtime_build_research_digest_can_persist_digest_page(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path / "runtime")
    scope = {"agent_id": "main", "workspace_id": "papers"}
    try:
        source = runtime.ingest_paper_source(
            {
                "source_kind": "url",
                "canonical_url": "https://example.test/papers/digest-runtime",
                "title": "Deterministic Research Digests",
                "abstract": "Deterministic digests summarize memory-only paper knowledge.",
                "published_at": "2026-04-20",
                "metadata": {"categories": ["cs.CL"]},
            },
            scope=scope,
        )
        extraction = runtime.extract_paper_memory(
            {
                "paper_source_id": source.record_id,
                "title": "Deterministic Research Digests",
                "abstract": "Deterministic digests summarize memory-only paper knowledge.",
                "metadata": {"categories": ["cs.CL"]},
            },
            scope=scope,
        )
        runtime.compile_paper_knowledge(extraction=extraction, scope=scope)

        digest = runtime.build_research_digest(scope=scope, persist=True, limit=5, digest_date="2026-04-23")
        pages = runtime.store.list_records(kinds=["knowledge_page"], scope=scope, limit=20)
        digest_pages = [page for page in pages if page.content.get("page_type") == "digest"]

        assert digest["persisted"] is True
        assert digest["persisted_page_id"] == digest_pages[0].record_id
        assert digest_pages[0].source == "eimemory.knowledge.synthesis"
        assert digest_pages[0].title == "Research digest 2026-04-23"
    finally:
        runtime.close()


def test_nightly_jobs_include_research_digest_summary(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path / "runtime")
    scope = {"agent_id": "main", "workspace_id": "papers"}
    try:
        source = runtime.ingest_paper_source(
            {
                "source_kind": "url",
                "canonical_url": "https://example.test/papers/digest-nightly",
                "title": "Nightly Research Synthesis",
                "abstract": "Nightly synthesis returns a digest from recent paper memory.",
                "published_at": "2026-04-20",
                "metadata": {"categories": ["cs.AI"]},
            },
            scope=scope,
        )
        extraction = runtime.extract_paper_memory(
            {
                "paper_source_id": source.record_id,
                "title": "Nightly Research Synthesis",
                "abstract": "Nightly synthesis returns a digest from recent paper memory.",
                "metadata": {"categories": ["cs.AI"]},
            },
            scope=scope,
        )
        runtime.compile_paper_knowledge(extraction=extraction, scope=scope)

        report = run_nightly_jobs(runtime, scope=scope)
        digest_pages = [
            page
            for page in runtime.store.list_records(kinds=["knowledge_page"], scope=scope, limit=20)
            if page.content.get("page_type") == "digest"
        ]

        assert report["research_digest"]["ok"] is True
        assert report["research_digest"]["paper_count"] == 1
        assert report["research_digest"]["persisted"] is True
        assert report["research_digest"]["persisted_page_id"]
        assert len(digest_pages) == 1
    finally:
        runtime.close()
