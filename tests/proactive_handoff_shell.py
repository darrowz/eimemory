"""AST-only handoff/close probe with scripted fake threads; no real thread starts."""
from __future__ import annotations

import argparse
import ast
import builtins
import copy
import hashlib
from pathlib import Path
from types import SimpleNamespace
import unittest


BASELINE_SHA256 = "81a40345afdd7e57d8795b5f332c95bcf370df008aecaed3fc85a86b99bb4696"
METHODS = {"close": (1360, 1391), "_recall_with_timeout": (1609, 1674)}


class FakeAbort(BaseException):
    pass


class FakeLock:
    def __init__(self):
        self.depth = 0
        self.on_exit = None
        self.in_hook = False
    def __enter__(self):
        self.depth += 1
        return self
    def __exit__(self, *_args):
        self.depth -= 1
        if self.depth == 0 and self.on_exit is not None and not self.in_hook:
            self.in_hook = True
            try:
                self.on_exit()
            finally:
                self.in_hook = False


class FakeSlots:
    def __init__(self):
        self.initial = 2
        self.occupied = self.initial
        self.accepted = True
        self.acquires = 0
        self.releases = 0
    def acquire(self, *, blocking):
        if blocking is not False:
            raise AssertionError("Only nonblocking fake admission is allowed")
        self.acquires += 1
        if self.accepted:
            self.occupied += 1
        return self.accepted
    def release(self):
        if self.occupied <= self.initial:
            raise AssertionError("Attempted to release an unrelated or already released permit")
        self.releases += 1
        self.occupied -= 1


class FakeThread:
    def __init__(self, factory, target):
        self.factory = factory
        self.target = target
        self.started = False
        self.alive = False
        self.join_calls = []
        self.uncaught = []
    def deliver(self):
        self.started = True
        self.alive = True
        try:
            self.target()
        except BaseException as exc:
            # A real target exception does not escape Thread.start into its caller.
            self.uncaught.append(exc)
        finally:
            self.alive = False
    def start(self):
        mode = self.factory.mode
        if mode in {"start_failure", "start_abort"}:
            raise self.factory.failure
        if mode in {"delayed", "deliver_on_join"}:
            self.started = True
            self.alive = True
            return
        self.deliver()
        if mode == "completed_then_start_failure":
            raise self.factory.failure
    def join(self, *, timeout):
        self.join_calls.append((self.started, timeout))
        if not self.started:
            self.factory.unstarted_joins.append(self)
            raise RuntimeError("fake cannot join thread before start")
        if self.factory.mode == "deliver_on_join" and self.alive:
            self.deliver()
    def is_alive(self):
        return self.alive


class FakeFactory:
    def __init__(self):
        self.mode = "sync"
        self.failure = RuntimeError("synthetic start failure")
        self.created = []
        self.unstarted_joins = []
    def __call__(self, *, target, name, daemon):
        if name != "eimemory-proactive-recall" or daemon is not True:
            raise AssertionError("Unexpected fake thread metadata")
        if self.mode == "constructor_failure":
            raise self.failure
        worker = FakeThread(self, target)
        self.created.append(worker)
        return worker


class FakeContext:
    def __enter__(self):
        return self
    def __exit__(self, *_args):
        return False


