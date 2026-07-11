from __future__ import annotations

from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import subprocess
from threading import Thread

import pytest

from eimemory.api.runtime import Runtime
from eimemory.cli.main import main as cli_main
from eimemory.governance.deployment_receipt import verify_and_record_deployment


SCOPE = {"agent_id": "agent-deployment", "workspace_id": "deployment-receipt", "user_id": "darrow"}


def test_deployment_receipt_reads_and_cross_checks_live_release_evidence(tmp_path) -> None:
    repo, prior_commit, head_commit = _git_release_repo(tmp_path, version="9.8.7")
    release_dir, current_link = _release_link(tmp_path, head_commit, repo=repo)
    health = _health_payload(
        commit=head_commit,
        version="9.8.7",
        current_link=current_link,
        release_dir=release_dir,
    )
    runtime = Runtime.create(root=tmp_path / "runtime")
    try:
        with _health_server(health) as health_url:
            report = verify_and_record_deployment(
                runtime,
                scope=SCOPE,
                repo_root=repo,
                current_link=current_link,
                health_url=health_url,
                prior_commit=prior_commit,
            )
        records = runtime.store.list_records(kinds=["promotion_request"], scope=SCOPE, limit=10)
    finally:
        runtime.close()

    assert report["ok"] is True
    assert report["commit"] == head_commit
    assert report["version"] == "9.8.7"
    assert Path(report["release_path"]).resolve() == release_dir.resolve()
    assert report["prior_commit"] == prior_commit
    assert report["promotion_request_id"]
    assert len(records) == 1
    receipt = records[0]
    assert receipt.status == "deployed"
    assert receipt.content["candidate_id"] == f"deployment:{head_commit}"
    assert receipt.content["side_effect"]["deployment_executed"] is True
    assert receipt.content["side_effect"]["post_deploy_health"]["commit"] == head_commit
    rollback = receipt.content["side_effect"]["rollback_evidence"]
    assert rollback["prior_commit_sha"] == prior_commit
    assert rollback["strategy"] == "install_prior_immutable_release_restart_and_health_check"
    assert rollback["commands"] == [
        ["bash", str(repo / "deploy" / "install_immutable_release.sh"), prior_commit],
        ["systemctl", "--user", "restart", "eimemory-rpc.service"],
        ["curl", "-fsS", report["health_url"]],
    ]


@pytest.mark.parametrize("mismatch", ["status_only", "commit", "version", "release"])
def test_deployment_receipt_rejects_status_only_or_mismatched_identity(tmp_path, mismatch) -> None:
    repo, prior_commit, head_commit = _git_release_repo(tmp_path, version="9.8.7")
    release_dir, current_link = _release_link(tmp_path, head_commit, repo=repo)
    health = _health_payload(
        commit=head_commit,
        version="9.8.7",
        current_link=current_link,
        release_dir=release_dir,
    )
    if mismatch == "status_only":
        health = {"ok": True, "status": "healthy"}
    elif mismatch == "commit":
        health["commit"] = "f" * 40
    elif mismatch == "version":
        health["version"] = "9.8.6"
    else:
        health["paths"]["release"] = str(tmp_path / "some-other-release")

    runtime = Runtime.create(root=tmp_path / "runtime")
    try:
        with _health_server(health) as health_url:
            report = verify_and_record_deployment(
                runtime,
                scope=SCOPE,
                repo_root=repo,
                current_link=current_link,
                health_url=health_url,
                prior_commit=prior_commit,
            )
        records = runtime.store.list_records(kinds=["promotion_request"], scope=SCOPE, limit=10)
    finally:
        runtime.close()

    assert report["ok"] is False
    assert report["error"] in {
        "health_identity_missing",
        "health_commit_mismatch",
        "health_version_mismatch",
        "health_release_mismatch",
    }
    assert records == []


def test_deployment_receipt_requires_prior_commit_to_be_rollback_ancestor(tmp_path) -> None:
    repo, _prior_commit, head_commit = _git_release_repo(tmp_path, version="9.8.7")
    release_dir, current_link = _release_link(tmp_path, head_commit, repo=repo)
    runtime = Runtime.create(root=tmp_path / "runtime")
    try:
        with _health_server(
            _health_payload(commit=head_commit, version="9.8.7", current_link=current_link, release_dir=release_dir)
        ) as health_url:
            report = verify_and_record_deployment(
                runtime,
                scope=SCOPE,
                repo_root=repo,
                current_link=current_link,
                health_url=health_url,
                prior_commit="e" * 40,
            )
    finally:
        runtime.close()

    assert report == {"ok": False, "error": "prior_commit_not_rollback_ancestor"}


