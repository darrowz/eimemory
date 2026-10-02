"""AST-only numeric input probe with synthetic environment; no real env access."""
from __future__ import annotations

import argparse
import ast
import builtins
import copy
import hashlib
import math
from pathlib import Path
import unittest


BASELINE_SHA256 = "293a40ff623a97508caaf7751aca13857b094c49f56f43e43c879589cb842329"
METHODS = {"_positive_float": (34, 44), "adapter_timeout_seconds": (53, 71)}
NONFINITE = ("nan", "NaN", "+nan", "-nan", "inf", "+inf", "Infinity", "+Infinity", "1e9999")
MISSING = object()


def extract_functions(path, expected):
    data = path.read_bytes()
    actual = hashlib.sha256(data).hexdigest()
    if actual != expected:
        raise ValueError(f"Pinned source digest mismatch: {actual}")
    module = ast.parse(data, filename=str(path))
    body = [ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0)]
    for name, baseline_range in METHODS.items():
        found = [node for node in module.body if isinstance(node, ast.FunctionDef) and node.name == name]
        if len(found) != 1 or found[0].decorator_list:
            raise ValueError(f"Expected one complete undecorated function: {name}")
        node = found[0]
        if actual == BASELINE_SHA256 and (node.lineno, node.end_lineno) != baseline_range:
            raise ValueError("Baseline function range mismatch")
        if any(isinstance(child, (ast.Import, ast.ImportFrom)) for child in ast.walk(node)):
            raise ValueError("Imports are forbidden inside extracted functions")
        body.append(copy.deepcopy(node))
        print(f"EXTRACTED {name} L{node.lineno}-{node.end_lineno}")
    margins = [node for node in module.body if isinstance(node, ast.Assign) and any(isinstance(target, ast.Name) and target.id == "ADAPTER_TIMEOUT_MARGIN_SECONDS" for target in node.targets)]
    if len(margins) != 1:
        raise ValueError("Expected one unchanged source margin constant")
    margin = ast.literal_eval(margins[0].value)
    if type(margin) not in (int, float) or not math.isfinite(margin):
        raise ValueError("Expected a finite numeric source margin")
    isolated = ast.fix_missing_locations(ast.Module(body=body, type_ignores=[]))
    forbidden_imports = []
    def guarded_import(name, *args, **kwargs):
        if name == "__future__":
            return builtins.__import__(name, *args, **kwargs)
        forbidden_imports.append(name)
        raise AssertionError("No target/environment/helper import is allowed")
    namespace = {
        "__builtins__": {**vars(builtins), "__import__": guarded_import},
        "ADAPTER_TIMEOUT_MARGIN_SECONDS": margin,
        "isfinite": math.isfinite,
    }
    exec(compile(isolated, str(path), "exec"), namespace)
    print(f"SOURCE_SHA256 {actual}")
    print(f"SOURCE_MARGIN {margin}")
    return namespace, forbidden_imports


class FakeEnviron:
    def __init__(self, values):
        self.values = dict(values)
        self.reads = []
        self.forbidden = []
    def get(self, name, default):
        self.reads.append(name)
        return self.values.get(name, default)
    def __getattr__(self, name):
        self.forbidden.append(name)
        raise AssertionError(f"Forbidden synthetic-environment operation: {name}")
    def __getitem__(self, key):
        self.forbidden.append("getitem")
        raise AssertionError("Only the recorded fake get operation is permitted")
    def __setitem__(self, key, value):
        self.forbidden.append("setitem")
        raise AssertionError("No environment mutation is permitted")


class FakeOs:
    def __init__(self, environ):
        self._environ = environ
        self.forbidden = []
    @property
    def environ(self):
        return self._environ
    @environ.setter
    def environ(self, value):
        self.forbidden.append("set-environ")
        raise AssertionError("No environment replacement is permitted")
    def __getattr__(self, name):
        self.forbidden.append(name)
        raise AssertionError(f"Forbidden OS operation: {name}")


