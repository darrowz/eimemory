#!/usr/bin/env python3
"""Materialize the eimemory bridge as a bundled OpenClaw plugin.

OpenClaw only grants in-process gateway requests (api.runtime.gateway.request)
to plugins whose registry record originates from its own bundled extension
directory (origin="bundled"). The bridge ships inside the immutable release
tree, so this helper creates a stable symlink under the installed OpenClaw
package's ``dist/extensions`` directory pointing at the candidate release's
bridge directory, and removes the legacy config-origin ``plugins.load.paths``
entry so discovery cannot race between two origins for the same plugin id.
"""

from __future__ import annotations

import argparse
import json
import os
import stat
import tempfile
from pathlib import Path


PLUGIN_ID = "eimemory-bridge"
BRIDGE_RELATIVE_SUFFIX = ("integrations", "openclaw", PLUGIN_ID)
REQUIRED_TARGET_FILES = ("index.js", "openclaw.plugin.json")
MAX_CONFIG_BYTES = 4 * 1024 * 1024


class OpenClawBundledBridgeError(RuntimeError):
    """Raised when the bundled bridge link cannot be established safely."""


def _fsync_directory(path: Path) -> None:
    if os.name != "posix":
        return
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _is_bridge_load_path(entry: object) -> bool:
    if not isinstance(entry, str) or not entry.strip():
        return False
    expanded = os.path.expanduser(entry.strip())
    parts = [part for part in os.path.normpath(expanded).split("/") if part not in ("", ".")]
    return len(parts) >= 3 and tuple(parts[-3:]) == BRIDGE_RELATIVE_SUFFIX


def _resolve_openclaw_package_root(binary: Path) -> Path:
    real_binary = Path(os.path.realpath(binary))
    if not real_binary.is_file():
        raise OpenClawBundledBridgeError(f"OpenClaw binary is missing: {binary}")
    package_root = real_binary.parent
    manifest = package_root / "package.json"
    try:
        payload = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise OpenClawBundledBridgeError(f"OpenClaw package manifest unreadable: {manifest}") from exc
    if not isinstance(payload, dict) or payload.get("name") != "openclaw":
        raise OpenClawBundledBridgeError(
            f"refusing to manage extensions outside an OpenClaw package root: {package_root}"
        )
    return package_root


def _validate_bridge_target(bridge_dir: Path) -> None:
    resolved = Path(os.path.realpath(bridge_dir))
    if not resolved.is_dir():
        raise OpenClawBundledBridgeError(f"bridge directory is missing: {bridge_dir}")
    for required in REQUIRED_TARGET_FILES:
        if not (resolved / required).is_file():
            raise OpenClawBundledBridgeError(
                f"bridge directory is incomplete ({required} missing): {bridge_dir}"
            )


def _atomic_symlink(extensions_dir: Path, link_path: Path, target: str) -> None:
    _fsync_directory(extensions_dir)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{PLUGIN_ID}-link-", dir=extensions_dir)
    os.close(descriptor)
    temporary = Path(temporary_name)
    temporary.unlink(missing_ok=True)
    try:
        os.symlink(target, temporary)
        # Replaces an existing same-name symlink atomically; a real directory
        # in the way makes rename fail loudly instead of clobbering content.
        os.replace(temporary, link_path)
        _fsync_directory(extensions_dir)
    finally:
        temporary.unlink(missing_ok=True)


def _ensure_bundled_link(extensions_dir: Path, target: Path) -> bool:
    link_path = extensions_dir / PLUGIN_ID
    expected = str(Path(os.path.abspath(target)))
    if link_path.is_symlink():
        try:
            current = os.readlink(link_path)
        except OSError as exc:  # pragma: no cover - platform dependent
            raise OpenClawBundledBridgeError(f"cannot read existing link: {link_path}") from exc
        if current == expected:
            return False
    elif link_path.exists():
        raise OpenClawBundledBridgeError(
            f"refusing to replace non-symlink plugin path: {link_path}"
        )
    _atomic_symlink(extensions_dir, link_path, expected)
    return True


def _strip_bridge_load_paths(config_path: Path) -> int:
    if not config_path.is_file():
        return 0
    metadata = config_path.stat()
    if metadata.st_size > MAX_CONFIG_BYTES:
        raise OpenClawBundledBridgeError("OpenClaw configuration is unexpectedly large")
    try:
        payload = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise OpenClawBundledBridgeError("OpenClaw configuration is unreadable or invalid") from exc
    if not isinstance(payload, dict):
        raise OpenClawBundledBridgeError("OpenClaw configuration must be an object")
    plugins_value = payload.get("plugins")
    if not isinstance(plugins_value, dict):
        return 0
    load_value = plugins_value.get("load")
    if not isinstance(load_value, dict):
        return 0
    paths_value = load_value.get("paths")
    if not isinstance(paths_value, list):
        return 0
    kept = [entry for entry in paths_value if not _is_bridge_load_path(entry)]
    removed = len(paths_value) - len(kept)
    if removed == 0:
        return 0
    load_value["paths"] = kept
    _fsync_directory(config_path.parent)
    descriptor, temporary_name = tempfile.mkstemp(prefix=".openclaw-config-", dir=config_path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, stat.S_IMODE(metadata.st_mode))
        if os.name == "posix":
            os.chown(temporary, metadata.st_uid, metadata.st_gid)
        current = config_path.stat(follow_symlinks=False)
        if (current.st_dev, current.st_ino) != (metadata.st_dev, metadata.st_ino):
            raise OpenClawBundledBridgeError("OpenClaw configuration changed during update")
        os.replace(temporary, config_path)
        _fsync_directory(config_path.parent)
    finally:
        temporary.unlink(missing_ok=True)
    return removed


def ensure_openclaw_bundled_bridge(
    *,
    binary: Path,
    bridge_dir: Path,
    config_path: Path,
) -> dict[str, object]:
    _validate_bridge_target(bridge_dir)
    package_root = _resolve_openclaw_package_root(binary)
    extensions_dir = package_root / "dist" / "extensions"
    if extensions_dir.exists() and not extensions_dir.is_dir():
        raise OpenClawBundledBridgeError(f"extensions path is not a directory: {extensions_dir}")
    extensions_dir.mkdir(parents=True, exist_ok=True)
    link_changed = _ensure_bundled_link(extensions_dir, bridge_dir)
    removed_paths = _strip_bridge_load_paths(config_path)
    return {
        "ok": True,
        "changed": link_changed or removed_paths > 0,
        "link": str(extensions_dir / PLUGIN_ID),
        "target": str(Path(os.path.abspath(bridge_dir))),
        "link_changed": link_changed,
        "removed_config_paths": removed_paths,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--bin", required=True, type=Path)
    parser.add_argument("--bridge-dir", required=True, type=Path)
    parser.add_argument("--config", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        report = ensure_openclaw_bundled_bridge(
            binary=args.bin,
            bridge_dir=args.bridge_dir,
            config_path=args.config,
        )
    except (OpenClawBundledBridgeError, OSError) as exc:
        parser.exit(2, f"OpenClaw bundled bridge setup failed: {exc}\n")
    print(f"openclaw_bundled_bridge={report['link']}")
    print(f"openclaw_bundled_bridge_changed={int(bool(report['changed']))}")
    print(f"openclaw_bundled_bridge_removed_config_paths={report['removed_config_paths']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
