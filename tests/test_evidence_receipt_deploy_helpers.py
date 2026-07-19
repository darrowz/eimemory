from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys

import pytest

from deploy.ensure_evidence_receipt_key import (
    EvidenceReceiptKeyError,
    ensure_evidence_receipt_key_file,
)
from deploy.find_prior_immutable_release import (
    _python_runtime_works,
    _valid_release,
    find_prior_immutable_release,
)


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


def test_evidence_receipt_key_is_private_stable_and_never_returned(tmp_path: Path) -> None:
    target = tmp_path / "evidence-receipt.env"

    first = ensure_evidence_receipt_key_file(target)
    original = target.read_text(encoding="utf-8")
    second = ensure_evidence_receipt_key_file(target)

    assert first == {"ok": True, "created": True, "path": str(target)}
    assert second == {"ok": True, "created": False, "path": str(target)}
    assert "EIMEMORY_EVIDENCE_RECEIPT_HMAC_KEY=" in original
    assert original == target.read_text(encoding="utf-8")
    assert "key" not in first and "key" not in second
    if os.name == "posix":
        assert target.stat().st_mode & 0o777 == 0o600


def test_evidence_receipt_key_rejects_weak_existing_file(tmp_path: Path) -> None:
    target = tmp_path / "evidence-receipt.env"
    target.write_text("EIMEMORY_EVIDENCE_RECEIPT_HMAC_KEY=weak\n", encoding="utf-8")
    if os.name == "posix":
        target.chmod(0o600)

    with pytest.raises(EvidenceReceiptKeyError, match="weak or malformed"):
        ensure_evidence_receipt_key_file(target)


def test_evidence_receipt_key_creation_race_normalizes_winner(monkeypatch, tmp_path: Path) -> None:
    target = tmp_path / "evidence-receipt.env"

    def race_link(source, destination, *, follow_symlinks=False):
        del follow_symlinks
        Path(destination).write_bytes(Path(source).read_bytes())
        if os.name == "posix":
            Path(destination).chmod(0o640)
        raise FileExistsError

    monkeypatch.setattr(os, "link", race_link)
    report = ensure_evidence_receipt_key_file(target)

    assert report == {"ok": True, "created": False, "path": str(target)}
    assert "EIMEMORY_EVIDENCE_RECEIPT_HMAC_KEY=" in target.read_text(encoding="utf-8")
    if os.name == "posix":
        assert target.stat().st_mode & 0o777 == 0o600


def test_prior_release_selector_uses_latest_valid_receipt_not_directory_mtime(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    releases = tmp_path / "releases"
    repo.mkdir()
    releases.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "tests@example.com")
    _git(repo, "config", "user.name", "Tests")
    (repo / "file.txt").write_text("one", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "one")
    first = _git(repo, "rev-parse", "HEAD")
    (releases / first).mkdir()
    (repo / "file.txt").write_text("two", encoding="utf-8")
    _git(repo, "commit", "-am", "two")
    second = _git(repo, "rev-parse", "HEAD")
    (releases / second).mkdir()
    (repo / "file.txt").write_text("three", encoding="utf-8")
    _git(repo, "commit", "-am", "three")
    deployed = _git(repo, "rev-parse", "HEAD")
    (releases / deployed).mkdir()
    os.utime(releases / first, ns=(2_000_000_000, 2_000_000_000))
    os.utime(releases / second, ns=(1_000_000_000, 1_000_000_000))

    assert find_prior_immutable_release(
        releases_root=releases,
        repo_root=repo,
        deployed_commit=deployed,
        receipt_commits=[second, first],
        release_validator=lambda _repo, _release, _commit: True,
    ) == second
    assert find_prior_immutable_release(
        releases_root=releases,
        repo_root=repo,
        deployed_commit=deployed,
        receipt_commits=[],
        release_validator=lambda _repo, _release, _commit: True,
    ) == ""


def test_prior_release_selector_requires_exact_immutable_tree_and_runtime(monkeypatch, tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    releases = tmp_path / "releases"
    repo.mkdir()
    releases.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "tests@example.com")
    _git(repo, "config", "user.name", "Tests")
    (repo / "pyproject.toml").write_text(
        '[project]\nname = "selector-test"\nversion = "1.2.3"\n',
        encoding="utf-8",
    )
    version_file = repo / "eimemory" / "version.py"
    version_file.parent.mkdir()
    version_file.write_text('__version__ = "1.2.3"\n', encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "release")
    commit = _git(repo, "rev-parse", "HEAD")
    release = releases / commit
    (release / "eimemory").mkdir(parents=True)
    (release / "pyproject.toml").write_bytes(
        subprocess.run(
            ["git", "-C", str(repo), "show", f"{commit}:pyproject.toml"],
            check=True,
            capture_output=True,
        ).stdout
    )
    (release / "eimemory" / "version.py").write_bytes(
        subprocess.run(
            ["git", "-C", str(repo), "show", f"{commit}:eimemory/version.py"],
            check=True,
            capture_output=True,
        ).stdout
    )

    assert _valid_release(repo, release, commit) is False
    runtime = release / ".venv" / "bin" / "python"
    runtime.parent.mkdir(parents=True)
    runtime.write_text("", encoding="utf-8")
    assert _valid_release(repo, release, commit) is False
    monkeypatch.setattr("deploy.find_prior_immutable_release._python_runtime_works", lambda *_: True)
    assert _valid_release(repo, release, commit) is True

    (release / "untracked.txt").write_text("tampered", encoding="utf-8")
    assert _valid_release(repo, release, commit) is False


def test_prior_release_python_runtime_probe_rejects_non_executable_placeholder(tmp_path: Path) -> None:
    placeholder = tmp_path / "python"
    placeholder.write_text("", encoding="utf-8")

    assert _python_runtime_works(tmp_path, placeholder) is False
    assert _python_runtime_works(Path(sys.prefix), Path(sys.executable)) is True
