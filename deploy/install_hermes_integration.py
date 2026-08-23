from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import stat
import tempfile
import tomllib
from typing import Any

import yaml


PLUGIN_LAYOUT = {
    "eimemory": Path("integrations/hermes/eimemory"),
    "eimemory_hook": Path("integrations/hermes/eimemory_hook"),
}


def provider_implementation_digest(release_root: str | Path) -> str:
    """Return the release-bound v2 provider implementation fingerprint."""

    from eimemory.adapters.hermes.code_implementation import implementation_digest

    return implementation_digest(Path(release_root).expanduser().resolve(strict=True))


def _plugin_version(path: Path) -> str:
    payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return str(payload.get("version") or "").strip()


def _managed_link(destination: Path, target: Path) -> str:
    destination.parent.mkdir(parents=True, exist_ok=True)
    expected = str(target)
    if destination.is_symlink() and os.readlink(destination) == expected:
        return "unchanged"
    backup_root = destination.parent.parent / ".eimemory-plugin-backups"
    backup = backup_root / f"{destination.name}.pre-managed"
    if destination.exists() and not destination.is_symlink():
        if backup.exists() or backup.is_symlink():
            raise RuntimeError(f"managed plugin backup already exists: {backup}")
        backup_root.mkdir(parents=True, exist_ok=True)
        destination.rename(backup)
    temporary = destination.parent / f".{destination.name}.next"
    if temporary.exists() or temporary.is_symlink():
        temporary.unlink()
    temporary.symlink_to(target, target_is_directory=True)
    os.replace(temporary, destination)
    return "migrated" if backup.exists() else "installed"


def _write_config(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = stat.S_IMODE(path.stat().st_mode) if path.exists() else 0o600
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            yaml.safe_dump(payload, handle, allow_unicode=True, sort_keys=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _remove_release_link(destination: Path, target: Path) -> str:
    if not destination.is_symlink():
        return "absent" if not destination.exists() else "unmanaged"
    if os.readlink(destination) != str(target):
        return "unmanaged"
    destination.unlink()
    return "removed"


def install_hermes_integration(
    *,
    release_root: str | Path,
    hermes_home: str | Path,
    current_root: str | Path = "/opt/eimemory/current",
    allow_provider_only: bool = False,
) -> dict[str, Any]:
    release = Path(release_root).expanduser().resolve(strict=True)
    home = Path(hermes_home).expanduser().resolve(strict=True)
    project = tomllib.loads((release / "pyproject.toml").read_text(encoding="utf-8"))
    expected_version = str(project["project"]["version"])
    links: dict[str, str] = {}
    hook_available = (release / PLUGIN_LAYOUT["eimemory_hook"] / "__init__.py").is_file()
    if not hook_available and not allow_provider_only:
        raise RuntimeError("Hermes hook plugin source is incomplete")
    for plugin_name, relative_source in PLUGIN_LAYOUT.items():
        source = release / relative_source
        target = Path(current_root) / relative_source
        if plugin_name == "eimemory_hook" and not hook_available:
            links[plugin_name] = _remove_release_link(
                home / "plugins" / plugin_name,
                target,
            )
            continue
        if not source.is_dir() or not (source / "__init__.py").is_file():
            raise RuntimeError(f"Hermes plugin source is incomplete: {source}")
        if _plugin_version(source / "plugin.yaml") != expected_version:
            raise RuntimeError(f"Hermes plugin version mismatch: {plugin_name}")
        links[plugin_name] = _managed_link(home / "plugins" / plugin_name, target)

    implementation_digest = ""
    if hook_available:
        implementation_digest = provider_implementation_digest(release)

    config_path = home / "config.yaml"
    config = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    if not isinstance(config, dict):
        raise RuntimeError("Hermes config root must be a mapping")
    memory = config.setdefault("memory", {})
    if not isinstance(memory, dict):
        raise RuntimeError("Hermes memory config must be a mapping")
    memory["provider"] = "eimemory"
    plugins = config.setdefault("plugins", {})
    if not isinstance(plugins, dict):
        raise RuntimeError("Hermes plugins config must be a mapping")
    enabled = [str(value) for value in plugins.get("enabled") or []]
    enabled = [value for value in enabled if value != "eimemory"]
    enabled = [value for value in enabled if value != "eimemory-hook"]
    if hook_available:
        enabled.append("eimemory-hook")
    plugins["enabled"] = enabled
    disabled = [
        str(value)
        for value in plugins.get("disabled") or []
        if str(value) not in {"eimemory", "eimemory-hook"}
    ]
    if disabled:
        plugins["disabled"] = disabled
    else:
        plugins.pop("disabled", None)
    _write_config(config_path, config)
    return {
        "ok": True,
        "version": expected_version,
        "memory_provider": memory["provider"],
        "hook_enabled": "eimemory-hook" in enabled,
        "links": links,
        "code_implementation": {
            "capability_id": "code.implementation",
            "revision_id": "code.implementation:v2",
            "binding_id": "binding.hermes.code-implementation:v2",
            "implementation_digest": implementation_digest,
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Install the release-bound Hermes eimemory plugins.")
    parser.add_argument("--release-root", required=True)
    parser.add_argument("--hermes-home", required=True)
    parser.add_argument("--current-root", default="/opt/eimemory/current")
    parser.add_argument("--allow-provider-only", action="store_true")
    args = parser.parse_args()
    report = install_hermes_integration(
        release_root=args.release_root,
        hermes_home=args.hermes_home,
        current_root=args.current_root,
        allow_provider_only=args.allow_provider_only,
    )
    print(json.dumps(report, ensure_ascii=True, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
