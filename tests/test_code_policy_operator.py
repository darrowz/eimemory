import json
import os
import subprocess
from pathlib import Path

import pytest
from test_code_automation_policy_issue import policy_repo

from eimemory.governance.evolution import code_automation_policy_issue as issuer


@pytest.mark.parametrize("digest", ["", "a" * 63, "A" * 64, "g" * 64, True])
def test_invalid_incident_digest_cannot_touch_an_install_destination(tmp_path, digest):
    destination = tmp_path / "not-created" / "policy.json"
    report = issuer.issue_code_automation_policy(
        repo_root=tmp_path, incident_class="deployment.runtime_commit_drift", detector_id="detector.test",
        incident_digest=digest, install_path=destination)
    assert report["ok"] is False and report["reason"] == "incident_digest_invalid"
    assert not destination.parent.exists()


def test_cli_policy_issue_forwards_exact_digest_and_does_not_construct_runtime(monkeypatch, capsys):
    from eimemory.cli import main as cli
    calls = []
    monkeypatch.setattr(cli.Runtime, "create", lambda **_kw: pytest.fail("policy tool constructed Runtime"))
    monkeypatch.setattr(cli, "load_settings", lambda: pytest.fail("policy tool entered ordinary runtime settings"))
    def issue(**kwargs):
        calls.append(kwargs)
        return {"ok": kwargs["max_transactions"] == 1}
    monkeypatch.setattr("eimemory.governance.code_automation_policy_issue.issue_code_automation_policy", issue)
    base = ["learn", "code-evolution-policy-issue", "--incident-class", "deployment.runtime_commit_drift",
            "--detector-id", "eimemory.runtime_identity_drift.v1", "--incident-digest", "a" * 64]
    assert cli.main(base) == 0
    assert calls[0]["incident_digest"] == "a" * 64
    assert calls[0]["effects_mode"] == "all-disabled" and calls[0]["install_path"] is None
    assert cli.main(base + ["--max-transactions", "0"]) == 1
    assert calls[1]["max_transactions"] == 0
    capsys.readouterr()


def test_cli_readonly_validation_never_constructs_a_runtime(monkeypatch, capsys):
    from eimemory.cli import main as cli
    calls = []
    monkeypatch.setattr(cli.Runtime, "create", lambda **_kw: pytest.fail("validation constructed Runtime"))
    monkeypatch.setattr(cli, "load_settings", lambda: pytest.fail("validation entered runtime settings"))
    def inspect(**kwargs):
        calls.append(kwargs)
        return {"ok": True, "read_only": True, "current_incident_verified": False}
    monkeypatch.setattr("eimemory.governance.code_automation_policy_issue.inspect_code_automation_policy", inspect)
    assert cli.main(["learn", "code-evolution-policy-validate", "--path", "private.json", "--incident-digest", "b" * 64]) == 0
    assert calls == [{"path": "private.json", "incident_digest": "b" * 64}]
    assert json.loads(capsys.readouterr().out)["current_incident_verified"] is False


@pytest.mark.parametrize("actual,expected,accepted,bound", [
    ("a" * 64, "a" * 64, True, True), ("a" * 64, "b" * 64, False, True),
    (None, None, True, False),
])
def test_inspection_delegates_to_native_authority_and_does_not_certify_an_incident(monkeypatch, actual, expected, accepted, bound):
    calls = []
    def load(**kwargs):
        calls.append(kwargs)
        return {"ok": True, "status": "enabled", "incident": {"incident_digest": actual}}
    monkeypatch.setattr("eimemory.governance.evolution.code_automation_policy.load_code_automation_policy", load)
    report = issuer.inspect_code_automation_policy(path="private.json", incident_digest=expected)
    assert calls == [{"path": "private.json"}]
    assert report["ok"] is accepted and report["system_route_policy_bound"] is bound
    assert report["native_policy_valid"] is True and report["current_incident_verified"] is False


def test_bad_validation_digest_stops_before_policy_read(monkeypatch):
    monkeypatch.setattr("eimemory.governance.evolution.code_automation_policy.load_code_automation_policy",
                        lambda **_kw: pytest.fail("invalid digest read authority"))
    assert issuer.inspect_code_automation_policy(incident_digest="bad")["reason"] == "incident_digest_invalid"


def test_atomic_policy_install_preserves_linked_original_and_private_complete_bytes(tmp_path):
    original = tmp_path / "original.json"; original.write_text("old-private-bytes", encoding="utf-8")
    destination = tmp_path / "policy.json"
    try:
        os.link(original, destination)
    except OSError:
        pytest.skip("hard links unavailable")
    assert issuer._install_policy_atomically(destination, {"policy": "complete"}) == ""
    assert original.read_text(encoding="utf-8") == "old-private-bytes"
    assert json.loads(destination.read_text(encoding="utf-8")) == {"policy": "complete"}
    if os.name == "posix":
        assert destination.stat().st_mode & 0o777 == 0o600
    assert not list(tmp_path.glob(".code-automation-policy-*.tmp"))


