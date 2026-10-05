"""EA-103 bounded text regressions; stdlib AST only, no engine/scoring execution."""
import ast
from bisect import bisect_right
from pathlib import Path
import re
from types import SimpleNamespace
import unittest


ROOT = Path(__file__).resolve().parents[1]
ENGINE = ROOT / 'eimemory/retrieval/engine.py'
LEXICAL = ROOT / 'eimemory/recall/lexical.py'
HIT_FIELDS = ('token_hits', 'exact_phrase_hits', 'entity_hits', 'version_hits')
LEXICAL_HELPERS = {
    '_synonym_neighbors', '_clean_text', '_extract_terms', '_matching_record_terms',
    '_split_chinese_compound', '_extract_phrase_terms', '_phrase_matches_record',
    '_term_matches_record', '_dedupe', '_is_entity_term', '_is_chinese',
    '_expand_chinese_context',
}
LEXICAL_CONSTANTS = {
    '_CHINESE_RE', '_TOKEN_RE', '_VERSION_RE', '_DOTTED_VERSION_RE',
    '_PHRASE_RE', '_SYNONYM_GROUPS', '_CLEAN_TEXT_RE',
}


def load_projection(source=ENGINE):
    """Extract only the field projection expression and its pure string helper."""
    tree = ast.parse(source.read_text(encoding='utf-8'))
    engine = next(node for node in tree.body if isinstance(node, ast.ClassDef)
                  and node.name == 'GovernedRecallEngine')
    method = next(node for node in engine.body if isinstance(node, ast.FunctionDef)
                  and node.name == '_keyword_exact_match')
    assignment = next(node for node in method.body if isinstance(node, ast.Assign)
                      and any(isinstance(target, ast.Name) and target.id == 'bounded_text'
                              for target in node.targets))
    projection = ast.parse('def project(record):\n    return None\n').body[0]
    projection.body[0].value = assignment.value
    helpers = [node for node in tree.body if isinstance(node, ast.FunctionDef)
               and node.name == '_bounded_keyword_field_text']
    lexical_tree = ast.parse(LEXICAL.read_text(encoding='utf-8'))
    dotted = next(node for node in lexical_tree.body if isinstance(node, ast.Assign)
                  and any(isinstance(target, ast.Name) and target.id == '_DOTTED_VERSION_RE'
                          for target in node.targets))
    namespace = {'re': re}
    module = ast.fix_missing_locations(ast.Module(body=[dotted] + helpers + [projection], type_ignores=[]))
    exec(compile(module, '<isolated-keyword-text-projection>', 'exec'), namespace)
    return namespace['project']


def load_lexical_hits(source=LEXICAL):
    """Stop the inspected lexical function before its first scoring statement."""
    tree = ast.parse(source.read_text(encoding='utf-8'))
    analyzer = next(node for node in tree.body if isinstance(node, ast.FunctionDef)
                    and node.name == 'analyze_lexical_signal')
    stop = next(index for index, node in enumerate(analyzer.body)
                if isinstance(node, ast.Assign)
                and any(isinstance(target, ast.Name) and target.id == 'score'
                        for target in node.targets))
    score = analyzer.body[stop]
    assert isinstance(score.value, ast.Call) and isinstance(score.value.func, ast.Name)
    assert score.value.func.id == '_compute_score'
    result = ast.parse(
        'def hits():\n'
        '    return {"token_hits": tuple(token_hits), '
        '"exact_phrase_hits": tuple(exact_phrase_hits), '
        '"entity_hits": tuple(entity_hits), "version_hits": tuple(version_hits)}\n'
    ).body[0].body[0]
    analyzer.body = analyzer.body[:stop] + [result]
    analyzer.returns = None
    nodes = [node for node in tree.body if (
        isinstance(node, ast.FunctionDef) and node.name in LEXICAL_HELPERS
    ) or (
        isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id in LEXICAL_CONSTANTS
            for target in node.targets
        )
    )]
    namespace = {
        're': re, 'bisect_right': bisect_right,
        '_empty_signal': lambda *args: {field: () for field in HIT_FIELDS},
    }
    module = ast.fix_missing_locations(ast.Module(body=nodes + [analyzer], type_ignores=[]))
    exec(compile(module, '<isolated-lexical-hits-no-scoring>', 'exec'), namespace)
    return namespace['analyze_lexical_signal']


