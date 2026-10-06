"""Inert, stdlib-only tests for provider invocation reporting.

The project module is parsed, never imported. Only its orchestration function
is compiled; paths, readers, request builders and providers are local doubles.
Every case exits before temporary directories, evaluators or receipt handling.
"""
from __future__ import annotations

import ast
from hashlib import sha256
from pathlib import Path
import unittest


SOURCE_PATH = (
    Path(__file__).resolve().parents[1]
    / "eimemory/evaluation/hongtu_code_implementation.py"
)


class _ForbiddenOperation(BaseException):
    """Cannot be swallowed by the function's broad Exception handler."""


class _Forbidden:
    def __getattr__(self, name):
        raise _ForbiddenOperation(name)

    def __call__(self, *args, **kwargs):
        raise _ForbiddenOperation("call")


class _InertPath:
    def __init__(self, value):
        self.parts = tuple(str(value).split("/"))

    def resolve(self):
        return self


class ProviderInvokedLocalTests(unittest.TestCase):
    def _run_case(self, case):
        tree = ast.parse(SOURCE_PATH.read_text(encoding="utf-8"))
        node = next(
            item for item in tree.body
            if isinstance(item, ast.FunctionDef)
            and item.name == "run_code_implementation_catalog_pass"
        )
        self.assertFalse(node.decorator_list)
        self.assertFalse(any(
            isinstance(item, (ast.Import, ast.ImportFrom))
            for item in ast.walk(node)
        ))
        calls = []
        digest_calls = []

        class InertProvider:
            def propose_patch_v2(self, request):
                calls.append(request)
                if case == "during_call":
                    raise RuntimeError("inert-call-error")
                return None

        def read_files(*args):
            if case == "before_call":
                raise ValueError("inert-read-error")
            return []

        def fixture_digest(*args):
            digest_calls.append(None)
            if case == "after_call" and len(digest_calls) == 2:
                raise OSError("inert-post-call-error")
            return "inert-digest"

        namespace = {
            "Path": _InertPath,
            "_read_allowed_files": read_files,
            "_tree_digest": lambda *args: "inert-tree",
            "_complete_fixture_digest": fixture_digest,
            "sha256": sha256,
            "build_request": lambda **kwargs: {"request_digest": "inert-request"},
            "_BASE_COMMIT": "0" * 40,
            "CATALOG_TEST_PLAN_ID": "inert-plan",
            "protected_test_plan_digest": lambda *args: "inert-plan-digest",
            "tempfile": _Forbidden(),
            "datetime": _Forbidden(),
        }
        # The only response supplied is None; this isinstance check is inert.
        from collections.abc import Mapping
        namespace["Mapping"] = Mapping
        extracted = ast.Module(body=[
            ast.ImportFrom(module="__future__", names=[
                ast.alias(name="annotations")
            ], level=0),
            node,
        ], type_ignores=[])
        exec(compile(ast.fix_missing_locations(extracted),
                     "<isolated-provider-report>", "exec"), namespace)
        provider = object() if case == "missing_operation" else InertProvider()
        result = namespace["run_code_implementation_catalog_pass"](
            provider=provider,
            fixture_root="inert",
            fixture_files=["fixture.py"],
            evaluator=_Forbidden(),
        )
        self.assertIs(result["ok"], False)
        return result, len(calls)

    def test_failure_before_call_reports_not_invoked(self):
        result, calls = self._run_case("before_call")
        self.assertEqual(calls, 0)
        self.assertIs(result["provider_invoked"], False)
        self.assertEqual(result["reason"], "catalog_pass_failed:ValueError")

    def test_missing_operation_reports_not_invoked(self):
        result, calls = self._run_case("missing_operation")
        self.assertEqual(calls, 0)
        self.assertIs(result["provider_invoked"], False)
        self.assertEqual(result["reason"], "provider_operation_unavailable")

    def test_failure_during_call_reports_invoked(self):
        result, calls = self._run_case("during_call")
        self.assertEqual(calls, 1)
        self.assertIs(result["provider_invoked"], True)
        self.assertEqual(result["reason"], "catalog_pass_failed:RuntimeError")

    def test_failure_after_call_reports_invoked(self):
        result, calls = self._run_case("after_call")
        self.assertEqual(calls, 1)
        self.assertIs(result["provider_invoked"], True)
        self.assertEqual(result["reason"], "catalog_pass_failed:OSError")

    def test_invalid_response_reports_invoked(self):
        result, calls = self._run_case("invalid_response")
        self.assertEqual(calls, 1)
        self.assertIs(result["provider_invoked"], True)
        self.assertEqual(result["reason"], "provider_attestation_invalid")


if __name__ == "__main__":
    unittest.main()
