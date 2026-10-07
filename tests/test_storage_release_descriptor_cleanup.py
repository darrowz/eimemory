"""Isolated local descriptor-ownership tests; no production module import.

Only the two named function ASTs execute. Subject paths are PurePosixPath;
all os/stat operations and the entry-binding check are inert event fakes.
Source reading below is harness input, never a subject filesystem operation.
Run from repository root: python -m unittest discover -s tests -p
    'test_storage_release_descriptor_cleanup.py' -v
"""
import ast
from contextlib import contextmanager
from pathlib import Path, PurePosixPath
from types import SimpleNamespace
import unittest


class SubjectError(RuntimeError):
    pass


class SyntheticExit(BaseException):
    pass


class FakeOS:
    O_RDONLY = 1
    O_DIRECTORY = 2
    O_NOFOLLOW = 4

    def __init__(self, events, failures, modes):
        self.events = events
        self.failures = dict(failures)
        self.modes = dict(modes)
        self.next_fd = 10

    def emit(self, event):
        self.events.append(event)
        error = self.failures.pop(event, None)
        if error is not None:
            raise error

    def open(self, name, flags, *, dir_fd=None):
        self.emit(('open', str(name), flags, dir_fd))
        descriptor = self.next_fd
        self.next_fd += 10
        return descriptor

    def fstat(self, descriptor):
        self.emit(('fstat', descriptor))
        return SimpleNamespace(st_mode=self.modes.get(descriptor, 'directory'))

    def stat(self, name, *, dir_fd, follow_symlinks):
        self.emit(('stat', name, dir_fd, follow_symlinks))
        return SimpleNamespace(st_mode=self.modes.get('target', 'directory'))

    def mkdir(self, name, *, mode, dir_fd):
        self.emit(('mkdir', name, mode, dir_fd))

    def fsync(self, descriptor):
        self.emit(('fsync', descriptor))

    def close(self, descriptor):
        self.emit(('close', descriptor))


def load_subject(events, failures=(), modes=()):
    source = Path(__file__).resolve().parents[1] / 'deploy/storage_release_transaction.py'
    parsed = ast.parse(source.read_text(encoding='utf-8'))
    names = {'_open_directory_fds', '_durably_sync_path_posix'}
    nodes = [node for node in parsed.body
             if isinstance(node, ast.FunctionDef) and node.name in names]
    assert {node.name for node in nodes} == names and len(nodes) == 2
    fake_os = FakeOS(events, failures, modes)

    def opaque(descriptor, *, parent_fd, name, message):
        fake_os.emit(('check', descriptor, parent_fd, name, message))

    namespace = {
        'Path': PurePosixPath,
        'contextmanager': contextmanager,
        'StorageReleaseTransactionError': SubjectError,
        'os': fake_os,
        'stat': SimpleNamespace(S_ISDIR=lambda mode: mode == 'directory',
                                S_ISREG=lambda mode: mode == 'regular'),
        '_assert_fd_matches_entry': opaque,
    }
    module = ast.Module(body=nodes, type_ignores=[])
    exec(compile(module, str(source), 'exec'), namespace)
    return namespace


# Literal event oracles include flags, names, parents, opaque arguments and order.
ROOT = [('open', '/', 7, None)]
A = ROOT + [
    ('open', 'a', 7, 10),
    ('fstat', 20),
    ('check', 20, 10, 'a', 'directory entry changed while opening path'),
]
B_OPEN = A + [('open', 'b', 7, 20), ('fstat', 30)]
TARGET = A + [('stat', 'b', 20, False), ('open', 'b', 7, 20)]
TARGET_CHECK = ('check', 30, 20, 'b', 'durable sync entry changed during fsync')
CLOSE_ANCESTORS = [('close', 20), ('close', 10)]


