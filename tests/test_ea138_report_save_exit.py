"""EA-138: pure AST regression for final-report exception return mapping.

Run with Python's standard-library unittest runner. Do not import the runner or
execute main: validate its final try/return AST exactly, replace only the
pre-inspected file-writing statement with inert save_reports, then execute that
small control-flow fragment with an inert logger and restricted builtins.
"""
from __future__ import annotations

import ast
from copy import deepcopy
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
RUNNER = ROOT / "scripts" / "run_full_eval.py"
EXPECTED = '''\
try:
    with open("/tmp/full_eval_report.json", "w", encoding="utf-8") as f:
        json.dump(final, f, ensure_ascii=False, indent=2)
    log("Final report saved to /tmp/full_eval_report.json")
except Exception as exc:
    log(f"Failed to save report: {exc}")
return 0
'''
SUCCESS = "Final report saved to /tmp/full_eval_report.json"


def extract_tail(source):
    """Fail closed on unexpected syntax before compiling the tiny fragment."""
    tree = ast.parse(source)
    mains = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "main"]
    if len(mains) != 1:
        raise AssertionError("Expected one main function")
    nodes = deepcopy(mains[0].body[-2:])
    if len(nodes) != 2 or not isinstance(nodes[0], ast.Try):
        raise AssertionError("Expected final try and return")
    expected = ast.parse(EXPECTED)
    candidate_expected = deepcopy(expected)
    candidate_expected.body[0].handlers[0].body.append(ast.Return(ast.Constant(1)))
    actual = ast.Module(body=nodes, type_ignores=[])
    dump = lambda node: ast.dump(node, include_attributes=False)
    if dump(actual) not in (dump(expected), dump(candidate_expected)):
        raise AssertionError("Unexpected final output boundary")
    # The exact validated with/open/json.dump statement is never compiled.
    nodes[0].body[0] = ast.Expr(ast.Call(ast.Name("save_reports", ast.Load()), [], []))
    fn = ast.parse("def isolated_tail():\n    pass\n").body[0]
    fn.body = nodes
    module = ast.fix_missing_locations(ast.Module(body=[fn], type_ignores=[]))
    allowed = {ast.Module, ast.FunctionDef, ast.arguments, ast.Try, ast.Expr,
               ast.Call, ast.Name, ast.Load, ast.Constant, ast.ExceptHandler,
               ast.JoinedStr, ast.FormattedValue, ast.Return}
    if any(type(node) not in allowed for node in ast.walk(module)):
        raise AssertionError("Unexpected executable AST node")
    calls = [node for node in ast.walk(module) if isinstance(node, ast.Call)]
    if any(not isinstance(node.func, ast.Name) or node.func.id not in
           {"save_reports", "log"} for node in calls):
        raise AssertionError("Unexpected executable call")
    return compile(module, "<ea138-isolated-report-tail>", "exec")


def load_tail(source, save_reports, logger):
    namespace = {"__builtins__": {"Exception": Exception},
                 "save_reports": save_reports, "log": logger}
    exec(extract_tail(source), namespace)
    return namespace["isolated_tail"]


class ReportSaveExitTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = RUNNER.read_text(encoding="utf-8")

    def test_success_returns_zero_and_logs_once(self):
        events = []
        run = load_tail(self.source, lambda: events.append("saved"), events.append)
        self.assertEqual(run(), 0)
        self.assertEqual(events, ["saved", SUCCESS])

    def test_exception_returns_one_and_preserves_diagnostic(self):
        for error in (OSError("disk full"), ValueError("serialization"),
                      TypeError("unsupported value"), Exception("")):
            with self.subTest(error=type(error).__name__):
                events = []
                def fail():
                    events.append("attempt")
                    raise error
                run = load_tail(self.source, fail, events.append)
                self.assertEqual(run(), 1)
                self.assertEqual(events, ["attempt", f"Failed to save report: {error}"])

    def test_base_exceptions_still_propagate(self):
        for error in (KeyboardInterrupt(), SystemExit(7)):
            with self.subTest(error=type(error).__name__):
                logs = []
                def fail():
                    raise error
                with self.assertRaises(type(error)) as caught:
                    load_tail(self.source, fail, logs.append)()
                self.assertIs(caught.exception, error)
                self.assertEqual(logs, [])

    def test_error_logger_exception_still_propagates(self):
        error = RuntimeError("logger failed")
        def fail_save():
            raise OSError("save failed")
        def fail_log(message):
            raise error
        with self.assertRaises(RuntimeError) as caught:
            load_tail(self.source, fail_save, fail_log)()
        self.assertIs(caught.exception, error)

    def test_success_logger_exception_uses_existing_catch_boundary(self):
        logs = []
        def logger(message):
            logs.append(message)
            if message == SUCCESS:
                raise ValueError("success log failed")
        run = load_tail(self.source, lambda: None, logger)
        self.assertEqual(run(), 1)
        self.assertEqual(logs, [SUCCESS, "Failed to save report: success log failed"])

    def test_unexpected_tail_call_is_rejected_before_compile(self):
        mutated = self.source.replace('json.dump(final, f, ensure_ascii=False, indent=2)',
                                      'unexpected_call()')
        self.assertNotEqual(mutated, self.source)
        with self.assertRaisesRegex(AssertionError, "Unexpected final output boundary"):
            extract_tail(mutated)

    def test_surrounding_module_and_main_are_never_executed(self):
        source = "raise AssertionError('module executed')\n" + self.source
        # Remove the future directive only because the synthetic sentinel precedes it.
        source = source.replace("from __future__ import annotations\n", "")
        source = source.replace("def main() -> int:\n",
                                "def main() -> int:\n    raise AssertionError('main executed')\n")
        run = load_tail(source, lambda: None, lambda message: None)
        self.assertEqual(run(), 0)


if __name__ == "__main__":
    unittest.main()
