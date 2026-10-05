"""Pure-function regressions; no project imports, runtime, or real datasets.

Run directly with Python or stdlib unittest discovery restricted to this file.
Only the named function definitions are compiled from project sources.
"""
import ast
from collections.abc import Mapping
from dataclasses import asdict, dataclass
import math
from pathlib import Path
from types import SimpleNamespace
from typing import Any
import unittest

ROOT = Path(__file__).resolve().parents[1]


@dataclass
class SyntheticScope:
    """Small scope stand-in for testing case normalization without runtime imports."""
    agent_id: str = "test"

    @classmethod
    def from_dict(cls, value):
        return cls(agent_id=value.get("agent_id", "test"))


def load_functions(relative_path, names, namespace):
    path = ROOT / relative_path
    tree = ast.parse(path.read_text(), filename=str(path))
    selected = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in names]
    if {node.name for node in selected} != set(names):
        raise AssertionError(f"Missing isolated functions in {path}")
    exec(compile(ast.Module(body=selected, type_ignores=[]), str(path), "exec"), namespace)


NS = {"Any": Any, "Mapping": Mapping, "ScopeRef": SyntheticScope, "asdict": asdict, "math": math}
load_functions("eimemory/evaluation/longmemeval.py", ["_strings"], NS)
load_functions("eimemory/evaluation/_text.py", ["extract_text_from_turn"], NS)
load_functions("eimemory/evaluation/locomo.py", [
    "_normalize_case", "_existing_chunks", "_sessions_from_case", "_session_chunks",
    "_turns", "_turn_text", "_expected_ids", "_returned_ids",
], NS)
load_functions("eimemory/evaluation/metrics.py", [
    "_round", "first_relevant_rank", "recall_at_k", "recall_any_at_k", "ndcg_at_k",
], NS)


def normalize(case, index=0):
    return NS["_normalize_case"](case, index=index, default_scope={})


def expected(case, granularity):
    return NS["_expected_ids"](case, granularity=granularity)


def example(**evidence):
    return normalize({"chunks": [
        {"chunk_id": "a", "session_id": "s1", "turn_id": "t1", "turn_ids": ["t1", "t2"], "text": "good"},
        {"chunk_id": "b", "session_id": "s1", "turn_id": "t3", "text": "irrelevant in same session"},
        {"chunk_id": "c", "session_id": "s2", "turn_id": "t4", "text": "irrelevant elsewhere"},
    ], **evidence})


