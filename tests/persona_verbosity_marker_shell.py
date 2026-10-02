"""Exact AST ordinary-text classification with capture-only events and restricted matching."""
from __future__ import annotations

import argparse
import ast
import builtins
import copy
import hashlib
from pathlib import Path
import unittest

BASELINE_SHA256 = "b8ff5e17f70eb1eab36fa4a98db64eaff56dde2d486b263cd6e9da85a8829129"
ENTRY = ("too verbose", "too wordy", "good reply", "nice reply", "wrong", "incorrect", "太啰嗦", "太罗嗦", "别演", "废话", "短一点", "直接说", "不错", "很好", "这样就对", "不对", "错了", "弄错", "查证据")
VERBOSITY = ("戏很多", "别演", "废话", "短一点", "直接说", "少说", "太啰嗦", "太罗嗦")
REINFORCEMENT = ("不错", "很好", "这样就对", "good reply", "nice reply")
ENGLISH = ("too verbose", "too wordy")


class FakeEvent:
    calls = []
    def __init__(self, **fields):
        if set(fields) != {"raw_text", "category", "severity", "trait_delta", "rule_candidate"}:
            raise AssertionError("Unexpected fake event field")
        if fields["category"] not in {"verbosity", "reinforcement", "tone"}:
            raise AssertionError("Only ordinary display classification may construct a fake event")
        self.fields = fields
        type(self).calls.append(self)
    def __getattr__(self, name):
        if name not in self.fields:
            raise AssertionError(f"Unexpected fake event access {name}")
        return self.fields[name]


def extract_shell(path, expected):
    data = path.read_bytes(); actual = hashlib.sha256(data).hexdigest()
    if actual != expected:
        raise ValueError(f"Pinned source digest mismatch: {actual}")
    module = ast.parse(data, filename=str(path))
    constants = [n for n in module.body if isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name) and n.target.id == "PERSONA_FEEDBACK_MARKERS"]
    if len(constants) != 1:
        raise ValueError("Expected one literal marker tuple")
    entry = ast.literal_eval(constants[0].value)
    if entry != ENTRY:
        raise ValueError("Entry vocabulary must stay unchanged")
    selected = []
    for name, span in [("persona_feedback_from_user_text", (29, 36)), ("correction_from_user_text", (39, 104))]:
        matches = [n for n in module.body if isinstance(n, ast.FunctionDef) and n.name == name]
        if len(matches) != 1 or matches[0].decorator_list:
            raise ValueError("Expected one complete undecorated function")
        node = matches[0]
        if actual == BASELINE_SHA256 and (node.lineno, node.end_lineno) != span:
            raise ValueError("Baseline function boundary mismatch")
        selected.append(copy.deepcopy(node))
        print(f"EXTRACTED {name} L{node.lineno}-{node.end_lineno}")
    control = {"forbidden": [], "matched": [], "inert_checks": 0}
    def fenced_import(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "__future__" and level == 0:
            return builtins.__import__(name, globals, locals, fromlist, level)
        control["forbidden"].append(name)
        raise AssertionError("No target/schema/helper import may execute")
    def restricted_match(text, markers):
        if markers in (entry, VERBOSITY, VERBOSITY + ENGLISH, REINFORCEMENT):
            control["matched"].append((text, markers))
            return any(marker in text for marker in markers)
        # Other category policies are not evaluated, even though their calls
        # remain in the extracted function's ordinary control-flow skeleton.
        control["inert_checks"] += 1
        return False
    namespace = {"__builtins__": {**vars(builtins), "__import__": fenced_import}, "PERSONA_FEEDBACK_MARKERS": entry, "PersonaCorrectionEvent": FakeEvent, "_has_any": restricted_match}
    isolated = ast.fix_missing_locations(ast.Module(body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0), *selected], type_ignores=[]))
    exec(compile(isolated, str(path), "exec"), namespace)
    print(f"SOURCE_SHA256 {actual}")
    return namespace, control


