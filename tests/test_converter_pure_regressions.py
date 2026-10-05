"""Converter regressions using only reviewed pure AST and invented JSON.

Run directly with Python (stdlib only). Neither source module is imported;
source file IO, convert(), main(), CLI and real datasets are never executed.
"""
import ast
import builtins
import copy
import json
from pathlib import Path
import unittest


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"


def pure_transform(filename, mine=True):
    """Extract the reviewed in-memory block, explicitly excluding source IO."""
    module = ast.parse((SCRIPTS / filename).read_text(encoding="utf-8"))
    original = next(n for n in module.body if isinstance(n, ast.FunctionDef) and n.name == "convert")
    assert ast.unparse(original.body[0]) == "raw = json.loads(raw_path.read_text(encoding='utf-8'))"
    assert ast.unparse(original.body[-2]) == "out_path.write_text(json.dumps(out, ensure_ascii=False), encoding='utf-8')"
    assert ast.unparse(original.body[-1]) in ("return len(cases)", "return (len(raw), len(cases))")
    transform = copy.deepcopy(original)
    transform.name = "transform"
    transform.args = ast.arguments(posonlyargs=[], args=[ast.arg(arg="raw")], vararg=None,
                                   kwonlyargs=[], kw_defaults=[], kwarg=None, defaults=[])
    transform.returns = None
    transform.body = transform.body[1:-2] + [ast.Return(value=ast.Name(id="out", ctx=ast.Load()))]
    selected = [transform]
    if filename == "convert_longmemeval_to_eimemory.py":
        helper = copy.deepcopy(next(n for n in module.body if isinstance(n, ast.FunctionDef)
                                    and n.name == "_extract_real_evidence"))
        helper.returns = None
        for arg in helper.args.args + helper.args.kwonlyargs:
            arg.annotation = None
        selected.insert(0, helper)
    names = {"enumerate", "sorted", "int", "isinstance", "list", "str", "len", "_extract_real_evidence"}
    methods = {"keys", "startswith", "endswith", "split", "get", "append", "strip"}
    tree = ast.fix_missing_locations(ast.Module(body=selected, type_ignores=[]))
    for node in ast.walk(tree):
        assert not isinstance(node, (ast.Import, ast.ImportFrom, ast.With, ast.AsyncWith,
                                     ast.Await, ast.Global, ast.Nonlocal))
        if isinstance(node, ast.Call):
            assert ((isinstance(node.func, ast.Name) and node.func.id in names) or
                    (isinstance(node.func, ast.Attribute) and node.func.attr in methods)), ast.dump(node)
    env = {"__builtins__": {n: getattr(builtins, n) for n in names | {"dict", "bool", "float"}
                            if hasattr(builtins, n)}, "_USE_REAL_EVIDENCE": mine}
    exec(compile(tree, "<reviewed-pure-converter-fragments>", "exec"), env)
    return lambda raw: env["transform"](json.loads(json.dumps(raw, ensure_ascii=False, allow_nan=False)))


def message(content="", marked=True, **extra):
    return dict({"content": content, "has_answer": marked, "role": "user"}, **extra)


def lme_case(sessions, ids=None, **extra):
    return dict({"question_id": "q1", "question": "Question?", "answer": "  零\nFalse  ",
                 "question_type": "test", "question_date": "2026-10-05",
                 "haystack_sessions": sessions, "haystack_session_ids": ["s0"] if ids is None else ids,
                 "haystack_dates": ["2026-10-01"]}, **extra)


