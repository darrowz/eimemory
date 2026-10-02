"""AST-only reply text from existing inert DTO fields, with no command execution."""
from __future__ import annotations

import argparse
import ast
import builtins
import copy
import hashlib
from pathlib import Path
import unittest

BASELINE_SHA256 = "d1b1160f703362cd311809a7c7ba30cce61961713e8f9085c2ad7e19d6e6af1c"


class FakeResult:
    def __init__(self, *, ok, summary, payload, audit, error):
        self._values = dict(ok=ok, summary=summary, payload=payload, audit=audit, error=error)
    def __getattr__(self, name):
        if name not in self._values:
            raise AssertionError(f"Unexpected fake DTO field {name}")
        return self._values[name]


def extract_shell(path, expected):
    data = path.read_bytes(); actual = hashlib.sha256(data).hexdigest()
    if actual != expected:
        raise ValueError(f"Pinned source digest mismatch: {actual}")
    module = ast.parse(data, filename=str(path))
    found = [n for n in module.body if isinstance(n, ast.FunctionDef) and n.name == "format_reply"]
    if len(found) != 1 or found[0].decorator_list:
        raise ValueError("Expected one complete undecorated function")
    method = found[0]
    if actual == BASELINE_SHA256 and (method.lineno, method.end_lineno) != (90, 103):
        raise ValueError("Unexpected baseline function boundary")
    forbidden = []
    def fenced_import(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "__future__" and level == 0:
            return builtins.__import__(name, globals, locals, fromlist, level)
        forbidden.append(name)
        raise AssertionError("Target/protocol/channel/helper imports are forbidden")
    namespace = {"__builtins__": {**vars(builtins), "__import__": fenced_import}}
    isolated = ast.fix_missing_locations(ast.Module(body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0), copy.deepcopy(method)], type_ignores=[]))
    exec(compile(isolated, str(path), "exec"), namespace)
    print(f"SOURCE_SHA256 {actual}")
    print(f"EXTRACTED format_reply L{method.lineno}-{method.end_lineno}")
    return namespace, forbidden


def reply_tests(namespace, forbidden):
    class BridgePendingReplyTests(unittest.TestCase):
        def setUp(self):
            self.payload_calls = []
            self.payload_summary = ""
            self.active_payload = None
            def fake_summary(payload):
                self.assertIs(payload, self.active_payload)
                self.payload_calls.append(payload)
                return self.payload_summary
            namespace["_payload_summary"] = fake_summary
        def tearDown(self):
            self.assertEqual(forbidden, [])
        def format(self, *, ok=True, summary="synthetic summary", payload=None, capability="synthetic ordinary", error=None):
            payload = {} if payload is None else payload
            audit = {"capability": capability, "synthetic_extra": "unchanged"}
            result = FakeResult(ok=ok, summary=summary, payload=payload, audit=audit, error=error)
            before = copy.deepcopy(result._values)
            self.active_payload = payload
            text = namespace["format_reply"](result)
            self.assertEqual(result._values, before)
            self.assertIs(result.payload, payload)
            self.assertIs(result.audit, audit)
            return text
        def test_accepted_planned_summary_retains_waiting_text(self):
            text = self.format(summary="  synthetic waiting summary  ", payload={"status": "accepted", "planned": True})
            self.assertEqual(text, "synthetic waiting summary")
            self.assertEqual(self.payload_calls, [])
        def test_exact_accepted_status_independently_marks_pending(self):
            text = self.format(summary="synthetic waiting", payload={"status": "accepted", "planned": False})
            self.assertEqual(text, "synthetic waiting")
            self.assertEqual(self.payload_calls, [])
        def test_exact_true_planned_independently_marks_pending(self):
            text = self.format(summary="synthetic waiting", payload={"planned": True})
            self.assertEqual(text, "synthetic waiting")
            self.assertEqual(self.payload_calls, [])
        def test_pending_without_any_summary_uses_neutral_waiting_text(self):
            for payload in ({"status": "accepted"}, {"planned": True}):
                with self.subTest(payload=payload):
                    self.payload_calls.clear()
                    self.assertEqual(self.format(summary=" \t ", payload=payload), "已接受，等待执行。")
                    self.assertEqual(self.payload_calls, [payload])
        def test_pending_payload_summary_is_selected_exactly_once(self):
            self.payload_summary = "synthetic payload waiting summary"
            payload = {"status": "accepted", "synthetic_extra": "unchanged"}
            text = self.format(summary="", payload=payload)
            self.assertEqual(text, "synthetic payload waiting summary")
            self.assertEqual(self.payload_calls, [payload])
        def test_ordinary_success_metadata_keeps_existing_completion_prefix(self):
            for payload in ({}, {"planned": False}, {"status": "ACCEPTED"}, {"status": " accepted "}, {"status": "synthetic other"}):
                with self.subTest(payload=payload):
                    self.assertEqual(self.format(summary="  synthetic summary  ", payload=payload), "已完成：synthetic summary")
            self.assertEqual(self.payload_calls, [])
        def test_truthy_nonboolean_planned_values_are_not_true(self):
            for value in (1, "true", "yes", ["synthetic"], {"synthetic": True}):
                with self.subTest(value=value):
                    self.assertEqual(self.format(payload={"planned": value}), "已完成：synthetic summary")
            self.assertEqual(self.payload_calls, [])
        def test_ordinary_empty_success_keeps_existing_empty_text(self):
            self.assertEqual(self.format(summary="", payload={}), "已完成。")
            self.assertEqual(self.payload_calls, [{}])
        def test_failures_ignore_pending_metadata_and_keep_error_text(self):
            payload = {"status": "accepted", "planned": True}
            self.assertEqual(self.format(ok=False, summary="  synthetic failure  ", payload=payload, error="synthetic error"), "执行失败：synthetic failure\n错误：synthetic error")
            self.assertEqual(self.format(ok=False, summary="synthetic failure", payload=payload), "执行失败：synthetic failure")
            self.assertEqual(self.format(ok=False, summary="  ", payload=payload), "执行失败：请求未完成")
            self.assertEqual(self.payload_calls, [])
        def test_vision_specific_text_remains_byte_identical(self):
            payload = {"status": "accepted", "planned": True}
            self.assertEqual(self.format(summary="  synthetic vision  ", payload=payload, capability=" VISION.DESCRIBE "), "synthetic vision")
            self.assertEqual(self.format(summary="", payload=payload, capability="vision.describe"), "我这会儿还没拿到可用画面，不能把现场情况编出来。")
            self.assertEqual(self.format(ok=False, summary="synthetic vision failure", payload=payload, capability="vision.describe", error="ignored by existing vision path"), "synthetic vision failure")
            self.assertEqual(self.format(ok=False, summary="", payload=payload, capability="vision.describe"), "请求未完成")
            self.assertEqual(self.payload_calls, [payload])
    return BridgePendingReplyTests


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--expected-sha256", required=True)
    args = parser.parse_args()
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(reply_tests(*extract_shell(args.source, args.expected_sha256)))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
