from __future__ import annotations

import json
from pathlib import Path

from eimemory.api.runtime import Runtime
from eimemory.cli.main import main as cli_main
from eimemory.identity import hongtu_scope


def test_classify_incident_category_mapping(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path / "runtime")

    def classify(payload: dict[str, object]) -> str:
        return runtime.run_code_sandbox(incident=payload, scope=hongtu_scope({})).get("incident_category", "")

    assert classify({"incident_type": "policy_update"}) == "policy_fixable"
    assert classify({"summary": "Configuration file default timeout invalid"}) == "config_fixable"
    assert classify({"summary": "NameError in runtime when recalling context"}) == "code_fixable"
    assert classify({"summary": "Deployment job cannot start on production host"}) == "infra_fixable"
    assert classify({"summary": "Unclear behavior not mapped to known area"}) == "unknown"


def test_code_sandbox_does_not_create_worktree_by_default(tmp_path, monkeypatch) -> None:
    runtime = Runtime.create(root=tmp_path / "runtime")
    scope = hongtu_scope({})

    class _NoopRunner:
        called = False

        def prepare_worktree(self, *, branch_name: str, root: Path) -> Path:
            self.called = True
            return root / branch_name

    runner = _NoopRunner()

    report = runtime.run_code_sandbox(
        incident={
            "incident_type": "NameError",
            "title": "Code path crash",
            "summary": "NameError in recall path",
        },
        scope=scope,
        create_worktree=False,
        persist_report=False,
        runner=runner,
    )

    assert report["incident_category"] == "code_fixable"
    assert report["ok"] is True
    plan = report["sandbox_plan"]
    assert plan["worktree_created"] is False
    assert plan["worktree_path"] is None
    assert runner.called is False


def test_code_sandbox_create_worktree_is_safe_root(tmp_path, monkeypatch) -> None:
    runtime = Runtime.create(root=tmp_path / "runtime")
    scope = hongtu_scope({})
    sandbox_root = tmp_path / "sandbox-worktree-root"

    class _SafeRunner:
        def __init__(self) -> None:
            self.calls: list[str] = []

        def prepare_worktree(self, *, branch_name: str, root: Path) -> Path:
            self.calls.append(str(root))
            path = root / branch_name
            path.mkdir(parents=True)
            return path

    runner = _SafeRunner()

    report = runtime.run_code_sandbox(
        incident={
            "incident_type": "CodeRegression",
            "title": "Fix broken recall path",
            "summary": "The recall path crashes with AttributeError after a refactor.",
        },
        scope=scope,
        create_worktree=True,
        persist_report=False,
        runner=runner,
        worktree_root=sandbox_root,
    )

    path = Path(report["sandbox_plan"]["worktree_path"])
    assert report["sandbox_plan"]["worktree_created"] is True
    assert str(path).startswith(str(sandbox_root))
    assert path.exists()
    assert runner.calls == [str(sandbox_root)]
    assert report["sandbox_plan"]["branch_name"]


def test_code_sandbox_default_runner_creates_nonempty_sandbox_copy(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path / "runtime")
    scope = hongtu_scope({})
    sandbox_root = tmp_path / "sandbox-copy-root"

    report = runtime.run_code_sandbox(
        incident={
            "incident_type": "CodeRegression",
            "title": "Fix broken recall path",
            "summary": "The recall path crashes with AttributeError after a refactor.",
        },
        scope=scope,
        create_worktree=True,
        persist_report=False,
        worktree_root=sandbox_root,
    )

    path = Path(report["sandbox_plan"]["worktree_path"])
    assert report["sandbox_plan"]["worktree_created"] is True
    assert path.exists()
    assert (path / "pyproject.toml").exists()
    assert (path / "eimemory").is_dir()
    assert not (path / ".git").exists()


def test_code_sandbox_cli_rejects_policy_fixable_issue(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("EIMEMORY_ROOT", str(tmp_path / "runtime"))
    incident = {
        "incident_type": "policy_incorrectness",
        "title": "Policy should not force strict mode",
        "summary": "A policy suggestion got drifted and now conflicts with operator preferences.",
    }

    assert (
        cli_main(
            [
                "evolve",
                "code-sandbox",
                "--incident-json",
                json.dumps(incident),
            ]
        )
        == 0
    )

    output = json.loads(capsys.readouterr().out)
    assert output["incident_category"] == "policy_fixable"
    assert output["sandbox_plan"] is None


def test_code_sandbox_cli_builds_code_candidate_with_verification_notes(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("EIMEMORY_ROOT", str(tmp_path / "runtime"))
    incident = {
        "incident_type": "TypeError",
        "title": "Recall path fails to handle empty payload",
        "summary": "Function get_recall_bundle raises TypeError on empty payload during CLI use.",
        "files": ["eimemory/api/runtime.py", "tests/test_runtime.py"],
    }

    assert (
        cli_main(
            [
                "evolve",
                "code-sandbox",
                "--incident-json",
                json.dumps(incident),
            ]
        )
        == 0
    )

    output = json.loads(capsys.readouterr().out)
    assert output["incident_category"] == "code_fixable"
    assert output["ok"] is True
    assert output["sandbox_plan"] is not None
    assert output["sandbox_plan"]["worktree_created"] is False
    assert output["sandbox_plan"]["verification_commands"]
    assert "allowed_files" in output["sandbox_plan"]
    assert "rollback_notes" in output["sandbox_plan"]
    assert "python -m compileall eimemory" in output["sandbox_plan"]["verification_commands"]


def test_cli_code_sandbox_rejects_invalid_incident_json(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("EIMEMORY_ROOT", str(tmp_path / "runtime"))

    assert (
        cli_main(
            [
                "evolve",
                "code-sandbox",
                "--incident-json",
                "{broken-json",
            ]
        )
        == 2
    )

    output = json.loads(capsys.readouterr().out)
    assert output["ok"] is False
    assert output["error"] == "invalid_incident_json"


def test_code_sandbox_cli_can_persist_reflection_report(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("EIMEMORY_ROOT", str(tmp_path / "runtime"))
    scope = hongtu_scope({})
    incident = {
        "incident_type": "CodeRegression",
        "title": "Recall regression after patch",
        "summary": "TypeError in runtime memory recall flow",
    }

    assert (
        cli_main(
            [
                "evolve",
                "code-sandbox",
                "--incident-json",
                json.dumps(incident),
                "--persist-report",
            ]
        )
        == 0
    )

    output = json.loads(capsys.readouterr().out)
    assert output["persisted"] is True
    assert output["persisted_record_id"]

    runtime = Runtime.create(root=tmp_path / "runtime")
    reflections = runtime.store.list_records(kinds=["reflection"], scope=scope, limit=10)
    assert output["persisted_record_id"] in {item.record_id for item in reflections}
    assert any(item.meta.get("report_type") == "code_evolution_sandbox" for item in reflections)
