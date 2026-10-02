"""AST-only aggregation of synthetic, preaggregated revision/binding counters."""
from __future__ import annotations

import argparse
import ast
import builtins
import copy
import hashlib
from pathlib import Path
import unittest

BASELINE_SHA256 = "f8dc4fbad6f3fa8906df3c6f7c1190ea19ad1a7500346c26317f471959e26e58"


def extract_shell(path, expected):
    data = path.read_bytes()
    actual = hashlib.sha256(data).hexdigest()
    if actual != expected:
        raise ValueError(f"Pinned source digest mismatch: {actual}")
    module = ast.parse(data, filename=str(path))
    functions = [n for n in module.body if isinstance(n, ast.FunctionDef) and n.name == "_dynamic_target_aggregate"]
    if len(functions) != 1 or functions[0].decorator_list:
        raise ValueError("Expected one complete undecorated function")
    function = functions[0]
    if actual == BASELINE_SHA256 and (function.lineno, function.end_lineno) != (613, 642):
        raise ValueError("Unexpected baseline boundary")
    forbidden = []
    def fenced_import(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "__future__" and level == 0:
            return builtins.__import__(name, globals, locals, fromlist, level)
        forbidden.append(name)
        raise AssertionError("Target imports, catalog and ledger helpers are forbidden")
    namespace = {"__builtins__": {**vars(builtins), "__import__": fenced_import}}
    isolated = ast.fix_missing_locations(ast.Module(body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0), copy.deepcopy(function)], type_ignores=[]))
    exec(compile(isolated, str(path), "exec"), namespace)
    print(f"SOURCE_SHA256 {actual}")
    print(f"EXTRACTED _dynamic_target_aggregate L{function.lineno}-{function.end_lineno}")
    return namespace, forbidden


def expected(observations=0, decisive=0, passes=0, failures=0, rate=None, latest=None):
    return {"observation_count": observations, "decisive_count": decisive, "pass_count": passes, "failure_count": failures, "pass_rate": rate, "latest_observation": latest if latest is not None else {}}


def bucket(observations, decisive, passes, failures, latest=None):
    assert all(type(value) is int and value >= 0 for value in (observations, decisive, passes, failures))
    result = {"observation_count": observations, "decisive_count": decisive, "pass_count": passes, "failure_count": failures}
    if latest is not None:
        result["latest"] = latest
    return result


def target(revision, binding, case):
    return {"capability_revision_id": revision, "provider_binding_id": binding, "case_label": case}


