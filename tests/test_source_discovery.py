from __future__ import annotations

from eimemory.intake.registry import SourceEntry
from eimemory.intake.source_discovery import discover_source_proposals


def test_robotics_and_vision_gap_proposes_chatpaper_arxiv_categories() -> None:
    proposals = discover_source_proposals(
        gap_queries=["Need robotics manipulation and visual grounding papers"],
        sources=[],
        recent_titles=["Embodied visual planning for robot assistants"],
    )

    categories = {
        proposal["metadata"].get("category")
        for proposal in proposals
        if proposal["metadata"].get("source_family") == "chatpaper_arxiv"
    }

    assert {"cs.RO", "cs.CV"}.issubset(categories)
    assert all(
        {"source_kind", "title", "uri", "tags", "metadata", "reason", "score", "decision"}.issubset(proposal)
        for proposal in proposals
    )


def test_existing_chatpaper_category_is_not_proposed_again() -> None:
    proposals = discover_source_proposals(
        gap_queries=["Need robotics papers for embodied agents"],
        sources=[
            SourceEntry(
                source_id="src_chatpaper",
                source_kind="url",
                title="ChatPaper arXiv cs.RO",
                uri="https://www.chatpaper.ai/zh/dashboard/arxiv/cs/RO",
                tags=["chatpaper", "arxiv"],
                metadata={"categories": ["cs.RO"]},
            )
        ],
        recent_titles=[],
    )

    assert all(proposal["metadata"].get("category") != "cs.RO" for proposal in proposals)


def test_news_and_product_opportunity_gap_proposes_rss_and_manual_review_needing_review() -> None:
    proposals = discover_source_proposals(
        gap_queries=["Track news and product launches for AI memory tools"],
        sources=[],
        recent_titles=["New developer product release changes memory workflows"],
    )

    kinds = {proposal["source_kind"] for proposal in proposals}
    product_proposals = [
        proposal for proposal in proposals if proposal["source_kind"] in {"rss", "news", "manual"}
    ]

    assert {"rss", "manual"}.issubset(kinds)
    assert product_proposals
    assert all(proposal["decision"] == "needs_review" for proposal in product_proposals)
