from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

from deploy.run_with_governance_env import GovernanceEnvironmentError, load_governance_environment
from deploy.summarize_release_closure import main as summarize_main
from deploy.summarize_release_closure import summarize_release_closure


def test_governance_env_loader_unquotes_allowlisted_values_without_expansion(tmp_path: Path) -> None:
    env_file = tmp_path / "governance.env"
    env_file.write_text(
        "\n".join(
            [
                "EIMEMORY_PROMPT_SAFETY_COMMAND='[\"/opt/eimemory/current/python\",\"-m\",\"safety\"]'",
                "EIMEMORY_PROMPT_SAFETY_MODEL=",
                "EIMEMORY_PROMPT_SAFETY_MAX_ATTEMPTS=3",
                "EIMEMORY_LLM_TIMEOUT_SECONDS=90",
                "UNRELATED_SECRET=must-not-be-imported",
            ]
        ),
        encoding="utf-8",
    )
    env_file.chmod(0o600)

    loaded = load_governance_environment(env_file, base_environment={"PATH": "trusted"})

    assert loaded == {
        "PATH": "trusted",
        "EIMEMORY_PROMPT_SAFETY_COMMAND": '["/opt/eimemory/current/python","-m","safety"]',
        "EIMEMORY_PROMPT_SAFETY_MODEL": "",
        "EIMEMORY_PROMPT_SAFETY_MAX_ATTEMPTS": "3",
        "EIMEMORY_LLM_TIMEOUT_SECONDS": "90",
    }


@pytest.mark.parametrize(
    "content",
    [
        "EIMEMORY_LLM_MODEL=first\nEIMEMORY_LLM_MODEL=second\n",
        "EIMEMORY_LLM_MODEL='unterminated\n",
        "not an assignment\n",
    ],
)
def test_governance_env_loader_rejects_ambiguous_or_malformed_files(tmp_path: Path, content: str) -> None:
    env_file = tmp_path / "governance.env"
    env_file.write_text(content, encoding="utf-8")

    with pytest.raises(GovernanceEnvironmentError):
        load_governance_environment(env_file, base_environment={})


def test_governance_env_loader_rejects_symlink(tmp_path: Path) -> None:
    target = tmp_path / "target.env"
    target.write_text("EIMEMORY_LLM_MODEL=safe\n", encoding="utf-8")
    link = tmp_path / "governance.env"
    try:
        link.symlink_to(target)
    except OSError:
        pytest.skip("symlink creation is unavailable")

    with pytest.raises(GovernanceEnvironmentError):
        load_governance_environment(link, base_environment={})


def test_release_closure_summary_is_compact_and_preserves_blocker() -> None:
    report = {
        "ok": False,
        "closure_complete": False,
        "data_accumulating": False,
        "blocked_stage": "closure_rehearsal",
        "blocked_reason": "prompt_safety_incomplete",
        "deployment": {"commit": "a" * 40, "version": "1.9.70", "promotion_request_id": "receipt-1"},
        "replay_bootstrap": {"ok": True},
        "live_acceptance": {"ok": True, "pass_count": 10, "case_count": 10},
        "closure_rehearsal": {"ok": False, "closure_complete": False},
        "readiness": {"current_stage": "not_run"},
        "large_payload": [json.dumps({"ignored": True})] * 100,
    }

    assert summarize_release_closure(report) == {
        "ok": False,
        "closure_complete": False,
        "data_accumulating": False,
        "blocked_stage": "closure_rehearsal",
        "blocked_reason": "prompt_safety_incomplete",
        "commit": "a" * 40,
        "version": "1.9.70",
        "receipt_id": "receipt-1",
        "production_recall_gate_ok": False,
        "production_recall_gate_status": "",
        "production_recall_gate_report_id": "",
        "production_recall_gate_reason": "",
        "replay_ok": True,
        "live_acceptance_ok": True,
        "live_pass_count": 10,
        "live_case_count": 10,
        "rehearsal_ok": False,
        "readiness_stage": "not_run",
        "readiness_score": None,
    }


def test_release_closure_summary_marks_data_accumulating_rehearsal_as_gate_success() -> None:
    summary = summarize_release_closure(
        {
            "ok": True,
            "closure_complete": False,
            "data_accumulating": True,
            "deployment": {"commit": "a" * 40, "version": "1.9.70", "promotion_request_id": "receipt-1"},
            "replay_bootstrap": {"ok": True},
            "live_acceptance": {"ok": True, "pass_count": 10, "case_count": 10},
            "closure_rehearsal": {"ok": True, "closure_complete": False, "data_accumulating": True},
            "readiness": {"current_stage": "data_accumulating", "readiness_score": 0.9},
        }
    )

    assert summary["ok"] is True
    assert summary["data_accumulating"] is True
    assert summary["rehearsal_ok"] is True
    assert summary["readiness_stage"] == "data_accumulating"


def test_release_closure_summary_cli_returns_nonzero_for_false_report_under_bash(tmp_path) -> None:
    report_path = tmp_path / "closure.json"
    report_path.write_text(
        json.dumps(
            {
                "ok": False,
                "closure_complete": False,
                "data_accumulating": False,
                "blocked_stage": "closure_rehearsal",
                "blocked_reason": "l5_readiness_not_l5",
                "closure_rehearsal": {"ok": False, "closure_complete": False},
            }
        ),
        encoding="utf-8",
    )
    git_bash = Path("C:/Program Files/Git/bin/bash.exe")
    bash = str(git_bash) if git_bash.exists() else shutil.which("bash")
    if not bash:
        pytest.skip("bash is required for the deployment status harness")

    result = subprocess.run(
        [
            bash,
            "-c",
            '"$1" -I -B "$2" --path "$3"',
            "bash",
            _bash_path(Path(sys.executable)),
            _bash_path(Path("deploy/summarize_release_closure.py").resolve()),
            _bash_path(report_path),
        ],
        cwd=Path.cwd(),
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode != 0
    assert json.loads(result.stdout)["ok"] is False


def test_release_closure_summary_cli_accepts_valid_data_accumulating_contract(tmp_path, capsys) -> None:
    report_path = tmp_path / "closure.json"
    report_path.write_text(
        json.dumps(
            {
                "ok": True,
                "closure_complete": False,
                "data_accumulating": True,
                "closure_rehearsal": {
                    "ok": True,
                    "closure_complete": False,
                    "data_accumulating": True,
                },
            }
        ),
        encoding="utf-8",
    )

    assert summarize_main(["--path", str(report_path)]) == 0
    assert json.loads(capsys.readouterr().out)["data_accumulating"] is True


def _bash_path(path: Path) -> str:
    value = path.as_posix()
    if os.name == "nt" and len(value) > 2 and value[1] == ":":
        return f"/{value[0].lower()}{value[2:]}"
    return value
