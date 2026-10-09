"""Offline selected live helpers; no runtime, project import or storage."""
from pathlib import Path
import re
import unittest


def entry_function():
    path = Path(__file__).parents[1] / "eimemory/judgment.py"
    lines = path.read_text().splitlines(keepends=True)
    names = {"_entry_from_good_items", "_trigger_for_events", "_unique_nonempty", "_clamp"}
    chunks = []
    for index, line in enumerate(lines):
        match = re.match(r"def (\w+)\(", line)
        if not match or match[1] not in names:
            continue
        end = index + 1
        while end < len(lines) and not lines[end].startswith("def "):
            end += 1
        chunks.append("".join(lines[index:end]))
    # Inert numeric collaborator: all fixture confidences are finite floats.
    namespace = {"_clamp_float": lambda value, default=0.0: float(value or default)}
    exec(compile("from __future__ import annotations\n" + "\n".join(chunks),
                 str(path), "exec"), namespace)
    assert names <= namespace.keys()
    return namespace["_entry_from_good_items"]


def pair(identifier, confidence, verification, *, event_verification=""):
    return {"event": {"id": identifier, "confidence": confidence,
                      "action_path": ["plan-" + identifier],
                      "verification": event_verification},
            "outcome": {"outcome": "good", "verification": verification,
                        "reason": "reason-" + identifier}}


class BestPairTest(unittest.TestCase):
    def test_later_best_policy_uses_its_own_outcome(self):
        result = entry_function()("task", [pair("a", .2, "verify-a"), pair("b", .9, "verify-b")])
        self.assertEqual(result["policy"], "plan-b")
        self.assertEqual(result["success_criteria"], "verify-b")
        self.assertEqual(result["source_event_ids"], ["a", "b"])
        self.assertEqual(result["evidence"], ["reason-a", "reason-b"])

    def test_pair_order_does_not_change_best_policy_verification(self):
        first = pair("a", .2, "verify-a")
        best = pair("b", .9, "verify-b")
        forward = entry_function()("task", [first, best])
        reverse = entry_function()("task", [best, first])
        self.assertEqual(forward["success_criteria"], reverse["success_criteria"])
        self.assertEqual(forward["policy"], reverse["policy"])

    def test_best_event_verification_still_precedes_its_outcome(self):
        result = entry_function()("task", [pair("a", .2, "verify-a"),
                                          pair("b", .9, "verify-b", event_verification="event-b")])
        self.assertEqual(result["success_criteria"], "event-b")

    def test_tied_confidence_preserves_first_pair(self):
        result = entry_function()("task", [pair("a", .9, "verify-a"), pair("b", .9, "verify-b")])
        self.assertEqual(result["policy"], "plan-a")
        self.assertEqual(result["success_criteria"], "verify-a")

    def test_single_pair_is_unchanged(self):
        result = entry_function()("task", [pair("a", .9, "verify-a")])
        self.assertEqual(result["policy"], "plan-a")
        self.assertEqual(result["success_criteria"], "verify-a")


if __name__ == "__main__":
    unittest.main()
