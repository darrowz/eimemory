from __future__ import annotations

from hashlib import sha256
from typing import Any
from urllib.parse import parse_qs, quote_plus, urlparse

from eimemory.intake.title_normalization import strip_candidate_title_prefixes


_CHATPAPER_DASHBOARD_PREFIX = "https://www.chatpaper.ai/zh/dashboard/arxiv"
_CATEGORY_HINTS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("cs.RO", ("robot", "robotics", "embodied", "manipulation", "navigation", "grasp")),
    ("cs.CV", ("vision", "visual", "image", "video", "multimodal", "grounding")),
    ("cs.CL", ("language", "llm", "prompt", "dialogue", "conversation", "nlp")),
    ("cs.LG", ("learning", "training", "reinforcement", "policy", "agent")),
    ("cs.IR", ("retrieval", "search", "memory", "knowledge", "rag")),
)
_NEWS_HINTS = ("news", "launch", "launches", "release", "product", "market", "company", "vendor", "tool")


def discover_source_proposals(
    *,
    gap_queries: list[str] | tuple[str, ...] | None = None,
    sources: list[Any] | tuple[Any, ...] | None = None,
    recent_titles: list[str] | tuple[str, ...] | None = None,
) -> list[dict[str, Any]]:
    """Build conservative source proposals without mutating the source registry."""
    source_items = list(sources or [])
    existing_uris = {_normalize_uri(_source_value(source, "uri")) for source in source_items}
    existing_categories = _existing_chatpaper_categories(source_items)
    evidence = _normalized_texts([*(gap_queries or []), *(recent_titles or [])])
    proposals: list[dict[str, Any]] = []

    for category in _candidate_categories(evidence):
        if category in existing_categories:
            continue
        proposals.append(
            _proposal(
                source_kind="url",
                title=f"ChatPaper arXiv {category}",
                uri=_chatpaper_category_uri(category),
                tags=["arxiv", "chatpaper", "paper"],
                metadata={
                    "source_family": "chatpaper_arxiv",
                    "category": category,
                    "frequency": "daily",
                    "max_items": 10,
                },
                reason=f"Gap evidence mentions topics covered by arXiv category {category}.",
                score=0.86,
                evidence=evidence,
            )
        )

    if _looks_like_news_or_product_gap(evidence):
        query = _best_query(evidence)
        proposals.extend(
            [
                _proposal(
                    source_kind="rss",
                    title=f"News RSS candidate: {query}",
                    uri=_google_news_rss_uri(query),
                    tags=["news", "rss", "needs-review"],
                    metadata={
                        "source_family": "news_rss",
                        "query": query,
                        "frequency": "daily",
                        "max_items": 10,
                    },
                    reason="Gap evidence asks for changing news, release, or product coverage.",
                    score=0.58,
                    evidence=evidence,
                ),
                _proposal(
                    source_kind="news",
                    title=f"News domain candidate: {query}",
                    uri=_google_news_search_uri(query),
                    tags=["news", "domain-candidate", "needs-review"],
                    metadata={
                        "source_family": "news_domain",
                        "query": query,
                        "frequency": "daily",
                        "max_items": 10,
                    },
                    reason="A human should review which recurring news domain is trustworthy for this gap.",
                    score=0.52,
                    evidence=evidence,
                ),
                _proposal(
                    source_kind="manual",
                    title=f"Manual source review: {query}",
                    uri=f"manual://source-review?query={quote_plus(query)}",
                    tags=["manual-review", "needs-review"],
                    metadata={
                        "source_family": "manual_review_query",
                        "query": query,
                        "frequency": "paused",
                        "max_items": 1,
                    },
                    reason="Low-confidence discovery should be reviewed before adding a new source.",
                    score=0.42,
                    evidence=evidence,
                ),
            ]
        )

    return _dedupe_new_proposals(proposals, existing_uris=existing_uris, existing_categories=existing_categories)


