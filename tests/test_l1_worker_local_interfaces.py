"""Local AST-only contract regressions. No project imports or real services run.

Run directly with python -I; L1_LOCAL_BASELINE_DIR optionally selects the saved
worker source and the 12-line pre-fix service method for a red baseline run.
"""
from __future__ import annotations

import ast
import builtins
import contextlib
import copy
import io
import json
import os
from pathlib import Path
import textwrap
import types
import unittest


ROOT = Path(__file__).resolve().parents[1]
BASELINE = os.environ.get("L1_LOCAL_BASELINE_DIR")


def _sources():
    if BASELINE:
        base = Path(BASELINE)
        return (base / "l1_worker.py").read_text(), (base / "service_method.py").read_text()
    worker = (ROOT / "eimemory/cli/l1_worker.py").read_text()
    # Only parse the permitted wrapper, never the rest of the service or pipeline.
    service = (ROOT / "eimemory/adapters/runtime/service.py").read_text()
    start = service.index("    def backfill_l1(")
    end = service.index("\n        )", start) + len("\n        )")
    return worker, service[start:end] + "\n"


class QueueStateError(Exception):
    def __init__(self, code="queue_failed", **context):
        super().__init__(code)
        self.code = code
        self.context = context
        self.retryable = True


class Harness:
    def __init__(self, *, log_error=None, queue_error=None, recent_error=None, failed=0):
        worker, method = _sources()
        self.calls = []
        self.logs = []
        self.extractions = []
        self.closed = 0
        self.memory = object()
        self.runtime = types.SimpleNamespace(memory=self.memory, close=self.close)
        self.log_error = log_error
        self.queue_error = queue_error
        self.recent_error = recent_error
        self.failed = failed
        self.jobs = []
        self.env = {"EIMEMORY_ROOT": "inert-root"}

        def pipeline(memory, **kwargs):
            # Inert sink; retain exact kwargs to test omitted-versus-explicit input.
            self.calls.append((memory, kwargs))
            return {"ok": True, "processed": 1}

        def guarded_import(name, globals=None, locals=None, fromlist=(), level=0):
            if name == "eimemory.knowledge.l1_pipeline" and fromlist == ("backfill_l1_from_l0",) and level == 0:
                return types.SimpleNamespace(backfill_l1_from_l0=pipeline)
            raise AssertionError("Unexpected project import: " + name)

        bindings = {
            "__builtins__": {**vars(builtins), "__import__": guarded_import},
            "normalize_runtime_channel": lambda channel: "normalized:" + channel,
            "resolve_channel_scope": lambda channel, scope: {"channel": channel, "scope": scope},
        }
        method_tree = ast.parse(textwrap.dedent(method))
        assert len(method_tree.body) == 1 and isinstance(method_tree.body[0], ast.FunctionDef)
        assert method_tree.body[0].name == "backfill_l1"
        exec(compile(method_tree, "<local-service-wrapper>", "exec"), bindings)
        self.service = types.SimpleNamespace(runtime=self.runtime)
        self.service.backfill_l1 = types.MethodType(bindings["backfill_l1"], self.service)
        self.service._extract_l1_inline = self.extract
        self.service._l1_queue = lambda: types.SimpleNamespace(drain_report=self.drain_report, recent_dead=self.recent_dead)

        selected = {"_queue_state_failure_report", "drain_l1", "main"}
        tree = ast.parse(worker)
        body = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in selected]
        assert {node.name for node in body} == selected
        # No imports, module initialization, real log function or runtime bodies.
        assert not any(isinstance(node, (ast.Import, ast.ImportFrom)) for fn in body for node in ast.walk(fn))
        self.ns = {
            "Any": object,
            "L1QueueStateError": QueueStateError,
            "Runtime": types.SimpleNamespace(create=lambda **kwargs: self.runtime),
            "AgentRuntimeMemoryService": lambda runtime: self.service,
            "_append_worker_log": self.log,
            "os": types.SimpleNamespace(environ=self.env),
            "json": json,
        }
        exec(compile(ast.Module(body=body, type_ignores=[]), "<local-worker-functions>", "exec"), self.ns)

    def close(self):
        self.closed += 1

    def extract(self, **kwargs):
        self.extractions.append(kwargs)
        return ["inert-atom"]

    def drain_report(self, handler, *, limit):
        self.drain_limit = limit
        for job in self.jobs:
            handler(job)
        if self.queue_error:
            raise self.queue_error
        return {"processed": len(self.jobs), "failed": self.failed, "newly_dead": 0, "pending": 0, "dead": 0, "errors": []}

    def recent_dead(self, *, limit):
        self.recent_limit = limit
        if self.recent_error:
            raise self.recent_error
        return []

    def log(self, root, report):
        self.logs.append((root, copy.deepcopy(report)))
        if self.log_error:
            raise self.log_error

    def main(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            status = self.ns["main"]()
        return status, json.loads(output.getvalue())


class LocalInterfaceTests(unittest.TestCase):
    def test_wrapper_default_omits_continuation(self):
        h = Harness()
        self.assertTrue(h.service.backfill_l1(channel="hermes", scope={"u": "x"})["ok"])
        memory, kwargs = h.calls[0]
        self.assertIs(memory, h.memory)
        self.assertEqual(kwargs, {"scope": {"channel": "normalized:hermes", "scope": {"u": "x"}}, "limit": 50, "use_llm": True, "retry_legacy": True})

    def test_wrapper_explicit_none_omits_continuation(self):
        h = Harness()
        h.service.backfill_l1(channel="hermes", scope={}, cursor=None, scan_limit=None)
        self.assertNotIn("cursor", h.calls[0][1])
        self.assertNotIn("scan_limit", h.calls[0][1])

    def test_wrapper_empty_and_zero_are_preserved(self):
        h = Harness()
        h.service.backfill_l1(channel="hermes", scope={}, cursor="", scan_limit=0)
        self.assertEqual(h.calls[0][1]["cursor"], "")
        self.assertEqual(h.calls[0][1]["scan_limit"], 0)

    def test_wrapper_each_optional_parameter_is_independent(self):
        for extra in ({"cursor": "page-2"}, {"scan_limit": 7}):
            with self.subTest(extra=extra):
                h = Harness()
                h.service.backfill_l1(channel="hermes", scope={}, **extra)
                self.assertEqual({k: v for k, v in h.calls[0][1].items() if k in {"cursor", "scan_limit"}}, extra)

    def test_repair_cli_default_to_wrapper_to_pipeline(self):
        h = Harness()
        h.env["EIMEMORY_L1_WORKER_ACTION"] = "repair"
        status, report = h.main()
        self.assertEqual((status, report["ok"], h.closed), (0, True, 1))
        self.assertEqual(h.calls[0][1]["limit"], 200)
        self.assertNotIn("cursor", h.calls[0][1])
        self.assertNotIn("scan_limit", h.calls[0][1])

    def test_repair_cli_explicit_continuation_to_pipeline(self):
        for cursor, scan in (("next-page", "11"), ("", "0")):
            with self.subTest(cursor=cursor, scan=scan):
                h = Harness()
                h.env.update({"EIMEMORY_L1_WORKER_ACTION": "repair", "EIMEMORY_L1_REPAIR_CURSOR": cursor, "EIMEMORY_L1_REPAIR_SCAN_LIMIT": scan, "EIMEMORY_L1_REPAIR_LIMIT": "3", "EIMEMORY_L1_REPAIR_CHANNEL": "test", "EIMEMORY_DEPLOY_SCOPE_TENANT": "t", "EIMEMORY_DEPLOY_SCOPE_AGENT": "a", "EIMEMORY_DEPLOY_SCOPE_USER": "u"})
                status, report = h.main()
                self.assertEqual((status, report["ok"], h.closed), (0, True, 1))
                memory, kwargs = h.calls[0]
                self.assertIs(memory, h.memory)
                self.assertEqual(kwargs, {"scope": {"channel": "normalized:test", "scope": {"tenant_id": "t", "agent_id": "a", "workspace_id": "embodied", "user_id": "u"}}, "limit": 3, "use_llm": True, "retry_legacy": True, "cursor": cursor, "scan_limit": int(scan)})

    def test_normal_drain_handler_counters_and_log(self):
        h = Harness()
        h.jobs = [{"user_text": "hello", "channel_scope": {"u": "x"}}]
        status, report = h.main()
        self.assertEqual((status, report["ok"], report["atoms_written"], h.closed), (0, True, 1, 1))
        self.assertEqual((h.drain_limit, h.recent_limit), (5, 5))
        self.assertEqual(h.extractions[0]["channel_scope"], {"u": "x"})
        self.assertEqual(h.extractions[0]["channel_id"], "hermes")
        self.assertEqual(len(h.logs), 1)
        self.assertNotIn("log_error", report)

    def test_success_report_survives_log_errors(self):
        for error in (OSError("inert log failed"), UnicodeError("inert encoding failed")):
            with self.subTest(error=type(error).__name__):
                h = Harness(log_error=error)
                status, report = h.main()
                self.assertEqual((status, report["ok"], h.closed), (0, True, 1))
                self.assertEqual(report["log_error"], type(error).__name__)
                self.assertEqual(report["processed"], 0)

    def test_failed_batch_report_survives_log_error(self):
        h = Harness(log_error=OSError("inert"), failed=2)
        status, report = h.main()
        self.assertEqual((status, report["ok"], report["failed"], h.closed), (1, False, 2, 1))
        self.assertEqual(report["log_error"], "OSError")

    def test_original_queue_failure_survives_log_errors(self):
        for log_error in (None, OSError("inert"), UnicodeError("inert")):
            with self.subTest(log_error=type(log_error).__name__):
                h = Harness(log_error=log_error, queue_error=QueueStateError("claim_failed", processed=2, handler_error="original", errors=["old"]))
                status, report = h.main()
                self.assertEqual((status, report["ok"], h.closed), (1, False, 1))
                self.assertEqual(report["error"], "claim_failed")
                self.assertEqual(report["handler_error"], "original")
                self.assertEqual(report["errors"], ["old", "claim_failed"])
                self.assertEqual(report["queue_state"], "unavailable")
                self.assertIsNone(report["pending"])
                self.assertEqual(report.get("log_error"), type(log_error).__name__ if log_error else None)

    def test_recent_dead_failure_preserves_completed_counters(self):
        h = Harness(log_error=OSError("inert"), recent_error=QueueStateError("dead_read_failed"))
        h.jobs = [{}]
        status, report = h.main()
        self.assertEqual((status, report["ok"], h.closed), (1, False, 1))
        self.assertEqual((report["processed"], report["atoms_written"], report["phase"]), (1, 1, "recent_dead"))
        self.assertEqual(report["error"], "dead_read_failed")

    def test_unrelated_logger_errors_are_not_swallowed(self):
        h = Harness(log_error=ValueError("out of scope"))
        with self.assertRaises(ValueError):
            h.main()
        self.assertEqual(h.closed, 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
