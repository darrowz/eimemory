#!/usr/bin/env python3
"""Select the newest related immutable release for receipt repair."""

from __future__ import annotations

import argparse
from collections.abc import Callable, Iterable
import os
from pathlib import Path
import re
import subprocess
import sys


COMMIT_RE = re.compile(r"[0-9a-fA-F]{40}")


def _git(repo: Path, *args: str) -> bool:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    ).returncode == 0


def _valid_release(repo_root: Path, release: Path, commit: str) -> bool:
    source_root = Path(__file__).resolve().parents[1]
    if str(source_root) not in sys.path:
        sys.path.insert(0, str(source_root))
    from eimemory.governance.deployment_receipt import valid_immutable_release_tree

    return bool(
        _python_runtime_works(release / ".venv", release / ".venv" / "bin" / "python")
        and valid_immutable_release_tree(repo=repo_root, release=release, commit=commit)
    )


def _python_runtime_works(venv_root: Path, interpreter: Path) -> bool:
    if not interpreter.is_file() or not os.access(interpreter, os.X_OK):
        return False
    probe = (
        "from pathlib import Path; import sys; "
        "raise SystemExit(0 if Path(sys.prefix).resolve() == Path(sys.argv[1]).resolve() else 1)"
    )
    try:
        result = subprocess.run(
            [str(interpreter), "-I", "-B", "-c", probe, str(venv_root)],
            check=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0


def find_prior_immutable_release(
    *,
    releases_root: Path,
    repo_root: Path,
    deployed_commit: str,
    receipt_commits: Iterable[str],
    release_validator: Callable[[Path, Path, str], bool] | None = None,
) -> str:
    target = str(deployed_commit or "").strip().lower()
    if not COMMIT_RE.fullmatch(target) or releases_root.is_symlink() or not releases_root.is_dir():
        return ""
    validate = release_validator or _valid_release
    for raw_commit in receipt_commits:
        commit = str(raw_commit or "").strip().lower()
        child = releases_root / commit
        if (
            commit == target
            or not COMMIT_RE.fullmatch(commit)
            or child.is_symlink()
            or not child.is_dir()
        ):
            continue
        if not _git(repo_root, "cat-file", "-e", f"{commit}^{{commit}}"):
            continue
        related = _git(repo_root, "merge-base", "--is-ancestor", commit, target) or _git(
            repo_root, "merge-base", "--is-ancestor", target, commit
        )
        if related and validate(repo_root, child, commit):
            return commit
    return ""


def trusted_receipt_commits(
    *,
    runtime_root: Path,
    repo_root: Path,
    scope_agent: str,
    scope_workspace: str,
    scope_user: str,
) -> list[str]:
    database = runtime_root / "state" / "eimemory.sqlite"
    if not database.is_file():
        return []
    source_root = Path(__file__).resolve().parents[1]
    if str(source_root) not in sys.path:
        sys.path.insert(0, str(source_root))
    from eimemory.api.runtime import Runtime
    from eimemory.governance.evidence_contract import verified_deployment_receipt_identity

    runtime = Runtime.create(root=runtime_root)
    try:
        records = runtime.store.list_records(
            kinds=["promotion_request"],
            scope={
                "agent_id": scope_agent,
                "workspace_id": scope_workspace,
                "user_id": scope_user,
            },
            limit=500,
        )
        return [
            identity.commit
            for record in records
            if (identity := verified_deployment_receipt_identity(record)) is not None
        ]
    finally:
        runtime.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--releases-root", required=True, type=Path)
    parser.add_argument("--repo-root", required=True, type=Path)
    parser.add_argument("--deployed-commit", required=True)
    parser.add_argument("--runtime-root", required=True, type=Path)
    parser.add_argument("--scope-agent", required=True)
    parser.add_argument("--scope-workspace", required=True)
    parser.add_argument("--scope-user", required=True)
    args = parser.parse_args(argv)
    commits = trusted_receipt_commits(
        runtime_root=args.runtime_root,
        repo_root=args.repo_root,
        scope_agent=args.scope_agent,
        scope_workspace=args.scope_workspace,
        scope_user=args.scope_user,
    )
    print(find_prior_immutable_release(
        releases_root=args.releases_root,
        repo_root=args.repo_root,
        deployed_commit=args.deployed_commit,
        receipt_commits=commits,
    ))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
