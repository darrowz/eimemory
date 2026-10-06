"""Offline compatibility checks using only AST-selected pure text helpers."""

import ast
import json
from pathlib import Path
import unittest


SOURCE_PATH = (
    Path(__file__).resolve().parents[1]
    / "eimemory/ei_bridge/channels/openclaw_feishu.py"
)
HELPERS = {
    "_extract_text", "_find_text", "_find_first", "_find_first_nonblank",
    "_parse_json_object",
}


class FeishuTextContainerCompatibilityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        tree = ast.parse(SOURCE_PATH.read_text(encoding="utf-8"))
        selected = []
        for node in tree.body:
            if isinstance(node, ast.FunctionDef) and node.name in HELPERS:
                if node.decorator_list or node.args.defaults or node.args.kw_defaults:
                    raise AssertionError("Selected helpers must have no evaluated defaults/decorators")
                node.returns = None
                for argument in node.args.posonlyargs + node.args.args + node.args.kwonlyargs:
                    argument.annotation = None
                selected.append(node)
        found = {node.name for node in selected}
        if not (HELPERS - {"_find_first_nonblank"}) <= found:
            raise AssertionError("Required pure text helpers missing")
        namespace = {"json": json}
        unit = ast.fix_missing_locations(ast.Module(body=selected, type_ignores=[]))
        exec(compile(unit, str(SOURCE_PATH), "exec"), namespace)
        cls.extract = staticmethod(namespace["_extract_text"])
        cls.find_first = staticmethod(namespace["_find_first"])

    def test_blank_outer_containers_reach_nested_json(self):
        for key in ("content", "body"):
            for blank in ("", " \n\t", None):
                with self.subTest(key=key, blank=blank):
                    event = {key: blank, "message": {key: json.dumps({"text": "系统状态"})}}
                    self.assertEqual(self.extract(event), "系统状态")

    def test_blank_siblings_reach_nested_json(self):
        for key in ("content", "body"):
            with self.subTest(key=key):
                event = {key: "", "messages": [{key: " "}, {key: json.dumps({"text": "inner"})}]}
                self.assertEqual(self.extract(event), "inner")

    def test_existing_text_precedence(self):
        cases = [
            ({"text": "", "message": {"text": "系统状态"}}, "系统状态"),
            ({"text": "outer", "message": {"text": "inner"}, "content": "other"}, "outer"),
            ({"raw_text": "outer", "content": json.dumps({"text": "inner"})}, "outer"),
        ]
        for event, expected in cases:
            with self.subTest(event=event):
                self.assertEqual(self.extract(event), expected)

    def test_nonblank_container_precedence(self):
        for key in ("content", "body"):
            for outer in ("outer", json.dumps({"text": "outer"})):
                with self.subTest(key=key, outer=outer):
                    event = {key: outer, "message": {key: json.dumps({"text": "inner"})}}
                    self.assertEqual(self.extract(event), "outer")

    def test_existing_json_and_fallback_behavior(self):
        cases = [
            ({"content": json.dumps({"type": "status"}), "body": "wake"}, "wake"),
            ({"content": json.dumps({"type": "status"})}, ""),
            ({"content": "status {"}, "status {"),
            ({"content": "", "body": "wake"}, "wake"),
            ({"content": "", "body": None, "message": {"content": " ", "body": ""}}, ""),
            ({}, ""),
            ({"content": {}, "message": {"content": json.dumps({"text": "inner"})}, "body": "wake"}, "wake"),
            ({"content": '["status"]'}, '["status"]'),
            ({"content": '"status"'}, '"status"'),
            ({"content": "123"}, "123"),
        ]
        for event, expected in cases:
            with self.subTest(event=event):
                self.assertEqual(self.extract(event), expected)

    def test_generic_id_lookup_unchanged(self):
        event = {"message_id": "", "nested": {"message_id": "nested"}, "event_id": "event"}
        self.assertEqual(self.find_first(event, "message_id"), "")
        self.assertEqual(self.find_first(event, "event_id"), "event")
        event = {"message_id": "outer", "nested": {"message_id": "inner"}}
        self.assertEqual(self.find_first(event, "message_id"), "outer")


if __name__ == "__main__":
    unittest.main()