def extract_shell(path, expected):
    data = path.read_bytes(); actual = hashlib.sha256(data).hexdigest()
    if actual != expected:
        raise ValueError(f"Pinned source digest mismatch: {actual}")
    module = ast.parse(data, filename=str(path))
    owners = [n for n in module.body if isinstance(n, ast.ClassDef) and n.name == "ProactiveRecallService"]
    if len(owners) != 1:
        raise ValueError("Expected one proactive service class")
    owner = owners[0]
    selected = []
    for name, span in METHODS.items():
        found = [n for n in owner.body if isinstance(n, ast.FunctionDef) and n.name == name]
        if len(found) != 1 or found[0].decorator_list:
            raise ValueError("Expected complete undecorated method")
        node = found[0]
        if actual == BASELINE_SHA256 and (node.lineno, node.end_lineno) != span:
            raise ValueError("Baseline method range mismatch")
        selected.append(copy.deepcopy(node))
        print(f"EXTRACTED {name} L{node.lineno}-{node.end_lineno}")
    defaults = [n for n in module.body if isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "DEFAULT_CLOSE_TIMEOUT_SECONDS" for t in n.targets)]
    if len(defaults) != 1:
        raise ValueError("Expected one numeric close default")
    close_default = ast.literal_eval(defaults[0].value)
    control = {"forbidden": [], "worker_import_failure": None, "worker_imports": 0}
    def fake_import(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "__future__" and level == 0:
            return builtins.__import__(name, globals, locals, fromlist, level)
        names = tuple(fromlist)
        if name == "time" and level == 0 and names == ("perf_counter",):
            return SimpleNamespace(perf_counter=lambda: 100.0)
        if name == "eimemory.core.budgets" and level == 0 and names == ("proactive_host_margin_seconds", "proactive_host_window_seconds"):
            return SimpleNamespace(proactive_host_margin_seconds=lambda: 0.0, proactive_host_window_seconds=lambda _channel: 0.0)
        if name == "caller_assistance" and level == 1 and names == ("host_delivery_deadline",):
            control["worker_imports"] += 1
            if control["worker_import_failure"] is not None:
                raise control["worker_import_failure"]
            return SimpleNamespace(host_delivery_deadline=lambda _deadline: FakeContext())
        if name == "caller_assistance" and level == 1 and names == ("enabled",):
            return SimpleNamespace(enabled=lambda: False)
        control["forbidden"].append((name, level, names))
        raise AssertionError("Only the exact named inert import substitutes are permitted")
    namespace = {
        "__builtins__": {**vars(builtins), "__import__": fake_import},
        "DEFAULT_CLOSE_TIMEOUT_SECONDS": close_default,
        "monotonic": lambda: 100.0,
    }
    isolated = ast.fix_missing_locations(ast.Module(body=[
        ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0),
        ast.ClassDef(name="Shell", bases=[], keywords=[], body=selected, decorator_list=[]),
    ], type_ignores=[]))
    exec(compile(isolated, str(path), "exec"), namespace)
    print(f"SOURCE_SHA256 {actual}")
    return namespace["Shell"], namespace, control


