"""Focused AST/fake regression tests; no project import or integration claims.

Install at repository tests/test_semantic_recall_context_prevalidation.py.
Run: python -m unittest discover -s tests -p test_semantic_recall_context_prevalidation.py -v
Standard pytest discovery is also supported; project pytest was not run here.
"""
import ast
from collections import UserDict
from copy import deepcopy
from dataclasses import asdict, dataclass
from hashlib import sha256
import json
from pathlib import Path
from types import MappingProxyType, SimpleNamespace
import unittest


@dataclass
class FakeScope:
    tenant: str = 'fake'

    @classmethod
    def from_dict(cls, value):
        return cls(**value)


class DictSubclass(dict):
    pass


MISSING = object()


def load_functions():
    source = Path(__file__).resolve().parents[1] / 'eimemory/evaluation/semantic_recall.py'
    tree = ast.parse(source.read_bytes(), filename=str(source))
    names = {'validate_dataset', 'evaluate_semantic_recall'}
    selected = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in names]
    if {node.name for node in selected} != names:
        raise AssertionError('Required source functions missing')
    namespace = {
        'ScopeRef': FakeScope, 'asdict': asdict, 'normalize_source_id': lambda value: value,
        'perf_counter': lambda: 0.0, 'sha256': sha256, 'json': json, 'THRESHOLDS': {},
        '_semantic_bundle_path_verified': lambda *args: False,
        'latency_tier': lambda *args: 'fake', 'score_case': lambda *args, **kwargs: {'passed': False},
        'summarize': lambda *args: {'passed': False}, 'release_quality_passed': lambda *args: False,
        'now_iso': lambda: 'FAKE',
    }
    exec(compile(ast.Module(body=selected, type_ignores=[]), str(source), 'exec'), namespace)
    return namespace


def make_dataset(contexts, positive=True):
    cases = []
    for index, context in enumerate(contexts):
        case = {'case_id': str(index), 'query': 'q' + str(index), 'intent_group': 'g' + str(index),
                'split': 'regression', 'expected_groups': [['ref' + str(index)]] if positive else []}
        if context is not MISSING:
            case['task_context'] = context
        cases.append(case)
    return {'schema': 'semantic_recall_cases.v1', 'scope': {'tenant': 'fake'},
            'source_id': 'fake-source', 'cases': cases}


