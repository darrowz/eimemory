from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from eimemory.api.runtime import Runtime
from eimemory.governance.code_evolution_bridge import propose_code_patch
from eimemory.governance.promotion_manager import (
    _code_patch_digest,
    _code_patch_subject_state_digest,
)
from eimemory.identity import hongtu_scope


@pytest.fixture(autouse=True)
def _local_apply_machine_policy(monkeypatch) -> None:
    """Bridge tests declare only the non-destructive local-apply authority."""

    monkeypatch.setenv(
        "EIMEMORY_CODE_AUTOMATION_POLICY_JSON",
        json.dumps(
            {
                "schema_version": "code_automation_policy.v1",
                "policy_id": "test-local-apply-v1",
                "actions": {
                    "local_apply": True,
                    "commit": False,
                    "deployment": False,
                },
            }
        ),
    )


class _TrackingRunner:
    def __init__(self) -> None:
        self.called = False

    def prepare_worktree(self, *, branch_name: str, root: Path) -> Path:
        self.called = True
        return root / branch_name


def test_code_patch_proposal_reports_unavailable_without_worktree_or_file_updates(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path / "runtime")
    runner = _TrackingRunner()

    proposal = runtime.propose_code_patch(
        incident={
            "incident_type": "TypeError",
            "title": "Recall path crash",
            "summary": "Function recall_bundle raises TypeError when payload is empty.",
            "files": ["eimemory/api/runtime.py", "tests/test_runtime.py"],
        },
        scope=hongtu_scope({}),
        runner=runner,
    )

    assert proposal["ok"] is True
    assert proposal["report_type"] == "code_patch_proposal"
    assert proposal["source_sandbox_report_type"] == "code_evolution_sandbox"
    assert proposal["proposal_status"] == "proposal_unavailable"
    assert proposal["blocked"] is True
    assert proposal["blocked_reason"] == "file_updates_unavailable"
    assert proposal["read_only"] is True
    assert proposal["mutates_repository"] is False
    assert proposal["incident_category"] == "code_fixable"
    assert proposal["patch_scope"] == {
        "allowed_files": ["eimemory/api/runtime.py", "tests/test_runtime.py"],
    }
    assert proposal["allowed_files"] == ["eimemory/api/runtime.py", "tests/test_runtime.py"]
    assert ["python", "-m", "compileall", "eimemory"] in proposal["verification_commands"]
    assert ["python", "-m", "pytest", "-q", "tests/test_runtime.py"] in proposal["verification_commands"]
    assert ["python", "-m", "pytest", "-q", "tests"] not in proposal["verification_commands"]
    assert proposal["rollback_notes"]
    assert proposal["sandbox_plan"]["worktree_created"] is False
    assert proposal["sandbox_plan"]["worktree_path"] is None
    assert proposal["persisted_record_id"] == ""
    assert runner.called is False


def _proposal_project(tmp_path: Path) -> Path:
    project = tmp_path / "proposal-project"
    source = project / "eimemory" / "api"
    tests = project / "tests"
    source.mkdir(parents=True)
    tests.mkdir()
    (source / "runtime.py").write_text("VALUE = 'old'\n", encoding="utf-8")
    (tests / "test_runtime.py").write_text("def test_placeholder():\n    assert True\n", encoding="utf-8")
    return project


def test_code_patch_proposal_builds_read_only_reviewable_diff_from_file_updates(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path / "runtime")
    project = _proposal_project(tmp_path)
    target = project / "eimemory" / "api" / "runtime.py"

    proposal = propose_code_patch(
        runtime,
        incident={
            "incident_type": "TypeError",
            "title": "Recall path crash",
            "summary": "Function recall_bundle raises TypeError when payload is empty.",
            "files": ["eimemory/api/runtime.py", "tests/test_runtime.py"],
            "file_updates": [
                {"path": "eimemory/api/runtime.py", "content": "VALUE = 'new'\n"},
            ],
        },
        scope=hongtu_scope({}),
        repo_root=project,
    )

    assert proposal["proposal_status"] == "proposal_ready"
    assert proposal["blocked"] is False
    assert proposal["read_only"] is True
    assert proposal["mutates_repository"] is False
    assert proposal["decision_authority"] == "machine_policy"
    assert proposal["automation_policy"]["source"] == "machine_environment"
    assert proposal["requested_machine_actions"] == ["local_apply"]
    assert proposal["proposal_source"] == "incident_file_updates"
    assert proposal["allowed_files"] == ["eimemory/api/runtime.py"]
    assert proposal["file_updates"] == [
        {"path": "eimemory/api/runtime.py", "content": "VALUE = 'new'\n"},
    ]
    assert proposal["patch_digest"]
    assert proposal["subject_state_digest"]
    assert proposal["file_base_digest"]
    assert proposal["repo_root"] == str(project.resolve())
    assert proposal["subject_commit"] == proposal["base_commit"]
    assert "--- a/eimemory/api/runtime.py" in proposal["unified_diff"]
    assert "+VALUE = 'new'" in proposal["unified_diff"]
    assert ["python", "-m", "pytest", "-q", "tests/test_runtime.py"] in proposal["verification_commands"]
    assert ["python", "-m", "pytest", "-q", "tests"] not in proposal["verification_commands"]
    assert target.read_text(encoding="utf-8") == "VALUE = 'old'\n"
    assert proposal["subject_state_digest"] == _code_patch_subject_state_digest(
        project,
        subject_commit=proposal["subject_commit"],
    )
    assert proposal["patch_digest"] == _code_patch_digest(
        proposal,
        repo_root=project,
        subject_commit=proposal["subject_commit"],
        subject_state_digest=proposal["subject_state_digest"],
        file_updates=proposal["file_updates"],
        verification_commands=proposal["verification_commands"],
    )


