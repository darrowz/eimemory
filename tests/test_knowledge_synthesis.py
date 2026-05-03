from eimemory.api.runtime import Runtime
from eimemory.knowledge.synthesis import build_research_digest
from eimemory.models.records import RecordEnvelope, ScopeRef
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


def test_daily_brief_keeps_news_digest_separate_from_research(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path / "runtime")
    scope = {"agent_id": "main", "workspace_id": "news"}
    try:
        runtime.store.append(
            RecordEnvelope.create(
                kind="news",
                title="News item: AI memory vendor launches update",
                summary="AI memory vendor launched a product update.",
                scope=ScopeRef.from_dict(scope),
                content={
                    "item_url": "https://example.test/news/ai-memory",
                    "published_at": "2026-04-29",
                    "source_kind": "rss",
                },
                tags=["news", "external"],
                source="eimemory.news.collect",
                meta={"source_kind": "rss"},
            )
        )

        brief = runtime.build_daily_brief(scope=scope)

        assert brief["news_digest"]["count"] == 1
        assert brief["news_digest"]["items"][0]["url"] == "https://example.test/news/ai-memory"
        assert brief["research_digest"]["count"] == 0
    finally:
        runtime.close()


def test_daily_brief_deduplicates_news_by_url(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path / "runtime")
    scope = {"agent_id": "main", "workspace_id": "news"}
    try:
        for index in range(2):
            runtime.store.append(
                RecordEnvelope.create(
                    kind="news",
                    title=f"News item duplicate {index}",
                    summary="Same URL should only appear once in daily brief.",
                    scope=ScopeRef.from_dict(scope),
                    content={"item_url": "https://example.test/news/same", "source_kind": "rss"},
                    tags=["news", "external"],
                    source="eimemory.news.collect",
                    meta={"source_kind": "rss"},
                )
            )

        brief = runtime.build_daily_brief(scope=scope)

        assert brief["news_digest"]["count"] == 1
        assert brief["news_digest"]["items"][0]["url"] == "https://example.test/news/same"
    finally:
        runtime.close()


def test_daily_brief_cleans_html_from_news_digest(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path / "runtime")
    scope = {"agent_id": "main", "workspace_id": "news"}
    try:
        runtime.store.append(
            RecordEnvelope.create(
                kind="news",
                title="News item: &lt;b&gt;AI memory update&lt;/b&gt;",
                summary=(
                    '<a href="https://example.test/news/html">AI memory vendor update</a> '
                    '<font color="#6f6f6f">Example News</font>'
                ),
                scope=ScopeRef.from_dict(scope),
                content={"item_url": "https://example.test/news/html", "source_kind": "rss"},
                tags=["news", "external"],
                source="eimemory.news.collect",
                meta={"source_kind": "rss"},
            )
        )

        brief = runtime.build_daily_brief(scope=scope)
        item = brief["news_digest"]["items"][0]

        assert item["title"] == "News item: AI memory update"
        assert item["summary"] == "AI memory vendor update Example News"
        assert "<" not in item["summary"]
        assert ">" not in item["summary"]
    finally:
        runtime.close()


def test_nightly_jobs_promote_news_rss_candidate_and_collect_news(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path / "runtime")
    scope = {"agent_id": "main", "workspace_id": "news"}
    try:
        runtime.store.append(
            RecordEnvelope.create(
                kind="source_candidate",
                title="News RSS candidate: AI memory tools",
                summary="Gap evidence asks for news coverage.",
                detail="https://example.test/rss",
                scope=ScopeRef.from_dict(scope),
                status="candidate",
                content={
                    "proposal": {
                        "source_kind": "rss",
                        "title": "News RSS candidate: AI memory tools",
                        "uri": "https://example.test/rss",
                        "tags": ["news", "rss", "needs-review"],
                        "metadata": {"source_family": "news_rss", "max_items": 3},
                    }
                },
                tags=["source-discovery", "needs-review", "news", "rss"],
                meta={"source_kind": "rss", "source_family": "news_rss", "source_uri": "https://example.test/rss"},
            )
        )
        xml = """<?xml version="1.0"?>
        <rss version="2.0"><channel><item>
          <title>AI memory news item</title>
          <link>https://example.test/news/2</link>
          <description>News content from promoted RSS candidate.</description>
        </item></channel></rss>
        """

        report = run_nightly_jobs(runtime, scope=scope, external_fetch_text=lambda _url: xml)
        news = runtime.store.list_records(kinds=["news"], scope=scope, limit=10)

        assert report["news_source_promotion"]["promoted_count"] == 1
        assert report["external_collection"]["written_count"] == 1
        assert report["daily_brief"]["news_item_count"] == 1
        assert len(news) == 1
        assert news[0].status == "active"
        assert runtime.sources.list_sources(source_kind="rss")[0].uri == "https://example.test/rss"
    finally:
        runtime.close()
