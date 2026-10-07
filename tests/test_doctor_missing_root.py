"""Truncated-prefix model: never execute doctor imports or downstream disk checks.

Place this file in repository tests/. Direct execution, unittest import and discovery
all work. The source is parsed as AST; only check_storage_disk's authorized opening
statements through its first metrics assignment are compiled, with fake dependencies.
This is deliberately not an integration test of the full function or filesystem.
"""
import ast
from pathlib import Path
from types import SimpleNamespace as NS
import unittest

SOURCE = Path(__file__).parent.parent / 'eimemory' / 'cli' / 'doctor.py'


class FakePath:
    calls = []

    def __init__(self, value):
        self.calls.append(('construct', type(value).__name__))
        if isinstance(value, FakePath):
            value = value.value
        elif not isinstance(value, str) and hasattr(type(value), '__fspath__'):
            value = type(value).__fspath__(value)
        if not isinstance(value, str):
            raise TypeError('controlled unsupported path type')
        self.value = value

    def resolve(self):
        self.calls.append(('resolve', str.__str__(self.value)))
        if str.__str__(self.value) == 'resolve-error':
            raise OSError('controlled resolve failure')
        return self

    def __str__(self):
        return '<FAKE_CWD>' if str.__len__(self.value) == 0 else str.__str__(self.value)


class EqualityTrapPath:
    def __fspath__(self):
        return 'fake-pathlike'

    def __eq__(self, other):
        raise AssertionError('pathlike equality must not be called')

    def __bool__(self):
        raise AssertionError('pathlike truthiness must not be called')


class EqualityTruePath(EqualityTrapPath):
    def __eq__(self, other):
        return True


class StringTrap(str):
    def __eq__(self, other):
        raise AssertionError('string subclass equality must not be called')

    def __bool__(self):
        raise AssertionError('string subclass truthiness must not be called')

    def __len__(self):
        raise AssertionError('string subclass overridden length must not be called')


def extract_prefix(source):
    module = ast.parse(source)
    matches = [node for node in module.body
               if isinstance(node, ast.FunctionDef) and node.name == 'check_storage_disk']
    if len(matches) != 1:
        raise AssertionError('expected exactly one check_storage_disk function')
    function = matches[0]
    prefix = []
    for node in function.body:
        prefix.append(node)
        if (isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name)
                and node.target.id == 'metrics'):
            break
    else:
        raise AssertionError('opening metrics assignment missing')
    # Allow exactly the known opening shapes; added intervening work fails closed.
    shapes = [type(node).__name__ for node in prefix]
    if shapes not in (['Expr', 'Assign', 'If', 'AnnAssign'],
                      ['Expr', 'Assign', 'If', 'Assign', 'AnnAssign']):
        raise AssertionError('unexpected prefix shape: ' + repr(shapes))
    first, last = function.lineno, prefix[-1].end_lineno
    if last - first + 1 != 7:
        raise AssertionError('authorized prefix must remain exactly seven lines')
    function.body = prefix + ast.parse('return metrics').body
    function.decorator_list = []
    isolated = ast.fix_missing_locations(ast.Module(body=[function], type_ignores=[]))
    scope = {'Any': object, 'CheckResult': lambda status, message: (status, message),
             'SKIP': 'SKIP', 'Path': FakePath}
    exec(compile(isolated, '<authorized-seven-line-prefix>', 'exec'), scope)
    return scope['check_storage_disk'], {'source_start': first, 'source_end': last,
                                       'source_lines': 7, 'model': 'truncated-prefix'}


class MissingRootPrefixTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.check, cls.mapping = extract_prefix(SOURCE.read_text(encoding='utf-8'))
        cls.check = staticmethod(cls.check)

    def setUp(self):
        FakePath.calls = []

    def test_missing_values_skip_without_path_calls(self):
        for runtime in (None, NS(), NS(store=None), NS(store=NS()),
                        NS(store=NS(root='')), NS(store=NS(root=None)),
                        NS(store=NS(root=StringTrap('')))):
            with self.subTest(runtime=repr(runtime)):
                FakePath.calls = []
                self.assertEqual(self.check(runtime), ('SKIP', 'could not resolve EIMEMORY_ROOT'))
                self.assertEqual(FakePath.calls, [])

    def test_valid_inputs_preserve_path_conversion(self):
        for value in ('/fake-absolute', 'fake-relative', '.', FakePath('fake-existing'),
                      EqualityTrapPath(), EqualityTruePath(), StringTrap('fake-string')):
            with self.subTest(value_type=type(value).__name__):
                FakePath.calls = []
                result = self.check(NS(store=NS(root=value)))
                self.assertIsInstance(result, dict)
                self.assertIn('root', result)
                self.assertEqual([call[0] for call in FakePath.calls], ['construct', 'resolve'])

    def test_invalid_inputs_still_reach_path_conversion(self):
        for value in (0, False, b'', b'fake-bytes'):
            with self.subTest(value=repr(value)):
                FakePath.calls = []
                with self.assertRaises(TypeError):
                    self.check(NS(store=NS(root=value)))
                self.assertEqual([call[0] for call in FakePath.calls], ['construct'])

    def test_resolve_failure_is_not_swallowed(self):
        with self.assertRaises(OSError):
            self.check(NS(store=NS(root='resolve-error')))
        self.assertEqual([call[0] for call in FakePath.calls], ['construct', 'resolve'])

    def test_mapping_is_seven_line_truncated_model(self):
        self.assertEqual(self.mapping['source_lines'], 7)
        self.assertEqual(self.mapping['model'], 'truncated-prefix')


if __name__ == '__main__':
    unittest.main()