def _proposal(
    *,
    source_kind: str,
    title: str,
    uri: str,
    tags: list[str],
    metadata: dict[str, Any],
    reason: str,
    score: float,
    evidence: list[str],
) -> dict[str, Any]:
    final_score = max(0.0, min(1.0, float(score)))
    decision = "approve" if final_score >= 0.7 else "needs_review"
    fingerprint = "|".join([source_kind, title, uri, str(metadata.get("category") or metadata.get("query") or "")])
    return {
        "proposal_id": "srcdisc_" + sha256(fingerprint.encode("utf-8")).hexdigest()[:16],
        "source_kind": source_kind,
        "title": title,
        "uri": uri,
        "tags": list(tags),
        "metadata": {**dict(metadata), "evidence": list(evidence)[:10]},
        "reason": reason,
        "score": round(final_score, 3),
        "decision": decision,
    }


def _candidate_categories(evidence: list[str]) -> list[str]:
    text = " ".join(evidence).lower()
    scores: dict[str, int] = {}
    for category, hints in _CATEGORY_HINTS:
        count = sum(1 for hint in hints if hint in text)
        if count:
            scores[category] = count
    return sorted(scores, key=lambda category: (-scores[category], category))


def _looks_like_news_or_product_gap(evidence: list[str]) -> bool:
    text = " ".join(evidence).lower()
    return any(hint in text for hint in _NEWS_HINTS)


def _best_query(evidence: list[str]) -> str:
    for text in evidence:
        if any(hint in text.lower() for hint in _NEWS_HINTS):
            return strip_candidate_title_prefixes(text, default="source discovery")[:120]
    return strip_candidate_title_prefixes(evidence[0] if evidence else "", default="source discovery")[:120]


def _dedupe_new_proposals(
    proposals: list[dict[str, Any]],
    *,
    existing_uris: set[str],
    existing_categories: set[str],
) -> list[dict[str, Any]]:
    deduped: list[dict[str, Any]] = []
    seen_keys: set[tuple[str, str]] = set()
    for proposal in proposals:
        uri = _normalize_uri(str(proposal.get("uri") or ""))
        category = str((proposal.get("metadata") or {}).get("category") or "")
        if uri and uri in existing_uris:
            continue
        if category and category in existing_categories:
            continue
        key = (str(proposal.get("source_kind") or ""), uri or category)
        if key in seen_keys:
            continue
        seen_keys.add(key)
        deduped.append(proposal)
    return deduped


def _existing_chatpaper_categories(sources: list[Any]) -> set[str]:
    categories: set[str] = set()
    for source in sources:
        uri_category = _chatpaper_category_from_uri(_source_value(source, "uri"))
        if uri_category:
            categories.add(uri_category)
        metadata = _source_value(source, "metadata")
        if not isinstance(metadata, dict):
            continue
        for category in metadata.get("categories") or []:
            text = str(category or "").strip()
            if text:
                categories.add(text)
    return categories


def _source_value(source: Any, key: str) -> Any:
    if isinstance(source, dict):
        return source.get(key)
    return getattr(source, key, "")


def _normalized_texts(values: list[str]) -> list[str]:
    normalized: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = " ".join(str(value or "").split())
        if not text or text in seen:
            continue
        seen.add(text)
        normalized.append(text)
    return normalized


def _chatpaper_category_uri(category: str) -> str:
    group, name = str(category).split(".", 1)
    return f"{_CHATPAPER_DASHBOARD_PREFIX}/{group}/{name}"


def _google_news_rss_uri(query: str) -> str:
    encoded = quote_plus(query)
    return f"https://news.google.com/rss/search?q={encoded}&hl=en-US&gl=US&ceid=US:en"


def _google_news_search_uri(query: str) -> str:
    return f"https://news.google.com/search?q={quote_plus(query)}"


def _normalize_uri(uri: Any) -> str:
    return str(uri or "").strip().rstrip("/")


def _chatpaper_category_from_uri(uri: str) -> str:
    parsed = urlparse(str(uri or ""))
    query_category = parse_qs(parsed.query).get("category", [""])[0]
    if query_category:
        return str(query_category).strip()
    parts = [part for part in parsed.path.split("/") if part]
    if "arxiv" not in parts:
        return ""
    index = parts.index("arxiv")
    if index + 2 < len(parts):
        return f"{parts[index + 1]}.{parts[index + 2]}"
    if index + 1 < len(parts) and "." in parts[index + 1]:
        return parts[index + 1]
    return ""
