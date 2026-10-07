"""Stdlib-only wrapper regression; no project imports or real lock/inner calls."""
import ast
from pathlib import Path
from types import SimpleNamespace
from typing import Any
import unittest

SOURCE = Path(__file__).resolve().parents[1] / 'eimemory' / 'ops' / 'timer_monitor.py'


def load_wrapper():
    tree = ast.parse(SOURCE.read_text(encoding='utf-8'), filename=str(SOURCE))
    functions = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == 'check_user_systemd_timers']
    if len(functions) != 1:
        raise AssertionError('Expected exactly one top-level timer wrapper')
    node = functions[0]
    if node.decorator_list:
        raise AssertionError('Unexpected wrapper decorators')
    isolated = ast.Module(body=[node], type_ignores=[])
    namespace = {'Any': Any, 'Path': Path}
    exec(compile(ast.fix_missing_locations(isolated), str(SOURCE), 'exec'), namespace)
    return namespace


class TimerMonitorTimeoutOriginTests(unittest.TestCase):
    def exercise(self, *, entry_error=None, body_error=None, exit_error=None, root='/fixed-root', flags=None):
        events, expected_result = [], {'fixed': 'result'}
        class FakeContext:
            def __enter__(self):
                events.append('enter_attempt')
                if entry_error is not None:
                    raise entry_error
                events.append('entered')
                return self
            def __exit__(self, kind, error, traceback):
                events.append('exit')
                if exit_error is not None:
                    raise exit_error
                return False
        def fake_lock(path, timeout):
            self.assertEqual(path, Path('/fixed-root/state/timer-monitor.lock'))
            self.assertEqual(timeout, 0)
            events.append('lock_factory')
            return FakeContext()
        def fake_inner(runtime, **kwargs):
            events.append('inner')
            if body_error is not None:
                raise body_error
            return expected_result
        namespace = load_wrapper()
        namespace.update(interprocess_lock=fake_lock, _check_user_systemd_timers=fake_inner)
        runtime = SimpleNamespace(store=SimpleNamespace(root=root))
        expected_error = exit_error if exit_error is not None else body_error if body_error is not None else entry_error
        acquisition_timeout = isinstance(entry_error, TimeoutError)
        if expected_error is not None and not acquisition_timeout:
            with self.assertRaises(type(expected_error)) as raised:
                namespace['check_user_systemd_timers'](runtime, **(flags or {}))
            self.assertIs(raised.exception, expected_error)
        else:
            actual = namespace['check_user_systemd_timers'](runtime, **(flags or {}))
            expected = {'ok': True, 'report_type': 'ops_timer_monitor', 'skipped': 'already_running', 'persisted': False, 'notified': False} if acquisition_timeout else expected_result
            self.assertEqual(actual, expected)
        bypass = root is None or flags == {'persist': False, 'notify': False}
        expected_events = ['inner'] if bypass else ['lock_factory', 'enter_attempt'] if entry_error is not None else ['lock_factory', 'enter_attempt', 'entered', 'inner', 'exit']
        self.assertEqual(events, expected_events)

    def test_acquisition_timeout_keeps_skip(self):
        self.exercise(entry_error=TimeoutError('fixed acquisition'))

    def test_body_timeout_propagates(self):
        self.exercise(body_error=TimeoutError('fixed body'))

    def test_exit_timeout_propagates(self):
        self.exercise(exit_error=TimeoutError('fixed exit'))

    def test_exit_timeout_during_body_error_propagates(self):
        self.exercise(body_error=ValueError('fixed body'), exit_error=TimeoutError('fixed exit'))

    def test_normal_result(self):
        self.exercise()

    def test_other_entry_error_propagates(self):
        self.exercise(entry_error=ValueError('fixed entry'))

    def test_other_body_error_propagates(self):
        self.exercise(body_error=ValueError('fixed body'))

    def test_other_exit_error_propagates(self):
        self.exercise(exit_error=ValueError('fixed exit'))

    def test_no_effect_bypass_timeout_propagates(self):
        self.exercise(body_error=TimeoutError('fixed bypass'), flags={'persist': False, 'notify': False})

    def test_missing_root_bypass_timeout_propagates(self):
        self.exercise(body_error=TimeoutError('fixed bypass'), root=None)


if __name__ == '__main__':
    unittest.main(verbosity=2)
