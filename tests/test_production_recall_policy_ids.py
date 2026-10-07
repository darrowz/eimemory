"""Local AST-only regression tests; no project imports or runtime calls."""

import ast
from pathlib import Path
import unittest


SOURCE = Path(__file__).resolve().parents[1] / "eimemory/evaluation/production_recall.py"


def isolated_targets():
    tree = ast.parse(SOURCE.read_text(encoding="utf-8"), filename=str(SOURCE))
    run_case = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "_run_case")
    projections = [
        value
        for node in ast.walk(run_case)
        if isinstance(node, ast.Dict)
        for key, value in zip(node.keys, node.values)
        if isinstance(key, ast.Constant) and key.value == "policy_suggestion_ids"
    ]
    if len(projections) != 1:
        raise AssertionError("Expected exactly one _run_case policy_suggestion_ids projection")
    expression = compile(ast.Expression(projections[0]), str(SOURCE), "eval")
    helper = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "_policy_hit")
    helper.returns = None
    for argument in helper.args.posonlyargs + helper.args.args + helper.args.kwonlyargs:
        argument.annotation = None
    module = ast.fix_missing_locations(ast.Module(body=[helper], type_ignores=[]))
    namespace = {"__builtins__": {"str": str}}
    exec(compile(module, str(SOURCE), "exec"), namespace)

    def project(suggestions):
        return eval(expression, {"__builtins__": {"str": str}, "policy_suggestions": suggestions})

    return project, namespace["_policy_hit"]


class PolicySuggestionIdsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        project, policy_hit = isolated_targets()
        cls.project = staticmethod(project)
        cls.policy_hit = staticmethod(policy_hit)

    def test_record_id_only_is_reported_and_hits(self):
        suggestions = [{"record_id": "P"}]
        self.assertEqual(self.policy_hit(suggestions, expected_policy_ids={"P"}), 1.0)
        self.assertEqual(self.project(suggestions), ["P"])

    def test_id_precedes_both_fallbacks(self):
        suggestions = [{"id": "I", "pattern_id": "Q", "record_id": "P"}]
        self.assertEqual(self.project(suggestions), ["I"])
        self.assertEqual(self.policy_hit(suggestions, expected_policy_ids={"I"}), 1.0)
        self.assertEqual(self.policy_hit(suggestions, expected_policy_ids={"Q", "P"}), 0.0)

    def test_pattern_id_precedes_record_id(self):
        suggestions = [{"pattern_id": "Q", "record_id": "P"}, {"id": "", "pattern_id": "R", "record_id": "S"}]
        self.assertEqual(self.project(suggestions), ["Q", "R"])
        self.assertEqual(self.policy_hit(suggestions, expected_policy_ids={"Q", "R"}), 1.0)
        self.assertEqual(self.policy_hit(suggestions, expected_policy_ids={"P", "S"}), 0.0)

    def test_falsey_primary_values_fall_back(self):
        suggestions = [
            {"id": "", "pattern_id": None, "record_id": "P"},
            {"id": 0, "pattern_id": False, "record_id": "Q"},
            {"id": [], "pattern_id": {}, "record_id": "R"},
        ]
        self.assertEqual(self.project(suggestions), ["P", "Q", "R"])
        for suggestion, expected in zip(suggestions, ("P", "Q", "R")):
            with self.subTest(expected=expected):
                self.assertEqual(self.policy_hit([suggestion], expected_policy_ids={expected}), 1.0)

    def test_missing_or_falsey_ids_remain_empty_strings(self):
        suggestions = [{}, {"id": None}, {"pattern_id": ""}, {"record_id": 0}, {"id": False, "pattern_id": [], "record_id": {}}]
        self.assertEqual(self.project(suggestions), ["", "", "", "", ""])
        self.assertEqual(self.policy_hit(suggestions, expected_policy_ids={""}), 1.0)
        self.assertEqual(self.policy_hit(suggestions, expected_policy_ids={"P"}), 0.0)

    def test_str_conversion_preserves_numbers_and_other_truthy_values(self):
        suggestions = [
            {"id": 7, "pattern_id": "Q", "record_id": "P"},
            {"pattern_id": 2.5, "record_id": "P"},
            {"record_id": -4},
            {"record_id": True},
            {"record_id": ["P"]},
        ]
        self.assertEqual(self.project(suggestions), ["7", "2.5", "-4", "True", "['P']"])
        for suggestion, expected in zip(suggestions, ("7", "2.5", "-4", "True", "['P']")):
            with self.subTest(expected=expected):
                self.assertEqual(self.policy_hit([suggestion], expected_policy_ids={expected}), 1.0)

    def test_order_duplicates_case_and_whitespace_are_preserved(self):
        suggestions = [{"record_id": " P "}, {"id": "P"}, {"pattern_id": "P"}, {"record_id": "p"}]
        self.assertEqual(self.project(suggestions), [" P ", "P", "P", "p"])
        self.assertEqual(self.policy_hit(suggestions, expected_policy_ids={" P "}), 1.0)
        self.assertEqual(self.policy_hit([{"record_id": "p"}], expected_policy_ids={"P"}), 0.0)

    def test_empty_suggestions_and_empty_expectations_are_unchanged(self):
        self.assertEqual(self.project([]), [])
        self.assertEqual(self.policy_hit([], expected_policy_ids=set()), 0.0)
        self.assertEqual(self.policy_hit([], expected_policy_ids={"P"}), 0.0)
        self.assertEqual(self.policy_hit([{}], expected_policy_ids=set()), 1.0)
        self.assertEqual(self.policy_hit([{"record_id": "P"}], expected_policy_ids=set()), 1.0)


if __name__ == "__main__":
    unittest.main()
