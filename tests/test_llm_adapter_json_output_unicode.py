"""Offline adapter JSON protocol tests: no project import or command execution.

Only the adapter's main function is extracted from its AST. Its request callback
is synthetic and stdout is an in-memory strict UTF-8 writer.
"""
from __future__ import annotations

import argparse
import ast
import builtins
import io
import json
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest


SOURCE = Path(__file__).resolve().parents[1] / "eimemory/llm/openclaw_adapter.py"


def extract_main(source: Path):
    tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
    mains = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "main"]
    if len(mains) != 1:
        raise AssertionError("Expected exactly one main function")
    main = mains[0]
    if main.decorator_list or main.args.defaults or main.args.kw_defaults:
        raise AssertionError("Unexpected executable main declaration")
    allowed_calls = {"json.load", "json.dumps", "complete_request", "print", "type"}
    forbidden = (ast.Import, ast.ImportFrom, ast.With, ast.AsyncWith, ast.Lambda)
    for node in ast.walk(main):
        if isinstance(node, forbidden):
            raise AssertionError("Unexpected executable dependency in main")
        if isinstance(node, ast.Call) and ast.unparse(node.func) not in allowed_calls:
            raise AssertionError("Unexpected main call: " + ast.unparse(node.func))
    isolated = ast.Module(body=[main], type_ignores=[])
    ast.fix_missing_locations(isolated)
    return compile(isolated, str(source) + "::isolated-main", "exec")



def parse_response_ascii_json(raw):
    """Exercise the actual current pure parser, without importing its module."""
    tree = ast.parse(SOURCE.read_text(encoding="utf-8"), filename=str(SOURCE))
    parsers = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "_parse_response"]
    if len(parsers) != 1:
        raise AssertionError("Expected exactly one response parser")
    parser = parsers[0]
    allowed_calls = {
        "isinstance", "ValueError", "response.get", "item['text'].strip",
        "texts.append", "'\\n'.join", "provider.strip", "model.strip",
    }
    for node in ast.walk(parser):
        if isinstance(node, (ast.Import, ast.ImportFrom, ast.With, ast.AsyncWith, ast.Lambda)):
            raise AssertionError("Unexpected executable dependency in parser")
        if isinstance(node, ast.Call) and ast.unparse(node.func) not in allowed_calls:
            raise AssertionError("Unexpected parser call: " + ast.unparse(node.func))
    isolated = ast.Module(body=[parser], type_ignores=[])
    ast.fix_missing_locations(isolated)
    namespace = {"__builtins__": {"dict": dict, "str": str, "list": list, "isinstance": isinstance, "ValueError": ValueError}, "Any": object}
    exec(compile(isolated, str(SOURCE) + "::isolated-parser", "exec"), namespace)
    return namespace["_parse_response"](json.loads(raw))


def invoke_main(result=None, error=None):
    buffer = io.BytesIO()
    writer = io.TextIOWrapper(buffer, encoding="utf-8", errors="strict", newline="\n")
    calls = []

    def complete_request(payload):
        calls.append(payload)
        if error is not None:
            raise error
        return result

    def protocol_print(*args, **kwargs):
        builtins.print(*args, file=writer, **kwargs)

    namespace = {
        "__builtins__": {"Exception": Exception, "int": int, "type": type},
        "json": json,
        "sys": SimpleNamespace(stdin=io.StringIO('{"user_prompt":"test"}')),
        "complete_request": complete_request,
        "print": protocol_print,
    }
    try:
        exec(extract_main(SOURCE), namespace)
        status = namespace["main"]()
        writer.flush()
        raw = buffer.getvalue()
    finally:
        writer.close()
    if calls != [{"user_prompt": "test"}]:
        raise AssertionError("Unexpected callback invocation")
    text = raw.decode("utf-8", errors="strict")
    if len(text.splitlines()) != 1 or not text.endswith("\n"):
        raise AssertionError("Expected exactly one newline-terminated JSON record")
    return status, json.loads(text)


class OpenClawJsonOutputUnicodeTests(unittest.TestCase):
    def test_success_surrogates_in_each_output_field(self):
        for field in ("text", "provider_id", "model_id"):
            for surrogate in ("\ud800", "\udfff"):
                with self.subTest(field=field, codepoint=ord(surrogate)):
                    result = {"text": "answer", "provider_id": "provider", "model_id": "provider/model"}
                    result[field] = "prefix" + surrogate + "suffix"
                    status, decoded = invoke_main(result=result)
                    self.assertEqual(status, 0)
                    self.assertEqual(decoded, result)

    def test_ascii_json_escape_reaches_success_output(self):
        raw = b'{"ok":true,"outputs":[{"text":"\\ud800"}],"provider":"p","model":"m"}'
        result = parse_response_ascii_json(raw)
        self.assertEqual(result, {"text": "\ud800", "provider_id": "p", "model_id": "p/m"})
        status, decoded = invoke_main(result=result)
        self.assertEqual(status, 0)
        self.assertEqual(decoded, result)

    def test_error_surrogates(self):
        for surrogate in ("\ud800", "\udfff"):
            with self.subTest(codepoint=ord(surrogate)):
                message = "bad" + surrogate + "value"
                status, decoded = invoke_main(error=ValueError(message))
                self.assertEqual(status, 1)
                self.assertEqual(decoded, {"ok": False, "error": "ValueError: " + message})

    def test_normal_non_ascii_success(self):
        result = {"text": "中文 😀", "provider_id": "提供方", "model_id": "提供方/模型 🚀"}
        status, decoded = invoke_main(result=result)
        self.assertEqual(status, 0)
        self.assertEqual(decoded, result)

    def test_normal_non_ascii_error(self):
        message = "中文错误 😀"
        status, decoded = invoke_main(error=RuntimeError(message))
        self.assertEqual(status, 1)
        self.assertEqual(decoded, {"ok": False, "error": "RuntimeError: " + message})

    def test_json_surrogate_pair_preserves_emoji(self):
        result = {"text": json.loads('"\\ud83d\\ude00"'), "provider_id": "p", "model_id": "p/m"}
        status, decoded = invoke_main(result=result)
        self.assertEqual(status, 0)
        self.assertEqual(decoded, result)
        self.assertEqual(decoded["text"], "😀")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--source", type=Path)
    options, remaining = parser.parse_known_args()
    if options.source is not None:
        SOURCE = options.source
    unittest.main(argv=[sys.argv[0], *remaining])
