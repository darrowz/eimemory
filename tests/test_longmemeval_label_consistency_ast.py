"""EA-075 finite source contracts, runnable directly with stdlib unittest.

No project module is imported or executed. A small allowlist of pure helper
definitions is AST-extracted for synthetic checks. Raw ID expressions are also
interpreted over literals; report and chunk metadata wiring is inspected as AST.
This does not test retrieval, stores, text extraction, or metric implementations.
"""
from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace
import unittest


SOURCE = Path(__file__).resolve().parents[1] / "eimemory/evaluation/longmemeval.py"
TREE = ast.parse(SOURCE.read_text(encoding="utf-8"))
FUNCTIONS = {node.name: node for node in TREE.body if isinstance(node, ast.FunctionDef)}


def shape(node):
    return ast.dump(node, include_attributes=False)


def expression(source):
    return ast.parse(source, mode="eval").body


def assigned(function, name):
    return next(node.value for node in ast.walk(FUNCTIONS[function])
                if isinstance(node, ast.Assign)
                and any(isinstance(target, ast.Name) and target.id == name for target in node.targets))


def literal_id(node, values):
    """Explicit small syntax allowlist for ID expressions; no eval/exec."""
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.Name):
        return values[node.id]
    if isinstance(node, ast.Subscript):
        return literal_id(node.value, values)[literal_id(node.slice, values)]
    if isinstance(node, ast.IfExp):
        branch = node.body if literal_id(node.test, values) else node.orelse
        return literal_id(branch, values)
    if isinstance(node, ast.Compare) and len(node.ops) == 1 and isinstance(node.ops[0], ast.Lt):
        return literal_id(node.left, values) < literal_id(node.comparators[0], values)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return literal_id(node.left, values) + literal_id(node.right, values)
    if isinstance(node, ast.JoinedStr):
        return "".join(str(literal_id(part.value, values)) if isinstance(part, ast.FormattedValue)
                       else literal_id(part, values) for part in node.values)
    if isinstance(node, ast.Call) and not node.keywords:
        if isinstance(node.func, ast.Name) and len(node.args) == 1:
            if node.func.id == "str":
                return str(literal_id(node.args[0], values))
            if node.func.id == "len":
                return len(literal_id(node.args[0], values))
        if isinstance(node.func, ast.Attribute) and node.func.attr == "strip" and not node.args:
            value = literal_id(node.func.value, values)
            assert isinstance(value, str)
            return value.strip()
    raise AssertionError(f"Unsupported literal ID syntax: {ast.dump(node)}")


