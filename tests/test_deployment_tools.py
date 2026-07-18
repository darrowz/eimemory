from __future__ import annotations

import importlib.util
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys

import pytest

from deploy.rotate_console_token import main as rotate_main
from deploy.rotate_console_token import rotate_token


@pytest.mark.parametrize("openclaw_version", ["2026.7.1-beta.2", "2026.7.1-2"])
def test_openclaw_restart_recovery_scope_patch_is_managed_and_idempotent(
    tmp_path, openclaw_version: str
) -> None:
    openclaw_root = tmp_path / "openclaw"
    dist = openclaw_root / "dist"
    dist.mkdir(parents=True)
    (openclaw_root / "package.json").write_text(
        json.dumps({"version": openclaw_version}),
        encoding="utf-8",
    )
    runtime = dist / "main-session-restart-recovery-test.js"
    runtime.write_text(
        """
async function sendUnresumableSessionNotice() {
    await callGateway({
        method: "message.action",
        params: {},
    });
}
async function resumeMainSession() {
    await callGateway({
        method: "agent",
        params: {},
    });
}
""".strip()
        + "\n",
        encoding="utf-8",
    )
    tools_runtime = dist / "openclaw-tools-test.js"
    tools_runtime.write_text(
        """
function createSessionsHistoryTool(opts) {
    const gatewayCall = opts?.callGateway ?? callGateway;
    return gatewayCall({ method: "chat.history", params: {} });
}
function createSessionsListTool(opts) {
    const gatewayCall = opts?.callGateway ?? callGateway;
    return gatewayCall({ method: "sessions.list", params: {} });
}
function createSessionsSendTool(opts) {
    const gatewayCall = opts?.callGateway ?? callGateway;
    return gatewayCall({ method: "sessions.resolve", params: {} });
}
let openClawToolsDeps = { callGateway };
""".strip()
        + "\n",
        encoding="utf-8",
    )
    gateway_runtime = dist / "gateway-test.js"
    gateway_runtime.write_text(
        """
const AGENT_RUNTIME_IDENTITY_METHODS = new Set(["cron.status", "cron.run"]);
async function callGatewayTool(method, opts, params, extra) {
    const gateway = resolveGatewayOptions(opts);
    const scopes = Array.isArray(extra?.scopes)
        ? extra.scopes
        : resolveLeastPrivilegeOperatorScopesForMethod(method, params);
    const agentRuntimeIdentityToken = resolveAgentRuntimeIdentityTokenForGatewayTool({
        method,
        opts,
        target: gateway.target,
    });
    return await callGateway({
        url: gateway.url,
        token: gateway.token,
        method,
        params,
        clientName: GATEWAY_CLIENT_NAMES.GATEWAY_CLIENT,
        clientDisplayName: "agent",
        mode: GATEWAY_CLIENT_MODES.BACKEND,
        ...(agentRuntimeIdentityToken ? { agentRuntimeIdentityToken } : {}),
        scopes,
    });
}
""".strip()
        + "\n",
        encoding="utf-8",
    )
    call_runtime = dist / "call-test.js"
    call_runtime.write_text(
        """
async function callGateway(opts) {
    const callerMode = opts.mode ?? GATEWAY_CLIENT_MODES.BACKEND;
    const callerName = opts.clientName ?? GATEWAY_CLIENT_NAMES.GATEWAY_CLIENT;
    if (callerMode === GATEWAY_CLIENT_MODES.CLI || callerName === GATEWAY_CLIENT_NAMES.CLI) {
        return await callGatewayCli(opts);
    }
    if (Array.isArray(opts.scopes)) {
        return await callGatewayWithScopes({
            ...opts,
            mode: callerMode,
            clientName: callerName,
        }, opts.scopes);
    }
    return await callGatewayLeastPrivilege({
        ...opts,
        mode: callerMode,
        clientName: callerName,
    });
}
""".strip()
        + "\n",
        encoding="utf-8",
    )
    script = Path("deploy/patch_openclaw_restart_recovery_scope.py")

    first = subprocess.run(
        [sys.executable, str(script), "--openclaw-root", str(openclaw_root)],
        cwd=Path.cwd(),
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    second = subprocess.run(
        [sys.executable, str(script), "--openclaw-root", str(openclaw_root)],
        cwd=Path.cwd(),
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )

    assert first.returncode == 0, first.stderr
    assert json.loads(first.stdout)["status"] == "patched"
    assert second.returncode == 0, second.stderr
    assert json.loads(second.stdout)["status"] == "already_patched"
    legacy_runtime = runtime.read_text(encoding="utf-8").replace(
        "        useStoredDeviceAuth: true,\n",
        "",
    )
    runtime.write_text(legacy_runtime, encoding="utf-8")
    legacy_tools = tools_runtime.read_text(encoding="utf-8").replace(
        ", useStoredDeviceAuth: true",
        "",
    )
    tools_runtime.write_text(legacy_tools, encoding="utf-8")
    legacy_gateway = gateway_runtime.read_text(encoding="utf-8").replace(
        "url: useLocalOperatorReadIdentity ? void 0 : gateway.url,",
        "url: gateway.url,",
    ).replace(
        "token: useLocalOperatorReadIdentity ? void 0 : gateway.token,",
        "token: gateway.token,",
    ).replace(
        "useStoredDeviceAuth: useLocalOperatorReadIdentity,\n",
        "",
    )
    gateway_runtime.write_text(legacy_gateway, encoding="utf-8")
    legacy_call = re.sub(
        r"    const localStoredAuthContext =.*?^    }\n",
        "    if (callerMode === GATEWAY_CLIENT_MODES.CLI || callerName === GATEWAY_CLIENT_NAMES.CLI) {\n"
        "        return await callGatewayCli(opts);\n"
        "    }\n",
        call_runtime.read_text(encoding="utf-8"),
        count=1,
        flags=re.MULTILINE | re.DOTALL,
    )
    legacy_call = legacy_call.replace(
        "    return await callGatewayLeastPrivilege({",
        "    const defaultLocalContext =\n"
        "        opts.mode === void 0 &&\n"
        "        opts.clientName === void 0 &&\n"
        "        opts.url === void 0 &&\n"
        "        opts.token === void 0 &&\n"
        "        opts.password === void 0\n"
        "            ? await resolveGatewayCallContext(opts)\n"
        "            : null;\n"
        "    if (defaultLocalContext && !defaultLocalContext.urlOverride && !defaultLocalContext.isRemoteMode) {\n"
        "        return await callGatewayCli({ ...opts, useStoredDeviceAuth: true });\n"
        "    }\n"
        "    return await callGatewayLeastPrivilege({",
        1,
    )
    call_runtime.write_text(legacy_call, encoding="utf-8")
    upgrade = subprocess.run(
        [sys.executable, str(script), "--openclaw-root", str(openclaw_root)],
        cwd=Path.cwd(),
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    assert upgrade.returncode == 0, upgrade.stderr
    assert json.loads(upgrade.stdout)["status"] == "patched"
    patched = runtime.read_text(encoding="utf-8")
    assert patched.count('clientName: "cli"') == 2
    assert patched.count('mode: "cli"') == 2
    assert patched.count("useStoredDeviceAuth: true") == 2
    patched_tools = tools_runtime.read_text(encoding="utf-8")
    assert patched_tools.count('clientName: "cli"') == 4
    assert patched_tools.count('mode: "cli"') == 4
    assert patched_tools.count("useStoredDeviceAuth: true") == 4
    assert "opts?.callGateway ?? callGateway" not in patched_tools
    assert "let openClawToolsDeps = { callGateway };" not in patched_tools
    assert "let openClawToolsDeps = { callGateway: callGatewayAsCli };" in patched_tools
    patched_gateway = gateway_runtime.read_text(encoding="utf-8")
    assert "const useLocalOperatorReadIdentity =" in patched_gateway
    assert "scopes.every((scope) => scope === \"operator.read\")" in patched_gateway
    assert (
        "clientName: useLocalOperatorReadIdentity ? GATEWAY_CLIENT_NAMES.CLI : "
        "GATEWAY_CLIENT_NAMES.GATEWAY_CLIENT"
    ) in patched_gateway
    assert (
        "mode: useLocalOperatorReadIdentity ? GATEWAY_CLIENT_MODES.CLI : "
        "GATEWAY_CLIENT_MODES.BACKEND"
    ) in patched_gateway
    assert (
        "agentRuntimeIdentityToken && !useLocalOperatorReadIdentity"
        in patched_gateway
    )
    assert "url: useLocalOperatorReadIdentity ? void 0 : gateway.url" in patched_gateway
    assert "token: useLocalOperatorReadIdentity ? void 0 : gateway.token" in patched_gateway
    assert "useStoredDeviceAuth: useLocalOperatorReadIdentity" in patched_gateway
    patched_call = call_runtime.read_text(encoding="utf-8")
    assert "const localStoredAuthContext =" in patched_call
    assert "const useLocalStoredDeviceAuth =" in patched_call
    assert "opts.mode === void 0" in patched_call
    assert "opts.clientName === void 0" in patched_call
    assert "await resolveGatewayCallContext(opts)" in patched_call
    assert "!localStoredAuthContext.urlOverride" in patched_call
    assert "!localStoredAuthContext.isRemoteMode" in patched_call
    assert "callerMode === GATEWAY_CLIENT_MODES.CLI" in patched_call
    assert "callerName === GATEWAY_CLIENT_NAMES.CLI" in patched_call
    assert "return await callGatewayCli({ ...opts, useStoredDeviceAuth: true });" in patched_call
    dropin = Path("deploy/systemd/openclaw-gateway-eimemory.conf").read_text(encoding="utf-8")
    assert "ExecStartPre=" in dropin
    assert "patch_openclaw_restart_recovery_scope.py" in dropin


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


def test_eibrain_rpc_service_requires_protected_auth_environment() -> None:
    unit_text = Path("deploy/systemd/eimemory-rpc.service").read_text(encoding="utf-8")

    assert "EnvironmentFile=/etc/eimemory/rpc.env" in unit_text
    assert "EnvironmentFile=-" not in unit_text
    assert "deploy/ensure_rpc_auth.py" in Path("deploy/install_immutable_release.sh").read_text(encoding="utf-8")


def test_rpc_auth_provisioner_creates_strong_private_token_and_rejects_weak_file(tmp_path) -> None:
    from deploy.ensure_rpc_auth import RPCAuthError, ensure_rpc_auth_file

    path = tmp_path / "rpc.env"
    report = ensure_rpc_auth_file(path)
    token = path.read_text(encoding="utf-8").strip().split("=", 1)[1]

    assert report["created"] is True
    assert len(token) >= 43
    if os.name == "posix":
        assert path.stat().st_mode & 0o777 == 0o640

    path.write_text("EIMEMORY_RPC_AUTH_TOKEN=weak\n", encoding="utf-8")
    with pytest.raises(RPCAuthError, match="weak"):
        ensure_rpc_auth_file(path)


def test_rpc_auth_provisioner_is_idempotent_and_rejects_extra_environment_entries(tmp_path) -> None:
    from deploy.ensure_rpc_auth import RPCAuthError, ensure_rpc_auth_file

    path = tmp_path / "rpc.env"
    first = ensure_rpc_auth_file(path)
    original = path.read_text(encoding="utf-8")
    second = ensure_rpc_auth_file(path)

    assert first["created"] is True
    assert second["created"] is False
    assert path.read_text(encoding="utf-8") == original
    path.write_text(f"{original}PYTHONPATH=/tmp/untrusted\n", encoding="utf-8")
    with pytest.raises(RPCAuthError, match="malformed"):
        ensure_rpc_auth_file(path)


def test_rpc_auth_provisioner_rejects_group_executable_secret_file(tmp_path) -> None:
    if os.name != "posix":
        pytest.skip("POSIX permission bits required")
    from deploy.ensure_rpc_auth import RPCAuthError, ensure_rpc_auth_file

    path = tmp_path / "rpc.env"
    ensure_rpc_auth_file(path)
    path.chmod(0o650)

    with pytest.raises(RPCAuthError, match="permissions"):
        ensure_rpc_auth_file(path)


def test_rpc_auth_provisioner_rejects_hardlinked_secret_file(tmp_path) -> None:
    from deploy.ensure_rpc_auth import RPCAuthError, ensure_rpc_auth_file

    path = tmp_path / "rpc.env"
    ensure_rpc_auth_file(path)
    alias = tmp_path / "rpc-copy.env"
    try:
        os.link(path, alias)
    except OSError:
        pytest.skip("hard links are unavailable")

    with pytest.raises(RPCAuthError, match="single-link"):
        ensure_rpc_auth_file(path)


def test_rpc_auth_provisioner_does_not_replace_a_racing_existing_file(tmp_path, monkeypatch) -> None:
    from deploy import ensure_rpc_auth as helper

    path = tmp_path / "rpc.env"
    original_link = helper.os.link

    def racing_link(source, target, **kwargs):
        target_path = Path(target)
        target_path.write_text("EIMEMORY_RPC_AUTH_TOKEN=weak\n", encoding="utf-8")
        return original_link(source, target, **kwargs)

    monkeypatch.setattr(helper.os, "link", racing_link)
    with pytest.raises(helper.RPCAuthError, match="weak"):
        helper.ensure_rpc_auth_file(path)

    assert path.read_text(encoding="utf-8") == "EIMEMORY_RPC_AUTH_TOKEN=weak\n"


def test_openclaw_bridge_config_enables_required_conversation_access_atomically(tmp_path) -> None:
    from deploy.ensure_openclaw_bridge_config import ensure_openclaw_bridge_config

    path = tmp_path / "openclaw.json"
    path.write_text(
        json.dumps(
            {
                "plugins": {
                    "allow": ["existing-plugin"],
                    "entries": {
                        "eimemory-bridge": {
                            "hooks": {"allowPromptInjection": False},
                        }
                    },
                },
                "unrelated": {"preserved": True},
            }
        ),
        encoding="utf-8",
    )

    first = ensure_openclaw_bridge_config(path)
    second = ensure_openclaw_bridge_config(path)
    payload = json.loads(path.read_text(encoding="utf-8"))

    assert first["changed"] is True
    assert second["changed"] is False
    assert payload["plugins"]["allow"] == ["existing-plugin", "eimemory-bridge"]
    assert payload["plugins"]["entries"]["eimemory-bridge"]["enabled"] is True
    assert payload["plugins"]["entries"]["eimemory-bridge"]["hooks"] == {
        "allowPromptInjection": False,
        "allowConversationAccess": True,
    }
    assert payload["unrelated"] == {"preserved": True}


def test_openclaw_bridge_config_creates_missing_allow_policy(tmp_path) -> None:
    from deploy.ensure_openclaw_bridge_config import ensure_openclaw_bridge_config

    path = tmp_path / "openclaw.json"
    path.write_text(json.dumps({"plugins": {"entries": {}}}), encoding="utf-8")

    ensure_openclaw_bridge_config(path)

    assert json.loads(path.read_text(encoding="utf-8"))["plugins"]["allow"] == ["eimemory-bridge"]


def test_openclaw_bridge_config_serializes_concurrent_updates(tmp_path) -> None:
    from deploy.ensure_openclaw_bridge_config import ensure_openclaw_bridge_config

    path = tmp_path / "openclaw.json"
    path.write_text(json.dumps({"plugins": {"entries": {}}, "preserved": {"value": 1}}), encoding="utf-8")

    with ThreadPoolExecutor(max_workers=8) as pool:
        reports = list(pool.map(lambda _index: ensure_openclaw_bridge_config(path), range(24)))

    payload = json.loads(path.read_text(encoding="utf-8"))
    assert all(report["ok"] is True for report in reports)
    assert payload["plugins"]["allow"] == ["eimemory-bridge"]
    assert payload["plugins"]["entries"]["eimemory-bridge"]["enabled"] is True
    assert payload["preserved"] == {"value": 1}


def test_openclaw_bridge_config_rejects_invalid_or_unsafe_configuration(tmp_path) -> None:
    from deploy.ensure_openclaw_bridge_config import OpenClawBridgeConfigError, ensure_openclaw_bridge_config

    path = tmp_path / "openclaw.json"
    path.write_text("[]\n", encoding="utf-8")
    with pytest.raises(OpenClawBridgeConfigError, match="object"):
        ensure_openclaw_bridge_config(path)

    path.write_text(json.dumps({"plugins": {"allow": "eimemory-bridge"}}), encoding="utf-8")
    with pytest.raises(OpenClawBridgeConfigError, match="plugins.allow"):
        ensure_openclaw_bridge_config(path)


def test_immutable_installer_enforces_and_inspects_openclaw_bridge_compatibility() -> None:
    script = Path("deploy/install_immutable_release.sh").read_text(encoding="utf-8")

    assert "deploy/ensure_openclaw_bridge_config.py" in script
    assert "plugins inspect eimemory-bridge --runtime --json" in script
    assert "deploy/verify_openclaw_plugin_runtime.py" in script
    assert script.index("deploy/ensure_openclaw_bridge_config.py") < script.rindex("_user_systemctl restart openclaw-gateway.service")


def test_openclaw_runtime_verifier_requires_loaded_hooks_tools_and_clean_diagnostics(tmp_path) -> None:
    from deploy.verify_openclaw_plugin_runtime import OpenClawRuntimeError, verify_openclaw_plugin_runtime

    root = tmp_path / "eimemory-bridge"
    root.mkdir()
    payload = {
        "plugin": {
            "id": "eimemory-bridge",
            "rootDir": str(root),
            "enabled": True,
            "activated": True,
            "status": "loaded",
            "toolNames": ["eimemory_bridge_status", "memory_e2e_check"],
            "contracts": {"tools": ["eimemory_bridge_status", "memory_e2e_check"]},
        },
        "typedHooks": [
            {"name": name}
            for name in (
                "after_tool_call",
                "agent_end",
                "before_agent_finalize",
                "before_prompt_build",
                "before_tool_call",
                "message_received",
                "message_sent",
                "session_end",
            )
        ],
        "diagnostics": [],
        "compatibility": [],
    }

    report = verify_openclaw_plugin_runtime(payload, expected_root=root)

    assert report == {"ok": True, "plugin_id": "eimemory-bridge", "hook_count": 8, "tool_count": 2}
    payload["plugin"]["toolNames"] = ["eimemory_bridge_status"]
    with pytest.raises(OpenClawRuntimeError, match="runtime tools"):
        verify_openclaw_plugin_runtime(payload, expected_root=root)
    payload["plugin"]["toolNames"] = ["eimemory_bridge_status", "memory_e2e_check"]
    payload["diagnostics"] = [{"level": "error", "message": "stale manifest"}]
    with pytest.raises(OpenClawRuntimeError, match="diagnostics"):
        verify_openclaw_plugin_runtime(payload, expected_root=root)


def test_eimemory_rpc_cleanup_script_kills_only_matching_port_listeners() -> None:
    script = Path("deploy/systemd/eimemory-rpc-cleanup-port.sh").read_text(encoding="utf-8")

    assert script.startswith("#!/usr/bin/env bash\n")
    assert "ss -ltnp" in script
    assert "pid=[0-9]+" in script
    assert "*eimemory*\"$MATCH\"*" in script
    assert "kill -TERM" in script
    assert "kill -KILL" in script


def test_openclaw_watchdog_systemd_limits_stuck_and_hook_pressure() -> None:
    unit_text = Path("deploy/systemd/openclaw-stuck-watchdog.service").read_text(encoding="utf-8")

    assert "--threshold-s 120" in unit_text
    assert "--min-restart-interval-s 300" in unit_text
    assert "--max-hook-processes 8" in unit_text
    assert "--max-hook-rss-mib 1536" in unit_text
    assert "--health-url" not in unit_text
    assert "--loopback-health-url" not in unit_text
    assert "TimeoutStartSec=30" in unit_text


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
    assert "Environment=PYTHONDONTWRITEBYTECODE=1" in override_text
    assert "Environment=PYTHONPYCACHEPREFIX=/var/lib/eimemory/.pycache/@EIMEMORY_COMMIT@" in override_text
    assert "Environment=EIMEMORY_RUNTIME_COMMIT=@EIMEMORY_COMMIT@" in override_text
    assert 'Environment="EIMEMORY_HOOK_COMMAND=/opt/eimemory/current/.venv/bin/eimemory openclaw-hook"' in override_text
    assert 'Environment="EIMEMORY_BRIDGE_COMMAND=/opt/eimemory/current/.venv/bin/eimemory ei-bridge feishu"' in override_text
    assert "/dev-project/eimemory/.venv" not in override_text
    assert "PYTHONPATH=/dev-project/eimemory" not in override_text
    assert "MemoryAccounting=yes" in override_text
    assert "MemoryHigh=3G" in override_text
    assert "MemoryMax=4G" in override_text
    assert "MemorySwapMax=512M" in override_text
    assert "TasksAccounting=yes" in override_text
    assert "TasksMax=96" in override_text
    assert "OOMPolicy=kill" in override_text


def test_python_systemd_units_never_write_bytecode_into_immutable_release() -> None:
    for unit_path in Path("deploy/systemd").glob("*.service"):
        unit_text = unit_path.read_text(encoding="utf-8")
        launches_release_python = "/opt/eimemory/current/.venv/bin/" in unit_text
        for script_path in Path("deploy/systemd").glob("*.sh"):
            if script_path.name in unit_text and "/opt/eimemory/current/.venv/bin/" in script_path.read_text(
                encoding="utf-8"
            ):
                launches_release_python = True
        if launches_release_python:
            assert "Environment=PYTHONDONTWRITEBYTECODE=1" in unit_text, unit_path.name
            assert "Environment=PYTHONPYCACHEPREFIX=/var/lib/eimemory/.pycache/runtime" in unit_text, unit_path.name
            assert not re.search(r"PYTHONPYCACHEPREFIX=.*?/\d+\.\d+\.\d+", unit_text), unit_path.name

    gate_script = Path("deploy/systemd/eimemory-l5-observation-gate.sh").read_text(encoding="utf-8")
    assert "export PYTHONDONTWRITEBYTECODE=1" in gate_script
    assert 'release_id="$(basename "$(readlink -f /opt/eimemory/current)")"' in gate_script
    assert 'export PYTHONPYCACHEPREFIX="/var/lib/eimemory/.pycache/$release_id"' in gate_script


def test_runtime_pycache_prefix_redirects_explicit_compileall(tmp_path) -> None:
    package = tmp_path / "package"
    package.mkdir()
    (package / "module.py").write_text("VALUE = 1\n", encoding="utf-8")
    pycache_root = tmp_path / "runtime-pycache"
    env = os.environ.copy()
    env["PYTHONPYCACHEPREFIX"] = str(pycache_root)

    result = subprocess.run(
        [sys.executable, "-m", "compileall", "-q", str(package)],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert not (package / "__pycache__").exists()
    assert list(pycache_root.rglob("*.pyc"))


def test_commit_scoped_pycache_does_not_reuse_same_mtime_same_size_bytecode(tmp_path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    module = source / "release_module.py"
    fixed_mtime = 1_700_000_000

    def import_value(commit: str, content: str) -> str:
        module.write_text(content, encoding="utf-8")
        os.utime(module, (fixed_mtime, fixed_mtime))
        env = os.environ.copy()
        env["PYTHONPYCACHEPREFIX"] = str(tmp_path / "pycache" / commit)
        result = subprocess.run(
            [sys.executable, "-c", "import release_module; print(release_module.VALUE)"],
            cwd=source,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, result.stderr
        return result.stdout.strip()

    first = import_value("a" * 40, "VALUE = 'one'\n")
    second = import_value("b" * 40, "VALUE = 'two'\n")

    assert first == "one"
    assert second == "two"


def test_immutable_release_installer_documents_non_editable_runtime() -> None:
    script = Path("deploy/install_immutable_release.sh").read_text(encoding="utf-8")

    assert "git -C \"$REPO_DIR\" archive \"$COMMIT\"" in script
    assert "pip install \"$STAGE_DIR\"" in script
    assert "pip install -e" not in script
    assert "/opt/eimemory" in script


def test_immutable_release_installer_deploys_gateway_runtime_override() -> None:
    script = Path("deploy/install_immutable_release.sh").read_text(encoding="utf-8")

    assert 'install_managed_systemd_dropin.py' in script
    assert '_run_as_service_user mkdir -p "$USER_SYSTEMD_DIR/openclaw-gateway.service.d"' in script
    assert '"$RELEASE_DIR/deploy/systemd/openclaw-gateway-eimemory.conf"' in script
    assert '"$USER_SYSTEMD_DIR/openclaw-gateway.service.d/90-eimemory-runtime.conf"' in script
    assert script.index("install_managed_systemd_dropin.py") < script.index(
        '"$RELEASE_DIR/deploy/systemd/eimemory-rpc.service"'
    )


def test_immutable_release_installer_deploys_python_runtime_protection_dropins() -> None:
    script = Path("deploy/install_immutable_release.sh").read_text(encoding="utf-8")
    discovery = Path("deploy/discover_python_runtime_units.sh").read_text(encoding="utf-8")
    runtime_dropin = Path("deploy/systemd/eimemory-python-runtime.conf").read_text(encoding="utf-8")
    expected_units = {
        "eimemory-audit-verify.service",
        "eimemory-console.service",
        "eimemory-l5-observation-gate.service",
        "eimemory-learn-dashboard.service",
        "eimemory-learn-think.service",
        "eimemory-learn-watch.service",
        "eimemory-nightly.service",
        "eimemory-rpc.service",
        "eimemory-timer-monitor.service",
        "openclaw-loop-watch.service",
        "openclaw-loop-compact.service",
        "openclaw-stuck-watchdog.service",
    }

    assert 'eimemory-python-runtime.conf' in script
    assert '90-eimemory-python-runtime.conf' in script
    assert script.count("--render-commit") == 3
    assert '--render-commit "$target_commit"' in script
    assert 'bash -s -- "$USER_SYSTEMD_DIR"' in script
    assert "Unable to discover Python runtime systemd units" in script
    assert "Environment=EIMEMORY_RUNTIME_COMMIT=@EIMEMORY_COMMIT@" in runtime_dropin
    assert "find \"$USER_SYSTEMD_DIR\" -maxdepth 1 -type f -name '*.service'" in discovery
    assert "grep -Fq '/opt/eimemory/current'" in discovery
    for unit in expected_units:
        assert unit in discovery


def test_immutable_release_installer_manages_truthful_loop_watchdog_unit() -> None:
    script = Path("deploy/install_immutable_release.sh").read_text(encoding="utf-8")
    service = Path("deploy/systemd/openclaw-loop-watch.service").read_text(encoding="utf-8")
    timer = Path("deploy/systemd/openclaw-loop-watch.timer").read_text(encoding="utf-8")

    assert "openclaw-loop-watch.service" in script
    assert "openclaw-loop-watch.timer" in script
    assert "_user_systemctl enable --now openclaw-loop-watch.timer" in script
    assert "openclaw_loop.py watch" in service
    assert "|| true" not in service
    assert "OnUnitActiveSec=5min" in timer


def test_immutable_release_installer_manages_user_level_loop_compaction_timer() -> None:
    script = Path("deploy/install_immutable_release.sh").read_text(encoding="utf-8")
    service = Path("deploy/systemd/openclaw-loop-compact.service").read_text(encoding="utf-8")
    timer = Path("deploy/systemd/openclaw-loop-compact.timer").read_text(encoding="utf-8")

    assert '"$RELEASE_DIR/deploy/systemd/openclaw-loop-compact.service"' in script
    assert '"$USER_SYSTEMD_DIR/openclaw-loop-compact.service"' in script
    assert '"$RELEASE_DIR/deploy/systemd/openclaw-loop-compact.timer"' in script
    assert '"$USER_SYSTEMD_DIR/openclaw-loop-compact.timer"' in script
    assert "_user_systemctl enable --now openclaw-loop-compact.timer" in script
    assert "openclaw_loop.py compact --terminal-retention-days 7" in service
    assert "OnCalendar=*-*-* 04:10:00" in timer


def test_immutable_release_installer_manages_stuck_watchdog_timer() -> None:
    script = Path("deploy/install_immutable_release.sh").read_text(encoding="utf-8")

    assert '"$RELEASE_DIR/deploy/systemd/openclaw-stuck-watchdog.service"' in script
    assert '"$USER_SYSTEMD_DIR/openclaw-stuck-watchdog.service"' in script
    assert '"$RELEASE_DIR/deploy/systemd/openclaw-stuck-watchdog.timer"' in script
    assert '"$USER_SYSTEMD_DIR/openclaw-stuck-watchdog.timer"' in script
    assert "_user_systemctl enable --now openclaw-stuck-watchdog.timer" in script


def test_immutable_release_installer_manages_feishu_reply_watchdog() -> None:
    script = Path("deploy/install_immutable_release.sh").read_text(encoding="utf-8")
    unit = Path("deploy/systemd/openclaw-feishu-reply-watchdog.service").read_text(encoding="utf-8")

    assert '"$RELEASE_DIR/deploy/systemd/openclaw-feishu-reply-watchdog.service"' in script
    assert '"$USER_SYSTEMD_DIR/openclaw-feishu-reply-watchdog.service"' in script
    assert "_user_systemctl enable openclaw-feishu-reply-watchdog.service" in script
    assert "_user_systemctl restart openclaw-feishu-reply-watchdog.service" in script
    assert "/home/darrow/.local/bin" in unit
    assert "/home/darrow/n/bin" in unit


def test_managed_systemd_dropin_installer_uses_posix_directory_fds() -> None:
    helper = Path("deploy/install_managed_systemd_dropin.py").read_text(encoding="utf-8")

    assert "os.O_DIRECTORY | os.O_NOFOLLOW" in helper
    assert "for component in parts[1:]" in helper
    assert "dir_fd=root_fd" in helper
    assert "src_dir_fd=parent_fd, dst_dir_fd=parent_fd" in helper


def test_managed_systemd_dropin_installer_is_atomic_and_preserves_unmanaged_files(tmp_path) -> None:
    helper = _load_managed_systemd_dropin_installer()
    source = tmp_path / "source.conf"
    source.write_text(f"{helper.MANAGED_MARKER}\n[Service]\nEnvironment=SAFE=1\n", encoding="utf-8")
    root = tmp_path / "systemd"
    dropin_dir = root / "openclaw-gateway.service.d"
    dropin_dir.mkdir(parents=True)
    unmanaged = dropin_dir / "local-secret.conf"
    unmanaged.write_text("Environment=LOCAL_SECRET=preserve\n", encoding="utf-8")
    target = dropin_dir / "90-eimemory-runtime.conf"

    helper.install_managed_dropin(source=source, target=target, root=root)

    assert target.read_text(encoding="utf-8") == source.read_text(encoding="utf-8")
    assert unmanaged.read_text(encoding="utf-8") == "Environment=LOCAL_SECRET=preserve\n"
    assert not list(dropin_dir.glob(".eimemory-dropin-*"))

    source.write_text(f"{helper.MANAGED_MARKER}\n[Service]\nEnvironment=SAFE=2\n", encoding="utf-8")
    helper.install_managed_dropin(source=source, target=target, root=root)
    assert "Environment=SAFE=2" in target.read_text(encoding="utf-8")


def test_managed_systemd_dropin_installer_renders_release_commit(tmp_path) -> None:
    helper = _load_managed_systemd_dropin_installer()
    source = tmp_path / "source.conf"
    source.write_text(
        f"{helper.MANAGED_MARKER}\n[Service]\nEnvironment=PYTHONPYCACHEPREFIX=/cache/@EIMEMORY_COMMIT@\n",
        encoding="utf-8",
    )
    root = tmp_path / "systemd"
    target = root / "example.service.d" / "90-runtime.conf"
    target.parent.mkdir(parents=True)
    commit = "a" * 40

    helper.install_managed_dropin(source=source, target=target, root=root, render_commit=commit)

    rendered = target.read_text(encoding="utf-8")
    assert f"Environment=PYTHONPYCACHEPREFIX=/cache/{commit}" in rendered
    assert "@EIMEMORY_COMMIT@" not in rendered


def test_managed_systemd_dropin_installer_rejects_unmanaged_target(tmp_path) -> None:
    helper = _load_managed_systemd_dropin_installer()
    source = tmp_path / "source.conf"
    source.write_text(f"{helper.MANAGED_MARKER}\n[Service]\n", encoding="utf-8")
    root = tmp_path / "systemd"
    target = root / "openclaw-gateway.service.d" / "90-eimemory-runtime.conf"
    target.parent.mkdir(parents=True)
    target.write_text("Environment=LOCAL_SECRET=do-not-overwrite\n", encoding="utf-8")

    with pytest.raises(helper.ManagedDropinError, match="not managed"):
        helper.install_managed_dropin(source=source, target=target, root=root)

    assert target.read_text(encoding="utf-8") == "Environment=LOCAL_SECRET=do-not-overwrite\n"


def test_managed_systemd_dropin_installer_rejects_symlink_target(tmp_path) -> None:
    helper = _load_managed_systemd_dropin_installer()
    source = tmp_path / "source.conf"
    source.write_text(f"{helper.MANAGED_MARKER}\n[Service]\n", encoding="utf-8")
    root = tmp_path / "systemd"
    target = root / "openclaw-gateway.service.d" / "90-eimemory-runtime.conf"
    target.parent.mkdir(parents=True)
    outside = tmp_path / "outside.conf"
    outside.write_text("outside\n", encoding="utf-8")
    try:
        target.symlink_to(outside)
    except OSError:
        pytest.skip("file symlinks require additional Windows privileges")

    with pytest.raises(helper.ManagedDropinError, match="symlink"):
        helper.install_managed_dropin(source=source, target=target, root=root)

    assert outside.read_text(encoding="utf-8") == "outside\n"


def test_managed_systemd_dropin_installer_rejects_ancestor_symlink(tmp_path) -> None:
    if os.name != "posix":
        pytest.skip("POSIX openat behavior is required")
    helper = _load_managed_systemd_dropin_installer()
    source = tmp_path / "source.conf"
    source.write_text(f"{helper.MANAGED_MARKER}\n[Service]\n", encoding="utf-8")
    real_home = tmp_path / "real-home"
    root = real_home / "systemd" / "user"
    root.mkdir(parents=True)
    linked_home = tmp_path / "linked-home"
    linked_home.symlink_to(real_home, target_is_directory=True)
    linked_root = linked_home / "systemd" / "user"
    target = linked_root / "openclaw-gateway.service.d" / "90-eimemory-runtime.conf"

    with pytest.raises(helper.ManagedDropinError, match="without symlink components"):
        helper.install_managed_dropin(source=source, target=target, root=linked_root)


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


def test_immutable_release_installer_checks_release_exists_before_strict_resolution() -> None:
    script = Path("deploy/install_immutable_release.sh").read_text(encoding="utf-8")

    existence_guard = '[[ -d "$RELEASE_DIR" && ! -L "$RELEASE_DIR" ]]'
    strict_resolution = "resolve(strict=True)"
    assert existence_guard in script
    assert script.index(existence_guard) < script.index(strict_resolution)


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


def test_immutable_release_installer_refreshes_runtime_metadata_when_release_is_already_current() -> None:
    script = Path("deploy/install_immutable_release.sh").read_text(encoding="utf-8")

    branch_start = script.index('if { [ -e "$CURRENT_LINK" ]')
    branch_end = script.index("\nfi", branch_start)
    already_current_branch = script[branch_start:branch_end]

    assert "_refresh_current_runtime_metadata" in already_current_branch
    assert already_current_branch.index("_refresh_current_runtime_metadata") < already_current_branch.index("exit 0")


@pytest.mark.parametrize("current_is_target", [True, False], ids=["already-current", "rollback"])
@pytest.mark.parametrize("source_drift", [False, True], ids=["bytecode-only", "source-drift"])
def test_immutable_release_installer_cleans_existing_release_before_strict_validation(
    tmp_path, current_is_target, source_drift
) -> None:
    if os.name != "posix":
        pytest.skip("fd-safe bytecode cleanup requires POSIX")
    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
    install_root = tmp_path / "install"
    releases_root = install_root / "releases"
    release_dir = releases_root / commit
    release_dir.mkdir(parents=True)
    archive = tmp_path / "release.tar"
    subprocess.run(
        ["git", "archive", "--format=tar", f"--output={archive}", commit],
        check=True,
        capture_output=True,
        text=True,
    )
    shutil.unpack_archive(archive, release_dir)
    source_cache = release_dir / "eimemory" / "__pycache__"
    source_cache.mkdir()
    (source_cache / "version.cpython-314.pyc").write_bytes(b"generated bytecode")
    if source_drift:
        version_file = release_dir / "eimemory" / "version.py"
        version_file.write_text(version_file.read_text(encoding="utf-8") + "# drift\n", encoding="utf-8")

    trusted_python = tmp_path / "trusted-python"
    _write_installer_test_python(trusted_python)
    previous_release = releases_root / ("0" * 40)
    if current_is_target:
        current_target = release_dir
    else:
        previous_release.mkdir()
        current_target = previous_release
    current_link = install_root / "current"
    _create_directory_link(current_link, current_target)
    env = dict(os.environ)
    env.update(
        {
            "REPO_DIR": Path.cwd().as_posix(),
            "INSTALL_ROOT": install_root.as_posix(),
            "PYTHON_BIN": _bash_path(trusted_python),
            "EIMEMORY_ROOT": (tmp_path / "runtime").as_posix(),
            "EIMEMORY_CONFIG_DIR": (tmp_path / "config").as_posix(),
            "EIMEMORY_LOG_DIR": (tmp_path / "logs").as_posix(),
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

    assert not source_cache.exists()
    assert result.returncode == (2 if source_drift else 0), result.stderr
    assert ("already_current=1" in result.stdout) is (current_is_target and not source_drift)
    expected_current = current_target if source_drift else release_dir
    assert current_link.resolve() == expected_current.resolve()


def _write_installer_test_python(target: Path) -> None:
    real_python = Path(sys.executable).resolve()
    target.write_text(
        f"#!{_bash_path(real_python)}\n"
        "import os\n"
        "from pathlib import Path\n"
        "import sys\n"
        "args = sys.argv[1:]\n"
        "if args[:4] == ['-I', '-B', '-m', 'venv']:\n"
        "    bin_dir = Path(args[-1]) / 'bin'\n"
        "    bin_dir.mkdir(parents=True)\n"
        "    stub = '#!/usr/bin/env bash\\nexit 0\\n'\n"
        "    for name in ('python', 'eimemory'):\n"
        "        script = bin_dir / name\n"
        "        script.write_text(stub, encoding='utf-8')\n"
        "        script.chmod(0o755)\n"
        "    raise SystemExit(0)\n"
        f"os.execv({str(real_python)!r}, [{str(real_python)!r}, *args])\n",
        encoding="utf-8",
    )
    target.chmod(0o755)


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


def _load_module_from_path(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_managed_systemd_dropin_installer():
    path = Path("deploy/install_managed_systemd_dropin.py")
    spec = importlib.util.spec_from_file_location("install_managed_systemd_dropin", path)
    assert spec and spec.loader
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
    assert '_install_as_service_user 0644' in script
    assert '"$RELEASE_DIR/deploy/systemd/eimemory-rpc.service" "$USER_SYSTEMD_DIR/eimemory-rpc.service"' in script
    assert "_user_systemctl daemon-reload" in script
    assert "_user_systemctl enable eimemory-rpc.service" in script
    assert re.search(r"^\s*systemctl enable eimemory-rpc\.service", script, re.MULTILINE) is None
    assert "_retire_system_rpc_unit" in script
    assert "systemctl disable --now eimemory-rpc.service" in script
    assert "retired-by-eimemory-user-systemd" in script


def test_immutable_release_installer_restarts_runtimes_after_current_switch() -> None:
    script = Path("deploy/install_immutable_release.sh").read_text(encoding="utf-8")

    current_switch = script.index('mv -Tf "$CURRENT_LINK.next" "$CURRENT_LINK"')
    rpc_restart = script.rindex("_user_systemctl restart eimemory-rpc.service")
    gateway_restart = script.rindex("_user_systemctl restart openclaw-gateway.service")

    assert current_switch < rpc_restart < gateway_restart


def test_immutable_release_installer_commits_only_after_post_switch_gates() -> None:
    script = Path("deploy/install_immutable_release.sh").read_text(encoding="utf-8")

    switch = script.index('mv -Tf "$CURRENT_LINK.next" "$CURRENT_LINK"')
    acceptance = script.index("_run_post_switch_acceptance", switch)
    committed = script.index("COMMITTED=1", switch)

    assert "PREVIOUS_CURRENT" in script
    assert "_rollback_current_release" in script
    assert "verify_release_health.py" in script
    assert switch < acceptance < committed
    assert "rollback_current_release=failed" in script
    assert "rollback_preserved_failed_release=" in script


def test_immutable_release_installer_rejects_dangling_current_link_with_clear_error(tmp_path) -> None:
    if os.name != "posix":
        pytest.skip("installer dangling-link behavior requires POSIX symlinks")
    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
    install_root = tmp_path / "install"
    (install_root / "releases").mkdir(parents=True)
    (install_root / "current").symlink_to(tmp_path / "missing-release", target_is_directory=True)
    env = dict(os.environ)
    env.update(
        {
            "REPO_DIR": Path.cwd().as_posix(),
            "INSTALL_ROOT": install_root.as_posix(),
            "PYTHON_BIN": _bash_path(Path(sys.executable)),
            "USER_SYSTEMD_ENABLE_SERVICE": "0",
            "EIMEMORY_POST_SWITCH_GATES": "0",
        }
    )

    result = subprocess.run(
        [_bash_binary(), "deploy/install_immutable_release.sh", commit],
        cwd=Path.cwd(),
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )

    assert result.returncode == 2
    assert "Current release link is dangling or unresolvable" in result.stderr


def test_release_health_verifier_requires_exact_runtime_identity(tmp_path) -> None:
    from eimemory.runtime_identity import package_tree_digest

    module = _load_module_from_path(
        "verify_release_health",
        Path("deploy/verify_release_health.py"),
    )
    release = tmp_path / "releases" / ("a" * 40)
    package = release / "eimemory"
    package.mkdir(parents=True)
    (package / "version.py").write_text('__version__ = "1.9.70"\n', encoding="utf-8")
    payload = {
        "ok": True,
        "service": "eimemory-rpc",
        "commit": "a" * 40,
        "version": "1.9.70",
        "import_root": str(package),
        "package_tree_digest": package_tree_digest(package),
        "paths": {"release": str(release)},
    }

    assert module.verify_health_payload(
        payload,
        commit="a" * 40,
        version="1.9.70",
        release_dir=release,
    )["ok"] is True
    forged = {**payload, "commit": "b" * 40}
    report = module.verify_health_payload(
        forged,
        commit="a" * 40,
        version="1.9.70",
        release_dir=release,
    )
    assert report["ok"] is False
    assert "commit" in report["failed_checks"]

    unhealthy = {**payload, "ok": False}
    report = module.verify_health_payload(
        unhealthy,
        commit="a" * 40,
        version="1.9.70",
        release_dir=release,
    )
    assert report["ok"] is False
    assert "service_ok" in report["failed_checks"]

    wrong_service = {**payload, "service": "unrelated-service"}
    report = module.verify_health_payload(
        wrong_service,
        commit="a" * 40,
        version="1.9.70",
        release_dir=release,
    )
    assert report["ok"] is False
    assert "service_identity" in report["failed_checks"]


@pytest.mark.parametrize(
    "fail_stage",
    ["registry", "rpc_restart", "gateway_restart", "health", "receipt", "acceptance"],
)
def test_installer_restores_previous_release_after_post_switch_failure(tmp_path, fail_stage) -> None:
    if os.name != "posix":
        pytest.skip("installer rollback fault injection requires POSIX rename and dir_fd semantics")
    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
    install_root = tmp_path / "install"
    releases_root = install_root / "releases"
    old_release = releases_root / ("0" * 40)
    old_release.mkdir(parents=True)
    current_link = install_root / "current"
    _create_directory_link(current_link, old_release)
    trusted_python = tmp_path / "trusted-python"
    _write_installer_test_python(trusted_python)
    env = dict(os.environ)
    env.update(
        {
            "REPO_DIR": Path.cwd().as_posix(),
            "INSTALL_ROOT": install_root.as_posix(),
            "PYTHON_BIN": _bash_path(trusted_python),
            "EIMEMORY_ROOT": (tmp_path / "runtime").as_posix(),
            "EIMEMORY_CONFIG_DIR": (tmp_path / "config").as_posix(),
            "EIMEMORY_LOG_DIR": (tmp_path / "logs").as_posix(),
            "USER_SYSTEMD_ENABLE_SERVICE": "0",
            "EIMEMORY_POST_SWITCH_GATES": "0",
            "EIMEMORY_DEPLOY_FAIL_STAGE": fail_stage,
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
        encoding="utf-8",
        errors="replace",
        check=False,
    )

    assert result.returncode != 0
    assert current_link.resolve() == old_release.resolve()
    assert "rollback_current_release=restored" in result.stderr
    assert "commit_complete=1" not in result.stdout


def test_installer_does_not_claim_success_or_delete_candidate_when_rollback_fails(tmp_path) -> None:
    if os.name != "posix":
        pytest.skip("installer rollback fault injection requires POSIX rename semantics")
    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
    install_root = tmp_path / "install"
    releases_root = install_root / "releases"
    old_release = releases_root / ("0" * 40)
    old_release.mkdir(parents=True)
    current_link = install_root / "current"
    _create_directory_link(current_link, old_release)
    trusted_python = tmp_path / "trusted-python"
    _write_installer_test_python(trusted_python)
    env = dict(os.environ)
    env.update(
        {
            "REPO_DIR": Path.cwd().as_posix(),
            "INSTALL_ROOT": install_root.as_posix(),
            "PYTHON_BIN": _bash_path(trusted_python),
            "EIMEMORY_ROOT": (tmp_path / "runtime").as_posix(),
            "EIMEMORY_CONFIG_DIR": (tmp_path / "config").as_posix(),
            "EIMEMORY_LOG_DIR": (tmp_path / "logs").as_posix(),
            "USER_SYSTEMD_ENABLE_SERVICE": "0",
            "EIMEMORY_POST_SWITCH_GATES": "0",
            "EIMEMORY_DEPLOY_FAIL_STAGE": "registry",
            "EIMEMORY_DEPLOY_FAIL_ROLLBACK_STAGE": "link",
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
        encoding="utf-8",
        errors="replace",
        check=False,
    )

    assert result.returncode != 0
    assert "rollback_current_release=failed" in result.stderr
    assert "rollback_preserved_failed_release=" in result.stderr
    assert "rollback_current_release=restored" not in result.stderr
    assert (releases_root / commit).is_dir()


def test_immutable_release_installer_refreshes_openclaw_registry_before_gateway_restart() -> None:
    script = Path("deploy/install_immutable_release.sh").read_text(encoding="utf-8")

    current_switch = script.index('mv -Tf "$CURRENT_LINK.next" "$CURRENT_LINK"')
    registry_refresh = script.rindex("\n_refresh_openclaw_plugin_registry\n")
    gateway_restart = script.rindex("_user_systemctl restart openclaw-gateway.service")

    assert 'OPENCLAW_BIN="${OPENCLAW_BIN:-$SERVICE_HOME/n/bin/openclaw}"' in script
    assert 'plugins registry --refresh --json' in script
    assert current_switch < registry_refresh < gateway_restart


def test_python_runtime_unit_discovery_is_dynamic_deduplicated_and_regular_file_only(tmp_path) -> None:
    systemd_dir = tmp_path / "systemd"
    systemd_dir.mkdir()
    (systemd_dir / "custom-worker.service").write_text(
        "[Service]\nExecStart=/opt/eimemory/current/.venv/bin/python -m worker\n",
        encoding="utf-8",
    )
    (systemd_dir / "eimemory-rpc.service").write_text(
        "[Service]\nExecStart=/opt/eimemory/current/.venv/bin/python -m rpc\n",
        encoding="utf-8",
    )
    (systemd_dir / "irrelevant.service").write_text("[Service]\nExecStart=/usr/bin/true\n", encoding="utf-8")
    (systemd_dir / "directory.service").mkdir()
    outside = tmp_path / "outside.service"
    outside.write_text("[Service]\nExecStart=/opt/eimemory/current/.venv/bin/python -m outside\n", encoding="utf-8")
    linked = systemd_dir / "linked.service"
    try:
        linked.symlink_to(outside)
    except OSError:
        linked = None

    result = subprocess.run(
        [_bash_binary(), "deploy/discover_python_runtime_units.sh", _bash_path(systemd_dir)],
        cwd=Path.cwd(),
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    units = result.stdout.splitlines()
    assert units.count("eimemory-rpc.service") == 1
    assert units.count("custom-worker.service") == 1
    assert "irrelevant.service" not in units
    assert "directory.service" not in units
    if linked is not None:
        assert "linked.service" not in units


def test_python_runtime_unit_discovery_failure_propagates_to_installer(tmp_path) -> None:
    systemd_dir = tmp_path / "systemd"
    systemd_dir.mkdir()
    result = subprocess.run(
        [
            _bash_binary(),
            "-c",
            'find(){ return 7; }; export -f find; source "$1" "$2"',
            "bash",
            "deploy/discover_python_runtime_units.sh",
            _bash_path(systemd_dir),
        ],
        cwd=Path.cwd(),
        capture_output=True,
        text=True,
        check=False,
    )
    installer = Path("deploy/install_immutable_release.sh").read_text(encoding="utf-8")

    assert result.returncode == 7
    assert 'if ! PYTHON_RUNTIME_UNIT_OUTPUT="$(_run_as_service_user bash -s --' in installer
    assert "Unable to discover Python runtime systemd units" in installer


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
