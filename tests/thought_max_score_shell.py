"""AST-only existing-record value merge with capture-only DTO dependencies."""
from __future__ import annotations

import argparse
import ast
import builtins
import copy
import hashlib
from pathlib import Path
import unittest

BASELINE_SHA256 = "5f44a93dc898107ddf0c0fe6c7b5c2adb2ff55d21f53fc48e0216a1083808a6a"
MISSING = object()
STATUS = "synthetic_existing_status"


def extract_shell(path, expected):
    data = path.read_bytes()
    actual = hashlib.sha256(data).hexdigest()
    if actual != expected:
        raise ValueError(f"Pinned source digest mismatch: {actual}")
    module = ast.parse(data, filename=str(path))
    functions = [n for n in module.body if isinstance(n, ast.FunctionDef) and n.name == "_upsert_thought"]
    if len(functions) != 1 or functions[0].decorator_list:
        raise ValueError("Expected one complete undecorated function")
    function = functions[0]
    if actual == BASELINE_SHA256 and (function.lineno, function.end_lineno) != (335, 384):
        raise ValueError("Unexpected baseline boundary")
    forbidden = []
    def fenced_import(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "__future__" and level == 0:
            return builtins.__import__(name, globals, locals, fromlist, level)
        forbidden.append(name)
        raise AssertionError("Target imports, generation, scoring and persistence are forbidden")
    namespace = {"__builtins__": {**vars(builtins), "__import__": fenced_import}}
    isolated = ast.fix_missing_locations(ast.Module(body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0), copy.deepcopy(function)], type_ignores=[]))
    exec(compile(isolated, str(path), "exec"), namespace)
    print(f"SOURCE_SHA256 {actual}")
    print(f"EXTRACTED _upsert_thought L{function.lineno}-{function.end_lineno}; existing-record branch only")
    return namespace, forbidden


