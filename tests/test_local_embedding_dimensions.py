"""Standard-library-only dimension regressions; never import the project."""

import ast
from functools import lru_cache
import hashlib
import math
from pathlib import Path
import re
import unittest


def _load_reviewed_embedding_functions():
    path = Path(__file__).resolve().parents[1] / "eimemory/embeddings/local.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    names = {"embed_text", "_embed_text_cached", "_embed_text_uncached", "_tokens"}
    functions = [
        node for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name in names
    ]
    namespace = {
        "hashlib": hashlib,
        "lru_cache": lru_cache,
        "math": math,
        "re": re,
        "TOKEN_RE": re.compile(r"[a-z0-9]{2,}", re.IGNORECASE),
        "VECTOR_SIZE": 128,
        "MAX_EMBED_CHARS": 4096,
        "MAX_CACHED_TEXT_CHARS": 4096,
    }
    exec(compile(ast.Module(body=functions, type_ignores=[]), str(path), "exec"), namespace)
    return namespace["embed_text"]


class LocalEmbeddingDimensionsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.embed = staticmethod(_load_reviewed_embedding_functions())

    def test_invalid_dimensions_are_rejected_for_every_text_shape(self):
        for text in ("", "a", "hello", "hello " * 1000):
            for size in (-1, -128, 0.5, "0", "-1"):
                with self.subTest(text_length=len(text), size=size):
                    with self.assertRaisesRegex(ValueError, "size must be positive"):
                        self.embed(text, size=size)

    def test_default_dimension_compatibility(self):
        for size in (0, None, False):
            with self.subTest(size=size):
                self.assertEqual(len(self.embed("hello", size=size)), 128)

    def test_existing_integer_coercion_is_preserved(self):
        for size in ("16", 16.9):
            with self.subTest(size=size):
                self.assertEqual(len(self.embed("hello", size=size)), 16)

    def test_valid_dimensions_and_empty_input(self):
        for size in (1, 16, 128):
            with self.subTest(size=size):
                self.assertEqual(self.embed("", size=size), [0.0] * size)
                vector = self.embed("deterministic hello", size=size)
                self.assertEqual(len(vector), size)
                self.assertTrue(all(isinstance(value, float) for value in vector))
                self.assertAlmostEqual(sum(value * value for value in vector), 1.0)
                self.assertEqual(vector, self.embed("deterministic hello", size=size))

    def test_results_do_not_share_mutable_storage(self):
        vector = self.embed("repeatable", size=16)
        expected = vector.copy()
        vector[0] = 999.0
        self.assertEqual(self.embed("repeatable", size=16), expected)


if __name__ == "__main__":
    unittest.main()
