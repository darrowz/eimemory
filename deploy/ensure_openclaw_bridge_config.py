#!/usr/bin/env python3
"""Atomically enforce the OpenClaw bridge's required host-side policy."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import tempfile


PLUGIN_ID = "eimemory-bridge"
MAX_CONFIG_BYTES = 4 * 1024 * 1024


class OpenClawBridgeConfigError(RuntimeError):
    """Raised when the OpenClaw configuration cannot be changed safely."""


def _object(value: object, field: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise OpenClawBridgeConfigError(f"{field} must be an object")
    return value


def _fsync_directory(path: Path) -> None:
    if os.name != "posix":
        return
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _write_atomic(path: Path, payload: dict[str, object], *, mode: int) -> None:
    descriptor, temporary_name = tempfile.mkstemp(prefix=".openclaw-config-", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, mode)
        if os.name == "posix":
            stat = path.stat(follow_symlinks=False)
            os.chown(temporary, stat.st_uid, stat.st_gid)
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def ensure_openclaw_bridge_config(path: str | Path) -> dict[str, object]:
    target = Path(path)
    if target.is_symlink():
        raise OpenClawBridgeConfigError("OpenClaw configuration must not be a symlink")
    if not target.is_file():
        raise OpenClawBridgeConfigError("OpenClaw configuration file is missing")
    try:
        if target.stat(follow_symlinks=False).st_size > MAX_CONFIG_BYTES:
            raise OpenClawBridgeConfigError("OpenClaw configuration is unexpectedly large")
        payload = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise OpenClawBridgeConfigError("OpenClaw configuration is unreadable or invalid") from exc
    config = _object(payload, "OpenClaw configuration")

    plugins_value = config.setdefault("plugins", {})
    plugins = _object(plugins_value, "plugins")
    allow = plugins.get("allow")
    if allow is not None:
        if not isinstance(allow, list) or any(not isinstance(item, str) for item in allow):
            raise OpenClawBridgeConfigError("plugins.allow must be an array of plugin IDs")
    entries = _object(plugins.setdefault("entries", {}), "plugins.entries")
    bridge = _object(entries.setdefault(PLUGIN_ID, {}), f"plugins.entries.{PLUGIN_ID}")
    hooks = _object(bridge.setdefault("hooks", {}), f"plugins.entries.{PLUGIN_ID}.hooks")
    before = json.dumps(config, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    if allow is not None:
        if PLUGIN_ID not in allow:
            allow.append(PLUGIN_ID)
    bridge["enabled"] = True
    hooks["allowConversationAccess"] = True
    after = json.dumps(config, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    changed = before != after
    if changed:
        mode = target.stat(follow_symlinks=False).st_mode & 0o777
        _write_atomic(target, config, mode=mode)
    return {"ok": True, "changed": changed, "path": str(target), "plugin_id": PLUGIN_ID}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--path", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        report = ensure_openclaw_bridge_config(args.path)
    except (OpenClawBridgeConfigError, OSError) as exc:
        parser.exit(2, f"OpenClaw bridge configuration failed: {exc}\n")
    print(f"openclaw_bridge_config={report['path']}")
    print(f"openclaw_bridge_config_changed={int(bool(report['changed']))}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
