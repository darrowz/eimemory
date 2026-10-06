"""Isolated descriptor-ownership regression tests; never imports the project.

Only the helper's AST is compiled. All filesystem-facing names in that helper
are in-memory fakes, and the successful wrapping path stops at fake fstat.
Importing this test module neither reads target bytes nor consumes sys.argv.
"""
import ast
from pathlib import Path
import unittest


def build_suite(source_text):
    tree = ast.parse(source_text)
    functions = [node for node in tree.body
                 if isinstance(node, ast.FunctionDef)
                 and node.name == 'import_native_memory_lines']
    if len(functions) != 1:
        raise AssertionError('expected exactly one target helper')
    module = ast.Module(body=functions, type_ignores=[])
    code = compile(ast.fix_missing_locations(module), '<isolated-native-memory>', 'exec')

    class FakePath:
        name = 'MEMORY.md'

        def __init__(self, value):
            self.value = value

        def is_symlink(self):
            return False

    class FakeHandle:
        def __init__(self, calls):
            self.calls = calls
            self.exited_with = None

        def __enter__(self):
            self.calls.append(('enter',))
            return self

        def fileno(self):
            self.calls.append(('fileno',))
            return 17

        def __exit__(self, kind, value, traceback):
            self.calls.append(('exit',))
            self.exited_with = value
            return False

    class FakeOS:
        O_RDONLY = 0
        O_NOFOLLOW = 1
        O_BINARY = 2

        def __init__(self, *, open_error=None, wrap_error=None, close_error=None):
            self.calls = []
            self.open_error = open_error
            self.wrap_error = wrap_error
            self.close_error = close_error
            self.stop = RuntimeError('stop after context entry; no real metadata or reads')
            self.handle = FakeHandle(self.calls)

        def open(self, path, flags):
            self.calls.append(('open', flags))
            if self.open_error is not None:
                raise self.open_error
            return 17

        def fdopen(self, descriptor, mode):
            self.calls.append(('fdopen', descriptor, mode))
            if self.wrap_error is not None:
                raise self.wrap_error
            return self.handle

        def close(self, descriptor):
            self.calls.append(('close', descriptor))
            if self.close_error is not None:
                raise self.close_error

        def fstat(self, descriptor):
            self.calls.append(('fstat', descriptor))
            raise self.stop

    class DescriptorOwnershipTests(unittest.TestCase):
        def invoke(self, fake_os):
            namespace = {'Path': FakePath, 'os': fake_os}
            exec(code, namespace)
            return namespace['import_native_memory_lines'](
                None, path='in-memory', lines=[], expected_digest='', scope=None)

        def assert_original(self, fake_os, error):
            try:
                self.invoke(fake_os)
            except BaseException as caught:
                self.assertIs(caught, error)
            else:
                self.fail('expected the original exception')

        def test_open_failure_does_not_close_unowned_descriptor(self):
            error = OSError('fake open failure')
            fake = FakeOS(open_error=error)
            self.assert_original(fake, error)
            self.assertEqual(fake.calls, [('open', 3)])

        def test_wrap_failure_closes_descriptor_once(self):
            error = ValueError('fake wrapping failure')
            fake = FakeOS(wrap_error=error)
            self.assert_original(fake, error)
            self.assertEqual(fake.calls, [('open', 3), ('fdopen', 17, 'rb'), ('close', 17)])

        def test_close_oserror_does_not_mask_wrapping_error(self):
            error = ValueError('original wrapping failure')
            fake = FakeOS(wrap_error=error, close_error=OSError('fake close failure'))
            self.assert_original(fake, error)
            self.assertEqual(fake.calls, [('open', 3), ('fdopen', 17, 'rb'), ('close', 17)])

        def test_wrap_baseexception_also_releases_untransferred_descriptor(self):
            error = KeyboardInterrupt('fake interruption')
            fake = FakeOS(wrap_error=error)
            self.assert_original(fake, error)
            self.assertEqual(fake.calls, [('open', 3), ('fdopen', 17, 'rb'), ('close', 17)])

        def test_wrapped_handle_retains_context_cleanup(self):
            fake = FakeOS()
            self.assert_original(fake, fake.stop)
            self.assertIs(fake.handle.exited_with, fake.stop)
            self.assertEqual(fake.calls, [('open', 3), ('fdopen', 17, 'rb'),
                                          ('enter',), ('fileno',), ('fstat', 17), ('exit',)])

    return unittest.defaultTestLoader.loadTestsFromTestCase(DescriptorOwnershipTests)


if __name__ == '__main__':
    source = Path(__file__).resolve().parents[1] / 'eimemory/adapters/hermes/native_memory.py'
    result = unittest.TextTestRunner(verbosity=2).run(build_suite(source.read_text()))
    raise SystemExit(0 if result.wasSuccessful() else 1)
