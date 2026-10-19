"""Exact, reversible host ingress compatibility; no model or production calls."""
from __future__ import annotations

import ast
import asyncio
import getpass
import json
import os
from contextvars import ContextVar
from pathlib import Path
import runpy
import shlex
import shutil
import subprocess
import sys
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
PATCH = runpy.run_path(str(ROOT / "deploy/ensure_hermes_profile_boundary.py"))
INSTALL = PATCH["install"]
REAL_HOST = ROOT / "tests/fixtures/hermes_hosts/run_inbound_admission_main_e0550c97.py.txt"
SOURCE = '''from __future__ import annotations

# Unrelated operator customization must survive apply and removal.
OPERATOR_CUSTOM_SETTING = "retain-this-exactly"

class GatewayInboundMixin:
    async def _handle_message(self, event: MessageEvent) -> Optional[str]:
        """Native ingress; never include a real conversation in this fixture."""
        from gateway.run import _AGENT_PENDING_SENTINEL
        _admitted = await self._hm_admit_event(event)
        if _admitted is None:
            return None
        return await self.finish(event)

    def unrelated_method(self):
        return OPERATOR_CUSTOM_SETTING
'''


def _host(tmp_path, source=SOURCE):
    target = tmp_path / "gateway/run_inbound.py"
    target.parent.mkdir(parents=True)
    target.write_text(source, encoding="utf-8")
    return target


def test_install_dry_run_apply_check_idempotence_and_exact_removal(tmp_path):
    target = _host(tmp_path)
    original = target.read_bytes()
    dry = INSTALL(tmp_path)
    assert dry["contract"] == "legacy_admission" and not dry["applied"]
    assert target.read_bytes() == original
    with pytest.raises(RuntimeError, match="hermes_profile_boundary_missing"):
        INSTALL(tmp_path, check=True)
    applied = INSTALL(tmp_path, apply=True)
    assert Path(applied["backup"]).read_bytes() == original
    installed = target.read_bytes()
    assert installed != original and PATCH["INSERT"] in installed.decode().replace("\r\n", "\n")
    again = INSTALL(tmp_path, apply=True)
    assert again["already_installed"] and again["contract"] == "managed_compat"
    assert target.read_bytes() == installed
    assert INSTALL(tmp_path, check=True)["ok"]
    removed = INSTALL(tmp_path, apply=True, remove=True)
    assert removed["ok"] and target.read_bytes() == original
    assert INSTALL(tmp_path, apply=True, remove=True)["contract"] == "legacy_admission"


def test_existing_fifteen_line_patch_is_adopted_without_host_rewrite(tmp_path):
    target = _host(tmp_path)
    INSTALL(tmp_path, apply=True)
    installed = target.read_bytes()
    for backup in target.parent.glob("*.pre-eimemory-profile-boundary-*"):
        backup.unlink()
    before_stat = target.stat()
    adopted = INSTALL(tmp_path, apply=True)
    assert adopted["contract"] == "managed_compat" and adopted["already_installed"]
    assert target.read_bytes() == installed
    assert target.stat().st_mtime_ns == before_stat.st_mtime_ns
    assert Path(adopted["backup"]).read_text(encoding="utf-8") == SOURCE


def test_remove_preserves_operator_edits_added_after_install(tmp_path):
    target = _host(tmp_path)
    INSTALL(tmp_path, apply=True)
    target.write_text(target.read_text(encoding="utf-8") + "\nAFTER_INSTALL_OPERATOR_EDIT = True\n", encoding="utf-8")
    INSTALL(tmp_path, apply=True, remove=True)
    assert target.read_text(encoding="utf-8") == SOURCE + "\nAFTER_INSTALL_OPERATOR_EDIT = True\n"


