"""Tests for the minimal eiskills bridge (Task 4.4).

eiskills is a sibling project — this bridge is a stub that backs registration
with a JSONL manifest store. Real eiskills integration is deferred to a
future task; the contract tested here is the in-process manifest API.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from eimemory.governance.skills.eiskills_bridge import (
    register_skill,
    list_active_skills,
    deregister_skill,
    get_skill,
)


@pytest.fixture()
def registry_path(tmp_path: Path) -> Path:
    """Fresh JSONL registry in a tmp dir for every test."""
    return tmp_path / "eiskills_registry.jsonl"


def test_register_skill_roundtrip(registry_path: Path):
    """A skill registered via register_skill must appear in list_active_skills."""
    register_skill(
        skill_name="auto-recall-v1",
        manifest={"triggers": ["recall_test"], "handler": "recall.invoke"},
        version="1.0.0",
        registry_path=registry_path,
    )
    active = list_active_skills(registry_path=registry_path)
    assert any(s["skill_name"] == "auto-recall-v1" for s in active), (
        f"expected auto-recall-v1 in active list, got: {active}"
    )


def test_register_skill_writes_jsonl_row(registry_path: Path):
    """register_skill must append a JSONL row with skill_name, version, manifest, status, ts."""
    register_skill(
        skill_name="auto-recall-v1",
        manifest={"triggers": ["recall_test"]},
        version="1.0.0",
        registry_path=registry_path,
    )
    rows = [json.loads(line) for line in registry_path.read_text(encoding="utf-8").splitlines() if line]
    assert len(rows) == 1
    row = rows[0]
    assert row["skill_name"] == "auto-recall-v1"
    assert row["version"] == "1.0.0"
    assert row["manifest"] == {"triggers": ["recall_test"]}
    assert row["status"] == "active"
    assert "ts" in row and row["ts"]


def test_register_skill_dedup_on_same_name_version(registry_path: Path):
    """Registering the same (skill_name, version) twice updates, not duplicates."""
    register_skill(
        skill_name="auto-recall-v1",
        manifest={"triggers": ["a"]},
        version="1.0.0",
        registry_path=registry_path,
    )
    register_skill(
        skill_name="auto-recall-v1",
        manifest={"triggers": ["a", "b"]},  # updated manifest
        version="1.0.0",
        registry_path=registry_path,
    )
    active = list_active_skills(registry_path=registry_path)
    matches = [s for s in active if s["skill_name"] == "auto-recall-v1" and s["version"] == "1.0.0"]
    assert len(matches) == 1
    assert matches[0]["manifest"]["triggers"] == ["a", "b"]


def test_register_skill_keeps_history_in_jsonl(registry_path: Path):
    """JSONL keeps an audit trail — dedup is at read-time, not write-time."""
    register_skill(
        skill_name="auto-recall-v1",
        manifest={"triggers": ["a"]},
        version="1.0.0",
        registry_path=registry_path,
    )
    register_skill(
        skill_name="auto-recall-v1",
        manifest={"triggers": ["a", "b"]},
        version="1.0.0",
        registry_path=registry_path,
    )
    raw_rows = [json.loads(line) for line in registry_path.read_text(encoding="utf-8").splitlines() if line]
    assert len(raw_rows) == 2  # both writes recorded


def test_deregister_skill_marks_inactive(registry_path: Path):
    """deregister_skill sets status=inactive, list_active_skills excludes it."""
    register_skill(
        skill_name="auto-recall-v1",
        manifest={"triggers": ["a"]},
        version="1.0.0",
        registry_path=registry_path,
    )
    deregister_skill(skill_name="auto-recall-v1", registry_path=registry_path)
    active = list_active_skills(registry_path=registry_path)
    assert not any(s["skill_name"] == "auto-recall-v1" for s in active)
    row = get_skill(skill_name="auto-recall-v1", registry_path=registry_path)
    assert row is not None
    assert row["status"] == "inactive"


def test_get_skill_returns_none_for_unknown(registry_path: Path):
    """get_skill returns None for a skill that was never registered."""
    assert get_skill(skill_name="never-registered", registry_path=registry_path) is None


def test_register_skill_creates_parent_dir(tmp_path: Path):
    """If the registry path's parent does not exist, register_skill creates it."""
    nested = tmp_path / "deep" / "nested" / "registry.jsonl"
    register_skill(
        skill_name="auto-recall-v1",
        manifest={"triggers": ["a"]},
        version="1.0.0",
        registry_path=nested,
    )
    assert nested.exists()
    assert nested.parent.is_dir()
