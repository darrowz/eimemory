"""Guard against reintroduction of shadowed jsonl.py duplicate definitions."""
from __future__ import annotations

import ast
from pathlib import Path


def test_jsonl_log_has_exactly_one_class_definition() -> None:
    path = Path(__file__).resolve().parents[1] / "eimemory" / "storage" / "jsonl.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    classes = [node.name for node in tree.body if isinstance(node, ast.ClassDef)]
    assert classes.count("JsonlLog") == 1, classes
    assert classes.count("_DigestAccumulator") == 1, classes
    # Top-level helpers must also be unique.
    funcs = [node.name for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))]
    for name in ("_fsync_directory", "canonical_payload_json", "payload_digest", "scan_jsonl_strict", "iter_jsonl_payloads"):
        assert funcs.count(name) == 1, (name, funcs.count(name))