def test_native_ast_equivalent_boundary_is_accepted_and_never_removed(tmp_path):
    target = _host(tmp_path)
    INSTALL(tmp_path, apply=True)
    native = target.read_text(encoding="utf-8").replace(PATCH["INNER"], "_handle_message_native")
    native = native.replace("# A standalone Feishu gateway belongs to its launch profile.",
                            "# Native upstream equivalent standalone boundary.")
    target.write_text(native, encoding="utf-8")
    result = INSTALL(tmp_path, check=True)
    assert result["contract"] == "native_equivalent"
    assert INSTALL(tmp_path, apply=True, remove=True)["contract"] == "native_equivalent"
    assert target.read_text(encoding="utf-8") == native


@pytest.mark.parametrize("mutation", ["admission", "signature", "partial", "finally", "collision", "syntax"])
def test_unknown_host_contract_fails_closed_without_any_mutation(tmp_path, mutation):
    target = _host(tmp_path)
    if mutation in {"partial", "finally"}:
        INSTALL(tmp_path, apply=True)
    source = target.read_text(encoding="utf-8")
    if mutation == "admission":
        source = source.replace("self._hm_admit_event(event)", "self.other_admission(event)")
    elif mutation == "signature":
        source = source.replace("self, event: MessageEvent", "self, event: MessageEvent, extra=None")
    elif mutation == "partial":
        source = source.replace('== Platform.FEISHU', '== Platform.SLACK')
    elif mutation == "finally":
        source = source.replace("reset_hermes_home_override(token)", "pass")
    elif mutation == "collision":
        source += "\n    async def _handle_message_in_profile(self, event):\n        return None\n"
    else:
        source += "\nthis is invalid python!"
    target.write_text(source, encoding="utf-8")
    before = target.read_bytes()
    with pytest.raises(RuntimeError, match="hermes_profile_boundary_contract_unknown"):
        INSTALL(tmp_path, apply=True)
    assert target.read_bytes() == before


def test_backup_conflict_refuses_adoption_without_touching_host(tmp_path):
    target = _host(tmp_path)
    expected = INSTALL(tmp_path)
    Path(expected["backup"]).write_text("operator file", encoding="utf-8")
    original = target.read_bytes()
    with pytest.raises(RuntimeError, match="backup_conflict"):
        INSTALL(tmp_path, apply=True)
    assert target.read_bytes() == original


def _runner(source, monkeypatch, *, home, multiplex=False):
    """Execute the real installed method bodies, not a copy of their algorithm."""
    tree = ast.parse(source)
    owner = next(node for node in tree.body if isinstance(node, ast.ClassDef))
    methods = [node for node in owner.body if isinstance(node, ast.AsyncFunctionDef)
               and node.name in {"_handle_message", "_handle_message_in_profile"}]
    model = ast.Module(body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0),
                            ast.ClassDef(name="HostMixin", bases=[], keywords=[], body=methods, decorator_list=[])], type_ignores=[])
    ast.fix_missing_locations(model)
    namespace = {"Platform": SimpleNamespace(FEISHU="feishu")}
    exec(compile(model, "actual-host-methods", "exec"), namespace)
    override = ContextVar("test_actual_host_override", default=None)
    observed = []
    constants = SimpleNamespace(
        get_process_hermes_home=lambda: home,
        set_hermes_home_override=override.set,
        reset_hermes_home_override=override.reset,
    )
    monkeypatch.setitem(sys.modules, "hermes_constants", constants)
    monkeypatch.setitem(sys.modules, "gateway.run", SimpleNamespace(_AGENT_PENDING_SENTINEL=object()))
    async def admit(_self, _event):
        observed.append(override.get() or home)
        return None
    runner = type("Runner", (namespace["HostMixin"],), {
        "config": SimpleNamespace(multiplex_profiles=multiplex), "_hm_admit_event": admit})()
    return runner, override, observed


@pytest.mark.parametrize("profile", ["hongrui", "hongtai", "hongxin", "xiaomage"])
@pytest.mark.parametrize("multiplex", [False, True])
def test_actual_method_boundary_covers_every_profile_and_preserves_routed_mode(tmp_path, monkeypatch, profile, multiplex):
    target = _host(tmp_path, REAL_HOST.read_text(encoding="utf-8"))
    INSTALL(tmp_path, apply=True)
    home = tmp_path / profile
    runner, override, observed = _runner(target.read_text(encoding="utf-8"), monkeypatch, home=home, multiplex=multiplex)
    foreign = tmp_path / "foreign"
    token = override.set(foreign)
    try:
        event = SimpleNamespace(source=SimpleNamespace(platform="feishu"))
        assert asyncio.run(runner._handle_message(event)) is None
        assert observed == [foreign if multiplex else home]
        assert override.get() == foreign
    finally:
        override.reset(token)


