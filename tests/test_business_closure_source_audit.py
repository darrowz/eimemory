from __future__ import annotations

from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
SPEC = spec_from_file_location(
    "audit_business_closure",
    ROOT / "scripts" / "audit_business_closure.py",
)
assert SPEC is not None and SPEC.loader is not None
AUDIT = module_from_spec(SPEC)
sys.modules[SPEC.name] = AUDIT
SPEC.loader.exec_module(AUDIT)


def test_audit_paths_parses_every_python_line_and_callable(tmp_path: Path) -> None:
    source = tmp_path / "eimemory" / "api" / "sample.py"
    source.parent.mkdir(parents=True)
    source.write_text(
        "def public(value: str) -> str:\n"
        "    if not value:\n"
        "        raise ValueError('value required')\n"
        "    return value\n",
        encoding="utf-8",
    )

    (item,) = AUDIT.audit_paths(tmp_path, (Path("eimemory/api/sample.py"),))

    assert item.disposition == "entry_or_adapter"
    assert item.total_lines == 4
    assert item.parsed_lines == 4
    assert item.callables == (("public", 1, 4),)
    assert item.syntax_status == "ok"


def test_audit_paths_rejects_invalid_python(tmp_path: Path) -> None:
    source = tmp_path / "eimemory" / "storage" / "broken.py"
    source.parent.mkdir(parents=True)
    source.write_text("def broken(:\n", encoding="utf-8")

    (item,) = AUDIT.audit_paths(tmp_path, (Path("eimemory/storage/broken.py"),))

    assert item.syntax_status == "invalid"
    assert item.disposition == "business_owner"
    try:
        AUDIT.validate_complete((item,))
    except ValueError as exc:
        assert "invalid syntax" in str(exc)
    else:
        raise AssertionError("invalid maintained source passed validation")


def test_python_string_literals_do_not_create_audit_signals(tmp_path: Path) -> None:
    source = tmp_path / "eimemory" / "core" / "markers.py"
    source.parent.mkdir(parents=True)
    source.write_text(
        "ENTRY_MARKER = '[project.scripts]'\n"
        "UNFINISHED_MARKER = 'TODO'\n"
        "MAIN_MARKER = 'if __name__ == \\\"__main__\\\"'\n",
        encoding="utf-8",
    )

    (item,) = AUDIT.audit_paths(tmp_path, (Path("eimemory/core/markers.py"),))

    assert item.entry_signals == ()
    assert item.risk_signals == ()


def test_tracked_inventory_ignores_files_deleted_in_worktree(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source = tmp_path / "eimemory" / "api" / "live.py"
    source.parent.mkdir(parents=True)
    source.write_text("VALUE = 1\n", encoding="utf-8")

    class Result:
        stdout = b"eimemory/api/live.py\0deploy/deleted.py\0"

    monkeypatch.setattr(AUDIT.subprocess, "run", lambda *args, **kwargs: Result())

    assert AUDIT.tracked_maintained_paths(tmp_path) == (Path("eimemory/api/live.py"),)


def test_repository_inventory_has_no_unclassified_maintained_source() -> None:
    paths = AUDIT.tracked_maintained_paths(ROOT)
    items = AUDIT.audit_paths(ROOT, paths)

    AUDIT.validate_complete(items)
    assert paths
    assert Path("pyproject.toml") in paths
    pyproject = next(item for item in items if item.path == "pyproject.toml")
    assert pyproject.entry_signals == ("console_script", "package_entry_point")
    assert {item.disposition for item in items} >= {
        "business_owner",
        "entry_or_adapter",
        "shared_contract",
        "operational_gate",
        "compatibility_surface",
    }
