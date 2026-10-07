"""Stdlib-only regression tests; run from the repository root.

python -m unittest discover -s path/to/tests -p test_persona_jsonl_regression.py
An optional EIMEMORY_PERSONA_SOURCE selects another source file for comparison.
Only the two target function AST nodes are executed; project imports never run.
"""
import ast
import json
import os
from pathlib import Path
import tempfile
import unittest
from typing import Any


def source_path():
    override = os.environ.get('EIMEMORY_PERSONA_SOURCE')
    if override:
        return Path(override)
    relative = Path('eimemory/persona/evals/run_persona_eval.py')
    for root in (Path.cwd(), *Path(__file__).resolve().parents):
        candidate = root / relative
        if candidate.is_file():
            return candidate
    raise FileNotFoundError('Run from the repository root or set EIMEMORY_PERSONA_SOURCE')


def isolated_functions(path):
    tree = ast.parse(path.read_text(encoding='utf-8'), filename=str(path))
    names = {'_load_cases', '_validate_case'}
    nodes = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in names]
    if {node.name for node in nodes} != names or len(nodes) != 2:
        raise ValueError('Expected exactly the two target functions')
    namespace = {'Path': Path, 'Any': Any, 'json': json}
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(path), 'exec'), namespace)
    return namespace['_load_cases'], namespace['_validate_case']


class PersonaJsonlRegression(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        load, validate = isolated_functions(source_path())
        cls.load = staticmethod(load)
        cls.validate = staticmethod(validate)

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / 'fixed.jsonl'

    def write_load(self, payload):
        self.path.write_bytes(payload)
        return self.load(self.path)

    def test_literal_unicode_separators_are_data(self):
        for character in ('\u0085', '\u2028', '\u2029'):
            with self.subTest(codepoint=ord(character)):
                row = json.dumps({'x': character}, ensure_ascii=False)
                expected = json.loads(row)
                self.assertEqual(self.write_load((row + '\n').encode('utf-8')), [expected])

    def test_regular_newlines_and_trailing_newline(self):
        for separator in ('\n', '\r\n', '\r'):
            for trailing in ('', separator):
                with self.subTest(separator=separator, trailing=trailing):
                    text = '{}' + separator + '{"forbidden":[]}' + trailing
                    self.assertEqual(self.write_load(text.encode()), [{}, {'forbidden': []}])

    def test_empty_and_blank_files(self):
        for payload in (b'', b'\n', b' \t\n\n', b'\r\n\t\r\n'):
            with self.subTest(payload=payload):
                self.assertEqual(self.write_load(payload), [])

    def test_blank_lines_preserve_object_error_line(self):
        with self.assertRaisesRegex(ValueError, '^Case at line 3 must be a JSON object$'):
            self.write_load(b'{}\n \t\n[]\n')

    def test_non_object_json_values(self):
        for value in (None, False, 0, 'x', []):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, '^Case at line 1 must be a JSON object$'):
                    self.write_load(json.dumps(value).encode())

    def test_string_lists_null_and_absent(self):
        for field in ('expected_contains', 'forbidden'):
            for value in (None, [], ['x', '']):
                with self.subTest(field=field, value=value):
                    row = {field: value}
                    self.assertIs(self.validate(row, line_number=2), row)
        row = {}
        self.assertIs(self.validate(row, line_number=2), row)

    def test_bad_list_shapes(self):
        for field in ('expected_contains', 'forbidden'):
            for value in ('x', {}, [0], [None], [[]]):
                with self.subTest(field=field, value=value):
                    with self.assertRaisesRegex(ValueError, f'^Case at line 2: {field} must be a list of strings or null$'):
                        self.validate({field: value}, line_number=2)

    def test_invalid_json_still_raises_original_type(self):
        with self.assertRaises(json.JSONDecodeError) as caught:
            self.write_load(b'{}\n{\n')
        self.assertEqual(caught.exception.lineno, 1)
        self.assertEqual(caught.exception.colno, 2)

    def test_invalid_utf8_still_propagates(self):
        with self.assertRaises(UnicodeDecodeError):
            self.write_load(b'\xff')


if __name__ == '__main__':
    unittest.main()
