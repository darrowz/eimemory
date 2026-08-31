from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from deploy.ensure_openclaw_bundled_bridge import (
    OpenClawBundledBridgeError,
    ensure_openclaw_bundled_bridge,
)
from deploy.verify_openclaw_plugin_runtime import (
    OpenClawRuntimeError,
    REQUIRED_HOOKS,
    verify_openclaw_plugin_runtime,
)


def _release(tmp_path: Path, commit: str = "a" * 40) -> Path:
    bridge = tmp_path / "eimemory" / "releases" / commit / "integrations" / "openclaw" / "eimemory-bridge"
    bridge.mkdir(parents=True)
    (bridge / "index.js").write_text("module.exports = {};\n", encoding="utf-8")
    (bridge / "package.json").write_text(json.dumps({
        "name": "openclaw-eimemory-bridge", "openclaw": {"extensions": ["./index.js"]},
    }), encoding="utf-8")
    (bridge / "openclaw.plugin.json").write_text(json.dumps({"id": "eimemory-bridge"}), encoding="utf-8")
    return bridge


def _host(tmp_path: Path) -> tuple[Path, Path, Path]:
    package = tmp_path / "openclaw"
    package.mkdir()
    binary = package / "openclaw.mjs"
    binary.write_text("export {};\n", encoding="utf-8")
    (package / "package.json").write_text('{"name":"openclaw","version":"2026.8.1"}', encoding="utf-8")
    config = tmp_path / "openclaw.json"
    config.write_text(json.dumps({"plugins": {"allow": ["other"], "entries": {
        "other": {"enabled": False}, "eimemory-bridge": {"hooks": {"allowPromptInjection": True}},
    }, "load": {"paths": ["/opt/other"]}}, "untouched": {"value": 7}}), encoding="utf-8")
    return binary, config, package / "dist" / "extensions" / "eimemory-bridge"


