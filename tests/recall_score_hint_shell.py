"""AST-only optional-hint selection with inert score values and fully fake helpers."""
from __future__ import annotations

import argparse
import ast
import builtins
import copy
import hashlib
from pathlib import Path
import unittest

BASELINE_SHA256 = "0b1d526d060267398084172062b06ad65dc162ead785a82ce734b508d4604de2"
REQUIRED = ("confidence", "salience", "freshness", "provenance", "reuse", "risk_penalty")
WEIGHTS = {"relevance": 0.4, "confidence": 0.1, "salience": 0.1, "freshness": 0.1, "provenance": 0.1, "reuse": 0.1, "risk_penalty": 0.2}


class Fields:
    """Capture-only DTO replacement; every unspecified attribute is forbidden."""
    def __init__(self, **values):
        self._values = values
    def __getattr__(self, name):
        if name not in self._values:
            raise AssertionError(f"Unexpected fake field {name}")
        return self._values[name]


def extract_shell(path, expected):
    data = path.read_bytes(); actual = hashlib.sha256(data).hexdigest()
    if actual != expected:
        raise ValueError(f"Pinned source digest mismatch: {actual}")
    module = ast.parse(data, filename=str(path))
    found = [n for n in module.body if isinstance(n, ast.FunctionDef) and n.name == "evaluate_recall_score"]
    if len(found) != 1 or found[0].decorator_list:
        raise ValueError("Expected one complete undecorated function")
    method = found[0]
    if actual == BASELINE_SHA256 and (method.lineno, method.end_lineno) != (311, 410):
        raise ValueError("Baseline function range mismatch")
    forbidden = []
    def fenced_import(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "__future__" and level == 0:
            return builtins.__import__(name, globals, locals, fromlist, level)
        forbidden.append(name)
        raise AssertionError("Target and helper imports are forbidden")
    namespace = {"__builtins__": {**vars(builtins), "__import__": fenced_import}}
    isolated = ast.fix_missing_locations(ast.Module(body=[
        ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0),
        copy.deepcopy(method),
    ], type_ignores=[]))
    exec(compile(isolated, str(path), "exec"), namespace)
    print(f"SOURCE_SHA256 {actual}")
    print(f"EXTRACTED evaluate_recall_score L{method.lineno}-{method.end_lineno}")
    return namespace, forbidden


