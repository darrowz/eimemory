"""Pure post-JSON parser regressions: no project, model, or store imports/calls."""
import ast
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any
import unittest


SOURCE = Path(__file__).resolve().parents[1] / "eimemory/knowledge/l1_conflict.py"


def load_parser():
    tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
    names = {"L1ConflictJudgeUnavailable", "_normalize_conflict_action", "ConflictDecision"}
    selected = [node for node in tree.body if getattr(node, "name", "") in names]
    adjudicate = next(node for node in tree.body if getattr(node, "name", "") == "adjudicate_l1_atoms")
    start = next(i for i, node in enumerate(adjudicate.body)
                 if isinstance(node, ast.If) and ast.unparse(node.test) == "isinstance(payload, dict)")
    parser = ast.parse("def parse(payload, atoms, strict, allowed_targets, matches):\n    pass\n").body[0]
    parser.body = adjudicate.body[start:]
    module = ast.fix_missing_locations(ast.Module(body=selected + [parser], type_ignores=[]))
    namespace = {"__name__": __name__, "dataclass": dataclass, "Any": Any,
                 "L1Atom": SimpleNamespace, "L1_ATOM_TYPES": {"fact", "persona", "instruction", "episodic"}}
    exec(compile(module, str(SOURCE), "exec"), namespace)
    return namespace["parse"], namespace["L1ConflictJudgeUnavailable"]


class ConflictOutputShapesTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        parser, error = load_parser()
        cls.parse, cls.error = staticmethod(parser), error

    def setUp(self):
        self.atoms = [SimpleNamespace(text="Original synthetic fact.", memory_type="fact")]

    def run_items(self, items, strict=True, targets=None):
        return self.parse(items, self.atoms, strict,
                          {f"new-{i}": set(targets or ["old-1"]) for i in range(len(self.atoms))},
                          [(atom, [object()]) for atom in self.atoms])

    def item(self, action="update", **fields):
        return {"record_id": "new-0", "action": action, "target_ids": ["old-1"], **fields}

    def test_malformed_target_shapes_use_failure_contract(self):
        for value in [7, True, 3.5, 0, False, {}, {"old-1": 1}]:
            for action in ["store", "skip", "update", "merge"]:
                with self.subTest(value=value, action=action):
                    item = self.item(action, target_ids=value)
                    with self.assertRaisesRegex(self.error, "l1_conflict_invalid_target_ids"):
                        self.run_items([item])
                    result = self.run_items([item], strict=False)
                    self.assertEqual(result[0].action, "store")
                    self.assertEqual(result[0].target_ids, ())
                    self.assertEqual(result[0].merged_content, "")

    def test_nontext_content_uses_failure_contract(self):
        for value in [{}, {"owner": "Alice"}, [], ["text"], 12.5, 0, True, False]:
            for action in ["store", "skip", "update", "merge"]:
                with self.subTest(value=value, action=action):
                    item = self.item(action, merged_content=value)
                    with self.assertRaisesRegex(self.error, "l1_conflict_invalid_merged_content"):
                        self.run_items([item])
                    result = self.run_items([item], strict=False)
                    self.assertEqual(result[0].action, "store")
                    self.assertEqual(result[0].merged_content, "")

    def test_supported_actions_and_text_remain_unchanged(self):
        for strict in [False, True]:
            for action in ["store", "skip", "update", "merge", " UPDATE "]:
                result = self.run_items([self.item(action, merged_content="  New fact.  ", merged_type=" PERSONA ")], strict)
                self.assertEqual(result[0].action, "update" if action.strip().lower() in {"update", "merge"} else action)
                self.assertEqual(result[0].target_ids, ("old-1",))
                self.assertEqual(result[0].merged_content, "New fact.")
                self.assertEqual(result[0].merged_type, "persona")

    def test_missing_null_empty_and_whitespace_content_remain_unchanged(self):
        for strict in [False, True]:
            for fields in [{}, {"merged_content": None}, {"merged_content": ""}]:
                self.assertEqual(self.run_items([self.item(**fields)], strict)[0].merged_content, self.atoms[0].text)
            self.assertEqual(self.run_items([self.item(merged_content="   ")], strict)[0].merged_content, "")

    def test_null_empty_targets_and_existing_string_iteration(self):
        for strict in [False, True]:
            for targets in [None, [], ""]:
                self.assertEqual(self.run_items([self.item("store", target_ids=targets)], strict)[0].target_ids, ())
            self.assertEqual(self.run_items([self.item(target_ids="ab")], strict, targets=["a", "b"])[0].target_ids, ("a", "b"))
            self.assertEqual(self.run_items([self.item(target_ids=[7, " ", "old-1"])], strict, targets=["7", "old-1"])[0].target_ids, ("7", "old-1"))

    def test_lenient_malformed_batch_discards_prior_decisions(self):
        self.atoms.append(SimpleNamespace(text="Second synthetic fact.", memory_type="fact"))
        items = [self.item("skip"), {"record_id": "new-1", "action": "update", "target_ids": True}]
        result = self.run_items(items, strict=False)
        self.assertEqual([decision.action for decision in result], ["store", "store"])
        self.assertEqual([decision.atom for decision in result], self.atoms)

    def test_unknown_ids_and_first_decision_semantics_remain_unchanged(self):
        items = [{"record_id": "unrelated", "action": "update", "target_ids": True},
                 self.item("skip"), self.item("update", target_ids=True)]
        for strict in [False, True]:
            self.assertEqual(self.run_items(items, strict)[0].action, "skip")


if __name__ == "__main__":
    unittest.main()