class ConverterPureRegressions(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.locomo = staticmethod(pure_transform("convert_locomo_to_eimemory.py"))
        cls.lme = staticmethod(pure_transform("convert_longmemeval_to_eimemory.py"))
        cls.lme_off = staticmethod(pure_transform("convert_longmemeval_to_eimemory.py", mine=False))

    def test_locomo_scalar_answers_and_preserved_fields(self):
        raw = [{"conversation": {"speaker_a": "Alice", "session_1_date_time": "original date",
                "session_1": [{"dia_id": "D1:1", "text": "café 中文"}],
                "session_2": [{"dia_id": "D2:1", "text": ""}]},
                "qa": [{"question": "Count?", "evidence": ["D1:1", "D2:1", "external"], "category": 1}]}]
        baseline = self.locomo(raw)
        for value in (0, 0.0, -0.0, False, None, "", True, 12, -7, 1.25, "0", " \n ", "零", [], {}, [0, False], {"n": 0}):
            with self.subTest(value=value, type=type(value).__name__):
                data = copy.deepcopy(raw)
                data[0]["qa"][0]["answer"] = value
                actual = self.locomo(data)
                # Scalar zero/False must survive; other historical coercions remain.
                expected = str(value) if isinstance(value, (int, float, bool)) else str(value or "")
                self.assertEqual(actual["cases"][0]["expected_answer"], expected)
                actual["cases"][0]["expected_answer"] = ""
                self.assertEqual(actual, baseline)
        case = baseline["cases"][0]
        self.assertEqual(case["evidence_session_ids"], ["conv0-s1", "conv0-s2"])
        self.assertEqual(case["evidence_turn_ids"], ["D1:1", "D2:1", "external"])
        self.assertEqual(len(case["haystack_sessions"]), 1)
        changed_date = copy.deepcopy(raw)
        changed_date[0]["conversation"]["session_1_date_time"] = "different date"
        self.assertEqual(self.locomo(changed_date), baseline)
        raw[0]["qa"] += [{"question": "", "answer": 0}, {"answer": False}]
        raw += [{"conversation": {"session_1": []}, "qa": [{"question": "Skipped", "answer": 0}]}]
        self.assertEqual(self.locomo(raw), baseline)

    def test_mined_ids_use_exact_emitted_spelling(self):
        for sid in ("s0", " s0 ", "", " \t "):
            with self.subTest(sid=sid):
                case = self.lme([lme_case([[message("Text")]], [sid])])["cases"][0]
                self.assertEqual(case["evidence_session_ids"], [sid])
                self.assertEqual(case["evidence_turn_ids"], [sid + ":m0"])
                self.assertEqual(case["haystack_sessions"][0]["session_id"], sid)
                self.assertEqual(case["haystack_sessions"][0]["turns"][0]["turn_id"], sid + ":m0")

    def test_content_filter_fallbacks_and_original_indices(self):
        raw = lme_case([None, [message("")], [None, message("", text="Text"),
            message("", text="", message="Message"), message("Content", text="Ignored"),
            message(""), message(" \t "), message("Unmarked", False)]], [])
        case = self.lme([raw])["cases"][0]
        self.assertEqual(case["evidence_session_ids"], ["s2"])
        self.assertEqual(case["evidence_turn_ids"], ["s2:m1", "s2:m2", "s2:m3", "s2:m5"])
        turns = case["haystack_sessions"][0]["turns"]
        self.assertEqual([t["turn_id"] for t in turns], ["s2:m1", "s2:m2", "s2:m3", "s2:m5", "s2:m6"])
        self.assertEqual([t["messages"][0]["content"] for t in turns], ["Text", "Message", "Content", " \t ", "Unmarked"])
        self.assertEqual(case["haystack_sessions"][0]["session_date"], "")

    def test_ordered_union_preserves_authored_external_labels(self):
        raw = lme_case([[message("")], [message("One"), message("Two")]], ["blank", " s1 "],
            answer_session_ids=[" blank ", "outside_abs", "s1", "", "s1"],
            evidence_session_ids=[" external ", "outside_abs"],
            evidence_turn_ids=[" blank:m0 ", "s1:m0", "older", "older", ""])
        case = self.lme([raw])["cases"][0]
        self.assertEqual(case["evidence_session_ids"], ["blank", "outside_abs", "s1", "external", " s1 "])
        self.assertEqual(case["evidence_turn_ids"], ["blank:m0", "s1:m0", "older", " s1 :m0", " s1 :m1"])
        self.assertEqual(case["meta"]["evidence_turn_id_count"], 5)
        self.assertEqual(len(case["haystack_sessions"]), 1)
        duplicate = self.lme([lme_case([[message("First")], [message("Second")]], ["s1"])])["cases"][0]
        self.assertEqual(duplicate["evidence_session_ids"], ["s1"])
        self.assertEqual(duplicate["evidence_turn_ids"], ["s1:m0"])

    def test_flag_off_and_non_evidence_output_preserved(self):
        for sid in ("s0", " s0 ", "", " \t "):
            raw = lme_case([[message(""), message("Text"), message("", text="Fallback")]], [sid],
                answer_session_ids=[" outside_abs ", "same", "same", ""],
                evidence_session_ids=["ignored"], evidence_turn_ids=["also-ignored"])
            on = self.lme([raw])
            off = self.lme_off([raw])
            c = off["cases"][0]
            self.assertEqual(c["evidence_session_ids"], [" outside_abs ", "same", "same", ""])
            self.assertEqual(c["evidence_turn_ids"], [])
            self.assertIs(c["meta"]["evidence_mining_enabled"], False)
            self.assertEqual(c["meta"]["evidence_turn_id_count"], 0)
            self.assertEqual(c["expected_answer"], raw["answer"])
            self.assertEqual(c["question_date"], raw["question_date"])
            self.assertEqual(c["haystack_sessions"][0]["session_date"], "2026-10-01")
            for output in (on, off):
                c = output["cases"][0]
                for key in ("evidence_session_ids", "evidence_turn_ids"):
                    del c[key]
                for key in ("evidence_mining_enabled", "evidence_turn_id_count"):
                    del c["meta"][key]
            self.assertEqual(on, off)

    def test_blank_case_eligibility_and_fallback_case_index(self):
        raw = [lme_case([[message("")]]), lme_case([[message("Text")]], [], question_id="")]
        for transform in (self.lme, self.lme_off):
            cases = transform(raw)["cases"]
            self.assertEqual(len(cases), 1)
            self.assertEqual(cases[0]["case_id"], "lme-case-1")
            self.assertEqual(cases[0]["haystack_sessions"][0]["session_id"], "s0")


if __name__ == "__main__":
    unittest.main()
