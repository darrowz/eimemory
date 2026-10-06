"""Pure AST-isolated URL-shape regression; no package or collector imports."""
import ast
from pathlib import Path
import unittest
from urllib.parse import urlparse


SOURCE = Path(__file__).resolve().parents[1] / "eimemory/governance/learning/evidence_collector.py"


def load_url_predicate(source=SOURCE):
    tree = ast.parse(source.read_text(encoding="utf-8"))
    selected = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "_is_http_url"]
    if len(selected) != 1:
        raise AssertionError("Expected exactly one URL predicate")
    namespace = {"urlparse": urlparse}
    exec(compile(ast.Module(body=selected, type_ignores=[]), str(source), "exec"), namespace)
    return namespace["_is_http_url"]


class EvidenceUrlParseTests(unittest.TestCase):
    def test_malformed_netloc_is_false(self):
        predicate = load_url_predicate()
        for value in ("https://[", "http://[not-ip]/", "http://]/", "https://example.com\uff0fpath"):
            with self.subTest(value=value):
                self.assertIs(predicate(value), False)

    def test_existing_shape_semantics_are_preserved(self):
        predicate = load_url_predicate()
        for value, expected in (
            ("https://example.com/a", True),
            (" HTTP://example.com ", True),
            ("https://[::1]/", True),
            ("ftp://example.com", False),
            ("https:///missing-host", False),
            ("/relative", False),
            ("", False),
        ):
            with self.subTest(value=value):
                self.assertIs(predicate(value), expected)


if __name__ == "__main__":
    unittest.main()
