"""A denied Hermes stop must never qualify the still-running old gateway.

Execute the real installer functions with a fake systemd transport only. No
production unit is queried or changed, and terminal-state verification is real.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys

import pytest


INSTALLER = Path(__file__).resolve().parents[1] / "deploy" / "install_immutable_release.sh"


def _function(name: str) -> str:
    script = INSTALLER.read_text(encoding="utf-8")
    marker = f"{name}() {{"
    return marker + script.split(marker, 1)[1].split("\n}", 1)[0] + "\n}\n"


def _restart(tmp_path: Path, scenario: str) -> tuple[subprocess.CompletedProcess[str], list[str]]:
    if shutil.which("bash") is None:
        pytest.skip("bash is required")
    profile = tmp_path / "hermes"
    profile.mkdir()
    (profile / "gateway.pid").write_text(json.dumps({"pid": 4242}), encoding="utf-8")
    trace = tmp_path / "systemctl-actions"
    # Fake state is held in the parent shell. show/is-active run in subshells
    # only to observe it; lifecycle calls mutate it like synchronous systemd.
    setup = f"""
USER_SYSTEMD_ENABLE_SERVICE=1
HERMES_HOME_DIR={shlex.quote(str(profile))}
PYTHON_BIN={shlex.quote(sys.executable)}
SCENARIO={shlex.quote(scenario)}
TRACE={shlex.quote(str(trace))}
UNIT_STATE=active
UNIT_PID=4242
systemctl() {{ return 0; }}
_hermes_is_installed() {{ return 0; }}
_user_systemctl() {{
  printf '%s\\n' "$*" >> "$TRACE"
  case "$1" in
    cat) return 0 ;;
    daemon-reload)
      [ "$SCENARIO" != reload_denied ] || return 42
      return 0 ;;
    stop)
      case "$SCENARIO" in
        stop_denied) return 42 ;;
        stop_still_active) return 0 ;;
        stop_deactivating) UNIT_STATE=deactivating; return 0 ;;
        stop_denied_inactive) UNIT_STATE=inactive; UNIT_PID=0; return 42 ;;
        graceful_exit_one) UNIT_STATE=inactive; UNIT_PID=0; return 1 ;;
        graceful_failed_state) UNIT_STATE=failed; UNIT_PID=0; return 1 ;;
        stop_unknown|stop_transport_unknown) UNIT_STATE=unknown; UNIT_PID=0; return 0 ;;
        *) UNIT_STATE=inactive; UNIT_PID=0; return 0 ;;
      esac ;;
    is-active)
      [ "$SCENARIO" != stop_transport_unknown ] || return 42
      case "$UNIT_STATE" in
        active) return 0 ;;
        *) return 3 ;;
      esac ;;
    show)
      case "$*" in
        *--property=ActiveState*)
          [ "$SCENARIO" != stop_unknown ] || return 46
          [ "$SCENARIO" != stop_transport_unknown ] || return 46
          printf '%s\\n' "$UNIT_STATE" ;;
        *--property=MainPID*) printf '%s\\n' "$UNIT_PID" ;;
        *) return 47 ;;
      esac ;;
    reset-failed) return 0 ;;
    start)
      [ "$SCENARIO" != start_denied ] || return 42
      # Starting an already active service is a no-op, not a replacement.
      if [ "$UNIT_STATE" != active ]; then
        UNIT_STATE=active
        UNIT_PID=5151
        printf '{{"pid":5151}}\\n' > "$HERMES_HOME_DIR/gateway.pid"
      fi
      return 0 ;;
    *) return 48 ;;
  esac
}}
"""
    result = subprocess.run(
        ["bash", "-s"],
        input=setup + _function("_storage_unit_is_active") + _function("_restart_gateway_unit")
        + _function("_restart_hermes_gateway")
        + "\n_restart_hermes_gateway\n",
        text=True,
        capture_output=True,
        timeout=10,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
    )
    return result, trace.read_text(encoding="utf-8").splitlines()


@pytest.mark.parametrize("scenario,diagnostic", [
    ("stop_denied", "stop_not_complete stop_exit=42"),
    ("stop_still_active", "stop_not_complete stop_exit=0"),
    ("stop_deactivating", "stop_not_complete stop_exit=0"),
    ("stop_unknown", "stop_state_unknown stop_exit=0"),
    ("stop_transport_unknown", "stop_state_unknown stop_exit=0"),
    ("stop_denied_inactive", "stop_failed stop_exit=42"),
])
def test_unconfirmed_stop_blocks_start_and_failure_reset(
    tmp_path: Path, scenario: str, diagnostic: str,
) -> None:
    result, actions = _restart(tmp_path, scenario)
    assert result.returncode != 0, (result.stdout, result.stderr, actions)
    assert "stop hermes-gateway.service" in actions
    assert "start hermes-gateway.service" not in actions
    assert "reset-failed hermes-gateway.service" not in actions
    assert "hermes_gateway_restart=ready" not in result.stdout
    assert f"hermes_gateway_restart=failed {diagnostic}" in result.stderr
    if scenario == "stop_deactivating":
        assert "show hermes-gateway.service --property=ActiveState --value" in actions
    if scenario == "stop_transport_unknown":
        assert "storage_writer_state=failed unit=hermes-gateway.service status=42" in result.stderr
        assert "show hermes-gateway.service --property=ActiveState --value" not in actions


@pytest.mark.parametrize("scenario", ["normal", "graceful_exit_one", "graceful_failed_state"])
def test_confirmed_terminal_stop_allows_fresh_gateway(tmp_path: Path, scenario: str) -> None:
    result, actions = _restart(tmp_path, scenario)
    assert result.returncode == 0, (result.stdout, result.stderr, actions)
    assert "show hermes-gateway.service --property=ActiveState --value" in actions
    stop = actions.index("stop hermes-gateway.service")
    state = actions.index("show hermes-gateway.service --property=ActiveState --value")
    reset = actions.index("reset-failed hermes-gateway.service")
    start = actions.index("start hermes-gateway.service")
    assert stop < state < reset < start
    assert "hermes_gateway_restart=ready managed_singleton=1" in result.stdout
    assert json.loads((tmp_path / "hermes" / "gateway.pid").read_text())["pid"] == 5151


def test_daemon_reload_failure_blocks_all_gateway_lifecycle_calls(tmp_path: Path) -> None:
    result, actions = _restart(tmp_path, "reload_denied")
    assert result.returncode != 0, (result.stdout, result.stderr, actions)
    assert "stop hermes-gateway.service" not in actions
    assert "start hermes-gateway.service" not in actions
    assert "reset-failed hermes-gateway.service" not in actions
    assert "hermes_gateway_restart=failed daemon_reload_failed" in result.stderr


def test_start_failure_is_reported_without_ready_verdict(tmp_path: Path) -> None:
    result, actions = _restart(tmp_path, "start_denied")
    assert result.returncode != 0, (result.stdout, result.stderr, actions)
    assert "start hermes-gateway.service" in actions
    assert "hermes_gateway_restart=ready" not in result.stdout
    assert "hermes_gateway_restart=failed start_failed" in result.stderr


def test_active_profile_gateways_restart_with_same_boundary(tmp_path: Path) -> None:
    """Active hermes-gateway-<profile> units restart stop-confirmed; inactive ones are skipped."""
    if shutil.which("bash") is None:
        pytest.skip("bash is required")
    home = tmp_path / "hermes"
    units = tmp_path / "units"
    units.mkdir()
    for name in ("hongtai", "idle"):
        (units / f"hermes-gateway-{name}.service").write_text("[Service]\n", encoding="utf-8")
        (home / "profiles" / name).mkdir(parents=True)
    (home / "gateway.pid").write_text(json.dumps({"pid": 10}), encoding="utf-8")
    trace = tmp_path / "trace"
    setup = f"""
