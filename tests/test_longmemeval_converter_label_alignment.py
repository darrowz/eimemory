"""Offline label contract tests; converter is lazily loaded at test execution."""
import copy
import importlib.util
import json
from pathlib import Path
import unittest


class MemoryPath:
    """Only the text I/O surface used by convert; no dataset files involved."""
    def __init__(self, text=""):
        self.text = text

    def read_text(self, *, encoding):
        assert encoding == "utf-8"
        return self.text

    def write_text(self, text, *, encoding):
        assert encoding == "utf-8"
        self.text = text
        return len(text)


class LongMemEvalLabelAlignmentTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        source = Path(__file__).resolve().parents[1] / "scripts" / "convert_longmemeval_to_eimemory.py"
        cls.load_converter(source)

    @classmethod
    def load_converter(cls, source):
        spec = importlib.util.spec_from_file_location("label_contract_converter", source)
        cls.converter = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.converter)

    def case(self, **changes):
        case = {
            "haystack_sessions": [[{"role": "user", "content": "合成", "has_answer": False}]],
            "haystack_session_ids": ["s0"],
            "answer_session_ids": ["s0"],
            "question_id": "synthetic-q",
            "question": "Synthetic question?",
            "answer": "Synthetic answer",
        }
        case.update(changes)
        return case

    def convert(self, case, enabled=True):
        module = self.converter
        previous = module._USE_REAL_EVIDENCE
        module._USE_REAL_EVIDENCE = enabled
        output = MemoryPath()
        try:
            count = module.convert(MemoryPath(json.dumps([case], ensure_ascii=False)), output)
        finally:
            module._USE_REAL_EVIDENCE = previous
        result = json.loads(output.text)
        self.assertEqual(count, len(result["cases"]))
        return result["cases"][0]

    def test_answer_session_literal_matches_definition(self):
        out = self.convert(self.case(haystack_session_ids=[" s0 "], answer_session_ids=[" s0 "]))
        self.assertEqual(out["evidence_session_ids"], [out["haystack_sessions"][0]["session_id"]])
        self.assertEqual(out["evidence_session_ids"], [" s0 "])

    def test_top_session_literal_matches_definition(self):
        out = self.convert(self.case(haystack_session_ids=["\ts0\n"], answer_session_ids=[], evidence_session_ids=["\ts0\n"]))
        self.assertEqual(out["evidence_session_ids"], [out["haystack_sessions"][0]["session_id"]])

    def test_top_turn_literal_matches_definition(self):
        out = self.convert(self.case(haystack_session_ids=[" s0 "], answer_session_ids=[], evidence_turn_ids=[" s0 :m0"]))
        self.assertEqual(out["evidence_turn_ids"], [out["haystack_sessions"][0]["turns"][0]["turn_id"]])

    def test_distinct_whitespace_ids_do_not_merge(self):
        case = self.case(haystack_session_ids=["s0", " s0 "], answer_session_ids=["s0", " s0 ", "s0"], evidence_turn_ids=["s0:m0", " s0 :m0", "s0:m0"])
        case["haystack_sessions"] *= 2
        out = self.convert(case)
        self.assertEqual(out["evidence_session_ids"], ["s0", " s0 "])
        self.assertEqual(out["evidence_turn_ids"], ["s0:m0", " s0 :m0"])

    def test_whitespace_only_nonempty_id(self):
        out = self.convert(self.case(haystack_session_ids=[" \t"], answer_session_ids=[" \t"], evidence_turn_ids=[" \t:m0"]))
        self.assertEqual(out["evidence_session_ids"], [" \t"])
        self.assertEqual(out["evidence_turn_ids"], [" \t:m0"])

    def test_abstract_labels_literal_and_not_filtered_to_haystack(self):
        out = self.convert(self.case(answer_session_ids=[" missing_abs ", "missing_abs"], evidence_session_ids=[" missing_abs "]))
        self.assertEqual(out["evidence_session_ids"], [" missing_abs ", "missing_abs"])

    def test_nonmatching_top_turn_is_not_rewritten(self):
        out = self.convert(self.case(evidence_turn_ids=["s0:m0 ", " external:m9 "]))
        self.assertEqual(out["evidence_turn_ids"], ["s0:m0 ", " external:m9 "])

    def test_empty_string_labels_still_ignored(self):
        out = self.convert(self.case(answer_session_ids=[""], evidence_session_ids=[""], evidence_turn_ids=[""]))
        self.assertEqual(out["evidence_session_ids"], [])
        self.assertEqual(out["evidence_turn_ids"], [])

    def test_clean_ids_and_union_order(self):
        out = self.convert(self.case(answer_session_ids=["abs", "s0", "abs"], evidence_session_ids=["other", "s0"], evidence_turn_ids=["s0:m0", "other:m1", "s0:m0"]))
        self.assertEqual(out["evidence_session_ids"], ["abs", "s0", "other"])
        self.assertEqual(out["evidence_turn_ids"], ["s0:m0", "other:m1"])

    def test_per_message_literals_deduplicate_top_labels(self):
        case = self.case(haystack_session_ids=[" s0 "], answer_session_ids=[" s0 "], evidence_turn_ids=[" s0 :m0"])
        case["haystack_sessions"][0][0]["has_answer"] = True
        out = self.convert(case)
        self.assertEqual(out["evidence_session_ids"], [" s0 "])
        self.assertEqual(out["evidence_turn_ids"], [" s0 :m0"])
        self.assertEqual(out["meta"]["evidence_turn_id_count"], 1)

    def test_per_message_only_unchanged(self):
        case = self.case(haystack_session_ids=[" s0 "], answer_session_ids=[])
        case["haystack_sessions"][0][0]["has_answer"] = True
        out = self.convert(case)
        self.assertEqual(out["evidence_session_ids"], [" s0 "])
        self.assertEqual(out["evidence_turn_ids"], [" s0 :m0"])

    def test_fallback_session_and_message_positions_unchanged(self):
        case = self.case(haystack_session_ids=[], answer_session_ids=[], haystack_sessions=[{}, [None, {}, {"text": "synthetic", "has_answer": True}]])
        out = self.convert(case)
        self.assertEqual(out["haystack_sessions"][0]["session_id"], "s1")
        self.assertEqual(out["evidence_session_ids"], ["s1"])
        self.assertEqual(out["evidence_turn_ids"], ["s1:m2"])
        self.assertEqual(out["haystack_sessions"][0]["turns"][0]["turn_id"], "s1:m2")

    def test_flag_off_historical_behavior_unchanged(self):
        out = self.convert(self.case(answer_session_ids=[" s0 ", " s0 ", ""], evidence_session_ids=["other"], evidence_turn_ids=["s0:m0"]), enabled=False)
        self.assertEqual(out["evidence_session_ids"], [" s0 ", " s0 ", ""])
        self.assertEqual(out["evidence_turn_ids"], [])
        self.assertFalse(out["meta"]["evidence_mining_enabled"])

    def test_duplicate_definitions_not_repaired(self):
        case = self.case(haystack_session_ids=["s0", "s0"], answer_session_ids=["s0", "s0"])
        case["haystack_sessions"] *= 2
        out = self.convert(case)
        self.assertEqual(len(out["haystack_sessions"]), 2)
        self.assertEqual(out["evidence_session_ids"], ["s0"])

    def test_empty_flagged_content_and_truthiness_unchanged(self):
        case = self.case(answer_session_ids=[], haystack_sessions=[[{"content": "", "has_answer": True}, {"content": "synthetic", "has_answer": "false"}]])
        self.assertEqual(self.convert(case)["evidence_turn_ids"], ["s0:m1"])

    def test_empty_definition_behavior_unchanged(self):
        out = self.convert(self.case(haystack_session_ids=[""], answer_session_ids=[""]))
        self.assertEqual(out["haystack_sessions"][0]["session_id"], "")
        self.assertEqual(out["evidence_session_ids"], [])

    def test_metadata_and_input_unchanged(self):
        case = self.case(haystack_session_ids=[" s0 "], haystack_dates=["date"])
        before = copy.deepcopy(case)
        out = self.convert(case)
        self.assertEqual(case, before)
        self.assertEqual(out["meta"]["haystack_session_ids"], [" s0 "])
        self.assertEqual(out["haystack_sessions"][0]["session_date"], "date")
        self.assertEqual(out["evidence_chunk_ids"], [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
