from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

from deploy.rotate_console_token import main as rotate_main
from deploy.rotate_console_token import rotate_token


def test_rotate_console_token_updates_unit_file(tmp_path) -> None:
    unit = tmp_path / "eimemory-console.service"
    unit.write_text(
        "\n".join(
            [
                "[Service]",
                "Environment=EIMEMORY_CONSOLE_TOKEN=old-token",
                "Environment=EIMEMORY_CONSOLE_PORT=8765",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    report = rotate_token(unit, token="new-token")

    assert report == {"ok": True, "unit_path": str(unit)}
    text = unit.read_text(encoding="utf-8")
    assert "EIMEMORY_CONSOLE_TOKEN=new-token" in text
    assert "old-token" not in text


def test_rotate_console_token_main_hides_token_by_default(tmp_path, capsys) -> None:
    unit = tmp_path / "eimemory-console.service"
    unit.write_text(
        "[Service]\nEnvironment=EIMEMORY_CONSOLE_TOKEN=old-token\n",
        encoding="utf-8",
    )

    assert rotate_main(["--unit", str(unit), "--token", "secret-token"]) == 0

    output = capsys.readouterr().out
    assert "secret-token" not in output
    assert "token_rotated" in output


def test_rotate_console_token_main_can_explicitly_show_token(tmp_path, capsys) -> None:
    unit = tmp_path / "eimemory-console.service"
    unit.write_text(
        "[Service]\nEnvironment=EIMEMORY_CONSOLE_TOKEN=old-token\n",
        encoding="utf-8",
    )

    assert rotate_main(["--unit", str(unit), "--token", "secret-token", "--show-token"]) == 0

    output = capsys.readouterr().out
    assert "secret-token" in output
    assert "new_url=http://<host>:8765/secret-token" in output


def test_console_systemd_uses_packaged_console_entrypoint() -> None:
    unit_text = Path("deploy/systemd/eimemory-console.service").read_text(encoding="utf-8")

    assert "/var/lib/eimemory/governance/serve_console.py" not in unit_text
    assert "python -m eimemory.governance.serve_console" in unit_text
    assert "EIMEMORY_CONFIG_DIR=/etc/eimemory" in unit_text


def test_learning_dashboard_systemd_unit_uses_user_report_path_and_release_binary() -> None:
    unit_path = Path("deploy/systemd/eimemory-learn-dashboard.service")
    unit_text = unit_path.read_text(encoding="utf-8")

    assert "daily autonomous learning dashboard" in unit_text
    assert "/var/lib/eimemory/autonomous-learning-dashboard.md" not in unit_text
    assert "--output %h/.openclaw/reports/autonomous-learning-dashboard.md" in unit_text
    assert "/opt/eimemory/current/.venv/bin/eimemory learn dashboard --persist" in unit_text


def test_learning_dashboard_timer_runs_daily_after_nightly() -> None:
    timer_text = Path("deploy/systemd/eimemory-learn-dashboard.timer").read_text(encoding="utf-8")

    assert "daily autonomous learning dashboard" in timer_text
    assert "OnCalendar=*-*-* 03:45:00" in timer_text
    assert "Mon *-*-* 09:00:00" not in timer_text


def test_l5_observation_gate_enables_autonomous_code_after_48_hours() -> None:
    unit_text = Path("deploy/systemd/eimemory-l5-observation-gate.service").read_text(encoding="utf-8")
    timer_text = Path("deploy/systemd/eimemory-l5-observation-gate.timer").read_text(encoding="utf-8")
    script_text = Path("deploy/systemd/eimemory-l5-observation-gate.sh").read_text(encoding="utf-8")

    assert "48-hour observation gate" in unit_text
    assert "ExecStart=%h/.config/systemd/user/eimemory-l5-observation-gate.sh" in unit_text
    assert "OnActiveSec=48h" in timer_text
    assert "Persistent=true" in timer_text
    assert "EIMEMORY_AUTONOMOUS_LEARNING_APPLY=1" in script_text
    assert "EIMEMORY_AUTONOMOUS_CODE_COMMIT=1" in script_text
    assert "EIMEMORY_AUTONOMOUS_CODE_DEPLOY=1" in script_text
    assert "EIMEMORY_AUTONOMOUS_CODE_DEPLOY_COMMAND" in script_text
    assert "install_immutable_release.sh" in script_text
    assert "systemctl --user restart eimemory-rpc.service" in script_text
    assert "EIMEMORY_AUTONOMOUS_CODE_HEALTH_COMMAND" in script_text
    assert "allowPromptInjection" in script_text
    assert "allowConversationAccess" in script_text
    assert "EIMEMORY_ENABLE_PROMPT_INJECTION=true" in script_text
    assert "systemctl --user restart \"$OPENCLAW_GATEWAY_UNIT\"" in script_text
    assert "http://127.0.0.1:18789/readyz" in script_text
    assert "systemctl --user disable --now" in script_text


def test_l5_observation_gate_requires_exact_l5_stage_before_apply() -> None:
    script_text = Path("deploy/systemd/eimemory-l5-observation-gate.sh").read_text(encoding="utf-8")

    assert 'if [ "$stage" != "L5" ]; then' in script_text
    assert "L4|L4.5|L5" not in script_text


def test_nightly_systemd_unit_sets_autonomous_learning_promotion_budget() -> None:
    unit_text = Path("deploy/systemd/eimemory-nightly.service").read_text(encoding="utf-8")

    assert "Environment=EIMEMORY_AUTONOMOUS_LEARNING_MAX_GOALS=3" in unit_text
    assert "Environment=EIMEMORY_AUTONOMOUS_LEARNING_MAX_PROMOTIONS=3" in unit_text
    assert "Environment=EIMEMORY_AUTONOMOUS_LEARNING_NETWORK=1" in unit_text


def test_production_systemd_has_single_autonomous_scheduler_owner() -> None:
    systemd_dir = Path("deploy/systemd")
    service_names = {path.name for path in systemd_dir.glob("*")}

    assert "eimemory-nightly.service" in service_names
    assert "eimemory-nightly.timer" in service_names
    assert "eimemory-karpathy-loop.service" not in service_names
    assert "eimemory-karpathy-loop.timer" not in service_names
    for unit_path in systemd_dir.glob("*"):
        unit_text = unit_path.read_text(encoding="utf-8")
        assert "karpathy_loop_cron" not in unit_text


def test_systemd_readme_recommends_only_nightly_timer_for_production() -> None:
    readme = Path("deploy/systemd/README.md").read_text(encoding="utf-8")

    assert "systemctl --user enable --now eimemory-nightly.timer" in readme
    assert "enable --now eimemory-learn-watch.timer" not in readme
    assert "enable --now eimemory-learn-think.timer" not in readme
    assert "enable --now eimemory-learn-dashboard.timer" not in readme
    assert "Legacy / Manual Timers" in readme


def test_eimemory_rpc_systemd_unit_uses_honxin_tailscale_endpoint() -> None:
    unit_text = Path("deploy/systemd/eimemory-rpc.service").read_text(encoding="utf-8")

    assert "User=" not in unit_text
    assert "Group=" not in unit_text
    assert "UMask=0027" in unit_text
    assert "Environment=HOME=/home/darrow" in unit_text
    assert "Environment=PYTHONPATH=/opt/eimemory/current" in unit_text
    assert unit_text.index("EnvironmentFile=") < unit_text.index("Environment=PYTHONPATH=")
    assert "Environment=PYTHONDONTWRITEBYTECODE=1" in unit_text
    assert (
        "ExecStart=/opt/eimemory/current/.venv/bin/python -m eimemory.cli.main serve-eibrain-rpc "
        "--host 100.105.189.120 --port 8091"
        in unit_text
    )
    assert "/opt/eimemory/current/.venv/bin/eimemory serve-eibrain-rpc" not in unit_text
    assert "--loopback-health-host 127.0.0.1 --loopback-health-port 8091" in unit_text
    assert "Environment=EIMEMORY_ROOT=/var/lib/eimemory" in unit_text
    assert "Environment=EIMEMORY_CONFIG_DIR=/etc/eimemory" in unit_text
    assert "KillMode=mixed" in unit_text
    assert "TimeoutStopSec=10" in unit_text
    assert (
        "ExecStartPre=/opt/eimemory/current/deploy/systemd/eimemory-rpc-cleanup-port.sh 8091 serve-eibrain-rpc"
        in unit_text
    )
    assert "ExecStopPost=" not in unit_text
    assert "WorkingDirectory=/opt/eimemory/current" in unit_text
    assert "/dev-project/eimemory" not in unit_text
    assert "/var/log/eimemory" not in unit_text
    assert (
        "StandardOutput=append:/home/darrow/.openclaw/logs/eimemory-rpc.service.log"
        in unit_text
    )
    assert (
        "StandardError=append:/home/darrow/.openclaw/logs/eimemory-rpc.service.log"
        in unit_text
    )
    assert "WantedBy=default.target" in unit_text
    assert "WantedBy=multi-user.target" not in unit_text


def test_eimemory_rpc_cleanup_script_kills_only_matching_port_listeners() -> None:
    script = Path("deploy/systemd/eimemory-rpc-cleanup-port.sh").read_text(encoding="utf-8")

    assert script.startswith("#!/usr/bin/env bash\n")
    assert "ss -ltnp" in script
    assert "pid=[0-9]+" in script
    assert "*eimemory*\"$MATCH\"*" in script
    assert "kill -TERM" in script
    assert "kill -KILL" in script


def test_openclaw_watchdog_systemd_uses_primary_and_loopback_health_gates() -> None:
    unit_text = Path("deploy/systemd/openclaw-stuck-watchdog.service").read_text(encoding="utf-8")

    assert "--health-url http://100.105.189.120:8091/health" in unit_text
    assert "--loopback-health-url http://127.0.0.1:8091/health" in unit_text


def test_systemd_units_use_immutable_current_release() -> None:
    for unit_path in Path("deploy/systemd").glob("*.service"):
        unit_text = unit_path.read_text(encoding="utf-8")
        assert "/opt/eimemory/venv" not in unit_text
        assert "/dev-project/eimemory" not in unit_text
        if unit_path.name != "openclaw-stuck-watchdog.service":
            assert "WorkingDirectory=/opt/eimemory/current" in unit_text


def test_openclaw_gateway_override_uses_production_eimemory_runtime() -> None:
    override_text = Path("deploy/systemd/openclaw-gateway-eimemory.conf").read_text(encoding="utf-8")

    assert "Environment=EIMEMORY_ROOT=/var/lib/eimemory" in override_text
    assert "Environment=EIMEMORY_CONFIG_DIR=/etc/eimemory" in override_text
    assert 'Environment="EIMEMORY_HOOK_COMMAND=/opt/eimemory/current/.venv/bin/eimemory openclaw-hook"' in override_text
    assert 'Environment="EIMEMORY_BRIDGE_COMMAND=/opt/eimemory/current/.venv/bin/eimemory ei-bridge feishu"' in override_text
    assert "/dev-project/eimemory/.venv" not in override_text
    assert "PYTHONPATH=/dev-project/eimemory" not in override_text


def test_immutable_release_installer_documents_non_editable_runtime() -> None:
    script = Path("deploy/install_immutable_release.sh").read_text(encoding="utf-8")

    assert "git -C \"$REPO_DIR\" archive \"$COMMIT\"" in script
    assert "pip install \"$STAGE_DIR\"" in script
    assert "pip install -e" not in script
    assert "/opt/eimemory" in script


def test_release_bytecode_cleaner_cleans_only_source_bytecode(tmp_path) -> None:
    cleaner = _load_release_bytecode_cleaner()
    if os.name != "posix":
        pytest.skip("dir_fd cleanup behavior requires POSIX")
    commit = "a" * 40
    install_root = tmp_path / "install"
    releases_root = install_root / "releases"
    release_dir = releases_root / commit
    source_cache = release_dir / "eimemory" / "__pycache__"
    source_cache.mkdir(parents=True)
    (source_cache / "module.cpython-314.pyc").write_bytes(b"source cache")
    (release_dir / "eimemory" / "orphan.pyc").write_bytes(b"source pyc")
    (release_dir / "eimemory" / "orphan.pyo").write_bytes(b"source pyo")
    untracked_source = release_dir / "eimemory" / "untracked.py"
    untracked_source.write_text("KEEP = True\n", encoding="utf-8")
    venv_cache = release_dir / ".venv" / "lib" / "site-packages" / "dependency" / "__pycache__"
    venv_cache.mkdir(parents=True)
    venv_bytecode = venv_cache / "module.cpython-314.pyc"
    venv_bytecode.write_bytes(b"venv cache")

    cleaner.clean_release_bytecode(release_dir=release_dir, releases_root=releases_root)

    assert not source_cache.exists()
    assert not (release_dir / "eimemory" / "orphan.pyc").exists()
    assert not (release_dir / "eimemory" / "orphan.pyo").exists()
    assert untracked_source.exists()
    assert venv_bytecode.read_bytes() == b"venv cache"


def test_release_bytecode_cleaner_does_not_follow_source_symlink(tmp_path) -> None:
    cleaner = _load_release_bytecode_cleaner()
    if os.name != "posix":
        pytest.skip("dir_fd symlink behavior requires POSIX")
    releases_root = tmp_path / "releases"
    release_dir = releases_root / ("b" * 40)
    release_dir.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside_cache = outside / "__pycache__"
    outside_cache.mkdir(parents=True)
    marker = outside_cache / "marker.pyc"
    marker.write_bytes(b"outside")
    (release_dir / "eimemory").symlink_to(outside, target_is_directory=True)

    cleaner.clean_release_bytecode(release_dir=release_dir, releases_root=releases_root)

    assert marker.read_bytes() == b"outside"
    assert (release_dir / "eimemory").is_symlink()


def test_release_bytecode_cleaner_rejects_unsafe_release_paths(tmp_path) -> None:
    cleaner = _load_release_bytecode_cleaner()
    releases_root = tmp_path / "releases"
    releases_root.mkdir()
    non_commit_release = releases_root / "test-release"
    non_commit_release.mkdir()
    outside_release = tmp_path / ("c" * 40)
    outside_release.mkdir()

    with pytest.raises(cleaner.CleanupError, match="40-character commit"):
        cleaner.resolve_release_paths(non_commit_release, releases_root)
    with pytest.raises(cleaner.CleanupError, match="direct child"):
        cleaner.resolve_release_paths(outside_release, releases_root)


def test_release_bytecode_cleaner_scan_failure_returns_nonzero(tmp_path, monkeypatch, capsys) -> None:
    cleaner = _load_release_bytecode_cleaner()
    releases_root = tmp_path / "releases"
    release_dir = releases_root / ("d" * 40)
    release_dir.mkdir(parents=True)

    def fail_scan(*, release_dir, releases_root, allow_stage=False):
        raise PermissionError("simulated scan failure")

    monkeypatch.setattr(cleaner, "clean_release_bytecode", fail_scan)
    result = cleaner.main(["--release-dir", str(release_dir), "--releases-root", str(releases_root)])

    assert result != 0
    assert "simulated scan failure" in capsys.readouterr().err


def test_release_bytecode_cleaner_relocates_virtualenv_console_scripts(tmp_path) -> None:
    cleaner = _load_release_bytecode_cleaner()
    if os.name != "posix":
        pytest.skip("dir_fd relocation behavior requires POSIX")
    releases_root = tmp_path / "releases"
    release_dir = releases_root / ("e" * 40)
    bin_dir = release_dir / ".venv" / "bin"
    bin_dir.mkdir(parents=True)
    old_stage = releases_root / f".eimemory-stage-{'e' * 40}-abcdefgh"
    scripts = {
        "eimemory": "python",
        "eimemory-qmd": "python3.14",
        "pip": "python3",
        "pip3": "python3.14",
    }
    for name, interpreter in scripts.items():
        (bin_dir / name).write_text(
            f"#!{old_stage}/.venv/bin/{interpreter}\nprint('ok')\n",
            encoding="utf-8",
        )
    binary = bin_dir / "binary-tool"
    binary.write_bytes(b"\x00\x01binary")
    outside = tmp_path / "outside-script"
    outside.write_text("unchanged\n", encoding="utf-8")
    (bin_dir / "linked-tool").symlink_to(outside)

    changed = cleaner.relocate_virtualenv_scripts(
        release_dir=release_dir,
        releases_root=releases_root,
        from_stage=old_stage,
        to_release=release_dir,
    )

    assert set(changed) == set(scripts)
    for name, interpreter in scripts.items():
        first_line = (bin_dir / name).read_text(encoding="utf-8").splitlines()[0]
        assert first_line == f"#!{release_dir}/.venv/bin/{interpreter}"
    assert binary.read_bytes() == b"\x00\x01binary"
    assert outside.read_text(encoding="utf-8") == "unchanged\n"


def test_immutable_release_installer_runs_fd_safe_cleanup_before_switch() -> None:
    script = Path("deploy/install_immutable_release.sh").read_text(encoding="utf-8")

    assert script.startswith("#!/usr/bin/env bash\nset -euo pipefail\n")
    assert "export PYTHONDONTWRITEBYTECODE=1" in script
    assert 'PYTHON_BIN="${PYTHON_BIN:-/usr/bin/python3}"' in script
    assert '[[ "$PYTHON_BIN" != /* ]]' in script
    assert 'git -C "$REPO_DIR" rev-parse HEAD' in script
    assert "rev-parse --short HEAD" not in script
    assert '[[ ! "$COMMIT" =~ ^[0-9a-fA-F]{40}$ ]]' in script
    assert '"$REPO_DIR/deploy/clean_release_bytecode.py"' in script
    assert "--validate-source" in script
    assert "--relocate-venv" in script
    assert '--from-stage "$OLD_STAGE_PATH"' in script
    assert '--to-release "$RELEASE_DIR"' in script
    assert 'mktemp -d "$INSTALL_ROOT/releases/.eimemory-stage-${COMMIT}-XXXXXXXX"' in script
    assert '"$PYTHON_BIN" -I -B -m venv --clear "$STAGE_DIR/.venv"' in script
    assert '--release-dir "$RELEASE_DIR"' in script
    assert '--releases-root "$INSTALL_ROOT/releases"' in script
    install_at = script.index('pip install "$STAGE_DIR"')
    verify_at = script.rindex("\n_run_openclaw_loop_deploy_verify ")
    stage_at = script.index('STAGE_DIR="$(mktemp')
    stage_python_at = script.rindex('"$STAGE_DIR/.venv/bin/python"')
    cleanup_at = script.index("--allow-stage --release-dir", install_at)
    relocate_at = script.index("--relocate-venv")
    switch_at = script.index('\nln -sfn "$RELEASE_DIR"')
    assert stage_at < stage_python_at
    assert install_at < verify_at < cleanup_at < switch_at
    assert cleanup_at < relocate_at < switch_at
    assert '_ensure_runtime_dir "$INSTALL_ROOT"' not in script


@pytest.mark.parametrize("attack", ["release_link", "releases_root_link"])
def test_immutable_release_installer_never_executes_linked_release_python(tmp_path, attack) -> None:
    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
    install_root = tmp_path / "install"
    releases_root = install_root / "releases"
    outside_root = tmp_path / "outside-releases"
    outside_root.mkdir()
    outside_root.chmod(0o755)
    outside_release = outside_root / commit
    fake_python = outside_release / ".venv" / "bin" / "python"
    fake_python.parent.mkdir(parents=True)
    marker = tmp_path / "malicious-python-ran"
    fake_python.write_text(f"#!/usr/bin/env bash\nprintf ran > '{marker.as_posix()}'\n", encoding="utf-8")
    fake_python.chmod(0o755)
    if attack == "release_link":
        releases_root.mkdir(parents=True)
        _create_directory_link(releases_root / commit, outside_release)
    else:
        install_root.mkdir(parents=True)
        _create_directory_link(releases_root, outside_root)
    old_release = tmp_path / "old-release"
    old_release.mkdir()
    current_link = install_root / "current"
    _create_directory_link(current_link, old_release)
    bash = _bash_binary()
    env = dict(os.environ)
    env.update(
        {
            "REPO_DIR": Path.cwd().as_posix(),
            "INSTALL_ROOT": install_root.as_posix(),
            "PYTHON_BIN": _bash_path(Path(sys.executable)),
            "EIMEMORY_ROOT": (tmp_path / "runtime").as_posix(),
            "EIMEMORY_CONFIG_DIR": (tmp_path / "config").as_posix(),
            "EIMEMORY_LOG_DIR": (tmp_path / "logs").as_posix(),
            "USER_SYSTEMD_ENABLE_SERVICE": "0",
            "OPENCLAW_LOOP_DEPLOY_VERIFY": "0",
            "OPENCLAW_LOOP_COMPAT_SCRIPT": "",
        }
    )

    result = subprocess.run(
        [bash, "deploy/install_immutable_release.sh", commit],
        cwd=Path.cwd(),
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode != 0
    assert not marker.exists()
    assert current_link.resolve() == old_release.resolve()
    if attack == "releases_root_link" and os.name == "posix":
        assert outside_root.stat().st_mode & 0o777 == 0o755


@pytest.mark.parametrize("source_valid", [True, False])
def test_immutable_release_installer_does_not_rebuild_active_existing_release(tmp_path, source_valid) -> None:
    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
    install_root = tmp_path / "install"
    release_dir = install_root / "releases" / commit
    fake_python = release_dir / ".venv" / "bin" / "python"
    fake_python.parent.mkdir(parents=True)
    marker = tmp_path / "active-python-ran"
    fake_python.write_text(f"#!/usr/bin/env bash\nprintf ran > '{marker.as_posix()}'\n", encoding="utf-8")
    fake_python.chmod(0o755)
    trusted_python = tmp_path / "trusted-python"
    trusted_python.write_text(
        f"#!{_bash_path(Path(sys.executable))}\n"
        "import sys\n"
        "if '--validate-source' in sys.argv:\n"
        f"    raise SystemExit({0 if source_valid else 2})\n"
        "raise SystemExit(0)\n",
        encoding="utf-8",
    )
    trusted_python.chmod(0o755)
    current_link = install_root / "current"
    _create_directory_link(current_link, release_dir)
    env = dict(os.environ)
    env.update(
        {
            "REPO_DIR": Path.cwd().as_posix(),
            "INSTALL_ROOT": install_root.as_posix(),
            "PYTHON_BIN": _bash_path(trusted_python),
            "USER_SYSTEMD_ENABLE_SERVICE": "0",
            "OPENCLAW_LOOP_DEPLOY_VERIFY": "0",
            "OPENCLAW_LOOP_COMPAT_SCRIPT": "",
        }
    )

    result = subprocess.run(
        [_bash_binary(), "deploy/install_immutable_release.sh", commit],
        cwd=Path.cwd(),
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == (0 if source_valid else 2)
    assert ("already_current=1" in result.stdout) is source_valid
    assert not marker.exists()
    assert current_link.resolve() == release_dir.resolve()


def _bash_binary() -> str:
    git_bash = Path("C:/Program Files/Git/bin/bash.exe")
    bash = str(git_bash) if git_bash.exists() else shutil.which("bash")
    if not bash:
        raise AssertionError("bash is required for installer behavior tests")
    return bash


def _bash_path(path: Path) -> str:
    value = path.as_posix()
    if os.name == "nt" and len(value) > 2 and value[1] == ":":
        return f"/{value[0].lower()}{value[2:]}"
    return value


def _create_directory_link(link: Path, target: Path) -> None:
    try:
        link.symlink_to(target, target_is_directory=True)
    except OSError:
        subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(target)],
            check=True,
            capture_output=True,
            text=True,
        )


def _load_release_bytecode_cleaner():
    cleaner_path = Path("deploy/clean_release_bytecode.py")
    assert cleaner_path.exists(), "release bytecode cleaner is missing"
    spec = importlib.util.spec_from_file_location("clean_release_bytecode", cleaner_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_immutable_release_installer_normalizes_service_ownership() -> None:
    script = Path("deploy/install_immutable_release.sh").read_text(encoding="utf-8")

    assert 'SERVICE_USER="${SERVICE_USER:-darrow}"' in script
    assert 'SERVICE_GROUP="${SERVICE_GROUP:-$SERVICE_USER}"' in script
    assert 'SERVICE_HOME="${SERVICE_HOME:-/home/$SERVICE_USER}"' in script
    assert 'SYSTEMD_ENABLE_SERVICE="${SYSTEMD_ENABLE_SERVICE:-0}"' not in script
    assert "SYSTEMD_UNIT_DIR" not in script
    assert 'USER_SYSTEMD_ENABLE_SERVICE="${USER_SYSTEMD_ENABLE_SERVICE:-1}"' in script
    assert 'USER_SYSTEMD_DIR="${USER_SYSTEMD_DIR:-$SERVICE_HOME/.config/systemd/user}"' in script
    assert "_ensure_runtime_dir" in script
    assert '"$INSTALL_ROOT"' in script
    assert '"$EIMEMORY_ROOT"' in script
    assert '"$EIMEMORY_CONFIG_DIR"' in script
    assert '"$EIMEMORY_LOG_DIR"' in script
    assert 'id "$SERVICE_USER" >/dev/null 2>&1' in script
    assert 'chown -R "$SERVICE_USER:$SERVICE_GROUP"' in script
    assert 'install -m 0644 "$RELEASE_DIR/deploy/systemd/eimemory-rpc.service" "$USER_SYSTEMD_DIR/eimemory-rpc.service"' in script
    assert "systemctl --user daemon-reload" in script
    assert "systemctl --user enable eimemory-rpc.service" in script
    assert "systemctl enable eimemory-rpc.service" not in script
    assert "_retire_system_rpc_unit" in script
    assert "systemctl disable --now eimemory-rpc.service" in script
    assert "retired-by-eimemory-user-systemd" in script


def test_user_systemd_owner_check_uses_only_user_service_as_rpc_owner() -> None:
    script = Path("deploy/check_user_systemd_owner.sh").read_text(encoding="utf-8")

    assert "systemctl --user is-active eimemory-rpc.service" in script
    assert "systemctl --user is-enabled eimemory-rpc.service" in script
    assert "systemctl is-active eimemory-rpc.service" in script
    assert "systemctl is-enabled eimemory-rpc.service" in script
    assert "system_owner_active" in script
    assert "system_owner_enabled" in script
    assert "system_owner_fragment" in script
    assert "system_rpc_service_unit_present" in script
    assert "ok=user_systemd_owner" in script


def test_learn_watch_timer_is_not_five_minute_heavy_polling() -> None:
    timer = Path("deploy/systemd/eimemory-learn-watch.timer").read_text(encoding="utf-8")

    assert "OnCalendar=*:00/15" in timer
    assert "OnCalendar=*:00/5" not in timer
