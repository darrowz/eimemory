"""AST-only wrapper transaction ownership/depth probe; no domain or real SQL."""
from __future__ import annotations

import argparse
import ast
import builtins
import copy
import hashlib
from pathlib import Path
from types import SimpleNamespace
import unittest


BASELINE_SHA256 = "478e347b0a2a42541a6dff592c1c26aacd4ce7c1cf03acd21140856b5fca80f7"
METHODS = {"_write": (302, 319), "_read": (340, 353)}


class FakeAbort(BaseException):
    pass


class FakeBeginFailure(Exception):
    pass


class FakeLock:
    def __init__(self):
        self.depth = 0
    def __enter__(self):
        self.depth += 1
        return self
    def __exit__(self, *_args):
        self.depth -= 1


class FakeConnection:
    def __init__(self, lock, *, caller_transaction=False):
        self.lock = lock
        self.in_transaction = caller_transaction
        self.caller_pending = object() if caller_transaction else None
        self.calls = []
        self.begin_failure = None
        self.commit_failure = None
        self.rollback_failure = None
    def held(self):
        if self.lock.depth <= 0:
            raise AssertionError("Wrapper operation requires its existing fake lock")
    def execute(self, sql):
        self.held()
        if sql not in ("BEGIN", "BEGIN IMMEDIATE"):
            raise AssertionError("Domain SQL is excluded")
        self.calls.append(sql)
        if self.begin_failure is not None:
            error, self.begin_failure = self.begin_failure, None
            raise error
        if self.in_transaction:
            raise FakeBeginFailure("fake caller transaction already open")
        self.in_transaction = True
    def commit(self):
        self.held(); self.calls.append("commit")
        if self.commit_failure is not None:
            raise self.commit_failure
        self.in_transaction = False
        self.caller_pending = None
    def rollback(self):
        self.held(); self.calls.append("rollback")
        if self.rollback_failure is not None:
            raise self.rollback_failure
        self.in_transaction = False
        self.caller_pending = None


def extract_shell(path, expected):
    data = path.read_bytes(); actual = hashlib.sha256(data).hexdigest()
    if actual != expected:
        raise ValueError(f"Pinned source digest mismatch: {actual}")
    module = ast.parse(data, filename=str(path))
    owners = [n for n in module.body if isinstance(n, ast.ClassDef) and n.name == "CodeEvolutionStore"]
    if len(owners) != 1:
        raise ValueError("Expected one transaction-wrapper owner")
    selected = []
    for name, span in METHODS.items():
        found = [n for n in owners[0].body if isinstance(n, ast.FunctionDef) and n.name == name]
        if len(found) != 1 or found[0].decorator_list:
            raise ValueError("Expected complete undecorated wrapper")
        node = found[0]
        if actual == BASELINE_SHA256 and (node.lineno, node.end_lineno) != span:
            raise ValueError("Baseline wrapper range mismatch")
        if any(isinstance(n, (ast.Import, ast.ImportFrom)) for n in ast.walk(node)):
            raise ValueError("Target/domain imports are forbidden")
        selected.append(copy.deepcopy(node))
        print(f"EXTRACTED {name} L{node.lineno}-{node.end_lineno}")
    forbidden = []
    def guarded_import(name, *args, **kwargs):
        if name == "__future__":
            return builtins.__import__(name, *args, **kwargs)
        forbidden.append(name)
        raise AssertionError("No target/domain import is allowed")
    namespace = {"__builtins__": {**vars(builtins), "__import__": guarded_import}}
    isolated = ast.fix_missing_locations(ast.Module(body=[
        ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0),
        ast.ClassDef(name="Shell", bases=[], keywords=[], body=selected, decorator_list=[]),
    ], type_ignores=[]))
    exec(compile(isolated, str(path), "exec"), namespace)
    print(f"SOURCE_SHA256 {actual}")
    return namespace["Shell"], forbidden


