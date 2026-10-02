"""AST-only update_index ownership; loader may fail or return only an empty list."""
from __future__ import annotations

import argparse
import ast
import builtins
import copy
import hashlib
from pathlib import Path
import unittest

BASELINE_SHA256 = "ac4b2df6f02bc39e3691a46e9ce94ac5dd46e828d0aaf171346513baf7f961ac"


class FakeAbort(BaseException):
    pass


class FakeConnection:
    def __init__(self, calls):
        self.calls = calls; self.commits = 0; self.closes = 0; self.closed = False
        self.commit_error = None; self.close_error = None
    def commit(self):
        self.calls.append("commit"); self.commits += 1
        if self.commit_error is not None:
            raise self.commit_error
    def close(self):
        self.calls.append("close"); self.closes += 1
        if self.close_error is not None:
            raise self.close_error
        self.closed = True
    def __getattr__(self, name):
        raise AssertionError(f"SQL and unexpected connection operations are forbidden: {name}")


def extract_shell(path, expected):
    data = path.read_bytes(); actual = hashlib.sha256(data).hexdigest()
    if actual != expected:
        raise ValueError(f"Pinned source digest mismatch: {actual}")
    module = ast.parse(data, filename=str(path))
    owners = [n for n in module.body if isinstance(n, ast.ClassDef) and n.name == "QmdCompatRuntime"]
    if len(owners) != 1:
        raise ValueError("Expected one runtime class")
    found = [n for n in owners[0].body if isinstance(n, ast.FunctionDef) and n.name == "update_index"]
    if len(found) != 1 or found[0].decorator_list:
        raise ValueError("Expected one complete undecorated method")
    method = found[0]
    if actual == BASELINE_SHA256 and (method.lineno, method.end_lineno) != (78, 119):
        raise ValueError("Unexpected baseline method boundary")
    forbidden = []
    def fenced_import(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "__future__" and level == 0:
            return builtins.__import__(name, globals, locals, fromlist, level)
        forbidden.append(name)
        raise AssertionError("Target/configuration/SQL/file helper imports are forbidden")
    namespace = {"__builtins__": {**vars(builtins), "__import__": fenced_import}}
    isolated = ast.fix_missing_locations(ast.Module(body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0), ast.ClassDef(name="Shell", bases=[], keywords=[], body=[copy.deepcopy(method)], decorator_list=[])], type_ignores=[]))
    exec(compile(isolated, str(path), "exec"), namespace)
    def forbidden_attribute(_self, name):
        forbidden.append(name)
        raise AssertionError(f"Unapproved runtime helper {name}")
    namespace["Shell"].__getattr__ = forbidden_attribute
    print(f"SOURCE_SHA256 {actual}")
    print(f"EXTRACTED update_index L{method.lineno}-{method.end_lineno}")
    return namespace["Shell"], forbidden


def ownership_tests(shell_type, forbidden):
    class QmdUpdateOwnershipTests(unittest.TestCase):
        def setUp(self):
            self.runtime = shell_type(); self.calls = []
            self.owned = FakeConnection(self.calls); self.foreign = FakeConnection([])
            self.connect_error = None; self.load_error = None
            def connect():
                self.calls.append("connect")
                if self.connect_error is not None:
                    raise self.connect_error
                return self.owned
            def load():
                self.calls.append("load")
                if self.load_error is not None:
                    raise self.load_error
                return []
            self.runtime._connect = connect
            self.runtime._load_collections = load
        def tearDown(self):
            self.assertEqual(forbidden, [])
            self.assertEqual(self.foreign.commits, 0); self.assertEqual(self.foreign.closes, 0)
        def error(self):
            try:
                self.runtime.update_index()
            except BaseException as exc:
                return exc
            self.fail("Expected stored synthetic exception")
        def assert_loader_cleanup(self, failure):
            self.load_error = failure
            self.assertIs(self.error(), failure)
            self.assertEqual(self.calls, ["connect", "load", "close"])
            self.assertEqual(self.owned.commits, 0); self.assertEqual(self.owned.closes, 1)
            self.assertTrue(self.owned.closed)
        def test_ordinary_loader_failure_closes_exact_owned_connection(self):
            self.assert_loader_cleanup(ValueError("synthetic loader failure"))
        def test_loader_baseexception_gets_same_finally_cleanup(self):
            self.assert_loader_cleanup(FakeAbort("synthetic abort; no signal"))
        def test_empty_list_success_keeps_order_and_zero_result(self):
            self.assertEqual(self.runtime.update_index(), {"ok": True, "collections": 0, "documents": 0, "skipped": 0})
            self.assertEqual(self.calls, ["connect", "load", "commit", "close"])
            self.assertEqual(self.owned.commits, 1); self.assertEqual(self.owned.closes, 1)
            self.assertTrue(self.owned.closed)
        def test_connect_failure_precedes_resource_ownership(self):
            failure = RuntimeError("synthetic connect failure"); self.connect_error = failure
            self.assertIs(self.error(), failure)
            self.assertEqual(self.calls, ["connect"])
            self.assertEqual(self.owned.commits, 0); self.assertEqual(self.owned.closes, 0)
        def test_commit_failure_still_closes_and_preserves_error(self):
            failure = RuntimeError("synthetic commit failure"); self.owned.commit_error = failure
            self.assertIs(self.error(), failure)
            self.assertEqual(self.calls, ["connect", "load", "commit", "close"])
            self.assertEqual(self.owned.commits, 1); self.assertEqual(self.owned.closes, 1)
            self.assertTrue(self.owned.closed)
        def test_close_failure_stays_visible_without_retry_or_success_claim(self):
            failure = RuntimeError("synthetic close failure"); self.owned.close_error = failure
            self.assertIs(self.error(), failure)
            self.assertEqual(self.calls, ["connect", "load", "commit", "close"])
            self.assertEqual(self.owned.closes, 1)
            self.assertFalse(self.owned.closed)
        def test_loader_and_close_failure_keep_finally_exception_precedence(self):
            for primary in (ValueError("synthetic loader failure"), FakeAbort("synthetic loader abort")):
                with self.subTest(kind=type(primary).__name__):
                    self.calls.clear(); self.owned = FakeConnection(self.calls)
                    cleanup = RuntimeError("synthetic close failure")
                    self.load_error = primary; self.owned.close_error = cleanup
                    self.assertIs(self.error(), cleanup)
                    self.assertIs(cleanup.__context__, primary)
                    self.assertEqual(self.calls, ["connect", "load", "close"])
                    self.assertEqual(self.owned.commits, 0); self.assertEqual(self.owned.closes, 1)
                    self.assertFalse(self.owned.closed)
        def test_commit_and_close_failure_keep_existing_context(self):
            primary = ValueError("synthetic commit failure"); cleanup = RuntimeError("synthetic close failure")
            self.owned.commit_error = primary; self.owned.close_error = cleanup
            self.assertIs(self.error(), cleanup)
            self.assertIs(cleanup.__context__, primary)
            self.assertEqual(self.calls, ["connect", "load", "commit", "close"])
            self.assertEqual(self.owned.closes, 1); self.assertFalse(self.owned.closed)
    return QmdUpdateOwnershipTests


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--expected-sha256", required=True)
    args = parser.parse_args()
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(ownership_tests(*extract_shell(args.source, args.expected_sha256)))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
