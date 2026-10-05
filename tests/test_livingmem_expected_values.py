"""AST-isolated expectation checks; no application imports or real datasets."""

import ast
from collections.abc import Mapping
from pathlib import Path
import unittest


def load_matches_expected(source_text=None):
    source = Path(__file__).resolve().parents[1] / "eimemory/evaluation/livingmem.py"
    if source_text is None:
        source_text = source.read_text(encoding="utf-8")
    tree = ast.parse(source_text, filename=str(source))
    functions = [
        node for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "_matches_expected"
    ]
    if len(functions) != 1:
        raise AssertionError("Expected exactly one _matches_expected function")
    module = ast.Module(
        body=[
            ast.ImportFrom(
                module="__future__", names=[ast.alias(name="annotations")], level=0,
            ),
            *functions,
        ],
        type_ignores=[],
    )
    namespace = {"Mapping": Mapping}
    exec(compile(ast.fix_missing_locations(module), str(source), "exec"), namespace)
    return namespace["_matches_expected"]


MATCHES_EXPECTED = load_matches_expected()


class LivingMemExpectedValuesTests(unittest.TestCase):
    def setUp(self):
        self.matches = MATCHES_EXPECTED

    def test_truthy_noniterable_actual_is_mismatch(self):
        for actual in (7, -2, 1.5, True):
            for expected in ([], ["wanted"]):
                with self.subTest(actual=actual, expected=expected):
                    self.assertIs(
                        self.matches({"boundary": actual}, {"boundary": expected}),
                        False,
                    )

    def test_list_equality_preserves_order_and_duplicates(self):
        expected = {"boundary": ["first", "second", "first"]}
        self.assertIs(self.matches(expected, expected), True)
        for actual in (["first", "second"], ["second", "first", "first"], []):
            with self.subTest(actual=actual):
                self.assertIs(self.matches({"boundary": actual}, expected), False)

    def test_list_values_are_not_flattened_or_stringified(self):
        actual = {"boundary": [{"nested": [1]}, [2], 3]}
        self.assertIs(self.matches(actual, actual), True)
        self.assertIs(self.matches({"boundary": ["3"]}, {"boundary": [3]}), False)

    def test_existing_tuple_conversion_is_preserved(self):
        self.assertIs(
            self.matches({"boundary": ("first", "second")}, {"boundary": ["first", "second"]}),
            True,
        )

    def test_existing_string_conversion_is_preserved(self):
        self.assertIs(self.matches({"boundary": "ab"}, {"boundary": ["a", "b"]}), True)
        self.assertIs(self.matches({"boundary": "ab"}, {"boundary": ["ab"]}), False)

    def test_existing_mapping_conversion_is_preserved(self):
        self.assertIs(
            self.matches({"boundary": {"first": 1, "second": 2}}, {"boundary": ["first", "second"]}),
            True,
        )

    def test_existing_falsy_actual_conversion_is_preserved(self):
        for actual in (None, False, 0, 0.0, "", [], (), {}):
            with self.subTest(actual=actual):
                self.assertIs(self.matches({"boundary": actual}, {"boundary": []}), True)
                self.assertIs(self.matches({"boundary": actual}, {"boundary": ["wanted"]}), False)
        self.assertIs(self.matches({}, {"boundary": []}), True)
        self.assertIs(self.matches({}, {"boundary": ["wanted"]}), False)

    def test_nonmapping_actual_uses_existing_empty_mapping_fallback(self):
        for actual in (None, 7, "text", []):
            with self.subTest(actual=actual):
                self.assertIs(self.matches(actual, {"boundary": []}), True)
                self.assertIs(self.matches(actual, {"boundary": ["wanted"]}), False)

    def test_nonmapping_expected_remains_unscored(self):
        for expected in (None, 7, "text", []):
            with self.subTest(expected=expected):
                self.assertIsNone(self.matches({"boundary": ["wanted"]}, expected))

    def test_empty_mapping_and_scalar_equality_are_unchanged(self):
        self.assertIs(self.matches(None, {}), True)
        self.assertIs(self.matches({"motive": "efficiency"}, {"motive": "efficiency"}), True)
        self.assertIs(self.matches({"motive": "efficiency"}, {"motive": "other"}), False)
        self.assertIs(self.matches({"trust_delta": 1}, {"trust_delta": 1}), True)

    def test_scalar_mismatch_does_not_abort_later_comparisons(self):
        actual_values = [7, ["wanted"], ["other"]]
        results = [
            self.matches({"boundary": actual}, {"boundary": ["wanted"]})
            for actual in actual_values
        ]
        self.assertEqual(results, [False, True, False])

    def test_all_expected_fields_are_still_required(self):
        actual = {"boundary": ["wanted"], "motive": "efficiency"}
        self.assertIs(self.matches(actual, actual), True)
        self.assertIs(
            self.matches(actual, {"boundary": ["wanted"], "motive": "other"}),
            False,
        )


if __name__ == "__main__":
    unittest.main()
