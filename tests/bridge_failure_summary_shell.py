"""AST-only bridge display propagation with fake DTOs, transport and formatters."""
from __future__ import annotations

import argparse
import ast
import builtins
import copy
import hashlib
from pathlib import Path
import unittest

BASELINE_SHA256 = "60b3c8938883dae669ed50713cd4f0d031eaa52e7a7cfd7e8157dc1b6972a456"
CAPABILITIES = ("vision.describe", "health.status", "engagement.wake", "engagement.sleep")


class Fields:
    def __init__(self, **values):
        self._values = values
    def __getattr__(self, name):
        if name not in self._values:
            raise AssertionError(f"Unexpected fake field {name}")
        return self._values[name]


class FakeResult(Fields):
    parsed = []
    def __init__(self, *, ok=True, command_id="", summary="", payload=None, error=None, audit=None):
        super().__init__(ok=ok, command_id=command_id, summary=summary, payload={} if payload is None else payload, error=error, audit={} if audit is None else audit)
    @classmethod
    def from_dict(cls, data):
        if set(data) - {"ok", "command_id", "summary", "payload", "error", "audit"}:
            raise AssertionError("Unexpected fake parser field")
        cls.parsed.append(dict(data))
        return cls(**data)


class FakeAbort(BaseException):
    pass


def extract_shell(path, expected):
    data = path.read_bytes(); actual = hashlib.sha256(data).hexdigest()
    if actual != expected:
        raise ValueError(f"Pinned source digest mismatch: {actual}")
    module = ast.parse(data, filename=str(path))
    owners = [n for n in module.body if isinstance(n, ast.ClassDef) and n.name == "EIBrainAgentAdapter"]
    if len(owners) != 1:
        raise ValueError("Expected one adapter class")
    methods = [n for n in owners[0].body if isinstance(n, ast.FunctionDef) and n.name == "handle_command"]
    helpers = [n for n in module.body if isinstance(n, ast.FunctionDef) and n.name == "_coerce_result"]
    if len(methods) != 1 or len(helpers) != 1:
        raise ValueError("Expected two exact complete nodes")
    selected = [methods[0], helpers[0]]
    for node, span in zip(selected, [(31, 73), (76, 83)]):
        if node.decorator_list or actual == BASELINE_SHA256 and (node.lineno, node.end_lineno) != span:
            raise ValueError("Source boundary mismatch")
        print(f"EXTRACTED {node.name} L{node.lineno}-{node.end_lineno}")
    forbidden = []
    def fenced_import(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "__future__" and level == 0:
            return builtins.__import__(name, globals, locals, fromlist, level)
        forbidden.append(name)
        raise AssertionError("No target or helper imports are allowed")
    isolated = ast.fix_missing_locations(ast.Module(body=[
        ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0),
        copy.deepcopy(helpers[0]),
        ast.ClassDef(name="Shell", bases=[], keywords=[], body=[copy.deepcopy(methods[0])], decorator_list=[]),
    ], type_ignores=[]))
    namespace = {"__builtins__": {**vars(builtins), "__import__": fenced_import}, "BridgeResult": FakeResult}
    exec(compile(isolated, str(path), "exec"), namespace)
    print(f"SOURCE_SHA256 {actual}")
    return namespace, forbidden


