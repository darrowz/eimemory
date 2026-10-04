"""Finite source-extracted diagnostics tests; no project module imports.

The selected pure function is compiled/executed after its sole relative import
is replaced with an inert safe_report stub. The local post-render dictionary
assignments are also compiled/executed; render itself is not. Caller branches
are checked structurally. Module bodies and all Runtime, retrieval, model,
provider, adapter and storage code remain unexecuted.
"""
from __future__ import annotations

import ast
import copy
import json
import math
from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parents[1]


def load_stage_diagnostics():
    source = ROOT / 'eimemory/retrieval/stage_diagnostics.py'
    tree = ast.parse(source.read_text(encoding='utf-8'), filename=str(source))
    selected = copy.deepcopy(next(node for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == 'retrieval_stage_diagnostics'))

    class InertImport(ast.NodeTransformer):
        replaced = 0

        def visit_ImportFrom(self, node):
            if (node.level == 1 and node.module == 'independent_evidence'
                    and [alias.name for alias in node.names] == ['safe_report']):
                self.replaced += 1
                return ast.copy_location(ast.Pass(), node)
            raise AssertionError('unexpected import in selected pure function')

        def visit_Import(self, node):
            raise AssertionError('unexpected import in selected pure function')

    transform = InertImport()
    selected = transform.visit(selected)
    if transform.replaced != 1:
        raise AssertionError('expected exactly one inert evidence-report import')
    module = ast.fix_missing_locations(ast.Module(body=[selected], type_ignores=[]))
    namespace = {'math': math, 're': re, 'safe_report': lambda _value: {}}
    # Deliberate extracted-function compile/exec, not a project module import.
    exec(compile(module, str(source) + ':inert-extraction', 'exec'), namespace)
    return namespace['retrieval_stage_diagnostics']


