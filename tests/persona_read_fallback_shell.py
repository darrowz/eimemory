"""AST-only read/decode recovery using fake paths and opaque state pipeline sentinels."""
from __future__ import annotations

import argparse
import ast
import builtins
import copy
import hashlib
from pathlib import Path
from types import SimpleNamespace
import unittest

BASELINE_SHA256 = "91669d3ded3fb09f690f2b3e812f549616d69a89ccd73c764b130135b26d82da"


class FakeJSONDecodeError(ValueError):
    pass


class FakePath:
    def __init__(self, label, token, calls):
        self.label = label; self.token = token; self.calls = calls
        self.present = True; self.failure = None
    def exists(self):
        self.calls.append((self.label, "exists"))
        return self.present
    def read_text(self, *, encoding):
        if encoding != "utf-8":
            raise AssertionError("Only the existing fake UTF-8 read is permitted")
        self.calls.append((self.label, "read"))
        if self.failure is not None:
            raise self.failure
        return self.token
    def __str__(self):
        return f"<synthetic {self.label}>"
    def __getattr__(self, name):
        raise AssertionError(f"No real filesystem or unexpected path method {name}")


def extract_shell(path, expected):
    data = path.read_bytes(); actual = hashlib.sha256(data).hexdigest()
    if actual != expected:
        raise ValueError(f"Pinned source digest mismatch: {actual}")
    module = ast.parse(data, filename=str(path))
    owners = [n for n in module.body if isinstance(n, ast.ClassDef) and n.name == "PersonaStore"]
    if len(owners) != 1:
        raise ValueError("Expected one store class")
    found = [n for n in owners[0].body if isinstance(n, ast.FunctionDef) and n.name == "load_state"]
    if len(found) != 1 or found[0].decorator_list:
        raise ValueError("Expected one complete undecorated method")
    method = found[0]
    if actual == BASELINE_SHA256 and (method.lineno, method.end_lineno) != (24, 51):
        raise ValueError("Unexpected baseline boundary")
    forbidden = []
    def fenced_import(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "__future__" and level == 0:
            return builtins.__import__(name, globals, locals, fromlist, level)
        forbidden.append(name)
        raise AssertionError("Target/schema/state/helper imports are forbidden")
    namespace = {"__builtins__": {**vars(builtins), "__import__": fenced_import}}
    isolated = ast.fix_missing_locations(ast.Module(body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0), ast.ClassDef(name="Shell", bases=[], keywords=[], body=[copy.deepcopy(method)], decorator_list=[])], type_ignores=[]))
    exec(compile(isolated, str(path), "exec"), namespace)
    print(f"SOURCE_SHA256 {actual}")
    print(f"EXTRACTED load_state L{method.lineno}-{method.end_lineno}")
    return namespace, forbidden


