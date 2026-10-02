"""AST-only prewarm cleanup ownership with inert clients, tokens and permits."""
from __future__ import annotations

import argparse
import ast
import builtins
import copy
from contextlib import contextmanager
import hashlib
from pathlib import Path
import unittest

BASELINE_SHA256 = "62594659bf2d2356ef1e33f84d5fc5d71963e736164aeee8251d39b5c308bd84"
ARGV = ("/synthetic/eimemory/llm/openclaw_gateway.mjs",)
ENV = {"EIMEMORY_RECALL_GATEWAY_POOL": "0", "EIMEMORY_RECALL_GATEWAY_PREWARM": "1"}


class SyntheticAbort(BaseException):
    pass


def extract_shell(path, expected):
    data = path.read_bytes()
    actual = hashlib.sha256(data).hexdigest()
    if actual != expected:
        raise ValueError(f"Pinned source digest mismatch: {actual}")
    module = ast.parse(data, filename=str(path))
    functions = [n for n in module.body if isinstance(n, ast.FunctionDef) and n.name == "prepared_verification"]
    if len(functions) != 1:
        raise ValueError("Expected exactly one complete generator")
    function = functions[0]
    if len(function.decorator_list) != 1 or ast.dump(function.decorator_list[0]) != ast.dump(ast.Name(id="contextmanager", ctx=ast.Load())):
        raise ValueError("Unexpected decorator")
    if actual == BASELINE_SHA256 and (function.decorator_list[0].lineno, function.lineno, function.end_lineno) != (121, 122, 166):
        raise ValueError("Unexpected baseline boundary")
    forbidden = []
    def fenced_import(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "__future__" and level == 0:
            return builtins.__import__(name, globals, locals, fromlist, level)
        forbidden.append(name)
        raise AssertionError("Target imports, process and configuration access are forbidden")
    namespace = {"__builtins__": {**vars(builtins), "__import__": fenced_import}, "contextmanager": contextmanager}
    isolated = ast.fix_missing_locations(ast.Module(body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0), copy.deepcopy(function)], type_ignores=[]))
    exec(compile(isolated, str(path), "exec"), namespace)
    print(f"SOURCE_SHA256 {actual}")
    print(f"EXTRACTED prepared_verification decorator L{function.decorator_list[0].lineno}; complete generator L{function.lineno}-{function.end_lineno}")
    return namespace, forbidden


