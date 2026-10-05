"""Offline helper regressions, isolated from runtime/storage initialization."""

import ast
from pathlib import Path
import unittest


class MarkdownTitleExtractionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        source = Path(__file__).resolve().parents[1] / "eimemory/compatibility/migration_helpers.py"
        tree = ast.parse(source.read_text(encoding="utf-8"))
        function = next(
            node for node in tree.body
            if isinstance(node, ast.FunctionDef)
            and node.name == "_extract_markdown_title_and_body"
        )
        namespace = {"Path": Path}
        exec(compile(ast.Module(body=[function], type_ignores=[]), str(source), "exec"), namespace)
        cls.extract = staticmethod(namespace[function.name])

    def test_body_preserves_indentation_tabs_and_trailing_spaces(self) -> None:
        text = "# Title\n\n    indented code  \n\tmore code\t\n- item\n  continuation  \n"
        self.assertEqual(
            self.extract(Path("note.md"), text),
            ("Title", "    indented code  \n\tmore code\t\n- item\n  continuation  "),
        )

    def test_no_heading_preserves_body_spaces(self) -> None:
        text = "\n    first line  \n\tlast line \n"
        self.assertEqual(
            self.extract(Path("file-name.md"), text),
            ("file-name", "    first line  \n\tlast line "),
        )

    def test_fenced_headings_do_not_supply_title(self) -> None:
        for opening, closing in [("```", "```"), ("~~~python", "~~~"), ("```md", "````")]:
            with self.subTest(opening=opening):
                text = f"{opening}\n# Code heading\n{closing}\n# Real title\nBody  \n"
                self.assertEqual(self.extract(Path("note.md"), text), ("Real title", "Body  "))

    def test_fenced_heading_without_real_title_uses_filename(self) -> None:
        text = "```markdown\n# Example\n```"
        self.assertEqual(self.extract(Path("note.md"), text), ("note", text))

    def test_unclosed_fences_hide_headings(self) -> None:
        for opening in ["```", "~~~", "````python"]:
            with self.subTest(opening=opening):
                text = f"{opening}\n# Still code\n"
                self.assertEqual(self.extract(Path("note.md"), text), ("note", text[:-1]))

    def test_invalid_closers_do_not_end_fence(self) -> None:
        for invalid_closer in ["```", "~~~~", "```` info", "    ````", "\t````"]:
            with self.subTest(invalid_closer=invalid_closer):
                text = f"````\n{invalid_closer}\n# Still code\n````\n# Real\nBody"
                self.assertEqual(self.extract(Path("note.md"), text), ("Real", "Body"))

    def test_indented_fences_and_whitespace_after_closer(self) -> None:
        for indent in ["", " ", "  ", "   "]:
            with self.subTest(indent=indent):
                text = f"{indent}~~~text\n# Code\n{indent}~~~~ \t\n# Real\nBody"
                self.assertEqual(self.extract(Path("note.md"), text), ("Real", "Body"))

    def test_non_fences_do_not_hide_title(self) -> None:
        for non_fence in ["``", "~~", "    ```", "\t~~~", "```bad`info"]:
            with self.subTest(non_fence=non_fence):
                text = f"{non_fence}\n# Real\nBody"
                self.assertEqual(self.extract(Path("note.md"), text), ("Real", "Body"))

    def test_pre_heading_content_is_still_discarded_and_closing_hashes_kept(self) -> None:
        text = "Preamble\n\n# Title ###  \n\nBody\n# Later title\nTail"
        self.assertEqual(
            self.extract(Path("note.md"), text),
            ("Title ###", "Body\n# Later title\nTail"),
        )

    def test_other_heading_forms_keep_legacy_filename_fallback(self) -> None:
        for text in ["## Level two", " # Indented", "#No space", "# ", "# \t"]:
            with self.subTest(text=text):
                self.assertEqual(self.extract(Path("note.md"), text), ("note", text))

    def test_empty_and_heading_only_bodies(self) -> None:
        for text, expected in [("", ("note", "")), ("\n\n", ("note", "")), ("# Title\n\n", ("Title", ""))]:
            with self.subTest(text=text):
                self.assertEqual(self.extract(Path("note.md"), text), expected)

    def test_crlf_normalization_with_title_retains_spaces(self) -> None:
        text = "# Title\r\n\r\n    code  \r\nlast  \r\n"
        self.assertEqual(self.extract(Path("note.md"), text), ("Title", "    code  \nlast  "))


if __name__ == "__main__":
    unittest.main()
