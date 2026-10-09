"""Compile only reviewed DTO and counter helper; no application imports or IO."""
import ast
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
import unittest

SOURCE = Path(__file__).resolve().parents[1] / 'eimemory/ops/backfill_capability_v3.py'

def extract(filename):
    parsed = ast.parse(Path(filename).read_text())
    wanted = {'BackfillRowResult', '_reason_counts'}
    nodes = [n for n in parsed.body if isinstance(n, (ast.ClassDef, ast.FunctionDef)) and n.name in wanted]
    assert {n.name for n in nodes} == wanted
    ns = {'__name__': __name__, 'dataclass': dataclass, 'Counter': Counter, '_MAX_REASON_BUCKETS': 128}
    exec(compile(ast.Module(body=nodes, type_ignores=[]), '<two-reviewed-definitions>', 'exec'), ns)
    return ns['BackfillRowResult'], ns['_reason_counts']

class ReasonCountsTests(unittest.TestCase):
    def rows(self, counts):
        return [self.row_type(str(i), 'unmappable', key) for key, value in counts.items() for i in range(value)]
    def count(self, counts):
        return type(self).counter(self.rows(counts))
    def test_reserved_reason_is_not_lost(self):
        counts = {'other_unmappable_reason': 5, **{f'r{i:03}': 1 for i in range(128)}}
        result = self.count(counts)
        self.assertEqual(sum(result.values()), 133)
        self.assertEqual(result['other_unmappable_reason'], 6)
        self.assertEqual(len(result), 128)
    def test_small_input_preserves_literal_reserved_reason(self):
        self.assertEqual(self.count({'other_unmappable_reason': 5, 'x': 2}), {'other_unmappable_reason': 5, 'x': 2})
    def test_no_reserved_input_retains_existing_tie_order(self):
        counts = {f'r{i:03}': 1 for i in range(130)}
        result = self.count(counts)
        self.assertEqual(result, {**{f'r{i:03}': 1 for i in range(127)}, 'other_unmappable_reason': 3})
    def test_reserved_already_in_overflow_is_counted(self):
        counts = {**{f'r{i:03}': 2 for i in range(128)}, 'other_unmappable_reason': 1}
        result = self.count(counts)
        self.assertEqual(sum(result.values()), 257)
        self.assertEqual(result['other_unmappable_reason'], 3)
    def test_empty_and_non_unmappable_rows_do_not_count(self):
        self.assertEqual(type(self).counter([]), {})
        rows = [self.row_type('x', 'mapped', 'reason'), self.row_type('y', 'unmappable', '')]
        self.assertEqual(type(self).counter(rows), {})

ReasonCountsTests.row_type, ReasonCountsTests.counter = extract(SOURCE)

if __name__ == '__main__':
    unittest.main(verbosity=2)
