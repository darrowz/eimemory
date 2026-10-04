"""EA-017: only already-reviewed pure URL formatter, isolated with stdlib AST."""
import ast
import unittest
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

TARGET = Path(__file__).resolve().parents[1] / 'eimemory/intake/papers/normalize.py'
source = TARGET.read_text()
tree = ast.parse(source)
functions = [node for node in tree.body
             if isinstance(node, ast.FunctionDef)
             and node.name == 'canonicalize_identifier_url']
assert len(functions) == 1
namespace = {'Any': Any, 'urlsplit': urlsplit, 'urlunsplit': urlunsplit}
exec(compile(ast.Module(body=functions, type_ignores=[]), str(TARGET), 'exec'), namespace)
canonicalize = namespace['canonicalize_identifier_url']
cases = [
    (None, ''), ('', ''), ('  ', ''),
    ('relative/path', 'relative/path'),
    ('HTTPS://Example.Invalid:443/a?q=x#frag', 'https://example.invalid/a?q=x'),
    ('http://example.invalid:80/paper', 'http://example.invalid/paper'),
    ('https://example.invalid:8443/paper', 'https://example.invalid:8443/paper'),
    ('https://example.invalid:0/paper', 'https://example.invalid:0/paper'),
    ('https://[2001:db8::1]/paper', 'https://[2001:db8::1]/paper'),
    ('https://[2001:db8::1]:8443/paper', 'https://[2001:db8::1]:8443/paper'),
    ('https://[2001:db8::1]:0/paper', 'https://[2001:db8::1]:0/paper'),
    ('https://[2001:DB8::1]:443/paper?q=x#frag', 'https://[2001:db8::1]/paper?q=x'),
    ('http://[2001:db8::1]:80/paper', 'http://[2001:db8::1]/paper'),
]
class URLSerializationTests(unittest.TestCase):
    def test_synthetic_url_serialization(self):
        for input_value, expected in cases:
            with self.subTest(input_value=input_value):
                actual = canonicalize(input_value)
                self.assertEqual(actual, expected)
                self.assertEqual(canonicalize(actual), actual)


if __name__ == '__main__':
    unittest.main()
