"""AST-only selection connection ownership with empty cases and entirely fake output."""
from __future__ import annotations

import argparse
import ast
import builtins
import copy
import hashlib
from pathlib import Path
from types import SimpleNamespace
import unittest

BASELINE_SHA256 = "6c137468590948e54a4eaabcbd41b23429b1fb452bfbcfbac38895921e446092"


class FakeDBError(Exception):
    pass


class FakeAbort(BaseException):
    pass


class Fields:
    def __init__(self, **values):
        self.values = values
    def __getattr__(self, name):
        if name not in self.values:
            raise AssertionError(f"Unexpected fake argument/helper field {name}")
        return self.values[name]


class FakeConnection:
    def __init__(self, calls):
        self.calls = calls; self.exit_calls = []; self.close_attempts = 0; self.closed = False
        self.exit_error = None; self.close_error = None
    def __enter__(self):
        self.calls.append("enter")
        return self
    def __exit__(self, error_type, error, traceback):
        self.calls.append("exit"); self.exit_calls.append((error_type, error))
        if self.exit_error is not None:
            raise self.exit_error
        return False
    def close(self):
        self.calls.append("close"); self.close_attempts += 1
        if self.close_error is not None:
            raise self.close_error
        self.closed = True
    def __getattr__(self, name):
        raise AssertionError(f"SQL and unexpected connection operations are forbidden: {name}")


class FakeOutput:
    def __init__(self, label, calls):
        self.label = label; self.calls = calls; self.writes = []; self.mkdirs = []
    @property
    def parent(self):
        return self
    def mkdir(self, *, parents, exist_ok):
        if parents is not True or exist_ok is not True:
            raise AssertionError("Unexpected fake output options")
        self.calls.append("mkdir:" + self.label); self.mkdirs.append((parents, exist_ok))
    def write_text(self, text, *, encoding):
        if encoding != "utf-8":
            raise AssertionError("Unexpected fake encoding")
        self.calls.append("write:" + self.label); self.writes.append((text, encoding))
    def __getattr__(self, name):
        raise AssertionError(f"Real path/output behavior is forbidden: {name}")


def extract_shell(path, expected):
    data = path.read_bytes(); actual = hashlib.sha256(data).hexdigest()
    if actual != expected:
        raise ValueError(f"Pinned source digest mismatch: {actual}")
    module = ast.parse(data, filename=str(path))
    found = [n for n in module.body if isinstance(n, ast.FunctionDef) and n.name == "main"]
    if len(found) != 1 or found[0].decorator_list:
        raise ValueError("Expected one complete undecorated main function")
    method = found[0]
    if actual == BASELINE_SHA256 and (method.lineno, method.end_lineno) != (467, 512):
        raise ValueError("Unexpected baseline boundary")
    forbidden = []
    def fenced_import(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "__future__" and level == 0:
            return builtins.__import__(name, globals, locals, fromlist, level)
        forbidden.append(name)
        raise AssertionError("Target/script/model/SQL/helper imports are forbidden")
    namespace = {"__builtins__": {**vars(builtins), "__import__": fenced_import}}
    isolated = ast.fix_missing_locations(ast.Module(body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0), copy.deepcopy(method)], type_ignores=[]))
    exec(compile(isolated, str(path), "exec"), namespace)
    print(f"SOURCE_SHA256 {actual}")
    print(f"EXTRACTED main L{method.lineno}-{method.end_lineno}")
    return namespace, forbidden


