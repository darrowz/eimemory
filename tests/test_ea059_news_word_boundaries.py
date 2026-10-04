"""EA-059: exercise only the pure news helper without importing the project."""

import ast
from pathlib import Path
import re
import unittest


def _load_news_cues():
    path = Path(__file__).resolve().parents[1] / "eimemory/recall/intent.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    selected = []
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "_TERM_PATTERN"
            for target in node.targets
        ):
            selected.append(node)
        elif isinstance(node, ast.FunctionDef) and node.name == "_apply_news_cues":
            selected.append(node)
    if len(selected) != 2:
        raise AssertionError("Expected only the term pattern and news helper")
    namespace = {"re": re}
    exec(compile(ast.Module(body=selected, type_ignores=[]), str(path), "exec"), namespace)
    return namespace["_apply_news_cues"]


class NewsWordBoundaryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.apply_news = staticmethod(_load_news_cues())

    def assert_news(self, query, score, expected_reasons):
        scores = {"news": 0.0}
        reasons = {"news": []}
        self.apply_news(normalized_lower=query.lower(), scores=scores, reasons=reasons)
        self.assertAlmostEqual(scores["news"], score)
        self.assertEqual(reasons["news"], expected_reasons)

    def test_english_words_and_separators(self):
        for query in ("AI news", "ai news", "ToDaY NeWs", "news TODAY", "ai-news", "today:news", "AI\tnews", "today\u00a0news"):
            with self.subTest(query=query):
                self.assert_news(query, 0.45, ["keyword: news"])

    def test_embedded_modifiers_do_not_match(self):
        for word in ("trains", "email", "retail", "airlines", "unavailable", "aim", "chair", "said", "todayish", "xtoday"):
            with self.subTest(word=word):
                self.assert_news("news about " + word, 0.0, [])

    def test_chinese_adjacent_modifiers_remain_supported(self):
        for query in ("今日news", "今天news", "最新news", "要闻news", "news今日摘要", "news今天摘要", "news最新摘要", "news要闻摘要"):
            with self.subTest(query=query):
                self.assert_news(query, 0.45, ["keyword: news"])

    def test_chinese_news_and_additive_score(self):
        self.assert_news("新闻", 0.82, ["keyword: 新闻"])
        self.assert_news("AI新闻news", 1.27, ["keyword: 新闻", "keyword: news"])

    def test_empty_and_missing_cues(self):
        for query in ("", " \t\n", "...", "news", "AI newsletter", "today newsletter", "latest news", "ＡＩ news"):
            with self.subTest(query=query):
                self.assert_news(query, 0.0, [])


if __name__ == "__main__":
    unittest.main()
