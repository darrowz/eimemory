"""Offline unit regressions; extract only two functions, never import the project.

Run directly with Python or discover with unittest/pytest. This isolated suite
checks function behavior only, not project imports, integration, or full pytest.
"""

from __future__ import annotations

import ast
import re
import unittest
from pathlib import Path
from types import SimpleNamespace


def _extract_function(relative_path, function_name):
    """Compile one top-level function without running its source module."""
    root = Path(__file__).resolve().parents[1]
    path = root / "eimemory" / "governance" / "learning" / relative_path
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    matches = [
        node for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == function_name
    ]
    if len(matches) != 1:
        raise AssertionError(f"Expected exactly one {function_name} in {path}")
    node = matches[0]
    if node.decorator_list or node.args.defaults or any(node.args.kw_defaults):
        raise AssertionError("Extraction requires no decorators or defaults")
    module = ast.Module(
        body=[
            ast.ImportFrom(
                module="__future__",
                names=[ast.alias(name="annotations")],
                level=0,
            ),
            node,
        ],
        type_ignores=[],
    )
    namespace = {"re": re}
    exec(compile(ast.fix_missing_locations(module), str(path), "exec"), namespace)
    return namespace[function_name]


def _record(steps=None, detail="", summary=""):
    return SimpleNamespace(content={"steps": steps}, detail=detail, summary=summary)


class StepsRegressionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.steps = staticmethod(_extract_function("skill_sedimentation.py", "_steps"))

    def test_blank_list_falls_through_to_later_valid_list(self):
        records = [
            _record(["", " ", "\t\n"], detail="ignore this text"),
            _record(["inspect input", "verify output"]),
        ]
        self.assertEqual(self.steps(records), ["inspect input", "verify output"])

    def test_blank_list_falls_back_to_text(self):
        records = [_record([" ", "\t"], detail="Inspect input; Verify output")]
        self.assertEqual(self.steps(records), ["Inspect input", "Verify output"])

    def test_blank_list_falls_back_to_default(self):
        self.assertEqual(
            self.steps([_record(["", " ", "\t\n"])]),
            ["Run the repeated SOP and verify replay evidence before activation."],
        )

    def test_first_valid_list_wins_over_later_list_and_text(self):
        records = [
            _record(["first"], detail="text fallback"),
            _record(["second"], summary="other text"),
        ]
        self.assertEqual(self.steps(records), ["first"])

    def test_later_valid_list_wins_over_earlier_text(self):
        records = [_record(detail="earlier text"), _record(["explicit step"])]
        self.assertEqual(self.steps(records), ["explicit step"])

    def test_explicit_list_preserves_whitespace_and_stringifies_values(self):
        self.assertEqual(
            self.steps([_record([" ", "  keep padding  ", 0, False, None, "\t"])]),
            ["  keep padding  ", "0", "False", "None"],
        )

    def test_explicit_list_is_not_subject_to_text_limits(self):
        expected = ["x" * 241, "two", "three", "four", "five", "six", "seven"]
        self.assertEqual(self.steps([_record(expected)]), expected)

    def test_empty_and_non_list_steps_use_text(self):
        for value in (None, [], "ignored string", ("ignored tuple",), {}):
            with self.subTest(value=value):
                self.assertEqual(self.steps([_record(value, detail="Use text")]), ["Use text"])

    def test_text_uses_record_detail_then_summary_order(self):
        records = [
            _record(detail="first;", summary="second;"),
            _record(detail="third;", summary="fourth"),
        ]
        self.assertEqual(self.steps(records), ["first", "second", "third", "fourth"])

    def test_text_numbering_separators_and_edge_punctuation(self):
        self.assertEqual(
            self.steps([_record(detail="1. First\n2) Second; .:-Third-:. ")]),
            ["First", "Second", "Third"],
        )

    def test_text_is_limited_to_six_steps_and_240_characters(self):
        detail = ";".join(["a" * 241, "two", "three", "four", "five", "six", "seven"])
        self.assertEqual(
            self.steps([_record(detail=detail)]),
            ["a" * 240, "two", "three", "four", "five", "six"],
        )

    def test_absent_records_and_empty_text_use_default(self):
        for records in ([], [_record()], [_record(detail=None, summary=None)]):
            with self.subTest(records=records):
                self.assertEqual(
                    self.steps(records),
                    ["Run the repeated SOP and verify replay evidence before activation."],
                )


class BoundedRegressionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.bounded = staticmethod(_extract_function("learning_eval.py", "_bounded"))

    def test_float_nan_is_zero(self):
        self.assertEqual(self.bounded(float("nan")), 0.0)

    def test_text_nan_is_zero(self):
        for value in ("nan", "NaN", " NAN ", "+nan", "-nan"):
            with self.subTest(value=value):
                self.assertEqual(self.bounded(value), 0.0)

    def test_finite_values_and_boundaries(self):
        for value, expected in (
            (-2.0, 0.0), (0, 0.0), (0.25, 0.25), (1, 1.0), (2.0, 1.0),
            ("-0.25", 0.0), (" 0.75 ", 0.75), ("1.25", 1.0),
        ):
            with self.subTest(value=value):
                self.assertEqual(self.bounded(value), expected)

    def test_invalid_values_and_none_are_zero(self):
        for value in ("", "not a number", None, [], {}):
            with self.subTest(value=value):
                self.assertEqual(self.bounded(value), 0.0)

    def test_boolean_behavior_is_preserved(self):
        self.assertEqual(self.bounded(False), 0.0)
        self.assertEqual(self.bounded(True), 1.0)

    def test_infinity_behavior_is_preserved(self):
        for value, expected in (
            (float("inf"), 1.0), (float("-inf"), 0.0),
            ("Infinity", 1.0), ("-Infinity", 0.0),
        ):
            with self.subTest(value=value):
                self.assertEqual(self.bounded(value), expected)


if __name__ == "__main__":
    unittest.main()
