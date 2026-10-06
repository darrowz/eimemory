"""AST-isolated path error tests; no project import or real path resolution."""
import argparse
import ast
import contextlib
import io
import json
from pathlib import Path
import sys
import types
import unittest

SOURCE = Path(__file__).parents[1] / "deploy" / "verify_openclaw_plugin_runtime.py"


def load_isolated(source):
    tree = ast.parse(source.read_text(encoding="utf-8"))
    selected = [node for node in tree.body if isinstance(node, ast.FunctionDef)
                and node.name in {"_string_set", "verify_openclaw_plugin_runtime", "main"}]
    if {node.name for node in selected} != {"_string_set", "verify_openclaw_plugin_runtime", "main"}:
        raise AssertionError("isolated function set changed")
    allowed_calls = {
        "isinstance", "any", "set", "str", "len", "sorted", "print",
        "OpenClawRuntimeError", "Path", "root_value.strip", "source.strip",
        "Path(root_value).resolve",
        "Path(expected_root).resolve", "Path(source).resolve",
        "(required_root / 'index.js').resolve", "plugin.get", "payload.get",
        "contracts.get", "item.get", "tool_names.issubset", "_string_set",
        "','.join", "argparse.ArgumentParser", "parser.add_argument",
        "parser.parse_args", "sys.stdin.read", "raw.encode", "json.loads",
        "verify_openclaw_plugin_runtime", "parser.exit",
    }
    for function in selected:
        if any(isinstance(node, (ast.Import, ast.ImportFrom)) for node in ast.walk(function)):
            raise AssertionError("imports in isolated function")
        calls = {ast.unparse(node.func) for node in ast.walk(function) if isinstance(node, ast.Call)}
        if not calls <= allowed_calls:
            raise AssertionError(f"unexpected calls: {calls - allowed_calls}")
    constants = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            name = node.targets[0].id
            if name in {"PLUGIN_ID", "REQUIRED_HOOKS", "REQUIRED_TOOLS"}:
                constants[name] = ast.literal_eval(node.value)
    if set(constants) != {"PLUGIN_ID", "REQUIRED_HOOKS", "REQUIRED_TOOLS"}:
        raise AssertionError("constant set changed")

    class Error(RuntimeError):
        pass

    state = types.SimpleNamespace(calls=[], failure_at=None, failure=None)

    class InertPath:
        def __init__(self, value):
            self.value = value.value if isinstance(value, InertPath) else value

        def resolve(self, *, strict):
            if strict is not True:
                raise AssertionError("strict flag changed")
            state.calls.append(self.value)
            if len(state.calls) == state.failure_at:
                raise state.failure
            if "\x00" in self.value:
                raise ValueError("synthetic embedded null character")
            return self

        def __truediv__(self, child):
            return InertPath(self.value + "/" + child)

        @property
        def parent(self):
            return InertPath(self.value.rsplit("/", 1)[0])

        def __eq__(self, other):
            return isinstance(other, InertPath) and self.value == other.value

    fake_sys = types.SimpleNamespace(stdin=None)
    namespace = dict(constants, argparse=argparse, json=json, Path=InertPath,
                     sys=fake_sys, MAX_INSPECT_BYTES=4096, OpenClawRuntimeError=Error)
    module = ast.Module(body=[ast.ImportFrom(module="__future__",
                        names=[ast.alias(name="annotations")], level=0), *selected], type_ignores=[])
    ast.fix_missing_locations(module)
    # Preflight above precedes compilation. Path is always inert, never pathlib.Path.
    exec(compile(module, "<isolated-runtime-paths>", "exec"), namespace)
    return namespace, state, fake_sys


