from __future__ import annotations

import os
from pathlib import Path
import shlex
import subprocess
import sys
import venv

import pytest


pytestmark = [pytest.mark.linux_deployment, pytest.mark.skipif(sys.platform != "linux", reason="Linux installer")]


def interpreter(root: Path, *, postgres: bool, prior: bool = False) -> Path:
    venv.EnvBuilder(with_pip=False).create(root / ".venv")
    python = root / ".venv/bin/python"
    site = Path(subprocess.check_output(
        [str(python), "-I", "-B", "-c", "import sysconfig; print(sysconfig.get_path('purelib'))"], text=True
    ).strip())
    if postgres:
        package = site / "psycopg"
        package.mkdir()
        # Preservation detects package presence without importing old code.
        (package / "__init__.py").write_text("raise AssertionError('prior imported')\n" if prior else "")
        (package / "rows.py").write_text("dict_row = object()\n")
    if not prior:
        pip = site / "pip"
        pip.mkdir()
        (pip / "__init__.py").write_text("")
        (pip / "__main__.py").write_text(
            "import os,sys\n"
            "with open(os.environ['PIP_TEST_LOG'], 'a') as handle:\n"
            "    handle.write(' '.join(sys.argv[1:]) + '\\n')\n"
        )
    return python


def run_dependency_stage(tmp_path: Path, *, prior_has: bool, override: str | None, staged_has: bool):
    prior = tmp_path / "prior"
    stage = tmp_path / "stage"
    interpreter(prior, postgres=prior_has, prior=True)
    interpreter(stage, postgres=staged_has)
    source = Path("deploy/install_immutable_release.sh").read_text()
    start = source.index('"$STAGE_DIR/.venv/bin/python" -I -B -m pip install "$STAGE_DIR"')
    end = source.index('"$STAGE_DIR/.venv/bin/python" -I -B -m compileall', start)
    env = dict(os.environ)
    env.pop("EIMEMORY_INSTALL_POSTGRES_EXTRA", None)
    if override is not None:
        env["EIMEMORY_INSTALL_POSTGRES_EXTRA"] = override
    log = tmp_path / "pip.log"
    env["PIP_TEST_LOG"] = str(log)
    result = subprocess.run(["bash", "-c",
        "set -euo pipefail\n" + f"STAGE_DIR={shlex.quote(str(stage))}\n"
        + f"PREVIOUS_CURRENT={shlex.quote(str(prior))}\n" + source[start:end]
        + "echo staged_dependencies_verified\n"], env=env, text=True, capture_output=True)
    return result, log.read_text()


@pytest.mark.parametrize("prior_has,override,expected", [
    (True, None, True), (False, None, False),
    (True, "0", False), (False, "1", True),
])
def test_dependency_stage_preserves_prior_optional_postgres_unless_overridden(tmp_path, prior_has, override, expected):
    result, log = run_dependency_stage(tmp_path, prior_has=prior_has, override=override, staged_has=expected)
    assert result.returncode == 0, result.stderr
    assert ("[postgres]" in log) is expected
    assert "check" in log
    assert "staged_dependencies_verified" in result.stdout


def test_requested_postgres_must_import_even_when_pip_check_passes(tmp_path):
    result, log = run_dependency_stage(tmp_path, prior_has=False, override="1", staged_has=False)
    assert "[postgres]" in log
    assert result.returncode != 0
    assert "staged_dependencies_verified" not in result.stdout
    assert "postgres_dependency=failed staged_import" in result.stderr
