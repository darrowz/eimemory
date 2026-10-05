"""Standalone synthetic regression: compile only _section_after, never import the project."""
import ast
from pathlib import Path
import re
import unittest


def load_section_after():
    source = Path(__file__).resolve().parents[1] / "eimemory/governance/learning/skill_candidate.py"
    module = ast.parse(source.read_text(encoding="utf-8"))
    matches = [node for node in module.body if isinstance(node, ast.FunctionDef) and node.name == "_section_after"]
    if len(matches) != 1:
        raise AssertionError("Expected exactly one _section_after function")
    function = matches[0]
    if function.decorator_list or function.args.defaults or function.args.kw_defaults:
        raise AssertionError("Unexpected definition-time execution")
    allowed_calls = {"text.lower", "lowered.find", "min", "len", "char.lower", "re.search", "next_match.start", "section.strip"}
    if any(isinstance(node, (ast.Import, ast.ImportFrom, ast.With, ast.AsyncWith, ast.Global, ast.Nonlocal)) for node in ast.walk(function)):
        raise AssertionError("Unexpected executable statement")
    calls = {ast.unparse(node.func) for node in ast.walk(function) if isinstance(node, ast.Call)}
    if not calls <= allowed_calls:
        raise AssertionError("Unexpected call: " + repr(calls - allowed_calls))
    function.returns = None
    for argument in function.args.args:
        argument.annotation = None
    isolated = ast.fix_missing_locations(ast.Module(body=[function], type_ignores=[]))
    namespace = {"re": re}
    exec(compile(isolated, "<isolated _section_after>", "exec"), namespace)
    return namespace["_section_after"]


CASES = [('expanding prefix', 'İİ steps: Verify input.', ('steps:',), 'Verify input.'), ('one expanding prefix', 'İ steps: Verify input.', ('steps:',), 'Verify input.'), ('prefix no spaces', 'İİsteps:Verify input.', ('steps:',), 'Verify input.'), ('no marker', 'İİ ordinary', ('steps:',), ''), ('empty markers', 'steps: body', (), ''), ('ASCII insensitive text', 'STEPS: Verify input.', ('steps:',), 'Verify input.'), ('marker stays case sensitive', 'STEPS: body', ('STEPS:',), ''), ('earliest wins', 'other: First steps: Later', ('steps:', 'other:'), 'First steps: Later'), ('tie preserves tuple first short', 'steps: body', ('step', 'steps:'), 's: body'), ('tie preserves tuple first long', 'steps: body', ('steps:', 'step'), 'body'), ('repeat marker', 'İsteps: one steps: two', ('steps:',), 'one steps: two'), ('empty marker zero end', 'İİ steps: body', ('',), 'İİ steps: body'), ('empty marker tie first', 'steps: body', ('', 'steps:'), 'steps: body'), ('empty marker tie second', 'steps: body', ('steps:', ''), 'body'), ('marker at end', 'İsteps:', ('steps:',), ''), ('Unicode whitespace', 'İsteps:\u2003  Verify input. \n', ('steps:',), 'Verify input.'), ('next header truncation', 'İİsteps: First. TOOLS: hammer', ('steps:',), 'First.'), ('next header requires boundary', 'İsteps: axtools: keep', ('steps:',), 'axtools: keep'), ('body expansion preserved', 'steps: İİ Verify input.', ('steps:',), 'İİ Verify input.'), ('non-BMP prefix', '😀İsteps: Verify input.', ('steps:',), 'Verify input.'), ('sigma contextual lower', 'ΟΣ steps: body', ('ος', 'steps:'), 'steps: body'), ('partial expansion end rounds up', 'İx', ('i',), 'x'), ('match expansion suffix', 'İx', ('̇',), 'x'), ('marker expanded spelling', 'İ: value', ('i̇:',), 'value'), ('literal uppercase marker not folded', 'İ: value', ('İ:',), ''), ('long s must not match s', 'ſteps: body', ('steps:',), ''), ('dotless i must not match i', 'ınput: body', ('input:',), ''), ('dotted I not equal single i', 'İnput: body', ('input:',), ''), ('Greek final sigma differs', 'ς: body', ('σ:',), ''), ('Kelvin lower still matches', 'K: body', ('k:',), 'body')]


class SectionAfterUnicodeTests(unittest.TestCase):
    def test_contract_cases(self):
        section_after = load_section_after()
        for name, text, markers, expected in CASES:
            with self.subTest(name=name):
                self.assertEqual(section_after(text, markers), expected)

    def test_original_substring_preserved(self):
        section_after = load_section_after()
        for prefix in ("", "ASCII ", "İ", "İİİ", "😀İ", "ΟΣİ"):
            for body in ("Original CASE", "İ e\u0301 😀", "ÄΩ Mixed İ"):
                with self.subTest(prefix=prefix, body=body):
                    self.assertEqual(section_after(prefix + "steps:" + body, ("steps:",)), body)

    def test_ascii_offsets_unchanged(self):
        section_after = load_section_after()
        for prefix in ("", "a", "Long prefix ", "12. " ):
            for marker in ("steps:", "workflow:", "procedure:"):
                for body in ("A", "MiXeD body.", "  Verify x.  "):
                    with self.subTest(prefix=prefix, marker=marker, body=body):
                        self.assertEqual(section_after(prefix + marker.upper() + body, (marker,)), body.strip())


if __name__ == "__main__":
    unittest.main()