class OpenClawRuntimePathLocalTests(unittest.TestCase):
    def setUp(self):
        self.namespace, self.state, self.fake_sys = load_isolated(SOURCE)
        self.error = self.namespace["OpenClawRuntimeError"]
        self.payload = {
            "plugin": {"id": "eimemory-bridge", "enabled": True, "activated": True,
                       "status": "loaded", "rootDir": "/inert/root", "origin": "config",
                       "source": "/inert/root/index.js", "toolNames": ["eimemory_bridge_status"],
                       "contracts": {"tools": ["eimemory_bridge_status"]}},
            "typedHooks": [{"name": name} for name in sorted(self.namespace["REQUIRED_HOOKS"])],
        }

    def verify(self):
        return self.namespace["verify_openclaw_plugin_runtime"](
            self.payload, expected_root="/inert/root")

    def test_nul_path_strings_are_normalized(self):
        for field, stage in [("rootDir", "root"), ("source", "source")]:
            with self.subTest(field=field):
                self.setUp()
                self.payload["plugin"][field] = "\x00"
                with self.assertRaises(self.error) as caught:
                    self.verify()
                self.assertEqual(str(caught.exception), f"runtime plugin {stage} cannot be resolved")
                self.assertIsInstance(caught.exception.__cause__, ValueError)

    def test_all_resolve_value_errors_are_normalized(self):
        for position in range(1, 5):
            with self.subTest(resolve_position=position):
                self.setUp()
                failure = ValueError("synthetic malformed path")
                self.state.failure_at, self.state.failure = position, failure
                with self.assertRaises(self.error) as caught:
                    self.verify()
                stage = "root" if position <= 2 else "source"
                self.assertEqual(str(caught.exception), f"runtime plugin {stage} cannot be resolved")
                self.assertIs(caught.exception.__cause__, failure)
                self.assertEqual(len(self.state.calls), position)

    def test_all_resolve_os_errors_keep_existing_diagnostic(self):
        for position in range(1, 5):
            with self.subTest(resolve_position=position):
                self.setUp()
                failure = OSError("synthetic unavailable path")
                self.state.failure_at, self.state.failure = position, failure
                with self.assertRaises(self.error) as caught:
                    self.verify()
                stage = "root" if position <= 2 else "source"
                self.assertEqual(str(caught.exception), f"runtime plugin {stage} cannot be resolved")
                self.assertIs(caught.exception.__cause__, failure)
                self.assertEqual(len(self.state.calls), position)

    def test_valid_paths_preserve_report_and_arguments(self):
        self.assertEqual(self.verify(), {"ok": True, "plugin_id": "eimemory-bridge",
                                       "hook_count": 8, "tool_count": 1})
        self.assertEqual(self.state.calls, ["/inert/root", "/inert/root",
                                          "/inert/root/index.js", "/inert/root/index.js"])

    def test_nul_path_cli_keeps_controlled_diagnostic(self):
        self.payload["plugin"]["rootDir"] = "\x00"
        self.fake_sys.stdin = io.StringIO(json.dumps(self.payload))
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            with self.assertRaises(SystemExit) as caught:
                self.namespace["main"](["--expected-root", "/inert/root"])
        self.assertEqual(caught.exception.code, 2)
        self.assertEqual(out.getvalue(), "")
        self.assertEqual(err.getvalue(), "OpenClaw runtime verification failed: "
                         "runtime plugin root cannot be resolved\n")

    def test_stdin_unicode_error_still_has_controlled_diagnostic(self):
        class BrokenInput:
            def read(self, size):
                raise UnicodeDecodeError("utf-8", b"\xff", 0, 1, "synthetic invalid byte")
        self.fake_sys.stdin = BrokenInput()
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            with self.assertRaises(SystemExit) as caught:
                self.namespace["main"](["--expected-root", "/inert/root"])
        self.assertEqual(caught.exception.code, 2)
        self.assertEqual(out.getvalue(), "")
        self.assertTrue(err.getvalue().startswith("OpenClaw runtime verification failed: "))
        self.assertEqual(self.state.calls, [])

    def test_stdin_value_error_still_propagates(self):
        failure = ValueError("synthetic closed input")
        class BrokenInput:
            def read(self, size):
                raise failure
        self.fake_sys.stdin = BrokenInput()
        with self.assertRaises(ValueError) as caught:
            self.namespace["main"](["--expected-root", "/inert/root"])
        self.assertIs(caught.exception, failure)
        self.assertEqual(self.state.calls, [])

    def test_unrelated_verifier_value_error_still_propagates(self):
        failure = ValueError("synthetic unrelated programming failure")
        def broken_verifier(*args, **kwargs):
            raise failure
        self.namespace["verify_openclaw_plugin_runtime"] = broken_verifier
        self.fake_sys.stdin = io.StringIO("{}")
        with self.assertRaises(ValueError) as caught:
            self.namespace["main"](["--expected-root", "/inert/root"])
        self.assertIs(caught.exception, failure)
        self.assertEqual(self.state.calls, [])


if __name__ == "__main__":
    if len(sys.argv) > 2 and sys.argv[1] == "--source":
        SOURCE = Path(sys.argv[2])
        del sys.argv[1:3]
    unittest.main()
