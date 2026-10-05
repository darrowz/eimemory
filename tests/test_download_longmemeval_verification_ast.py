"""EA-136: only preinspected verifier AST functions run on in-memory inputs.

Never import the download script: its module body changes socket defaults.
Only this test's source lookup uses the filesystem; verifier paths/streams are
inert doubles. No download function, CLI, project import, or dataset is run.
"""
from __future__ import annotations

import ast
import codecs
import contextlib
import io
import json
from pathlib import Path
import unittest

SOURCE = Path(__file__).resolve().parents[1] / "scripts/download_longmemeval.py"
PREFIX = 1024 * 1024


class MemoryPath:
    def __init__(self, payload):
        self.payload = payload
        self.binary_reads = []

    def __str__(self):
        return "<in-memory-json>"

    def open(self, mode, encoding=None):
        assert mode in ("rb", "r")
        if mode == "r":
            assert encoding == "utf-8"
            return io.TextIOWrapper(io.BytesIO(self.payload), encoding=encoding)
        reads = self.binary_reads

        class TrackedBytes(io.BytesIO):
            def read(self, size=-1):
                reads.append(size)
                return super().read(size)

        return TrackedBytes(self.payload)

    def read_text(self, encoding, errors="strict"):
        # Retained only to observe the frozen baseline's warning branch.
        assert encoding == "utf-8"
        return self.payload.decode(encoding, errors=errors)


def extract_verifiers(source):
    tree = ast.parse(source)
    names = {"verify_integrity", "verify"}
    definitions = [node for node in tree.body
                   if isinstance(node, ast.FunctionDef) and node.name in names]
    assert {node.name for node in definitions} == names
    for definition in definitions:
        assert not definition.decorator_list
        assert not any(isinstance(node, (ast.Import, ast.ImportFrom))
                       for node in ast.walk(definition))
    namespace = {"Path": MemoryPath, "json": json, "codecs": codecs}
    module = ast.Module(body=definitions, type_ignores=[])
    exec(compile(module, "<inspected-verifiers-only>", "exec"), namespace)
    return namespace


def observe(namespace, payload, functions=("verify_integrity", "verify")):
    path = MemoryPath(payload)
    output = io.StringIO()
    try:
        with contextlib.redirect_stdout(output):
            for name in functions:
                namespace[name](path)
    except (SystemExit, Exception) as exc:
        return {"outcome": type(exc).__name__, "detail": str(exc),
                "stdout": output.getvalue(), "binary_reads": path.binary_reads}
    return {"outcome": "success", "detail": "", "stdout": output.getvalue(),
            "binary_reads": path.binary_reads}


def split_payload(character, prefix_bytes):
    start = b'[{"question_id":"'
    return (start + b"a" * (PREFIX - len(start) - prefix_bytes)
            + character.encode("utf-8") + b'"}]')


def witness_cases():
    return {
        "split_valid_utf8": split_payload("€", 1),
        "leading_whitespace_valid": b' \t\r\n[{"question_id":"q1"}]',
        "leading_whitespace_invalid": b" [}",
        "whitespace_prefix_valid": b" " * (PREFIX + 1) + b'[{"id":"q2"}]',
        "empty_array": b"[]",
        "nonobject_first_record": b"[null]",
        "valid_object_warning": b'{"a": 1}',
        "normal_array": b'[{"question_id":"q1"}]',
        "incomplete_utf8_eof": b'[{"id":"' + b"\xe2\x82",
        "bad_utf8_after_prefix": b'[{"id":"' + b"a" * PREFIX + b'\xff"}]',
    }


class DownloadVerificationRegression(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.namespace = extract_verifiers(SOURCE.read_text(encoding="utf-8"))

    def check(self, payload, outcome="success", detail="", stdout=""):
        result = observe(self.namespace, payload)
        self.assertEqual(result["outcome"], outcome, result)
        self.assertIn(detail, result["detail"])
        self.assertIn(stdout, result["stdout"])
        return result

    def test_split_utf8_at_each_multibyte_position(self):
        for character in ("é", "€", "😀"):
            for retained in range(1, len(character.encode("utf-8"))):
                with self.subTest(character=character, retained=retained):
                    result = self.check(split_payload(character, retained))
                    self.assertEqual(result["binary_reads"], [PREFIX, 1])

    def test_incomplete_utf8_at_eof_including_exact_prefix(self):
        for payload in (b'[{"id":"\xe2', b'[{"id":"\xe2\x82',
                        b'[{"id":"' + b"a" * (PREFIX - 10) + b"\xe2\x82"):
            self.check(payload, "SystemExit", "not valid UTF-8")
        self.assertEqual(len(payload), PREFIX)

    def test_malformed_utf8_everywhere(self):
        for payload in (b'[{"id":"\xff"}]',
                        split_payload("€", 1)[:PREFIX] + b'X"}]',
                        b'[{"id":"' + b"a" * PREFIX + b'\xff"}]'):
            self.check(payload, "SystemExit", "not valid UTF-8")

    def test_full_parse_with_leading_whitespace(self):
        for whitespace in (b"", b" ", b"\t\r\n ", b" " * (PREFIX + 1)):
            self.check(whitespace + b'[{"id":"q"}]', stdout="first id: q\n")
            for invalid in (b"[}", b"[{", b"{} trailing", b"{}\n{}"):
                self.check(whitespace + invalid, "SystemExit", "not valid JSON")

    def test_empty_and_whitespace_only_files(self):
        self.check(b"", "SystemExit", "empty")
        for payload in (b" \n", b" " * (PREFIX + 1)):
            self.check(payload, "SystemExit", "JSON")

    def test_unsupported_array_shapes_are_controlled_errors(self):
        self.check(b"[]", "SystemExit", "array is empty")
        for first in (None, 1, "text", [], True):
            self.check(json.dumps([first]).encode(), "SystemExit", "not an object")

    def test_first_id_formatting_and_fallback(self):
        for record, expected in (({"question_id": "q", "id": "i"}, "q"),
                                 ({"id": "i"}, "i"), ({}, "?"),
                                 ({"question_id": None, "id": "i"}, "None")):
            self.check(json.dumps([record]).encode(),
                       stdout=f"OK JSON array, 1 cases, first id: {expected}\n")

    def test_valid_object_retains_warning(self):
        for payload in (b'{"a": 1}', b' \n{"a": 1}',
                        json.dumps({"long": "a" * 400}).encode()):
            result = self.check(payload)
            self.assertEqual(result["stdout"], "Verifying <in-memory-json>\n"
                             + f"WARN first 300 chars: {payload.decode()[:300]!r}\n")

    def test_no_full_dataset_schema_claim(self):
        self.check(b'[{}, null]', stdout="2 cases, first id: ?")

    def test_verify_itself_controls_decode_parse_and_shape_errors(self):
        for payload, detail in ((b"\xff", "UTF-8"), (b" [}", "JSON"),
                                (b"null", "array or object"), (b"[]", "empty")):
            result = observe(self.namespace, payload, functions=("verify",))
            self.assertEqual(result["outcome"], "SystemExit", result)
            self.assertIn(detail, result["detail"])
