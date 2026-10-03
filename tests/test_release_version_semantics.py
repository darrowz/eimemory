from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

from eimemory.governance.release import release_impact as impact
from eimemory.governance.release import release_lineage as lineage


def _git(repo: Path, *args: str) -> str:
    return subprocess.check_output(
        ["git", "-C", str(repo), *args], text=True
    ).strip()


def _repo(tmp_path: Path) -> Path:
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.name", "Test")
    _git(tmp_path, "config", "user.email", "test@example.com")
    return tmp_path


def _commit(repo: Path, path: str, content: str) -> str:
    target = repo / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "test fixture")
    return _git(repo, "rev-parse", "HEAD")


@pytest.mark.parametrize(
    ("before", "after", "changed"),
    [
        ('__version__ = "1.0.0"\n', '__version__ = "1.0.1"\n', False),
        ('__version__: str = "1.0.0"\n', '__version__: str = "1.0.1"\n', False),
        ('__version__ = "1.0.0"\n', '__version__ = str(123)\n', True),
        ('__version__ = str(123)\n', '__version__ = str(456)\n', True),
        ('__version__: str = "1.0.0"\n', '__version__: str = str(123)\n', True),
        ('__version__ = 1\n', '__version__ = 2\n', True),
        ('__version__: str\n', '__version__: str = "1.0.0"\n', True),
        ('FLAG = __version__ = "stable"\n', 'FLAG = __version__ = "preview"\n', True),
        ('def feature():\n    __version__ = "before"\n    return __version__\n',
         'def feature():\n    __version__ = "after"\n    return __version__\n', True),
        ('class Feature:\n    __version__ = "before"\n',
         'class Feature:\n    __version__ = "after"\n', True),
        ('if True:\n    __version__ = "before"\n',
         'if True:\n    __version__ = "after"\n', True),
        ('__version__ = "before"\nFLAG = __version__\n__version__ = "1.0.0"\n',
         '__version__ = "after"\nFLAG = __version__\n__version__ = "1.0.0"\n', True),
        ('__version__ = "1.0.0"\nFLAG = True\n',
         '__version__ = "1.0.1"\nFLAG = False\n', True),
    ],
)
def test_version_normalization_preserves_non_metadata_semantics(
    tmp_path: Path, before: str, after: str, changed: bool
) -> None:
    # Never import or execute fixture source; only parse it and compare Git blobs.
    repo = _repo(tmp_path)
    prior = _commit(repo, "eimemory/version.py", before)
    current = _commit(repo, "eimemory/version.py", after)

    result = impact.release_impact(repo, ancestor=prior, current=current)
    assert result["requires_closure"] is changed
    assert result["affected_domains"] == (sorted(impact.DOMAINS) if changed else [])
    assert result["unknown_production_paths"] == []

    normalized_before = lineage._normalized_release_metadata_at_commit(
        repo, commit=prior, path="eimemory/version.py"
    )
    normalized_after = lineage._normalized_release_metadata_at_commit(
        repo, commit=current, path="eimemory/version.py"
    )
    assert normalized_before is not None and normalized_after is not None
    assert (normalized_before != normalized_after) is changed
    for domain in impact.DOMAINS:
        summary = lineage._domain_change_summary(
            repo, domain=domain, ancestor=prior, current=current
        )
        assert summary is not None and summary["changed"] is changed


def test_lineage_and_impact_share_the_version_normalizer() -> None:
    assert lineage._normalized_version_module is impact._normalized_version_module


@pytest.mark.parametrize(
    ("action", "destination", "domains"),
    [
        ("rename", "docs/retired_engine.py", {"memory.recall"}),
        ("rename", "eimemory/storage/new_store.py", {"memory.recall", "storage.integrity"}),
        ("delete", None, {"memory.recall"}),
        ("copy", "docs/engine_example.py", set()),
        ("copy", "eimemory/storage/new_store.py", {"storage.integrity"}),
    ],
)
def test_release_path_changes_keep_source_and_destination_domains(
    tmp_path: Path, action: str, destination: str | None, domains: set[str]
) -> None:
    repo = _repo(tmp_path)
    source = "eimemory/retrieval/engine.py"
    prior = _commit(repo, source, "def execute():\n    return 42\n")
    # Make the source-path contract independent of operator rename preferences.
    _git(repo, "config", "diff.renames", "copies")
    if destination is not None:
        (repo / destination).parent.mkdir(parents=True, exist_ok=True)
    if action == "rename":
        _git(repo, "mv", source, destination)
    elif action == "delete":
        _git(repo, "rm", source)
    else:
        shutil.copyfile(repo / source, repo / destination)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", action)
    current = _git(repo, "rev-parse", "HEAD")

    result = impact.release_impact(repo, ancestor=prior, current=current)
    assert result["requires_closure"] is bool(domains)
    assert result["affected_domains"] == sorted(domains)
    assert result["unknown_production_paths"] == []
    expected_paths = {source} if action == "delete" else {destination}
    if action == "rename":
        expected_paths.add(source)
    assert {entry["path"] for entry in result["paths"]} == expected_paths

    command = Path(__file__).resolve().parents[1] / "deploy/release_impact.py"
    completed = subprocess.run(
        [sys.executable, "-I", "-B", str(command), "--repository", str(repo),
         "--prior-commit", prior, "--current-commit", current],
        capture_output=True, text=True, check=False,
    )
    assert completed.returncode == (0 if domains else 1), completed.stderr
    assert json.loads(completed.stdout) == result