def marker_tests(namespace, control):
    class PersonaVerbosityMarkerTests(unittest.TestCase):
        def setUp(self):
            FakeEvent.calls.clear(); control["matched"].clear(); control["inert_checks"] = 0
        def tearDown(self):
            self.assertEqual(control["forbidden"], [])
            self.assertEqual(namespace["PERSONA_FEEDBACK_MARKERS"], ENTRY)
        def detector(self, text):
            return namespace["persona_feedback_from_user_text"](text)
        def classifier(self, text):
            return namespace["correction_from_user_text"](text)
        def assert_verbosity(self, event, text):
            self.assertIsNotNone(event)
            self.assertEqual(event.fields, {"raw_text": text.strip(), "category": "verbosity", "severity": 0.85, "trait_delta": {"verbosity": -0.15, "humor": -0.05, "latency_priority": 0.08}, "rule_candidate": "When the user says the agent is overacting or verbose, answer direct result first."})
        def test_advertised_english_markers_classify_through_entry(self):
            for marker in ENGLISH:
                with self.subTest(marker=marker):
                    self.assert_verbosity(self.detector(marker), marker)
        def test_english_markers_classify_directly(self):
            for marker in ENGLISH:
                with self.subTest(marker=marker):
                    self.assert_verbosity(self.classifier(marker), marker)
        def test_case_and_whitespace_normalization_are_unchanged(self):
            for text in ("  TOO VERBOSE  ", "\nToo Wordy\t"):
                with self.subTest(text=text):
                    self.assert_verbosity(self.detector(text), text)
        def test_ordinary_sentences_with_either_phrase_match(self):
            for text in ("This reply is too verbose for my question.", "The answer feels too wordy today."):
                with self.subTest(text=text):
                    self.assert_verbosity(self.detector(text), text)
        def test_existing_chinese_verbosity_controls_are_unchanged(self):
            for marker in VERBOSITY:
                with self.subTest(marker=marker):
                    self.assert_verbosity(self.classifier(marker), marker)
                    if marker in ENTRY:
                        self.assert_verbosity(self.detector(marker), marker)
        def test_empty_and_unmatched_detector_inputs_construct_nothing(self):
            for text in ("", " \n\t ", "a neutral ordinary sentence"):
                with self.subTest(text=text):
                    self.assertIsNone(self.detector(text))
            self.assertEqual(FakeEvent.calls, [])
        def test_reinforcement_controls_keep_existing_fields(self):
            for marker in REINFORCEMENT:
                with self.subTest(marker=marker):
                    event = self.detector(marker)
                    self.assertEqual(event.fields, {"raw_text": marker, "category": "reinforcement", "severity": 0.55, "trait_delta": {"precision": 0.03, "empathy": 0.02}, "rule_candidate": "Keep the current reply style when the user explicitly says it worked well."})
        def test_direct_neutral_control_keeps_tone_shape(self):
            event = self.classifier("  an ordinary neutral note  ")
            self.assertEqual(event.fields, {"raw_text": "an ordinary neutral note", "category": "tone", "severity": 0.4, "trait_delta": {"empathy": 0.03}, "rule_candidate": "Keep tone grounded and adapt to the user's correction."})
        def test_ordinary_verbosity_precedes_reinforcement(self):
            for text in ("短一点 good reply", "too verbose good reply", "nice reply but too wordy"):
                with self.subTest(text=text):
                    self.assert_verbosity(self.detector(text), text)
        def test_entry_tuple_and_restricted_matcher_boundary_are_stable(self):
            self.detector("good reply")
            self.assertEqual(namespace["PERSONA_FEEDBACK_MARKERS"], ENTRY)
            self.assertTrue(control["inert_checks"])
            for _text, markers in control["matched"]:
                self.assertIn(markers, (ENTRY, VERBOSITY, VERBOSITY + ENGLISH, REINFORCEMENT))
            self.assertEqual({event.category for event in FakeEvent.calls}, {"reinforcement"})
    return PersonaVerbosityMarkerTests


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--expected-sha256", required=True)
    args = parser.parse_args()
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(marker_tests(*extract_shell(args.source, args.expected_sha256)))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