def test_actual_method_resets_context_when_admission_raises(tmp_path, monkeypatch):
    target = _host(tmp_path, REAL_HOST.read_text(encoding="utf-8"))
    INSTALL(tmp_path, apply=True)
    runner, override, observed = _runner(target.read_text(encoding="utf-8"), monkeypatch, home=tmp_path / "launch")
    async def fail(_event):
        observed.append(override.get())
        raise RuntimeError("admission failed")
    runner._hm_admit_event = fail
    token = override.set(tmp_path / "foreign")
    try:
        with pytest.raises(RuntimeError, match="admission failed"):
            asyncio.run(runner._handle_message(SimpleNamespace(source=SimpleNamespace(platform="feishu"))))
        assert observed == [tmp_path / "launch"]
        assert override.get() == tmp_path / "foreign"
    finally:
        override.reset(token)


def test_real_upstream_admission_fixture_demonstrates_why_environment_alone_is_insufficient(tmp_path, monkeypatch):
    source = REAL_HOST.read_text(encoding="utf-8")
    runner, override, observed = _runner(source, monkeypatch, home=tmp_path / "launch")
    token = override.set(tmp_path / "foreign")
    try:
        asyncio.run(runner._handle_message(SimpleNamespace(source=SimpleNamespace(platform="feishu"))))
        assert observed == [tmp_path / "foreign"]
    finally:
        override.reset(token)


def test_real_upstream_method_is_recovered_byte_for_byte_after_removal(tmp_path):
    target = _host(tmp_path, REAL_HOST.read_text(encoding="utf-8"))
    original = target.read_bytes()
    INSTALL(tmp_path, apply=True)
    INSTALL(tmp_path, apply=True, remove=True)
    assert target.read_bytes() == original


def test_non_feishu_keeps_existing_context(tmp_path, monkeypatch):
    target = _host(tmp_path)
    INSTALL(tmp_path, apply=True)
    runner, override, observed = _runner(target.read_text(encoding="utf-8"), monkeypatch, home=tmp_path / "launch")
    token = override.set(tmp_path / "foreign")
    try:
        asyncio.run(runner._handle_message(SimpleNamespace(source=SimpleNamespace(platform="slack"))))
        assert observed == [tmp_path / "foreign"]
    finally:
        override.reset(token)


def test_standard_installer_applies_and_verifies_boundary_without_profile_specific_patch():
    source = (ROOT / "deploy/install_immutable_release.sh").read_text(encoding="utf-8")
    install = source.split("_install_hermes_integration() {", 1)[1].split("\n_refresh_current_runtime_metadata()", 1)[0]
    verify = source.split("_verify_hermes_integration() {", 1)[1].split("\n_install_candidate_runtime_metadata()", 1)[0]
    assert '"$REPO_DIR/deploy/ensure_hermes_profile_boundary.py"' in install
    assert '--hermes-agent-root "$HERMES_HOME_DIR/hermes-agent" --apply || return 2' in install
    assert '"$REPO_DIR/deploy/ensure_hermes_profile_boundary.py"' in verify
    assert '--hermes-agent-root "$HERMES_HOME_DIR/hermes-agent" --check || return 2' in verify


def test_startup_of_already_applied_boundary_writes_neither_source_nor_backup(tmp_path):
    target = _host(tmp_path)
    INSTALL(tmp_path, apply=True)
    for backup in target.parent.glob("*.pre-eimemory-profile-boundary-*"):
        backup.unlink()
    before = target.read_bytes()
    stamp = target.stat().st_mtime_ns
    result = INSTALL(tmp_path, apply=True, startup=True)
    assert result["already_installed"] and result["startup"]
    assert target.read_bytes() == before and target.stat().st_mtime_ns == stamp
    assert not list(target.parent.glob("*.pre-eimemory-profile-boundary-*"))


