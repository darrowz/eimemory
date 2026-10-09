"""Offline selected live report helpers; no memory API/store imports."""
from pathlib import Path
from types import MethodType, SimpleNamespace
import re
import textwrap
import unittest


def merger():
    path = Path(__file__).parents[1] / "eimemory/api/memory.py"
    lines = path.read_text().splitlines(keepends=True)
    names = {"_merge_search_reports", "_scored_entry_key"}
    chunks = []
    for index, line in enumerate(lines):
        match = re.match(r"( *)def (\w+)\(", line)
        if not match or match[2] not in names:
            continue
        indent = len(match[1])
        end = index + 1
        while end < len(lines):
            current = lines[end]
            if (re.match(r" *(?:def |class |@)", current)
                    and len(current) - len(current.lstrip()) <= indent):
                break
            end += 1
        chunks.append(textwrap.dedent("".join(lines[index:end])))
    namespace = {}
    exec(compile("from __future__ import annotations\n" + "\n".join(chunks),
                 str(path), "exec"), namespace)
    assert names <= namespace.keys()
    fixture = SimpleNamespace(_scored_entry_key=namespace["_scored_entry_key"])
    return MethodType(namespace["_merge_search_reports"], fixture)


def entry(source="one", agent="a", score=1.0):
    return {"record_id": "same-id", "source_id": source,
            "scope": {"tenant_id": "default", "agent_id": agent,
                      "workspace_id": "w", "user_id": "u"}, "score": score}


class ReportScoreKeyTest(unittest.TestCase):
    def test_same_id_different_sources_keep_both_scores(self):
        first, second = entry(), entry(source="two", score=2.0)
        result = merger()([{"scored_items": [first]}, {"scored_items": [second]}])
        self.assertEqual(result["scored_items"], [first, second])

    def test_same_id_different_scopes_keep_both_scores(self):
        first, second = entry(), entry(agent="b", score=2.0)
        result = merger()([{"scored_items": [first, second]}])
        self.assertEqual(result["scored_items"], [first, second])

    def test_same_exact_ref_preserves_first_score(self):
        first, second = entry(), entry(score=2.0)
        self.assertEqual(merger()([{"scored_items": [first, second]}])["scored_items"], [first])

    def test_legacy_unqualified_scores_preserve_first_id_behavior(self):
        first = {"record_id": "same-id", "score": 1.0}
        second = {"record_id": "same-id", "score": 2.0}
        self.assertEqual(merger()([{"scored_items": [first, second]}])["scored_items"], [first])

    def test_aggregates_and_invalid_entry_handling_are_unchanged(self):
        no_id = {"score": 1.0}
        result = merger()([None, {"retrieval_mode": "vector", "vector_hits": 2,
                                 "blocked_counts": {"quality": 1}, "scored_items": [no_id, "invalid"]},
                           {"vector_hits": 3, "recall_filters": {"blocked_counts": {"quality": 2}},
                            "scored_items": [no_id]}])
        self.assertEqual(result["scored_items"], [no_id, no_id])
        self.assertEqual(result["vector_hits"], 5)
        self.assertEqual(result["blocked_counts"], {"quality": 3})
        self.assertEqual(result["retrieval_mode"], "vector")


if __name__ == "__main__":
    unittest.main()
