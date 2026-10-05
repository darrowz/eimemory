"""Exercise ordinary persona numeric helpers without importing project modules.

Only the four pre-inspected, module-level helper definitions are compiled.
Field metadata is inert stdlib data; no persona model or runtime is constructed.
Run directly with Python, optionally passing --source to check a source snapshot.
"""

from __future__ import annotations

import argparse
import ast
import builtins
import math
from pathlib import Path
from types import SimpleNamespace
from typing import Any
import unittest


SOURCE = Path(__file__).resolve().parents[1] / "eimemory/persona/schema.py"
HELPERS = {"_coerce_int", "_coerce_float", "_coerce_numeric_fields", "_known_fields"}


def _load_helpers(source: Path) -> dict[str, Any]:
    tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
    selected = [
        node for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name in HELPERS
    ]
    if len(selected) != len(HELPERS) or {node.name for node in selected} != HELPERS:
        raise AssertionError("Expected exactly the four ordinary helper definitions")
    for node in selected:
        if node.decorator_list or any(
            isinstance(child, (ast.Import, ast.ImportFrom, ast.ClassDef))
            for child in ast.walk(node)
        ):
            raise AssertionError("Unexpected executable dependency in isolated helper")
    allowed = (
        "int", "float", "str", "dict", "set", "getattr", "isinstance", "type",
        "TypeError", "ValueError", "OverflowError",
    )
    namespace = {
        "__builtins__": {name: getattr(builtins, name) for name in allowed},
        "Any": Any,
    }
    exec(compile(ast.Module(body=selected, type_ignores=[]), str(source), "exec"), namespace)
    return namespace


class PersonaNumericCoercionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.helpers = _load_helpers(SOURCE)

    def test_int_overflow_uses_requested_default(self) -> None:
        for value in (float("inf"), float("-inf")):
            with self.subTest(value=value):
                self.assertEqual(self.helpers["_coerce_int"](value, default=1), 1)

    def test_float_overflow_uses_requested_default(self) -> None:
        for value in (10**400, -(10**400)):
            with self.subTest(sign=1 if value > 0 else -1):
                self.assertEqual(self.helpers["_coerce_float"](value, default=0.25), 0.25)

    def test_numeric_int_field_overflow_uses_zero(self) -> None:
        shape = SimpleNamespace(
            __annotations__={"count": "int"}, __dataclass_fields__={"count": None}
        )
        for value in (float("inf"), float("-inf")):
            with self.subTest(value=value):
                self.assertEqual(
                    self.helpers["_coerce_numeric_fields"](shape, {"count": value}),
                    {"count": 0},
                )

    def test_numeric_float_field_overflow_uses_zero(self) -> None:
        shape = SimpleNamespace(
            __annotations__={"weight": "float"}, __dataclass_fields__={"weight": None}
        )
        for value in (10**400, -(10**400)):
            with self.subTest(sign=1 if value > 0 else -1):
                self.assertEqual(
                    self.helpers["_coerce_numeric_fields"](shape, {"weight": value}),
                    {"weight": 0.0},
                )

    def test_finite_int_conversions_are_unchanged(self) -> None:
        for value, expected in (("12", 12), (12.9, 12), (-4, -4), (True, 1), (10**400, 10**400)):
            with self.subTest(value=value):
                self.assertEqual(self.helpers["_coerce_int"](value, default=7), expected)

    def test_finite_float_conversions_are_unchanged(self) -> None:
        for value, expected in (("0.8", 0.8), (2, 2.0), (-4, -4.0), (False, 0.0)):
            with self.subTest(value=value):
                self.assertEqual(self.helpers["_coerce_float"](value, default=0.25), expected)
        self.assertEqual(math.copysign(1, self.helpers["_coerce_float"](-0.0)), -1)

    def test_existing_malformed_input_fallbacks_are_unchanged(self) -> None:
        for value in (None, "bad", "", [], {}):
            with self.subTest(value=value):
                self.assertEqual(self.helpers["_coerce_int"](value, default=7), 7)
                self.assertEqual(self.helpers["_coerce_float"](value, default=0.25), 0.25)
        self.assertEqual(self.helpers["_coerce_int"](float("nan"), default=7), 7)

    def test_successfully_converted_nonfinite_floats_are_preserved(self) -> None:
        for value in (float("inf"), float("-inf"), "inf", "-inf", "1e999", "-1e999"):
            with self.subTest(value=value):
                self.assertEqual(self.helpers["_coerce_float"](value, default=0.25), float(value))
        for value in (float("nan"), "nan"):
            with self.subTest(value=value):
                self.assertTrue(math.isnan(self.helpers["_coerce_float"](value, default=0.25)))

    def test_default_conversion_and_invalid_default_errors_are_unchanged(self) -> None:
        self.assertEqual(self.helpers["_coerce_int"]("bad", default="2"), 2)
        self.assertEqual(self.helpers["_coerce_float"]("bad", default="0.5"), 0.5)
        for name in ("_coerce_int", "_coerce_float"):
            with self.subTest(name=name):
                with self.assertRaises(ValueError):
                    self.helpers[name](None, default="bad")
                with self.assertRaises(TypeError):
                    self.helpers[name](None, default=None)
        with self.assertRaises(OverflowError):
            self.helpers["_coerce_int"](None, default=float("inf"))
        with self.assertRaises(OverflowError):
            self.helpers["_coerce_float"](None, default=10**400)

    def test_numeric_field_coercion_filtering_and_input_preservation(self) -> None:
        shape = SimpleNamespace(
            __annotations__={"count": "int", "weight": "float", "label": "str"},
            __dataclass_fields__={"count": None, "weight": None, "label": None},
        )
        payload = {"count": "4", "weight": "0.5", "label": ["kept"], "extra": "drop"}
        result = self.helpers["_coerce_numeric_fields"](shape, payload)
        self.assertEqual(result, {"count": 4, "weight": 0.5, "label": ["kept"]})
        self.assertIs(result["label"], payload["label"])
        self.assertEqual(payload, {"count": "4", "weight": "0.5", "label": ["kept"], "extra": "drop"})
        self.assertEqual(
            self.helpers["_coerce_numeric_fields"](shape, {"count": "bad", "weight": None}),
            {"count": 0, "weight": 0.0},
        )

    def test_known_field_filtering_is_unchanged(self) -> None:
        shape = SimpleNamespace(__dataclass_fields__={"count": None})
        self.assertEqual(self.helpers["_known_fields"](shape, {"count": 3, "extra": 4}), {"count": 3})
        for value in (None, [], [("count", 3)], "count", 0):
            with self.subTest(value=value):
                self.assertEqual(self.helpers["_known_fields"](shape, value), {})


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=SOURCE)
    args = parser.parse_args()
    SOURCE = args.source
    unittest.main(argv=[__file__], verbosity=2)
