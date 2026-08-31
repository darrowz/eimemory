#!/usr/bin/env python3
"""Install the bridge as an explicit external OpenClaw plugin.

The filename is retained for old rollback callers. No bundled plugin is
created, and OpenClaw's containment and authorization checks are untouched.
Only a proven eimemory immutable-release legacy symlink may be removed.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import json
import os
from pathlib import Path
import re
import stat
import tempfile
from threading import RLock

PLUGIN_ID = "eimemory-bridge"
BRIDGE_RELATIVE_SUFFIX = ("integrations", "openclaw", PLUGIN_ID)
MAX_CONFIG_BYTES = 4 * 1024 * 1024
_COMMIT = re.compile(r"[0-9a-f]{40}")
_LOCAL_CONFIG_LOCK = RLock()


class OpenClawBundledBridgeError(RuntimeError):
    """Historical public exception for fail-closed external installation."""


def _fsync_directory(path: Path) -> None:
    if os.name == "posix":
        descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def _no_symlink_components(path: Path) -> None:
    for item in (*reversed(path.parents), path):
        if item.is_symlink():
            raise OpenClawBundledBridgeError(f"managed path traverses a symlink: {item}")


def _object(value: object, field: str) -> dict:
    if not isinstance(value, dict):
        raise OpenClawBundledBridgeError(f"{field} must be an object")
    return value


def _resolve_openclaw_package_root(binary: Path) -> Path:
    real_binary = binary.resolve(strict=True)
    if not real_binary.is_file():
        raise OpenClawBundledBridgeError(f"OpenClaw binary is missing: {binary}")
    package_root = real_binary.parent
    manifest = _object(json.loads((package_root / "package.json").read_text(encoding="utf-8")), "OpenClaw package")
    if manifest.get("name") != "openclaw":
        raise OpenClawBundledBridgeError(f"refusing to manage a non-OpenClaw package root: {package_root}")
    return package_root


def _owned_release_bridge(path: Path, install_root: Path) -> bool:
    return bool(path.is_absolute() and len(path.parts) >= 6
                and tuple(path.parts[-3:]) == BRIDGE_RELATIVE_SUFFIX
                and _COMMIT.fullmatch(path.parents[2].name)
                and path.parents[3].name == "releases" and path.parents[4] == install_root)


def _validate_bridge_target(bridge: Path) -> Path:
    if not bridge.is_absolute() or len(bridge.parents) < 5:
        raise OpenClawBundledBridgeError("bridge must use an absolute immutable release path")
    install_root = bridge.parents[4]
    if not _owned_release_bridge(bridge, install_root):
        raise OpenClawBundledBridgeError("bridge must use an exact immutable release commit")
    _no_symlink_components(bridge)
    for name in ("index.js", "openclaw.plugin.json", "package.json"):
        path = bridge / name
        if path.is_symlink() or not path.is_file():
            raise OpenClawBundledBridgeError(f"bridge source is missing or not regular: {name}")
    manifest = _object(json.loads((bridge / "openclaw.plugin.json").read_text(encoding="utf-8")), "bridge manifest")
    package = _object(json.loads((bridge / "package.json").read_text(encoding="utf-8")), "bridge package")
    if manifest.get("id") != PLUGIN_ID or package.get("name") != "openclaw-eimemory-bridge":
        raise OpenClawBundledBridgeError("bridge package identity mismatch")
    if _object(package.get("openclaw"), "bridge package.openclaw").get("extensions") != ["./index.js"]:
        raise OpenClawBundledBridgeError("bridge package must declare the exact index.js extension")
    return install_root


def _legacy_bundled_link(package_root: Path, install_root: Path) -> tuple[Path, tuple | None]:
    link = package_root / "dist" / "extensions" / PLUGIN_ID
    _no_symlink_components(link.parent)
    if not link.exists() and not link.is_symlink():
        return link, None
    if not link.is_symlink():
        raise OpenClawBundledBridgeError(f"refusing to replace non-symlink plugin path: {link}")
    metadata = link.lstat()
    target_text = os.readlink(link)
    target = Path(target_text)
    if not _owned_release_bridge(target, install_root):
        raise OpenClawBundledBridgeError("refusing to remove an unowned bundled plugin symlink")
    _validate_bridge_target(target)
    return link, (metadata.st_dev, metadata.st_ino, target_text)


def _read_config(path: Path) -> tuple[dict, os.stat_result, bytes]:
    _no_symlink_components(path)
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > MAX_CONFIG_BYTES:
            raise OpenClawBundledBridgeError("OpenClaw configuration must be a bounded regular file")
        with os.fdopen(descriptor, "rb", closefd=False) as handle:
            raw = handle.read(MAX_CONFIG_BYTES + 1)
        if len(raw) > MAX_CONFIG_BYTES:
            raise OpenClawBundledBridgeError("OpenClaw configuration is unexpectedly large")
    finally:
        os.close(descriptor)
    return _object(json.loads(raw.decode("utf-8")), "OpenClaw configuration"), metadata, raw


def _external_config(payload: dict, bridge: Path, install_root: Path) -> dict:
    plugins = _object(payload.setdefault("plugins", {}), "plugins")
    load = _object(plugins.setdefault("load", {}), "plugins.load")
    paths = load.setdefault("paths", [])
    if not isinstance(paths, list) or any(not isinstance(item, str) or not item.strip() for item in paths):
        raise OpenClawBundledBridgeError("plugins.load.paths must be a string array")
    kept = []
    current_bridge = install_root / "current" / Path(*BRIDGE_RELATIVE_SUFFIX)
    for raw in paths:
        path = Path(os.path.expanduser(raw))
        if _owned_release_bridge(path, install_root) or path == current_bridge:
            continue
        if tuple(path.parts[-3:]) == BRIDGE_RELATIVE_SUFFIX:
            raise OpenClawBundledBridgeError("unowned eimemory external load path would create ambiguous authority")
        kept.append(raw)
    load["paths"] = [*kept, str(bridge)]
    allow = plugins.setdefault("allow", [])
    if not isinstance(allow, list) or any(not isinstance(item, str) for item in allow):
        raise OpenClawBundledBridgeError("plugins.allow must be a string array")
    if PLUGIN_ID not in allow:
        allow.append(PLUGIN_ID)
    entries = _object(plugins.setdefault("entries", {}), "plugins.entries")
    _object(entries.setdefault(PLUGIN_ID, {}), "plugins.entries.eimemory-bridge")["enabled"] = True
    return payload


@contextmanager
def _config_lock(path: Path):
    with _LOCAL_CONFIG_LOCK:
        descriptor = os.open(path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
        locked = False
        try:
            if os.name == "posix":
                import fcntl
                fcntl.flock(descriptor, fcntl.LOCK_EX)
                locked = True
            elif os.name == "nt":
                import msvcrt
                if os.fstat(descriptor).st_size == 0:
                    os.write(descriptor, b"0")
                os.lseek(descriptor, 0, os.SEEK_SET)
                msvcrt.locking(descriptor, msvcrt.LK_LOCK, 1)
                locked = True
            yield
        finally:
            if locked and os.name == "posix":
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            elif locked and os.name == "nt":
                os.lseek(descriptor, 0, os.SEEK_SET)
                msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
            os.close(descriptor)


def _write_config(path: Path, payload: dict, metadata: os.stat_result, before: bytes) -> None:
    _fsync_directory(path.parent)
    descriptor, name = tempfile.mkstemp(prefix=".openclaw-config-", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, stat.S_IMODE(metadata.st_mode))
        if os.name == "posix":
            os.chown(temporary, metadata.st_uid, metadata.st_gid)
        _, current, raw = _read_config(path)
        if (current.st_dev, current.st_ino) != (metadata.st_dev, metadata.st_ino) or raw != before:
            raise OpenClawBundledBridgeError("OpenClaw configuration changed during update")
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def _install(binary: Path, bridge: Path, config_path: Path, *, preflight: bool) -> dict[str, object]:
    install_root = _validate_bridge_target(bridge)
    package_root = _resolve_openclaw_package_root(binary)
    link, legacy_identity = _legacy_bundled_link(package_root, install_root)
    payload, metadata, before = _read_config(config_path)
    projected = _external_config(payload, bridge, install_root)
    changed_config = json.loads(before) != projected
    if not preflight:
        if changed_config:
            _write_config(config_path, projected, metadata, before)
        if legacy_identity is not None:
            # Never follow or recursively remove the target; recheck the one
            # owned directory entry immediately before unlinking it.
            _, checked_identity = _legacy_bundled_link(package_root, install_root)
            if checked_identity != legacy_identity:
                raise OpenClawBundledBridgeError("bundled plugin symlink changed during migration")
            link.unlink()
            _fsync_directory(link.parent)
    return {"ok": True, "origin": "config", "target": str(bridge), "preflight": preflight,
            "changed": not preflight and (changed_config or legacy_identity is not None),
            "would_change": changed_config or legacy_identity is not None,
            "removed_bundled_link": not preflight and legacy_identity is not None}


def ensure_openclaw_bundled_bridge(
    *, binary: Path, bridge_dir: Path, config_path: Path, preflight: bool = False,
) -> dict[str, object]:
    """Compatibility entrypoint: configure an external plugin, never a bundle."""
    binary, bridge, config = Path(binary), Path(bridge_dir), Path(config_path)
    try:
        if preflight:
            return _install(binary, bridge, config, preflight=True)
        _no_symlink_components(config)
        with _config_lock(config.with_name(f".{config.name}.lock")):
            return _install(binary, bridge, config, preflight=False)
    except (OSError, UnicodeError, ValueError) as exc:
        raise OpenClawBundledBridgeError(f"external plugin installation failed: {exc}") from exc


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--bin", required=True, type=Path)
    parser.add_argument("--bridge-dir", required=True, type=Path)
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--preflight", action="store_true", help="validate migration without changing any files")
    args = parser.parse_args(argv)
    try:
        report = ensure_openclaw_bundled_bridge(binary=args.bin, bridge_dir=args.bridge_dir,
                                              config_path=args.config, preflight=args.preflight)
    except OpenClawBundledBridgeError as exc:
        parser.exit(2, f"OpenClaw external bridge setup failed: {exc}\n")
    print(f"openclaw_external_bridge={report['target']} origin=config")
    print(f"openclaw_external_bridge_changed={int(bool(report['changed']))}")
    print(f"openclaw_external_bridge_preflight={int(bool(report['preflight']))}")
    print(f"openclaw_legacy_bundled_link_removed={int(bool(report['removed_bundled_link']))}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
