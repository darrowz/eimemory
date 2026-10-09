"""Algorithm-only closure checks; never import the production module or its models.

Run directly with stdlib unittest. EA633_PROJECTOR_SOURCE optionally selects the
pinned old source for the red baseline. These synthetic checks do not certify
production DTO admission, persisted effects, or the cause of recall=0.
"""

import ast
from collections import defaultdict
from collections.abc import Mapping, Sequence
import os
from pathlib import Path
from typing import Any
import unittest


FUNCTION_NAMES = ("_expand_affected_capabilities", "_expand_projection_targets")
SOURCE_PATH = Path(os.environ.get(
    "EA633_PROJECTOR_SOURCE",
    str(Path(__file__).resolve().parents[1] / "eimemory/capabilities/projector.py"),
))


def load_closure_helpers(path):
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    definitions = [node for node in tree.body
                   if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                   and node.name in FUNCTION_NAMES]
    if tuple(node.name for node in definitions) != FUNCTION_NAMES:
        raise AssertionError("Expected exactly the two approved closure helpers")
    for node in definitions:
        if type(node) is not ast.FunctionDef or node.decorator_list:
            raise AssertionError("Only undecorated synchronous helpers may execute")
        if any(isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
               for child in ast.walk(node) if child is not node):
            raise AssertionError("Unexpected nested definition")
    isolated = ast.Module(body=definitions, type_ignores=[])
    namespace = {"defaultdict": defaultdict, "Mapping": Mapping,
                 "Sequence": Sequence, "Any": Any}
    exec(compile(isolated, str(path), "exec"), namespace)
    return tuple(namespace[name] for name in FUNCTION_NAMES)


expand_affected, expand_targets = load_closure_helpers(SOURCE_PATH)


def entry(capability, dependencies=(), flag=True):
    return {"capability_id": capability,
            "requirement": {"require_dependencies": flag},
            "revisions": [{"descriptor": {"contract": {
                "dependencies": dependencies}}}]}


def relation(source, target, kind="depends_on"):
    return {"source_capability_id": source, "target_capability_id": target,
            "relation_type": kind}


def closure(entries, seeds, selected, relations=()):
    initial = expand_affected(seeds, relations, selected)
    return expand_targets({"requirements": entries}, target_ids=initial,
                          relations=relations, selected_capabilities=selected)


