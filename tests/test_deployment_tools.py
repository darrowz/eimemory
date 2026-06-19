from __future__ import annotations

from pathlib import Path

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


def test_eimemory_rpc_systemd_unit_uses_honxin_tailscale_endpoint() -> None:
    unit_text = Path("deploy/systemd/eimemory-rpc.service").read_text(encoding="utf-8")

    assert "User=" not in unit_text
    assert "Group=" not in unit_text
    assert "UMask=0027" in unit_text
    assert "Environment=HOME=/home/darrow" in unit_text
    assert (
        "ExecStart=/opt/eimemory/current/.venv/bin/eimemory serve-eibrain-rpc --host 100.105.189.120 --port 8091"
        in unit_text
    )
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
    assert "pip install \"$RELEASE_DIR\"" in script
    assert "pip install -e" not in script
    assert "/opt/eimemory" in script


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


def test_user_systemd_owner_check_uses_only_user_service_as_rpc_owner() -> None:
    script = Path("deploy/check_user_systemd_owner.sh").read_text(encoding="utf-8")

    assert "systemctl --user is-active eimemory-rpc.service" in script
    assert "systemctl --user is-enabled eimemory-rpc.service" in script
    assert "systemctl is-active eimemory-rpc.service" in script
    assert "systemctl is-enabled eimemory-rpc.service" in script
    assert "system_owner_active" in script
    assert "system_owner_enabled" in script
    assert "ok=user_systemd_owner" in script
