"""AST-only leased-worker accounting probe; all workers and synchronization are fake."""
from __future__ import annotations

import argparse
import ast
import builtins
import copy
import hashlib
from pathlib import Path
from types import SimpleNamespace
import unittest


BASELINE_SHA256 = "d031bff9cf569bfc78d7aade3bc8618e665ada73d5d4345f96b554345ca2844b"
METHODS = {"warm": (126, 144), "complete": (146, 171)}


class FakeEmpty(Exception):
    pass


class FakeLock:
    def __init__(self):
        self.depth = 0
    def __enter__(self):
        self.depth += 1
        return self
    def __exit__(self, *_args):
        self.depth -= 1


class FakeQueue:
    def __init__(self):
        self.items = []
        self.waits = []
    def empty(self):
        return not self.items
    def put_nowait(self, worker):
        self.items.append(worker)
    def get_nowait(self):
        if not self.items:
            raise FakeEmpty("fake empty queue")
        return self.items.pop(0)
    def get(self, *, timeout):
        self.waits.append(timeout)
        return self.get_nowait()


class FakeProcess:
    def __init__(self):
        self.status = None
    def poll(self):
        return self.status


class FakeWorker:
    def __init__(self, outcome, payload):
        self.closed = False
        self.process = FakeProcess()
        self.outcome = outcome
        self.expected_payload = payload
        self.calls = 0
        self.closes = 0
        self.dead_at_return = False
    def close(self):
        if not self.closed:
            self.closes += 1
            self.closed = True
            self.process.status = 1
    def call(self, payload, timeout):
        if payload is not self.expected_payload or self.closed or self.process.poll() is not None:
            raise AssertionError("Unexpected payload access or reuse of a dead fake worker")
        self.calls += 1
        if isinstance(self.outcome, Exception):
            self.close()
            raise self.outcome
        if self.dead_at_return:
            self.process.status = 0
        return self.outcome


class FakeFactory:
    def __init__(self, pool, payload):
        self.pool = pool
        self.payload = payload
        self.outcomes = []
        self.created = []
        self.max_live = 0
    def __call__(self, argv):
        if argv is not self.pool.argv or self.pool.lock.depth <= 0:
            raise AssertionError("Allocation must use only opaque argv while holding the pool lock")
        result = self.outcomes.pop(0) if self.outcomes else object()
        worker = FakeWorker(result, self.payload)
        self.created.append(worker)
        live = sum(not item.closed and item.process.poll() is None for item in self.created)
        self.max_live = max(self.max_live, live)
        if live > 2:
            raise AssertionError("Created more than two represented live workers")
        return worker


def extract_shell(path, expected):
    data = path.read_bytes(); actual = hashlib.sha256(data).hexdigest()
    if actual != expected:
        raise ValueError(f"Pinned source digest mismatch: {actual}")
    module = ast.parse(data, filename=str(path))
    owners = [n for n in module.body if isinstance(n, ast.ClassDef) and n.name == "_Pool"]
    if len(owners) != 1:
        raise ValueError("Expected one pool class")
    methods = []
    for name, span in METHODS.items():
        found = [n for n in owners[0].body if isinstance(n, ast.FunctionDef) and n.name == name]
        if len(found) != 1 or found[0].decorator_list:
            raise ValueError("Expected complete undecorated pool method")
        node = found[0]
        if actual == BASELINE_SHA256 and (node.lineno, node.end_lineno) != span:
            raise ValueError("Baseline method range mismatch")
        if any(isinstance(n, (ast.Import, ast.ImportFrom)) for n in ast.walk(node)):
            raise ValueError("Pool source imports are forbidden")
        methods.append(copy.deepcopy(node))
        print(f"EXTRACTED _Pool.{name} L{node.lineno}-{node.end_lineno}")
    isolated = ast.fix_missing_locations(ast.Module(body=[
        ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0),
        ast.ClassDef(name="Shell", bases=[], keywords=[], body=methods, decorator_list=[]),
    ], type_ignores=[]))
    forbidden = []
    def guard_import(name, *args, **kwargs):
        if name == "__future__":
            return builtins.__import__(name, *args, **kwargs)
        forbidden.append(name)
        raise AssertionError("No target, worker, startup or backend import is allowed")
    namespace = {"__builtins__": {**vars(builtins), "__import__": guard_import},
                 "queue": SimpleNamespace(Empty=FakeEmpty), "time": SimpleNamespace(monotonic=lambda: 100.0)}
    exec(compile(isolated, str(path), "exec"), namespace)
    print(f"SOURCE_SHA256 {actual}")
    return namespace["Shell"], namespace, forbidden


