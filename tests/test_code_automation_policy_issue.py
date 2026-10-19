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


def test_real_parallel_empty_policy_lock_initializes_only_after_acquisition(tmp_path, monkeypatch):
    import inspect
    import linecache
    import threading
    from eimemory.governance.evolution import code_automation_policy_issue as product
    original_fstat, original_write = os.fstat, os.write
    snapshots = threading.Barrier(2)
    acquired, contended, release = threading.Event(), threading.Event(), threading.Event()
    local = threading.local()
    errors, entered = [], []
    def checked_fstat(fd):
        info = original_fstat(fd)
        caller = inspect.currentframe().f_back
        if caller.f_code.co_name == '_policy_lock':
            line = linecache.getline(caller.f_code.co_filename, caller.f_lineno)
            if 'info = os.fstat' in line:
                local.initial = info
                snapshots.wait(4)
            elif 'os.fstat(descriptor).st_size' in line:
                return local.initial  # Both real openers observed the empty version.
        return info
    def checked_write(fd, raw):
        if inspect.currentframe().f_back.f_code.co_name == '_policy_lock':
            assert getattr(local, 'holds_lock', False), 'marker_write_before_real_lock'
        return original_write(fd, raw)
    if os.name == 'nt':
        import msvcrt
        real_lock = msvcrt.locking
        def checked_lock(fd, operation, length):
            try:
                result = real_lock(fd, operation, length)
            except OSError:
                contended.set()
                raise
            local.holds_lock = operation != msvcrt.LK_UNLCK
            return result
        monkeypatch.setattr(msvcrt, 'locking', checked_lock)
    elif os.name == 'posix':
        import fcntl
        real_lock = fcntl.flock
        def checked_lock(fd, operation):
            try:
                result = real_lock(fd, operation)
            except OSError:
                contended.set()
                raise
            local.holds_lock = operation != fcntl.LOCK_UN
            return result
        monkeypatch.setattr(fcntl, 'flock', checked_lock)
    else:
        pytest.skip('No supported native file-lock platform')
    monkeypatch.setattr(os, 'fstat', checked_fstat)
    monkeypatch.setattr(os, 'write', checked_write)
    path = tmp_path / 'real-empty-owner.lock'
    def owner():
        try:
            with product._policy_lock(path):
                entered.append(threading.current_thread().name)
                if len(entered) == 1:
                    acquired.set()
                    assert release.wait(4)
        except BaseException as error:
            errors.append(error)
    threads = [threading.Thread(target=owner, name='owner-'+str(index)) for index in range(2)]
    try:
        for thread in threads:
            thread.start()
        assert acquired.wait(4), errors
        assert contended.wait(4), errors
        assert len(entered) == 1  # The second real kernel lock is still blocked.
    finally:
        release.set()
        for thread in threads:
            thread.join(5)
    assert not any(thread.is_alive() for thread in threads)
    assert not errors and len(entered) == 2, errors
    assert path.read_bytes() == b'0'


def test_contended_policy_lock_keeps_bounded_timeout_and_existing_marker(tmp_path, monkeypatch):
    import threading
    from eimemory.governance.evolution import code_automation_policy_issue as product
    path = tmp_path / 'existing-owner.lock'
    path.write_bytes(b'existing-original-marker')
    path.chmod(0o600)
    errors = []
    with product._policy_lock(path):
        ticks = iter([0.0, 6.0])
        monkeypatch.setattr(product.time, 'monotonic', lambda: next(ticks))
        def contender():
            try:
                with product._policy_lock(path):
                    errors.append(AssertionError('contender_bypassed_real_lock'))
            except BaseException as error:
                errors.append(error)
        thread = threading.Thread(target=contender)
        thread.start(); thread.join(3)
        assert not thread.is_alive()
    assert len(errors) == 1 and isinstance(errors[0], ValueError)
    assert str(errors[0]) == 'policy_owner_busy'
    assert path.read_bytes() == b'existing-original-marker'