def depth_tests(shell_type, forbidden):
    class TransactionDepthTests(unittest.TestCase):
        def make(self, *, depth=0, caller_transaction=False):
            shell = shell_type()
            shell.lock = FakeLock()
            shell._tx_depth = SimpleNamespace(value=depth)
            shell.conn = FakeConnection(shell.lock, caller_transaction=caller_transaction)
            return shell
        def tearDown(self):
            self.assertEqual(forbidden, [])
        def test_failed_begin_restores_depth_for_next_outer_attempt(self):
            for name, statement in (("_write", "BEGIN IMMEDIATE"), ("_read", "BEGIN")):
                with self.subTest(wrapper=name):
                    shell = self.make(); failure = FakeBeginFailure("synthetic begin failure")
                    shell.conn.begin_failure = failure; calls = []
                    with self.assertRaises(FakeBeginFailure) as raised:
                        getattr(shell, name)(lambda: calls.append("callback"))
                    self.assertIs(raised.exception, failure)
                    self.assertEqual(shell._tx_depth.value, 0)
                    self.assertEqual(calls, [])
                    self.assertEqual(shell.conn.calls, [statement])
                    result = object()
                    self.assertIs(getattr(shell, name)(lambda: result), result)
                    self.assertEqual(shell.conn.calls[:2], [statement, statement])
                    self.assertEqual(shell._tx_depth.value, 0)
        def test_failed_begin_does_not_touch_caller_transaction(self):
            for name in ("_write", "_read"):
                with self.subTest(wrapper=name):
                    shell = self.make(caller_transaction=True); pending = shell.conn.caller_pending; calls = []
                    with self.assertRaises(FakeBeginFailure):
                        getattr(shell, name)(lambda: calls.append("callback"))
                    self.assertEqual(shell._tx_depth.value, 0)
                    self.assertTrue(shell.conn.in_transaction)
                    self.assertIs(shell.conn.caller_pending, pending)
                    self.assertEqual(calls, [])
                    self.assertNotIn("commit", shell.conn.calls)
                    self.assertNotIn("rollback", shell.conn.calls)
        def test_outer_write_success_commits_once_and_returns_opaque_value(self):
            shell = self.make(); result = object()
            def callback():
                self.assertEqual(shell._tx_depth.value, 1)
                self.assertTrue(shell.conn.in_transaction)
                return result
            self.assertIs(shell._write(callback), result)
            self.assertEqual(shell.conn.calls, ["BEGIN IMMEDIATE", "commit"])
            self.assertEqual(shell._tx_depth.value, 0)
        def test_owned_write_failures_roll_back_and_preserve_error_identity(self):
            for failure in (ValueError("synthetic callback error"), FakeAbort("synthetic callback abort")):
                with self.subTest(error=type(failure).__name__):
                    shell = self.make()
                    def callback():
                        self.assertEqual(shell._tx_depth.value, 1)
                        raise failure
                    with self.assertRaises(type(failure)) as raised:
                        shell._write(callback)
                    self.assertIs(raised.exception, failure)
                    self.assertEqual(shell.conn.calls, ["BEGIN IMMEDIATE", "rollback"])
                    self.assertEqual(shell._tx_depth.value, 0)
                    self.assertFalse(shell.conn.in_transaction)
        def test_nested_success_and_handled_failure_preserve_outer_owner(self):
            shell = self.make(); result = object(); failure = ValueError("nested fake failure")
            def outer():
                def failing():
                    self.assertEqual(shell._tx_depth.value, 2)
                    raise failure
                with self.assertRaises(ValueError) as raised:
                    shell._write(failing)
                self.assertIs(raised.exception, failure)
                self.assertEqual(shell._tx_depth.value, 1)
                self.assertTrue(shell.conn.in_transaction)
                return shell._read(lambda: shell._write(lambda: result))
            self.assertIs(shell._write(outer), result)
            self.assertEqual(shell.conn.calls, ["BEGIN IMMEDIATE", "commit"])
            self.assertEqual(shell._tx_depth.value, 0)
        def test_read_success_and_callback_errors_roll_back_only_owned_read(self):
            for failure in (None, ValueError("read callback error"), FakeAbort("read callback abort")):
                with self.subTest(error=type(failure).__name__):
                    shell = self.make(); result = object()
                    def callback():
                        if failure is not None:
                            raise failure
                        return result
                    if failure is None:
                        self.assertIs(shell._read(callback), result)
                    else:
                        with self.assertRaises(type(failure)) as raised:
                            shell._read(callback)
                        self.assertIs(raised.exception, failure)
                    self.assertEqual(shell.conn.calls, ["BEGIN", "rollback"])
                    self.assertEqual(shell._tx_depth.value, 0)
        def test_read_cleanup_failure_restores_depth_and_keeps_precedence(self):
            for callback_failure in (None, ValueError("original callback error")):
                with self.subTest(callback_error=callback_failure is not None):
                    shell = self.make(); cleanup = RuntimeError("synthetic read rollback failure")
                    shell.conn.rollback_failure = cleanup
                    def callback():
                        if callback_failure is not None:
                            raise callback_failure
                        return object()
                    with self.assertRaises(RuntimeError) as raised:
                        shell._read(callback)
                    self.assertIs(raised.exception, cleanup)
                    self.assertEqual(shell._tx_depth.value, 0)
                    self.assertTrue(shell.conn.in_transaction)
                    self.assertEqual(shell.conn.calls, ["BEGIN", "rollback"])
        def test_write_cleanup_failure_restores_depth_and_keeps_precedence(self):
            shell = self.make(); cleanup = RuntimeError("synthetic write rollback failure")
            shell.conn.rollback_failure = cleanup
            def callback():
                raise ValueError("original callback error")
            with self.assertRaises(RuntimeError) as raised:
                shell._write(callback)
            self.assertIs(raised.exception, cleanup)
            self.assertEqual(shell._tx_depth.value, 0)
            self.assertTrue(shell.conn.in_transaction)
            self.assertEqual(shell.conn.calls, ["BEGIN IMMEDIATE", "rollback"])
        def test_commit_failure_attempts_owned_cleanup_and_restores_depth(self):
            for failure in (RuntimeError("commit failure"), FakeAbort("commit abort")):
                with self.subTest(error=type(failure).__name__):
                    shell = self.make(); shell.conn.commit_failure = failure
                    with self.assertRaises(type(failure)) as raised:
                        shell._write(lambda: object())
                    self.assertIs(raised.exception, failure)
                    self.assertEqual(shell.conn.calls, ["BEGIN IMMEDIATE", "commit", "rollback"])
                    self.assertEqual(shell._tx_depth.value, 0)
        def test_borrowed_depth_never_takes_transaction_ownership(self):
            for name in ("_write", "_read"):
                for failure in (None, FakeAbort("nested abort")):
                    with self.subTest(wrapper=name, failing=failure is not None):
                        shell = self.make(depth=4, caller_transaction=True); pending = shell.conn.caller_pending; result = object()
                        def callback():
                            self.assertEqual(shell._tx_depth.value, 5)
                            if failure is not None:
                                raise failure
                            return result
                        if failure is None:
                            self.assertIs(getattr(shell, name)(callback), result)
                        else:
                            with self.assertRaises(FakeAbort) as raised:
                                getattr(shell, name)(callback)
                            self.assertIs(raised.exception, failure)
                        self.assertEqual(shell._tx_depth.value, 4)
                        self.assertTrue(shell.conn.in_transaction)
                        self.assertIs(shell.conn.caller_pending, pending)
                        self.assertEqual(shell.conn.calls, [])
    return TransactionDepthTests


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--expected-sha256", required=True)
    args = parser.parse_args()
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(depth_tests(*extract_shell(args.source, args.expected_sha256)))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    failed = {getattr(test, "test_case", test)._testMethodName for test, _ in [*result.failures, *result.errors]}
    print(f"FAILING_METHODS {len(failed)}")
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