def budget_tests(namespace, forbidden_imports):
    class BudgetInputTests(unittest.TestCase):
        def setUp(self):
            self.environments = []
            self.fake_oses = []
            self.budget = 3.0
            self.budget_calls = []
            def fake_recall_budget():
                self.budget_calls.append("fake-budget")
                return self.budget
            namespace["recall_budget_seconds"] = fake_recall_budget
        def tearDown(self):
            self.assertEqual(forbidden_imports, [])
            for environ, os in zip(self.environments, self.fake_oses):
                self.assertEqual(environ.forbidden, [])
                self.assertEqual(os.forbidden, [])
        def configure(self, name, raw):
            values = {} if raw is MISSING else {name: raw}
            environ = FakeEnviron(values)
            os = FakeOs(environ)
            self.environments.append(environ)
            self.fake_oses.append(os)
            namespace["os"] = os
            return environ
        def helper(self, raw):
            environ = self.configure("SYNTHETIC_BUDGET", raw)
            result = namespace["_positive_float"]("SYNTHETIC_BUDGET", 7.25)
            self.assertEqual(environ.reads, ["SYNTHETIC_BUDGET"])
            return result
        def adapter(self, raw):
            environ = self.configure("EIMEMORY_ADAPTER_TIMEOUT_SECONDS", raw)
            count = len(self.budget_calls)
            result = namespace["adapter_timeout_seconds"]()
            self.assertEqual(environ.reads, ["EIMEMORY_ADAPTER_TIMEOUT_SECONDS"])
            self.assertEqual(len(self.budget_calls), count + 1)
            return result
        def test_helper_nonfinite_inputs_use_supplied_fallback(self):
            for raw in NONFINITE:
                with self.subTest(raw=raw):
                    self.assertEqual(self.helper(raw), 7.25)
        def test_adapter_nonfinite_inputs_use_derived_fallback(self):
            for raw in NONFINITE:
                with self.subTest(raw=raw):
                    expected = self.budget + namespace["ADAPTER_TIMEOUT_MARGIN_SECONDS"]
                    self.assertEqual(self.adapter(raw), expected)
        def test_helper_existing_invalid_inputs_keep_fallback(self):
            for raw in (MISSING, "", "  ", "bad-value", "0", "-3.5", "-inf", "-Infinity"):
                with self.subTest(raw="missing" if raw is MISSING else raw):
                    self.assertEqual(self.helper(raw), 7.25)
        def test_adapter_existing_invalid_inputs_keep_fallback(self):
            for raw in (MISSING, "", "  ", "bad-value", "0", "-3.5", "-inf", "-Infinity"):
                with self.subTest(raw="missing" if raw is MISSING else raw):
                    self.assertEqual(self.adapter(raw), self.budget + namespace["ADAPTER_TIMEOUT_MARGIN_SECONDS"])
        def test_helper_positive_finite_values_are_unchanged(self):
            for raw in ("0.125", "2.5", " 60 ", "1e100"):
                with self.subTest(raw=raw):
                    self.assertEqual(self.helper(raw), float(raw))
        def test_adapter_positive_finite_values_are_unchanged(self):
            for raw in ("0.125", "2.5", " 60 ", "1e100"):
                with self.subTest(raw=raw):
                    self.assertEqual(self.adapter(raw), float(raw))
        def test_adapter_keeps_fake_budget_plus_source_margin(self):
            for budget in (0.5, 5.0, 90.0):
                with self.subTest(budget=budget):
                    self.budget = budget
                    self.assertEqual(self.adapter(MISSING), budget + namespace["ADAPTER_TIMEOUT_MARGIN_SECONDS"])
        def test_valid_explicit_value_below_derived_stays_honored(self):
            self.budget = 90.0
            self.assertEqual(self.adapter("0.125"), 0.125)
    return BudgetInputTests


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--expected-sha256", required=True)
    args = parser.parse_args()
    extracted = extract_functions(args.source, args.expected_sha256)
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(budget_tests(*extracted))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    failed = {getattr(test, "test_case", test)._testMethodName for test, _ in [*result.failures, *result.errors]}
    print(f"FAILING_METHODS {len(failed)}")
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