class EvidenceMappingTests(unittest.TestCase):
    def test_converter_shaped_irrelevant_only_retrieval_scores_zero(self):
        case = normalize({
            "case_id": "converted", "question": "synthetic question",
            "haystack_sessions": [{"session_id": "s1", "turns": [
                {"turn_id": "correct", "messages": [{"role": "speaker", "content": "answer"}]},
                {"turn_id": "wrong", "messages": [{"role": "speaker", "content": "unrelated"}]},
            ]}],
            "evidence_session_ids": ["s1"], "evidence_turn_ids": ["correct"],
        })
        wanted = expected(case, "chunk")
        self.assertEqual(wanted, {"converted:s1:0"})
        returned = NS["_returned_ids"]([SimpleNamespace(content=case["chunks"][1])], granularity="chunk")
        self.assertEqual(NS["first_relevant_rank"](returned, wanted), 0)
        for metric in ("recall_at_k", "recall_any_at_k", "ndcg_at_k"):
            self.assertEqual(NS[metric](returned, wanted, k=1), 0.0)

    def test_all_cross_granularity_mappings(self):
        cases = [
            ("turn", ["t2"], "chunk", {"a"}),
            ("turn", ["t2"], "session", {"s1"}),
            ("chunk", ["a"], "turn", {"t1", "t2"}),
            ("chunk", ["a"], "session", {"s1"}),
            ("session", ["s1"], "chunk", {"a", "b"}),
            ("session", ["s1"], "turn", {"t1", "t2", "t3"}),
        ]
        for source, labels, target, wanted in cases:
            with self.subTest(source=source, target=target):
                self.assertEqual(expected(example(**{f"evidence_{source}_ids": labels}), target), wanted)

    def test_explicit_target_ids_preserved_even_if_absent_from_chunks(self):
        for target in ("chunk", "turn", "session"):
            with self.subTest(target=target):
                case = example(evidence_chunk_ids=["unmapped-chunk"], evidence_turn_ids=["unmapped-turn"], evidence_session_ids=["unmapped-session"])
                self.assertEqual(expected(case, target), {f"unmapped-{target}"})

    def test_finer_evidence_does_not_expand_via_session_labels(self):
        self.assertEqual(expected(example(evidence_turn_ids=["t2"], evidence_session_ids=["s1"]), "chunk"), {"a"})
        self.assertEqual(expected(example(evidence_chunk_ids=["a"], evidence_session_ids=["s1"]), "turn"), {"t1", "t2"})

    def test_unmapped_and_partially_mapped_evidence_raise(self):
        for source in ("chunk", "turn", "session"):
            known = {"chunk": "a", "turn": "t1", "session": "s1"}[source]
            for target in set(("chunk", "turn", "session")) - {source}:
                for labels in (["missing"], [known, "missing"]):
                    with self.subTest(source=source, target=target, labels=labels):
                        with self.assertRaisesRegex(ValueError, "cannot map"):
                            expected(example(**{f"evidence_{source}_ids": labels}), target)

    def test_unmapped_precise_evidence_does_not_fall_back_to_session(self):
        with self.assertRaisesRegex(ValueError, "cannot map"):
            expected(example(evidence_turn_ids=["missing"], evidence_session_ids=["s1"]), "chunk")

    def test_missing_target_metadata_raises(self):
        case = normalize({"chunks": [{"chunk_id": "a", "text": "no turn or session"}], "evidence_chunk_ids": ["a"]})
        for target in ("turn", "session"):
            with self.subTest(target=target), self.assertRaisesRegex(ValueError, "cannot map"):
                expected(case, target)

    def test_primary_turn_and_secondary_membership_both_map(self):
        case = example(evidence_turn_ids=["primary"])
        case["chunks"][0]["turn_id"] = "primary"
        self.assertEqual(expected(case, "chunk"), {"a"})
        case["evidence_turn_ids"] = ["t2"]
        self.assertEqual(expected(case, "chunk"), {"a"})
        case["evidence_turn_ids"] = []
        case["evidence_chunk_ids"] = ["a"]
        self.assertEqual(expected(case, "turn"), {"primary", "t1", "t2"})

    def test_one_turn_can_map_to_multiple_chunks(self):
        case = example(evidence_turn_ids=["t2"])
        case["chunks"][1]["turn_ids"] = ["t2", "t3"]
        self.assertEqual(expected(case, "chunk"), {"a", "b"})

    def test_no_evidence_retains_legacy_fallback(self):
        case = example()
        self.assertEqual(expected(case, "chunk"), {"a", "b", "c"})
        self.assertEqual(expected(case, "session"), {"s1", "s2"})
        self.assertEqual(expected(case, "turn"), {"t1", "t3", "t4"})


class ChunkIdentifierTests(unittest.TestCase):
    def test_anonymous_cases_have_distinct_generated_chunk_ids(self):
        a = normalize({"chunks": [{"text": "first"}]}, index=0)
        b = normalize({"chunks": [{"text": "second"}]}, index=1)
        self.assertEqual(a["chunks"][0]["chunk_id"], "locomo-1:chunk:0")
        self.assertEqual(b["chunks"][0]["chunk_id"], "locomo-2:chunk:0")

    def test_explicit_ids_and_alias_precedence(self):
        for identity, prefix in (({"id": "id", "case_id": "case"}, "id"), ({"case_id": "case"}, "case")):
            with self.subTest(identity=identity):
                case = normalize({**identity, "chunks": [{"text": "generated"}, {"chunk_id": "keep", "text": "explicit"}]})
                self.assertEqual(case["case_id"], prefix)
                self.assertEqual([c["chunk_id"] for c in case["chunks"]], [f"{prefix}:chunk:0", "keep"])
                self.assertEqual(normalize(case, index=100), case)


if __name__ == "__main__":
    unittest.main()