class DependencyClosureTests(unittest.TestCase):
    def test_changed_dependency_includes_revision_local_source(self):
        self.assertEqual(closure([entry("A", ["B"]), entry("B")],
                                 ["B"], {"A", "B"}), {"A", "B"})

    def test_forward_dependency_still_expands(self):
        self.assertEqual(closure([entry("A", ["B"]), entry("B")],
                                 ["A"], {"A", "B"}), {"A", "B"})

    def test_reverse_chain_reaches_fixed_point(self):
        self.assertEqual(closure([entry("A", ["B"]), entry("B", ["C"]),
                                  entry("C")], ["C"], {"A", "B", "C"}),
                         {"A", "B", "C"})

    def test_registered_relation_kinds_remain_bidirectional(self):
        for kind in ("depends_on", "composes", "conflicts_with", "supersedes"):
            for seed in ("A", "B"):
                with self.subTest(kind=kind, seed=seed):
                    self.assertEqual(closure([], [seed], {"A", "B"},
                                             [relation("A", "B", kind)]),
                                     {"A", "B"})

    def test_unsupported_registered_relation_ignored(self):
        self.assertEqual(closure([], ["B"], {"A", "B"},
                                 [relation("A", "B", "other")]), {"B"})

    def test_mixed_revision_registered_chain(self):
        self.assertEqual(closure([entry("A", ["B"]), entry("C", ["D"])],
                                 ["D"], {"A", "B", "C", "D"},
                                 [relation("B", "C")]), {"A", "B", "C", "D"})

    def test_only_literal_true_enables_dependency_edges(self):
        for flag in (False, None, 0, 1, "true", [], {}):
            for seed in ("A", "B"):
                with self.subTest(flag=flag, seed=seed):
                    self.assertEqual(closure([entry("A", ["B"], flag)],
                                             [seed], {"A", "B"}), {seed})

    def test_missing_requirement_ignores_dependencies(self):
        value = entry("A", ["B"])
        value.pop("requirement")
        self.assertEqual(closure([value], ["B"], {"A", "B"}), {"B"})

    def test_unselected_dependency_does_not_bridge_selected_sources(self):
        entries = [entry("A", ["X"]), entry("B", ["X"])]
        self.assertEqual(closure(entries, ["A"], {"A", "B"}), {"A"})

    def test_unselected_source_does_not_expand(self):
        self.assertEqual(closure([entry("X", ["A", "B"])],
                                 ["B"], {"A", "B"}), {"B"})

    def test_registered_path_cannot_traverse_unselected_node(self):
        self.assertEqual(closure([], ["A"], {"A", "B"},
                                 [relation("A", "X"), relation("X", "B")]), {"A"})

    def test_cycles_self_edges_and_duplicates_terminate(self):
        entries = [entry("A", ["B", "B", "A"]), entry("B", ["A"])]
        self.assertEqual(closure(entries, ["B"], {"A", "B"}), {"A", "B"})

    def test_empty_seed_does_not_activate_disconnected_dependencies(self):
        self.assertEqual(closure([entry("A", ["B"])], [], {"A", "B"}), set())

    def test_disconnected_selected_component_stays_out(self):
        self.assertEqual(closure([entry("A", ["B"]), entry("C", ["D"])],
                                 ["A"], {"A", "B", "C", "D"}), {"A", "B"})

    def test_affected_input_is_bounded_by_selection(self):
        self.assertEqual(closure([], ["X", "B", "B"], {"A", "B"}), {"B"})

    def test_multiple_revisions_preserve_capability_level_union(self):
        value = entry("A", ["B"])
        value["revisions"].extend(entry("A", ["C"])["revisions"])
        self.assertEqual(closure([value], ["C"], {"A", "B", "C"}), {"A", "B", "C"})

    def test_guarded_malformed_entries_descriptors_and_contracts(self):
        malformed = [None, 17, "bad", {},
                     {"capability_id": "A", "requirement": "bad"},
                     {"capability_id": "A", "requirement": {"require_dependencies": True},
                      "revisions": [None, 17, "bad", {}, {"descriptor": "bad"},
                                    {"descriptor": {"contract": "bad"}}]}]
        self.assertEqual(closure(malformed, ["A"], {"A", "B"}), {"A"})

    def test_falsy_containers_remain_empty(self):
        for value in (None, False, 0, "", [], {}):
            with self.subTest(value=value):
                self.assertEqual(expand_targets({"requirements": value},
                    target_ids={"A"}, relations=[], selected_capabilities={"A"}), {"A"})
                item = entry("A", value)
                self.assertEqual(closure([item], ["A"], {"A", "B"}), {"A"})
                item["revisions"] = value
                self.assertEqual(closure([item], ["A"], {"A", "B"}), {"A"})

    def test_iterable_dependency_container_behavior_unchanged(self):
        for value in ("B", {"B": "ignored"}, ("B",)):
            with self.subTest(value=value):
                self.assertEqual(closure([entry("A", value)], ["A"], {"A", "B"}),
                                 {"A", "B"})

    def test_truthy_noniterable_containers_still_raise(self):
        with self.assertRaises(TypeError):
            expand_targets({"requirements": 7}, target_ids={"A"},
                           relations=[], selected_capabilities={"A"})
        value = entry("A")
        value["revisions"] = 7
        with self.assertRaises(TypeError):
            closure([value], ["A"], {"A"})
        with self.assertRaises(TypeError):
            closure([entry("A", 7)], ["A"], {"A"})

    def test_malformed_registered_relation_still_raises(self):
        with self.assertRaises(AttributeError):
            closure([], ["A"], {"A"}, [None])


if __name__ == "__main__":
    unittest.main(verbosity=2)