class NumericStageBounds(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.diagnostics = staticmethod(load_stage_diagnostics())

    def stage_count(self, value):
        return self.diagnostics({'engine_diagnostics': {'candidate_count': value}})['engine']['candidate_count']

    def test_existing_finite_values_keep_values_and_types(self):
        for raw, expected in ((0, 0), (12, 12), (12.5, 12.5), (-1, 0),
                              (1_000_001, 1_000_000), (1_000_001.5, 1_000_000)):
            with self.subTest(value=raw):
                result = self.stage_count(raw)
                self.assertEqual(result, expected)
                self.assertIs(type(result), type(expected))

    def test_401_digit_positive_and_negative_integers_saturate(self):
        positive = json.loads('1' + '0' * 400)
        for raw, expected in ((positive, 1_000_000), (-positive, 0)):
            with self.subTest(sign='positive' if raw > 0 else 'negative'):
                self.assertEqual(self.stage_count(raw), expected)

    def test_unsupported_values_keep_zero_fallback(self):
        for raw in (True, False, float('nan'), float('inf'), -float('inf'), '12', None, [], {}):
            with self.subTest(type=type(raw).__name__, value=repr(raw)):
                self.assertEqual(self.stage_count(raw), 0)

    def test_huge_nested_drop_counts_are_bounded(self):
        result = self.diagnostics({'engine_diagnostics': {
            key: {'too_large': 10**400, 'negative': -(10**400), 'boolean': True}
            for key in ('drops', 'dropped_reasons', 'blocked_counts')}})
        for key in ('drops', 'dropped_reasons', 'blocked_counts'):
            self.assertEqual(result['engine'][key], {'too_large': 1_000_000, 'negative': 0, 'boolean': 0})

    def test_untrusted_huge_values_cannot_block_demotion(self):
        result = self.diagnostics({'engine_diagnostics': {'candidate_count': 10**400}}, trusted_retrieval=False)
        self.assertEqual(result['engine'], {'status': 'unknown'})
        self.assertEqual(result['retrieval_status'], 'unknown')

    def test_numeric_saturation_does_not_mint_success(self):
        result = self.diagnostics({'engine_diagnostics': {'candidate_count': 10**400}})
        self.assertEqual(result['retrieval_status'], 'unknown')
        self.assertNotIn('status', result['engine'])
        self.assertEqual(result['delivery'], {})
        self.assertEqual(result['assistance'], {'status': 'not_reported'})

    def test_stricter_counters_and_empty_shapes_are_unchanged(self):
        result = self.diagnostics({'relevance_selector': {'caller_assistance': {
            'candidate_count': 10**400, 'calls': True}}},
            trusted_retrieval=True, post_selection={'selected_unique_count': 10**400, 'render_empty_count': 0})
        self.assertEqual(result['verifier_boundary'], {'candidate_count': 'unknown', 'calls': 'unknown'})
        self.assertEqual(result['post_selection'], {'selected_unique_count': 'unknown', 'render_empty_count': 0})
        empty = self.diagnostics({'relevance_selector': {'caller_assistance': {}}}, post_selection={})
        self.assertEqual(empty['engine'], {})
        self.assertEqual(empty['selector'], {})
        self.assertEqual(empty['assistance'], {'status': 'not_run', 'calls': 0})
        self.assertEqual(empty['post_selection'], {})


def proactive_source():
    source = ROOT / 'eimemory/retrieval/proactive.py'
    return source, ast.parse(source.read_text(encoding='utf-8'), filename=str(source))


def load_render_projection():
    """Execute only copied local dict assignments after rendering, never render."""
    source, tree = proactive_source()
    decide = next(node for node in ast.walk(tree)
                  if isinstance(node, ast.FunctionDef) and node.name == 'decide')
    def target_names(node):
        if not isinstance(node, ast.Assign):
            return []
        return [child.id for target in node.targets for child in ast.walk(target)
                if isinstance(child, ast.Name)]
    start = next(index for index, node in enumerate(decide.body)
                 if target_names(node) == ['context', 'delivered_items'])
    end = next(index for index in range(start + 1, len(decide.body))
               if target_names(decide.body[index]) == ['persisted_items'])
    statements = copy.deepcopy(decide.body[start + 1:end])
    # The slice must stay pure data projection, never gain another call/import.
    if any(isinstance(node, (ast.Import, ast.ImportFrom)) for statement in statements
           for node in ast.walk(statement)):
        raise AssertionError('unexpected import in local delivery projection')
    for statement in statements:
        if not isinstance(statement, ast.Assign):
            raise AssertionError('expected only local delivery assignments')
        for node in ast.walk(statement):
            if isinstance(node, ast.Call) and not (isinstance(node.func, ast.Name) and node.func.id == 'len'):
                raise AssertionError('unexpected call in local delivery projection')
    function = ast.FunctionDef(name='project', args=ast.arguments(
        posonlyargs=[], args=[ast.arg(arg=name) for name in
            ('explanation', 'proposed_delivery', 'delivered_items', 'context')],
        kwonlyargs=[], kw_defaults=[], defaults=[]),
        body=statements + [ast.Return(value=ast.Name(id='explanation', ctx=ast.Load()))],
        decorator_list=[])
    namespace = {}
    module = ast.fix_missing_locations(ast.Module(body=[function], type_ignores=[]))
    # Source-derived pure assignments only; the render callback is not executed.
    exec(compile(module, str(source) + ':inert-render-projection', 'exec'), namespace)
    return namespace['project']


class LocalDeliveryProvenance(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.diagnostics = staticmethod(load_stage_diagnostics())
        cls.project = staticmethod(load_render_projection())

    def test_upstream_delivery_claims_are_never_inherited(self):
        claims = ({'status': 'context_delivered', 'delivered_count': 3},
                  {'status': 'context_delivered', 'delivered_count': 10**400},
                  {'status': 'no_context', 'delivered_count': True}, 'context_delivered', [1, 2], None)
        for trust in (True, False, None):
            for claim in claims:
                with self.subTest(trusted=trust, claim_type=type(claim).__name__):
                    result = self.diagnostics({'delivery_diagnostics': claim,
                        'local_delivery': {'status': 'context_delivered', 'delivered_count': 99}},
                        trusted_retrieval=trust)
                    self.assertEqual(result['delivery'], {})

    def test_explicit_local_delivery_survives_both_retrieval_trust_states(self):
        local = {'status': 'context_delivered', 'proposed_count': 2,
                 'delivered_count': 1, 'context_chars': 7,
                 'dropped_reasons': {'context_budget_exceeded': 1}}
        for trust in (True, False):
            with self.subTest(trusted=trust):
                result = self.diagnostics({'retrieval_status': 'evidence_found',
                    'engine_diagnostics': {'status': 'success'},
                    'delivery_diagnostics': {'status': 'no_context'}},
                    local_delivery=local, trusted_retrieval=trust)
                self.assertEqual(result['delivery'], local)
                if trust is False:
                    self.assertEqual(result['engine'], {'status': 'unknown'})
                    self.assertEqual(result['retrieval_status'], 'unknown')

    def test_explicit_no_context_zero_counts_and_empty_shapes_survive(self):
        local = {'status': 'no_context', 'proposed_count': 0,
                 'delivered_count': 0, 'context_chars': 0, 'dropped_reasons': {}}
        self.assertEqual(self.diagnostics({}, local_delivery=local)['delivery'], local)
        for empty in (None, {}):
            with self.subTest(empty=empty):
                self.assertEqual(self.diagnostics({}, local_delivery=empty)['delivery'], {})
        self.assertEqual(self.diagnostics({})['delivery'], {})

    def test_local_argument_uses_bounded_sanitization_without_minting_status(self):
        local = {'delivered_count': 10**400, 'proposed_count': -(10**400),
                 'context_chars': True, 'raw_prompt': 'synthetic ignored data'}
        result = self.diagnostics({}, local_delivery=local)
        self.assertEqual(result['delivery'], {'delivered_count': 1_000_000, 'proposed_count': 0, 'context_chars': 0})
        self.assertNotIn('status', result['delivery'])
        self.assertEqual(result['retrieval_status'], 'unknown')

    def test_unrelated_diagnostic_sections_are_unchanged(self):
        explanation = {'retrieval_status': 'degraded',
            'pipeline': {'phases': [{'name': 'collect', 'selected_count': 2}]},
            'relevance_selector': {'status': 'degraded', 'caller_assistance': {}},
            'delivery_diagnostics': {'status': 'fabricated'}}
        baseline = self.diagnostics(explanation, post_selection={})
        updated = self.diagnostics(explanation, post_selection={}, local_delivery={'status': 'no_context'})
        self.assertEqual({k: v for k, v in baseline.items() if k != 'delivery'},
                         {k: v for k, v in updated.items() if k != 'delivery'})

    def test_actual_render_projection_uses_only_measured_values(self):
        original = {'delivery_diagnostics': {'status': 'fabricated', 'delivered_count': 10**400},
                    'local_delivery': {'status': 'fabricated'}, 'retained': 'same'}
        projected = self.project(original, [1, 2], [1], 'context')
        self.assertEqual(projected['delivery_diagnostics'], {
            'status': 'context_delivered', 'proposed_count': 2, 'delivered_count': 1,
            'context_chars': 7, 'dropped_reasons': {'context_budget_exceeded': 1}})
        self.assertEqual(original['delivery_diagnostics']['status'], 'fabricated')
        self.assertEqual(projected['retained'], 'same')
        empty = self.project(original, [], [], '')
        self.assertEqual(empty['delivery_diagnostics'], {
            'status': 'no_context', 'proposed_count': 0, 'delivered_count': 0,
            'context_chars': 0, 'dropped_reasons': {}})

    def test_actual_early_branch_omits_local_delivery_and_returns_empty_context(self):
        _source, tree = proactive_source()
        calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call)
                 and isinstance(node.func, ast.Name) and node.func.id == 'retrieval_stage_diagnostics']
        self.assertEqual(len(calls), 2)
        early, normal = sorted(calls, key=lambda node: node.lineno)
        self.assertNotIn('local_delivery', [item.arg for item in early.keywords])
        empty_fn = next(node for node in ast.walk(tree)
                        if isinstance(node, ast.FunctionDef) and node.name == '_empty_decision')
        payload = next(node.value for node in empty_fn.body if isinstance(node, ast.Return))
        fields = {key.value: value for key, value in zip(payload.keys, payload.values)}
        self.assertIsInstance(fields['items'], ast.List)
        self.assertEqual(fields['items'].elts, [])
        self.assertEqual(fields['context'].value, '')
        observed = self.diagnostics({'retrieval_status': 'unavailable',
            'delivery_diagnostics': {'status': 'context_delivered', 'delivered_count': 3}},
            trusted_retrieval=False)
        self.assertEqual(observed['delivery'], {})

    def test_actual_normal_call_passes_only_the_local_projection(self):
        _source, tree = proactive_source()
        calls = sorted([node for node in ast.walk(tree) if isinstance(node, ast.Call)
                 and isinstance(node.func, ast.Name) and node.func.id == 'retrieval_stage_diagnostics'],
                 key=lambda node: node.lineno)
        self.assertEqual(len(calls), 2)
        local = next((item.value for item in calls[1].keywords if item.arg == 'local_delivery'), None)
        self.assertIsInstance(local, ast.Name)
        self.assertEqual(local.id, 'local_delivery')
        assignments = [node for node in ast.walk(tree) if isinstance(node, ast.Assign)
                       and any(isinstance(target, ast.Name) and target.id == 'local_delivery' for target in node.targets)]
        self.assertEqual(len(assignments), 1)
        self.assertIsInstance(assignments[0].value, ast.Dict)
        names = {node.id for node in ast.walk(assignments[0].value) if isinstance(node, ast.Name)}
        self.assertEqual(names, {'len', 'proposed_delivery', 'delivered_items', 'context'})
        self.assertLess(assignments[0].lineno, calls[1].lineno)


if __name__ == '__main__':
    unittest.main(verbosity=2)
