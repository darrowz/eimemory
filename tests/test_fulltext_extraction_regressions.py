"""Synthetic, stdlib-only fulltext regressions; no package/runtime imports.

Run directly with python tests/test_fulltext_extraction_regressions.py.
"""
from __future__ import annotations

import ast
from pathlib import Path
import sys
import types
import unittest


def load_parser():
    path = Path(__file__).resolve().parents[1] / "eimemory/intake/fulltext.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    allowed = {"__future__", "dataclasses", "html", "html.parser", "re", "typing", "urllib.parse"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            assert all(alias.name in allowed for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0 and node.module in allowed
    module = types.ModuleType("_isolated_fulltext_extraction_regressions")
    sys.modules[module.__name__] = module
    exec(compile(tree, str(path), "exec"), module.__dict__)
    return module


FULLTEXT = load_parser()
URL = "https://example.test/news/story"
ARTICLE = "Actual article content explains the findings in useful detail. " * 8


def parse(body, head="", source_kind=None):
    return FULLTEXT.parse_fulltext_document(
        f"<html><head>{head}</head><body>{body}</body></html>",
        url=URL, source_kind=source_kind,
    )


class FulltextExtractionRegressions(unittest.TestCase):
    def test_nested_entities_decode_once_in_text_and_inline_order(self):
        result = parse("<article><p>Literal &amp;lt;tag&amp;gt; and <b>&amp;amp;</b> text. "
                       "Research &amp; development&nbsp;continues.</p></article>")
        self.assertEqual(result.text, "Literal &lt;tag&gt; and &amp; text. Research & development continues.")

    def test_nested_entities_decode_once_in_titles_and_metadata(self):
        for head in (
            "<title>Literal &amp;lt;tag&amp;gt; - Example</title>",
            '<meta property="og:title" content="Literal &amp;lt;tag&amp;gt;">',
            '<meta name="twitter:title" content="Literal &amp;lt;tag&amp;gt;">',
        ):
            with self.subTest(head=head):
                result = parse(f"<article><p>{ARTICLE}</p></article>", head +
                               '<meta name="author" content="R&amp;amp;D">'
                               '<meta name="date" content="2026 &amp;lt;October&amp;gt;">')
                self.assertEqual(result.title, "Literal &lt;tag&gt;")
                self.assertEqual(result.byline, "R&amp;D")
                self.assertEqual(result.date, "2026 &lt;October&gt;")
        result = parse(f'<article><h1 id="activity-name">Literal &amp;lt;tag&amp;gt;</h1><p>{ARTICLE}</p></article>')
        self.assertEqual(result.title, "Literal &lt;tag&gt;")

    def test_skipped_ancestors_exclude_candidates_and_preferred_id(self):
        for tag in ("nav", "footer", "form", "aside", "header"):
            for preferred in ("", ' id="js_content"'):
                for source_kind in (None, "web", "wechat"):
                    with self.subTest(tag=tag, preferred=preferred, source_kind=source_kind):
                        result = parse(f"<{tag}><section><div{preferred}><p>{'Menu noise. ' * 200}</p>"
                                       f"</div></section></{tag}><article><p>{ARTICLE}</p></article>",
                                       source_kind=source_kind)
                        self.assertEqual(result.text, ARTICLE.strip())
                        self.assertEqual(result.meta["content_node"], "article")
                        self.assertTrue(result.ok)

    def test_skipped_paragraphs_do_not_inflate_candidate_score(self):
        result = parse("<div>Short teaser.<nav>" + "<p>Long navigation paragraph.</p>" * 100 +
                       f"</nav></div><article><p>{ARTICLE}</p></article>")
        self.assertEqual(result.meta["content_node"], "article")

    def test_eligible_preferred_container_still_wins(self):
        result = parse(f'<div id="js_content"><p>{ARTICLE}</p></div>'
                       f"<article><p>{'Other content. ' * 100}</p></article>", source_kind="wechat")
        self.assertEqual(result.text, ARTICLE.strip())
        self.assertEqual(result.meta["content_node"], "div#js_content")

    def test_image_paths_use_document_not_canonical(self):
        result = parse(f'<article><p>{ARTICLE}</p><img src="photo.jpg"><img data-src="/root.png">'
                       '<img src="https://images.example.test/absolute.png"></article>',
                       '<link rel="canonical" href="https://example.test/archive/story">'
                       '<meta property="og:image" content="cover.jpg?name=R&amp;amp;D">')
        self.assertEqual(result.canonical_url, "https://example.test/archive/story")
        self.assertEqual(result.images, ["https://example.test/news/cover.jpg?name=R&amp;D",
                                        "https://example.test/news/photo.jpg", "https://example.test/root.png",
                                        "https://images.example.test/absolute.png"])

    def test_first_explicit_base_href_controls_resource_paths(self):
        for bases, expected in (
            ('<base href="/assets/"><base href="/ignored/">', "https://example.test/assets/photo.jpg"),
            ('<base target="_blank"><base href="../media/">', "https://example.test/media/photo.jpg"),
            ('<base href="https://images.example.test/assets/">', "https://images.example.test/assets/photo.jpg"),
            ('<base href=""><base href="/ignored/">', "https://example.test/news/photo.jpg"),
        ):
            with self.subTest(bases=bases):
                result = parse(f'<article><p>{ARTICLE}</p><img src="photo.jpg"></article>',
                               bases + '<link rel="canonical" href="https://example.test/archive/story">')
                self.assertEqual(result.images, [expected])
                self.assertEqual(result.canonical_url, "https://example.test/archive/story")

    def test_block_boundaries_empty_and_low_quality_controls(self):
        result = parse("<article><p>First <b>inline</b> paragraph.</p><p>Second paragraph.</p></article>")
        self.assertEqual(result.text, "First inline paragraph.\n\nSecond paragraph.")
        self.assertFalse(parse("<article>Short.</article>").ok)
        self.assertFalse(FULLTEXT.parse_fulltext_document("", url=URL).ok)
        self.assertEqual(parse("<nav><div>Navigation only.</div></nav>").text, "")


if __name__ == "__main__":
    unittest.main()