def test_deployment_receipt_reads_version_from_head_not_dirty_worktree(tmp_path) -> None:
    repo, prior_commit, head_commit = _git_release_repo(tmp_path, version="9.8.7")
    (repo / "pyproject.toml").write_text(
        '[project]\nname = "deployment-test"\nversion = "9.8.8"\n',
        encoding="utf-8",
    )
    release_dir, current_link = _release_link(tmp_path, head_commit, repo=repo)
    runtime = Runtime.create(root=tmp_path / "runtime")
    try:
        with _health_server(
            _health_payload(commit=head_commit, version="9.8.8", current_link=current_link, release_dir=release_dir)
        ) as health_url:
            report = verify_and_record_deployment(
                runtime,
                scope=SCOPE,
                repo_root=repo,
                current_link=current_link,
                health_url=health_url,
                prior_commit=prior_commit,
            )
    finally:
        runtime.close()

    assert report == {"ok": False, "error": "health_version_mismatch"}


def test_deployment_receipt_idempotency_includes_prior_rollback_commit(tmp_path) -> None:
    repo, first_prior, second_prior = _git_release_repo(tmp_path, version="9.8.7")
    (repo / "README.md").write_text("third release\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-m", "third release")
    head_commit = _git(repo, "rev-parse", "HEAD")
    release_dir, current_link = _release_link(tmp_path, head_commit, repo=repo)
    runtime = Runtime.create(root=tmp_path / "runtime")
    try:
        with _health_server(
            _health_payload(commit=head_commit, version="9.8.7", current_link=current_link, release_dir=release_dir)
        ) as health_url:
            first = verify_and_record_deployment(
                runtime,
                scope=SCOPE,
                repo_root=repo,
                current_link=current_link,
                health_url=health_url,
                prior_commit=first_prior,
            )
            second = verify_and_record_deployment(
                runtime,
                scope=SCOPE,
                repo_root=repo,
                current_link=current_link,
                health_url=health_url,
                prior_commit=second_prior,
            )
        records = runtime.store.list_records(kinds=["promotion_request"], scope=SCOPE, limit=10)
    finally:
        runtime.close()

    assert first["promotion_request_id"] != second["promotion_request_id"]
    assert {
        record.content["side_effect"]["rollback_evidence"]["prior_commit_sha"]
        for record in records
    } == {first_prior, second_prior}


def test_deployment_receipt_rejects_non_http_health_url(tmp_path) -> None:
    repo, prior_commit, head_commit = _git_release_repo(tmp_path, version="9.8.7")
    _release_dir, current_link = _release_link(tmp_path, head_commit, repo=repo)
    runtime = Runtime.create(root=tmp_path / "runtime")
    try:
        report = verify_and_record_deployment(
            runtime,
            scope=SCOPE,
            repo_root=repo,
            current_link=current_link,
            health_url=(tmp_path / "health.json").as_uri(),
            prior_commit=prior_commit,
        )
    finally:
        runtime.close()

    assert report == {"ok": False, "error": "health_url_scheme_not_allowed"}


def test_deployment_receipt_rejects_release_outside_trusted_root(tmp_path) -> None:
    repo, prior_commit, head_commit = _git_release_repo(tmp_path, version="9.8.7")
    outside_release, current_link = _release_link(
        tmp_path,
        head_commit,
        repo=repo,
        release_root=tmp_path / "outside-releases",
    )
    runtime = Runtime.create(root=tmp_path / "runtime")
    try:
        with _health_server(
            _health_payload(commit=head_commit, version="9.8.7", current_link=current_link, release_dir=outside_release)
        ) as health_url:
            report = verify_and_record_deployment(
                runtime,
                scope=SCOPE,
                repo_root=repo,
                current_link=current_link,
                health_url=health_url,
                prior_commit=prior_commit,
            )
    finally:
        runtime.close()

    assert report == {"ok": False, "error": "current_release_untrusted"}


