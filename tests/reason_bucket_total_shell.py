"""AST-only bounded counter tests with synthetic labels and ordinary integers."""
from __future__ import annotations

import argparse
import ast
import builtins
import copy
import hashlib
from pathlib import Path
import unittest

BASELINE_SHA256 = "c0f20e4126deb8277bfead20ccc21bd52f054b8070bda32761da12beffdb4e1e"
OVERFLOW = "other_unmappable_reason"


def extract_shell(path, expected):
    data = path.read_bytes()
    actual = hashlib.sha256(data).hexdigest()
    if actual != expected:
        raise ValueError(f"Pinned source digest mismatch: {actual}")
    module = ast.parse(data, filename=str(path))
    methods = [n for n in module.body if isinstance(n, ast.FunctionDef) and n.name == "_bounded_reason_counts"]
    constants = [n for n in module.body if isinstance(n, ast.Assign) and len(n.targets) == 1 and isinstance(n.targets[0], ast.Name) and n.targets[0].id == "_MAX_REASON_BUCKETS"]
    if len(methods) != 1 or methods[0].decorator_list or len(constants) != 1:
        raise ValueError("Expected one complete helper and one literal cap")
    method, constant = methods[0], constants[0]
    if not isinstance(constant.value, ast.Constant) or type(constant.value.value) is not int or constant.value.value != 128:
        raise ValueError("Unexpected cap literal")
    if actual == BASELINE_SHA256 and ((method.lineno, method.end_lineno) != (1519, 1529) or constant.lineno != 57):
        raise ValueError("Unexpected baseline boundary")
    forbidden = []
    def fenced_import(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "__future__" and level == 0:
            return builtins.__import__(name, globals, locals, fromlist, level)
        forbidden.append(name)
        raise AssertionError("Target imports and other helpers are forbidden")
    namespace = {"__builtins__": {**vars(builtins), "__import__": fenced_import}}
    isolated = ast.fix_missing_locations(ast.Module(body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0), copy.deepcopy(constant), copy.deepcopy(method)], type_ignores=[]))
    exec(compile(isolated, str(path), "exec"), namespace)
    print(f"SOURCE_SHA256 {actual}")
    print(f"EXTRACTED _bounded_reason_counts L{method.lineno}-{method.end_lineno}; literal cap128")
    return namespace, forbidden


