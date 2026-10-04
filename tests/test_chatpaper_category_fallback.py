"""Regression coverage for ChatPaper -> arXiv category identity (offline only)."""
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

import pytest

from eimemory.intake.connectors import collect_from_source_entry


@pytest.mark.parametrize("uri, metadata, expected", [
    ("https://www.chatpaper.ai/api/papers/arxiv?category=cs.RO", {}, ["cs.RO"]),
    ("https://www.chatpaper.ai/zh/dashboard/arxiv/cs/AI?category=cs.RO", {}, ["cs.RO"]),
    ("https://www.chatpaper.ai/api/papers/arxiv?category=cs%2ERO", {}, ["cs.RO"]),
    ("https://www.chatpaper.ai/api/papers/arxiv?category=cs.AI&category=cs.RO", {}, ["cs.RO"]),
    ("https://www.chatpaper.ai/zh/dashboard/arxiv/cs/RO?category=", {}, ["cs.RO"]),
    ("https://www.chatpaper.ai/zh/dashboard/arxiv/cs/RO", {}, ["cs.RO"]),
    ("https://www.chatpaper.ai/api/papers/arxiv", {}, ["cs.AI"]),
    ("https://www.chatpaper.ai/api/papers/arxiv?category=cs.RO", {"categories": ["cs.LG", "cs.CV", "cs.LG"]}, ["cs.LG", "cs.CV"]),
])
def test_fallback_preserves_primary_category_resolution(uri, metadata, expected):
    calls = []

    def fake_fetch(url):
        calls.append(url)
        if urlparse(url).hostname == "www.chatpaper.ai":
            raise TimeoutError("injected timeout")
        assert urlparse(url).hostname == "export.arxiv.org"
        return '''<feed xmlns="http://www.w3.org/2005/Atom"><entry>
        <id>https://arxiv.org/abs/2604.19740v1</id><title>Offline fixture</title>
        <summary>A sufficiently detailed fixture about local recall policy.</summary>
        </entry></feed>'''

    result = collect_from_source_entry(SimpleNamespace(source_kind="url", uri=uri, metadata=metadata), fetch_text=fake_fetch)
    primary_categories = [parse_qs(urlparse(url).query)["category"][0] for url in calls if urlparse(url).hostname == "www.chatpaper.ai"]
    fallback_queries = [parse_qs(urlparse(url).query)["search_query"][0] for url in calls if urlparse(url).hostname == "export.arxiv.org"]
    assert primary_categories == expected
    assert fallback_queries == [f"cat:{category}" for category in expected]
    assert result.ok is True
    assert result.metadata["categories"] == expected
    assert result.metadata["fallback"] == "arxiv"
    assert len(result.items) == 1  # cross-category duplicate remains bounded