class LongMemEvalLabelConsistencyAST(unittest.TestCase):
    def assert_expression(self, actual, wanted):
        self.assertEqual(shape(actual), shape(expression(wanted)))

    def test_raw_id_creation_normalizes_before_turn_derivation(self):
        session = assigned("_normalize_raw_case_haystack", "session_id")
        turn = assigned("_normalize_raw_case_haystack", "turn_id")
        self.assert_expression(session, 'str(raw_session_ids[session_index] if session_index < len(raw_session_ids) else f"{case_id}-session-{session_index + 1}").strip()')
        self.assert_expression(turn, 'f"{session_id}:m{message_index}"')
        for raw, expected in [(" s1 ", "s1"), ("\ts1\n", "s1"), ("s1", "s1"), ("s 1", "s 1"), (7, "7")]:
            with self.subTest(raw=raw):
                actual = literal_id(session, {"raw_session_ids": [raw], "session_index": 0, "case_id": "q1"})
                self.assertEqual(actual, expected)
                actual_turn = literal_id(turn, {"session_id": actual, "message_index": 2})
                self.assertEqual(actual_turn, f"{expected}:m2")
                # Mining's canonicalization must be a no-op for created IDs.
                self.assertEqual(actual.strip(), actual)
                self.assertEqual(actual_turn.strip(), actual_turn)

    def test_missing_raw_id_keeps_stable_positional_fallback(self):
        session = assigned("_normalize_raw_case_haystack", "session_id")
        self.assertEqual(literal_id(session, {"raw_session_ids": [], "session_index": 3, "case_id": "q1"}), "q1-session-4")

    def test_blank_raw_ids_follow_existing_empty_id_skip(self):
        session = assigned("_normalize_raw_case_haystack", "session_id")
        for raw in ("", " \t\n "):
            self.assertEqual(literal_id(session, {"raw_session_ids": [raw], "session_index": 0, "case_id": "q1"}), "")
        guard = next(node for node in ast.walk(FUNCTIONS["_normalize_raw_case_haystack"])
                     if isinstance(node, ast.If) and shape(node.test) == shape(expression("not session_id")))
        self.assertEqual(len(guard.body), 1)
        self.assertIsInstance(guard.body[0], ast.Continue)

    def test_mined_labels_and_session_payload_share_created_ids(self):
        fn = FUNCTIONS["_normalize_raw_case_haystack"]
        calls = {shape(node) for node in ast.walk(fn) if isinstance(node, ast.Call)}
        for wanted in ('_append_unique(evidence_session_ids, session_id)',
                       '_append_unique(evidence_turn_ids, turn_id)',
                       'sessions.append({"session_id": session_id, "turns": turns})',
                       'turns.append({"turn_id": turn_id, "messages": [{"role": speaker, "content": content}]})'):
            self.assertIn(shape(expression(wanted)), calls)
        self.assert_expression(assigned("_append_unique", "text"), 'str(value or "").strip()')

    def test_chunk_metadata_preserves_normalized_raw_ids_and_ordinary_fallbacks(self):
        self.assert_expression(assigned("_session_chunks", "session_id"), 'str(session.get("session_id") or session.get("id") or f"{case_id}-session-{session_index + 1}")')
        self.assert_expression(assigned("_session_chunks", "turn_id"), 'str(turn.get("turn_id") or turn.get("id") or f"{session_id}-turn-{turn_index + 1}")')
        payload = next(node for node in ast.walk(FUNCTIONS["_session_chunks"])
                       if isinstance(node, ast.Dict) and any(isinstance(key, ast.Constant) and key.value == "chunk_id" for key in node.keys))
        fields = {key.value: value for key, value in zip(payload.keys, payload.values)}
        self.assert_expression(fields["session_id"], 'session_id')
        self.assert_expression(fields["turn_id"], 'turn_ids[0] if turn_ids else ""')
        self.assert_expression(fields["turn_ids"], 'turn_ids')
        self.assert_expression(fields["chunk_id"], 'str(session.get("chunk_id") or f"{case_id}:{session_id}:0")')

    def test_hit_chunk_report_uses_effective_chunk_ground_truth(self):
        sample = assigned("run_longmemeval", "sample")
        fields = {key.value: value for key, value in zip(sample.keys, sample.values)}
        self.assert_expression(fields["hit_chunk_ids"], '_hit_ids(retrieved, expected_ids=sorted(_expected_ids(case, granularity="chunk")), key="chunk_id")')
        self.assert_expression(assigned("run_longmemeval", "expected_ids"), '_expected_ids(case, granularity=granularity)')
        self.assert_expression(fields["retrieval_recall_at_5"], 'recall_at_k(returned_ids, expected_ids, k=5)')

    def test_effective_chunk_labels_keep_explicit_priority_and_session_inference(self):
        fn = FUNCTIONS["_expected_ids"]
        explicit = next(node for node in fn.body if isinstance(node, ast.If) and isinstance(node.test, ast.Name))
        inferred = next(node for node in fn.body if isinstance(node, ast.If) and isinstance(node.test, ast.Compare))
        self.assert_expression(explicit.test, 'expected')
        self.assert_expression(explicit.body[0].value, 'expected')
        self.assertLess(fn.body.index(explicit), fn.body.index(inferred))
        self.assert_expression(inferred.test, 'granularity == "chunk"')
        self.assert_expression(inferred.body[0].value, '{str(chunk["chunk_id"]) for chunk in case["chunks"] if chunk["session_id"] in set(case["evidence_session_ids"])}')