def aggregate_tests(namespace, forbidden):
    class DynamicTargetCountTests(unittest.TestCase):
        def tearDown(self):
            self.assertEqual(forbidden, [])

        def evaluate(self, value, targets):
            def check_plain(node):
                self.assertIn(type(node), (dict, list, str, int, type(None)))
                if type(node) is dict:
                    self.assertTrue(all(type(key) is str for key in node))
                    for child in node.values():
                        check_plain(child)
                elif type(node) is list:
                    for child in node:
                        check_plain(child)
                elif type(node) is int:
                    self.assertGreaterEqual(node, 0)
            check_plain(value)
            check_plain(targets)
            self.assertTrue(all(type(item) is dict for item in targets))
            before_value = copy.deepcopy(value)
            before_targets = copy.deepcopy(targets)
            target_ids = [id(item) for item in targets]
            target_key_orders = [list(item) for item in targets]
            result = namespace["_dynamic_target_aggregate"](value, targets=targets)
            self.assertEqual(value, before_value)
            self.assertEqual(targets, before_targets)
            self.assertEqual([id(item) for item in targets], target_ids)
            self.assertEqual([list(item) for item in targets], target_key_orders)
            self.assertEqual(set(result), set(expected()))
            return result

        def test_several_cases_sharing_one_pair_count_once(self):
            value = {"revisions": {"synthetic_r0": {"bindings": {"synthetic_b0": bucket(10, 8, 6, 2)}}}}
            targets = [target("synthetic_r0", "synthetic_b0", f"synthetic_case_{i}") for i in range(3)]
            result = self.evaluate(value, targets)
            self.assertEqual(result, expected(10, 8, 6, 2, 0.75))
            self.assertEqual(len(targets), 3)

        def test_two_bindings_within_one_revision_each_contribute_once(self):
            value = {"revisions": {"synthetic_r0": {"bindings": {"synthetic_b0": bucket(2, 2, 1, 1), "synthetic_b1": bucket(5, 4, 3, 1)}}}}
            targets = [target("synthetic_r0", binding, f"synthetic_case_{i}") for i, binding in enumerate(("synthetic_b0", "synthetic_b1", "synthetic_b0", "synthetic_b1"))]
            self.assertEqual(self.evaluate(value, targets), expected(7, 6, 4, 2, 0.666667))

        def test_same_binding_key_in_distinct_revisions_contributes_twice(self):
            value = {"revisions": {"synthetic_r0": {"bindings": {"synthetic_b0": bucket(2, 2, 1, 1)}}, "synthetic_r1": {"bindings": {"synthetic_b0": bucket(5, 4, 3, 1)}}}}
            targets = [target("synthetic_r0", "synthetic_b0", "synthetic_case_0"), target("synthetic_r1", "synthetic_b0", "synthetic_case_1")]
            self.assertEqual(self.evaluate(value, targets), expected(7, 6, 4, 2, 0.666667))

        def test_uneven_duplicate_multiplicity_preserves_weighted_rate(self):
            value = {"revisions": {"synthetic_r0": {"bindings": {"synthetic_b0": bucket(10, 8, 6, 2), "synthetic_b1": bucket(9, 3, 1, 2)}}}}
            targets = [target("synthetic_r0", binding, f"synthetic_case_{i}") for i, binding in enumerate(("synthetic_b0", "synthetic_b0", "synthetic_b1", "synthetic_b0"))]
            self.assertEqual(self.evaluate(value, targets), expected(19, 11, 7, 4, 0.636364))

        def test_empty_targets_ignore_unselected_buckets(self):
            value = {"revisions": {"synthetic_r0": {"bindings": {"synthetic_b0": bucket(10, 8, 6, 2)}}}}
            self.assertEqual(self.evaluate(value, []), expected())

        def test_missing_and_nondictionary_lookup_values_keep_empty_result(self):
            values = [None, [], "synthetic_value", 0, {}, {"revisions": []}, {"revisions": {}}, {"revisions": {"synthetic_r0": []}}, {"revisions": {"synthetic_r0": {"bindings": []}}}, {"revisions": {"synthetic_r0": {"bindings": {}}}}, {"revisions": {"synthetic_r0": {"bindings": {"synthetic_b0": []}}}}]
            targets = [target("synthetic_r0", "synthetic_b0", "synthetic_case_0"), target("synthetic_missing", "synthetic_missing", "synthetic_case_1")]
            for value in values:
                with self.subTest(value=value):
                    self.assertEqual(self.evaluate(value, targets), expected())

        def test_explicit_blank_lookup_mapping_keeps_default_empty_pair(self):
            value = {"revisions": {"": {"bindings": {"": bucket(4, 3, 2, 1)}}}}
            targets = [{"case_label": "synthetic_case_0"}, target("", "", "synthetic_case_1"), target(None, None, "synthetic_case_2")]
            self.assertEqual(self.evaluate(value, targets), expected(4, 3, 2, 1, 0.666667))

        def test_latest_uses_timestamp_then_event_string_descending(self):
            a = {"observed_at": "synthetic_time_002", "ledger_event_id": "synthetic_event_001", "label": "synthetic_a"}
            b = {"observed_at": "synthetic_time_002", "ledger_event_id": "synthetic_event_002", "label": "synthetic_b"}
            c = {"observed_at": "synthetic_time_001", "ledger_event_id": "synthetic_event_999", "label": "synthetic_c"}
            value = {"revisions": {"synthetic_r0": {"bindings": {"synthetic_a": bucket(1, 1, 1, 0, a), "synthetic_b": bucket(1, 1, 1, 0, b), "synthetic_c": bucket(1, 1, 1, 0, c)}}}}
            targets = [target("synthetic_r0", key, f"synthetic_case_{i}") for i, key in enumerate(("synthetic_a", "synthetic_c", "synthetic_b"))]
            self.assertEqual(self.evaluate(value, targets), expected(3, 3, 3, 0, 1.0, b))

        def test_equal_latest_sort_keys_keep_first_distinct_pair(self):
            a = {"observed_at": "synthetic_time", "ledger_event_id": "synthetic_event", "label": "synthetic_a"}
            b = {"observed_at": "synthetic_time", "ledger_event_id": "synthetic_event", "label": "synthetic_b"}
            value = {"revisions": {"synthetic_r0": {"bindings": {"synthetic_a": bucket(1, 1, 1, 0, a), "synthetic_b": bucket(1, 1, 1, 0, b)}}}}
            for first, second, selected in (("synthetic_b", "synthetic_a", b), ("synthetic_a", "synthetic_b", a)):
                with self.subTest(first=first):
                    targets = [target("synthetic_r0", first, "synthetic_case_0"), target("synthetic_r0", second, "synthetic_case_1")]
                    self.assertEqual(self.evaluate(value, targets), expected(2, 2, 2, 0, 1.0, selected))

        def test_latest_is_shallow_copied_without_input_mutation(self):
            nested = {"label": "synthetic_nested"}
            latest = {"observed_at": "synthetic_time", "ledger_event_id": "synthetic_event", "metadata": nested}
            value = {"revisions": {"synthetic_r0": {"bindings": {"synthetic_b0": bucket(1, 0, 0, 0, latest)}}}}
            result = self.evaluate(value, [target("synthetic_r0", "synthetic_b0", "synthetic_case_0")])
            self.assertEqual(result, expected(1, 0, 0, 0, None, latest))
            self.assertIsNot(result["latest_observation"], latest)
            self.assertIs(result["latest_observation"]["metadata"], nested)
            result["latest_observation"]["new_label"] = "synthetic_output_only"
            self.assertNotIn("new_label", latest)

        def test_missing_counters_and_nondictionary_latest_keep_defaults(self):
            for binding in ({}, {"latest": []}, {"latest": "synthetic_latest"}, {"latest": {}}):
                with self.subTest(binding=binding):
                    value = {"revisions": {"synthetic_r0": {"bindings": {"synthetic_b0": binding}}}}
                    self.assertEqual(self.evaluate(value, [target("synthetic_r0", "synthetic_b0", "synthetic_case_0")]), expected())

        def test_unique_pair_keeps_six_decimal_rate(self):
            value = {"revisions": {"synthetic_r0": {"bindings": {"synthetic_b0": bucket(5, 3, 1, 2)}}}}
            self.assertEqual(self.evaluate(value, [target("synthetic_r0", "synthetic_b0", "synthetic_case_0")]), expected(5, 3, 1, 2, 0.333333))
    return DynamicTargetCountTests


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--expected-sha256", required=True)
    args = parser.parse_args()
    namespace, forbidden = extract_shell(args.source, args.expected_sha256)
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(aggregate_tests(namespace, forbidden))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    raise SystemExit(0 if result.wasSuccessful() else 1)


if __name__ == "__main__":
    main()
