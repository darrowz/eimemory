"""Pure string/AST checks; never execute migration scanners or importers."""

import ast
import copy
from itertools import product
from pathlib import Path
import unittest


class MarkdownMigrationTextBoundaryTests(unittest.TestCase):
    source_path = Path(__file__).resolve().parents[1] / "eimemory/compatibility/migration_helpers.py"

    @classmethod
    def setUpClass(cls) -> None:
        tree = ast.parse(cls.source_path.read_text(encoding="utf-8"))
        functions = {node.name: node for node in tree.body if isinstance(node, ast.FunctionDef)}
        helper = functions["_extract_markdown_title_and_body"]
        namespace = {"Path": Path}
        exec(compile(ast.Module(body=[helper], type_ignores=[]), str(cls.source_path), "exec"), namespace)
        cls.extract = staticmethod(namespace[helper.name])

        scanner = functions["_scan_markdown"]
        scanner_assigns = [node for node in ast.walk(scanner) if isinstance(node, ast.Assign)]
        read = next(node.value for node in scanner_assigns if ast.dump(node.targets[0]) == ast.dump(ast.Name(id="text", ctx=ast.Store())))
        reads = [node for node in ast.walk(read) if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "read_text"]
        if len(reads) != 1 or ast.dump(reads[0].func.value) != ast.dump(ast.Name(id="file_path", ctx=ast.Load())):
            raise AssertionError("Expected exactly one source read; no scanner execution is permitted")

        class ReplaceSourceRead(ast.NodeTransformer):
            def visit_Call(self, node):
                if isinstance(node.func, ast.Attribute) and node.func.attr == "read_text":
                    return ast.copy_location(ast.Name(id="raw_text", ctx=ast.Load()), node)
                return self.generic_visit(node)

        cls.scan_text_expr = cls._expression(ReplaceSourceRead().visit(copy.deepcopy(read)))
        extraction = next(node.value for node in scanner_assigns if isinstance(node.targets[0], ast.Tuple))
        cls.scan_extract_expr = cls._expression(extraction, allow_helper=True)

        candidate = functions["_candidate_payload"]
        cls.cleaned_expr = cls._expression(next(node.value for node in candidate.body if isinstance(node, ast.Assign) and node.targets[0].id == "cleaned_text"))
        cls.combined_expr = cls._expression(next(node.value for node in candidate.body if isinstance(node, ast.Assign) and node.targets[0].id == "combined"))
        returned = next(node.value for node in candidate.body if isinstance(node, ast.Return))
        cls.payload_text_expr = cls._expression(next(value for key, value in zip(returned.keys, returned.values) if isinstance(key, ast.Constant) and key.value == "text"))

        importer_loop = next(node for node in functions["import_candidates"].body if isinstance(node, ast.For))
        text_index = next(index for index, node in enumerate(importer_loop.body) if isinstance(node, ast.Assign) and node.targets[0].id == "text")
        title_index = next(index for index, node in enumerate(importer_loop.body) if isinstance(node, ast.Assign) and node.targets[0].id == "title")
        prelude = ast.Module(body=importer_loop.body[text_index:title_index], type_ignores=[])
        cls._check_pure(prelude)
        cls.import_text_prelude = compile(prelude, "<import-text-fragment>", "exec")
        empty_gate = importer_loop.body[title_index + 1]
        if not isinstance(empty_gate, ast.If) or not isinstance(empty_gate.body[0], ast.Continue):
            raise AssertionError("Expected an empty-text guard immediately after title preparation")
        cls.empty_expr = cls._expression(empty_gate.test)

    @staticmethod
    def _check_pure(node, *, allow_helper=False) -> None:
        names = {"str"}
        if allow_helper:
            names.add("_extract_markdown_title_and_body")
        for child in ast.walk(node):
            if isinstance(child, (ast.Import, ast.ImportFrom, ast.For, ast.While, ast.With, ast.Try)):
                raise AssertionError("Only inspected string fragments may execute")
            if isinstance(child, ast.Call):
                if isinstance(child.func, ast.Name) and child.func.id in names:
                    continue
                if isinstance(child.func, ast.Attribute) and child.func.attr in {"get", "strip", "join"}:
                    continue
                raise AssertionError("Unexpected call in an executable string fragment")

    @classmethod
    def _expression(cls, node, *, allow_helper=False):
        cls._check_pure(node, allow_helper=allow_helper)
        return compile(ast.fix_missing_locations(ast.Expression(body=node)), "<migration-string-fragment>", "eval")

    def scanner_text_view(self, raw_text):
        text = eval(self.scan_text_expr, {"raw_text": raw_text})
        return eval(self.scan_extract_expr, {
            "_extract_markdown_title_and_body": self.extract,
            "file_path": Path("note.md"),
            "text": text,
        })

    def candidate_text_view(self, text, source_type):
        namespace = {"text": text, "source_type": source_type}
        namespace["cleaned_text"] = eval(self.cleaned_expr, namespace)
        return eval(self.payload_text_expr, namespace), namespace["cleaned_text"]

    def import_text_view(self, text, source_type):
        namespace = {"candidate": {"text": text, "source_type": source_type}}
        exec(self.import_text_prelude, namespace)
        return namespace["text"], eval(self.empty_expr, namespace)

    def test_scanner_expression_preserves_no_title_boundary_spaces(self) -> None:
        raw = "\n    First indented line  \n\tFinal hard break  \n"
        self.assertEqual(self.scanner_text_view(raw), ("note", raw.strip("\r\n")))

    def test_scanner_expression_preserves_title_body_boundaries(self) -> None:
        raw = "  # Title\n\n    First indented line  \n\tFinal hard break  \n"
        self.assertEqual(self.scanner_text_view(raw), ("Title", "    First indented line  \n\tFinal hard break  "))

    def test_markdown_candidate_text_preserves_presentation(self) -> None:
        raw = "    First indented line  \n\tFinal hard break  "
        self.assertEqual(self.candidate_text_view(raw, "markdown"), (raw, raw.strip()))

    def test_other_candidate_source_types_remain_normalized(self) -> None:
        raw = "    Four ordinary words remain here  \n"
        for source_type in ["jsonl", "sqlite", "unknown", "", None]:
            with self.subTest(source_type=source_type):
                self.assertEqual(self.candidate_text_view(raw, source_type), (raw.strip(), raw.strip()))

    def test_markdown_import_text_fragment_preserves_presentation(self) -> None:
        raw = "    First indented line  \n\tFinal hard break  "
        self.assertEqual(self.import_text_view(raw, "markdown"), (raw, False))

    def test_other_import_source_types_remain_normalized(self) -> None:
        raw = "    Four ordinary words remain here  \n"
        for source_type in ["jsonl", "sqlite", "unknown", "", None]:
            with self.subTest(source_type=source_type):
                self.assertEqual(self.import_text_view(raw, source_type), (raw.strip(), False))

    def test_empty_guard_still_uses_normalized_text(self) -> None:
        for text, source_type in product(["", " ", "\t\n  \r\n", None, 0], ["markdown", "jsonl", "sqlite"]):
            with self.subTest(text=text, source_type=source_type):
                self.assertTrue(self.import_text_view(text, source_type)[1])

    def test_all_three_string_boundaries_preserve_markdown(self) -> None:
        raw = "# Useful facts\n\n    Four ordinary words remain here  \n\tTail\t \n"
        expected = "    Four ordinary words remain here  \n\tTail\t "
        title, body = self.scanner_text_view(raw)
        payload_text, normalized = self.candidate_text_view(body, "markdown")
        import_text, empty = self.import_text_view(payload_text, "markdown")
        self.assertEqual(title, "Useful facts")
        self.assertEqual((body, payload_text, import_text), (expected, expected, expected))
        self.assertEqual(normalized, expected.strip())
        self.assertFalse(empty)

    def test_first_line_indented_titles_keep_legacy_selection(self) -> None:
        for prefix in [" ", "    ", "\t", "\n \t", "\u2003"]:
            with self.subTest(prefix=prefix):
                self.assertEqual(self.scanner_text_view(prefix + "# Title\nBody  "), ("Title", "Body  "))

    def test_first_line_indented_fences_keep_legacy_selection(self) -> None:
        for prefix, fence in product(["", "    ", "\t", "\n \t"], ["```", "~~~md"]):
            closer = fence[:3]
            raw = prefix + fence + "\n# Code heading\n" + closer + "\n# Real title\nBody  "
            with self.subTest(prefix=prefix, fence=fence):
                self.assertEqual(self.scanner_text_view(raw), ("Real title", "Body  "))
        raw = "    ```\n# Real\nalpha beta gamma delta  \n"
        self.assertEqual(self.scanner_text_view(raw), ("note", raw.strip("\r\n")))

    def test_later_line_indentation_keeps_legacy_selection(self) -> None:
        cases = [
            ("Preamble\n # Indented\nText  ", ("note", "Preamble\n # Indented\nText  ")),
            ("Preamble\n    ```\n# Title\nTail  ", ("Title", "Tail  ")),
            ("Preamble\n\t~~~\n# Title\nTail  ", ("Title", "Tail  ")),
            ("Preamble\n   ```\n# Code\n```\n# Real\nTail  ", ("Real", "Tail  ")),
        ]
        for raw, expected in cases:
            with self.subTest(raw=raw):
                self.assertEqual(self.scanner_text_view(raw), expected)

    def test_legacy_title_and_normalized_decision_inputs_match(self) -> None:
        prefixes = ["", " ", "    ", "\t", "\n\n", "\r\n\t", "\v\f", "\x1c", "\u2003", "\u2028"]
        suffixes = ["", " ", "  ", "\t", "\n", "\r\n  ", "\v\f", "\x1e", "\u2029", "\u3000"]
        documents = [
            "", "# ", "# \t", "# Title", "# Title\n", "# Title\n    ",
            "# Title\n\n    Four ordinary words remain here  ",
            "# Title ###  \n\tBody\t", "    Indented body\nLast line",
            "## Other heading\nBody", "Preamble\n # Indented\nBody",
            "```\n# Fenced\n```", "~~~md\n# Fenced\n~~~\n# Real\nBody",
            "    ```\n# Fenced\n```\n# Real\nBody", "```bad`info\n# Real\nBody",
            "````\n```\n# Fenced\n````\n# Real\nBody",
            "Preamble\n    ```\n# Title\nBody", "# Title\r\n\r\nBody  \r\n",
            "# Title\vIndented\fTail", "# Title\u2028Body\u2029Tail",
            "# Title\nBody\n# Later title\nTail",
        ]
        for prefix, document, suffix in product(prefixes, documents, suffixes):
            raw = prefix + document + suffix
            with self.subTest(raw=raw):
                old_title, old_body = self.extract(Path("note.md"), raw.strip())
                title, body = self.scanner_text_view(raw)
                self.assertEqual(title, old_title)
                self.assertEqual(body.strip(), old_body.strip())
                self.assertEqual(
                    eval(self.combined_expr, {"cleaned_title": title.strip() or "Migrated memory", "cleaned_text": body.strip()}),
                    eval(self.combined_expr, {"cleaned_title": old_title.strip() or "Migrated memory", "cleaned_text": old_body.strip()}),
                )


if __name__ == "__main__":
    unittest.main()
