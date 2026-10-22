from __future__ import annotations

import argparse
import importlib.util
import json
import os
from pathlib import Path
import re
import stat
import tempfile
import tomllib
from typing import Any


PLUGIN_LAYOUT = {
    "eimemory": Path("integrations/hermes/eimemory"),
    "eimemory_hook": Path("integrations/hermes/eimemory_hook"),
}

# Match Hermes' bounded named-profile discovery without executing host code.
# These identity markers also cover profiles that inherit their configuration.
_PROFILE_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_PROFILE_IDENTITY_MARKERS = ("config.yaml", ".env", "SOUL.md", "profile.yaml", "auth.json", "state.db")


def _load_release_provider(release: Path):
    provider_path = (release / "eimemory/adapters/hermes/code_implementation.py").resolve(strict=True)
    if release not in provider_path.parents:
        raise RuntimeError("provider implementation escaped release root")
    spec = importlib.util.spec_from_file_location(
        "_eimemory_release_code_implementation",
        provider_path,
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("unable to load release-bound provider implementation")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def provider_implementation_digest(release_root: str | Path) -> str:
    """Return the release-bound provider implementation fingerprint."""

    release = Path(release_root).expanduser().resolve(strict=True)
    module = _load_release_provider(release)
    digest_function = getattr(module, "implementation_digest", None)
    if not callable(digest_function):
        raise RuntimeError("release-bound provider digest function is unavailable")
    return str(digest_function(release))


def provider_identity(release_root: str | Path) -> dict[str, str]:
    """Identity of the release tree: stable revision, digest-derived binding."""

    release = Path(release_root).expanduser().resolve(strict=True)
    module = _load_release_provider(release)
    digest = provider_implementation_digest(release)
    bind = getattr(module, "binding_id_for_implementation", None)
    binding_id = str(bind(digest)) if callable(bind) else ""
    return {
        "capability_id": str(getattr(module, "CAPABILITY_ID", "code.implementation")),
        "revision_id": str(getattr(module, "REVISION_ID", "")),
        "binding_id": binding_id,
        "implementation_digest": digest,
    }


def _plugin_version(path: Path) -> str:
    import yaml

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
    import yaml

    path.parent.mkdir(parents=True, exist_ok=True)
    mode = stat.S_IMODE(path.stat().st_mode) if path.exists() else 0o600
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        try:
            os.fchmod(descriptor, mode)
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n", closefd=False) as handle:
                yaml.safe_dump(payload, handle, allow_unicode=True, sort_keys=False)
                handle.flush()
                os.fsync(handle.fileno())
        finally:
            os.close(descriptor)
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


def prepare_handoff_directories(home: Path) -> dict[str, Any]:
    """Prepare owned profile directories for the private durable outbox.

    Older Hermes profiles can inherit group-write from their creation umask.
    The runtime correctly refuses such paths; fix only the installation's own
    directories, never an outside ancestor, managed child symlink, foreign owner
    or payload. The explicitly supplied home keeps its existing canonical-path
    semantics, as does the runtime outbox.
    Validate every existing target before changing any mode.
    """
    if os.name != "posix":
        return {"status": "not_required", "profile_count": 0, "adjusted_count": 0,
                "created_count": 0}
    home = home.resolve(strict=True)
    owner = os.geteuid()
    # PrivateUsers leaves host root unmapped. Reuse the bounded UID-map
    # validation for system ancestors; profile directories still require the
    # exact runtime owner below.
    system_owner = lambda info, path: info.st_uid == 0
    if owner > 0:
        source = Path(__file__).resolve().parents[1] / "eimemory/contracts/user_control_plane.py"
        specification = importlib.util.spec_from_file_location("_handoff_user_control", source)
        control = importlib.util.module_from_spec(specification)
        specification.loader.exec_module(control)
        system_owner = lambda info, path: control._system_owner(info, path, owner)
    for ancestor in home.parents:
        info = ancestor.lstat()
        if (not stat.S_ISDIR(info.st_mode) or
                (info.st_uid != owner and not system_owner(info, ancestor))
                or (info.st_mode & 0o022 and not info.st_mode & stat.S_ISVTX)):
            raise RuntimeError(f"Hermes handoff ancestor is untrusted: {ancestor}")
    directories = [(home, False), (home / "state", True)]
    profiles = home / "profiles"
    profile_count = 0
    if os.path.lexists(profiles):
        info = profiles.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != owner:
            raise RuntimeError(f"Hermes handoff directory is untrusted: {profiles}")
        directories.append((profiles, False))
        for profile in sorted(profiles.iterdir()):
            if (profile.name == "default" or _PROFILE_ID.fullmatch(profile.name) is None
                    or (profiles / ".deleted" / profile.name).exists()):
                continue
            info = profile.lstat()
            if stat.S_ISLNK(info.st_mode):
                raise RuntimeError(f"Hermes handoff directory is untrusted: {profile}")
            if not stat.S_ISDIR(info.st_mode):
                continue
            markers = []
            for name in _PROFILE_IDENTITY_MARKERS:
                marker = profile / name
                if os.path.lexists(marker):
                    markers.append(marker.lstat())
            if not any(stat.S_ISREG(marker.st_mode) or stat.S_ISLNK(marker.st_mode)
                       for marker in markers):
                continue
            directories.extend(((profile, False), (profile / "state", True)))
            profile_count += 1
    modes = []
    for directory, private in directories:
        if not os.path.lexists(directory):
            if not private:
                raise RuntimeError(f"Hermes handoff directory is missing: {directory}")
            modes.append((directory, None, 0o700))
            continue
        info = directory.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != owner:
            raise RuntimeError(f"Hermes handoff directory is untrusted: {directory}")
        modes.append((directory, info, 0o700 if private else stat.S_IMODE(info.st_mode) & ~0o022))
    adjusted = created = 0
    for directory, before, mode in modes:
        if before is None:
            directory.mkdir(mode=mode)
            created += 1
            continue
        current = directory.lstat()
        if (current.st_dev, current.st_ino, current.st_uid, current.st_mode) != (
                before.st_dev, before.st_ino, before.st_uid, before.st_mode):
            raise RuntimeError(f"Hermes handoff directory changed during installation: {directory}")
        if stat.S_IMODE(current.st_mode) != mode:
            # Open without following a replacement symlink; keep the checked
            # directory inode pinned while tightening its permissions.
            descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                opened = os.fstat(descriptor)
                if (opened.st_dev, opened.st_ino, opened.st_uid, opened.st_mode) != (
                        before.st_dev, before.st_ino, before.st_uid, before.st_mode):
                    raise RuntimeError(f"Hermes handoff directory changed during installation: {directory}")
                os.fchmod(descriptor, mode)
            finally:
                os.close(descriptor)
            adjusted += 1
    return {"status": "ready", "profile_count": profile_count,
            "adjusted_count": adjusted, "created_count": created}


def install_hermes_integration(
    *,
    release_root: str | Path,
    hermes_home: str | Path,
    current_root: str | Path = "/opt/eimemory/current",
    allow_provider_only: bool = False,
) -> dict[str, Any]:
    import yaml

    release = Path(release_root).expanduser().resolve(strict=True)
    home = Path(hermes_home).expanduser().resolve(strict=True)
    current = Path(current_root).expanduser().absolute()
    project = tomllib.loads((release / "pyproject.toml").read_text(encoding="utf-8"))
    expected_version = str(project["project"]["version"])
    handoff = prepare_handoff_directories(home)
    links: dict[str, str] = {}
    hook_available = (release / PLUGIN_LAYOUT["eimemory_hook"] / "__init__.py").is_file()
    if not hook_available and not allow_provider_only:
        raise RuntimeError("Hermes hook plugin source is incomplete")
    for plugin_name, relative_source in PLUGIN_LAYOUT.items():
        source = release / relative_source
        target = current / relative_source
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

    identity = {
        "capability_id": "code.implementation",
        "revision_id": "",
        "binding_id": "",
        "implementation_digest": "",
    }
    if hook_available:
        identity = provider_identity(release)

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
        "handoff_directories": handoff,
        "links": links,
        "code_implementation": identity,
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
