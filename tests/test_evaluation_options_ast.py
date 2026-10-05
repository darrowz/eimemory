"""EA-077 option conversion tests using stdlib AST only, without project imports.

Only the entry-point signature and its two pure normalization assignments run.
No runtime, semantic evaluator, provider, store, scope, seed, or recall executes.
Wiring checks inspect AST nodes without evaluating the corresponding calls.
"""
from __future__ import annotations

import ast
import copy
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
FRAMEWORK = ROOT / "eimemory/evaluation/framework.py"


def definitions():
    tree = ast.parse(FRAMEWORK.read_text(encoding="utf-8"))
    return {node.name: node for node in tree.body if isinstance(node, ast.FunctionDef)}


def option_namespace():
    functions = definitions()
    entry = copy.deepcopy(functions["run_evaluation"])
    selected = []
    for node in entry.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            if isinstance(node.targets[0], ast.Name) and node.targets[0].id in {"normalized", "default_profile"}:
                selected.append(node)
    assert len(selected) == 2
    entry.body = selected + [ast.Return(value=ast.Name(id="default_profile", ctx=ast.Load()))]
    body = [ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0),
            *[copy.deepcopy(functions[name]) for name in ("_normalize_dataset", "_normalize_profile", "_positive_int")],
            entry]
    namespace = {}
    exec(compile(ast.fix_missing_locations(ast.Module(body=body, type_ignores=[])),
                 "<evaluation-pure-options>", "exec"), namespace)
    return namespace


class EvaluationOptionsRegression(unittest.TestCase):
    def setUp(self):
        self.namespace = option_namespace()
        self.choose = self.namespace["run_evaluation"]

    def test_omitted_profile_honors_normalized_dataset(self):
        for profile in ("precision", "exploratory", "balanced"):
            with self.subTest(profile=profile):
                self.assertEqual(self.choose(None, {"profile": "  " + profile.upper() + "  "}), profile)

    def test_explicit_profile_overrides_dataset_including_balanced(self):
        for profile in ("precision", "exploratory", "balanced"):
            with self.subTest(profile=profile):
                self.assertEqual(self.choose(None, {"profile": "precision"}, profile=profile), profile)

    def test_missing_or_invalid_profile_falls_back_to_balanced(self):
        for dataset in ({}, [], {"profile": None}, {"profile": ""}, {"profile": "unknown"}):
            with self.subTest(dataset=dataset):
                self.assertEqual(self.choose(None, dataset), "balanced")
        self.assertEqual(self.choose(None, {"profile": "precision"}, profile="unknown"), "balanced")

    def test_falsey_profile_preserves_existing_dataset_fallback(self):
        for profile in (None, ""):
            self.assertEqual(self.choose(None, {"profile": "exploratory"}, profile=profile), "exploratory")

    def test_positive_integer_invalid_numeric_fallback(self):
        positive_int = self.namespace["_positive_int"]
        for value in (None, "bad", "", [], float("nan"), float("inf"), float("-inf")):
            with self.subTest(value=value):
                self.assertEqual(positive_int(value, default=5), 5)

    def test_positive_integer_conversion_and_clamp_unchanged(self):
        positive_int = self.namespace["_positive_int"]
        for value, expected in (("7", 7), (2.9, 2), (-7, 1), (0, 1), (1001, 1000), (10**100, 1000)):
            with self.subTest(value=value):
                self.assertEqual(positive_int(value, default=5), expected)

    def test_static_profile_and_limit_wiring(self):
        functions = definitions()
        runner = functions["run_evaluation"]
        calls = [node for node in ast.walk(runner) if isinstance(node, ast.Call)
                 and isinstance(node.func, ast.Name) and node.func.id == "_run_recall_case"]
        self.assertEqual(len(calls), 1)
        self.assertEqual(ast.unparse(next(k.value for k in calls[0].keywords if k.arg == "default_profile")),
                         "default_profile")
        case = functions["_run_recall_case"]
        statements = [ast.unparse(node) for node in case.body]
        self.assertIn("profile = _normalize_profile(case.get('profile') or default_profile)", statements)
        self.assertIn("task_context.setdefault('recall_profile', profile)", statements)
        self.assertIn("limit = _positive_int(case.get('limit'), default=5)", statements)
        recalls = [node for node in ast.walk(case) if isinstance(node, ast.Call)
                   and isinstance(node.func, ast.Attribute) and node.func.attr == "recall"]
        self.assertEqual(len(recalls), 1)
        self.assertEqual(ast.unparse(recalls[0]),
                         "memory_api.recall(query=query, scope=sample_scope, task_context=task_context, limit=limit)")

    def test_semantic_dispatch_remains_before_profile_selection(self):
        runner = definitions()["run_evaluation"]
        branch = next(node for node in runner.body if isinstance(node, ast.If))
        self.assertEqual(ast.unparse(branch.test),
                         "isinstance(dataset, dict) and str(dataset.get('schema') or '') == 'semantic_recall_cases.v1'")
        self.assertEqual(ast.unparse(branch.body[-1]),
                         "return dict(evaluate_semantic_recall(runtime, dataset))")
        normalization = next(node for node in runner.body if isinstance(node, ast.Assign)
                             and isinstance(node.targets[0], ast.Name) and node.targets[0].id == "normalized")
        self.assertLess(runner.body.index(branch), runner.body.index(normalization))

    def test_runtime_signature_preserves_omission_and_forwarding(self):
        tree = ast.parse((ROOT / "eimemory/api/runtime.py").read_text(encoding="utf-8"))
        runners = [node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)
                   and node.name == "run_evaluation"]
        self.assertEqual(len(runners), 1)
        runner = runners[0]
        for signature in (runner.args, definitions()["run_evaluation"].args):
            index = next(i for i, arg in enumerate(signature.kwonlyargs) if arg.arg == "profile")
            self.assertIsNone(ast.literal_eval(signature.kw_defaults[index]))
            self.assertEqual(ast.unparse(signature.kwonlyargs[index].annotation), "str | None")
        self.assertEqual(ast.unparse(runner.body[-1]),
                         "return run_evaluation(self, dataset, scope=scope, task_type=task_type, profile=profile, seed=seed)")

    def test_cli_eval_run_specific_profile_default_and_forwarding(self):
        tree = ast.parse((ROOT / "eimemory/cli/main.py").read_text(encoding="utf-8"))
        options = [node for node in ast.walk(tree) if isinstance(node, ast.Call)
                   and isinstance(node.func, ast.Attribute) and node.func.attr == "add_argument"
                   and isinstance(node.func.value, ast.Name) and node.func.value.id == "eval_run"
                   and node.args and isinstance(node.args[0], ast.Constant) and node.args[0].value == "--profile"]
        self.assertEqual(len(options), 1)
        self.assertIsNone(ast.literal_eval(next(k.value for k in options[0].keywords if k.arg == "default")))
        command = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "_cmd_eval")
        calls = [node for node in ast.walk(command) if isinstance(node, ast.Call)
                 and isinstance(node.func, ast.Attribute) and node.func.attr == "run_evaluation"]
        self.assertEqual(len(calls), 1)
        self.assertEqual(ast.unparse(next(k.value for k in calls[0].keywords if k.arg == "profile")), "parsed.profile")


if __name__ == "__main__":
    unittest.main()