def test_deployment_receipt_rejects_release_symlink_escape_from_trusted_root(tmp_path) -> None:
    repo, prior_commit, head_commit = _git_release_repo(tmp_path, version="9.8.7")
    outside_release, _outside_link = _release_link(
        tmp_path,
        head_commit,
        repo=repo,
        release_root=tmp_path / "outside-releases",
        link_name="outside-current",
    )
    trusted_root = tmp_path / "releases"
    trusted_root.mkdir()
    _create_dir_link(trusted_root / head_commit, outside_release)
    current_link = tmp_path / "current"
    _create_dir_link(current_link, trusted_root / head_commit)
    runtime = Runtime.create(root=tmp_path / "runtime")
    try:
        with _health_server(
            _health_payload(commit=head_commit, version="9.8.7", current_link=current_link, release_dir=outside_release)
        ) as health_url:
            report = verify_and_record_deployment(
                runtime,
                scope=SCOPE,
                repo_root=repo,
                current_link=current_link,
                health_url=health_url,
                prior_commit=prior_commit,
            )
    finally:
        runtime.close()

    assert report == {"ok": False, "error": "current_release_untrusted"}


def test_deployment_receipt_rejects_release_identity_files_not_matching_head(tmp_path) -> None:
    repo, prior_commit, head_commit = _git_release_repo(tmp_path, version="9.8.7")
    release_dir, current_link = _release_link(tmp_path, head_commit, repo=repo)
    (release_dir / "pyproject.toml").write_text(
        '[project]\nname = "deployment-test"\nversion = "forged"\n',
        encoding="utf-8",
    )
    runtime = Runtime.create(root=tmp_path / "runtime")
    try:
        with _health_server(
            _health_payload(commit=head_commit, version="9.8.7", current_link=current_link, release_dir=release_dir)
        ) as health_url:
            report = verify_and_record_deployment(
                runtime,
                scope=SCOPE,
                repo_root=repo,
                current_link=current_link,
                health_url=health_url,
                prior_commit=prior_commit,
            )
    finally:
        runtime.close()

    assert report == {"ok": False, "error": "release_identity_mismatch", "path": "pyproject.toml"}


def test_deployment_receipt_rejects_oversized_health_response(tmp_path) -> None:
    repo, prior_commit, head_commit = _git_release_repo(tmp_path, version="9.8.7")
    release_dir, current_link = _release_link(tmp_path, head_commit, repo=repo)
    health = _health_payload(commit=head_commit, version="9.8.7", current_link=current_link, release_dir=release_dir)
    health["padding"] = "x" * 70_000
    runtime = Runtime.create(root=tmp_path / "runtime")
    try:
        with _health_server(health) as health_url:
            report = verify_and_record_deployment(
                runtime,
                scope=SCOPE,
                repo_root=repo,
                current_link=current_link,
                health_url=health_url,
                prior_commit=prior_commit,
            )
    finally:
        runtime.close()

    assert report == {"ok": False, "error": "health_response_too_large"}


def test_deployment_receipt_idempotency_binds_link_and_health_endpoint(tmp_path) -> None:
    repo, prior_commit, head_commit = _git_release_repo(tmp_path, version="9.8.7")
    release_dir, first_link = _release_link(tmp_path, head_commit, repo=repo, link_name="current-a")
    second_link = tmp_path / "current-b"
    _create_dir_link(second_link, release_dir)
    runtime = Runtime.create(root=tmp_path / "runtime")
    try:
        with _health_server(
            _health_payload(commit=head_commit, version="9.8.7", current_link=first_link, release_dir=release_dir)
        ) as first_url:
            first = verify_and_record_deployment(
                runtime,
                scope=SCOPE,
                repo_root=repo,
                current_link=first_link,
                health_url=first_url,
                prior_commit=prior_commit,
            )
        with _health_server(
            _health_payload(commit=head_commit, version="9.8.7", current_link=second_link, release_dir=release_dir)
        ) as second_url:
            second = verify_and_record_deployment(
                runtime,
                scope=SCOPE,
                repo_root=repo,
                current_link=second_link,
                health_url=second_url,
                prior_commit=prior_commit,
            )
        records = {
            record.record_id: record
            for record in runtime.store.list_records(kinds=["promotion_request"], scope=SCOPE, limit=10)
        }
    finally:
        runtime.close()

    assert first["promotion_request_id"] != second["promotion_request_id"]
    first_record = records[first["promotion_request_id"]]
    second_record = records[second["promotion_request_id"]]
    assert first_record.content["side_effect"]["deployment"]["current_link"] == first["current_link"]
    assert second_record.content["side_effect"]["deployment"]["current_link"] == second["current_link"]
    assert first_record.content["side_effect"]["post_deploy_health"]["url"] == first["health_url"]
    assert second_record.content["side_effect"]["post_deploy_health"]["url"] == second["health_url"]