def handoff_tests(shell_type, namespace, control):
    class ProactiveHandoffTests(unittest.TestCase):
        def setUp(self):
            control["worker_import_failure"] = None
            control["worker_imports"] = 0
            self.service = shell_type()
            self.service._lock = FakeLock()
            self.service._closing = False
            self.service._recall_slots = FakeSlots()
            self.service._workers = set()
            self.service._drain_finalizer = None
            self.service._on_drained_called = False
            self.service._on_drained_callback = None
            self.service.recall_timeout_seconds = 0.2
            self.drains = []
            self.service._start_drain_finalizer = lambda workers: self.drains.append(("finalizer", tuple(workers)))
            self.service._run_on_drained_if_ready = lambda **kwargs: self.drains.append(("drained", kwargs))
            self.result = object()
            self.runtime_calls = []
            def fake_recall(**kwargs):
                self.runtime_calls.append(kwargs)
                return self.result
            self.service.runtime = SimpleNamespace(memory=SimpleNamespace(recall=fake_recall))
            self.factory = FakeFactory()
            namespace["Thread"] = self.factory
        def tearDown(self):
            self.assertEqual(control["forbidden"], [])
            self.assertEqual(self.service._lock.depth, 0)
        def recall(self):
            return self.service._recall_with_timeout(query="synthetic query", scope={"tenant_id": "synthetic"}, source_ids=("synthetic",), task_type="synthetic.task")
        def error(self):
            try:
                self.recall()
            except BaseException as exc:
                return exc
            self.fail("Expected the scripted failure")
        def assert_reclaimed(self, other):
            self.assertEqual(self.service._recall_slots.occupied, 2)
            self.assertEqual(self.service._recall_slots.releases, 1)
            self.assertEqual(self.service._workers, {other})
        def test_constructor_failure_reclaims_only_owned_slot(self):
            other = object(); self.service._workers.add(other)
            self.factory.mode = "constructor_failure"
            raised = self.error()
            self.assertIs(raised, self.factory.failure)
            self.assert_reclaimed(other)
            self.assertEqual(self.runtime_calls, [])
        def test_start_failure_reclaims_only_owned_slot_and_registration(self):
            other = object(); self.service._workers.add(other)
            self.factory.mode = "start_failure"
            raised = self.error()
            self.assertIs(raised, self.factory.failure)
            self.assert_reclaimed(other)
        def test_start_baseexception_reclaims_only_owned_slot(self):
            other = object(); self.service._workers.add(other)
            self.factory.mode = "start_abort"; self.factory.failure = FakeAbort("synthetic abort, no signal")
            raised = self.error()
            self.assertIs(raised, self.factory.failure)
            self.assert_reclaimed(other)
        def test_failed_admission_and_preclosing_release_nothing(self):
            self.service._recall_slots.accepted = False
            self.assertIsInstance(self.error(), TimeoutError)
            self.assertEqual(self.service._recall_slots.releases, 0)
            self.assertEqual(self.service._recall_slots.occupied, 2)
            self.service._closing = True
            before = self.service._recall_slots.acquires
            self.assertIsInstance(self.error(), RuntimeError)
            self.assertEqual(self.service._recall_slots.acquires, before)
            self.assertEqual(self.factory.created, [])
        def test_synchronous_completion_releases_once(self):
            self.assertIs(self.recall(), self.result)
            self.assertEqual(self.service._recall_slots.occupied, 2)
            self.assertEqual(self.service._recall_slots.releases, 1)
            self.assertEqual(self.service._workers, set())
        def test_delayed_join_timeout_retains_then_releases_owned_slot(self):
            self.factory.mode = "delayed"
            self.assertIsInstance(self.error(), TimeoutError)
            worker = self.factory.created[-1]
            self.assertEqual(self.service._recall_slots.occupied, 3)
            self.assertEqual(self.service._recall_slots.releases, 0)
            self.assertIn(worker, self.service._workers)
            worker.deliver()
            self.assertEqual(self.service._recall_slots.occupied, 2)
            self.assertEqual(self.service._recall_slots.releases, 1)
            self.assertNotIn(worker, self.service._workers)
        def test_start_exception_after_completed_target_does_not_release_twice(self):
            self.factory.mode = "completed_then_start_failure"
            raised = self.error()
            self.assertIs(raised, self.factory.failure)
            self.assertEqual(self.service._recall_slots.occupied, 2)
            self.assertEqual(self.service._recall_slots.releases, 1)
            self.assertEqual(self.service._workers, set())
        def test_late_canceled_target_has_no_runtime_helper_or_second_release(self):
            self.factory.mode = "start_failure"
            self.assertIs(self.error(), self.factory.failure)
            worker = self.factory.created[-1]
            releases = self.service._recall_slots.releases
            worker.deliver()
            self.assertEqual(self.runtime_calls, [])
            self.assertEqual(control["worker_imports"], 0)
            self.assertEqual(self.service._recall_slots.releases, releases)
            self.assertEqual(self.service._recall_slots.occupied, 2)
            self.assertEqual(worker.uncaught, [])
        def test_worker_import_failure_reaches_cleanup_and_caller(self):
            failure = ImportError("synthetic worker setup failure")
            control["worker_import_failure"] = failure
            raised = self.error()
            self.assertEqual(self.service._recall_slots.occupied, 2)
            self.assertEqual(self.service._recall_slots.releases, 1)
            self.assertEqual(self.service._workers, set())
            self.assertIs(raised, failure)
            self.assertEqual(self.runtime_calls, [])
        def test_close_never_joins_an_unstarted_registration(self):
            self.factory.mode = "deliver_on_join"
            attempted = []; close_errors = []
            def close_on_registration():
                if attempted or not self.factory.created:
                    return
                worker = self.factory.created[-1]
                if worker not in self.service._workers:
                    return
                attempted.append(worker)
                try:
                    self.service.close(timeout_seconds=0)
                except BaseException as exc:
                    close_errors.append(exc)
            self.service._lock.on_exit = close_on_registration
            self.assertIs(self.recall(), self.result)
            self.assertTrue(attempted)
            self.assertEqual(self.factory.unstarted_joins, [])
            self.assertTrue(all(isinstance(exc, TimeoutError) for exc in close_errors))
            self.assertEqual(self.service._recall_slots.occupied, 2)
            self.assertEqual(self.service._workers, set())
    return ProactiveHandoffTests


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--expected-sha256", required=True)
    args = parser.parse_args()
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(handoff_tests(*extract_shell(args.source, args.expected_sha256)))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
