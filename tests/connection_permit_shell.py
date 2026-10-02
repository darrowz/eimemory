"""AST-only connection permit probe; no driver, factory, DSN, or real resource."""
from __future__ import annotations

import argparse
import ast
import builtins
import copy
import hashlib
from pathlib import Path
from types import SimpleNamespace
import unittest


BASELINE_SHA256 = "a76c533dcca8c252482742565b74f7ece6dd154fac74224bd33f2bded32cbeaa"


def extract_shell(path, expected):
    data = path.read_bytes()
    actual = hashlib.sha256(data).hexdigest()
    if actual != expected:
        raise ValueError(f"Pinned source digest mismatch: {actual}")
    module = ast.parse(data, filename=str(path))
    owners = [node for node in module.body if isinstance(node, ast.ClassDef) and node.name == "PostgresCandidateRepository"]
    if len(owners) != 1:
        raise ValueError("Expected one repository owner")
    selected = [node for node in owners[0].body if isinstance(node, ast.FunctionDef) and node.name == "_connect"]
    if len(selected) != 1 or selected[0].decorator_list:
        raise ValueError("Expected complete undecorated _connect")
    method = selected[0]
    if actual == BASELINE_SHA256 and (method.lineno, method.end_lineno) != (423, 457):
        raise ValueError("Baseline method range mismatch")
    isolated = ast.fix_missing_locations(ast.Module(body=[
        ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0),
        ast.ClassDef(name="Shell", bases=[], keywords=[], body=[copy.deepcopy(method)], decorator_list=[]),
    ], type_ignores=[]))
    forbidden_imports = []
    def guarded_import(name, *args, **kwargs):
        if name == "__future__":
            return builtins.__import__(name, *args, **kwargs)
        forbidden_imports.append(name)
        raise AssertionError("Driver/project imports are forbidden")
    namespace = {"__builtins__": {**vars(builtins), "__import__": guarded_import}}
    exec(compile(isolated, str(path), "exec"), namespace)
    print(f"SOURCE_SHA256 {actual}")
    print(f"EXTRACTED _connect L{method.lineno}-{method.end_lineno}")
    return namespace["Shell"], namespace, forbidden_imports


class FakeConfig:
    connect_timeout_seconds = 1.0
    connection_factory = None
    def __init__(self):
        self.forbidden = []
    def __getattr__(self, name):
        self.forbidden.append(name)
        raise AssertionError(f"Forbidden configuration field: {name}")


class FakeLock:
    def __enter__(self):
        return self
    def __exit__(self, *_args):
        pass


class FakeGate:
    def __init__(self, *, accepted=True, initial_owned=0):
        self.accepted = accepted
        self.owned = initial_owned
        self.acquires = 0
        self.releases = 0
    def acquire(self, timeout):
        if not isinstance(timeout, (int, float)):
            raise AssertionError("Expected fake numeric timeout")
        self.acquires += 1
        if self.accepted:
            self.owned += 1
        return self.accepted
    def release(self):
        self.owned -= 1
        self.releases += 1


class FakeAbort(BaseException):
    pass


def permit_tests(shell_type, namespace, forbidden_imports):
    class ConnectionPermitTests(unittest.TestCase):
        def setUp(self):
            self.shell = shell_type()
            self.shell.config = FakeConfig()
            self.shell._idle_lock = FakeLock()
            self.shell._idle_pool = []
            self.shell._gate = FakeGate(initial_owned=2)
            self.timeout_calls = 0
            self.handoffs = []
            self.forbidden_handoffs = []
            def deny_handoff(*_args, **_kwargs):
                self.forbidden_handoffs.append("handoff")
                raise AssertionError("Handoff is forbidden in an abort case")
            namespace["_GatedConnection"] = deny_handoff
        def tearDown(self):
            self.assertEqual(forbidden_imports, [])
            self.assertEqual(self.shell.config.forbidden, [])
            self.assertEqual(self.forbidden_handoffs, [])
        def timeout_stub(self, failure, *, at=2):
            def remaining(_deadline, _configured):
                self.timeout_calls += 1
                if self.timeout_calls == at:
                    raise failure
                return 0.5
            namespace["_remaining_timeout"] = remaining
        def assert_after_acquire_failure(self, failure):
            self.timeout_stub(failure)
            with self.assertRaises(type(failure)) as raised:
                self.shell._connect(deadline_at=1.0)
            self.assertIs(raised.exception, failure)
            self.assertEqual(self.timeout_calls, 2)
            self.assertEqual(self.shell._gate.acquires, 1)
            self.assertEqual(self.shell._gate.releases, 1)
            self.assertEqual(self.shell._gate.owned, 2)
        def test_abort_after_acquire_releases_only_its_owned_permit(self):
            self.assert_after_acquire_failure(FakeAbort("fake callback abort; no signal"))
        def test_abort_before_acquire_releases_nothing(self):
            failure = FakeAbort("fake pre-acquisition abort")
            self.timeout_stub(failure, at=1)
            with self.assertRaises(FakeAbort) as raised:
                self.shell._connect(deadline_at=1.0)
            self.assertIs(raised.exception, failure)
            self.assertEqual(self.shell._gate.acquires, 0)
            self.assertEqual(self.shell._gate.releases, 0)
            self.assertEqual(self.shell._gate.owned, 2)
        def test_gate_rejection_owns_no_permit(self):
            self.shell._gate.accepted = False
            self.timeout_stub(FakeAbort("must not reach second timeout"))
            with self.assertRaisesRegex(TimeoutError, "connection_queue_full"):
                self.shell._connect(deadline_at=1.0)
            self.assertEqual(self.timeout_calls, 1)
            self.assertEqual(self.shell._gate.releases, 0)
            self.assertEqual(self.shell._gate.owned, 2)
        def test_ordinary_failure_keeps_existing_cleanup(self):
            self.assert_after_acquire_failure(ValueError("fake ordinary timeout-helper error"))
        def test_timeout_error_keeps_existing_cleanup(self):
            self.assert_after_acquire_failure(TimeoutError("fake deadline exhausted"))
        def test_idle_success_transfers_without_releasing_early(self):
            raw = SimpleNamespace(closed=False)
            self.shell._idle_pool = [raw]
            self.timeout_stub(FakeAbort("idle success must not compute second timeout"))
            handoff = object()
            def record_handoff(connection, gate, *, repository):
                self.handoffs.append((connection, gate, repository))
                return handoff
            namespace["_GatedConnection"] = record_handoff
            self.assertIs(self.shell._connect(deadline_at=1.0), handoff)
            self.assertEqual(self.handoffs, [(raw, self.shell._gate, self.shell)])
            self.assertEqual(self.shell._idle_pool, [])
            self.assertEqual(self.timeout_calls, 1)
            self.assertEqual(self.shell._gate.releases, 0)
            self.assertEqual(self.shell._gate.owned, 3)
    return ConnectionPermitTests


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--expected-sha256", required=True)
    args = parser.parse_args()
    extracted = extract_shell(args.source, args.expected_sha256)
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(permit_tests(*extracted))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
