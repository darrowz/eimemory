from __future__ import annotations

from pathlib import Path

from eimemory.api.runtime import Runtime
from eimemory.identity import hongtu_scope


class _TrackingRunner:
    def __init__(self) -> None:
        self.called = False

    def prepare_worktree(self, *, branch_name: str, root: Path) -> Path:
        self.called = True
        return root / branch_name


def test_code_patch_proposal_returns_sandbox_ready_without_worktree_by_default(tmp_path) -> None:
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
    assert proposal["proposal_status"] == "sandbox_ready"
    assert proposal["incident_category"] == "code_fixable"
    assert proposal["patch_scope"] == {
        "allowed_files": ["eimemory/api/runtime.py", "tests/test_runtime.py"],
    }
    assert proposal["allowed_files"] == ["eimemory/api/runtime.py", "tests/test_runtime.py"]
    assert "python -m compileall eimemory" in proposal["verification_commands"]
    assert proposal["rollback_notes"]
    assert proposal["sandbox_plan"]["worktree_created"] is False
    assert proposal["sandbox_plan"]["worktree_path"] is None
    assert proposal["persisted_record_id"] == ""
    assert runner.called is False


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

    assert proposal["proposal_status"] == "sandbox_ready"
    assert proposal["persisted_record_id"]

    reflections = runtime.store.list_records(kinds=["reflection"], scope=scope, limit=10)
    persisted = next(item for item in reflections if item.record_id == proposal["persisted_record_id"])
    assert persisted.meta["report_type"] == "code_evolution_sandbox"
    assert persisted.source == "eimemory.code_evolution"
