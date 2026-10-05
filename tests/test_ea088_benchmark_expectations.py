"""EA-088: isolated benchmark assembly with synthetic records and API doubles.

Only AST-selected functions execute. No project imports, stores, providers,
network, incident persistence, or downstream replay engines are exercised.
"""

import ast
from dataclasses import asdict, dataclass
import math
from pathlib import Path
from types import SimpleNamespace
from typing import Any
import unittest


@dataclass
class FakeScope:
    tenant_id: str = "synthetic"

    @classmethod
    def from_dict(cls, value):
        return cls(**value)


def load_helpers(benchmark_path=None):
    root = Path(__file__).resolve().parents[1]
    namespace = {"Any": Any, "math": math, "asdict": asdict,
                 "ScopeRef": FakeScope, "MemoryAPI": Any}
    selections = (
        (root / "eimemory/evaluation/contracts.py", {
            "normalize_memory_eval_suite", "_normalize_case", "_int_value", "_clamp_float",
        }, {"SUPPORTED_PHASES"}),
        (benchmark_path or root / "eimemory/evaluation/benchmarks.py", {
            "_run_case", "_run_extraction_case", "_run_update_case", "_run_recall_case",
            "_matching_ranks", "_record_matches_expected", "_collect_record_texts",
            "_text_contains_any", "_normalize_terms", "_merge_terms", "_limit_value",
            "_rank_metrics", "_invalid_case", "_emit_eval_incident",
        }, set()),
    )
    for path, functions, constants in selections:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        nodes = [node for node in tree.body if (
            isinstance(node, ast.FunctionDef) and node.name in functions
        ) or (
            isinstance(node, ast.Assign)
            and any(isinstance(target, ast.Name) and target.id in constants
                    for target in node.targets)
        )]
        assert {node.name for node in nodes if isinstance(node, ast.FunctionDef)} == functions
        exec(compile(ast.Module(body=nodes, type_ignores=[]), str(path), "exec"), namespace)
    return namespace


def record(**changes):
    fields = {
        "record_id": "a", "title": "Destination", "kind": "memory",
        "summary": "Oldtown", "detail": "", "content": {},
        "meta": {"memory_type": "fact"}, "status": "active",
    }
    fields.update(changes)
    return SimpleNamespace(**fields)


class FakeMemory:
    def __init__(self, items=(), extracted=None):
        self.items = list(items)
        self.extracted = extracted
        self.ingest_calls = []

    def recall(self, **kwargs):
        return SimpleNamespace(items=self.items)

    def ingest(self, **kwargs):
        self.ingest_calls.append(kwargs)
        if self.extracted is not None:
            return self.extracted
        return record(
            title=kwargs["title"], summary=kwargs["text"],
            meta={"memory_type": kwargs["memory_type"]},
            status="active" if kwargs["force_capture"] else "rejected",
        )


class BenchmarkExpectationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.helpers = load_helpers()

    def run_case(self, case, memory):
        normalized = self.helpers["normalize_memory_eval_suite"]([case])["cases"][0]
        return self.helpers["_run_case"](
            SimpleNamespace(memory=memory), memory, normalized,
            index=0, default_scope=FakeScope(),
        )

    def replay_row(self, sample):
        captured = []

        def observe(**kwargs):
            captured.append(kwargs)
            return SimpleNamespace(record_id="synthetic-incident")

        result = self.helpers["_emit_eval_incident"](
            SimpleNamespace(evolution=SimpleNamespace(observe=observe)), sample,
            {"name": "synthetic-ci", "scope": asdict(FakeScope())},
        )
        self.assertEqual(result.record_id, "synthetic-incident")
        self.assertEqual(len(captured), 1)
        return captured[0]["payload"]["suggested_replay_dataset"][0]

    def test_current_text_cannot_be_bypassed_by_any_ordinary_category(self):
        for key, value in (
            ("expect_any_record_id", "a"), ("expect_any_title", "Destination"),
            ("expect_any_kind", "memory"), ("expect_any_text", "Oldtown"),
        ):
            with self.subTest(key=key):
                sample = self.run_case({"phase": "update", "query": "destination",
                    key: [value], "expect_current_text": ["Newtown"]}, FakeMemory([record()]))
                self.assertFalse(sample["passed"])
                self.assertEqual(sample["failure_reason"], "expectation_mismatch")
                self.assertEqual(sample["expected_rank"], 0)
                for metric in ("recall_at_k", "precision_at_k", "mrr", "ndcg_at_k"):
                    self.assertEqual(sample[metric], 0.0)

    def test_current_text_cannot_replace_an_unmatched_ordinary_expectation(self):
        sample = self.run_case({"phase": "update", "query": "destination",
            "expect_any_record_id": ["missing"], "expect_current_text": ["Newtown"]},
            FakeMemory([record(summary="Newtown")]))
        self.assertFalse(sample["passed"])

    def test_current_text_and_ordinary_hit_must_belong_to_same_record(self):
        sample = self.run_case({"phase": "update", "query": "destination",
            "expect_any_record_id": ["a"], "expect_current_text": ["Newtown"]},
            FakeMemory([record(), record(record_id="b", summary="Newtown")]))
        self.assertFalse(sample["passed"])

    def test_matching_current_state_controls_rank_and_hit_count(self):
        sample = self.run_case({"phase": "update", "query": "destination",
            "expect_any_kind": ["memory"], "expect_current_text": ["Newtown"]},
            FakeMemory([record(), record(record_id="b", summary="Newtown"),
                        record(record_id="c", meta={"current_text": "Newtown"})]))
        self.assertTrue(sample["passed"])
        self.assertEqual(sample["expected_rank"], 2)
        self.assertEqual(sample["mrr"], 0.5)
        self.assertEqual(sample["precision_at_k"], 0.667)

    def test_current_only_case_preserves_case_insensitive_any_term_matching(self):
        sample = self.run_case({"phase": "update", "query": "destination",
            "expect_current_text": ["elsewhere", "NEWTOWN"]},
            FakeMemory([record(meta={"current_text": "Newtown"})]))
        self.assertTrue(sample["passed"])
        self.assertEqual(sample["expected_text"], [])
        self.assertEqual(sample["expected"]["texts"], [])
        self.assertEqual(sample["expected_current_text"], ["elsewhere", "NEWTOWN"])

    def test_ordinary_categories_still_use_any_hit_semantics(self):
        sample = self.run_case({"query": "destination", "expect_any_record_id": ["a"],
            "expect_any_text": ["missing"]}, FakeMemory([record()]))
        self.assertTrue(sample["passed"])
        ranks = self.helpers["_matching_ranks"](
            returned=[SimpleNamespace(record_id="a"), SimpleNamespace(record_id="b")],
            expected_record_ids=["a", "b"], expected_titles=[], expected_kinds=[],
            expected_text=[], expected_current_text=[],
        )
        self.assertEqual(ranks, [1, 2])

    def test_current_state_outside_limit_does_not_pass(self):
        sample = self.run_case({"phase": "update", "query": "destination", "limit": 1,
            "expect_current_text": ["Newtown"]},
            FakeMemory([record(), record(record_id="b", summary="Newtown")]))
        self.assertFalse(sample["passed"])

    def test_current_state_uses_existing_record_text_fields(self):
        for changes in (
            {"title": "Newtown"}, {"summary": "Newtown"}, {"detail": "Newtown"},
            {"content": {"text": "Newtown"}}, {"content": {"summary": "Newtown"}},
            {"meta": {"memory_type": "Newtown"}}, {"meta": {"current_text": "Newtown"}},
        ):
            with self.subTest(changes=changes):
                sample = self.run_case({"phase": "update", "query": "destination",
                    "expect_any_record_id": ["a"], "expect_current_text": ["Newtown"]},
                    FakeMemory([record(**changes)]))
                self.assertTrue(sample["passed"])

    def test_empty_result_cannot_meet_current_state(self):
        sample = self.run_case({"phase": "update", "query": "destination",
            "expect_current_text": ["Newtown"]}, FakeMemory())
        self.assertFalse(sample["passed"])
        self.assertEqual(sample["expected_rank"], 0)

    def test_extraction_forbidden_title_causes_failure(self):
        sample = self.run_case({"phase": "extraction", "input_text": "ordinary body",
            "expect_any_text": ["ordinary"], "forbid_any_text": ["forbidden"]},
            FakeMemory(extracted=record(title="FORBIDDEN", summary="ordinary body")))
        self.assertFalse(sample["passed"])
        self.assertTrue(sample["hallucinated"])
        self.assertEqual(sample["failure_reason"], "hallucination_detected")

    def test_extraction_forbidden_body_fields_still_fail(self):
        for changes in (
            {"summary": "forbidden"}, {"detail": "forbidden"},
            {"content": {"text": "forbidden"}}, {"content": {"summary": "forbidden"}},
        ):
            with self.subTest(changes=changes):
                sample = self.run_case({"phase": "extraction", "input_text": "ordinary",
                    "forbid_any_text": ["forbidden"]}, FakeMemory(extracted=record(**changes)))
                self.assertFalse(sample["passed"])
                self.assertTrue(sample["hallucinated"])

    def test_extraction_clean_title_preserves_success(self):
        sample = self.run_case({"phase": "extraction", "input_text": "ordinary body",
            "expect_any_text": ["ordinary"], "forbid_any_text": ["forbidden"]},
            FakeMemory(extracted=record(title="Clean", summary="ordinary body")))
        self.assertTrue(sample["passed"])
        self.assertFalse(sample["hallucinated"])

    def test_extraction_title_does_not_replace_positive_body_match(self):
        sample = self.run_case({"phase": "extraction", "input_text": "ordinary body",
            "expect_any_text": ["title only"]},
            FakeMemory(extracted=record(title="title only", summary="ordinary body")))
        self.assertFalse(sample["passed"])
        self.assertEqual(sample["failure_reason"], "extraction_mismatch")

    def test_replay_preserves_empty_expectations_and_verdict_for_every_alias(self):
        for key in ("expected_empty", "expect_empty", "no_answer", "expect_no_answer"):
            for items in ([], [record()]):
                with self.subTest(key=key, empty=not items):
                    memory = FakeMemory(items)
                    original = self.run_case({"phase": "temporal", "query": "unknown",
                        key: True}, memory)
                    row = self.replay_row(original)
                    replay = self.run_case(row, memory)
                    self.assertIs(row.get("expected_empty"), True)
                    self.assertEqual(replay["phase"], original["phase"])
                    self.assertEqual(replay["passed"], original["passed"])
                    self.assertEqual(replay["expected_empty"], original["expected_empty"])

    def test_replay_preserves_all_recall_phases_and_current_expectations(self):
        for phase in ("update", "usage", "consistency", "temporal", "implicit"):
            with self.subTest(phase=phase):
                memory = FakeMemory([record()])
                original = self.run_case({"id": "replay-case", "phase": phase,
                    "query": "destination", "expect_any_record_id": ["a"],
                    "expect_current_text": ["Newtown"], "limit": 2}, memory)
                row = self.replay_row(original)
                replay = self.run_case(row, memory)
                for key in ("case_id", "phase", "query", "limit", "expected_text",
                            "expected_current_text", "expected_record_ids", "passed"):
                    self.assertEqual(replay[key], original[key])

    def test_extraction_replay_preserves_ingestion_inputs_and_failure(self):
        memory = FakeMemory()
        original = self.run_case({"id": "extract-case", "phase": "extraction",
            "title": "custom forbidden title", "input_text": "ordinary body",
            "expect_memory_type": "decision", "force_capture": False,
            "expect_any_text": ["ordinary"], "forbid_any_text": ["forbidden"]}, memory)
        row = self.replay_row(original)
        replay = self.run_case(row, memory)
        self.assertEqual(row.get("phase"), "extraction")
        self.assertEqual(row.get("input_text"), "ordinary body")
        self.assertEqual(row.get("expect_memory_type"), "decision")
        self.assertEqual(row.get("title"), "custom forbidden title")
        self.assertIs(row.get("force_capture"), False)
        self.assertEqual(len(memory.ingest_calls), 2)
        self.assertEqual(memory.ingest_calls[0], memory.ingest_calls[1])
        for key in ("phase", "input_text", "expected_memory_type", "passed", "failure_reason"):
            self.assertEqual(replay[key], original[key])

    def test_replay_preserves_successful_extraction_defaults(self):
        memory = FakeMemory()
        original = self.run_case({"phase": "extraction", "input_text": "ordinary body"}, memory)
        replay = self.run_case(self.replay_row(original), memory)
        self.assertTrue(original["passed"])
        self.assertTrue(replay["passed"])
        self.assertEqual(len(memory.ingest_calls), 2)
        self.assertEqual(memory.ingest_calls[0], memory.ingest_calls[1])

    def test_negative_only_and_no_expectation_replay_keep_existing_rules(self):
        for extra in ({"forbid_any_text": ["forbidden"]}, {}):
            for items in ([], [record()]):
                with self.subTest(extra=extra, empty=not items):
                    memory = FakeMemory(items)
                    original = self.run_case({"query": "destination", **extra}, memory)
                    replay = self.run_case(self.replay_row(original), memory)
                    self.assertEqual(replay["passed"], original["passed"])
                    self.assertEqual(replay["expected_empty"], original["expected_empty"])


if __name__ == "__main__":
    unittest.main()
