"""Isolated stdlib regressions for prompt formatting helpers.

Load only the two helper definitions; never import the application runtime.
"""
from __future__ import annotations

import ast
from pathlib import Path
import unittest


def _load_helpers():
    source = Path(__file__).resolve().parents[1] / "eimemory/persona/prompt.py"
    tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
    helpers = [
        node for node in tree.body
        if isinstance(node, ast.FunctionDef)
        and node.name in {"_fit_lines", "_safe_max_chars"}
    ]
    if len(helpers) != 2:
        raise AssertionError("Expected both prompt formatting helpers")
    module = ast.Module(
        body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0), *helpers],
        type_ignores=[],
    )
    namespace = {}
    exec(compile(ast.fix_missing_locations(module), str(source), "exec"), namespace)
    return namespace["_fit_lines"], namespace["_safe_max_chars"]


_fit_lines, _safe_max_chars = _load_helpers()


class PromptCharacterBudgetTests(unittest.TestCase):
    def test_fitting_lines_preserve_whitespace(self):
        lines = ["  abc  ", "\tdef\t", ""]
        expected = "\n".join(lines)
        self.assertEqual(_fit_lines(lines, max_chars=len(expected)), expected)
        self.assertEqual(_fit_lines(lines, max_chars=800), expected)

    def test_first_line_overflow_uses_original_prefix(self):
        self.assertEqual(_fit_lines(["abc   "], max_chars=3), "abc")
        self.assertEqual(_fit_lines(["  abc"], max_chars=3), "  a")
        self.assertEqual(_fit_lines(["\t  "], max_chars=2), "\t ")

    def test_later_line_overflow_keeps_fitting_prefix(self):
        self.assertEqual(_fit_lines(["abc", "def   "], max_chars=7), "abc")
        self.assertEqual(_fit_lines(["abc", ""], max_chars=3), "abc")
        self.assertEqual(_fit_lines(["abc", ""], max_chars=4), "abc\n")

    def test_stops_at_first_overflow(self):
        self.assertEqual(_fit_lines(["ab", "too long", "c"], max_chars=4), "ab")
        self.assertEqual(_fit_lines(["too long", "c"], max_chars=4), "too ")

    def test_existing_plain_text_branches(self):
        self.assertEqual(_fit_lines(["abc", "def"], max_chars=7), "abc\ndef")
        self.assertEqual(_fit_lines(["abc", "def"], max_chars=6), "abc")
        self.assertEqual(_fit_lines(["abc"], max_chars=2), "ab")
        self.assertEqual(_fit_lines(["abc"], max_chars=0), "")
        self.assertEqual(_fit_lines(["", ""], max_chars=0), "")
        self.assertEqual(_fit_lines([""], max_chars=1), "")

    def test_actual_length_respects_each_nonnegative_limit(self):
        cases = [
            ["abc   "], ["  abc"], ["\t  "], ["", "abc"],
            ["abc", "", "def  "], ["  ", "\t", ""],
            ["é🙂", " z  "], ["abc", "def"],
        ]
        for lines in cases:
            for limit in range(30):
                with self.subTest(lines=lines, limit=limit):
                    self.assertLessEqual(len(_fit_lines(lines, max_chars=limit)), limit)

    def test_safe_limits_existing_conversion_and_fallbacks(self):
        cases = [
            (None, 800), ("bad", 800), (object(), 800),
            (float("nan"), 800), ("17", 17), (" 17 ", 17),
            (17.9, 17), (0, 0), (-1, 0), ("-17", 0),
            (False, 0), (True, 1), (800, 800),
        ]
        for value, expected in cases:
            with self.subTest(value=value):
                self.assertEqual(_safe_max_chars(value), expected)

    def test_overflow_uses_existing_fallback(self):
        class OverflowingInt:
            def __int__(self):
                raise OverflowError("cannot convert")

        for value in [float("inf"), float("-inf"), OverflowingInt()]:
            with self.subTest(value=value):
                self.assertEqual(_safe_max_chars(value), 800)


if __name__ == "__main__":
    unittest.main()
