from __future__ import annotations

import json
import os
from pathlib import Path
import stat
import subprocess
import sys

import pytest


@pytest.fixture
def deploy_worker_environment(tmp_path: Path, request):
    """Isolate shell children too; Python monkeypatches do not cross exec."""
    home = tmp_path / "home"
    config = tmp_path / "config"
    guard = tmp_path / "python-guard"
    for path in (home, config, guard):
        path.mkdir()
    settings = config / "settings.json"
    settings.write_text(json.dumps({"trusted_repository_root": "before", "preserve": True}))
    violations = guard / "violations"
    # Every worker Python child loads this, including the settings heredoc.
    (guard / "sitecustomize.py").write_text('''
import os, sys
from pathlib import Path
root = Path(os.environ["WORKER_TEST_ROOT"]).resolve()
def audit(event, args):
    paths = ()
    if event == "open":
        path, mode, flags = args
        if flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND):
            paths = (path,)
    elif event in ("os.rename", "os.link", "os.symlink"):
        paths = args[:2]
    elif event in ("os.remove", "os.rmdir", "os.mkdir", "os.chmod"):
        paths = args[:1]
    for path in paths:
        if isinstance(path, (str, bytes, os.PathLike)) and not Path(os.fsdecode(path)).resolve().is_relative_to(root):
            with open(root / "python-guard" / "violations", "a") as log:
                log.write(event + "\\n")
            raise PermissionError("worker test attempted a write outside scratch")
sys.addaudithook(audit)
''')
    env = {**os.environ, "HOME": str(home), "USERPROFILE": str(home),
           "PYTHONPATH": str(guard), "PYTHONDONTWRITEBYTECODE": "1",
           "WORKER_TEST_ROOT": str(tmp_path), "EIMEMORY_CONFIG_DIR": str(config)}
    if getattr(request, "param", "path") == "path":
        settings = home / "settings.json"
        settings.write_bytes((config / "settings.json").read_bytes())
        env["EIMEMORY_CONFIG_PATH"] = str(settings)
    else:
        env.pop("EIMEMORY_CONFIG_PATH", None)
    # Emit the write audit event without ever opening the production path.
    probe = subprocess.run([sys.executable, "-B", "-c",
                            "import os, sys; sys.audit('open', '/etc/eimemory/settings.json', 'w', os.O_WRONLY)"],
                           env=env, text=True, capture_output=True, timeout=10)
    assert probe.returncode != 0 and "worker test attempted" in probe.stderr
    assert violations.read_text() == "open\n"
    violations.unlink()
    yield env, settings
    assert not violations.exists(), "worker subprocess attempted a write outside scratch"


