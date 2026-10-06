"""Offline wiring tests: no project imports or real loop, store, or network calls."""
import ast
from pathlib import Path
from types import SimpleNamespace
import unittest


class StartLoopTaskAstTests(unittest.TestCase):
    source_path = None

    def _load_function(self, loop_callbacks):
        source_path = self.source_path
        if source_path is None:
            source_path = (
                Path(__file__).resolve().parents[1]
                / "eimemory/ei_bridge/openclaw_runtime.py"
            )
        module = ast.parse(Path(source_path).read_text(encoding="utf-8"))
        selected = [
            node for node in module.body
            if isinstance(node, ast.FunctionDef) and node.name == "_start_loop_task"
        ]
        self.assertEqual(len(selected), 1)
        function = selected[0]
        function.returns = None
        for arg in [*function.args.args, *function.args.kwonlyargs]:
            arg.annotation = None
        self.assertFalse(function.decorator_list)
        self.assertFalse(function.args.defaults)
        self.assertTrue(all(value is None for value in function.args.kw_defaults))
        allowed_calls = {
            "str", "getattr", "isinstance", "params.get", "event.get", "task.get",
            "openclaw_loop.create_task", "openclaw_loop.record_heartbeat",
        }
        for node in ast.walk(function):
            self.assertNotIsInstance(node, (ast.Import, ast.ImportFrom))
            if isinstance(node, ast.Call):
                name = ast.unparse(node.func)
                allowed_strip = (
                    isinstance(node.func, ast.Attribute)
                    and node.func.attr == "strip"
                )
                self.assertTrue(name in allowed_calls or allowed_strip, name)
        isolated = ast.fix_missing_locations(ast.Module(body=[function], type_ignores=[]))
        namespace = {"openclaw_loop": loop_callbacks}
        exec(compile(isolated, "<isolated-local-start-loop-task>", "exec"), namespace)
        return namespace["_start_loop_task"]

    def _run_case(self, *, task=None, create_error=None, heartbeat_error=None,
                  command=None, event=None, use_default_task=True):
        calls = []
        if task is None and use_default_task:
            task = {"task_id": "T", "extra": {"kept": True}}

        def create_task(**kwargs):
            calls.append(("create", kwargs))
            if create_error is not None:
                raise create_error
            return task

        def record_heartbeat(task_id, **kwargs):
            calls.append(("heartbeat", task_id, kwargs))
            if heartbeat_error is not None:
                raise heartbeat_error

        function = self._load_function(SimpleNamespace(
            create_task=create_task, record_heartbeat=record_heartbeat,
        ))
        if command is None:
            command = SimpleNamespace(
                command_id="C", params={"raw_text": " hello "},
                target=SimpleNamespace(capability="vision.describe"),
            )
        result = function(command=command, event={} if event is None else event)
        return result, calls, task

    def test_success_identity_and_exact_wiring(self):
        result, calls, task = self._run_case()
        self.assertIs(result, task)
        self.assertEqual(calls[0], ("create", {
            "title": "Feishu command: hello", "objective": "vision.describe",
            "source": "feishu", "owner": "openclaw", "risk_level": "low",
            "report_policy": "always", "dedupe_key": "feishu:C",
        }))
        self.assertEqual(calls[1], ("heartbeat", "T", {
            "lease_seconds": 300, "progress": "feishu command received",
            "source": "feishu",
        }))
        self.assertEqual(len(calls), 2)

    def test_creation_error_remains_error_only(self):
        result, calls, _ = self._run_case(create_error=RuntimeError("create failed"))
        self.assertEqual(result, {"error": "create failed"})
        self.assertEqual(len(calls), 1)

    def test_heartbeat_error_preserves_fields_without_mutation(self):
        result, calls, task = self._run_case(heartbeat_error=RuntimeError("heartbeat failed"))
        self.assertEqual(result, {**task, "error": "heartbeat failed"})
        self.assertEqual(result["task_id"], "T")
        self.assertIs(result["extra"], task["extra"])
        self.assertNotIn("error", task)
        self.assertEqual(len(calls), 2)

    def test_heartbeat_error_overwrites_error_in_copy(self):
        result, _, task = self._run_case(
            task={"task_id": "T", "error": "old"}, heartbeat_error=ValueError("new"),
        )
        self.assertEqual(result, {"task_id": "T", "error": "new"})
        self.assertEqual(task["error"], "old")

    def test_pre_creation_error_still_caught(self):
        result, calls, _ = self._run_case(command=SimpleNamespace(command_id="C", params={}))
        self.assertEqual(set(result), {"error"})
        self.assertIsInstance(result["error"], str)
        self.assertFalse(calls)

    def test_fallback_parameter_wiring_unchanged(self):
        _, calls, _ = self._run_case(
            command=SimpleNamespace(command_id="", params=None, target=SimpleNamespace(capability="")),
            event={"event_id": "E", "query": " x "},
        )
        self.assertEqual(calls[0][1]["title"], "Feishu command: x")
        self.assertEqual(calls[0][1]["dedupe_key"], "feishu:E")
        self.assertEqual(calls[0][1]["objective"], "route Feishu command to OpenClaw agent")

    def test_empty_input_defaults_unchanged(self):
        _, calls, _ = self._run_case(command=SimpleNamespace(target=SimpleNamespace(capability="")))
        self.assertEqual(calls[0][1]["title"], "Feishu command")
        self.assertIsNone(calls[0][1]["dedupe_key"])

    def test_base_exception_not_swallowed(self):
        with self.assertRaises(KeyboardInterrupt):
            self._run_case(heartbeat_error=KeyboardInterrupt())

    def test_non_dict_creation_result_remains_error_only(self):
        # Invalid external return values have no assumed task contract.
        # Assert the existing error-only boundary, not a particular error text.
        for value in (None, [], "invalid", 0):
            with self.subTest(value=value):
                result, calls, _ = self._run_case(task=value, use_default_task=False)
                self.assertEqual(set(result), {"error"})
                self.assertIsInstance(result["error"], str)
                self.assertTrue(result["error"])
                self.assertEqual(len(calls), 1)


if __name__ == "__main__":
    unittest.main()
