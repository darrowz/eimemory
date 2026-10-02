"""Exact AST batch-savepoint wrapper with opaque pending values and fake SQL commands."""
from __future__ import annotations

import argparse
import ast
import builtins
import copy
import hashlib
from pathlib import Path
import re
from types import SimpleNamespace
import unittest

BASELINE_SHA256 = "3def8cee95612f3b9c9917636add64a293ad3f496b07bab2348af050ab8c5e7c"


class FakeStoreError(RuntimeError):
    pass


class FakeConnection:
    def __init__(self):
        self.in_transaction = True
        self.calls = []
        self.rollback_error = None
        self.release_error = None
    def execute(self, command):
        match = re.fullmatch(r"(SAVEPOINT|ROLLBACK TO SAVEPOINT|RELEASE SAVEPOINT) (capability_snapshot_batch_[0-9]+)", command)
        if not match:
            raise AssertionError("Only this wrapper's exact savepoint commands are allowed")
        verb, name = match.groups()
        self.calls.append((verb, name))
        if verb == "ROLLBACK TO SAVEPOINT" and self.rollback_error is not None:
            raise self.rollback_error
        if verb == "RELEASE SAVEPOINT" and self.release_error is not None:
            raise self.release_error
    def __getattr__(self, name):
        raise AssertionError(f"Unexpected connection operation {name}")


def extract_shell(path, expected):
    data = path.read_bytes(); actual = hashlib.sha256(data).hexdigest()
    if actual != expected:
        raise ValueError(f"Pinned source digest mismatch: {actual}")
    module = ast.parse(data, filename=str(path))
    owner = [n for n in module.body if isinstance(n, ast.ClassDef) and n.name == "CapabilityStore"]
    if len(owner) != 1:
        raise ValueError("Expected one store class")
    found = [n for n in owner[0].body if isinstance(n, ast.FunctionDef) and n.name == "register_snapshots"]
    if len(found) != 1 or found[0].decorator_list:
        raise ValueError("Expected one complete undecorated method")
    method = found[0]
    if actual == BASELINE_SHA256 and (method.lineno, method.end_lineno) != (614, 647):
        raise ValueError("Unexpected baseline boundary")
    forbidden = []
    def fenced_import(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "__future__" and level == 0:
            return builtins.__import__(name, globals, locals, fromlist, level)
        forbidden.append(name)
        raise AssertionError("No target or helper import is permitted")
    isolated = ast.fix_missing_locations(ast.Module(body=[
        ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0),
        ast.ClassDef(name="Shell", bases=[], keywords=[], body=[copy.deepcopy(method)], decorator_list=[]),
    ], type_ignores=[]))
    namespace = {"__builtins__": {**vars(builtins), "__import__": fenced_import}, "CapabilityStoreError": FakeStoreError}
    exec(compile(isolated, str(path), "exec"), namespace)
    print(f"SOURCE_SHA256 {actual}")
    print(f"EXTRACTED register_snapshots L{method.lineno}-{method.end_lineno}")
    return namespace["Shell"], forbidden


