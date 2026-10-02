"""AST-only diagnostic-value propagation; no real batch, consumer, or backend."""
from __future__ import annotations

import argparse
import ast
import builtins
import copy
import hashlib
from pathlib import Path
import unittest


BASELINE_SHA256 = "a76c533dcca8c252482742565b74f7ece6dd154fac74224bd33f2bded32cbeaa"
REASONS = (
    "recall_budget_exhausted", "candidate_hydration_timeout", "candidate_collection_incomplete",
    "authority_unavailable", "authority_changed", "selection_deadline_exceeded",
)


class FakeBatch:
    def __init__(self, *, hits, diagnostics):
        self.hits = hits
        self.diagnostics = diagnostics
    def diagnostic_dict(self):
        return self.diagnostics


class RestrictedMetadata:
    def __init__(self, **fields):
        self.fields = fields
        self.forbidden = []
    def __getattr__(self, name):
        if name in self.fields:
            return self.fields[name]
        self.forbidden.append(name)
        raise AssertionError(f"Forbidden metadata/backend access: {name}")


def extract_shell(path, expected):
    data = path.read_bytes()
    actual = hashlib.sha256(data).hexdigest()
    if actual != expected:
        raise ValueError(f"Pinned source digest mismatch: {actual}")
    module = ast.parse(data, filename=str(path))
    owners = [node for node in module.body if isinstance(node, ast.ClassDef) and node.name == "PostgresVectorCandidateSource"]
    if len(owners) != 1:
        raise ValueError("Expected one candidate-source class")
    selected = [node for node in owners[0].body if isinstance(node, ast.FunctionDef) and node.name == "_batch"]
    if len(selected) != 1 or selected[0].decorator_list:
        raise ValueError("Expected one complete undecorated _batch method")
    method = selected[0]
    if actual == BASELINE_SHA256 and (method.lineno, method.end_lineno) != (1942, 1978):
        raise ValueError("Baseline method range mismatch")
    if any(isinstance(node, (ast.Import, ast.ImportFrom)) for node in ast.walk(method)):
        raise ValueError("Imports are forbidden inside the extracted wrapper")
    isolated = ast.fix_missing_locations(ast.Module(body=[
        ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0),
        ast.ClassDef(name="Shell", bases=[], keywords=[], body=[copy.deepcopy(method)], decorator_list=[]),
    ], type_ignores=[]))
    forbidden_imports = []
    def guarded_import(name, *args, **kwargs):
        if name == "__future__":
            return builtins.__import__(name, *args, **kwargs)
        forbidden_imports.append(name)
        raise AssertionError("No target/helper/backend import is allowed")
    namespace = {
        "__builtins__": {**vars(builtins), "__import__": guarded_import},
        "CandidateBatch": FakeBatch,
        "_public_lag_seconds": lambda _value: 0,
    }
    exec(compile(isolated, str(path), "exec"), namespace)
    print(f"SOURCE_SHA256 {actual}")
    print(f"EXTRACTED _batch L{method.lineno}-{method.end_lineno}")
    return namespace["Shell"], forbidden_imports