def _link(path: Path, target: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        path.symlink_to(target, target_is_directory=True)
    except OSError:
        pytest.skip("directory symlinks unavailable")


def test_external_install_preserves_other_policy_and_never_creates_bundled_files(tmp_path: Path) -> None:
    binary, config, bundled = _host(tmp_path)
    bridge = _release(tmp_path)
    result = ensure_openclaw_bundled_bridge(binary=binary, bridge_dir=bridge, config_path=config)
    payload = json.loads(config.read_text(encoding="utf-8"))
    assert result["origin"] == "config"
    assert result["changed"] is True
    assert not bundled.parent.exists()
    assert payload["plugins"]["load"]["paths"] == ["/opt/other", str(bridge)]
    assert payload["plugins"]["allow"] == ["other", "eimemory-bridge"]
    assert payload["plugins"]["entries"]["other"] == {"enabled": False}
    assert payload["plugins"]["entries"]["eimemory-bridge"] == {
        "enabled": True, "hooks": {"allowPromptInjection": True},
    }
    assert payload["untouched"] == {"value": 7}
    assert ensure_openclaw_bundled_bridge(binary=binary, bridge_dir=bridge, config_path=config)["changed"] is False


def test_external_install_removes_only_owned_immutable_bundled_link_and_supports_rollback(tmp_path: Path) -> None:
    binary, config, bundled = _host(tmp_path)
    old, new = _release(tmp_path), _release(tmp_path, "b" * 40)
    _link(bundled, old)
    # This is the production OpenClaw containment rule that rejected our old installation.
    assert not Path(os.path.realpath(bundled)).is_relative_to(bundled.parent.resolve())
    result = ensure_openclaw_bundled_bridge(binary=binary, bridge_dir=new, config_path=config)
    assert result["removed_bundled_link"] is True
    assert not bundled.exists() and not bundled.is_symlink()
    assert old.is_dir()
    ensure_openclaw_bundled_bridge(binary=binary, bridge_dir=old, config_path=config)
    assert json.loads(config.read_text())["plugins"]["load"]["paths"] == ["/opt/other", str(old)]
    assert not bundled.is_symlink()


def test_external_preflight_is_read_only_even_when_migration_is_needed(tmp_path: Path) -> None:
    binary, config, bundled = _host(tmp_path)
    bridge = _release(tmp_path)
    _link(bundled, bridge)
    before = config.read_bytes(), config.stat().st_mtime_ns, os.readlink(bundled)
    result = ensure_openclaw_bundled_bridge(binary=binary, bridge_dir=bridge, config_path=config, preflight=True)
    assert result["preflight"] is True and result["changed"] is False
    assert result["would_change"] is True
    assert before == (config.read_bytes(), config.stat().st_mtime_ns, os.readlink(bundled))
    assert not config.with_name(".openclaw.json.lock").exists()


@pytest.mark.parametrize("kind", ["foreign_link", "directory", "file"])
def test_external_install_refuses_unowned_bundled_target_without_mutation(tmp_path: Path, kind: str) -> None:
    binary, config, bundled = _host(tmp_path)
    bridge = _release(tmp_path)
    if kind == "foreign_link":
        other = tmp_path / "foreign"
        other.mkdir()
        _link(bundled, other)
    else:
        bundled.parent.mkdir(parents=True)
        if kind == "directory":
            bundled.mkdir()
        else:
            bundled.write_text("untouched")
    before = config.read_bytes()
    with pytest.raises(OpenClawBundledBridgeError):
        ensure_openclaw_bundled_bridge(binary=binary, bridge_dir=bridge, config_path=config)
    assert config.read_bytes() == before
    assert bundled.exists()


@pytest.mark.parametrize("plugins", [[], {"load": []}, {"load": {"paths": "bad"}},
                                    {"load": {"paths": [7]}}, {"allow": "bad"},
                                    {"entries": []}, {"entries": {"eimemory-bridge": []}}])
def test_external_install_rejects_malformed_policy_before_removing_owned_link(tmp_path: Path, plugins: object) -> None:
    binary, config, bundled = _host(tmp_path)
    bridge = _release(tmp_path)
    _link(bundled, bridge)
    config.write_text(json.dumps({"plugins": plugins}), encoding="utf-8")
    before = config.read_bytes()
    with pytest.raises(OpenClawBundledBridgeError):
        ensure_openclaw_bundled_bridge(binary=binary, bridge_dir=bridge, config_path=config)
    assert config.read_bytes() == before and bundled.is_symlink()


def test_external_install_rejects_foreign_eimemory_load_path(tmp_path: Path) -> None:
    binary, config, _ = _host(tmp_path)
    bridge = _release(tmp_path)
    config.write_text(json.dumps({"plugins": {"load": {"paths": [
        "/foreign/releases/" + "c" * 40 + "/integrations/openclaw/eimemory-bridge",
    ]}}}), encoding="utf-8")
    before = config.read_bytes()
    with pytest.raises(OpenClawBundledBridgeError, match="unowned"):
        ensure_openclaw_bundled_bridge(binary=binary, bridge_dir=bridge, config_path=config)
    assert config.read_bytes() == before


def _inspection(bridge: Path) -> dict:
    return {"plugin": {"id": "eimemory-bridge", "origin": "config", "rootDir": str(bridge),
                       "source": str(bridge / "index.js"), "enabled": True, "activated": True,
                       "status": "loaded", "toolNames": ["eimemory_bridge_status"],
                       "contracts": {"tools": ["eimemory_bridge_status"]}},
            "typedHooks": [{"name": name} for name in REQUIRED_HOOKS], "diagnostics": [], "compatibility": []}


def test_external_verifier_requires_config_origin_and_exact_release_source(tmp_path: Path) -> None:
    bridge = _release(tmp_path)
    assert verify_openclaw_plugin_runtime(_inspection(bridge), expected_root=bridge)["ok"] is True


@pytest.mark.parametrize("field,value", [("origin", "bundled"), ("origin", "global"), ("origin", None),
                                          ("source", None), ("source", "/foreign/index.js"),
                                          ("activated", False), ("enabled", False), ("status", "error")])
def test_external_verifier_fails_closed_on_wrong_authority_or_runtime(tmp_path: Path, field: str, value: object) -> None:
    bridge = _release(tmp_path)
    payload = _inspection(bridge)
    payload["plugin"][field] = value
    with pytest.raises(OpenClawRuntimeError):
        verify_openclaw_plugin_runtime(payload, expected_root=bridge)
    with pytest.raises(OpenClawRuntimeError):
        verify_openclaw_plugin_runtime(payload, expected_root=bridge, allow_legacy_runtime=True)