def fallback_tests(namespace, forbidden):
    class PersonaReadFallbackTests(unittest.TestCase):
        def setUp(self):
            self.calls = []; self.json_calls = []; self.state_calls = []; self.boundary_calls = []
            self.store = namespace["Shell"]()
            self.primary = FakePath("primary", "primary-token", self.calls)
            self.snapshot = FakePath("snapshot", "snapshot-token", self.calls)
            self.store.state_path = self.primary
            self.last_good = self.snapshot
            def lookup():
                self.calls.append(("lookup",))
                return self.last_good
            self.store._latest_snapshot_path = lookup
            self.payloads = {"primary-token": {"opaque": object()}, "snapshot-token": {"opaque": object()}}
            self.json_errors = {}; self.state_errors = {}; self.boundary_errors = {}
            self.states = {token: object() for token in self.payloads}
            self.outputs = {token: object() for token in self.payloads}
            self.default_output = object(); self.default_calls = []
            def fake_json_loads(token):
                if token not in self.payloads:
                    raise AssertionError("Unexpected fake parse token")
                self.json_calls.append(token)
                if token in self.json_errors:
                    raise self.json_errors[token]
                return self.payloads[token]
            def fake_from_dict(payload):
                token = next((key for key, value in self.payloads.items() if value is payload), None)
                if token is None:
                    raise AssertionError("Unexpected fake state payload")
                self.state_calls.append(payload)
                if token in self.state_errors:
                    raise self.state_errors[token]
                return self.states[token]
            def fake_boundary(state):
                token = next((key for key, value in self.states.items() if value is state), None)
                if token is None:
                    raise AssertionError("Unexpected opaque state")
                self.boundary_calls.append(state)
                if token in self.boundary_errors:
                    raise self.boundary_errors[token]
                return self.outputs[token]
            def fake_default():
                self.default_calls.append(True)
                return self.default_output
            namespace.update(json=SimpleNamespace(loads=fake_json_loads, JSONDecodeError=FakeJSONDecodeError), PersonaState=SimpleNamespace(from_dict=fake_from_dict), enforce_hard_boundaries=fake_boundary, default_persona_state=fake_default)
        def tearDown(self):
            self.assertEqual(forbidden, [])
        def decode_error(self):
            return UnicodeDecodeError("utf-8", b"\xff", 0, 1, "synthetic invalid byte; no file decode")
        def error(self):
            try:
                self.store.load_state()
            except BaseException as exc:
                return exc
            self.fail("Expected a stored synthetic error")
        def assert_corrupt(self, error, original):
            self.assertIs(type(error), ValueError)
            self.assertEqual(str(error), "corrupt persona state at <synthetic primary>")
            self.assertIs(error.__cause__, original)
        def test_primary_decode_error_uses_existing_snapshot_pipeline(self):
            self.primary.failure = self.decode_error()
            result = self.store.load_state()
            self.assertIs(result, self.outputs["snapshot-token"])
            self.assertEqual(self.calls, [("primary", "exists"), ("lookup",), ("primary", "read"), ("snapshot", "read")])
            self.assertEqual(self.json_calls, ["snapshot-token"])
            self.assertEqual(self.state_calls, [self.payloads["snapshot-token"]])
            self.assertEqual(self.boundary_calls, [self.states["snapshot-token"]])
            self.assertEqual(self.default_calls, [])
        def test_primary_decode_without_snapshot_keeps_normalized_cause(self):
            original = self.decode_error(); self.primary.failure = original; self.last_good = None
            self.assert_corrupt(self.error(), original)
            self.assertEqual(self.json_calls, []); self.assertEqual(self.state_calls, [])
            self.assertNotIn(("snapshot", "read"), self.calls)
        def test_failed_snapshot_read_or_decode_keeps_primary_cause(self):
            for failure in (OSError("synthetic snapshot read"), self.decode_error(), FakeJSONDecodeError("synthetic snapshot parse")):
                with self.subTest(kind=type(failure).__name__):
                    self.calls.clear(); self.json_calls.clear(); self.state_calls.clear(); self.boundary_calls.clear()
                    original = self.decode_error(); self.primary.failure = original
                    self.snapshot.failure = failure if not isinstance(failure, FakeJSONDecodeError) else None
                    self.json_errors = {"snapshot-token": failure} if isinstance(failure, FakeJSONDecodeError) else {}
                    self.assert_corrupt(self.error(), original)
                    self.assertIn(("snapshot", "read"), self.calls)
                    self.assertEqual(self.state_calls, []); self.assertEqual(self.boundary_calls, [])
        def test_existing_primary_read_and_json_errors_keep_fallback(self):
            for failure in (OSError("synthetic primary read"), FakeJSONDecodeError("synthetic primary parse")):
                with self.subTest(kind=type(failure).__name__):
                    self.primary.failure = failure if isinstance(failure, OSError) else None
                    self.json_errors = {"primary-token": failure} if isinstance(failure, FakeJSONDecodeError) else {}
                    self.assertIs(self.store.load_state(), self.outputs["snapshot-token"])
        def test_valid_primary_keeps_pipeline_without_snapshot_read(self):
            self.assertIs(self.store.load_state(), self.outputs["primary-token"])
            self.assertEqual(self.calls, [("primary", "exists"), ("lookup",), ("primary", "read")])
            self.assertEqual(self.json_calls, ["primary-token"])
            self.assertEqual(self.state_calls, [self.payloads["primary-token"]])
            self.assertEqual(self.boundary_calls, [self.states["primary-token"]])
        def test_missing_primary_returns_default_without_snapshot_lookup(self):
            self.primary.present = False
            self.assertIs(self.store.load_state(), self.default_output)
            self.assertEqual(self.default_calls, [True])
            self.assertEqual(self.calls, [("primary", "exists")])
            self.assertEqual(self.json_calls, []); self.assertEqual(self.state_calls, []); self.assertEqual(self.boundary_calls, [])
        def test_existing_invalid_primary_state_uses_snapshot(self):
            for failure in (TypeError("synthetic state type"), ValueError("synthetic state validation")):
                with self.subTest(kind=type(failure).__name__):
                    self.state_errors = {"primary-token": failure}
                    self.assertIs(self.store.load_state(), self.outputs["snapshot-token"])
        def test_failed_snapshot_state_retains_invalid_primary_outcome(self):
            original = ValueError("synthetic invalid primary state")
            self.state_errors = {"primary-token": original, "snapshot-token": TypeError("synthetic invalid snapshot")}
            error = self.error()
            self.assertIs(type(error), ValueError)
            self.assertEqual(str(error), "invalid persona state at <synthetic primary>")
            self.assertIs(error.__cause__, original)
            self.assertEqual(self.state_calls, [self.payloads["primary-token"], self.payloads["snapshot-token"]])
            self.assertEqual(self.boundary_calls, [])
        def test_unrelated_primary_valueerror_is_not_broadened_into_recovery(self):
            original = ValueError("synthetic non-decode parser failure")
            self.json_errors = {"primary-token": original}
            self.assertIs(self.error(), original)
            self.assertNotIn(("snapshot", "read"), self.calls)
            self.assertEqual(self.state_calls, []); self.assertEqual(self.boundary_calls, [])
    return PersonaReadFallbackTests


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--expected-sha256", required=True)
    args = parser.parse_args()
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(fallback_tests(*extract_shell(args.source, args.expected_sha256)))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