USER_SYSTEMD_ENABLE_SERVICE=1
HERMES_HOME_DIR={shlex.quote(str(home))}
USER_SYSTEMD_DIR={shlex.quote(str(units))}
PYTHON_BIN={shlex.quote(sys.executable)}
TRACE={shlex.quote(str(trace))}
declare -A STATE=([hermes-gateway.service]=active [hermes-gateway-hongtai.service]=active [hermes-gateway-idle.service]=inactive)
declare -A PID=([hermes-gateway.service]=10 [hermes-gateway-hongtai.service]=20 [hermes-gateway-idle.service]=0)
systemctl() {{ return 0; }}
_hermes_is_installed() {{ return 0; }}
_pidfile() {{
  case "$1" in
    hermes-gateway.service) echo "$HERMES_HOME_DIR/gateway.pid" ;;
    *) local p="${{1#hermes-gateway-}}"; echo "$HERMES_HOME_DIR/profiles/${{p%.service}}/gateway.pid" ;;
  esac
}}
_user_systemctl() {{
  printf '%s\\n' "$*" >> "$TRACE"
  local unit="${{@: -1}}"
  case "$1" in
    cat|daemon-reload|reset-failed) return 0 ;;
    stop) STATE[$unit]=inactive; PID[$unit]=0; return 1 ;;
    is-active) [ "${{STATE[$unit]}}" = active ] && return 0 || return 3 ;;
    show)
      case "$*" in
        *ActiveState*) printf '%s\\n' "${{STATE[$2]}}" ;;
        *MainPID*) printf '%s\\n' "${{PID[$2]}}" ;;
      esac ;;
    start)
      STATE[$unit]=active; PID[$unit]=$((PID[$unit] + 900))
      printf '{{"pid":%s}}\\n' "${{PID[$unit]}}" > "$(_pidfile "$unit")" ;;
    *) return 48 ;;
  esac
}}
"""
    result = subprocess.run(
        ["bash", "-s"],
        input=setup + _function("_storage_unit_is_active") + _function("_restart_gateway_unit")
        + _function("_restart_hermes_gateway") + "\n_restart_hermes_gateway\n",
        text=True, capture_output=True, timeout=20,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
    )
    actions = trace.read_text(encoding="utf-8").splitlines()
    assert result.returncode == 0, (result.stdout, result.stderr, actions)
    assert "hermes_gateway_restart=ready managed_singleton=1\n" in result.stdout + "\n"
    assert "hermes_gateway_restart=ready managed_singleton=1 unit=hermes-gateway-hongtai.service" in result.stdout
    assert "hermes_gateway_restart=skipped_inactive unit=hermes-gateway-idle.service" in result.stdout
    assert actions.index("stop hermes-gateway-hongtai.service") < actions.index("start hermes-gateway-hongtai.service")
    assert "start hermes-gateway-idle.service" not in actions
    assert "stop hermes-gateway-idle.service" not in actions
