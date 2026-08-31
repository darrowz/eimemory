from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess

import pytest


SOURCE = Path("deploy/install_immutable_release.sh").read_text(encoding="utf-8")


def function(name: str) -> str:
    start = SOURCE.index(f"{name}() {{")
    return SOURCE[start:SOURCE.index("\n}", start) + 2]


def run(script: str) -> subprocess.CompletedProcess[str]:
    git_bash = Path("C:/Program Files/Git/bin/bash.exe")
    bash = str(git_bash) if git_bash.exists() else shutil.which("bash")
    if not bash:
        pytest.skip("bash unavailable")
    return subprocess.run([bash, "-c", "set -euo pipefail\n" + script],
                          text=True, capture_output=True, env=os.environ.copy())


@pytest.mark.parametrize("mode,installed,expected", [
    ("auto", False, "0"), ("auto", True, "1"),
    ("enabled", True, "1"), ("disabled", False, "0"),
])
def test_optional_adapter_topology(mode, installed, expected):
    result = run(function("_resolve_openclaw_adapter") + f"""
EIMEMORY_OPENCLAW_ADAPTER={mode}
OPENCLAW_BIN={'/bin/true' if installed else '/nonexistent-eimemory-openclaw'}
OPENCLAW_LOOP_CONFIG_PATH={'/dev/null' if installed else '/nonexistent-eimemory-config'}
USER_SYSTEMD_ENABLE_SERVICE=0
_resolve_openclaw_adapter
echo selected=$OPENCLAW_ADAPTER_ENABLED
""")
    assert result.returncode == 0, result.stderr
    assert f"selected={expected}" in result.stdout


@pytest.mark.parametrize("mode", ["enabled", "auto", "unexpected"])
def test_requested_or_partial_adapter_is_not_silently_disabled(mode):
    result = run(function("_resolve_openclaw_adapter") + f"""
EIMEMORY_OPENCLAW_ADAPTER={mode}
OPENCLAW_BIN=/nonexistent-eimemory-openclaw
OPENCLAW_LOOP_CONFIG_PATH=/dev/null
USER_SYSTEMD_ENABLE_SERVICE=0
_resolve_openclaw_adapter
""")
    assert result.returncode != 0


@pytest.mark.parametrize("name", [
    "_run_openclaw_loop_deploy_verify", "_install_openclaw_loop_compat_script",
    "_refresh_openclaw_plugin_registry", "_install_openclaw_bundled_bridge",
    "_inspect_openclaw_plugin_runtime", "_refresh_openclaw_gateway_metadata",
])
def test_disabled_adapter_never_calls_plugin_or_gateway(name):
    result = run(function("_openclaw_is_enabled") + "\n" + function(name) + f"""
OPENCLAW_ADAPTER_ENABLED=0
{name}
""")
    assert result.returncode == 0, result.stderr


def test_core_restart_does_not_require_openclaw():
    result = run(function("_openclaw_is_enabled") + "\n" + function("_restart_current_services") + """
OPENCLAW_ADAPTER_ENABLED=0
USER_SYSTEMD_ENABLE_SERVICE=1
command() { return 0; }
_pause_release_closure_reconcile() { :; }
_user_systemctl() { echo "$*"; }
_restart_hermes_gateway() { echo hermes; }
_restart_current_services
""")
    assert result.returncode == 0, result.stderr
    assert "restart eimemory-rpc.service" in result.stdout
    assert "hermes" in result.stdout
    assert "openclaw" not in result.stdout


def test_recovery_only_is_bound_and_exits_before_new_release_work():
    assert 'DEPLOY_MODE="${2:-deploy}"' in SOURCE
    assert '"$DEPLOY_MODE" = "--recover-only"' in SOURCE
    main = SOURCE[SOURCE.index('_ensure_runtime_dir "$EIMEMORY_ROOT" 0750\n_resolve_openclaw_adapter'):]
    reconcile = main.index("_reconcile_interrupted_storage_release")
    recovery = main.index('if [ "$DEPLOY_MODE" = "--recover-only" ]', reconcile)
    assert reconcile < recovery < main.index('BASELINE_PRIOR_COMMIT="$(_select_baseline_prior_commit)"')
    assert '_verify_release_health "$PREVIOUS_CURRENT" "$PREVIOUS_COMMIT"' in main[recovery:main.index("BASELINE_PRIOR_COMMIT=")]
    assert "exit 0" in main[recovery:main.index("BASELINE_PRIOR_COMMIT=")]
    assert "_verify_effective_runtime_metadata" in main[recovery:main.index("BASELINE_PRIOR_COMMIT=")]
    assert 'git -C "$REPO_DIR" diff --quiet HEAD -- deploy' in SOURCE
    assert '_install_openclaw_bundled_bridge "$PREVIOUS_CURRENT" "$REPO_DIR"' in SOURCE
    assert '_inspect_openclaw_plugin_runtime "$PREVIOUS_CURRENT" "$REPO_DIR" "1"' in SOURCE


def test_compatibility_preflight_precedes_storage_writer_stop():
    main = SOURCE[SOURCE.index('_provision_evidence_receipt_key\n_provision_hermes_attestation'):]
    assert main.index("_preflight_openclaw_adapter") < main.index("_observe_pre_switch_l5")
    assert main.index("_preflight_openclaw_adapter") < main.index("_prepare_storage_for_release")


def test_absent_adapter_not_a_required_identity_unit():
    identity = function("_verify_effective_runtime_metadata")
    assert "--exclude-openclaw" in identity
    required = identity[identity.index("local -a required_runtime_units=("):identity.index("local -A seen_runtime_units=")]
    assert "openclaw" not in required
    assert "_openclaw_is_enabled" in identity


def test_preswitch_failure_restores_adapter_before_removing_candidate():
    cleanup = function("cleanup_stage")
    assert "OPENCLAW_CONFIG_SWITCHED" in cleanup
    restore = cleanup.index('_install_openclaw_bundled_bridge "$PREVIOUS_CURRENT" "$REPO_DIR"')
    remove = cleanup.index('mv -T "$RELEASE_DIR" "$FAILED_DIR"')
    assert restore < remove
    assert "OPENCLAW_CONFIG_RESTORED" in cleanup[restore:remove]


@pytest.mark.parametrize("name", [
    "_refresh_openclaw_plugin_registry", "_inspect_openclaw_plugin_runtime",
    "_preflight_openclaw_adapter",
])
def test_cli_binds_the_same_config_as_external_plugin_installer(name):
    body = function(name)
    assert 'OPENCLAW_CONFIG_PATH="$OPENCLAW_LOOP_CONFIG_PATH"' in body


def test_rollback_verifies_selected_daemons_before_clearing_marker():
    body = function("_rollback_current_release")
    restart = body.index("_restart_current_services")
    verify = body.index('_verify_effective_runtime_metadata "$PREVIOUS_COMMIT" "$PREVIOUS_CURRENT" "$REPO_DIR"')
    clear = body.index("_clear_storage_release_transaction")
    assert restart < verify < clear
    assert "rollback_failed=1" in body[verify:clear]
    assert 'local policy_release="${3:-$target_release}"' in function("_verify_effective_runtime_metadata")
