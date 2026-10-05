"""Offline stdlib unittest; extract only reviewed pure AST definitions."""
import ast
import math
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]


def load_functions(relative_path, names, namespace):
    path = ROOT / relative_path
    tree = ast.parse(path.read_text(), filename=str(path))
    nodes = [node for node in tree.body
             if isinstance(node, ast.FunctionDef) and node.name in names]
    assert {node.name for node in nodes} == set(names)
    assert len(nodes) == len(names)
    for node in nodes:
        assert not node.decorator_list
        assert not any(isinstance(child, (ast.Import, ast.ImportFrom))
                       for child in ast.walk(node))
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(path), 'exec'), namespace)
    return tree


NS = {'isfinite': math.isfinite}
load_functions('eimemory/evaluation/metrics.py', ('_round', 'percentile'), NS)
TREE = load_functions('eimemory/evaluation/recall_latency.py',
                      ('summarize_latency', 'tier'), NS)
POLICIES = [node for node in TREE.body if isinstance(node, ast.Assign)
            and any(isinstance(target, ast.Name) and target.id == 'POLICY'
                    for target in node.targets)]
assert len(POLICIES) == 1
NS['POLICY'] = ast.literal_eval(POLICIES[0].value)
SUMMARY = NS['summarize_latency']


class RecallLatencyOverflowTests(unittest.TestCase):
    def test_overflow_returns_exact_existing_invalid_contract(self):
        for value in (10**400, -(10**400)):
            for tier in ('ordinary', 'assisted', 'unknown'):
                with self.subTest(sign=value > 0, tier=tier):
                    self.assertEqual(SUMMARY([{'latency_ms': value, 'latency_tier': tier}]),
                                     {'policy': 'recall-latency-fast3s-assisted10s.v1',
                                      'passed': False, 'reason': 'latency_invalid'})

    def test_existing_invalid_values_unchanged(self):
        for value in (-1, float('inf'), float('-inf'), float('nan'), True, False, None, '5'):
            with self.subTest(value=repr(value)):
                self.assertEqual(SUMMARY([{'latency_ms': value}]),
                                 {'policy': NS['POLICY'], 'passed': False, 'reason': 'latency_invalid'})

    def test_limits_and_counts_unchanged(self):
        result = SUMMARY([{'latency_ms': 3000}, {'latency_ms': 10000, 'latency_tier': 'assisted'}])
        self.assertEqual(result, {'policy': NS['POLICY'], 'ordinary_count': 1,
                         'assisted_count': 1, 'ordinary_p95_ms': 3000.0,
                         'assisted_max_ms': 10000, 'ordinary_limit_ms': 3000,
                         'assisted_limit_ms': 10000, 'passed': True})
        self.assertFalse(SUMMARY([{'latency_ms': 3001}])['passed'])
        self.assertFalse(SUMMARY([{'latency_ms': 10001, 'latency_tier': 'assisted'}])['passed'])

    def test_rounding_and_nearest_rank_unchanged(self):
        self.assertTrue(SUMMARY([{'latency_ms': 3000.0004}])['passed'])
        self.assertFalse(SUMMARY([{'latency_ms': 10000.0004, 'latency_tier': 'assisted'}])['passed'])
        self.assertTrue(SUMMARY([{'latency_ms': 0}] * 19 + [{'latency_ms': 9000}])['passed'])

    def test_tier_semantics_empty_and_large_finite_values(self):
        self.assertTrue(SUMMARY([])['passed'])
        self.assertFalse(SUMMARY([{'latency_ms': 8000, 'latency_tier': 'unknown'}])['passed'])
        self.assertFalse(SUMMARY([{'latency_ms': 10**300}])['passed'])
        self.assertEqual(NS['tier'](None), 'ordinary')
        self.assertEqual(NS['tier']({'relevance_selector': {'caller_assistance': {'calls': 1}}}), 'assisted')

    def test_mixed_sequence_invalidates_whole_report(self):
        self.assertEqual(SUMMARY([{'latency_ms': 1}, {'latency_ms': 10**400}]),
                         {'policy': NS['POLICY'], 'passed': False, 'reason': 'latency_invalid'})


if __name__ == '__main__':
    unittest.main(verbosity=2)
