"""Regression for normalized reserved parameter bindings; no target imports."""
import ast
from pathlib import Path
import unittest


class Unsupported(Exception):
    """Local substitute for the isolated helper's exception dependency."""


class ReservedParameterTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Read lazily from a normal repository layout, never during test import.
        target = Path(__file__).resolve().parents[1] / 'deploy/luna_bridge/make_luna_patch.py'
        source = target.read_text(encoding='utf-8')
        old_guard = (
            "    if any(isinstance(node, ast.Name) and node.id == '_luna_trace'\n"
            "           for node in ast.walk(before)):\n"
        )
        new_guard = (
            "    if any((isinstance(node, ast.Name) and node.id == '_luna_trace')\n"
            "           or (isinstance(node, ast.arg) and node.arg == '_luna_trace')\n"
            "           for node in ast.walk(before)):\n"
        )
        guard_counts = (source.count(old_guard), source.count(new_guard))
        if guard_counts not in ((1, 0), (0, 1)):
            raise AssertionError('Expected exactly one original or candidate guard')
        baseline = source.replace(new_guard, old_guard) if guard_counts == (0, 1) else source
        cls.helpers = []
        for text in (baseline, source):
            module = ast.parse(text, filename=str(target))
            names = ('_literal_values', '_assert_literal_fidelity')
            definitions = [node for node in module.body
                           if isinstance(node, ast.FunctionDef) and node.name in names]
            if tuple(node.name for node in definitions) != names:
                raise AssertionError('Expected exactly the two named top-level helpers')
            isolated = ast.Module(body=definitions, type_ignores=[])
            namespace = {'ast': ast, 'Unsupported': Unsupported}
            exec(compile(isolated, '<isolated-ordinary-ast-helpers>', 'exec'), namespace)
            cls.helpers.append(namespace['_assert_literal_fidelity'])

    @staticmethod
    def fixture(signature='value', body='return 1'):
        source = f'def sample({signature}):\n    {body}\n'
        before = ast.parse(source)
        after = ast.parse(source)
        markers = (
            "_luna_trace.session(active=__name__ == '__main__')",
            "_luna_trace.stage('bridge_import_ms')",
            "_luna_trace.stage('bridge_client_setup_ms')",
            "_luna_trace.stage('provider_response_ms')",
        )
        def context(text, statements):
            return ast.With(
                items=[ast.withitem(context_expr=ast.parse(text, mode='eval').body,
                                    optional_vars=None)],
                body=statements, type_comment=None,
            )
        after.body[0].body = [context(markers[2], after.body[0].body)]
        for marker in (markers[1], markers[3], markers[0]):
            after.body.append(context(marker, [ast.Pass()]))
        after = ast.parse(ast.unparse(ast.fix_missing_locations(after)))
        return source, before, after

    @staticmethod
    def outcome(helper, before, after):
        try:
            helper(before, after)
        except Unsupported as error:
            return str(error)
        return 'ACCEPT'

    def outcomes(self, before, after):
        return [self.outcome(helper, before, after) for helper in self.helpers]

    def test_all_five_reserved_parameter_categories(self):
        signatures = ('_ｌuna_trace', '_ｌuna_trace, /', '*, _ｌuna_trace',
                      '*_ｌuna_trace', '**_ｌuna_trace')
        for signature in signatures:
            with self.subTest(signature=signature):
                source, before, after = self.fixture(signature)
                self.assertNotIn('_luna_trace', source)
                self.assertNotIn('luna_observability', source)
                arguments = [node.arg for node in ast.walk(before)
                             if isinstance(node, ast.arg)]
                self.assertIn('_luna_trace', arguments)
                self.assertEqual(self.outcomes(before, after),
                                 ['ACCEPT', 'already_instrumented_or_reserved_name'])

    def test_nonconflicting_parameters_keep_acceptance(self):
        signatures = ('value', 'value, /', '*, value', '*values', '**options',
                      '_ｌuna_other', '_trace_extra')
        for signature in signatures:
            with self.subTest(signature=signature):
                source, before, after = self.fixture(signature)
                self.assertNotIn('_luna_trace', source)
                self.assertNotIn('luna_observability', source)
                self.assertEqual(self.outcomes(before, after), ['ACCEPT', 'ACCEPT'])

    def test_existing_name_rejection_is_preserved(self):
        _, before, after = self.fixture('_ｌuna_trace', 'return _ｌuna_trace')
        self.assertEqual(self.outcomes(before, after),
                         ['already_instrumented_or_reserved_name'] * 2)

    def test_ascii_parameter_matches_existing_raw_reservation(self):
        source, before, after = self.fixture('_luna_trace')
        self.assertIn('_luna_trace', source)
        # The unchanged raw predicate rejects this before either helper is reached.
        self.assertTrue('_luna_trace' in source or 'luna_observability' in source)
        self.assertEqual(self.outcomes(before, after),
                         ['ACCEPT', 'already_instrumented_or_reserved_name'])

    def test_missing_marker_rejection_is_preserved(self):
        _, before, after = self.fixture()
        after.body.pop()
        self.assertEqual(self.outcomes(before, after),
                         ['generated_literal_context_unproven'] * 2)

    def test_changed_literal_rejection_is_preserved(self):
        _, before, after = self.fixture('value', "return 'original'")
        literal = next(node for node in ast.walk(after)
                       if isinstance(node, ast.Constant) and node.value == 'original')
        literal.value = 'changed'
        self.assertEqual(self.outcomes(before, after),
                         ['string_or_bytes_literal_changed'] * 2)


if __name__ == '__main__':
    unittest.main()