def score_tests(namespace, forbidden):
    class ThoughtMaxScoreTests(unittest.TestCase):
        def setUp(self):
            self.cases = []

        def tearDown(self):
            self.assertEqual(forbidden, [])
            self.assertEqual(namespace["THOUGHT_STATUSES"], {STATUS})
            for case in self.cases:
                self.assertEqual(case["forbidden"], [])
                self.assertEqual(case["scored"], case["scored_before"])
                self.assertEqual(case["thought"], case["thought_before"])
                self.assertIs(case["scored"]["source_record_ids"], case["scored_sources"])
                self.assertEqual(case["scored_sources"], [])
                self.assertIs(case["scored"]["incoming_payload"], case["incoming"])

        def make_case(self, old=MISSING, new=MISSING, *, rewrite_error=None, content_repeat=3, meta_repeat=3):
            for score in (old, new):
                if score is not MISSING:
                    self.assertIn(type(score), (int, float))
                    self.assertGreaterEqual(score, 0)
                    self.assertLess(score, float("inf"))
            case = {"events": [], "forbidden": [], "scope": object(), "loop": object(), "key": object(), "result": object(), "incoming": object(), "prior": object(), "outer": object(), "meta_marker": object(), "touches": 0, "rewrites": 0}
            thought = {"target_capability": object(), "question": object()}
            scored = {"repeat_count": 2, "source_record_ids": [], "incoming_payload": case["incoming"]}
            current = {"source_record_ids": [], "prior_payload": case["prior"]}
            if content_repeat is not MISSING:
                current["repeat_count"] = content_repeat
            if old is not MISSING:
                current["score"] = old
            if new is not MISSING:
                scored["score"] = new
            case.update(thought=thought, thought_before=dict(thought), scored=scored, scored_before=dict(scored), scored_sources=scored["source_record_ids"], expected_repeat=(meta_repeat if content_repeat is MISSING else content_repeat) + 2)
            def denied(label):
                case["forbidden"].append(label)
                raise AssertionError(f"Forbidden dependency: {label}")
            class FakeExisting:
                __slots__ = ("content", "meta", "status")
                def __init__(self):
                    self.content = {"thought": current, "outer_payload": case["outer"]}
                    self.meta = {"repeat_count": meta_repeat, "meta_payload": case["meta_marker"]}
                    self.status = STATUS
                def touch(self):
                    case["events"].append("touch")
                    case["touches"] += 1
                def __getattr__(self, name):
                    return denied("existing attribute:" + name)
            existing = FakeExisting()
            class FakeStore:
                __slots__ = ()
                def rewrite(_, record):
                    case["events"].append("rewrite")
                    case["rewrites"] += 1
                    self.assertIs(record, existing)
                    self.assertEqual(case["touches"], 1)
                    if rewrite_error is not None:
                        raise rewrite_error
                    return case["result"]
                def __getattr__(_, name):
                    return denied("store attribute:" + name)
            class FakeRuntime:
                __slots__ = ()
                store = FakeStore()
                def __getattr__(_, name):
                    return denied("runtime attribute:" + name)
            runtime = FakeRuntime()
            def semantic(kind, target_value, question_value):
                case["events"].append("semantic")
                self.assertEqual(kind, "thought")
                self.assertIs(target_value, thought["target_capability"])
                self.assertIs(question_value, thought["question"])
                return case["key"]
            def find(found_runtime, *, scope, semantic_key):
                case["events"].append("find")
                self.assertIs(found_runtime, runtime)
                self.assertIs(scope, case["scope"])
                self.assertIs(semantic_key, case["key"])
                return existing
            def fake_scored(incoming):
                case["events"].append("scored")
                self.assertIs(incoming, thought)
                return scored
            namespace.update(stable_semantic_key=semantic, _find_by_semantic_key=find, _scored_thought=fake_scored, THOUGHT_STATUSES={STATUS}, append_learning_record_once=lambda *args, **kwargs: denied("new-record append"))
            case.update(existing=existing, runtime=runtime)
            self.cases.append(case)
            return case

        def exercise(self, case):
            result = None
            caught = None
            try:
                result = namespace["_upsert_thought"](case["runtime"], case["thought"], scope=case["scope"], loop_id=case["loop"])
            except Exception as error:
                caught = error
            self.assertEqual(case["events"], ["semantic", "find", "scored", "touch", "rewrite"])
            self.assertEqual(case["touches"], 1)
            self.assertEqual(case["rewrites"], 1)
            self.assertEqual(case["existing"].status, STATUS)
            return result, caught

        def assert_merge(self, case, score):
            record = case["existing"]
            content = record.content["thought"]
            self.assertEqual(content["score"], score)
            self.assertEqual(record.meta["score"], score)
            self.assertEqual(content["repeat_count"], case["expected_repeat"])
            self.assertEqual(record.meta["repeat_count"], case["expected_repeat"])
            self.assertEqual(content["source_record_ids"], [])
            self.assertEqual(record.meta["source_type"], "thought")
            self.assertIs(content["prior_payload"], case["prior"])
            self.assertIs(content["incoming_payload"], case["incoming"])
            self.assertIs(record.content["outer_payload"], case["outer"])
            self.assertIs(record.meta["meta_payload"], case["meta_marker"])
            self.assertNotIn("source_goal_id", record.meta)
            self.assertNotIn("thought_id", record.meta)

        def successful(self, case, score):
            result, error = self.exercise(case)
            self.assertIsNone(error)
            self.assertIs(result, case["result"])
            self.assert_merge(case, score)

        def test_lower_supplied_score_preserves_existing_maximum(self):
            self.successful(self.make_case(0.9, 0.2), 0.9)

        def test_higher_supplied_score_keeps_existing_max_rule(self):
            self.successful(self.make_case(0.2, 0.9), 0.9)

        def test_equal_scores_remain_equal(self):
            self.successful(self.make_case(0.6, 0.6), 0.6)

        def test_zero_supplied_score_does_not_reduce_positive_old_score(self):
            self.successful(self.make_case(0.9, 0), 0.9)

        def test_zero_pair_remains_zero(self):
            self.successful(self.make_case(0, 0), 0.0)

        def test_missing_old_score_uses_existing_default_zero(self):
            self.successful(self.make_case(new=0.4), 0.4)

        def test_missing_new_score_preserves_existing_score(self):
            self.successful(self.make_case(old=0.7), 0.7)

        def test_both_missing_scores_use_existing_default_zero(self):
            self.successful(self.make_case(), 0.0)

        def test_repeat_meta_fallback_payload_status_touch_and_return_are_preserved(self):
            case = self.make_case(0.8, 0.3, content_repeat=MISSING, meta_repeat=4)
            self.successful(case, 0.8)
            self.assertEqual(case["existing"].meta["repeat_count"], 6)

        def test_fake_rewrite_failure_keeps_exception_identity(self):
            failure = RuntimeError("synthetic rewrite failure")
            case = self.make_case(0.1, 0.4, rewrite_error=failure)
            result, error = self.exercise(case)
            self.assertIsNone(result)
            self.assertIs(error, failure)
            self.assert_merge(case, 0.4)
    return ThoughtMaxScoreTests


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--expected-sha256", required=True)
    args = parser.parse_args()
    namespace, forbidden = extract_shell(args.source, args.expected_sha256)
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(score_tests(namespace, forbidden))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    raise SystemExit(0 if result.wasSuccessful() else 1)


if __name__ == "__main__":
    main()