def permit_tests(namespace, forbidden):
    class PrewarmPermitCleanupTests(unittest.TestCase):
        def setUp(self):
            self.cases = []

        def tearDown(self):
            self.assertEqual(forbidden, [])
            for case in self.cases:
                self.assertEqual(case["forbidden"], [])
                self.assertEqual(case["env"], case["env_before"])
                self.assertEqual(case["client"].argv, ARGV)
                self.assertEqual(case["foreign"], case["ledger"][:2])
                self.assertTrue(all(query is case["query"] for query in case["query_calls"]))
                self.assertTrue(all(token is case["token"] for token in case["reset_tokens"]))
                self.assertTrue(all(client is case["client"] for client in case["set_clients"]))

        def make_case(self, *, acquired=True, enabled=True, prewarm=True, reset_error=None, close_error=None, release_error=None):
            case = {"events": [], "env": dict(ENV), "foreign": [object(), object()], "owned": object(), "query": object(), "token": object(), "query_calls": [], "set_clients": [], "reset_tokens": [], "forbidden": [], "released": 0}
            if not prewarm:
                case["env"]["EIMEMORY_RECALL_GATEWAY_PREWARM"] = "0"
            case["env_before"] = dict(case["env"])
            case["ledger"] = list(case["foreign"])
            def denied(label):
                case["forbidden"].append(label)
                raise AssertionError(f"Forbidden dependency: {label}")
            class FakeEnv:
                __slots__ = ()
                def get(_, key, default=None):
                    if key not in ENV or default != "0":
                        return denied("environment field")
                    return case["env"][key]
                def __getattr__(_, name):
                    return denied("environment attribute:" + name)
            class FakeOs:
                __slots__ = ()
                environ = FakeEnv()
                def __getattr__(_, name):
                    return denied("os attribute:" + name)
            class FakeQuestion:
                __slots__ = ()
                def search(_, query):
                    case["query_calls"].append(query)
                    self.assertIs(query, case["query"])
                    return True
                def __getattr__(_, name):
                    return denied("question attribute:" + name)
            class FakeSlots:
                __slots__ = ()
                def acquire(_, *, blocking):
                    self.assertIs(blocking, False)
                    case["events"].append("acquire")
                    self.assertNotIn(case["owned"], case["ledger"])
                    if acquired:
                        case["ledger"].append(case["owned"])
                    return acquired
                def release(_):
                    case["events"].append("release")
                    self.assertEqual(case["ledger"], [*case["foreign"], case["owned"]])
                    if release_error is not None:
                        raise release_error
                    case["ledger"].remove(case["owned"])
                    case["released"] += 1
                def __getattr__(_, name):
                    return denied("slots attribute:" + name)
            class FakeClient:
                __slots__ = ()
                argv = ARGV
                def prepare(_):
                    case["events"].append("prepare")
                def close(_):
                    case["events"].append("close")
                    if close_error is not None:
                        raise close_error
                def __getattr__(_, name):
                    return denied("client attribute:" + name)
            class FakePrepared:
                __slots__ = ()
                def set(_, client):
                    case["events"].append("set")
                    case["set_clients"].append(client)
                    self.assertIs(client, case["client"])
                    return case["token"]
                def reset(_, token):
                    case["events"].append("reset")
                    case["reset_tokens"].append(token)
                    self.assertIs(token, case["token"])
                    if reset_error is not None:
                        raise reset_error
                def __getattr__(_, name):
                    return denied("token attribute:" + name)
            case["client"] = FakeClient()
            def factory(purpose):
                self.assertEqual(purpose, "recall")
                case["events"].append("factory")
                return case["client"]
            namespace.update(enabled=lambda: enabled, os=FakeOs(), _bridge_pool_configured=lambda: False, configured_client=lambda: denied("pool client factory"), _QUESTION=FakeQuestion(), _PREPARE_SLOTS=FakeSlots(), llm_client_from_env=factory, _PREPARED=FakePrepared())
            self.cases.append(case)
            return case

        def exercise(self, case, body_error=None):
            caught = None
            try:
                with namespace["prepared_verification"](case["query"]) as yielded:
                    self.assertIsNone(yielded)
                    case["events"].append("body")
                    if body_error is not None:
                        raise body_error
            except BaseException as exc:
                caught = exc
            return caught

        def assert_released_once(self, case):
            self.assertEqual(case["events"].count("release"), 1)
            self.assertEqual(case["released"], 1)
            self.assertEqual(case["ledger"], case["foreign"])

        def test_healthy_exit_keeps_order_and_releases_once(self):
            case = self.make_case()
            self.assertIsNone(self.exercise(case))
            self.assertEqual(case["events"], ["acquire", "factory", "prepare", "set", "body", "reset", "close", "release"])
            self.assert_released_once(case)

        def test_close_errors_release_owned_permit_and_keep_exception_identity(self):
            for error in (RuntimeError("synthetic close"), SyntheticAbort("synthetic close abort")):
                with self.subTest(error_type=type(error).__name__):
                    case = self.make_case(close_error=error)
                    self.assertIs(self.exercise(case), error)
                    self.assert_released_once(case)
                    self.assertEqual(case["events"], ["acquire", "factory", "prepare", "set", "body", "reset", "close", "release"])

        def test_reset_errors_release_but_keep_close_skipped(self):
            for error in (RuntimeError("synthetic reset"), SyntheticAbort("synthetic reset abort")):
                with self.subTest(error_type=type(error).__name__):
                    case = self.make_case(reset_error=error)
                    self.assertIs(self.exercise(case), error)
                    self.assert_released_once(case)
                    self.assertEqual(case["events"], ["acquire", "factory", "prepare", "set", "body", "reset", "release"])

        def test_body_errors_survive_healthy_cleanup(self):
            for error in (RuntimeError("synthetic body"), SyntheticAbort("synthetic body abort")):
                with self.subTest(error_type=type(error).__name__):
                    case = self.make_case()
                    self.assertIs(self.exercise(case, error), error)
                    self.assert_released_once(case)
                    self.assertEqual(case["events"][-3:], ["reset", "close", "release"])

        def test_close_failure_keeps_precedence_over_body_and_still_releases(self):
            body = RuntimeError("synthetic body")
            close = RuntimeError("synthetic close")
            case = self.make_case(close_error=close)
            self.assertIs(self.exercise(case, body), close)
            self.assertIs(close.__context__, body)
            self.assert_released_once(case)

        def test_failed_acquire_preserves_foreign_permits_without_factory(self):
            case = self.make_case(acquired=False)
            self.assertIsNone(self.exercise(case))
            self.assertEqual(case["events"], ["acquire", "body"])
            self.assertEqual(case["ledger"], case["foreign"])
            self.assertEqual(case["released"], 0)

        def test_disabled_preparation_preserves_foreign_permits(self):
            case = self.make_case(enabled=False)
            self.assertIsNone(self.exercise(case))
            self.assertEqual(case["events"], ["body"])
            self.assertEqual(case["query_calls"], [])
            self.assertEqual(case["ledger"], case["foreign"])
            self.assertEqual(case["released"], 0)

        def test_disabled_fake_prewarm_key_preserves_foreign_permits(self):
            case = self.make_case(prewarm=False)
            self.assertIsNone(self.exercise(case))
            self.assertEqual(case["events"], ["body"])
            self.assertEqual(case["ledger"], case["foreign"])
            self.assertEqual(case["released"], 0)

        def test_release_error_is_visible_once_without_success_claim(self):
            release = RuntimeError("synthetic release")
            case = self.make_case(release_error=release)
            self.assertIs(self.exercise(case), release)
            self.assertEqual(case["events"].count("release"), 1)
            self.assertEqual(case["released"], 0)
            self.assertEqual(case["ledger"], [*case["foreign"], case["owned"]])

        def test_release_error_after_close_failure_has_finally_precedence(self):
            close = RuntimeError("synthetic close")
            release = RuntimeError("synthetic release")
            case = self.make_case(close_error=close, release_error=release)
            self.assertIs(self.exercise(case), release)
            self.assertIs(release.__context__, close)
            self.assertEqual(case["events"][-3:], ["reset", "close", "release"])
            self.assertEqual(case["events"].count("release"), 1)
            self.assertEqual(case["released"], 0)
            self.assertEqual(case["ledger"], [*case["foreign"], case["owned"]])

        def test_release_error_after_reset_failure_keeps_close_skipped(self):
            reset = RuntimeError("synthetic reset")
            release = RuntimeError("synthetic release")
            case = self.make_case(reset_error=reset, release_error=release)
            self.assertIs(self.exercise(case), release)
            self.assertIs(release.__context__, reset)
            self.assertEqual(case["events"][-2:], ["reset", "release"])
            self.assertNotIn("close", case["events"])
            self.assertEqual(case["events"].count("release"), 1)
            self.assertEqual(case["released"], 0)
            self.assertEqual(case["ledger"], [*case["foreign"], case["owned"]])
    return PrewarmPermitCleanupTests


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--expected-sha256", required=True)
    args = parser.parse_args()
    namespace, forbidden = extract_shell(args.source, args.expected_sha256)
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(permit_tests(namespace, forbidden))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    raise SystemExit(0 if result.wasSuccessful() else 1)


if __name__ == "__main__":
    main()
