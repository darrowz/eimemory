"""EA-084 identifier suffix regressions using only the isolated stdlib harness."""
import unittest

# Direct execution avoids package initialization and project conftest/runtime.
from test_lexical_phrase_entity_isolated import load_pure_lexical


class LexicalIdentifierSuffixTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ns = load_pure_lexical()

    def analyze(self, query, record):
        return self.ns['analyze_lexical_signal'](query, record)

    def test_short_identifiers_do_not_match_suffixed_records(self):
        for query, record in (
            ('v2', 'v2beta'), ('v2', 'v2_foo'), ('v2', 'v2_'),
            ('abc12', 'abc12def'), ('abc12', 'abc12def34'),
            ('abc12', 'abc12_foo'), ('12', '12abc'),
            ('12', '12_foo'), ('NASA12', 'nasa12beta'),
        ):
            with self.subTest(query=query, record=record):
                result = self.analyze(query, record)
                self.assertEqual(result.score, 0.0)
                for field in ('token_hits', 'exact_phrase_hits', 'entity_hits', 'version_hits'):
                    self.assertEqual(getattr(result, field), ())

    def test_suffixed_queries_do_not_match_short_records(self):
        for query, record in (
            ('v2beta', 'v2'), ('v2_foo', 'v2'), ('abc12def', 'abc12'),
            ('abc12def34', 'def34'), ('12abc', '12'), ('v2_', 'v2'),
        ):
            with self.subTest(query=query, record=record):
                self.assertEqual(self.analyze(query, record).score, 0.0)

    def test_suffixes_are_not_separate_evidence(self):
        for query, record in (
            ('beta', 'v2beta'), ('foo', 'v2_foo'), ('def', 'abc12def'),
            ('def34', 'abc12def34'), ('abc', '123abc'),
        ):
            with self.subTest(query=query, record=record):
                self.assertEqual(self.analyze(query, record).score, 0.0)

    def test_full_identifier_matches_case_insensitively(self):
        for token in ('v2beta', 'v2_foo', 'v2_', 'abc12def', 'abc12def34', '12abc', '12_foo'):
            with self.subTest(token=token):
                result = self.analyze(token.upper(), token)
                self.assertEqual(result.token_hits, (token,))
                self.assertEqual(result.exact_phrase_hits, (token,))
                self.assertEqual(result.entity_hits, (token,))
                self.assertEqual(result.version_hits, ())
                self.assertEqual(result.score, 0.162)
                self.assertEqual(self.ns['_extract_terms'](token), [token])

    def test_unsuffixed_version_and_id_scores_unchanged(self):
        for token, score, version in (
            ('v2', 0.18, ('v2',)), ('v20', 0.18, ('v20',)),
            ('abc12', 0.162, ()), ('123', 0.162, ()),
        ):
            with self.subTest(token=token):
                result = self.analyze(token, token.upper())
                self.assertEqual(result.score, score)
                self.assertEqual(result.version_hits, version)

    def test_matching_membership_equivalence_in_both_paths(self):
        text = self.ns['_clean_text']('发布v2beta版本 abc12def34 123abc v2_foo v1.2beta v1.2')
        wanted = {'v2', 'beta', 'foo', 'abc12', 'def34', '123', 'abc', 'v2beta',
                  'abc12def34', '123abc', 'v2_foo', 'v1.2beta', 'v1.2', '发布', '版本'}
        for requested in (wanted, wanted | {f'absent{i}' for i in range(70)}):
            with self.subTest(count=len(requested)):
                expected = set(self.ns['_extract_terms'](text)) & requested
                self.assertEqual(self.ns['_matching_record_terms'](text, requested), expected)
                self.assertFalse(expected & {'v2', 'beta', 'foo', 'abc12', 'def34', '123', 'abc'})

    def test_existing_focused_dotted_version_cases(self):
        for text in ('v1.14.42', '发布v1.14.42版本', '(v1.14.42)。', '版本：V1.14.42，'):
            with self.subTest(text=text):
                self.assertIn('v1.14.42', self.ns['_clean_text'](text))
                self.assertEqual(self.analyze('v1.14.42', text).version_hits, ('v1.14.42',))
        for text in ('v1.14.43', 'v1.14.420', 'v1.14.42beta', 'MIPROv1.14.42', 'v1.14.42-other'):
            with self.subTest(text=text):
                self.assertEqual(self.analyze('v1.14.42', text).version_hits, ())
        self.assertEqual(self.ns['_clean_text']('Some.Path / foo-bar MIPROv2'), 'some path foo bar miprov2')

    def test_cjk_synonyms_and_punctuation_preserved(self):
        for query, record, score in (
            ('v2', '发布v2版本', 0.18), ('abc12', '(ABC12)。', 0.162),
            ('任务', '任务', 0.162), ('link', 'url', 0.144),
            ('链接', '短链', 0.162),
        ):
            with self.subTest(query=query, record=record):
                self.assertEqual(self.analyze(query, record).score, score)
        self.assertEqual(self.analyze('v2', '发布v2beta版本').version_hits, ())


if __name__ == '__main__':
    unittest.main()