def diagnostic_tests(shell_type, forbidden_imports):
    class DiagnosticWrapperTests(unittest.TestCase):
        def setUp(self):
            self.shell = shell_type()
            self.shell.name = "fake-source"
            self.shell.policy_version = "fake-policy"
            self.shell.config = RestrictedMetadata(top_k_max=10)
            self.shell._last_state = RestrictedMetadata(watermark="opaque-watermark", authority_revision="opaque-revision", lag_seconds=0)
            self.request = RestrictedMetadata(limit=3)
            self.hits = (object(), object())
        def tearDown(self):
            self.assertEqual(forbidden_imports, [])
            self.assertEqual(self.shell.config.forbidden, [])
            self.assertEqual(self.shell._last_state.forbidden, [])
            self.assertEqual(self.request.forbidden, [])
        def batch(self, diagnostics=None, *, child_hits=None, **kwargs):
            child = FakeBatch(hits=self.hits if child_hits is None else child_hits, diagnostics=diagnostics or {})
            return self.shell._batch(child, request=self.request, state="bypassed", **kwargs)
        def assert_complete(self, report):
            self.assertNotIn("retrieval_mode", report)
            self.assertNotIn("drops", report)
        def test_child_deadline_mode_is_preserved(self):
            result = self.batch({"retrieval_mode": "deadline_exhausted"})
            self.assertEqual(result.diagnostics.get("retrieval_mode"), "deadline_exhausted")
        def test_each_known_positive_count_is_preserved(self):
            for reason in REASONS:
                with self.subTest(reason=reason):
                    result = self.batch({"drops": {reason: 7}})
                    self.assertEqual(result.diagnostics.get("drops"), {reason: 7})
        def test_other_truthy_values_become_bounded_integer_signals(self):
            opaque = object()
            values = [True, "opaque-text", [opaque], {"opaque": opaque}, -2, 1.5]
            for value in values:
                with self.subTest(kind=type(value).__name__):
                    result = self.batch({"drops": {REASONS[1]: value}})
                    self.assertEqual(result.diagnostics.get("drops"), {REASONS[1]: 1})
                    self.assertIs(type(result.diagnostics["drops"][REASONS[1]]), int)
        def test_falsey_markers_are_omitted(self):
            for value in (False, 0, "", None, [], {}):
                with self.subTest(kind=type(value).__name__):
                    self.assert_complete(self.batch({"drops": {REASONS[0]: value}}).diagnostics)
        def test_own_request_budget_is_visible_at_top_level(self):
            result = self.batch(error_code="recall_budget_exhausted")
            self.assertEqual(result.diagnostics.get("retrieval_mode"), "deadline_exhausted")
            self.assertEqual(result.diagnostics.get("drops"), {"recall_budget_exhausted": 1})
            self.assertEqual(result.diagnostics["postgres"]["error_code"], "recall_budget_exhausted")
        def test_own_budget_does_not_add_to_child_count(self):
            for value, expected in ((7, 7), (True, 1), (0, 1)):
                with self.subTest(value=value):
                    result = self.batch({"drops": {REASONS[0]: value, REASONS[1]: 2}}, error_code=REASONS[0])
                    self.assertEqual(result.diagnostics.get("drops"), {REASONS[0]: expected, REASONS[1]: 2})
                    self.assertEqual(result.diagnostics.get("retrieval_mode"), "deadline_exhausted")
        def test_optional_backend_failures_keep_complete_fallback(self):
            for error in ("", "postgres_disabled", "postgres_startup_error", "postgres_unavailable"):
                with self.subTest(error=error):
                    result = self.batch({"retrieval_mode": "recall_index_hybrid"}, error_code=error)
                    self.assert_complete(result.diagnostics)
                    self.assertTrue(result.diagnostics["fallback"])
                    self.assertEqual(result.diagnostics["postgres"]["error_code"], error)
        def test_hits_and_explicit_empty_override_are_preserved(self):
            replacement = (object(),)
            for override, expected in ((None, self.hits), (replacement, replacement), ((), ())):
                with self.subTest(count=len(expected)):
                    result = self.batch(hits=override, valid_empty=True)
                    self.assertEqual(result.hits, expected)
                    self.assertTrue(all(left is right for left, right in zip(result.hits, expected)))
                    self.assertEqual(result.diagnostics["candidate_count"], len(expected))
                    self.assertEqual(result.diagnostics["returned_count"], len(expected))
                    self.assertTrue(result.diagnostics["postgres"]["valid_empty"])
                    self.assert_complete(result.diagnostics)
            empty = self.batch(child_hits=(), valid_empty=True)
            self.assertEqual(empty.hits, ())
            self.assert_complete(empty.diagnostics)
        def test_unknown_status_error_and_scoring_fields_are_excluded(self):
            opaque = object()
            report = self.batch({
                "status": "incomplete", "error": opaque, "unknown": opaque, "retrieval_mode": "unknown-mode",
                "drops": {"candidate_scoring_timeout": 5, "unknown-reason": opaque},
            }).diagnostics
            self.assert_complete(report)
            for key in ("status", "error", "unknown"):
                self.assertNotIn(key, report)
            def contains_opaque(value):
                if value is opaque:
                    return True
                if isinstance(value, dict):
                    return any(contains_opaque(key) or contains_opaque(item) for key, item in value.items())
                if isinstance(value, (list, tuple)):
                    return any(contains_opaque(item) for item in value)
                return False
            self.assertFalse(contains_opaque(report))
        def test_maximum_report_retains_a_slot_for_timing(self):
            result = self.batch({"retrieval_mode": "deadline_exhausted", "drops": dict.fromkeys(REASONS, 3)}, error_code=REASONS[0])
            report = result.diagnostics
            self.assertEqual(report.get("drops"), dict.fromkeys(REASONS, 3))
            self.assertEqual(report.get("retrieval_mode"), "deadline_exhausted")
            self.assertLessEqual(len(report), 10)
            with_timing = {**report, "timing": {"elapsed_ms": 0}}
            self.assertLessEqual(len(with_timing), 12)
        def test_child_diagnostics_are_not_mutated(self):
            drops = {REASONS[0]: 5, "unknown-reason": object()}
            report = {"retrieval_mode": "deadline_exhausted", "drops": drops}
            before = dict(drops)
            self.batch(report, error_code=REASONS[0])
            self.assertEqual(drops, before)
            self.assertIs(report["drops"], drops)
        def test_backend_drop_metrics_stay_separate(self):
            result = self.batch({"drops": {REASONS[0]: 3}}, drops={"fake-backend-metric": 2})
            self.assertEqual(result.diagnostics.get("drops"), {REASONS[0]: 3})
            self.assertEqual(result.diagnostics["postgres"]["drops"], {"fake-backend-metric": 2})
    return DiagnosticWrapperTests


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--expected-sha256", required=True)
    args = parser.parse_args()
    extracted = extract_shell(args.source, args.expected_sha256)
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(diagnostic_tests(*extracted))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    failed = {getattr(test, "test_case", test)._testMethodName for test, _ in [*result.failures, *result.errors]}
    print(f"FAILING_METHODS {len(failed)}")
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