class LongMemEvalPureHelperRegression(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        names = ("_normalize_raw_case_haystack", "_append_unique", "_expected_ids", "_hit_ids")
        definitions = [FUNCTIONS[name] for name in names]
        for definition in definitions:
            if any(isinstance(node, (ast.Import, ast.ImportFrom)) for node in ast.walk(definition)):
                raise AssertionError("Pure helper unexpectedly imports a dependency")
        # Annotations are postponed so no RecordEnvelope, Any, or project binding
        # is needed. Original module imports and top-level statements are absent.
        tree = ast.Module(body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0), *definitions], type_ignores=[])
        cls.helpers = {}
        exec(compile(ast.fix_missing_locations(tree), "<longmemeval-pure-helpers>", "exec"), cls.helpers)

    def test_padded_raw_ids_match_all_mined_labels(self):
        for raw_id in (" s1 ", "\ts1\n", "s1"):
            with self.subTest(raw_id=raw_id):
                sessions, session_ids, turn_ids = self.helpers["_normalize_raw_case_haystack"](
                    {"haystack_session_ids": [raw_id], "haystack_sessions": [[
                        {"content": "first", "has_answer": True},
                        {"content": ""},
                        {"content": "second", "has_answer": True},
                    ]]}, case_id="q1")
                self.assertEqual(session_ids, ["s1"])
                self.assertEqual(sessions[0]["session_id"], session_ids[0])
                self.assertEqual(turn_ids, ["s1:m0", "s1:m2"])
                self.assertEqual([turn["turn_id"] for turn in sessions[0]["turns"]], turn_ids)

    def test_stable_fallback_and_empty_skip(self):
        normalize = self.helpers["_normalize_raw_case_haystack"]
        message = {"content": "answer", "has_answer": True}
        sessions, session_ids, turn_ids = normalize({"haystack_sessions": [[message]]}, case_id="q1")
        self.assertEqual(sessions[0]["session_id"], "q1-session-1")
        self.assertEqual(session_ids, ["q1-session-1"])
        self.assertEqual(turn_ids, ["q1-session-1:m0"])
        for raw in ("", " \t "):
            self.assertEqual(normalize({"haystack_session_ids": [raw], "haystack_sessions": [[message]]}, case_id="q1"), ([], [], []))

    def test_inferred_chunk_labels_and_hits_agree(self):
        case = {"evidence_session_ids": ["s1"], "evidence_chunk_ids": [], "chunks": [
            {"session_id": "s1", "chunk_id": "q1:s1:0"},
            {"session_id": "s2", "chunk_id": "q1:s2:0"},
        ]}
        expected = self.helpers["_expected_ids"](case, granularity="chunk")
        records = [SimpleNamespace(content={"chunk_id": value}) for value in ("q1:s2:0", "q1:s1:0", "q1:s1:0")]
        self.assertEqual(expected, {"q1:s1:0"})
        self.assertEqual(self.helpers["_hit_ids"](records, expected_ids=sorted(expected), key="chunk_id"), ["q1:s1:0"])

    def test_explicit_chunk_labels_override_inference_and_empty_has_no_hits(self):
        case = {"evidence_session_ids": ["s1"], "evidence_chunk_ids": ["explicit"], "chunks": [{"session_id": "s1", "chunk_id": "inferred"}]}
        records = [SimpleNamespace(content={"chunk_id": value}) for value in ("inferred", "explicit")]
        expected = self.helpers["_expected_ids"](case, granularity="chunk")
        self.assertEqual(expected, {"explicit"})
        self.assertEqual(self.helpers["_hit_ids"](records, expected_ids=sorted(expected), key="chunk_id"), ["explicit"])
        case.update(evidence_session_ids=[], evidence_chunk_ids=[])
        expected = self.helpers["_expected_ids"](case, granularity="chunk")
        self.assertEqual(expected, set())
        self.assertEqual(self.helpers["_hit_ids"](records, expected_ids=sorted(expected), key="chunk_id"), [])


if __name__ == "__main__":
    unittest.main()
