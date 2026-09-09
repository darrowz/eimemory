from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess
import sys

import pytest


def _git(repo: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    return repo


def _commit(repo: Path, path: str, content: str, message: str) -> str:
    target = repo / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    _git(repo, "add", "--", path)
    _git(repo, "commit", "-qm", message)
    return _git(repo, "rev-parse", "HEAD")


@pytest.mark.parametrize(
    ("path", "expected_domains", "expected_unknown", "expected_reason"),
    [
        (
            "eimemory/retrieval/engine.py",
            ["memory.recall"],
            [],
            "classified_production_change",
        ),
        (
            "eimemory/models/records.py",
            [
                "channel.delivery",
                "code.evolution",
                "deployment.runtime",
                "memory.governance",
                "memory.recall",
                "storage.integrity",
            ],
            [],
            "classified_production_change",
        ),
        (
            "eimemory/future_runtime.py",
            [],
            ["eimemory/future_runtime.py"],
            "unknown_production_change",
        ),
        (
            "deploy/release_impact.py",
            ["code.evolution", "deployment.runtime"],
            [],
            "classified_production_change",
        ),
        (
            "eimemory/governance/release_impact.py",
            ["code.evolution", "deployment.runtime", "memory.governance"],
            [],
            "classified_production_change",
        ),
    ],
)
def test_production_change_requires_closure_and_is_fail_closed(
    tmp_path: Path,
    path: str,
    expected_domains: list[str],
    expected_unknown: list[str],
    expected_reason: str,
) -> None:
    from eimemory.governance.release_impact import release_impact

    repo = _repo(tmp_path)
    prior = _commit(repo, "docs/baseline.md", "baseline\n", "prior")
    current = _commit(repo, path, "CURRENT = True\n", "current")

    impact = release_impact(repo, ancestor=prior, current=current)

    assert impact["requires_closure"] is True
    assert impact["affected_domains"] == expected_domains
    assert impact["unknown_production_paths"] == expected_unknown
    assert impact["reason"] == expected_reason
    assert impact["paths"] == [
        {
            "path": path,
            "domains": expected_domains,
            "classification": "unknown_production" if expected_unknown else "classified",
        }
    ]


def test_documentation_only_change_is_safely_lightweight(tmp_path: Path) -> None:
    from eimemory.governance.release_impact import release_impact

    repo = _repo(tmp_path)
    prior = _commit(repo, "docs/guide.md", "before\n", "prior")
    current = _commit(repo, "docs/guide.md", "after\n", "current")

    impact = release_impact(repo, ancestor=prior, current=current)

    assert impact == {
        "requires_closure": False,
        "reason": "lightweight_release",
        "affected_domains": [],
        "unknown_production_paths": [],
        "paths": [
            {
                "path": "docs/guide.md",
                "domains": [],
                "classification": "ignored",
            }
        ],
    }


def test_release_lineage_reexports_the_shared_impact_policy() -> None:
    import eimemory.governance.release_impact as impact
    import eimemory.governance.release_lineage as lineage

    assert lineage.DOMAINS is impact.DOMAINS
    assert lineage.DOMAIN_PATHS is impact.DOMAIN_PATHS
    assert lineage._release_change_summary is impact._release_change_summary
    assert lineage._domains_for_change is impact._domains_for_change


@pytest.mark.parametrize(
    ("path", "expected_status"),
    [
        ("docs/guide.md", 1),
        ("eimemory/retrieval/engine.py", 0),
        ("eimemory/future_runtime.py", 0),
    ],
)
def test_release_impact_cli_reports_bounded_classification_and_exit_status(
    tmp_path: Path,
    path: str,
    expected_status: int,
) -> None:
    repo = _repo(tmp_path)
    prior = _commit(repo, "docs/baseline.md", "baseline\n", "prior")
    current = _commit(repo, path, "changed\n", "current")

    completed = subprocess.run(
        [
            sys.executable,
            "-I",
            "-B",
            "deploy/release_impact.py",
            "--repository",
            str(repo),
            "--prior-commit",
            prior,
            "--current-commit",
            current,
        ],
        cwd=Path.cwd(),
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == expected_status, completed.stderr
    payload = json.loads(completed.stdout)
    assert set(payload) == {
        "affected_domains",
        "paths",
        "reason",
        "requires_closure",
        "unknown_production_paths",
    }
    assert payload["requires_closure"] is (expected_status == 0)


def test_release_impact_cli_fails_for_non_exact_commit(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    current = _commit(repo, "docs/baseline.md", "baseline\n", "current")

    completed = subprocess.run(
        [
            sys.executable,
            "-I",
            "-B",
            "deploy/release_impact.py",
            "--repository",
            str(repo),
            "--prior-commit",
            "HEAD",
            "--current-commit",
            current,
        ],
        cwd=Path.cwd(),
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 2
    assert completed.stdout == ""
    assert "release impact failed" in completed.stderr


def test_release_impact_cli_does_not_import_the_application_package(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    prior = _commit(repo, "docs/guide.md", "before\n", "prior")
    current = _commit(repo, "docs/guide.md", "after\n", "current")
    release = tmp_path / "release"
    (release / "deploy").mkdir(parents=True)
    (release / "eimemory" / "governance").mkdir(parents=True)
    shutil.copy2("deploy/release_impact.py", release / "deploy" / "release_impact.py")
    shutil.copy2(
        "eimemory/governance/release_impact.py",
        release / "eimemory" / "governance" / "release_impact.py",
    )
    (release / "eimemory" / "__init__.py").write_text(
        "raise RuntimeError('application package imported')\n",
        encoding="utf-8",
    )

    completed = subprocess.run(
        [
            sys.executable,
            "-I",
            "-B",
            str(release / "deploy" / "release_impact.py"),
            "--repo-root",
            str(repo),
            "--prior-commit",
            prior,
            "--current-commit",
            current,
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 1, completed.stderr
    assert json.loads(completed.stdout)["reason"] == "lightweight_release"