def pool_tests(shell_type, namespace, forbidden):
    class GatewayPoolTests(unittest.TestCase):
        def setUp(self):
            self.payload = object()
            self.pool = shell_type()
            self.pool.lock = FakeLock()
            self.pool.closed = False
            self.pool.workers = []
            self.pool.available = FakeQueue()
            self.pool.argv = (object(),)
            self.factory = FakeFactory(self.pool, self.payload)
            namespace["_Worker"] = self.factory
        def tearDown(self):
            self.assertEqual(forbidden, [])
            self.assertLessEqual(self.factory.max_live, 2)
            self.assertEqual(self.pool.lock.depth, 0)
        def complete(self):
            return self.pool.complete(self.payload, 1.0)
        def add_borrowed(self):
            with self.pool.lock:
                worker = self.factory(self.pool.argv)
                self.pool.workers.append(worker)
            return worker
        def test_two_failed_calls_then_recovery_without_pool_recreation(self):
            failures = [ValueError("fake worker one"), ValueError("fake worker two")]
            result = object()
            self.factory.outcomes = [*failures, result]
            for failure in failures:
                with self.assertRaises(ValueError) as raised:
                    self.complete()
                self.assertIs(raised.exception, failure)
            try:
                actual = self.complete()
            except Exception as exc:
                self.fail(f"Pool did not recover after two failures: {type(exc).__name__}")
            self.assertIs(actual, result)
            self.assertEqual(len(self.factory.created), 3)
            self.assertEqual(self.pool.workers, [self.factory.created[-1]])
        def test_exact_timed_out_lease_is_removed_and_never_reused(self):
            failure = TimeoutError("fake timeout; no real wait")
            result = object()
            self.factory.outcomes = [failure, result]
            with self.assertRaises(TimeoutError) as raised:
                self.complete()
            self.assertIs(raised.exception, failure)
            failed = self.factory.created[0]
            self.assertTrue(failed.closed)
            self.assertEqual(self.pool.workers, [])
            self.assertEqual(self.pool.available.items, [])
            self.assertEqual(len(self.factory.created), 1)
            self.assertIs(self.complete(), result)
            self.assertEqual(failed.calls, 1)
        def test_healthy_worker_is_reused(self):
            result = object(); self.factory.outcomes = [result]
            self.assertIs(self.complete(), result)
            self.assertIs(self.complete(), result)
            worker = self.factory.created[0]
            self.assertEqual(len(self.factory.created), 1)
            self.assertEqual(worker.calls, 2)
            self.assertEqual(self.pool.workers, [worker])
            self.assertEqual(self.pool.available.items, [worker])
        def test_idle_dead_replacement_success_is_preserved(self):
            first, second = object(), object(); self.factory.outcomes = [first, second]
            self.assertIs(self.complete(), first)
            dead = self.factory.created[0]; dead.process.status = 0
            self.assertIs(self.complete(), second)
            self.assertTrue(dead.closed)
            self.assertEqual(self.pool.workers, [self.factory.created[1]])
        def test_two_represented_busy_workers_do_not_allocate_a_third(self):
            first, second = self.add_borrowed(), self.add_borrowed()
            with self.assertRaises(FakeEmpty):
                self.complete()
            self.assertEqual(self.pool.workers, [first, second])
            self.assertEqual(len(self.factory.created), 2)
            self.assertEqual(self.pool.available.items, [])
            self.assertEqual(self.pool.available.waits, [0.05])
        def test_failed_lease_preserves_another_inflight_worker(self):
            busy = self.add_borrowed()
            failure = ValueError("fake leased failure")
            self.factory.outcomes = [failure]
            with self.assertRaises(ValueError) as raised:
                self.complete()
            self.assertIs(raised.exception, failure)
            self.assertEqual(self.pool.workers, [busy])
            self.assertEqual(self.pool.available.items, [])
            self.assertFalse(busy.closed)
            self.assertEqual(busy.calls, 0)
        def test_dead_at_return_no_longer_consumes_capacity(self):
            self.pool.warm()
            worker = self.factory.created[0]; worker.dead_at_return = True
            result = worker.outcome
            self.assertIs(self.complete(), result)
            self.assertEqual(self.pool.workers, [])
            self.assertEqual(self.pool.available.items, [])
        def test_closed_pool_rejects_warm_without_allocation(self):
            self.pool.closed = True
            with self.assertRaisesRegex(RuntimeError, "gateway_pool_closed"):
                self.pool.warm()
            self.assertEqual(self.factory.created, [])
    return GatewayPoolTests


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--expected-sha256", required=True)
    args = parser.parse_args()
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(pool_tests(*extract_shell(args.source, args.expected_sha256)))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