def test_deployment_receipt_cli_persists_verified_receipt(tmp_path, monkeypatch, capsys) -> None:
    repo, prior_commit, head_commit = _git_release_repo(tmp_path, version="9.8.7")
    release_dir, current_link = _release_link(tmp_path, head_commit, repo=repo)
    monkeypatch.setenv("EIMEMORY_ROOT", str(tmp_path / "runtime"))

    with _health_server(
        _health_payload(commit=head_commit, version="9.8.7", current_link=current_link, release_dir=release_dir)
    ) as health_url:
        exit_code = cli_main(
            [
                "learn",
                "deployment-receipt",
                "--repo-root",
                str(repo),
                "--current-link",
                str(current_link),
                "--health-url",
                health_url,
                "--prior-commit",
                prior_commit,
                "--scope-agent",
                SCOPE["agent_id"],
                "--scope-workspace",
                SCOPE["workspace_id"],
                "--scope-user",
                SCOPE["user_id"],
                "--json",
            ]
        )
    payload = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert payload["ok"] is True
    assert payload["commit"] == head_commit
    assert payload["promotion_request_id"]


def _git_release_repo(tmp_path: Path, *, version: str) -> tuple[Path, str, str]:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "deployment@example.test")
    _git(repo, "config", "user.name", "Deployment Test")
    (repo / "README.md").write_text("prior\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-m", "prior")
    prior_commit = _git(repo, "rev-parse", "HEAD")
    (repo / "pyproject.toml").write_text(
        f'[project]\nname = "deployment-test"\nversion = "{version}"\n',
        encoding="utf-8",
    )
    version_file = repo / "eimemory" / "version.py"
    version_file.parent.mkdir(parents=True)
    version_file.write_text(f'__version__ = "{version}"\n', encoding="utf-8")
    _git(repo, "add", "pyproject.toml", "eimemory/version.py")
    _git(repo, "commit", "-m", "release")
    return repo, prior_commit, _git(repo, "rev-parse", "HEAD")


def _release_link(
    tmp_path: Path,
    commit: str,
    *,
    repo: Path,
    release_root: Path | None = None,
    link_name: str = "current",
) -> tuple[Path, Path]:
    release_dir = (release_root or tmp_path / "releases") / commit
    release_dir.mkdir(parents=True)
    (release_dir / "pyproject.toml").write_bytes(_git_bytes(repo, "show", f"{commit}:pyproject.toml"))
    version_file = release_dir / "eimemory" / "version.py"
    version_file.parent.mkdir(parents=True)
    version_file.write_bytes(_git_bytes(repo, "show", f"{commit}:eimemory/version.py"))
    current_link = tmp_path / link_name
    _create_dir_link(current_link, release_dir)
    return release_dir, current_link


def _create_dir_link(current_link: Path, release_dir: Path) -> None:
    try:
        current_link.symlink_to(release_dir, target_is_directory=True)
    except OSError:
        subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(current_link), str(release_dir)],
            check=True,
            capture_output=True,
            text=True,
        )


def _health_payload(*, commit: str, version: str, current_link: Path, release_dir: Path) -> dict:
    return {
        "ok": True,
        "service": "eimemory-rpc",
        "version": version,
        "commit": commit,
        "paths": {"current": str(current_link), "release": str(release_dir)},
        "checks": {"process": True, "store": True, "ready": True},
    }


@contextmanager
def _health_server(payload: dict):
    body = json.dumps(payload).encode("utf-8")

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, _format, *_args):
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/health"
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _git_bytes(repo: Path, *args: str) -> bytes:
    result = subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    return bytes(result.stdout)
