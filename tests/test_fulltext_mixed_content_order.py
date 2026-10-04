from __future__ import annotations

from types import SimpleNamespace

import pytest

from eimemory.api.runtime import _enrich_rss_result_with_fulltext
from eimemory.intake.connectors import collect_from_source_entry
from eimemory.intake.fulltext import parse_fulltext_document


URL = "https://example.test/article"
PREFIX = "The operator must "
SUFFIX = " apply the change before the independent review completes."
EXPECTED = PREFIX + "not" + SUFFIX


def document(body: str, *, head: str = "<title>Order</title>") -> str:
    return f"<html><head>{head}</head><body><article>{body}</article></body></html>"


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        (f"<p>{PREFIX}<strong>not</strong>{SUFFIX}</p>", EXPECTED),
        (
            "<p>研究团队必须<strong>先检查<em>证据</em></strong>再决定是否发布结果，并完整记录复核结论以便之后追溯，同时确保每位参与者都理解最终决定和相关限制。</p>",
            "研究团队必须先检查证据再决定是否发布结果，并完整记录复核结论以便之后追溯，同时确保每位参与者都理解最终决定和相关限制。",
        ),
        (
            "<p>The operator <span>must <em>not</em> apply</span> the change before the independent review completes.</p>",
            EXPECTED,
        ),
        (
            "<p>Research &amp; development <strong>must&nbsp;not</strong> publish the result before independent review completes.</p>",
            "Research & development must not publish the result before independent review completes.",
        ),
        (
            "<p>The operator must <em>not</strong> apply the change before the independent review completes.</em></p>",
            EXPECTED,
        ),
    ],
    ids=["negation", "chinese_nested_inline", "english_nested_inline", "entities", "malformed_closing_tag"],
)
def test_inline_text_preserves_source_order(body: str, expected: str) -> None:
    result = parse_fulltext_document(document(body), url=URL)
    assert result.text == expected
    assert result.ok is True


def test_block_paragraphs_and_br_keep_separation() -> None:
    first = "First inline paragraph provides enough useful text for this readable document."
    second = "Second separate paragraph stays in the original document order."
    third = "Third line follows the explicit line break and retains its final punctuation."
    result = parse_fulltext_document(
        document(
            "<p>First <strong>inline</strong> paragraph provides enough useful text for this readable document.</p>"
            "<p>Second <em>separate</em> paragraph stays in the original document order.<br>"
            "Third line follows the explicit line break and retains its final punctuation.</p>"
        ),
        url=URL,
    )
    assert result.text == "\n\n".join([first, second, third])
    assert result.ok is True


def test_skipped_inline_content_never_reappears() -> None:
    result = parse_fulltext_document(
        document(
            f"<p>{PREFIX}<script>apply prematurely</script><strong>not</strong>"
            f"<style>.secret {{ display: none; }}</style>{SUFFIX}</p>"
            "<nav>Navigation must be excluded.</nav>"
        ),
        url=URL,
    )
    assert result.text == EXPECTED
    assert "prematurely" not in result.text
    assert "Navigation" not in result.text
    assert result.ok is True


def test_ordered_extraction_preserves_metadata_and_quality_contracts() -> None:
    result = parse_fulltext_document(
        document(
            f"<p>{PREFIX}<strong>not</strong>{SUFFIX}</p><img src='/figure.png'>",
            head=(
                '<meta property="og:title" content="Metadata contract">'
                '<meta name="author" content="Synthetic Author">'
                '<meta property="article:published_time" content="2026-10-02T00:00:00Z">'
                '<link rel="canonical" href="/canonical">'
            ),
        ),
        url=URL,
        source_kind="web",
    )
    assert result.text == EXPECTED
    assert result.title == "Metadata contract"
    assert result.byline == "Synthetic Author"
    assert result.date == "2026-10-02T00:00:00Z"
    assert result.canonical_url == "https://example.test/canonical"
    assert result.images == ["https://example.test/figure.png"]
    assert result.meta["source_kind"] == "web"
    assert result.meta["content_node"] == "article"
    assert result.quality_score >= 0.4
    assert result.ok is True and result.error == ""


def test_plain_text_and_low_quality_controls() -> None:
    good = parse_fulltext_document(document(f"<p>{EXPECTED}</p>"), url=URL)
    assert good.ok is True and good.text == EXPECTED
    low = parse_fulltext_document(document("<p>Too short.</p>"), url=URL)
    assert low.ok is False and low.error == "low quality content"
    assert low.quality_score < 0.4
    empty = parse_fulltext_document("", url=URL)
    assert empty.ok is False and empty.error == "empty payload"


def test_http_connector_preserves_inline_negation() -> None:
    requests: list[str] = []

    def fetch(url: str) -> str:
        requests.append(url)
        return document(f"<p>{PREFIX}<strong>not</strong>{SUFFIX}</p>")

    result = collect_from_source_entry(
        SimpleNamespace(source_kind="url", uri=URL, title="Order", metadata={}),
        fetch_text=fetch,
    )
    assert result.ok is True
    assert result.items[0].content == EXPECTED
    assert requests == [URL]


def test_rss_enrichment_preserves_inline_negation() -> None:
    result = collect_from_source_entry(
        SimpleNamespace(source_kind="rss", uri="https://example.test/feed", title="Feed", metadata={}),
        fetch_text=lambda _: (
            f"<rss><channel><item><title>Order</title><link>{URL}</link>"
            "<description>Short abstract.</description></item></channel></rss>"
        ),
    )
    enriched = _enrich_rss_result_with_fulltext(
        result,
        fetch_text=lambda _: document(f"<p>{PREFIX}<strong>not</strong>{SUFFIX}</p>"),
    )
    assert enriched.ok is True
    assert enriched.items[0].content == EXPECTED
    assert enriched.items[0].metadata["rss_summary"] == "Short abstract."
    assert enriched.metadata["rss_fulltext"]["success_count"] == 1