def pending_tests(shell_type, forbidden):
    class SavepointPendingTests(unittest.TestCase):
        def setUp(self):
            self.store = shell_type()
            self.store._read_only = False
            self.store._savepoint_counter = 7
            self.conn = FakeConnection()
            self.store._sqlite = SimpleNamespace(conn=self.conn)
            self.prefix = [object(), object()]
            self.store._pending_audits = list(self.prefix)
            self.original_queue = self.store._pending_audits
            self.scope = object()
            self.snapshots = [SimpleNamespace(snapshot_id=f"synthetic-{i}") for i in range(3)]
            self.values = [object() for _ in self.snapshots]
            self.pending = [object() for _ in self.snapshots]
            self.receipts = [object() for _ in self.snapshots]
            self.inputs = [(snapshot, None, f"request-{i}") for i, snapshot in enumerate(self.snapshots)]
            self.write_calls = []
            self.fail_at = None
            self.failure = ValueError("synthetic later item failure")
            self.idempotent = set()
            def fake_snapshot_write(snapshot, *, provider_binding_id):
                self.assertIsNone(provider_binding_id)
                index = next(i for i, value in enumerate(self.snapshots) if value is snapshot)
                return self.values[index]
            def fake_write(value, *, scope, request_key, before_insert):
                self.assertIs(scope, self.scope)
                self.assertIsNone(before_insert)
                index = next(i for i, candidate in enumerate(self.values) if candidate is value)
                self.assertEqual(request_key, f"request-{index}")
                self.write_calls.append(index)
                if index == self.fail_at:
                    raise self.failure
                if index not in self.idempotent:
                    self.store._pending_audits.append(self.pending[index])
                return self.receipts[index]
            self.store._snapshot_write = fake_snapshot_write
            self.store._write_in_savepoint = fake_write
        def tearDown(self):
            self.assertEqual(forbidden, [])
            self.assertIs(self.store._pending_audits, self.original_queue)
            self.assertTrue(self.conn.in_transaction)
        def call(self, inputs=None):
            return self.store.register_snapshots(self.inputs if inputs is None else inputs, scope=self.scope)
        def error(self):
            try:
                self.call()
            except BaseException as exc:
                return exc
            self.fail("Expected stored synthetic exception")
        def assert_identity_list(self, expected):
            self.assertEqual(len(self.store._pending_audits), len(expected))
            for actual, wanted in zip(self.store._pending_audits, expected):
                self.assertIs(actual, wanted)
        def test_later_failure_restores_only_batch_suffix(self):
            self.fail_at = 2
            self.assertIs(self.error(), self.failure)
            self.assert_identity_list(self.prefix)
            self.assertEqual(self.write_calls, [0, 1, 2])
            self.assertEqual([verb for verb, _ in self.conn.calls], ["SAVEPOINT", "ROLLBACK TO SAVEPOINT", "RELEASE SAVEPOINT"])
        def test_first_failure_preserves_existing_prefix(self):
            self.fail_at = 0
            self.assertIs(self.error(), self.failure)
            self.assert_identity_list(self.prefix)
            self.assertEqual(self.write_calls, [0])
        def test_success_retains_entries_and_receipt_identities(self):
            result = self.call()
            self.assert_identity_list(self.prefix + self.pending)
            self.assertEqual(set(result), {snapshot.snapshot_id for snapshot in self.snapshots})
            for snapshot, receipt in zip(self.snapshots, self.receipts):
                self.assertIs(result[snapshot.snapshot_id], receipt)
            self.assertEqual([verb for verb, _ in self.conn.calls], ["SAVEPOINT", "RELEASE SAVEPOINT"])
        def test_empty_batch_leaves_queue_and_returns_empty_mapping(self):
            self.assertEqual(self.call([]), {})
            self.assert_identity_list(self.prefix)
            self.assertEqual(self.write_calls, [])
            self.assertEqual([verb for verb, _ in self.conn.calls], ["SAVEPOINT", "RELEASE SAVEPOINT"])
        def test_idempotent_fake_success_preserves_existing_behavior(self):
            self.idempotent = {0, 1, 2}
            result = self.call()
            self.assert_identity_list(self.prefix)
            for snapshot, receipt in zip(self.snapshots, self.receipts):
                self.assertIs(result[snapshot.snapshot_id], receipt)
        def test_rollback_failure_preserves_uncertain_pending_suffix(self):
            self.fail_at = 1
            cleanup = RuntimeError("synthetic rollback failure")
            self.conn.rollback_error = cleanup
            self.assertIs(self.error(), cleanup)
            self.assert_identity_list(self.prefix + self.pending[:1])
            self.assertEqual([verb for verb, _ in self.conn.calls], ["SAVEPOINT", "ROLLBACK TO SAVEPOINT"])
        def test_release_failure_after_rollback_still_restores_queue(self):
            self.fail_at = 1
            cleanup = RuntimeError("synthetic release failure")
            self.conn.release_error = cleanup
            self.assertIs(self.error(), cleanup)
            self.assert_identity_list(self.prefix)
            self.assertEqual([verb for verb, _ in self.conn.calls], ["SAVEPOINT", "ROLLBACK TO SAVEPOINT", "RELEASE SAVEPOINT"])
        def test_readonly_and_inactive_rejections_mutate_nothing(self):
            for read_only, active in [(True, True), (False, False)]:
                with self.subTest(read_only=read_only, active=active):
                    self.store._read_only = read_only; self.conn.in_transaction = active
                    self.assertIsInstance(self.error(), FakeStoreError)
                    self.assert_identity_list(self.prefix)
                    self.assertEqual(self.conn.calls, [])
                    self.assertEqual(self.write_calls, [])
                    self.assertEqual(self.store._savepoint_counter, 7)
            self.conn.in_transaction = True
        def test_failed_batch_does_not_remove_previous_batch_entries(self):
            self.call(self.inputs[:1])
            self.fail_at = 2
            self.assertIs(self.error(), self.failure)
            self.assert_identity_list(self.prefix + self.pending[:1])
            self.assertEqual(self.store._savepoint_counter, 9)
            self.assertEqual([name for _, name in self.conn.calls], ["capability_snapshot_batch_8"] * 2 + ["capability_snapshot_batch_9"] * 3)
    return SavepointPendingTests


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--expected-sha256", required=True)
    args = parser.parse_args()
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(pending_tests(*extract_shell(args.source, args.expected_sha256)))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    print(f"FAILING_METHODS {len({str(case) for case, _ in result.failures + result.errors})}")
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