def test_code_patch_proposal_accepts_deterministic_injected_proposer(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path / "runtime")
    project = _proposal_project(tmp_path)
    calls: list[dict] = []

    def proposer(**context) -> dict:
        calls.append(context)
        return {
            "file_updates": [
                {"path": "eimemory/api/runtime.py", "content": "VALUE = 'proposed'\n"},
            ],
            "verification_commands": [["python", "-m", "pytest", "-q", "tests/test_runtime.py"]],
        }

    first = propose_code_patch(
        runtime,
        incident={
            "incident_type": "TypeError",
            "title": "Recall path crash",
            "summary": "Function recall_bundle raises TypeError when payload is empty.",
            "files": ["eimemory/api/runtime.py", "tests/test_runtime.py"],
        },
        scope=hongtu_scope({}),
        proposer=proposer,
        repo_root=project,
    )
    second = propose_code_patch(
        runtime,
        incident={
            "incident_type": "TypeError",
            "title": "Recall path crash",
            "summary": "Function recall_bundle raises TypeError when payload is empty.",
            "files": ["eimemory/api/runtime.py", "tests/test_runtime.py"],
        },
        scope=hongtu_scope({}),
        proposer=proposer,
        repo_root=project,
    )

    assert first["proposal_status"] == "proposal_ready"
    assert first["proposal_source"] == "proposer"
    assert first["patch_digest"] == second["patch_digest"]
    assert calls[0]["repo_root"] == project.resolve()
    assert calls[0]["allowed_files"] == ["eimemory/api/runtime.py", "tests/test_runtime.py"]


def test_code_patch_proposal_blocks_unsafe_or_full_suite_inputs(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path / "runtime")
    project = _proposal_project(tmp_path)
    incident = {
        "incident_type": "TypeError",
        "title": "Recall path crash",
        "summary": "Function recall_bundle raises TypeError when payload is empty.",
        "files": ["eimemory/api/runtime.py"],
    }

    unsafe = propose_code_patch(
        runtime,
        incident={**incident, "file_updates": [{"path": "../outside.py", "content": "x = 1\n"}]},
        scope=hongtu_scope({}),
        repo_root=project,
    )
    full_suite = propose_code_patch(
        runtime,
        incident={
            **incident,
            "file_updates": [{"path": "eimemory/api/runtime.py", "content": "VALUE = 'new'\n"}],
            "verification_commands": ["python -m pytest -q tests"],
        },
        scope=hongtu_scope({}),
        repo_root=project,
    )
    shell_string = propose_code_patch(
        runtime,
        incident={
            **incident,
            "file_updates": [{"path": "eimemory/api/runtime.py", "content": "VALUE = 'new'\n"}],
            "verification_commands": ["python -m pytest -q tests/test_runtime.py"],
        },
        scope=hongtu_scope({}),
        repo_root=project,
    )
    external_argv = propose_code_patch(
        runtime,
        incident={
            **incident,
            "file_updates": [{"path": "eimemory/api/runtime.py", "content": "VALUE = 'new'\n"}],
            "verification_commands": [["git", "push"]],
        },
        scope=hongtu_scope({}),
        repo_root=project,
    )
    python_eval = propose_code_patch(
        runtime,
        incident={
            **incident,
            "file_updates": [{"path": "eimemory/api/runtime.py", "content": "VALUE = 'new'\n"}],
            "verification_commands": [["python", "-c", "print('unsafe')"]],
        },
        scope=hongtu_scope({}),
        repo_root=project,
    )

    assert unsafe["proposal_status"] == "proposal_invalid"
    assert unsafe["blocked_reason"] == "unsafe_file_update_path"
    assert full_suite["proposal_status"] == "proposal_invalid"
    assert full_suite["blocked_reason"] == "full_test_suite_verification_not_allowed"
    assert shell_string["proposal_status"] == "proposal_invalid"
    assert shell_string["blocked_reason"] == "verification_commands_must_be_argv"
    assert external_argv["proposal_status"] == "proposal_invalid"
    assert external_argv["blocked_reason"] == "code_patch_verification_command_not_allowed"
    assert python_eval["proposal_status"] == "proposal_invalid"
    assert python_eval["blocked_reason"] == "code_patch_verification_command_not_allowed"


