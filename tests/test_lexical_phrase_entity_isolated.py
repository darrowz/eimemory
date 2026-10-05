"""EA-082 pure lexical regressions; stdlib AST loading, no project imports."""
import ast
from bisect import bisect_right
from pathlib import Path
import re
from types import SimpleNamespace
import unittest


SOURCE = Path(__file__).resolve().parents[1] / 'eimemory/recall/lexical.py'
FUNCTIONS = {
    'analyze_lexical_signal', '_synonym_neighbors', '_content_query_terms',
    '_clean_text', '_extract_terms', '_matching_record_terms',
    '_split_chinese_compound', '_extract_phrase_terms', '_phrase_matches_record',
    '_term_matches_record', '_dedupe', '_is_entity_term', '_is_chinese',
    '_expand_chinese_context', '_compute_score',
}
CONSTANTS = {
    '_CHINESE_RE', '_TOKEN_RE', '_VERSION_RE', '_DOTTED_VERSION_RE',
    '_PHRASE_RE', '_FILLER_TERMS', '_SYNONYM_GROUPS', '_CLEAN_TEXT_RE',
}


def load_pure_lexical():
    tree = ast.parse(SOURCE.read_text(encoding='utf-8'))
    nodes = [node for node in tree.body if (
        isinstance(node, ast.FunctionDef) and node.name in FUNCTIONS
    ) or (
        isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id in CONSTANTS
            for target in node.targets
        )
    )]
    namespace = {
        're': re, 'bisect_right': bisect_right, 'LexicalSignal': SimpleNamespace,
        '_max_adjustment': lambda: 0.18,
        '_build_kind_suppression_reason': lambda **kwargs: '',
        '_empty_signal': lambda *args: SimpleNamespace(
            score=0.0, exact_phrase_hits=(), entity_hits=(), version_hits=(),
            token_hits=(), suppression_reason='',
        ),
    }
    exec(compile(ast.Module(body=nodes, type_ignores=[]), '<pure-lexical>', 'exec'), namespace)
    return namespace


class LexicalPhraseEntityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ns = load_pure_lexical()

    def analyze(self, query, record):
        return self.ns['analyze_lexical_signal'](query, record)

    def test_quoted_phrase_recognizes_order(self):
        for quote in ('"', "'"):
            with self.subTest(quote=quote):
                query = f'{quote}red blue{quote}'
                forward = self.analyze(query, 'red blue')
                reverse = self.analyze(query, 'blue red')
                self.assertEqual(forward.exact_phrase_hits, ('red', 'blue', 'red blue'))
                self.assertEqual(reverse.exact_phrase_hits, ('red', 'blue'))
                self.assertEqual(forward.token_hits, reverse.token_hits)
                # The pre-existing phrase-rate cap intentionally preserves this tie.
                self.assertEqual(forward.score, reverse.score)

    def test_partial_query_exposes_phrase_score(self):
        forward = self.analyze('"red blue" green', 'red blue')
        reverse = self.analyze('"red blue" green', 'blue red')
        self.assertEqual(forward.score, 0.111)
        self.assertEqual(reverse.score, 0.096)

    def test_phrase_normalization(self):
        result = self.analyze('"RED,   Blue"', 'prefix red\n BLUE suffix')
        self.assertIn('red blue', result.exact_phrase_hits)

    def test_phrase_word_boundaries(self):
        for record in ('infrared blue', 'red blueberry', 'blue red', 'red green blue'):
            with self.subTest(record=record):
                self.assertNotIn('red blue', self.analyze('"red blue"', record).exact_phrase_hits)

    def test_no_phrase_for_unquoted_query(self):
        self.assertEqual(self.analyze('red blue', 'red blue').exact_phrase_hits, ('red', 'blue'))

    def test_phrase_deduplication(self):
        hits = self.analyze('"red blue" "red blue"', 'red blue').exact_phrase_hits
        self.assertEqual(hits, ('red', 'blue', 'red blue'))

    def test_uppercase_entity_case_insensitive_record(self):
        for record in ('NASA', 'nasa', 'Nasa'):
            with self.subTest(record=record):
                result = self.analyze('NASA', record)
                self.assertEqual(result.entity_hits, ('nasa',))
                self.assertEqual(result.token_hits, ('nasa',))
                self.assertEqual(result.score, 0.162)

    def test_lowercase_and_titlecase_are_not_acronyms(self):
        for query in ('nasa', 'Nasa', 'ordinary'):
            with self.subTest(query=query):
                result = self.analyze(query, query.upper())
                self.assertEqual(result.entity_hits, ())
                self.assertEqual(result.score, 0.144)

    def test_acronym_token_edges_and_mixed_case(self):
        self.assertEqual(self.analyze('NASA', 'nasal').entity_hits, ())
        self.assertEqual(self.analyze('NASA nasa', 'nasa').entity_hits, ('nasa',))
        self.assertEqual(self.analyze('NASA/API', 'nasa api').entity_hits, ('nasa', 'api'))

    def test_entity_predicate(self):
        predicate = self.ns['_is_entity_term']
        for term in ('NASA', 'API', 'abc12', '任务'):
            self.assertTrue(predicate(term), term)
        for term in ('nasa', 'Nasa', 'api', 'A', ''):
            self.assertFalse(predicate(term), term)

    def test_acronym_survives_content_rescoring(self):
        result = self.analyze('应该 NASA', 'nasa')
        self.assertEqual(result.entity_hits, ('nasa',))
        self.assertEqual(result.score, 0.162)

    def test_existing_ordinary_scores(self):
        for query, record, score in (
            ('alpha', 'alpha', 0.144), ('alpha beta', 'alpha', 0.072),
            ('alpha', 'bravo', 0.0), ('abc12', 'abc12', 0.162),
            ('v1.2', 'v1.2', 0.18), ('v1.2', 'v1.2beta', 0.0),
            ('任务', '任务', 0.162), ('link', 'url', 0.144),
        ):
            with self.subTest(query=query, record=record):
                self.assertEqual(self.analyze(query, record).score, score)

    def test_quoted_version_cannot_match_longer_version(self):
        for query, record, phrase in (
            ('"v1.2"', 'v1.2.3', 'v1.2'),
            ('"release v1.2"', 'release v1.2.3', 'release v1.2'),
            ('"release v1.2"', 'release v1.2beta', 'release v1.2'),
        ):
            with self.subTest(query=query, record=record):
                self.assertNotIn(phrase, self.analyze(query, record).exact_phrase_hits)

    def test_version_boundaries_unchanged(self):
        for query, record, hits in (
            ('v1.2', 'v1.2.3', ()), ('v1.2', 'v1.2beta', ()),
            ('v1.2', '发布v1.2版本', ('v1.2',)),
            ('v1.2', 'version v1.2.', ('v1.2',)),
        ):
            with self.subTest(query=query, record=record):
                self.assertEqual(self.analyze(query, record).version_hits, hits)


if __name__ == '__main__':
    unittest.main()