class DescriptorCleanupTests(unittest.TestCase):
    def setUp(self):
        self.events = []

    def subject(self, failures=(), modes=()):
        return load_subject(self.events, failures, modes)

    def opening_failure(self, error, close_error=None):
        failures = {('fstat', 30): error}
        if close_error is not None:
            failures[('close', 30)] = close_error
        subject = self.subject(failures)
        expected = close_error if close_error is not None else error
        with self.assertRaises(type(expected)) as caught:
            with subject['_open_directory_fds']('/a/b'):
                self.fail('must not yield')
        self.assertIs(caught.exception, expected)
        self.assertEqual(self.events, B_OPEN + [('close', 30)] + CLOSE_ANCESTORS)
        if close_error is not None:
            self.assertIs(close_error.__context__, error)

    def sync_failure(self, error, close_error=None):
        failures = {TARGET_CHECK: error}
        if close_error is not None:
            failures[('close', 30)] = close_error
        subject = self.subject(failures)
        expected = close_error if close_error is not None else error
        with self.assertRaises(type(expected)) as caught:
            subject['_durably_sync_path_posix']('/a/b', boundary='/a')
        self.assertIs(caught.exception, expected)
        self.assertEqual(self.events, TARGET + [TARGET_CHECK, ('close', 30)] + CLOSE_ANCESTORS)
        if close_error is not None:
            self.assertIs(close_error.__context__, error)

    def test_open_success_yields_registered_entries(self):
        subject = self.subject()
        with subject['_open_directory_fds']('/a') as entries:
            self.assertEqual(entries, [(None, '/', 10), (10, 'a', 20)])
            self.events.append(('yield',))
        self.assertEqual(self.events, A + [('yield',)] + CLOSE_ANCESTORS)

    def test_open_fstat_error_closes_new_fd_then_ancestors(self):
        self.opening_failure(OSError('synthetic fstat'))

    def test_open_fstat_baseexception_is_not_swallowed(self):
        self.opening_failure(SyntheticExit('synthetic exit'))

    def test_open_fstat_close_error_keeps_ancestor_cleanup(self):
        self.opening_failure(OSError('synthetic fstat'), OSError('synthetic close'))

    def test_open_fstat_close_baseexception_keeps_ancestor_cleanup(self):
        self.opening_failure(SyntheticExit('synthetic fstat'), SyntheticExit('synthetic close'))

    def test_open_non_directory_existing_cleanup(self):
        subject = self.subject(modes={30: 'other'})
        with self.assertRaisesRegex(SubjectError, '^path component is not a directory$'):
            with subject['_open_directory_fds']('/a/b'):
                self.fail('must not yield')
        self.assertEqual(self.events, B_OPEN + [('close', 30)] + CLOSE_ANCESTORS)

    def test_open_non_directory_close_error_existing_cleanup(self):
        error = OSError('synthetic close')
        subject = self.subject({('close', 30): error}, {30: 'other'})
        with self.assertRaises(OSError) as caught:
            with subject['_open_directory_fds']('/a/b'):
                self.fail('must not yield')
        self.assertIs(caught.exception, error)
        self.assertEqual(self.events, B_OPEN + [('close', 30)] + CLOSE_ANCESTORS)

    def test_open_registered_check_error_existing_cleanup(self):
        check = ('check', 30, 20, 'b', 'directory entry changed while opening path')
        error = RuntimeError('synthetic opaque error')
        subject = self.subject({check: error})
        with self.assertRaises(RuntimeError) as caught:
            with subject['_open_directory_fds']('/a/b'):
                self.fail('must not yield')
        self.assertIs(caught.exception, error)
        self.assertEqual(self.events, B_OPEN + [check, ('close', 30)] + CLOSE_ANCESTORS)

    def test_open_body_error_existing_cleanup(self):
        error = RuntimeError('synthetic body')
        subject = self.subject()
        with self.assertRaises(RuntimeError) as caught:
            with subject['_open_directory_fds']('/a'):
                self.events.append(('body',))
                raise error
        self.assertIs(caught.exception, error)
        self.assertEqual(self.events, A + [('body',)] + CLOSE_ANCESTORS)

    def test_open_registered_close_error_existing_stop(self):
        error = OSError('synthetic registered close')
        subject = self.subject({('close', 20): error})
        with self.assertRaises(OSError) as caught:
            with subject['_open_directory_fds']('/a'):
                pass
        self.assertIs(caught.exception, error)
        self.assertEqual(self.events, A + [('close', 20)])

    def test_open_create_retry_fstat_error(self):
        error = OSError('synthetic fstat')
        subject = self.subject({('open', 'b', 7, 20): FileNotFoundError('synthetic missing'),
                                ('fstat', 30): error})
        with self.assertRaises(OSError) as caught:
            with subject['_open_directory_fds']('/a/b', create=True):
                self.fail('must not yield')
        self.assertIs(caught.exception, error)
        self.assertEqual(self.events, A + [
            ('open', 'b', 7, 20), ('mkdir', 'b', 448, 20),
            ('open', 'b', 7, 20), ('fstat', 30), ('close', 30),
        ] + CLOSE_ANCESTORS)

    def test_sync_directory_success_check_and_sync_order(self):
        subject = self.subject()
        subject['_durably_sync_path_posix']('/a/b', boundary='/a')
        self.assertEqual(self.events, TARGET + [
            TARGET_CHECK, TARGET_CHECK, ('fsync', 30), TARGET_CHECK,
            ('check', 20, 10, 'a', 'durable sync entry changed during fsync'),
            ('fsync', 20),
            ('check', 20, 10, 'a', 'durable sync entry changed during fsync'),
            ('close', 30),
        ] + CLOSE_ANCESTORS)

    def test_sync_opaque_error_closes_new_fd_then_ancestors(self):
        self.sync_failure(RuntimeError('synthetic opaque error'))

    def test_sync_opaque_baseexception_is_not_swallowed(self):
        self.sync_failure(SyntheticExit('synthetic opaque exit'))

    def test_sync_opaque_close_error_keeps_ancestor_cleanup(self):
        self.sync_failure(RuntimeError('synthetic opaque error'), OSError('synthetic close'))

    def test_sync_opaque_close_baseexception_keeps_ancestor_cleanup(self):
        self.sync_failure(SyntheticExit('synthetic opaque exit'), SyntheticExit('synthetic close'))

    def test_sync_directory_fsync_error_existing_cleanup(self):
        error = OSError('synthetic sync')
        subject = self.subject({('fsync', 30): error})
        with self.assertRaises(OSError) as caught:
            subject['_durably_sync_path_posix']('/a/b', boundary='/a')
        self.assertIs(caught.exception, error)
        self.assertEqual(self.events, TARGET + [
            TARGET_CHECK, TARGET_CHECK, ('fsync', 30), ('close', 30),
        ] + CLOSE_ANCESTORS)

    def test_sync_regular_success_existing_cleanup(self):
        subject = self.subject(modes={'target': 'regular', 30: 'regular'})
        subject['_durably_sync_path_posix']('/a/b', boundary='/a')
        self.assertEqual(self.events, A + [
            ('stat', 'b', 20, False), ('open', 'b', 5, 20), ('fstat', 30),
            ('fsync', 30), TARGET_CHECK, ('close', 30),
            ('check', 20, 10, 'a', 'durable sync entry changed during fsync'),
            ('fsync', 20),
            ('check', 20, 10, 'a', 'durable sync entry changed during fsync'),
        ] + CLOSE_ANCESTORS)

    def test_sync_regular_fstat_error_existing_cleanup(self):
        error = OSError('synthetic regular fstat')
        subject = self.subject({('fstat', 30): error}, {'target': 'regular'})
        with self.assertRaises(OSError) as caught:
            subject['_durably_sync_path_posix']('/a/b', boundary='/a')
        self.assertIs(caught.exception, error)
        self.assertEqual(self.events, A + [
            ('stat', 'b', 20, False), ('open', 'b', 5, 20),
            ('fstat', 30), ('close', 30),
        ] + CLOSE_ANCESTORS)


if __name__ == '__main__':
    unittest.main()