def test_policy_install_rejects_symlink_without_changing_target(tmp_path):
    original = tmp_path / "original.json"; original.write_text("original", encoding="utf-8")
    destination = tmp_path / "policy.json"
    try:
        destination.symlink_to(original)
    except OSError:
        pytest.skip("symlink creation unavailable")
    assert issuer._install_policy_atomically(destination, {"new": True}) == "policy_symlink_rejected"
    assert original.read_text(encoding="utf-8") == "original" and destination.is_symlink()


def test_policy_install_failure_never_truncates_old_policy(tmp_path, monkeypatch):
    destination = tmp_path / "policy.json"; destination.write_text("original", encoding="utf-8")
    def fail(_descriptor):
        raise OSError("injected fsync failure")
    monkeypatch.setattr(issuer.os, "fsync", fail)
    with pytest.raises(OSError):
        issuer._install_policy_atomically(destination, {"new": True})
    assert destination.read_text(encoding="utf-8") == "original"
    assert not list(tmp_path.glob(".code-automation-policy-*.tmp"))


def test_current_native_policy_binds_exact_digest_and_passes_the_same_loader(tmp_path, monkeypatch, policy_repo):
    if os.name != "posix":
        pytest.skip("native policy owner authority requires POSIX")
    monkeypatch.setattr("eimemory.config.trusted.trusted_repository_root", lambda: policy_repo)
    monkeypatch.setattr("eimemory.config.trusted.trusted_remote", lambda: "origin")
    monkeypatch.setattr("eimemory.config.trusted.trusted_branch_allowed", lambda _branch: True)
    destination = tmp_path / "current-policy.json"
    report = issuer.issue_code_automation_policy(
        repo_root=policy_repo, incident_class="deployment.runtime_commit_drift",
        detector_id="eimemory.runtime_identity_drift.v1", incident_digest="a" * 64,
        test_plan_id="deployment.runtime-identity-drift.v1", install_path=destination)
    assert report["ok"] is True, report
    assert report["policy"]["incident"]["incident_digest"] == "a" * 64
    assert report["policy"]["max_transactions"] == 1 and not any(report["policy"]["effects"].values())
    loaded = issuer.inspect_code_automation_policy(path=destination, incident_digest="a" * 64)
    assert loaded["ok"] is True and loaded["system_route_policy_bound"] is True, loaded
    assert loaded["current_incident_verified"] is False
    mismatch = issuer.inspect_code_automation_policy(path=destination, incident_digest="b" * 64)
    assert mismatch["ok"] is False and mismatch["reason"] == "incident_digest_mismatch"


def test_dirty_repository_cannot_publish_a_policy(tmp_path, monkeypatch, policy_repo):
    monkeypatch.setattr("eimemory.config.trusted.trusted_repository_root", lambda: policy_repo)
    readme = policy_repo / "README.md"
    readme.write_text(readme.read_text(encoding="utf-8") + "\ncontrolled dirty repository\n", encoding="utf-8")
    destination = tmp_path / "current-policy.json"; destination.write_bytes(b"existing-private-policy")
    report = issuer.issue_code_automation_policy(
        repo_root=policy_repo, incident_class="deployment.runtime_commit_drift",
        detector_id="eimemory.runtime_identity_drift.v1", incident_digest="a" * 64,
        test_plan_id="deployment.runtime-identity-drift.v1", install_path=destination)
    assert report["ok"] is False and report["reason"] == "repository_not_clean"
    assert destination.read_bytes() == b"existing-private-policy"


def test_issuer_binds_the_configured_remote_url_not_origin(monkeypatch, policy_repo):
    from eimemory.governance.evolution.code_evolution_repository import remote_url_digest
    configured = "https://example.test/authorized/eimemory.git"
    subprocess.run(["git", "-C", str(policy_repo), "remote", "add", "authorized", configured], check=True)
    monkeypatch.setattr("eimemory.config.trusted.trusted_repository_root", lambda: policy_repo)
    monkeypatch.setattr("eimemory.config.trusted.trusted_remote", lambda: "authorized")
    report = issuer.issue_code_automation_policy(
        repo_root=policy_repo, incident_class="deployment.runtime_commit_drift",
        detector_id="eimemory.runtime_identity_drift.v1", incident_digest="a" * 64,
        test_plan_id="deployment.runtime-identity-drift.v1")
    assert report["ok"] is True, report
    assert report["policy"]["repository"]["remote"] == "authorized"
    assert report["policy"]["repository"]["remote_url_digest"] == remote_url_digest(configured)


def test_detached_repository_cannot_publish_policy_or_replace_original(tmp_path, monkeypatch, policy_repo):
    monkeypatch.setattr("eimemory.config.trusted.trusted_repository_root", lambda: policy_repo)
    subprocess.run(["git", "-C", str(policy_repo), "checkout", "--quiet", "--detach"], check=True)
    destination = tmp_path / "original-policy.json"
    destination.write_bytes(b"original-private-policy")
    report = issuer.issue_code_automation_policy(
        repo_root=policy_repo, incident_class="deployment.runtime_commit_drift",
        detector_id="eimemory.runtime_identity_drift.v1", incident_digest="a" * 64,
        test_plan_id="deployment.runtime-identity-drift.v1", install_path=destination)
    assert report["ok"] is False and report["reason"].startswith("git_identity_unavailable:"), report
    assert destination.read_bytes() == b"original-private-policy"
