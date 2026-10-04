"""Pure numeric regression checks; never import or execute the lexical module."""
import ast
from pathlib import Path
import random
import re
import unittest


def _isolated_score(ceiling):
    path = Path(__file__).resolve().parents[1] / 'eimemory' / 'recall' / 'lexical.py'
    module = ast.parse(path.read_text(encoding='utf-8'))
    node = next(
        item for item in module.body
        if isinstance(item, ast.FunctionDef) and item.name == '_compute_score'
    )
    # Only the arithmetic helper executes. Both dependencies are synthetic.
    namespace = {
        '_VERSION_RE': re.compile(r'^v\d+(?:\.\d+)*$', re.IGNORECASE),
        '_max_adjustment': lambda: ceiling,
    }
    exec(compile(ast.Module(body=[node], type_ignores=[]), '<score-only>', 'exec'), namespace)
    return namespace['_compute_score']


def _perfect_hits():
    return {
        key: ('v1.2',)
        for key in ('query_terms', 'token_hits', 'exact_phrase_hits', 'entity_hits', 'version_hits')
    }


class ScoreCeilingTests(unittest.TestCase):
    def test_rounding_cannot_exceed_ceiling(self):
        for ceiling in (0.00006, 0.123456):
            with self.subTest(ceiling=ceiling):
                self.assertEqual(_isolated_score(ceiling)(**_perfect_hits()), ceiling)

    def test_default_ceiling_and_empty_query(self):
        score = _isolated_score(0.18)
        self.assertEqual(score(**_perfect_hits()), 0.18)
        empty = {key: () for key in _perfect_hits()}
        self.assertEqual(score(**empty), 0.0)

    def test_synthetic_positive_ceilings(self):
        rng = random.Random(31031)
        for _ in range(1000):
            ceiling = rng.uniform(0.000001, 1.0)
            result = _isolated_score(ceiling)(**_perfect_hits())
            self.assertGreaterEqual(result, 0.0)
            self.assertLessEqual(result, ceiling)


if __name__ == '__main__':
    unittest.main()