def hint_tests(namespace, forbidden):
    class RecallScoreHintTests(unittest.TestCase):
        def setUp(self):
            self.meta = object()
            self.record = Fields(content={"text": "synthetic text", "memory_type": "unused"}, summary="synthetic summary", detail="synthetic detail", title="synthetic title", source="synthetic source", record_id="synthetic record", meta=self.meta)
            self.context = Fields(activity="synthetic activity", source="synthetic context", profile="synthetic profile")
            self.fallback_calls = []
            self.context_calls = []
            self.label_calls = []
            self.fallback = self.hint(values=[0.65, 0.55, 0.45, 0.35, 0.25, 0.15])
            def fake_context(**kwargs):
                self.assertEqual(set(kwargs), {"activity", "source"})
                self.context_calls.append(dict(kwargs))
                return Fields(**kwargs, profile="synthetic profile")
            def fake_weights(profile):
                self.assertEqual(profile, "synthetic profile")
                return dict(WEIGHTS)
            def fake_metadata(marker):
                self.assertIs(marker, self.meta)
                return {"memory_type": "synthetic type", "quality": {"neutral": 2}}
            def fake_fallback(**kwargs):
                self.fallback_calls.append(kwargs)
                return self.fallback
            def fake_terms(query):
                self.assertEqual(query, "synthetic query")
                return ["fake-one", "fake-two"]
            def fake_component(name, value, weight, evidence):
                return Fields(name=name, value=value, weight=weight, evidence=evidence)
            def fake_labels(**kwargs):
                self.assertEqual(set(kwargs), {"components", "source", "tier", "memory_type", "activity"})
                self.label_calls.append(kwargs)
                return ["synthetic label"]
            namespace.update(ScoreContext=fake_context, weights_for_profile=fake_weights, business_metadata=fake_metadata, evaluate_memory_score=fake_fallback, _normalized_terms=fake_terms, ScoreComponent=fake_component, clamp_score=lambda value: float(value), tier_for_score=lambda _value: "synthetic tier", component_labels=fake_labels, _score_activity=lambda context: context.activity, now_iso=lambda: "synthetic time", ScoreProvenance=lambda **kwargs: Fields(**kwargs), MemoryScore=lambda **kwargs: Fields(**kwargs))
        def tearDown(self):
            self.assertEqual(forbidden, [])
        def hint(self, values=None):
            numbers = values if values is not None else [0.1, 0.2, 0.3, 0.4, 0.5, 0.6]
            components = {name: Fields(value=value, evidence={"labels": [], "opaque": object()}) for name, value in zip(REQUIRED, numbers)}
            return Fields(components=components, tier="synthetic stored tier", final_score=0.75)
        def call(self, hint, *, context=True):
            return namespace["evaluate_recall_score"](record=self.record, query="synthetic query", lexical_score=1.0, semantic_score=0.2, vector_score=0.4, context=self.context if context else None, stored_score=hint)
        def assert_selected(self, result, hint):
            self.assertEqual(set(result.components), set(REQUIRED) | {"relevance"})
            for name in REQUIRED:
                self.assertEqual(result.components[name].value, hint.components[name].value)
                self.assertIs(result.components[name].evidence, hint.components[name].evidence)
                self.assertEqual(result.components[name].weight, WEIGHTS[name])
            self.assertEqual(result.explanation["stored_tier"], hint.tier)
            self.assertEqual(result.explanation["stored_final_score"], hint.final_score)
        def assert_unchanged(self, hint, before):
            self.assertEqual(set(hint.components), set(before))
            for name, component in before.items():
                self.assertIs(hint.components[name], component)
        def test_each_required_missing_key_uses_one_whole_hint_fallback(self):
            for missing in REQUIRED:
                with self.subTest(missing=missing):
                    self.fallback_calls.clear()
                    hint = self.hint(); del hint.components[missing]
                    before = dict(hint.components)
                    result = self.call(hint)
                    self.assertEqual(len(self.fallback_calls), 1)
                    self.assert_selected(result, self.fallback)
                    self.assert_unchanged(hint, before)
        def test_empty_hint_uses_fallback_without_mutating_input(self):
            hint = self.hint(); hint.components.clear()
            result = self.call(hint)
            self.assertEqual(len(self.fallback_calls), 1)
            self.assert_selected(result, self.fallback)
            self.assertEqual(hint.components, {})
        def test_multiple_missing_keys_use_same_fallback(self):
            hint = self.hint(); del hint.components["salience"]; del hint.components["risk_penalty"]
            before = dict(hint.components)
            result = self.call(hint)
            self.assertEqual(len(self.fallback_calls), 1)
            self.assert_selected(result, self.fallback)
            self.assert_unchanged(hint, before)
        def test_none_keeps_existing_fallback_argument_shape(self):
            result = self.call(None)
            self.assert_selected(result, self.fallback)
            self.assertEqual(len(self.fallback_calls), 1)
            arguments = dict(self.fallback_calls[0]); context = arguments.pop("context")
            self.assertEqual(arguments, {"text": "synthetic text", "title": "synthetic title", "memory_type": "synthetic type", "source": "synthetic source", "legacy_quality": {"neutral": 2}})
            self.assertEqual(context.activity, "record.create")
            self.assertEqual(context.source, "synthetic source")
        def test_six_components_without_relevance_are_complete(self):
            hint = self.hint(); before = dict(hint.components)
            result = self.call(hint)
            self.assertEqual(self.fallback_calls, [])
            self.assert_selected(result, hint)
            self.assert_unchanged(hint, before)
            self.assertAlmostEqual(result.components["relevance"].value, 0.365)
        def test_complete_zero_values_remain_present(self):
            hint = self.hint(values=[0.0] * 6)
            result = self.call(hint)
            self.assertEqual(self.fallback_calls, [])
            self.assert_selected(result, hint)
            self.assertAlmostEqual(result.final_score, 0.146)
        def test_optional_extra_components_are_ignored(self):
            hint = self.hint(); extra = object()
            hint.components["relevance"] = extra; hint.components["unknown"] = extra
            before = dict(hint.components)
            result = self.call(hint)
            self.assertEqual(self.fallback_calls, [])
            self.assert_selected(result, hint)
            self.assert_unchanged(hint, before)
            self.assertNotIn("unknown", result.components)
            self.assertIsNot(result.components["relevance"], extra)
        def test_complete_hint_preserves_deterministic_result_fields(self):
            hint = self.hint(); before = dict(hint.components)
            result = self.call(hint)
            self.assert_selected(result, hint)
            self.assert_unchanged(hint, before)
            self.assertEqual(self.fallback_calls, [])
            self.assertAlmostEqual(result.final_score, 0.176)
            self.assertEqual(result.tier, "synthetic tier")
            self.assertEqual(result.labels, ["synthetic label"])
            self.assertEqual(result.explanation["formula"], {"base_score": 0.296, "risk_multiplier": 0.2, "final_score": result.final_score})
            self.assertEqual(result.provenance.generated_at, "synthetic time")
            self.assertIs(self.label_calls[0]["components"], result.components)
        def test_absent_context_keeps_existing_context_factory(self):
            result = self.call(self.hint(), context=False)
            self.assertEqual(self.context_calls, [{"activity": "sqlite.recall", "source": "sqlite.recall"}])
            self.assertEqual(self.fallback_calls, [])
            self.assertEqual(result.provenance.source, "sqlite.recall")
    return RecallScoreHintTests


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--expected-sha256", required=True)
    args = parser.parse_args()
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(hint_tests(*extract_shell(args.source, args.expected_sha256)))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