def summary_tests(namespace, forbidden):
    class BridgeFailureSummaryTests(unittest.TestCase):
        def setUp(self):
            FakeResult.parsed.clear()
            self.adapter = namespace["Shell"]()
            self.adapter.agent_id = "synthetic agent"
            self.payload = object(); self.extra = object(); self.error_value = object()
            self.audit = {"opaque": self.extra, "agent_id": "old agent", "capability": "old capability"}
            self.raw = None; self.transport_error = None
            self.transport_calls = []; self.summary_calls = []; self.error_calls = []
            self.generated_summary = "synthetic success summary"
            def transport(command):
                self.transport_calls.append(command)
                if self.transport_error is not None:
                    raise self.transport_error
                return self.raw
            def formatter(capability, payload):
                self.summary_calls.append((capability, payload))
                return self.generated_summary
            def error_formatter(capability, error):
                self.error_calls.append((capability, error))
                return "synthetic transport failure summary"
            self.adapter.transport = transport
            namespace.update(_summary_for_capability=formatter, _transport_error_summary=error_formatter)
        def tearDown(self):
            self.assertEqual(forbidden, [])
        def command(self, capability):
            return Fields(target=Fields(capability=capability), command_id="synthetic command")
        def dto(self, *, ok, summary="original failure summary"):
            return FakeResult(ok=ok, command_id="original result id", summary=summary, payload=self.payload, error=self.error_value, audit=self.audit)
        def assert_preserved(self, result, capability, *, summary="original failure summary"):
            self.assertIs(result.ok, False)
            self.assertEqual(result.command_id, "original result id")
            self.assertEqual(result.summary, summary)
            self.assertIs(result.payload, self.payload)
            self.assertIs(result.error, self.error_value)
            self.assertEqual(result.audit, {"opaque": self.extra, "agent_id": "synthetic agent", "capability": capability})
            self.assertIs(result.audit["opaque"], self.extra)
            self.assertIsNot(result.audit, self.audit)
            self.assertEqual(self.audit, {"opaque": self.extra, "agent_id": "old agent", "capability": "old capability"})
        def test_failed_dtos_preserve_summary_and_skip_success_formatter(self):
            for capability in CAPABILITIES:
                with self.subTest(capability=capability):
                    self.summary_calls.clear(); self.raw = self.dto(ok=False)
                    original = dict(self.raw._values)
                    result = self.adapter.handle_command(self.command(capability))
                    self.assert_preserved(result, capability)
                    self.assertEqual(self.summary_calls, [])
                    self.assertEqual(self.raw._values, original)
        def test_explicit_failed_dictionary_preserves_all_fields(self):
            for capability in CAPABILITIES:
                with self.subTest(capability=capability):
                    self.summary_calls.clear(); self.raw = dict(self.dto(ok=False)._values)
                    original = dict(self.raw)
                    result = self.adapter.handle_command(self.command(capability))
                    self.assert_preserved(result, capability)
                    self.assertEqual(self.summary_calls, [])
                    self.assertEqual(self.raw, original)
                    self.assertIs(self.raw["audit"], self.audit)
        def test_empty_failed_summary_does_not_get_success_text(self):
            self.raw = self.dto(ok=False, summary="")
            result = self.adapter.handle_command(self.command("vision.describe"))
            self.assert_preserved(result, "vision.describe", summary="")
            self.assertEqual(self.summary_calls, [])
        def test_success_still_uses_nonempty_formatter(self):
            self.raw = self.dto(ok=True, summary="original success")
            result = self.adapter.handle_command(self.command("health.status"))
            self.assertIs(result.ok, True)
            self.assertEqual(result.summary, self.generated_summary)
            self.assertEqual(self.summary_calls, [("health.status", self.payload)])
            self.assertEqual(result.command_id, self.raw.command_id)
            self.assertIs(result.error, self.error_value)
            self.assertIs(result.payload, self.payload)
            self.assertIs(result.audit["opaque"], self.extra)
            self.assertEqual(result.audit["agent_id"], self.adapter.agent_id)
        def test_empty_success_formatter_keeps_original_summary(self):
            self.generated_summary = ""; self.raw = self.dto(ok=True, summary="original success")
            result = self.adapter.handle_command(self.command("synthetic unknown"))
            self.assertEqual(result.summary, "original success")
            self.assertEqual(self.summary_calls, [("synthetic unknown", self.payload)])
            self.assertIs(result.ok, True)
        def test_dictionary_defaults_are_still_supplied_without_input_mutation(self):
            self.raw = {"summary": "dictionary success", "payload": self.payload, "audit": self.audit}
            original = dict(self.raw)
            result = self.adapter.handle_command(self.command("health.status"))
            self.assertIs(result.ok, True)
            self.assertEqual(result.command_id, "synthetic command")
            self.assertEqual(result.summary, self.generated_summary)
            self.assertEqual(self.raw, original)
            self.assertNotIn("ok", self.raw); self.assertNotIn("command_id", self.raw)
            self.assertIs(FakeResult.parsed[0]["ok"], True)
            self.assertEqual(FakeResult.parsed[0]["command_id"], "synthetic command")
        def test_no_transport_engagement_stays_accepted_and_planned(self):
            self.adapter.transport = None
            for capability in ("engagement.wake", "engagement.sleep"):
                with self.subTest(capability=capability):
                    result = self.adapter.handle_command(self.command(capability))
                    self.assertIs(result.ok, True)
                    self.assertEqual(result.summary, f"已接受 {capability}，等待部署服务执行")
                    self.assertEqual(result.payload, {"status": "accepted", "planned": True})
                    self.assertEqual(result.audit, {"agent_id": self.adapter.agent_id, "capability": capability})
            self.assertEqual(self.transport_calls, []); self.assertEqual(self.summary_calls, [])
            self.assertEqual(FakeResult.parsed, [])
        def test_ordinary_transport_exception_keeps_error_path(self):
            failure = RuntimeError("synthetic transport failure"); self.transport_error = failure
            result = self.adapter.handle_command(self.command("health.status"))
            self.assertIs(result.ok, False)
            self.assertEqual(result.error, "transport_error")
            self.assertEqual(result.command_id, "synthetic command")
            self.assertEqual(result.summary, "synthetic transport failure summary")
            self.assertEqual(result.audit, {"agent_id": self.adapter.agent_id, "capability": "health.status"})
            self.assertEqual(self.error_calls, [("health.status", failure)])
            self.assertEqual(self.summary_calls, []); self.assertEqual(FakeResult.parsed, [])
        def test_baseexception_keeps_existing_propagation_policy(self):
            failure = FakeAbort("synthetic exception, no signal"); self.transport_error = failure
            with self.assertRaises(FakeAbort) as caught:
                self.adapter.handle_command(self.command("health.status"))
            self.assertIs(caught.exception, failure)
            self.assertEqual(self.error_calls, []); self.assertEqual(self.summary_calls, [])
        def test_other_no_transport_path_keeps_empty_dictionary_defaults(self):
            self.adapter.transport = None
            result = self.adapter.handle_command(self.command("health.status"))
            self.assertIs(result.ok, True)
            self.assertEqual(result.command_id, "synthetic command")
            self.assertEqual(result.summary, self.generated_summary)
            self.assertEqual(FakeResult.parsed, [{"ok": True, "command_id": "synthetic command"}])
            self.assertEqual(self.summary_calls, [("health.status", {})])
            self.assertEqual(self.transport_calls, [])
    return BridgeFailureSummaryTests


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--expected-sha256", required=True)
    args = parser.parse_args()
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(summary_tests(*extract_shell(args.source, args.expected_sha256)))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
