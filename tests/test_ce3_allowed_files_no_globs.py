"""CE-3: v1 allowed_files must be exact paths — no model-declared globs."""
from __future__ import annotations

from eimemory.governance.promotion_manager import (
    _code_patch_contract_error,
    _exact_allowed_files,
    _path_allowed,
)


def test_glob_allowlist_rejected() -> None:
    patch = {"allowed_files": ["tests/**", "module.py"]}
    updates = [{"path": "module.py", "content": "x=1\n"}]
    assert _exact_allowed_files(patch, updates) == "code_patch_allowed_files_glob_not_allowed"
    assert _path_allowed("tests/conftest.py", ["tests/**"]) is False
    assert _path_allowed("module.py", ["module.py"]) is True


def test_exact_allowlist_must_cover_updates() -> None:
    patch = {"allowed_files": ["other.py"]}
    updates = [{"path": "module.py", "content": "x=1\n"}]
    err = _exact_allowed_files(patch, updates)
    assert isinstance(err, str) and err.startswith("code_patch_path_not_in_allowed_files")


def test_contract_rejects_glob(tmp_path, monkeypatch) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "module.py").write_text("x=1\n", encoding="utf-8")
    monkeypatch.setenv("EIMEMORY_AUTONOMOUS_CODE_REPO", str(repo))
    patch = {
        "repo_root": str(repo),
        "allowed_files": ["**/*.py"],
        "file_updates": [{"path": "module.py", "content": "x=2\n"}],
        "verification_commands": [["python", "-m", "compileall", "module.py"]],
    }
    from eimemory.governance.promotion_manager import _file_updates

    updates = _file_updates(patch)
    err = _code_patch_contract_error(patch, repo_root=repo, file_updates=updates)
    assert err == "code_patch_allowed_files_glob_not_allowed"


def test_exact_paths_pass_resolution() -> None:
    patch = {"allowed_files": ["module.py", "pkg/util.py"]}
    updates = [
        {"path": "module.py", "content": "x=1\n"},
        {"path": "pkg/util.py", "content": "y=2\n"},
    ]
    resolved = _exact_allowed_files(patch, updates)
    assert resolved == ["module.py", "pkg/util.py"]
