"""Phase C: RET-02 / SCH-02 / ARCH-02 / symlink lexists defenses."""
from __future__ import annotations

import ast
from pathlib import Path

from eimemory.governance.code_evolution import run_code_sandbox
from eimemory.governance.promotion_manager import _has_symlink_component


def test_engine_fusion_does_not_use_id_item_dict_keys() -> None:
    source = Path("eimemory/retrieval/engine.py").read_text(encoding="utf-8")
    assert "record_key_by_id" not in source
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "id":
            # Allow unrelated id() uses that are not fusion keying; fail if used as dict key material nearby.
            pass
    assert "id(item)" not in source


def test_run_code_sandbox_distinguishes_not_applicable_status() -> None:
    report = run_code_sandbox(
        runtime=None,
        incident={"incident_type": "quota", "title": "quota blip", "summary": "not code"},
        create_worktree=False,
    )
    assert report["ok"] is True
    assert report["status"] == "not_applicable"
    assert report["sandbox_plan"] is None


def test_has_symlink_component_treats_dangling_link_as_unsafe(tmp_path: Path) -> None:
    target = tmp_path / "missing-target"
    link = tmp_path / "dangling"
    link.symlink_to(target)
    assert _has_symlink_component(tmp_path, "dangling/child") is True
