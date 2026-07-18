#!/usr/bin/env python3
"""Atomically enforce the OpenClaw bridge's required host-side policy."""

from __future__ import annotations

import argparse
from contextlib import contextmanager
import json
import os
from pathlib import Path
import stat
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


@contextmanager
def _interprocess_lock(path: Path):
    flags = os.O_CREAT | os.O_RDWR
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags, 0o600)
    try:
        if os.name == "posix":
            import fcntl

            fcntl.flock(descriptor, fcntl.LOCK_EX)
        elif os.name == "nt":
            import msvcrt

            if os.fstat(descriptor).st_size == 0:
                os.write(descriptor, b"0")
            os.lseek(descriptor, 0, os.SEEK_SET)
            msvcrt.locking(descriptor, msvcrt.LK_LOCK, 1)
        yield
    finally:
        if os.name == "posix":
            import fcntl

            fcntl.flock(descriptor, fcntl.LOCK_UN)
        elif os.name == "nt":
            import msvcrt

            os.lseek(descriptor, 0, os.SEEK_SET)
            msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
        os.close(descriptor)


def _read_config(path: Path) -> tuple[dict[str, object], os.stat_result]:
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags)
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise OpenClawBridgeConfigError("OpenClaw configuration must be a regular file")
        if metadata.st_size > MAX_CONFIG_BYTES:
            raise OpenClawBridgeConfigError("OpenClaw configuration is unexpectedly large")
        with os.fdopen(descriptor, "r", encoding="utf-8", closefd=False) as handle:
            payload = json.load(handle)
    finally:
        os.close(descriptor)
    return _object(payload, "OpenClaw configuration"), metadata


def _write_atomic(
    path: Path,
    payload: dict[str, object],
    *,
    metadata: os.stat_result,
) -> None:
    # Verify directory fsync capability before the atomic replace.  A failure
    # after replace would otherwise report a failed update that already took
    # effect and could not be rolled back by this helper.
    _fsync_directory(path.parent)
    descriptor, temporary_name = tempfile.mkstemp(prefix=".openclaw-config-", dir=path.parent)
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
        current = path.stat(follow_symlinks=False)
        if (current.st_dev, current.st_ino) != (metadata.st_dev, metadata.st_ino):
            raise OpenClawBridgeConfigError("OpenClaw configuration changed during update")
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def ensure_openclaw_bridge_config(path: str | Path) -> dict[str, object]:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    lock_path = target.with_name(f".{target.name}.lock")
    try:
        with _interprocess_lock(lock_path):
            return _ensure_openclaw_bridge_config_locked(target)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise OpenClawBridgeConfigError("OpenClaw configuration is unreadable or invalid") from exc


def _ensure_openclaw_bridge_config_locked(target: Path) -> dict[str, object]:
    if target.is_symlink():
        raise OpenClawBridgeConfigError("OpenClaw configuration must not be a symlink")
    if not target.is_file():
        raise OpenClawBridgeConfigError("OpenClaw configuration file is missing")
    config, metadata = _read_config(target)
    before = json.dumps(config, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    plugins_value = config.setdefault("plugins", {})
    plugins = _object(plugins_value, "plugins")
    allow = plugins.setdefault("allow", [])
    if not isinstance(allow, list) or any(not isinstance(item, str) for item in allow):
        raise OpenClawBridgeConfigError("plugins.allow must be an array of plugin IDs")
    entries = _object(plugins.setdefault("entries", {}), "plugins.entries")
    bridge = _object(entries.setdefault(PLUGIN_ID, {}), f"plugins.entries.{PLUGIN_ID}")
    legacy_config = bridge.get("config")
    if legacy_config is not None:
        plugin_config = _object(legacy_config, f"plugins.entries.{PLUGIN_ID}.config")
        plugin_config.pop("enabled", None)
        if plugin_config:
            unsupported = ",".join(sorted(str(key) for key in plugin_config))
            raise OpenClawBridgeConfigError(
                f"plugins.entries.{PLUGIN_ID}.config contains unsupported properties: {unsupported}"
            )
    hooks = _object(bridge.setdefault("hooks", {}), f"plugins.entries.{PLUGIN_ID}.hooks")
    if PLUGIN_ID not in allow:
        allow.append(PLUGIN_ID)
    bridge["enabled"] = True
    hooks["allowConversationAccess"] = True
    after = json.dumps(config, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    changed = before != after
    if changed:
        _write_atomic(target, config, metadata=metadata)
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
