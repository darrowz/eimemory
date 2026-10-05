"""EA-088 A4: isolated normalization, dispatch, and invalid-report checks.

Only pre-inspected AST-selected pure/local-report functions execute. Phase
handlers stop after their missing-input checks and return inert routing data.
No project imports, Runtime, store, model, retrieval, ingestion, or incidents.
"""

import ast
from dataclasses import asdict, dataclass
from pathlib import Path
import sys
from typing import Any
import unittest


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "eimemory/evaluation/benchmarks.py"
if len(sys.argv) > 2 and sys.argv[1] == "--source":
    SOURCE = Path(sys.argv[2])
    del sys.argv[1:3]


@dataclass
class FakeScope:
    tenant_id: str = "synthetic-a4"

    @classmethod
    def from_dict(cls, value):
        return cls(**value)


def load_helpers(source=SOURCE):
    namespace = {"Any": Any, "asdict": asdict, "ScopeRef": FakeScope,
                 "MemoryAPI": Any}
    selections = (
        (ROOT / "eimemory/evaluation/contracts.py", {
            "normalize_memory_eval_suite", "_normalize_case", "_int_value", "_clamp_float",
        }, {"SUPPORTED_PHASES"}),
        (source, {"_run_case", "_run_update_case", "_invalid_case",
                  "_normalize_terms", "_limit_value"}, set()),
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

    tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
    for name, input_name in (("_run_recall_case", "query"),
                             ("_run_extraction_case", "input_text")):
        node = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == name)
        # The inspected first three statements only prepare a scope and input,
        # then return an invalid report if that input is absent. Discard all
        # remaining statements, including every integration operation.
        assert len(node.body) > 3
        assert isinstance(node.body[0], ast.Assign)
        assert node.body[0].targets[0].id == "case_scope"
        assert isinstance(node.body[1], ast.Assign)
        assert node.body[1].targets[0].id == input_name
        assert isinstance(node.body[2], ast.If)
        assert len(node.body[2].body) == 1
        assert isinstance(node.body[2].body[0], ast.Return)
        assert node.body[2].body[0].value.func.id == "_invalid_case"
        node.body = node.body[:3] + ast.parse(
            "return {'phase': phase, 'case': case, 'index': index, "
            "'scope': asdict(case_scope)}"
        ).body
        exec(compile(ast.fix_missing_locations(ast.Module(body=[node], type_ignores=[])),
                     "<input-check-and-inert-routing-only>", "exec"), namespace)
    return namespace


class MalformedCaseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.helpers = load_helpers()

    def run_case(self, case, *, index=0, scope=None):
        return self.helpers["_run_case"](
            None, None, case, index=index, default_scope=scope or FakeScope())

    def normalized_case(self, item, *, index=0, scope=None):
        return self.helpers["_normalize_case"](
            item, index=index, default_scope=asdict(scope or FakeScope()))

    def test_non_object_json_rows_report_invalid_case_after_normalization(self):
        for item in (None, "bad row", 3, 1.5, False, [], ["bad row"]):
            with self.subTest(item=item):
                case = self.normalized_case(item, index=7)
                self.assertEqual(case["invalid_case"], "invalid_case")
                self.assertEqual(case["query"], "")
                sample = self.run_case(case, index=7)
                self.assertEqual(sample["failure_reason"], "invalid_case")
                self.assertEqual(sample["case_id"], "7")
                self.assertEqual(sample["index"], 7)

    def test_direct_non_object_rows_still_report_invalid_case(self):
        for item in (None, "bad row", 3, 1.5, False, [], ["bad row"]):
            with self.subTest(item=item):
                self.assertEqual(self.run_case(item)["failure_reason"], "invalid_case")

    def test_normalized_and_direct_invalid_reports_agree(self):
        direct = self.run_case(None, index=4)
        normalized = self.run_case(self.normalized_case(None, index=4), index=4)
        self.assertEqual(normalized, direct)
        self.assertIs(normalized["passed"], False)
        self.assertIs(normalized["hallucinated"], False)
        for field in ("recall_at_k", "precision_at_k", "ndcg_at_k", "mrr", "expected_rank"):
            self.assertEqual(normalized[field], 0)
        for field in ("returned_record_ids", "returned_titles", "returned_texts"):
            self.assertEqual(normalized[field], [])

    def test_suite_default_scope_and_row_identity_are_preserved(self):
        suite = self.helpers["normalize_memory_eval_suite"]({
            "scope": {"tenant_id": "suite-a4"}, "cases": [{"query": "valid"}, None],
        })
        sample = self.run_case(suite["cases"][1], index=1, scope=FakeScope("suite-a4"))
        self.assertEqual(sample["failure_reason"], "invalid_case")
        self.assertEqual(sample["scope"], suite["scope"])
        self.assertEqual(sample["case_id"], "1")
        self.assertEqual(sample["phase"], "usage")
        self.assertEqual(sample["limit"], 5)

    def test_real_empty_query_objects_keep_their_existing_diagnosis(self):
        for phase in ("usage", "update", "consistency", "temporal", "implicit"):
            for query in (None, "", "   "):
                with self.subTest(phase=phase, query=query):
                    case = self.normalized_case({"phase": phase, "query": query})
                    self.assertNotIn("invalid_case", case)
                    sample = self.run_case(case)
                    self.assertEqual(sample["failure_reason"], "empty_query")
                    self.assertEqual(sample["phase"], phase)

    def test_missing_extraction_input_keeps_its_existing_diagnosis(self):
        sample = self.run_case(self.normalized_case({"phase": "extraction"}))
        self.assertEqual(sample["failure_reason"], "missing_input_text")
        self.assertEqual(sample["phase"], "extraction")

    def test_supported_phases_still_receive_the_original_normalized_object(self):
        for phase in sorted(self.helpers["SUPPORTED_PHASES"]):
            with self.subTest(phase=phase):
                case = self.normalized_case({"phase": phase, "query": "q", "input_text": "i"})
                routed = self.run_case(case)
                self.assertEqual(routed["phase"], phase)
                self.assertIs(routed["case"], case)

    def test_noncanonical_marker_values_do_not_change_dispatch(self):
        for marker in (None, "", False, 0, True, "unknown", "INVALID_CASE", [], {}):
            for phase in sorted(self.helpers["SUPPORTED_PHASES"]):
                with self.subTest(marker=marker, phase=phase):
                    case = self.normalized_case({"phase": phase, "query": "q", "input_text": "i",
                                                 "invalid_case": marker})
                    routed = self.run_case(case)
                    self.assertIs(routed["case"], case)

    def test_normalized_marker_is_checked_before_phase_dispatch(self):
        for phase in sorted(self.helpers["SUPPORTED_PHASES"]):
            with self.subTest(phase=phase):
                case = {"phase": phase, "query": "q", "input_text": "i",
                        "case_id": "marked", "invalid_case": "invalid_case"}
                sample = self.run_case(case)
                self.assertEqual(sample.get("failure_reason"), "invalid_case")
                self.assertEqual(sample["case_id"], "marked")
                self.assertEqual(sample["phase"], phase)


if __name__ == "__main__":
    unittest.main()