class ContextPrevalidationTests(unittest.TestCase):
    def setUp(self):
        self.functions = load_functions()
        self.events = []

        def lookup(ref, **kwargs):
            self.events.append(('label', ref, kwargs))
            return SimpleNamespace(status='active')

        def recall(**kwargs):
            self.events.append(('recall', kwargs))
            return SimpleNamespace(items=[], explanation={})

        self.runtime = SimpleNamespace(
            store=SimpleNamespace(get_by_exact_ref=lookup),
            memory=SimpleNamespace(recall=recall, recall_engine=SimpleNamespace(effective_identity=lambda: {})))

    def evaluate(self, dataset):
        return self.functions['evaluate_semantic_recall'](self.runtime, dataset)

    def test_bad_integer_preflight_at_every_position(self):
        for positive in (True, False):
            for position in range(3):
                with self.subTest(positive=positive, position=position):
                    self.events.clear()
                    contexts = [None, None, None]
                    contexts[position] = 42
                    dataset = make_dataset(contexts, positive)
                    before = deepcopy(dataset)
                    with self.assertRaisesRegex(ValueError, '^semantic_task_context_invalid$'):
                        self.evaluate(dataset)
                    self.assertEqual(self.events, [])
                    self.assertEqual(dataset, before)

    def test_malformed_pairs_preflight(self):
        for bad in ([['key']], [['key', 'value', 'extra']], [None], 'bad'):
            with self.subTest(bad=bad):
                self.events.clear()
                with self.assertRaisesRegex(ValueError, '^semantic_task_context_invalid$'):
                    self.evaluate(make_dataset([None, bad]))
                self.assertEqual(self.events, [])

    def test_compatible_shapes_original_digest_and_authority_override(self):
        options = [MISSING, None, False, 0, '', [], {},
                   {'hint': 'keep', 'exact_scope_only': False, 'source_ids': ['wrong']},
                   DictSubclass(hint='keep'), (('hint', 'keep'),), [['hint', 'keep']]]
        for context in options:
            with self.subTest(context=context):
                self.events.clear()
                dataset = make_dataset([context])
                before = deepcopy(dataset)
                serialized = json.dumps(dataset, ensure_ascii=False, sort_keys=True)
                report = self.evaluate(dataset)
                received = self.events[-1][1]['task_context']
                self.assertIs(received['exact_scope_only'], True)
                self.assertEqual(received['source_ids'], ['fake-source'])
                if context is not MISSING and context:
                    self.assertEqual(received.get('hint'), 'keep')
                self.assertEqual(dataset, before)
                self.assertEqual(json.dumps(dataset, ensure_ascii=False, sort_keys=True), serialized)
                self.assertEqual(report['dataset_digest'], sha256(serialized.encode()).hexdigest())
                self.assertEqual([event[0] for event in self.events], ['label', 'recall'])

    def test_cached_context_is_independent_shallow_dict(self):
        nested = ['retain']
        original = {'hint': nested}
        dataset = make_dataset([original])
        cases = self.functions['validate_dataset'](dataset)
        cached = cases[0]['task_context']
        self.assertIs(type(cached), dict)
        self.assertIsNot(cases[0], dataset['cases'][0])
        self.assertIsNot(cached, original)
        self.assertIs(cached['hint'], nested)
        # Pin evaluator's separate copy, in addition to validator's cached copy.
        self.functions['validate_dataset'] = lambda dataset: cases
        self.evaluate(dataset)
        received = self.events[-1][1]['task_context']
        self.assertIsNot(received, cached)
        self.assertEqual(cached, {'hint': nested})
        self.assertEqual(original, {'hint': nested})

    def test_case_scope_and_source_override_dataset_defaults(self):
        dataset = make_dataset([{'exact_scope_only': False, 'source_ids': ['wrong']}])
        dataset['cases'][0].update(scope={'tenant': 'case'}, source_id='case-source')
        before = deepcopy(dataset)
        self.evaluate(dataset)
        received = self.events[-1][1]
        self.assertEqual(received['scope'], {'tenant': 'case'})
        self.assertEqual(received['task_context'], {'exact_scope_only': True, 'source_ids': ['case-source']})
        self.assertEqual(self.events[0][2], {'scope': FakeScope('case'), 'source_id': 'case-source'})
        self.assertEqual(dataset, before)

    def test_non_json_mappings_and_iterator_retain_digest_limitation(self):
        for context in (UserDict(hint='keep'), MappingProxyType({'hint': 'keep'}), iter([('hint', 'keep')])):
            with self.subTest(kind=type(context).__name__):
                self.events.clear()
                with self.assertRaisesRegex(TypeError, 'not JSON serializable'):
                    self.evaluate(make_dataset([context]))
                self.assertEqual([event[0] for event in self.events], ['label', 'recall'])
                self.assertEqual(self.events[-1][1]['task_context']['hint'], 'keep')

    def test_existing_validation_diagnostics_and_order(self):
        examples = [(None, 'semantic_dataset_schema_invalid'),
                    (make_dataset([]), 'semantic_dataset_bounds')]
        for change, expected in [({'case_id': ''}, 'semantic_case_identity_invalid'),
                                 ({'expected_groups': 'bad'}, 'semantic_expected_groups_invalid'),
                                 ({'expected_groups': [['ref'], ['ref']]}, 'semantic_expected_groups_overlap'),
                                 ({'forbidden_refs': [1]}, 'semantic_forbidden_refs_invalid')]:
            dataset = make_dataset([42])
            dataset['cases'][0].update(change)
            examples.append((dataset, expected))
        dataset = make_dataset([None]); dataset['cases'] = [42]
        examples.append((dataset, 'semantic_case_invalid'))
        for dataset, expected in examples:
            with self.subTest(expected=expected):
                with self.assertRaisesRegex(ValueError, '^' + expected + '$'):
                    self.evaluate(dataset)
                self.assertEqual(self.events, [])

    def test_all_labels_still_precede_any_recall(self):
        self.evaluate(make_dataset([None, {}]))
        self.assertEqual([event[0] for event in self.events], ['label', 'label', 'recall', 'recall'])
        self.events.clear()
        self.runtime.store.get_by_exact_ref = lambda *args, **kwargs: None
        with self.assertRaisesRegex(ValueError, '^semantic_label_authority_missing$'):
            self.evaluate(make_dataset([None]))
        self.assertEqual(self.events, [])


if __name__ == '__main__':
    unittest.main()
