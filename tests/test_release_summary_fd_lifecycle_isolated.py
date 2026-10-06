"""Isolated ownership tests: parse source, extract one function, use only fakes."""
import ast
from pathlib import Path
from types import SimpleNamespace
import unittest

SOURCE = Path(__file__).resolve().parents[1] / 'deploy/summarize_release_closure.py'


class StopAfterOwnership(Exception):
    pass


class FakeHandle:
    def __init__(self, events):
        self.events = events

    def __enter__(self):
        self.events.append(('enter',))
        return self

    def __exit__(self, kind, value, traceback):
        self.events.append(('exit', kind, value))
        return False

    def fileno(self):
        return 73


class FdLifecycle(unittest.TestCase):
    def exercise(self, open_error=None, wrap_error=None, close_error=None):
        events = []
        stop = StopAfterOwnership('no report operations beyond fake fstat')

        def fake_open(path, flags):
            events.append(('open', path, flags))
            if open_error is not None:
                raise open_error
            return 73

        def fake_fdopen(fd, mode, *, closefd):
            events.append(('fdopen', fd, mode, closefd))
            if wrap_error is not None:
                raise wrap_error
            return FakeHandle(events)

        def fake_close(fd):
            events.append(('close', fd))
            if close_error is not None:
                raise close_error

        def fake_fstat(fd):
            events.append(('fstat', fd))
            raise stop

        tree = ast.parse(SOURCE.read_text(encoding='utf-8'))
        function = next(node for node in tree.body
                        if isinstance(node, ast.FunctionDef) and node.name == '_read_report')
        isolated = ast.Module(body=[function], type_ignores=[])
        fake_os = SimpleNamespace(O_RDONLY=1, O_CLOEXEC=2, O_NOFOLLOW=4,
                                  O_NONBLOCK=8, open=fake_open, fdopen=fake_fdopen,
                                  close=fake_close, fstat=fake_fstat)
        namespace = {'os': fake_os, 'Path': Path, 'MAX_REPORT_BYTES': 16777216}
        exec(compile(isolated, '<isolated _read_report>', 'exec'), namespace)
        expected = open_error or wrap_error or stop
        with self.assertRaises(type(expected)) as raised:
            namespace['_read_report'](Path('never-opened-report.json'))
        self.assertIs(raised.exception, expected)
        return events, stop

    def test_open_failure_never_closes_unowned_fd(self):
        events, _ = self.exercise(open_error=OSError('open failed'))
        self.assertEqual([event[0] for event in events], ['open'])

    def test_all_wrap_failures_close_once_and_preserve_exception(self):
        for kind in (OSError, ValueError, RuntimeError, MemoryError, KeyboardInterrupt, SystemExit):
            with self.subTest(kind=kind.__name__):
                events, _ = self.exercise(wrap_error=kind('wrap failed'))
                self.assertEqual([event[0] for event in events], ['open', 'fdopen', 'close'])
                self.assertEqual(events[-1], ('close', 73))
                self.assertEqual(events[1], ('fdopen', 73, 'rb', True))

    def test_cleanup_oserror_does_not_mask_original_wrap_error(self):
        for kind in (OSError, ValueError, RuntimeError, MemoryError, KeyboardInterrupt, SystemExit):
            with self.subTest(kind=kind.__name__):
                events, _ = self.exercise(wrap_error=kind('original'), close_error=OSError('cleanup'))
                self.assertEqual([event[0] for event in events], ['open', 'fdopen', 'close'])

    def test_successful_wrap_retains_context_ownership(self):
        events, stop = self.exercise()
        self.assertEqual([event[0] for event in events], ['open', 'fdopen', 'enter', 'fstat', 'exit'])
        self.assertEqual(events[0], ('open', Path('never-opened-report.json'), 15))
        self.assertEqual(events[1], ('fdopen', 73, 'rb', True))
        self.assertEqual(events[-1], ('exit', StopAfterOwnership, stop))


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--source', type=Path, default=SOURCE)
    args, remaining = parser.parse_known_args()
    SOURCE = args.source
    unittest.main(argv=[__file__, *remaining])
