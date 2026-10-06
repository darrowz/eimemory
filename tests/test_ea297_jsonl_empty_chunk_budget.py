"""Offline exact-window regression; no project imports or real parser/cleaner."""
import io
from pathlib import Path
import unittest


def load_excerpt_functions(source):
    # Read only the two authorized functions, never parse/import the module.
    with Path(source).open(encoding="utf-8") as handle:
        for _ in range(523):
            next(handle)
        first = next(handle)
        if not first.startswith("def _excerpt_from_jsonl("):
            raise AssertionError("authorized window start moved")
        selected = [first]
        for _ in range(32):  # Original 31 lines, candidate 33 lines.
            line = next(handle)
            selected.append(line)
            if line == "    return str(value)\n":
                break
        else:
            raise AssertionError("authorized window end moved")
    namespace = {"Any": object}
    exec(compile("".join(selected), "<authorized-jsonl-window>", "exec"), namespace)
    return namespace


class ParserFake:
    class JSONDecodeError(ValueError):
        pass

    def __init__(self, values):
        self.values = values
        self.calls = []

    def loads(self, line):
        self.calls.append(line)
        value = self.values[line]
        if isinstance(value, BaseException):
            raise value
        return value


class JsonlBudgetTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.namespace = load_excerpt_functions(
            Path(__file__).resolve().parents[1] / "eimemory/intake/loop.py"
        )

    def run_excerpt(self, tokens, limit, values):
        parser = ParserFake(values)
        clean_calls = []
        def clean_fake(text, maximum):
            clean_calls.append((text, maximum))
            return text[:maximum]
        self.namespace.update(json=parser, _clean_excerpt=clean_fake)
        raw = io.StringIO("\n".join(tokens)).read()
        result = self.namespace["_excerpt_from_jsonl"](raw, limit)
        return result, parser.calls, clean_calls

    def test_leading_nulls_do_not_exhaust_positive_budget(self):
        for limit in (1, 2, 5, 20):
            with self.subTest(limit=limit):
                result, calls, clean = self.run_excerpt(
                    ["null"] * (limit + 1) + ['"abcdef"'], limit,
                    {"null": None, '"abcdef"': "abcdef"})
                self.assertEqual(result, "abcdef"[:limit])
                self.assertEqual(calls[-1], '"abcdef"')
                self.assertEqual(clean, [("abcdef", limit)])

    def test_empty_forms_and_interleaved_empty_chunks(self):
        result, calls, clean = self.run_excerpt(
            ["a", "null", "empty", "list", "dict", "b", "tail"], 3,
            {"a": "a", "null": None, "empty": "", "list": [],
             "dict": {}, "b": "b", "tail": "unread"})
        self.assertEqual(result, "a\nb")
        self.assertEqual(calls[-1], "b")
        self.assertEqual(clean, [("a\nb", 3)])

    def test_real_fragments_keep_separator_and_cutoff(self):
        result, calls, clean = self.run_excerpt(
            ["a", "b", "tail"], 4, {"a": "ab", "b": "cd", "tail": "unread"})
        self.assertEqual(result, "ab\nc")
        self.assertEqual(calls, ["a", "b"])
        self.assertEqual(clean, [("ab\ncd", 4)])

    def test_blank_lines_and_invalid_json_fallback(self):
        result, calls, clean = self.run_excerpt(
            ["", "  ", " invalid ", "tail"], 7,
            {"invalid": ParserFake.JSONDecodeError("synthetic"), "tail": "unread"})
        self.assertEqual(result, "invalid")
        self.assertEqual(calls, ["invalid"])
        self.assertEqual(clean, [("invalid", 7)])

    def test_other_parser_exception_propagates(self):
        with self.assertRaisesRegex(RuntimeError, "synthetic"):
            self.run_excerpt(["boom"], 5, {"boom": RuntimeError("synthetic")})

    def test_cleaner_exception_propagates(self):
        self.namespace["json"] = ParserFake({"null": None})
        def clean_fake(text, maximum):
            raise RuntimeError("cleaner synthetic")
        self.namespace["_clean_excerpt"] = clean_fake
        with self.assertRaisesRegex(RuntimeError, "cleaner synthetic"):
            self.namespace["_excerpt_from_jsonl"]("null", 1)

    def test_nonpositive_limit_retains_existing_stop(self):
        for limit in (0, -1):
            result, calls, clean = self.run_excerpt(
                ["null", "tail"], limit, {"null": None, "tail": "unread"})
            self.assertEqual(result, "")
            self.assertEqual(calls, ["null"])
            self.assertEqual(clean, [("", limit)])

    def test_all_empty_and_blank_input(self):
        for tokens, values in ((["null"] * 8, {"null": None}), (["", " "], {})):
            result, _, clean = self.run_excerpt(tokens, 3, values)
            self.assertEqual(result, "")
            self.assertEqual(clean, [("", 3)])

    def test_nested_preferred_and_scalar_text_remain_unchanged(self):
        _, _, clean = self.run_excerpt(["nested", "zero"], 30,
            {"nested": {"title": None, "text": ["x", "y"]}, "zero": 0})
        self.assertEqual(clean, [("x\ny\n0", 30)])

    def test_whitespace_fragment_is_not_newly_filtered(self):
        _, calls, clean = self.run_excerpt(["space", "tail"], 1,
            {"space": " ", "tail": "unread"})
        self.assertEqual(calls, ["space"])
        self.assertEqual(clean, [(" ", 1)])


if __name__ == "__main__":
    unittest.main()
