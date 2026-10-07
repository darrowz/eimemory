"""Regression for JSONL diagnostic physical line numbers; stdlib only."""
import ast
import json
import os
from pathlib import Path
import tempfile
from typing import Any
import unittest


def _source_path():
    override = os.environ.get("EIMEMORY_DOCTOR_SOURCE")
    if override:
        return Path(override)
    for root in Path(__file__).resolve().parents:
        candidate = root / "eimemory/cli/doctor.py"
        if candidate.is_file():
            return candidate
    raise FileNotFoundError("Cannot locate repository eimemory/cli/doctor.py")


def _load_functions():
    source = _source_path().read_text(encoding="utf-8")
    tree = ast.parse(source, filename="<doctor source>")
    names = {"_count_lines", "_head_parse_check"}
    selected = [node for node in tree.body
                if isinstance(node, ast.FunctionDef) and node.name in names]
    if len(selected) != 2 or {node.name for node in selected} != names:
        raise ValueError("Expected exactly the two doctor functions")
    module = ast.Module(body=selected, type_ignores=[])
    namespace = {
        "Path": Path, "Any": Any, "json": json,
        "JSONL_PARSE_SAMPLE_LIMIT": 100,
        "JSONL_STREAMING_THRESHOLD_BYTES": 1024,
        "_file_size": lambda p: p.stat().st_size if p.exists() else 0,
    }
    exec(compile(module, "<selected doctor functions>", "exec"), namespace)
    return namespace


class DoctorPhysicalLineTests(unittest.TestCase):
    def setUp(self):
        self.functions = _load_functions()
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "sample.jsonl"

    def inspect(self, payload, limit=100):
        self.path.write_bytes(payload)
        return self.functions["_head_parse_check"](self.path, limit)

    def test_blank_first_line_reports_physical_second_line(self):
        result = self.inspect(b"\nBAD\n")
        self.assertEqual(result["first_error"], "line 2: Expecting value")
        self.assertEqual((result["lines"], result["blank_count"],
                          result["sample_lines"]), (2, 1, 1))
        self.assertFalse(result["head_parse_ok"])

    def test_multiple_blank_lines_preserve_first_physical_error(self):
        result = self.inspect(b" \t\n{}\n\nBAD\nBAD\n")
        self.assertEqual(result["first_error"], "line 4: Expecting value")
        self.assertEqual((result["blank_count"], result["sample_lines"],
                          result["empty_dict_count"]), (2, 3, 1))

    def test_nonblank_sample_limit_is_preserved(self):
        result = self.inspect(b"\n{}\n\n1\nBAD\n", 2)
        self.assertTrue(result["head_parse_ok"])
        self.assertIsNone(result["first_error"])
        self.assertEqual((result["sample_lines"], result["blank_count"],
                          result["empty_dict_count"], result["non_empty_count"]),
                         (2, 2, 1, 1))

    def test_no_blank_error_number_unchanged(self):
        result = self.inspect(b"{}\nBAD\n")
        self.assertEqual(result["first_error"], "line 2: Expecting value")
        self.assertFalse(result["head_parse_ok"])

    def test_whitespace_only_input_and_zero_limit(self):
        result = self.inspect(b"\n \t\n")
        self.assertEqual((result["blank_count"], result["sample_lines"]), (2, 0))
        self.assertTrue(result["head_parse_ok"])
        result = self.inspect(b"BAD\n", 0)
        self.assertIsNone(result["first_error"])
        self.assertEqual(result["sample_lines"], 0)


if __name__ == "__main__":
    unittest.main()