def counter_tests(namespace, forbidden):
    class ReasonBucketTotalTests(unittest.TestCase):
        def tearDown(self):
            self.assertEqual(forbidden, [])
            self.assertEqual(namespace["_MAX_REASON_BUCKETS"], 128)

        def evaluate(self, counts):
            self.assertTrue(all(type(key) is str and type(value) is int for key, value in counts.items()))
            original = dict(counts)
            result = namespace["_bounded_reason_counts"](counts)
            self.assertEqual(counts, original)
            self.assertIsNot(result, counts)
            self.assertTrue(all(type(key) is str and key and type(value) is int and value >= 0 for key, value in result.items()))
            self.assertLessEqual(len(result), 128)
            return result

        def assert_total(self, counts, result):
            self.assertEqual(sum(result.values()), sum(max(0, value) for key, value in counts.items() if key))

        def ordinary(self, size, value=1):
            return {f"synthetic_reason_{i:03d}": value for i in range(size)}

        def test_high_existing_overflow_is_counted_once(self):
            counts = {**self.ordinary(128), OVERFLOW: 1000}
            result = self.evaluate(counts)
            self.assert_total(counts, result)
            self.assertEqual(result, {**self.ordinary(127), OVERFLOW: 1001})
            self.assertEqual(list(result), [*self.ordinary(127), OVERFLOW])

        def test_existing_overflow_on_rank_tie_does_not_take_an_ordinary_slot(self):
            counts = {**self.ordinary(128), OVERFLOW: 1}
            result = self.evaluate(counts)
            self.assert_total(counts, result)
            self.assertEqual(result, {**self.ordinary(127), OVERFLOW: 2})

        def test_low_and_zero_overflow_are_not_duplicated(self):
            for old in (0, 1):
                with self.subTest(old=old):
                    counts = {**self.ordinary(130, 2), OVERFLOW: old}
                    result = self.evaluate(counts)
                    self.assert_total(counts, result)
                    self.assertEqual(result, {**self.ordinary(127, 2), OVERFLOW: old + 6})

        def test_absent_overflow_keeps_existing_rank_and_sum(self):
            counts = {f"synthetic_reason_{i:03d}": i + 1 for i in range(129)}
            result = self.evaluate(counts)
            self.assert_total(counts, result)
            expected = {f"synthetic_reason_{i:03d}": i + 1 for i in range(2, 129)}
            self.assertEqual(result, {**expected, OVERFLOW: 3})
            self.assertEqual(list(result), [*expected, OVERFLOW])

        def test_empty_and_small_inputs_keep_normalization_and_sorted_keys(self):
            examples = [({}, {}), ({"synthetic_z": -2, "": 500, "synthetic_a": 3}, {"synthetic_a": 3, "synthetic_z": 0}), ({"synthetic_z": 0, OVERFLOW: 5}, {OVERFLOW: 5, "synthetic_z": 0})]
            for counts, expected in examples:
                with self.subTest(counts=counts):
                    result = self.evaluate(counts)
                    self.assert_total(counts, result)
                    self.assertEqual(result, expected)
                    self.assertEqual(list(result), sorted(expected))

        def test_exact_cap_with_existing_overflow_is_unchanged(self):
            counts = {**self.ordinary(127), OVERFLOW: 1000}
            result = self.evaluate(counts)
            self.assert_total(counts, result)
            self.assertEqual(result, counts)
            self.assertEqual(list(result), sorted(counts))

        def test_exact_cap_without_overflow_and_one_above_cap(self):
            for size in (128, 129):
                with self.subTest(size=size):
                    counts = self.ordinary(size)
                    result = self.evaluate(counts)
                    self.assert_total(counts, result)
                    expected = counts if size == 128 else {**self.ordinary(127), OVERFLOW: 2}
                    self.assertEqual(result, expected)

        def test_negative_and_empty_key_normalization_precedes_capping(self):
            counts = {**self.ordinary(129), "synthetic_negative": -7, "": 10000, OVERFLOW: 700}
            result = self.evaluate(counts)
            self.assert_total(counts, result)
            self.assertEqual(result, {**self.ordinary(127), OVERFLOW: 702})
            self.assertNotIn("", result)
            self.assertNotIn("synthetic_negative", result)

        def test_all_zero_ties_retain_best_ordinary_keys(self):
            counts = {**self.ordinary(128, 0), OVERFLOW: 0}
            result = self.evaluate(counts)
            self.assert_total(counts, result)
            self.assertEqual(result, {**self.ordinary(127, 0), OVERFLOW: 0})

        def test_multiple_sizes_and_distributions_preserve_normalized_total(self):
            for size in (1, 127, 128, 129, 257):
                for old in (None, 0, 1, 10000):
                    with self.subTest(size=size, old=old):
                        counts = {f"synthetic_reason_{i:03d}": (i * 17) % 41 - 3 for i in range(size)}
                        if old is not None:
                            counts[OVERFLOW] = old
                        result = self.evaluate(counts)
                        self.assert_total(counts, result)
                        if len(counts) > 128:
                            self.assertIn(OVERFLOW, result)
                            self.assertEqual(len(result), 128)
                            self.assertEqual(list(result)[:-1], sorted(key for key in result if key != OVERFLOW))
    return ReasonBucketTotalTests


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--expected-sha256", required=True)
    args = parser.parse_args()
    namespace, forbidden = extract_shell(args.source, args.expected_sha256)
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(counter_tests(namespace, forbidden))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    raise SystemExit(0 if result.wasSuccessful() else 1)


if __name__ == "__main__":
    main()
