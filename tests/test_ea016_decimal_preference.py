"""EA-016 text-only regressions; load pure functions without project imports."""

import ast
from pathlib import Path
import re
import unittest


def _extract_preference_function():
    path = Path(__file__).resolve().parents[1] / "eimemory/raw/synthetic.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    tree.body = [node for node in tree.body if isinstance(node, ast.FunctionDef)]
    namespace = {"re": re}
    exec(compile(tree, str(path), "exec"), namespace)
    return namespace["synthetic_preference_texts"]


class DecimalPreferenceTests(unittest.TestCase):
    def test_numeric_and_version_fragments(self):
        extract = _extract_preference_function()
        cases = [
            ("I prefer Python 3.12.", "prefer Python 3.12"),
            ("I prefer Python3.12.", "prefer Python3.12"),
            ("I like 3.5 mm headphones.", "like 3.5 mm headphones"),
            ("I like 3.5mm headphones.", "like 3.5mm headphones"),
            ("I prefer 3.5毫米耳机.", "prefer 3.5毫米耳机"),
            ("I prefer 版本3.12.1.", "prefer 版本3.12.1"),
            ("I don’t like 3.5mm cables.", "do not like 3.5mm cables"),
            ("I find Python 3.12 more reliable.", "find Python 3.12 more reliable"),
        ]
        for source, expected in cases:
            with self.subTest(source=source):
                self.assertEqual(extract(source), ["User preference: " + expected])

    def test_sentence_stops(self):
        extract = _extract_preference_function()
        for ending in (".", "!", "?", ";", "\n"):
            for text in ("tea", "Python 3.12", "3.5mm耳机"):
                with self.subTest(ending=ending, text=text):
                    self.assertEqual(
                        extract("I prefer " + text + ending + " Extra words"),
                        ["User preference: prefer " + text],
                    )

    def test_existing_text_behavior(self):
        extract = _extract_preference_function()
        cases = [
            ("", []),
            ("I like Tea. I LIKE tea.", ["User preference: like Tea"]),
            ('I prefer : ; I like "".', []),
            ("I like apples\nand pears", ["User preference: like apples"]),
            ("I prefer a..b", ["User preference: prefer a"]),
        ]
        for source, expected in cases:
            with self.subTest(source=source):
                self.assertEqual(extract(source), expected)


if __name__ == "__main__":
    unittest.main()
