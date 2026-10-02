"""AST-only owned-connection return probe; every resource/configuration is fake."""
from __future__ import annotations

import argparse
import ast
import builtins
import copy
import hashlib
from pathlib import Path
import unittest


BASELINE_SHA256 = "a76c533dcca8c252482742565b74f7ece6dd154fac74224bd33f2bded32cbeaa"
METHODS = (("PostgresCandidateRepository", "_release_idle", "RepositoryShell", (459, 471)), ("_GatedConnection", "close", "GatedShell", (1349, 1359)))


def extract_shells(path, expected):
    data = path.read_bytes()
    actual = hashlib.sha256(data).hexdigest()
    if actual != expected:
        raise ValueError(f"Pinned source digest mismatch: {actual}")
    module = ast.parse(data, filename=str(path))
    body = [ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0)]
    for owner, name, shell, baseline_range in METHODS:
        owners = [node for node in module.body if isinstance(node, ast.ClassDef) and node.name == owner]
        if len(owners) != 1:
            raise ValueError(f"Expected one owner: {owner}")
        selected = [node for node in owners[0].body if isinstance(node, ast.FunctionDef) and node.name == name]
        if len(selected) != 1 or selected[0].decorator_list:
            raise ValueError(f"Expected one complete undecorated method: {owner}.{name}")
        method = selected[0]
        if actual == BASELINE_SHA256 and (method.lineno, method.end_lineno) != baseline_range:
            raise ValueError("Baseline method range mismatch")
        if any(isinstance(node, (ast.Import, ast.ImportFrom)) for node in ast.walk(method)):
            raise ValueError("Source imports are outside this shell")
        body.append(ast.ClassDef(name=shell, bases=[], keywords=[], body=[copy.deepcopy(method)], decorator_list=[]))
        print(f"EXTRACTED {owner}.{name} L{method.lineno}-{method.end_lineno}")
    isolated = ast.fix_missing_locations(ast.Module(body=body, type_ignores=[]))
    forbidden = []
    def guarded_import(name, *args, **kwargs):
        if name == "__future__":
            return builtins.__import__(name, *args, **kwargs)
        forbidden.append(name)
        raise AssertionError("No target or driver imports are allowed")
    namespace = {"__builtins__": {**vars(builtins), "__import__": guarded_import}}
    exec(compile(isolated, str(path), "exec"), namespace)
    print(f"SOURCE_SHA256 {actual}")
    return namespace["RepositoryShell"], namespace["GatedShell"], forbidden


class FakeLock:
    def __enter__(self):
        return self
    def __exit__(self, *_args):
        pass


class FakeConfig:
    pool_size = 1
    def __init__(self):
        self.forbidden = []
    def __getattr__(self, name):
        self.forbidden.append(name)
        raise AssertionError(f"Forbidden configuration field: {name}")


class FakeGate:
    def __init__(self, trace):
        self.trace = trace
        self.owned = 1
        self.releases = 0
    def release(self):
        if self.owned != 1:
            raise AssertionError("This lease owns only one gate permit")
        self.trace.append("gate-release")
        self.owned = 0
        self.releases += 1


class FakeRaw:
    def __init__(self, trace, *, reset_failure=False, close_failure=False):
        self.trace = trace
        self.pending = True
        self.closed = False
        self.reset_failure = reset_failure
        self.close_failure = close_failure
    def rollback(self):
        self.trace.append("rollback")
        if self.reset_failure:
            raise ValueError("fake reset failure")
        self.pending = False
    def close(self):
        self.trace.append("raw-close")
        if self.close_failure:
            raise ValueError("fake close failure")
        self.closed = True


class MissingRollbackRaw:
    def __init__(self, trace):
        self.trace = trace
        self.closed = False
    def close(self):
        self.trace.append("raw-close")
        self.closed = True


class FakePool(list):
    def __init__(self, trace):
        super().__init__()
        self.trace = trace
    def append(self, connection):
        self.trace.append("pool")
        super().append(connection)


def return_tests(repository_type, gated_type, forbidden_imports):
    class ConnectionReturnTests(unittest.TestCase):
        def setUp(self):
            self.trace = []
            self.repository = repository_type()
            self.repository.config = FakeConfig()
            self.repository._idle_lock = FakeLock()
            self.repository._idle_pool = FakePool(self.trace)
            self.gate = FakeGate(self.trace)
        def tearDown(self):
            self.assertEqual(forbidden_imports, [])
            self.assertEqual(self.repository.config.forbidden, [])
        def wrapper(self, raw, *, pooled=True):
            wrapper = gated_type()
            wrapper._closed = False
            wrapper._repository = self.repository if pooled else None
            wrapper._connection = raw
            wrapper._gate = self.gate
            return wrapper
        def assert_discarded(self, raw):
            self.assertNotIn(raw, self.repository._idle_pool)
            self.assertEqual(self.gate.releases, 1)
            self.assertEqual(self.gate.owned, 0)
        def test_reset_succeeds_before_pool_return(self):
            raw = FakeRaw(self.trace)
            self.wrapper(raw).close()
            self.assertFalse(raw.pending)
            self.assertEqual(self.repository._idle_pool, [raw])
            self.assertEqual(self.trace, ["rollback", "pool", "gate-release"])
            self.assertFalse(raw.closed)
        def test_reset_failure_closes_and_discards(self):
            raw = FakeRaw(self.trace, reset_failure=True)
            self.wrapper(raw).close()
            self.assert_discarded(raw)
            self.assertTrue(raw.closed)
            self.assertEqual(self.trace, ["rollback", "raw-close", "gate-release"])
        def test_full_pool_still_closes_and_releases_once(self):
            occupied = object()
            list.append(self.repository._idle_pool, occupied)
            raw = FakeRaw(self.trace)
            self.wrapper(raw).close()
            self.assert_discarded(raw)
            self.assertTrue(raw.closed)
            self.assertEqual(self.repository._idle_pool, [occupied])
            self.assertEqual(self.trace[-2:], ["raw-close", "gate-release"])
        def test_missing_rollback_is_not_reused(self):
            raw = MissingRollbackRaw(self.trace)
            self.wrapper(raw).close()
            self.assert_discarded(raw)
            self.assertTrue(raw.closed)
        def test_noncallable_rollback_is_not_reused(self):
            raw = FakeRaw(self.trace)
            raw.rollback = None
            self.wrapper(raw).close()
            self.assert_discarded(raw)
            self.assertTrue(raw.closed)
        def test_repeated_gated_close_resets_and_releases_once(self):
            raw = FakeRaw(self.trace)
            wrapper = self.wrapper(raw)
            wrapper.close()
            wrapper.close()
            self.assertEqual(self.trace, ["rollback", "pool", "gate-release"])
            self.assertEqual(self.gate.releases, 1)
        def test_close_failure_never_reinserts_failed_connection(self):
            raw = FakeRaw(self.trace, reset_failure=True, close_failure=True)
            self.wrapper(raw).close()
            self.assert_discarded(raw)
            self.assertFalse(raw.closed)
            self.assertEqual(self.trace, ["rollback", "raw-close", "gate-release"])
        def test_nonpooled_gated_close_keeps_existing_cleanup(self):
            raw = FakeRaw(self.trace)
            self.wrapper(raw, pooled=False).close()
            self.assertTrue(raw.closed)
            self.assertEqual(self.trace, ["raw-close", "gate-release"])
            self.assertEqual(self.gate.releases, 1)
    return ConnectionReturnTests


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--expected-sha256", required=True)
    args = parser.parse_args()
    classes = extract_shells(args.source, args.expected_sha256)
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(return_tests(*classes))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