def record(**overrides):
    values = dict(title='', summary='', detail='', content={})
    values.update(overrides)
    return SimpleNamespace(**values)


class KeywordTextBoundaryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.project = staticmethod(load_projection())
        cls.hits = staticmethod(load_lexical_hits())

    def test_cut_identifiers_do_not_become_shorter_hits_in_any_field(self):
        for prefix, suffix in (
            ('v2', 'beta'), ('abc12', 'def'), ('v1.2', 'beta'),
            ('v2', '_foo'), ('12', 'abc'), ('NASA', 'LOGY'),
            ('v1.2', '.3'), ('v1.2', '-other'),
            ('roll', 'back'), ('alpha', 'beta'), ('v2', '٢'), ('v2', '２'),
            ('v2', 'K'), ('v2', 'İ'), ('v1.2', '.alpha'), ('v1.2', '._a'),
        ):
            text = 'x' * (2047 - len(prefix)) + ' ' + prefix + suffix
            for field in ('title', 'summary', 'detail', 'text', 'excerpt'):
                item = record(**{field: text}) if field in ('title', 'summary', 'detail') else (
                    record(content={field: text})
                )
                with self.subTest(prefix=prefix, suffix=suffix, field=field):
                    self.assertTrue(all(not values for values in self.hits(prefix, text).values()))
                    self.assertTrue(all(not values for values in self.hits(prefix, self.project(item)).values()))

    def test_all_internal_identifier_cuts_discard_partial_evidence(self):
        for token in ('v2beta', 'abc12def34', 'v1.2beta', 'v1.2.3', 'v2_foo', '123abc'):
            for offset in range(1, len(token)):
                prefix = 'x' * (2047 - offset) + ' '
                with self.subTest(token=token, offset=offset):
                    self.assertEqual(self.project(record(detail=prefix + token)), '  ' + prefix + '  ')

    def test_complete_identifiers_at_boundary_keep_hits(self):
        for token in ('v2', 'abc12', 'v1.2', 'v2beta', 'abc12def', 'v1.2beta', 'NASA'):
            prefix = 'x' * (2047 - len(token)) + ' '
            for tail in ('', ' trailing', '\ttrailing', '\ntrailing', '\u3000trailing'):
                with self.subTest(token=token, tail=tail):
                    text = prefix + token + tail
                    projected = self.project(record(detail=text))
                    self.assertEqual(projected, '  ' + text[:2048] + '  ')
                    self.assertIn(token.lower(), self.hits(token, projected)['token_hits'])

    def test_retained_identifiers_keep_suffix_and_version_contract(self):
        for short, whole in (('v2', 'v2beta'), ('abc12', 'abc12def'), ('v1.2', 'v1.2beta')):
            with self.subTest(whole=whole):
                text = self.project(record(detail=whole))
                self.assertTrue(all(not values for values in self.hits(short, text).values()))
                hits = self.hits(whole.upper(), text)
                self.assertEqual(hits['token_hits'], (whole,))
                self.assertEqual(hits['entity_hits'], (whole,))
                self.assertEqual(hits['version_hits'], ())

    def test_no_truncation_preserves_field_order_selection_and_coercion(self):
        item = record(title='TITLE', summary=None, detail=42,
                      content={'text': 'TEXT', 'excerpt': 'EXCERPT', 'ignored': 'NO'},
                      other='NO')
        self.assertEqual(self.project(item), 'TITLE  42 TEXT EXCERPT')
        self.assertEqual(self.project(record(title=0, summary=False, content=['NO'])), '    ')
        text = 'A' * 2048
        self.assertEqual(self.project(record(detail=text)), '  ' + text + '  ')

    def test_previous_complete_segment_survives_and_cap_never_expands(self):
        text = 'v2 ' + 'x' * 2046
        projected = self.project(record(detail=text))
        self.assertEqual(projected, '  v2   ')
        self.assertEqual(self.hits('v2', projected)['version_hits'], ('v2',))
        fields = ['a ' * 1100, 'b' * 3000, 'c' * 2048, 'd ' * 1100, 'e' * 3000]
        item = record(title=fields[0], summary=fields[1], detail=fields[2],
                      content={'text': fields[3], 'excerpt': fields[4]})
        self.assertLessEqual(len(self.project(item)), 5 * 2048 + 4)

    def test_whitespace_boundaries_and_unbroken_ascii_segment(self):
        for delimiter in (' ', '\t', '\n', '\r', '\u3000'):
            prefix = 'keep' + delimiter
            text = prefix + 'v' * (2048 - len(prefix)) + 'suffix'
            with self.subTest(delimiter=delimiter):
                self.assertEqual(self.project(record(detail=text)), '  ' + prefix + '  ')
        for text in ('x' * 2049, '1' * 2049):
            with self.subTest(text_start=text[:5]):
                self.assertEqual(self.project(record(detail=text)), '    ')

    def test_cjk_and_punctuation_delimited_content_remains(self):
        for text in ('甲' * 2049, 'x.' * 1025, '中文，保留。' * 400):
            with self.subTest(text_start=text[:5]):
                self.assertEqual(self.project(record(detail=text)), '  ' + text[:2048] + '  ')
        for separator in ('中文', '/', '.', '-', ',', '。'):
            prefix = 'keep' + separator
            token = 'abc12def'
            leading = 'x' * (2048 - len(prefix) - 5) + ' '
            # The boundary lands four characters into the last identifier.
            text = leading + prefix + token
            with self.subTest(separator=separator):
                self.assertEqual(self.project(record(detail=text)), '  ' + leading + prefix + '  ')
        for next_text in ('中文后缀', '. trailing', '..3'):
            prefix = '甲' * (2048 - len('v1.2')) + 'v1.2'
            with self.subTest(next_text=next_text):
                self.assertEqual(self.project(record(detail=prefix + next_text)), '  ' + prefix + '  ')

    def test_dotted_version_exposure_and_lowercase_expansion(self):
        for retained, tail, query in (
            ('v1.', '2', 'v1'), ('v1.2.', 'alpha', 'v1.2'),
            ('v1.2.al', 'pha', 'v1.2'), ('İv1.2', '.3', 'v1.2'),
            ('İv1.2b', 'eta', 'v1.2b'),
        ):
            prefix = '甲' * (2048 - len(retained))
            with self.subTest(retained=retained, tail=tail):
                text = prefix + retained + tail
                self.assertTrue(all(not values for values in self.hits(query, text).values()))
                self.assertTrue(all(not values for values in self.hits(query, self.project(record(detail=text))).values()))

    def test_cut_phrase_does_not_gain_a_short_final_word(self):
        text = 'x' * (2048 - len(' red blue')) + ' red blueberry'
        projected = self.project(record(detail=text))
        self.assertNotIn('red blue', self.hits('"red blue"', projected)['exact_phrase_hits'])
        self.assertEqual(self.hits('red', projected)['token_hits'], ('red',))

    def test_final_ambiguous_segment_is_conservatively_discarded(self):
        # Fixed lookahead and conservative decimal handling can lose a valid
        # boundary hit; earlier complete text must remain available.
        for token, tail in (('v1', '.2.beta'), ('abc_12', '٢'), ('abc_12', '２')):
            prefix = '甲' * (2048 - len('/alpha/') - len(token)) + '/alpha/'
            text = prefix + token + tail
            with self.subTest(token=token, tail=tail):
                self.assertIn(token, self.hits(token, text)['token_hits'])
                projected = self.project(record(detail=text))
                self.assertEqual(projected, '  ' + prefix + '  ')
                self.assertNotIn(token, self.hits(token, projected)['token_hits'])
                self.assertEqual(self.hits('alpha', projected)['token_hits'], ('alpha',))


if __name__ == '__main__':
    unittest.main()
