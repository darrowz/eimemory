"""Offline stdlib-only regression: execute only the pinned final function AST."""
import ast
import math
from pathlib import Path
import unittest

SOURCE = Path(__file__).resolve().parents[1] / 'eimemory/events.py'
NEW = '''def _clamp_float(value: Any, *, default: float) -> float:
    try:
        number = float(value)
        if number != number:
            number = default
    except (TypeError, ValueError, OverflowError):
        number = default
    return round(max(0.0, min(1.0, number)), 3)
'''


def load_function():
    data = SOURCE.read_bytes()
    # Everything before the approved final function is opaque byte data.
    tail = b''.join(data.splitlines(keepends=True)[187:])
    if tail != NEW.encode():
        raise AssertionError('Expected exact approved function at line 188 through EOF')
    tree = ast.parse(tail.decode(), filename=str(SOURCE))
    if len(tree.body) != 1:
        raise AssertionError('Expected exactly one function')
    node = tree.body[0]
    if not isinstance(node, ast.FunctionDef) or node.name != '_clamp_float':
        raise AssertionError('Expected _clamp_float')
    if any(isinstance(n, (ast.Import, ast.ImportFrom)) for n in ast.walk(tree)):
        raise AssertionError('Imports are not allowed in the extracted function')
    namespace = {'Any': object}
    exec(compile(tree, '<extracted _clamp_float only>', 'exec'), namespace)
    return namespace['_clamp_float']


class NumericRegression(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fn = staticmethod(load_function())

    def test_finite_inputs(self):
        cases = [(-100, 0.0), (-0.0, 0.0), (0, 0.0), (0.123456, 0.123),
                 (0.9996, 1.0), (1, 1.0), (100, 1.0), ('0.625', 0.625),
                 (True, 1.0), (False, 0.0), (b'0.25', 0.25)]
        for value, expected in cases:
            with self.subTest(value=value):
                actual = self.fn(value, default=0.37)
                self.assertEqual(actual, expected)
                self.assertIs(type(actual), float)

    def test_infinity_inputs(self):
        for value, expected in [(math.inf, 1.0), (-math.inf, 0.0),
                                ('Infinity', 1.0), ('-Infinity', 0.0)]:
            with self.subTest(value=value):
                self.assertEqual(self.fn(value, default=0.37), expected)

    def test_nan_falls_back(self):
        for value in [math.nan, float('-nan'), 'nan', 'NaN', b'nan']:
            for default, expected in [(0.37, 0.37), (-2.0, 0.0), (2.0, 1.0),
                                      (0.123456, 0.123), (math.inf, 1.0),
                                      (-math.inf, 0.0), (math.nan, 1.0)]:
                with self.subTest(value=value, default=default):
                    self.assertEqual(self.fn(value, default=default), expected)

    def test_overflow_falls_back(self):
        for value in [10 ** 400, -(10 ** 400)]:
            for default, expected in [(0.37, 0.37), (-2.0, 0.0), (2.0, 1.0),
                                      (0.123456, 0.123), (math.inf, 1.0),
                                      (-math.inf, 0.0), (math.nan, 1.0)]:
                with self.subTest(sign=1 if value > 0 else -1, default=default):
                    self.assertEqual(self.fn(value, default=default), expected)

    def test_existing_conversion_failure_defaults(self):
        for value in [None, [], {}, 'not-a-number', '']:
            for default, expected in [(0.37, 0.37), (-2.0, 0.0), (2.0, 1.0),
                                      (0.123456, 0.123), (math.inf, 1.0),
                                      (-math.inf, 0.0), (math.nan, 1.0),
                                      (10 ** 400, 1.0), (-(10 ** 400), 0.0)]:
                with self.subTest(value=value, default=default):
                    self.assertEqual(self.fn(value, default=default), expected)

    def test_invalid_default_contract_unchanged(self):
        for default in [None, '0.3', [], object()]:
            with self.subTest(default_type=type(default).__name__):
                with self.assertRaises(TypeError):
                    self.fn(None, default=default)
                self.assertEqual(self.fn(0.25, default=default), 0.25)

    def test_success_does_not_touch_default(self):
        for default in [math.nan, math.inf, -math.inf, 10 ** 400]:
            self.assertEqual(self.fn(0.25, default=default), 0.25)


if __name__ == '__main__':
    unittest.main()
