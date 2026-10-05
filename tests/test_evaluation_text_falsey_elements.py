"""Offline stdlib regression tests; load only the two helper function ASTs."""
from __future__ import annotations

import ast
from collections.abc import Mapping
from pathlib import Path
import unittest


SOURCE = Path(__file__).resolve().parents[1] / "eimemory/evaluation/_text.py"


def load_helpers():
    tree = ast.parse(SOURCE.read_text(encoding="utf-8"), filename=str(SOURCE))
    names = {"extract_text_from_turn", "extract_text_from_messages"}
    functions = [
        node for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name in names
    ]
    if len(functions) != 2 or {node.name for node in functions} != names:
        raise AssertionError("Expected exactly the two text helpers")
    for function in functions:
        if function.decorator_list or any(
            isinstance(node, (ast.Import, ast.ImportFrom))
            for node in ast.walk(function)
        ):
            raise AssertionError("Helper decorators and imports are not allowed")
    module = ast.fix_missing_locations(ast.Module(body=functions, type_ignores=[]))
    namespace = {
        "Mapping": Mapping,
        "__builtins__": {"isinstance": isinstance, "str": str, "list": list},
    }
    exec(compile(module, str(SOURCE), "exec"), namespace)
    return namespace["extract_text_from_turn"], namespace["extract_text_from_messages"]


class FalseyMessageElementsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        turn, messages = load_helpers()
        cls.turn = staticmethod(turn)
        cls.messages = staticmethod(messages)

    def test_zero_and_false_are_preserved(self):
        self.assertEqual(self.messages([0, False]), "0\nFalse")

    def test_mixed_elements_preserve_order(self):
        self.assertEqual(
            self.messages([" first ", 0, None, False, "", " \t", 1, True]),
            "first\n0\nFalse\n1\nTrue",
        )

    def test_null_and_empty_inputs_are_omitted(self):
        for value in (None, [], (), {}, set(), "", [None, "", " \n\t"]):
            with self.subTest(value=value):
                self.assertEqual(self.messages(value), "")

    def test_empty_non_mapping_elements_use_string_form(self):
        self.assertEqual(self.messages([[], (), set(), {}]), "[]\n()\nset()")

    def test_mapping_falsey_fields_are_unchanged(self):
        for value in (0, False, None, ""):
            with self.subTest(value=value):
                item = {"content": value, "text": "shadowed"}
                self.assertEqual(self.messages([item]), "")
                self.assertEqual(self.messages(item), "")
                self.assertEqual(self.turn({"messages": [item]}), "")

    def test_mapping_alias_and_role_precedence_are_unchanged(self):
        self.assertEqual(self.messages([
            {"content": "first", "text": "second", "message": "third",
             "role": "speaker", "speaker": "shadowed"},
            {"text": "second", "message": "third"},
            {"message": "third", "speaker": "fallback"},
        ]), "speaker: first\nsecond\nfallback: third")

    def test_nested_turn_precedence_is_unchanged(self):
        self.assertEqual(self.messages({
            "messages": [{"role": "A", "content": " nested "}, 0, False],
            "content": "shadowed",
        }), "A: nested")
        self.assertEqual(self.messages({"messages": [None], "text": "shadowed"}), "")
        self.assertEqual(self.messages({"messages": [], "text": "fallback"}), "fallback")

    def test_top_level_bare_string_behavior_is_unchanged(self):
        self.assertEqual(self.messages("ab"), "a\nb")
        self.assertEqual(self.turn("ab"), "")


if __name__ == "__main__":
    unittest.main()
