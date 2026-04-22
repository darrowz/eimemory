from __future__ import annotations

from eimemory.intake.connectors import (
    CollectedItem,
    FetchResult,
    build_arxiv_api_url,
    build_chatpaper_arxiv_api_url,
    build_crossref_work_url,
    collect_from_source_entry,
    fetch_arxiv,
    normalize_github_url,
    parse_arxiv_xml,
    parse_chatpaper_arxiv_json,
    parse_crossref_work_json,
    parse_feed_xml,
)
from eimemory.intake.loop import KIND_NAME, KnowledgeIntakeLoop, candidates_to_records
from eimemory.intake.packs import export_knowledge_pack, import_knowledge_pack
from eimemory.intake.pipeline import PaperIntakePipeline, promote_paper_candidate
from eimemory.intake.policy import build_source_quality_report, recommend_collection_policy
from eimemory.intake.registry import SourceEntry, SourceRegistry
from eimemory.intake.review import (
    list_review_queue,
    merge_candidates,
    promote_candidate,
    review_candidate,
)

__all__ = [
    "CollectedItem",
    "FetchResult",
    "KIND_NAME",
    "KnowledgeIntakeLoop",
    "PaperIntakePipeline",
    "SourceEntry",
    "SourceRegistry",
    "build_arxiv_api_url",
    "build_chatpaper_arxiv_api_url",
    "build_crossref_work_url",
    "candidates_to_records",
    "collect_from_source_entry",
    "fetch_arxiv",
    "export_knowledge_pack",
    "import_knowledge_pack",
    "build_source_quality_report",
    "list_review_queue",
    "merge_candidates",
    "normalize_github_url",
    "parse_arxiv_xml",
    "parse_chatpaper_arxiv_json",
    "parse_crossref_work_json",
    "parse_feed_xml",
    "promote_candidate",
    "promote_paper_candidate",
    "recommend_collection_policy",
    "review_candidate",
]