def ownership_tests(namespace, forbidden):
    class ReflectiveSelectionOwnershipTests(unittest.TestCase):
        def setUp(self):
            self.calls = []; self.report_calls = []; self.selection_calls = []; self.print_calls = []
            self.conn = FakeConnection(self.calls); self.foreign = FakeConnection([])
            self.connect_error = None; self.selection_error = None; self.detection_error = None
            self.db = object(); self.snapshot = object(); self.model = object(); self.fallback_model = object(); self.stderr = object()
            self.argv = ["synthetic argument"]
            self.output = FakeOutput("markdown", self.calls); self.json_output = FakeOutput("json", self.calls)
            self.args = Fields(db=self.db, source_snapshot_at="", limit=2, context_limit=3, capability_limit=4, since_days=None, select_only=True, model=self.model, fallback_model=self.fallback_model, allow_fallback_minimax=False, output=self.output, json_output=self.json_output)
            self.report = {"skipped_count": 0, "opaque": object()}
            def parse(argv):
                self.calls.append("parse")
                self.assertIs(argv, self.argv)
                return self.args
            def parser():
                self.calls.append("parser")
                return Fields(parse_args=parse)
            def connect(db):
                self.calls.append("connect"); self.assertIs(db, self.db)
                if self.connect_error is not None:
                    raise self.connect_error
                return self.conn
            def detect(conn):
                self.calls.append("detect"); self.assertIs(conn, self.conn)
                if self.detection_error is not None:
                    raise self.detection_error
                return self.snapshot
            def select(conn, **kwargs):
                self.calls.append("select"); self.assertIs(conn, self.conn)
                self.selection_calls.append(kwargs)
                self.assertEqual(kwargs, {"limit": 2, "context_limit": 3, "capability_limit": 4, "source_snapshot_at": self.snapshot, "since_days": None})
                if self.selection_error is not None:
                    raise self.selection_error
                return []
            def analyze(*args, **kwargs):
                raise AssertionError("Actual analysis/model execution is forbidden")
            def now():
                self.calls.append("now")
                return "synthetic report time"
            def report(**kwargs):
                self.calls.append("report"); self.report_calls.append(kwargs)
                self.assertEqual(kwargs, {"cases": [], "analyses": [], "generated_at": "synthetic report time", "source_snapshot_at": self.snapshot, "model": self.model, "fallback_model": self.fallback_model, "allow_fallback_minimax": False})
                return self.report
            def render(value):
                self.calls.append("render"); self.assertIs(value, self.report)
                return "synthetic markdown"
            def dumps(value, **kwargs):
                if value is self.report:
                    self.calls.append("dumps:report")
                    self.assertEqual(kwargs, {"ensure_ascii": False, "indent": 2, "sort_keys": True})
                    return "synthetic report JSON"
                self.calls.append("dumps:summary")
                self.assertEqual(value, {"ok": True, "case_count": 0, "skipped_count": 0})
                self.assertEqual(kwargs, {"sort_keys": True})
                return "synthetic summary JSON"
            def fake_print(text, *, file=None):
                self.calls.append("print:stderr" if file is self.stderr else "print:stdout")
                self.assertTrue(file is None or file is self.stderr)
                self.print_calls.append((text, file))
            namespace.update(build_parser=parser, connect_readonly=connect, detect_source_snapshot_at=detect, select_replay_cases=select, analyze_case=analyze, now_iso=now, build_report=report, render_markdown_report=render, json=SimpleNamespace(dumps=dumps), sqlite3=SimpleNamespace(Error=FakeDBError), sys=SimpleNamespace(stderr=self.stderr), print=fake_print)
        def tearDown(self):
            self.assertEqual(forbidden, [])
            self.assertEqual(self.foreign.close_attempts, 0)
            self.assertIs(self.args.select_only, True)
        def main(self):
            return namespace["main"](self.argv)
        def error(self):
            try:
                self.main()
            except BaseException as exc:
                return exc
            self.fail("Expected stored synthetic error")
        def assert_no_report(self):
            self.assertEqual(self.report_calls, [])
            self.assertEqual(self.output.writes, []); self.assertEqual(self.json_output.writes, [])
            self.assertEqual(self.output.mkdirs, []); self.assertEqual(self.json_output.mkdirs, [])
        def test_empty_select_only_closes_before_fake_report_and_output(self):
            self.assertEqual(self.main(), 0)
            self.assertEqual(self.calls, ["parser", "parse", "connect", "enter", "detect", "select", "exit", "close", "now", "report", "mkdir:markdown", "mkdir:json", "render", "write:markdown", "dumps:report", "write:json", "dumps:summary", "print:stdout"])
            self.assertEqual(self.conn.exit_calls, [(None, None)])
            self.assertEqual(self.conn.close_attempts, 1); self.assertTrue(self.conn.closed)
            self.assertEqual(self.output.writes, [("synthetic markdown", "utf-8")])
            self.assertEqual(self.json_output.writes, [("synthetic report JSON", "utf-8")])
            self.assertEqual(self.print_calls, [("synthetic summary JSON", None)])
        def test_provided_opaque_snapshot_keeps_detection_bypass(self):
            self.args.values["source_snapshot_at"] = self.snapshot
            self.assertEqual(self.main(), 0)
            self.assertNotIn("detect", self.calls)
            self.assertIn("close", self.calls)
            self.assertLess(self.calls.index("close"), self.calls.index("report"))
            self.assertEqual(self.conn.close_attempts, 1)
        def test_selection_error_exits_context_and_closes_owned_handle(self):
            failure = ValueError("synthetic selection failure"); self.selection_error = failure
            self.assertIs(self.error(), failure)
            self.assertEqual(self.conn.exit_calls, [(ValueError, failure)])
            self.assertEqual(self.conn.close_attempts, 1); self.assertTrue(self.conn.closed)
            self.assert_no_report()
        def test_detection_error_exits_context_and_closes_owned_handle(self):
            failure = ValueError("synthetic detection failure"); self.detection_error = failure
            self.assertIs(self.error(), failure)
            self.assertEqual(self.conn.exit_calls, [(ValueError, failure)])
            self.assertEqual(self.conn.close_attempts, 1)
            self.assertEqual(self.selection_calls, [])
            self.assert_no_report()
        def test_synthetic_baseexception_gets_owned_cleanup(self):
            failure = FakeAbort("synthetic abort; no signal"); self.selection_error = failure
            self.assertIs(self.error(), failure)
            self.assertEqual(self.conn.exit_calls, [(FakeAbort, failure)])
            self.assertEqual(self.conn.close_attempts, 1)
            self.assert_no_report()
        def test_connect_error_preserves_return_two_and_fake_stderr(self):
            self.connect_error = FakeDBError("synthetic connection failure")
            self.assertEqual(self.main(), 2)
            self.assertEqual(self.calls, ["parser", "parse", "connect", "print:stderr"])
            self.assertEqual(self.print_calls, [("failed to open DB read-only: synthetic connection failure", self.stderr)])
            self.assertEqual(self.conn.close_attempts, 0); self.assertEqual(self.conn.exit_calls, [])
            self.assert_no_report()
        def test_context_exit_error_still_closes_once(self):
            failure = RuntimeError("synthetic context-exit failure"); self.conn.exit_error = failure
            self.assertIs(self.error(), failure)
            self.assertEqual(self.conn.exit_calls, [(None, None)])
            self.assertEqual(self.conn.close_attempts, 1)
            self.assert_no_report()
        def test_close_failure_is_visible_and_prevents_report_work(self):
            failure = RuntimeError("synthetic close failure"); self.conn.close_error = failure
            self.assertIs(self.error(), failure)
            self.assertEqual(self.conn.close_attempts, 1); self.assertFalse(self.conn.closed)
            self.assert_no_report()
        def test_selection_and_close_failure_keep_finally_precedence(self):
            primary = ValueError("synthetic selection failure"); cleanup = RuntimeError("synthetic close failure")
            self.selection_error = primary; self.conn.close_error = cleanup
            self.assertIs(self.error(), cleanup)
            self.assertIs(cleanup.__context__, primary)
            self.assertEqual(self.conn.exit_calls, [(ValueError, primary)])
            self.assertEqual(self.conn.close_attempts, 1); self.assertFalse(self.conn.closed)
            self.assert_no_report()
        def test_context_exit_and_close_failure_keep_finally_precedence(self):
            primary = RuntimeError("synthetic context exit failure"); cleanup = RuntimeError("synthetic close failure")
            self.conn.exit_error = primary; self.conn.close_error = cleanup
            self.assertIs(self.error(), cleanup)
            self.assertIs(cleanup.__context__, primary)
            self.assertEqual(self.conn.close_attempts, 1); self.assertFalse(self.conn.closed)
            self.assert_no_report()
    return ReflectiveSelectionOwnershipTests


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
