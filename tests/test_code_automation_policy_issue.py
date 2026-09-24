from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from eimemory.governance.code_automation_policy import (
    load_code_automation_policy,
)
from eimemory.governance.code_automation_policy_issue import (
    issue_code_automation_policy,
)
from eimemory.governance.code_evolution_observation import DEFAULT_OBSERVATION_SECONDS
from eimemory.adapters.hermes.code_implementation import BINDING_ID, REVISION_ID


@pytest.fixture
def policy_repo(tmp_path):
    # Exercise real Git digests without depending on a machine-specific checkout.
    repo = tmp_path / "repository"
    subprocess.run(["git", "clone", "--quiet", "--shared",
                    str(Path(__file__).resolve().parents[1]), str(repo)], check=True)
    return repo


def test_issue_fills_digests_and_v10_ids(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, policy_repo) -> None:
    repo = policy_repo
    monkeypatch.setenv("EIMEMORY_TRUSTED_REPOSITORY_ROOT", str(repo))
    # Match loader expectation used in this environment.
    monkeypatch.setattr(
        "eimemory.config.trusted.trusted_repository_root",
        lambda: repo,
    )
    monkeypatch.setattr("eimemory.config.trusted.trusted_remote", lambda: "origin")
    monkeypatch.setattr("eimemory.config.trusted.trusted_branch_allowed", lambda _branch: True)

    report = issue_code_automation_policy(
        repo_root=repo,
        incident_class="l5.product_completion_semantic_misreport",
        detector_id="detector.test",
        test_plan_id="l5.product-completion-reporting.v1",
        effects_mode="all-disabled",
        policy_id="issued-test-bootstrap",
        not_before="2026-09-23T00:00:00Z",
        expires_at="2099-09-25T00:00:00Z",
    )
    assert report["ok"] is True
    policy = report["policy"]
    assert policy["capability"]["revision_id"] == REVISION_ID
    assert policy["capability"]["binding_id"] == BINDING_ID
    assert len(policy["repository"]["base_commit"]) == 40
    assert len(policy["repository"]["base_tree_digest"]) == 64
    assert len(policy["repository"]["remote_url_digest"]) == 64
    assert len(policy["deployment"]["installer_digest"]) == 64
    assert len(policy["verification"]["test_plan_digest"]) == 64
    assert policy["deployment"]["observation_seconds"] == DEFAULT_OBSERVATION_SECONDS == 28_800
    assert policy["effects"] == {
        "commit": False,
        "push": False,
        "deployment": False,
        "rollback": False,
        "sedimentation": False,
    }
    assert report["written_path"] == ""

    path = tmp_path / "policy.json"
    path.write_text(json.dumps(policy), encoding="utf-8")
    os.chmod(path, 0o600)
    monkeypatch.setattr(
        "eimemory.governance.code_automation_policy._secure_read_v2_policy",
        lambda _path: (json.dumps(policy), ""),
    )
    loaded = load_code_automation_policy(path=path, checked_at="2026-09-23T12:00:00Z")
    assert loaded["ok"] is True, loaded


def test_issue_commit_push_only_and_full_modes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, policy_repo) -> None:
    repo = policy_repo
    monkeypatch.setattr("eimemory.config.trusted.trusted_repository_root", lambda: repo)
    monkeypatch.setattr("eimemory.config.trusted.trusted_remote", lambda: "origin")
    monkeypatch.setattr("eimemory.config.trusted.trusted_branch_allowed", lambda _branch: True)

    mid = issue_code_automation_policy(
        repo_root=repo,
        incident_class="l5.product_completion_semantic_misreport",
        detector_id="detector.test",
        test_plan_id="l5.product-completion-reporting.v1",
        effects_mode="commit-push-only",
        not_before="2026-09-23T00:00:00Z",
        expires_at="2099-09-25T00:00:00Z",
        install_path=tmp_path / "mid.json",
    )
    assert mid["ok"] is True
    assert mid["policy"]["effects"]["commit"] is True
    assert mid["policy"]["effects"]["push"] is True
    assert mid["policy"]["effects"]["deployment"] is False
    assert Path(mid["written_path"]).is_file()

    full = issue_code_automation_policy(
        repo_root=repo,
        incident_class="l5.product_completion_semantic_misreport",
        detector_id="detector.test",
        test_plan_id="l5.product-completion-reporting.v1",
        effects_mode="full",
        not_before="2026-09-23T00:00:00Z",
        expires_at="2099-09-25T00:00:00Z",
    )
    assert full["ok"] is True
    assert all(full["policy"]["effects"].values())
