"""AST-only Runtime.create ownership with inert root/store/constructor dependencies."""
from __future__ import annotations

import argparse
import ast
import builtins
import copy
import hashlib
from pathlib import Path
import unittest

BASELINE_SHA256 = "54bb41301c712207a35e24f80be24b60f7d3f513a5dc656e7a4605c16ccddf7f"


class FakeAbort(BaseException):
    pass


class Borrowed:
    def __init__(self):
        self.accesses = []
    def __getattr__(self, name):
        self.accesses.append(name)
        raise AssertionError("Borrowed objects must not be used or closed")


class FakeStore:
    def __init__(self):
        self.close_attempts = 0
        self.closed = False
        self.failure = None
    def close(self):
        self.close_attempts += 1
        if self.failure is not None:
            raise self.failure
        self.closed = True
    def __getattr__(self, name):
        raise AssertionError(f"Unexpected store operation {name}")


class FakeInstance:
    def __init__(self):
        object.__setattr__(self, "assignments", [])
    def __setattr__(self, name, value):
        if name != "candidate_source_error":
            raise AssertionError("Only the existing result metadata assignment is allowed")
        self.assignments.append((name, value))
        object.__setattr__(self, name, value)
    def __getattr__(self, name):
        raise AssertionError(f"Unexpected instance access {name}")


def extract_shell(path, expected):
    data = path.read_bytes(); actual = hashlib.sha256(data).hexdigest()
    if actual != expected:
        raise ValueError(f"Pinned source digest mismatch: {actual}")
    module = ast.parse(data, filename=str(path))
    owners = [n for n in module.body if isinstance(n, ast.ClassDef) and n.name == "Runtime"]
    if len(owners) != 1:
        raise ValueError("Expected one Runtime class")
    found = [n for n in owners[0].body if isinstance(n, ast.FunctionDef) and n.name == "create"]
    if len(found) != 1:
        raise ValueError("Expected complete create method")
    method = found[0]
    if len(method.decorator_list) != 1 or not isinstance(method.decorator_list[0], ast.Name) or method.decorator_list[0].id != "classmethod":
        raise ValueError("Preserve the exact classmethod decorator")
    if actual == BASELINE_SHA256 and (method.decorator_list[0].lineno, method.lineno, method.end_lineno) != (164, 165, 195):
        raise ValueError("Baseline boundary mismatch")
    forbidden = []
    def fenced_import(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "__future__" and level == 0:
            return builtins.__import__(name, globals, locals, fromlist, level)
        forbidden.append(name)
        raise AssertionError("Provider/environment/project imports are forbidden")
    namespace = {"__builtins__": {**vars(builtins), "__import__": fenced_import}}
    isolated = ast.fix_missing_locations(ast.Module(body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0), ast.ClassDef(name="Shell", bases=[], keywords=[], body=[copy.deepcopy(method)], decorator_list=[])], type_ignores=[]))
    exec(compile(isolated, str(path), "exec"), namespace)
    print(f"SOURCE_SHA256 {actual}")
    print(f"EXTRACTED create decorator L{method.decorator_list[0].lineno}, function L{method.lineno}-{method.end_lineno}")
    return namespace, forbidden