@pytest.mark.skipif(os.name != "posix", reason="production startup uses POSIX flock")
def test_concurrent_gateway_startups_share_one_lock_and_one_patch(tmp_path):
    target = _host(tmp_path, REAL_HOST.read_text(encoding="utf-8"))
    command = [sys.executable, "-I", "-B", str(ROOT / "deploy/ensure_hermes_profile_boundary.py"),
               "--hermes-agent-root", str(tmp_path), "--apply", "--startup"]
    processes = [subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                 for _ in range(8)]
    reports = []
    for process in processes:
        out, err = process.communicate(timeout=20)
        assert process.returncode == 0, (out, err)
        reports.append(json.loads(out))
    assert sum(not report["already_installed"] for report in reports) == 1
    assert len({report["target_sha256"] for report in reports}) == 1
    assert target.read_text(encoding="utf-8").count(PATCH["INSERT"]) == 1
    assert len(list(target.parent.glob("*.pre-eimemory-profile-boundary-*"))) == 1


def test_unknown_contract_stops_gateway_before_its_entrypoint(tmp_path):
    target = _host(tmp_path, SOURCE.replace("self._hm_admit_event(event)", "self.unknown_admission(event)"))
    before = target.read_bytes()
    result = subprocess.run([sys.executable, "-I", "-B", str(ROOT / "deploy/ensure_hermes_profile_boundary.py"),
                             "--hermes-agent-root", str(tmp_path), "--apply", "--startup"],
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 2
    assert json.loads(result.stdout)["error"].startswith("hermes_profile_boundary_contract_unknown:")
    assert target.read_bytes() == before
    assert not list(target.parent.glob("*.pre-eimemory-profile-boundary-*"))


def _render_boundary_dropin(tmp_path, *, helper=None, bad_python=None):
    writer = runpy.run_path(str(ROOT / "deploy/install_managed_systemd_dropin.py"))["install_managed_dropin"]
    systemd = tmp_path / "systemd"
    systemd.mkdir(exist_ok=True)
    helper = helper or tmp_path / "controller/deploy/ensure_hermes_profile_boundary.py"
    return writer, systemd, {
        "source": ROOT / "deploy/systemd/hermes-profile-boundary.conf", "root": systemd,
        "render_hermes_boundary_python": str(bad_python or Path(sys.executable)),
        "render_hermes_boundary_helper": str(helper),
        "render_hermes_boundary_host_root": str(tmp_path / "hermes-agent"),
    }


@pytest.mark.parametrize("unit", ["hermes-gateway.service", "hermes-gateway-hongtai.service",
                                  "hermes-gateway-hongxin.service", "hermes-gateway-xiaomage.service"])
def test_each_gateway_dropin_uses_controller_helper_across_old_release_rollback(tmp_path, unit):
    controller_helper = tmp_path / "controller/deploy/ensure_hermes_profile_boundary.py"
    controller_helper.parent.mkdir(parents=True)
    shutil.copyfile(ROOT / "deploy/ensure_hermes_profile_boundary.py", controller_helper)
    old_release = tmp_path / "old-release-without-helper"
    old_release.mkdir()
    writer, systemd, kwargs = _render_boundary_dropin(tmp_path, helper=controller_helper)
    target = systemd / (unit + ".d") / "94-eimemory-hermes-profile-boundary.conf"
    writer(target=target, **kwargs)
    content = target.read_text(encoding="utf-8")
    assert content.startswith("# Managed by eimemory immutable release installer\n")
    assert str(controller_helper) in content and str(old_release) not in content
    assert "--apply --startup" in content and "@EIMEMORY_" not in content
    assert controller_helper.is_file() and not (old_release / "deploy/ensure_hermes_profile_boundary.py").exists()


@pytest.mark.parametrize("bad", ["relative/python", "has space/python", "with%specifier", "with$expansion", "with\nnewline"])
def test_boundary_dropin_rejects_unsafe_paths_before_writing(tmp_path, bad):
    python = bad if bad.startswith("relative") else str(tmp_path / bad)
    writer, systemd, kwargs = _render_boundary_dropin(tmp_path, bad_python=python)
    target = systemd / "hermes-gateway.service.d/guard.conf"
    with pytest.raises(RuntimeError, match="systemd-safe"):
        writer(target=target, **kwargs)
    assert not target.exists()


@pytest.mark.skipif(os.name != "posix" or shutil.which("bash") is None, reason="production installer is Linux bash")
def test_actual_installer_generates_shared_startup_guard_for_all_four_gateways(tmp_path):
    systemd = tmp_path / "systemd"
    systemd.mkdir()
    units = ["hermes-gateway.service", "hermes-gateway-hongtai.service",
             "hermes-gateway-hongxin.service", "hermes-gateway-xiaomage.service"]
    for unit in units:
        (systemd / unit).write_text("[Service]\nExecStart=/usr/bin/python gateway\n", encoding="utf-8")
    (systemd / "hermes-gateway-unregistered.service").write_text("[Service]\n", encoding="utf-8")
    control = tmp_path / "registered-runtime-control.py"
    control.write_text("import sys\n"
                       "assert sys.argv[1:] == ['systemctl', 'list-units']\n"
                       f"print('\\n'.join({units!r}))\n", encoding="utf-8")
    home = tmp_path / "home"
    host = home / ".hermes/hermes-agent"
    _host(host, REAL_HOST.read_text(encoding="utf-8"))
    (host / "agent").mkdir()
    shutil.copyfile(ROOT / "tests/fixtures/hermes_hosts/memory_manager_main_e0550c97.py.txt", host / "agent/memory_manager.py")
    installer = (ROOT / "deploy/install_immutable_release.sh").read_text(encoding="utf-8")
    name = "_install_hermes_integration"
    function = name + "() {" + installer.split(name + "() {", 1)[1].split("\n}", 1)[0] + "\n}\n"
    values = {"REPO_DIR": ROOT, "RELEASE_DIR": ROOT, "PYTHON_BIN": sys.executable,
              "EIMEMORY_RUNTIME_MANAGER_CONTROL": control,
              "HERMES_PYTHON": sys.executable, "HERMES_HOME_DIR": home / ".hermes",
              "SERVICE_HOME": home, "SERVICE_USER": getpass.getuser(), "USER_SYSTEMD_DIR": systemd,
              "INSTALL_ROOT": tmp_path / "install", "EVIDENCE_RECEIPT_ENV_FILE": tmp_path / "evidence.env",
              "USER_SYSTEMD_ENABLE_SERVICE": "1", "COMMIT": "a" * 40}
    setup = "\n".join(key + "=" + shlex.quote(str(value)) for key, value in values.items())
    setup += '''
export EIMEMORY_RUNTIME_MANAGER_CONTROL
_hermes_is_installed() { return 0; }
systemctl() { return 0; }
_user_systemctl() { return 0; }
_run_as_service_user() {
  local arg
  for arg in "$@"; do
    case "$arg" in */install_hermes_integration.py) return 0 ;; esac
  done
  "$@"
}
_install_as_service_user() { local mode="$1"; shift; install -m "$mode" "$@"; }
'''
    result = subprocess.run(["bash", "-s"], input="set -euo pipefail\n" + setup + function + "\n_install_hermes_integration\n",
                            text=True, capture_output=True, timeout=30)
    assert result.returncode == 0, (result.stdout, result.stderr)
    guards = list(systemd.glob("*.service.d/94-eimemory-hermes-profile-boundary.conf"))
    assert {guard.parent.name.removesuffix(".d") for guard in guards} == set(units)
    assert not (systemd / "hermes-gateway-unregistered.service.d").exists()
    assert all(str(ROOT / "deploy/ensure_hermes_profile_boundary.py") in guard.read_text() for guard in guards)
