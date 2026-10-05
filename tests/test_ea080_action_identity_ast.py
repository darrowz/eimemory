"""EA-080-B1: pure action-key compatibility checks, without project imports."""

import __future__
import ast
import itertools
from pathlib import Path
import unittest


def _load_action_identity():
    path = Path(__file__).resolve().parents[1] / "eimemory/storage/replay_buffer.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    selected = [
        node for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "action_identity"
    ]
    if len(selected) != 1:
        raise AssertionError("Expected exactly one action_identity helper")
    if any(isinstance(node, (ast.Import, ast.ImportFrom)) for node in ast.walk(selected[0])):
        raise AssertionError("The extracted helper must not import project code")
    namespace = {"__builtins__": {"dict": dict, "str": str}}
    exec(
        compile(
            ast.Module(body=selected, type_ignores=[]), str(path), "exec",
            flags=__future__.annotations.compiler_flag,
        ),
        namespace,
    )
    return namespace["action_identity"]


def _legacy_key(action):
    """Pre-repair normalization and formatting, retained as an inert oracle."""
    payload = dict(action or {})
    action_type = str(payload.get("type") or "action").strip() or "action"
    action_id = str(payload.get("id") or payload.get("name") or payload.get("action") or action_type).strip()
    return f"{action_type}:{action_id}"


class ActionIdentityCompatibilityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.identity = staticmethod(_load_action_identity())

    def decode_key(self, key):
        self.assertTrue(key.startswith("@action-v2@"))
        self.assertNotIn(":", key)
        components = key[len("@action-v2@"):].split(".")
        self.assertEqual(len(components), 2)
        return tuple(bytes.fromhex(part).decode("utf-8", "surrogatepass") for part in components)

    def test_original_collision_is_separated_with_exact_format(self):
        first = {"type": "a:b", "id": "c"}
        second = {"type": "a", "id": "b:c"}
        self.assertEqual(_legacy_key(first), _legacy_key(second))
        self.assertEqual(_legacy_key(first), "a:b:c")
        self.assertEqual(self.identity(first), "@action-v2@613a62.63")
        self.assertEqual(self.identity(second), "@action-v2@61.623a63")
        self.assertNotEqual(self.identity(first), self.identity(second))

    def test_colon_boundary_placements_remain_distinct(self):
        pairs = (("a::b", "c"), ("a:", "b:c"), ("a", ":b:c"), (":", ":"), ("::", " "))
        keys = set()
        for kind, ident in pairs:
            with self.subTest(kind=kind, ident=ident):
                key = self.identity({"type": kind, "id": ident})
                self.assertEqual(self.decode_key(key), (kind, ident.strip()))
                keys.add(key)
        self.assertEqual(len(keys), len(pairs))

    def test_colon_free_keys_preserve_all_legacy_characters(self):
        components = ("tool", "%3A", "\\u003a", "@action-v2@61.623a63", ".", "\"", "\x00", "中", "\U0001f600", "\ud83d\ude00", "\ud800")
        for kind, ident in itertools.product(components, repeat=2):
            with self.subTest(kind=kind, ident=ident):
                action = {"type": kind, "id": ident}
                self.assertEqual(self.identity(action), _legacy_key(action))
                self.assertEqual(self.identity(action), f"{kind}:{ident}")

    def test_normalization_and_fallbacks_are_unchanged(self):
        fixtures = (
            (None, ("action", "action")),
            ({}, ("action", "action")),
            ({"type": " tool ", "id": " x "}, ("tool", "x")),
            ({"type": "tool", "id": 0, "name": "named"}, ("tool", "named")),
            ({"type": "tool", "id": 0}, ("tool", "tool")),
            ({"type": "tool", "id": " ", "name": "usable"}, ("tool", "")),
            ({"type": " ", "id": ":"}, ("action", ":")),
            ({"type": "a:b", "id": ""}, ("a:b", "a:b")),
            ({"type": 0, "id": 1}, ("action", "1")),
            ({"name": "name:value"}, ("action", "name:value")),
            ({"name": "", "action": "call:task"}, ("action", "call:task")),
            ({"type": " a:b ", "id": " c "}, ("a:b", "c")),
            ({"type": False, "id": False}, ("action", "action")),
            ({"type": "tool", "id": [], "name": "n"}, ("tool", "n")),
        )
        for action, expected in fixtures:
            with self.subTest(action=action):
                self.assertEqual(_legacy_key(action), ":".join(expected))
                key = self.identity(action)
                if any(":" in part for part in expected):
                    self.assertEqual(self.decode_key(key), expected)
                else:
                    self.assertEqual(key, _legacy_key(action))

    def test_empty_normalized_id_works_in_both_branches(self):
        self.assertEqual(self.identity({"type": "tool", "id": " "}), "tool:")
        key = self.identity({"type": "a:b", "id": " ", "name": "unused"})
        self.assertEqual(key, "@action-v2@613a62.")
        self.assertEqual(self.decode_key(key), ("a:b", ""))

    def test_scalar_and_surrogate_pair_remain_distinct(self):
        scalar = {"type": "a:", "id": "\U0001f600"}
        surrogates = {"type": "a:", "id": "\ud83d\ude00"}
        self.assertNotEqual(self.identity(scalar), self.identity(surrogates))
        self.assertEqual(self.decode_key(self.identity(scalar)), ("a:", "\U0001f600"))
        self.assertEqual(self.decode_key(self.identity(surrogates)), ("a:", "\ud83d\ude00"))

    def test_exhaustive_bounded_corpus_is_injective_and_disjoint(self):
        strings = sorted(
            {"".join(chars) for size in (1, 2, 3) for chars in itertools.product(("a", ":", "%", "\\", "\""), repeat=size)}
            | {"a:b", "b:c", "action", "tool", "%3A", "\\u003a", "\ud800", "\U0001f600", "\ud83d\ude00", "\ud83d", "\ude00", "\x00", "中", "@action-v2@", "@", "[", "]"},
            key=repr,
        )
        seen = set()
        legacy_keys = set()
        encoded_keys = set()
        ordinary_count = 0
        for kind, ident in itertools.product(strings, repeat=2):
            action = {"type": kind, "id": ident}
            key = self.identity(action)
            self.assertNotIn(key, seen, (kind, ident))
            seen.add(key)
            legacy_keys.add(_legacy_key(action))
            if ":" in kind or ":" in ident:
                self.assertEqual(self.decode_key(key), (kind, ident))
                encoded_keys.add(key)
            else:
                ordinary_count += 1
                self.assertEqual(key, _legacy_key(action))
        self.assertEqual(len(strings), 172)
        self.assertEqual(len(seen), 29584)
        self.assertEqual(ordinary_count, 9801)
        self.assertEqual(len(encoded_keys), 19783)
        self.assertTrue(encoded_keys.isdisjoint(legacy_keys))

    def test_old_ambiguous_entries_are_not_reused_or_modified(self):
        history = {"a:b:c": "unassigned historical aggregate", "tool:tool": "ordinary value"}
        before = dict(history)
        first_key = self.identity({"type": "a:b", "id": "c"})
        second_key = self.identity({"type": "a", "id": "b:c"})
        self.assertNotIn(first_key, history)
        self.assertNotIn(second_key, history)
        self.assertEqual(history[self.identity({"type": "tool", "id": "tool"})], "ordinary value")
        self.assertEqual(history, before)

    def test_input_mapping_is_not_modified(self):
        for action in ({"type": " a:b ", "id": " c "}, {"type": "tool", "id": " ", "name": "unchanged"}):
            before = dict(action)
            self.identity(action)
            self.assertEqual(action, before)

    def test_repeated_calls_are_deterministic(self):
        for action in ({"type": "a:b", "id": "c"}, {"type": "a", "id": "b:c"}, {"type": "tool", "id": "x"}):
            expected = self.identity(action)
            self.assertEqual(self.identity(dict(action)), expected)
            self.assertEqual(self.identity(dict(reversed(list(action.items())))), expected)


if __name__ == "__main__":
    unittest.main()
