"""Stdlib-only isolated helper tests with fixed synthetic inputs."""
import ast
from collections import defaultdict, namedtuple, OrderedDict
import dataclasses
import json
import os
from pathlib import Path
from typing import Any
import unittest


def load_json_safe():
    default = Path(__file__).resolve().parents[1] / "eimemory/intake/loop.py"
    source = Path(os.environ.get("EA304_SOURCE", str(default)))
    lines = source.read_text(encoding="utf-8").splitlines(keepends=True)
    start, = [i for i, line in enumerate(lines)
              if line == "def _json_safe(value: Any) -> Any:\n"]
    end = start + 1
    while end < len(lines) and (not lines[end].strip() or lines[end].startswith(" ")):
        end += 1
    tree = ast.parse("".join(lines[start:end]).rstrip() + "\n")
    if len(tree.body) != 1 or not isinstance(tree.body[0], ast.FunctionDef):
        raise AssertionError("Expected one allowlisted helper")
    if tree.body[0].name != "_json_safe" or tree.body[0].decorator_list:
        raise AssertionError("Unexpected function or decorator")
    namespace = {"Any": Any, "asdict": dataclasses.asdict}
    exec(compile(tree, str(source), "exec"), namespace)
    return namespace["_json_safe"]


@dataclasses.dataclass
class Record:
    value: Any


class StringKey(str):
    pass


class PlainList(list):
    pass


class PlainDict(dict):
    pass


class RaisingGetitemDict(dict):
    def __getitem__(self, key):
        raise RuntimeError("synthetic lookup failure")


class DifferentGetitemDict(dict):
    def __getitem__(self, key):
        return b"synthetic different lookup"


class PlainTuple(tuple):
    pass


Pair = namedtuple("Pair", "left right")


class BadString:
    def __str__(self):
        raise TypeError("synthetic leaf string failure")

    def __repr__(self):
        return "BadString()"


class BadCopy:
    def __deepcopy__(self, memo):
        raise TypeError("synthetic asdict copy failure")

    def __repr__(self):
        return "BadCopy()"


def dump(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def old_dataclass_branch(value):
    try:
        return dataclasses.asdict(value)
    except TypeError:
        return str(value)


def old_success_cases():
    return [
        (1, "two", None),
        {"rows": [(1, [2, {"x": 3}])]},
        {"rows": [[1, {"x": 2}]]},
        {2: "two", 10: "ten"},
        {None: "none"},
        {True: "true"},
        {StringKey("b"): 2, StringKey("a"): 1},
        PlainTuple((1, "two")),
        PlainList([1, {"x": (2, 3)}]),
        PlainDict({"a": [1, 2]}),
        Pair(1, {"x": (2, 3)}),
        OrderedDict([("b", 2), ("a", 1)]),
        defaultdict(list, {"a": [1, 2]}),
        [], {}, (), None, True, 3, 2.5, "文本",
    ]


class IntakeJsonDataclassTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.normalize = staticmethod(load_json_safe())

    def test_old_successful_json_outputs_preserved(self):
        for item in old_success_cases():
            with self.subTest(item=repr(item)):
                value = Record(item)
                self.assertEqual(dump(self.normalize(value)), dump(old_dataclass_branch(value)))

    def test_original_dataclass_container_types_preserved(self):
        for item in old_success_cases():
            with self.subTest(item=repr(item)):
                expected = old_dataclass_branch(Record(item))
                actual = self.normalize(Record(item))
                if isinstance(expected, dict):
                    expected = expected["value"]
                    actual = actual["value"]
                self.assertIs(type(actual), type(expected))
                self.assertEqual(actual, expected)

    def test_items_with_raising_getitem_preserved(self):
        value = Record(RaisingGetitemDict({"answer": [1, 2]}))
        self.assertEqual(dump(self.normalize(value)), dump(old_dataclass_branch(value)))

    def test_items_with_different_getitem_preserved(self):
        value = Record(DifferentGetitemDict({"answer": [1, 2]}))
        self.assertEqual(dump(self.normalize(value)), dump(old_dataclass_branch(value)))

    def test_dataclass_key_types_preserved(self):
        for key in [None, True, 2, StringKey("name")]:
            with self.subTest(key=repr(key)):
                actual = self.normalize(Record({key: "fixed"}))["value"]
                actual_key, = actual
                self.assertIs(type(actual_key), type(key))
                self.assertEqual(actual_key, key)

    def test_mixed_key_sort_error_remains(self):
        with self.assertRaises(TypeError):
            dump(self.normalize(Record({1: "one", "two": "two"})))

    def test_path_and_bytes_leaf_conversion(self):
        value = Record({"path": Path("synthetic/leaf"), "payload": b"fixed"})
        expected = {"value": {"path": str(value.value["path"]), "payload": "b'fixed'"}}
        self.assertEqual(self.normalize(value), expected)
        self.assertEqual(json.loads(dump(self.normalize(value))), expected)

    def test_nested_tuple_non_json_leaves(self):
        value = Record((Path("synthetic/leaf"), [b"fixed", {"x": (b"nested",)}]))
        expected = {"value": (str(value.value[0]), ["b'fixed'", {"x": ("b'nested'",)}])}
        self.assertEqual(self.normalize(value), expected)
        dump(self.normalize(value))

    def test_namedtuple_non_json_leaves(self):
        actual = self.normalize(Record(Pair(Path("synthetic/leaf"), b"fixed")))["value"]
        self.assertIs(type(actual), Pair)
        self.assertEqual(actual, Pair(str(Path("synthetic/leaf")), "b'fixed'"))

    def test_tuple_subclass_non_json_leaves(self):
        actual = self.normalize(Record(PlainTuple((b"fixed",))))["value"]
        self.assertIs(type(actual), PlainTuple)
        self.assertEqual(actual, ("b'fixed'",))

    def test_direct_tuple_policy_unchanged(self):
        value = (1, "two", None)
        self.assertEqual(self.normalize(value), str(value))

    def test_direct_dictionary_policy_unchanged(self):
        self.assertEqual(self.normalize({None: b"fixed", 2: [True]}),
                         {"None": "b'fixed'", "2": [True]})

    def test_asdict_typeerror_fallback_unchanged(self):
        self.assertEqual(self.normalize(Record(BadCopy())), "Record(value=BadCopy())")

    def test_recursive_leaf_typeerror_propagates(self):
        with self.assertRaisesRegex(TypeError, "synthetic leaf string failure"):
            self.normalize(Record(BadString()))

    def test_direct_dictionary_leaf_typeerror_propagates(self):
        with self.assertRaisesRegex(TypeError, "synthetic leaf string failure"):
            self.normalize({"value": BadString()})

    def test_no_input_mutation(self):
        value = Record({"rows": [(Path("synthetic/leaf"), [b"fixed"])]})
        before = dataclasses.asdict(value)
        self.normalize(value)
        self.assertEqual(dataclasses.asdict(value), before)


if __name__ == "__main__":
    unittest.main()