def ownership_tests(namespace, forbidden):
    class RuntimeFactoryOwnershipTests(unittest.TestCase):
        def setUp(self):
            self.root = object(); self.final_root = object()
            self.source = Borrowed(); self.engine = Borrowed()
            self.owned = FakeStore(); self.foreign_store = FakeStore()
            self.instance = FakeInstance()
            self.root_calls = []; self.store_calls = []; self.constructor_calls = []
            self.root_failure = None; self.store_failure = None; self.constructor_failure = None
            def fake_root(value):
                self.root_calls.append(value)
                if self.root_failure is not None:
                    raise self.root_failure
                return self.final_root
            def fake_store(value):
                self.store_calls.append(value)
                if self.store_failure is not None:
                    raise self.store_failure
                return self.owned
            def fake_cls(store, *, candidate_source, recall_engine, profile):
                self.constructor_calls.append((store, candidate_source, recall_engine, profile))
                if self.constructor_failure is not None:
                    raise self.constructor_failure
                return self.instance
            self.fake_cls = fake_cls
            namespace.update(default_root=fake_root, RuntimeStore=fake_store)
        def tearDown(self):
            self.assertEqual(forbidden, [])
            self.assertEqual(self.source.accesses, [])
            self.assertEqual(self.engine.accesses, [])
            self.assertEqual(self.foreign_store.close_attempts, 0)
        def create(self, *, source=True, engine=True):
            if not source and not engine:
                raise AssertionError("The optional provider/environment branch is outside this shell")
            return namespace["Shell"].create.__func__(self.fake_cls, root=self.root, candidate_source=self.source if source else None, recall_engine=self.engine if engine else None, profile="synthetic profile")
        def error(self):
            try:
                self.create()
            except BaseException as exc:
                return exc
            self.fail("Expected a stored synthetic error")
        def test_constructor_failure_closes_only_its_new_store_once(self):
            failure = ValueError("synthetic constructor conflict")
            self.constructor_failure = failure
            self.assertIs(self.error(), failure)
            self.assertEqual(self.owned.close_attempts, 1)
            self.assertTrue(self.owned.closed)
            self.assertEqual(self.constructor_calls, [(self.owned, self.source, self.engine, "synthetic profile")])
            self.assertEqual(self.instance.assignments, [])
        def test_constructor_baseexception_gets_owned_cleanup(self):
            failure = FakeAbort("synthetic abort; no signal")
            self.constructor_failure = failure
            self.assertIs(self.error(), failure)
            self.assertEqual(self.owned.close_attempts, 1)
            self.assertTrue(self.owned.closed)
        def test_ordinary_close_failure_keeps_primary_error_identity(self):
            for failure in (ValueError("synthetic constructor error"), FakeAbort("synthetic constructor abort")):
                with self.subTest(kind=type(failure).__name__):
                    self.owned = FakeStore(); self.owned.failure = RuntimeError("synthetic failed cleanup")
                    self.constructor_failure = failure
                    self.assertIs(self.error(), failure)
                    self.assertEqual(self.owned.close_attempts, 1)
                    self.assertFalse(self.owned.closed)
        def assert_success(self, *, source, engine):
            result = self.create(source=source, engine=engine)
            self.assertIs(result, self.instance)
            self.assertEqual(self.owned.close_attempts, 0)
            self.assertFalse(self.owned.closed)
            self.assertEqual(self.root_calls, [self.root])
            self.assertEqual(self.store_calls, [self.final_root])
            self.assertEqual(self.constructor_calls, [(self.owned, self.source if source else None, self.engine if engine else None, "synthetic profile")])
            self.assertEqual(self.instance.assignments, [("candidate_source_error", "")])
        def test_success_with_borrowed_source_hands_off_store(self):
            self.assert_success(source=True, engine=False)
        def test_success_with_borrowed_engine_hands_off_store(self):
            self.assert_success(source=False, engine=True)
        def test_root_failure_precedes_store_ownership(self):
            failure = RuntimeError("synthetic root failure"); self.root_failure = failure
            self.assertIs(self.error(), failure)
            self.assertEqual(self.root_calls, [self.root])
            self.assertEqual(self.store_calls, []); self.assertEqual(self.constructor_calls, [])
            self.assertEqual(self.owned.close_attempts, 0)
        def test_store_factory_failure_does_not_close_unowned_objects(self):
            failure = RuntimeError("synthetic store factory failure"); self.store_failure = failure
            self.assertIs(self.error(), failure)
            self.assertEqual(self.store_calls, [self.final_root])
            self.assertEqual(self.constructor_calls, [])
            self.assertEqual(self.owned.close_attempts, 0)
    return RuntimeFactoryOwnershipTests


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
