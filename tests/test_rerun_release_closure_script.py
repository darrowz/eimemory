"""deploy/rerun_release_closure.sh registers a manual closure capture."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

pytestmark = pytest.mark.skipif(os.name != "posix", reason="native Bash and POSIX release symlinks required")

SCRIPT = Path(__file__).resolve().parents[1] / "deploy" / "rerun_release_closure.sh"
COMMIT = "7" * 40


def _release(tmp_path: Path, *, commit: str = COMMIT) -> Path:
    database = tmp_path / "root" / "state" / "eimemory.sqlite"
    database.parent.mkdir(parents=True)
    database.touch()
    release = tmp_path / "releases" / commit
    (release / "deploy").mkdir(parents=True)
    (release / ".venv" / "bin").mkdir(parents=True)
    os.symlink(sys.executable, release / ".venv" / "bin" / "python")
    (release / "deploy" / "record_release_closure_incident.py").write_text(
        "import json, sys\nprint(json.dumps(sys.argv[1:]))\n", encoding="utf-8")
    os.symlink(release, tmp_path / "current")
    return release


def _run(tmp_path: Path, *args: str, extra_env: dict | None = None) -> subprocess.CompletedProcess:
    env = {**os.environ, "INSTALL_ROOT": str(tmp_path), "EIMEMORY_ROOT": str(tmp_path / "root"),
           "EIMEMORY_CONFIG_DIR": str(tmp_path / "config"),
           "EIMEMORY_LOG_DIR": str(tmp_path / "logs"), "EIMEMORY_DEPLOY_SCOPE_USER": "darrow"}
    env.update(extra_env or {})
    return subprocess.run(["bash", str(SCRIPT), *args], capture_output=True, text=True, env=env, check=False)


def test_register_existing_capture_goes_through_incident_recorder(tmp_path) -> None:
    _release(tmp_path)
    capture = tmp_path / "manual-closure.json"
    capture.write_text("{}", encoding="utf-8")
    completed = _run(tmp_path, "--register", str(capture), "--exit-status", "2")
    assert completed.returncode == 0, completed.stderr
    argv = json.loads(completed.stdout.strip().splitlines()[-1])
    assert argv[-1] == "--enforce-gate"
    opts = dict(zip(argv[::2], argv[1::2]))
    assert opts["--path"] == str(capture)
    assert opts["--expected-commit"] == COMMIT
    assert opts["--attempt-id"].startswith(f"{COMMIT}-manual-")
    assert opts["--closure-exit-status"] == "2"
    assert opts["--evidence-dir"] == str(tmp_path / "logs" / "release-closure-captures")
    assert (opts["--scope-agent"], opts["--scope-workspace"], opts["--scope-user"]) == ("hongtu", "embodied", "darrow")
    assert capture.exists()


def test_run_requires_prior_commit(tmp_path) -> None:
    _release(tmp_path)
    completed = _run(tmp_path)
    assert completed.returncode == 64
    assert "--prior-commit" in completed.stderr


@pytest.mark.parametrize("accepted", [False, True])
@pytest.mark.parametrize("mode", ["register", "run"])
def test_native_capture_admission_controls_bash_exit_and_preserves_evidence(tmp_path, accepted, mode):
    from test_controlled_production_release_closure import complete_report

    report = complete_report()
    if not accepted:
        report.update(ok=False, closure_complete=False, blocked_stage="stability",
                      blocked_reason="sample_health_unverified")
    release = _release(tmp_path, commit=report["deployment"]["commit"])
    source = tmp_path / "source.json"
    raw = json.dumps(report).encode()
    source.write_bytes(raw)
    # Exercise the real recorder, native verdict and durable capture pipeline.
    # Its storage-only fixture avoids creating a deployment Runtime or models.
    repository = SCRIPT.parents[1]
    recorder = release / "deploy" / "record_release_closure_incident.py"
    recorder.write_text(
        "import pathlib,runpy,sys\n"
        f"sys.path[:0] = [{str(repository)!r}, {str(repository / 'tests')!r}]\n"
        "from eimemory.ops import closure_capture\n"
        "from test_closure_capture_pipeline import FixtureRuntime\n"
        "native = closure_capture.process_capture\n"
        "def capture(*args, **kwargs):\n"
        f"    kwargs['runtime_factory'] = lambda: FixtureRuntime(pathlib.Path({str(tmp_path / 'incident.sqlite')!r}))\n"
        "    return native(*args, **kwargs)\n"
        "closure_capture.process_capture = capture\n"
        f"runpy.run_path({str(repository / 'deploy' / 'record_release_closure_incident.py')!r}, run_name='__main__')\n",
        encoding="utf-8", newline="\n")
    scope = report["scope"]
    env = {"EIMEMORY_DEPLOY_SCOPE_AGENT": scope["agent_id"],
           "EIMEMORY_DEPLOY_SCOPE_WORKSPACE": scope["workspace_id"],
           "EIMEMORY_DEPLOY_SCOPE_USER": scope["user_id"], "PYTHON_BIN": sys.executable}
    if mode == "register":
        args = ["--register", str(source), "--exit-status", "0" if accepted else "1"]
    else:
        (release / "deploy" / "run_with_governance_env.py").write_text(
            f"from pathlib import Path\nprint(Path({str(source)!r}).read_text())\n",
            encoding="utf-8", newline="\n")
        args = ["--prior-commit", "6" * 40]
    completed = _run(tmp_path, *args, extra_env=env)
    assert completed.returncode == (0 if accepted else 1), completed.stderr
    result = json.loads(completed.stdout.strip().splitlines()[-1])
    assert result["recording_ok"] is True and result["capture_saved"] is True
    assert result["ordinary_release_admission"] == ("admitted" if accepted else "denied")
    assert result["exit_code"] == result["gate_exit_code"] == completed.returncode
    captured = Path(result["capture"]["path"])
    saved = (captured / "source.bin").read_bytes()
    assert saved.rstrip() == raw
    assert json.loads((captured / "result.json").read_text()) == result
    assert source.read_bytes() == raw
    if accepted:
        assert result["incident_record_id"] == ""
    else:
        assert result["incident_record_id"] and (tmp_path / "incident.sqlite").is_file()