def test_code_patch_proposal_rejects_symlinked_target(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path / "runtime")
    project = _proposal_project(tmp_path)
    outside = tmp_path / "outside.py"
    outside.write_text("VALUE = 'outside'\n", encoding="utf-8")
    link = project / "linked.py"
    try:
        os.symlink(outside, link)
    except OSError:
        pytest.skip("symlink creation is unavailable on this host")

    proposal = propose_code_patch(
        runtime,
        incident={
            "incident_type": "TypeError",
            "title": "Reject linked target",
            "summary": "A proposed update must not traverse a link.",
            "files": ["linked.py"],
            "file_updates": [{"path": "linked.py", "content": "VALUE = 'new'\n"}],
            "verification_commands": [["python", "-m", "compileall", "linked.py"]],
        },
        scope=hongtu_scope({}),
        repo_root=project,
    )

    assert proposal["proposal_status"] == "proposal_invalid"
    assert proposal["blocked_reason"] == "file_update_symlink_not_allowed"
    assert outside.read_text(encoding="utf-8") == "VALUE = 'outside'\n"


def test_code_patch_proposal_non_code_incident_is_not_applicable(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path / "runtime")

    proposal = runtime.propose_code_patch(
        incident={
            "incident_type": "policy_incorrectness",
            "title": "Policy drift",
            "summary": "A memory retrieval policy suggestion conflicts with operator preference.",
        },
        scope=hongtu_scope({}),
    )

    assert proposal["ok"] is True
    assert proposal["report_type"] == "code_patch_proposal"
    assert proposal["proposal_status"] == "not_applicable"
    assert proposal["incident_category"] == "policy_fixable"
    assert proposal["patch_scope"] is None
    assert proposal["allowed_files"] == []
    assert proposal["verification_commands"] == []
    assert proposal["sandbox_plan"] is None
    assert proposal["persisted_record_id"] == ""


def test_runtime_code_patch_proposal_wrapper_can_persist_sandbox_report(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path / "runtime")
    scope = hongtu_scope({})

    proposal = runtime.propose_code_patch(
        incident={
            "incident_type": "AttributeError",
            "title": "Runtime recall crash",
            "summary": "Runtime method raises AttributeError during code path execution.",
        },
        scope=scope,
        persist_report=True,
    )

    assert proposal["proposal_status"] == "proposal_unavailable"
    assert proposal["persisted_record_id"]

    reflections = runtime.store.list_records(kinds=["reflection"], scope=scope, limit=10)
    persisted = next(item for item in reflections if item.record_id == proposal["persisted_record_id"])
    assert persisted.meta["report_type"] == "code_evolution_sandbox"
    assert persisted.source == "eimemory.code_evolution"


def test_code_patch_proposal_rejects_candidate_claimed_policy_when_machine_policy_missing(tmp_path, monkeypatch) -> None:
    runtime = Runtime.create(root=tmp_path / "runtime")
    project = _proposal_project(tmp_path)
    monkeypatch.delenv("EIMEMORY_CODE_AUTOMATION_POLICY_JSON", raising=False)

    proposal = propose_code_patch(
        runtime,
        incident={
            "classification": "code_fixable",
            "incident_type": "TypeError",
            "title": "Claimed policy must not authorize a patch",
            "summary": "The incident must not grant code-apply authority.",
            "files": ["eimemory/api/runtime.py"],
            "automation_policy": {
                "policy_id": "candidate-claim",
                "actions": {"local_apply": True, "commit": True, "deployment": True},
            },
            "file_updates": [
                {"path": "eimemory/api/runtime.py", "content": "VALUE = 'new'\n"},
            ],
        },
        scope=hongtu_scope({}),
        repo_root=project,
    )

    assert proposal["proposal_status"] == "proposal_blocked"
    assert proposal["blocked_reason"] == "machine_policy_environment_missing"
    assert proposal["automation_policy"]["declared"] is False
    assert proposal["automation_policy"]["source"] == "machine_environment"


def test_code_patch_proposal_requires_distinct_commit_authority(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path / "runtime")
    project = _proposal_project(tmp_path)

    proposal = propose_code_patch(
        runtime,
        incident={
            "classification": "code_fixable",
            "incident_type": "TypeError",
            "title": "Commit requires an explicit action",
            "summary": "A local apply policy cannot implicitly authorize a commit.",
            "files": ["eimemory/api/runtime.py"],
            "file_updates": [
                {"path": "eimemory/api/runtime.py", "content": "VALUE = 'new'\n"},
            ],
            "commit_to_repo": True,
        },
        scope=hongtu_scope({}),
        repo_root=project,
    )

    assert proposal["proposal_status"] == "proposal_blocked"
    assert proposal["blocked_reason"] == "machine_policy_commit_not_enabled"
    assert proposal["requested_machine_actions"] == ["local_apply", "commit"]