@pytest.fixture
def absent_hermes_installation(tmp_path: Path, monkeypatch):
    """Exercise real discovery with no install, independent of operator settings."""
    home = tmp_path / "user without Hermes"
    path = tmp_path / "empty runtime path"
    home.mkdir()
    path.mkdir()
    for key in ("EIMEMORY_HERMES_BIN", "EIMEMORY_HERMES_AGENT_ROOT",
                "EIMEMORY_HERMES_HOME", "HERMES_HOME"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("PATH", str(path))


@pytest.fixture
def managed_research_preflight(tmp_path: Path):
    """Run the real wrapper with an isolated OS account home and system PATH.

    Setting the controller's HOME cannot isolate this check: the wrapper reads
    pwd.getpwuid() to match the service account. Stub only that OS boundary (and
    os.defpath) inside the test subprocess, keeping discovery, file permissions
    and the Hermes configuration check intact. No production test hook is used.
    """
    account_home = tmp_path / "service account"
    system_path = tmp_path / "system bin"
    account_home.mkdir()
    system_path.mkdir()
    wrapper = Path(__file__).resolve().parents[1] / "deploy/run_with_governance_env.py"
    bootstrap = """
import os, runpy, sys
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

home, path, wrapper, *arguments = sys.argv[1:]
with ExitStack() as stack:
    stack.enter_context(patch('os.defpath', path))
    if os.name == 'posix':
        import pwd
        account = list(pwd.getpwuid(os.geteuid()))
        account[5] = home
        stack.enter_context(patch('pwd.getpwuid', return_value=pwd.struct_passwd(account)))
    else:
        stack.enter_context(patch('pathlib.Path.home', return_value=Path(home)))
    sys.argv = [wrapper, *arguments]
    runpy.run_path(wrapper, run_name='__main__')
"""

    def check(env_file: Path, *, environment: dict[str, str], home: Path = account_home,
              path: Path = system_path):
        return subprocess.run(
            [sys.executable, "-I", "-B", "-c", bootstrap, str(home), str(path), str(wrapper),
             "--env-file", str(env_file), "--optional", "--check-research-review"],
            env=environment, capture_output=True, text=True, check=False, timeout=30,
        )

    return check


@pytest.fixture(autouse=True)
def isolate_eimemory_config_environment(monkeypatch, tmp_path: Path) -> None:
    """Keep tests independent from operator configuration and live data roots.

    Individual configuration tests set these variables explicitly after this
    fixture runs.  Clearing inherited values prevents an intentionally empty
    isolated test directory from turning unrelated CLI tests into production
    configuration checks.  Default runtime and OpenClaw loop writes must also
    remain below pytest's managed root so subprocess and hook tests cannot add
    synthetic records to a real operator store or task ledger.
    """

    monkeypatch.delenv("EIMEMORY_CONFIG_DIR", raising=False)
    monkeypatch.delenv("EIMEMORY_CONFIG_PATH", raising=False)
    monkeypatch.setenv("EIMEMORY_ROOT", str(tmp_path / "eimemory-root"))
    monkeypatch.setenv("OPENCLAW_LOOP_HOME", str(tmp_path / "openclaw-loop"))
    # Explicit trust anchors for code-evolution tests (never source-level author defaults).
    monkeypatch.setenv("EIMEMORY_TRUSTED_REPOSITORY_ROOT", "/dev-project/eimemory")
    monkeypatch.setenv("EIMEMORY_TRUSTED_REMOTE", "origin")
    monkeypatch.setenv("EIMEMORY_TRUSTED_BRANCH", "master")


@pytest.fixture
def trusted_dataset_path_ancestors(tmp_path, monkeypatch) -> None:
    """Model conventional root-owned ancestors in user-namespace sandboxes."""

    if os.name == "nt":
        return
    effective_uid = os.geteuid()
    trusted_uids = {0, effective_uid}
    ancestors = set(tmp_path.parents)
    real_lstat = Path.lstat

    def trusted_ancestor_lstat(path: Path):
        metadata = real_lstat(path)
        if path not in ancestors:
            return metadata
        values = list(metadata)
        if metadata.st_uid not in trusted_uids:
            values[4] = 0
        if not metadata.st_mode & stat.S_ISVTX:
            values[0] = int(metadata.st_mode) & ~(stat.S_IWGRP | stat.S_IWOTH)
        return os.stat_result(values)

    monkeypatch.setattr(Path, "lstat", trusted_ancestor_lstat)


@pytest.fixture
def local_collection_boundary(monkeypatch):
    """Use the existing fetch injection boundary; catch even swallowed network errors."""
    import socket

    attempts = []

    def fail_connect(*args, **kwargs):
        attempts.append((args, kwargs))
        raise AssertionError("Local fixture attempted a socket connection")

    monkeypatch.setattr(socket.socket, "connect", fail_connect)
    monkeypatch.setattr(socket.socket, "connect_ex", fail_connect)
    # Empty controlled feed: production collectors still run and parse locally.
    monkeypatch.setattr(
        "eimemory.api.runtime._default_fetch_text",
        lambda url: "<rss><channel></channel></rss>",
    )
    yield
    assert attempts == [], "Network errors must not be swallowed into passing reports"
