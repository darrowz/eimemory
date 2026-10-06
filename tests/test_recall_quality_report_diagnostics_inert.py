"""Report-shape regressions with no eimemory package or dependency imports."""
import ast
import math
from pathlib import Path
import typing
import unittest


def load_quality_functions():
    source = Path(__file__).resolve().parents[1] / 'eimemory/evaluation/recall_quality_contract.py'
    tree = ast.parse(source.read_text(encoding='utf-8'))
    tree.body = [node for node in tree.body if not isinstance(node, (ast.Import, ast.ImportFrom))]
    namespace = {'isfinite': math.isfinite, 'Any': typing.Any, 'Mapping': typing.Mapping}
    exec(compile(tree, str(source) + '[inert]', 'exec'), namespace)
    return namespace['evaluate_quality_report']


class RecallQualityReportDiagnosticsTest(unittest.TestCase):
    def evaluate(self, changes=None, *, limits=None):
        report = {'sample_count': 2, 'cross_channel_leakage_count': 0,
                  'source_filter_leakage_count': 0, 'hit_at_1': 1}
        report.update(changes or {})
        return load_quality_functions()(report, limits={'hit_at_1': .5} if limits is None else limits,
            minimum_metrics={'hit_at_1'}, minimum_samples=2,
            judged_contract='judged.v1', required_roles=frozenset({'positive'}))

    def test_missing_roles_yields_boolean_flag(self):
        result = self.evaluate({'evaluation_contract': 'judged.v1',
            'label_trust': 'operator_judged', 'label_roles': []})
        self.assertIs(result['vacuous'], True)
        self.assertEqual(result['recall_quality_evidence']['missing_roles'], ['positive'])
        self.assertEqual(result['evidence_status'], 'insufficient')

    def test_missing_metric_does_not_unassess_performed_comparison(self):
        result = self.evaluate({'source_filter_leakage_count': None, 'hit_at_1': 0})
        self.assertEqual(result['blocking_metrics']['hit_at_1'],
                         {'actual': 0, 'threshold': .5, 'operator': '>='})
        self.assertEqual(result['evidence_status'], 'failed')
        self.assertEqual(result['unassessed_metrics'], [])
        self.assertIs(result['vacuous'], True)

    def test_only_configured_valid_rank_comparisons_are_skipped(self):
        result = self.evaluate({'sample_count': 1, 'hit_at_1': 0})
        self.assertEqual(result['unassessed_metrics'], ['hit_at_1'])
        self.assertEqual(result['blocking_metrics'], {})
        self.assertEqual(result['evidence_status'], 'insufficient')

    def test_invalid_rank_is_blocked_not_unassessed(self):
        result = self.evaluate({'sample_count': 1, 'hit_at_1': float('nan')})
        self.assertIn('hit_at_1', result['blocking_metrics'])
        self.assertEqual(result['unassessed_metrics'], [])
        self.assertEqual(result['evidence_status'], 'failed')

    def test_absent_rank_has_no_performed_or_skipped_comparison(self):
        result = self.evaluate({'sample_count': 1, 'hit_at_1': None})
        self.assertEqual(result['unassessed_metrics'], [])
        self.assertIn('hit_at_1', result['recall_quality_evidence']['missing_metrics'])

    def test_sufficient_path_and_threshold_failure_unchanged(self):
        result = self.evaluate()
        self.assertIs(result['ok'], True)
        self.assertIs(result['vacuous'], False)
        self.assertEqual(result['unassessed_metrics'], [])
        result = self.evaluate({'hit_at_1': 0})
        self.assertEqual(result['evidence_status'], 'failed')
        self.assertIs(result['vacuous'], False)
        self.assertIn('hit_at_1', result['blocking_metrics'])

    def test_leakage_failure_retains_priority_on_small_sample(self):
        result = self.evaluate({'sample_count': 1, 'cross_channel_leakage_count': 1})
        self.assertEqual(result['evidence_status'], 'failed')
        self.assertIn('cross_channel_leakage_count', result['blocking_metrics'])
        self.assertEqual(result['unassessed_metrics'], ['hit_at_1'])


if __name__ == '__main__':
    unittest.main()
