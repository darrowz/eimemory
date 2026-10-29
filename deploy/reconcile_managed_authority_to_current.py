#!/usr/bin/env python3
"""Keep managed-authority registry+manifest bound to /opt/eimemory/current.

Immutable release installs can advance ``current`` while leaving
``runtime-manager.v1.json`` / managed-code-incident-manifest pinned to a
pruned prior release. Checkpoint then fails with opaque
``manager_operation_failed`` (underlying FileNotFoundError on the old
release directory).

This helper is idempotent: when already aligned it exits 0 without rewrite.
It never forges L5 certificates. Intended for user control-plane hosts after
a successful immutable install.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path


def _atomic_write(path: Path, raw: bytes, mode: int = 0o640) -> None:
    tmp = path.with_name(f"{path.name}.tmp.{os.getpid()}")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode)
    try:
        os.write(fd, raw)
        os.fsync(fd)
    finally:
        os.close(fd)
    os.chmod(tmp, mode)
    os.replace(tmp, path)


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    expected = argv[0] if argv else ""
    if expected and (
        len(expected) != 40 or any(c not in "0123456789abcdef" for c in expected)
    ):
        print("reconcile_managed_authority=invalid_commit", file=sys.stderr)
        return 64

    if os.environ.get("EIMEMORY_CONTROL_PLANE") != "user":
        # System/root topology is owned by bootstrap_runtime_manager; skip.
        print("reconcile_managed_authority=skipped_not_user_control_plane")
        return 0
    if not hasattr(os, "geteuid") or os.geteuid() <= 0:
        print("reconcile_managed_authority=skipped_root", file=sys.stderr)
        return 0

    os.environ.setdefault("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
    os.environ.setdefault(
        "DBUS_SESSION_BUS_ADDRESS", f"unix:path=/run/user/{os.getuid()}/bus"
    )

    home = Path.home()
    authority = home / ".config/eimemory/control-plane"
    cp = home / ".local/lib/eimemory/control-plane"
    reg_path = authority / "runtime-manager.v1.json"
    man_path = authority / "managed-code-incident-manifest.v1.json"
    if not reg_path.is_file() or not man_path.is_file():
        print("reconcile_managed_authority=skipped_not_installed")
        return 0

    current = Path("/opt/eimemory/current").resolve(strict=True)
    commit = current.name
    if len(commit) != 40 or any(c not in "0123456789abcdef" for c in commit):
        print(f"reconcile_managed_authority=current_invalid:{commit}", file=sys.stderr)
        return 1
    if expected and commit != expected:
        print(
            f"reconcile_managed_authority=current_mismatch expected={expected} current={commit}",
            file=sys.stderr,
        )
        return 1

    cspec = importlib.util.spec_from_file_location(
        "eimemory_runtime_manager_contract", cp / "runtime_manager_contract.py"
    )
    contract = importlib.util.module_from_spec(cspec)
    assert cspec.loader is not None
    cspec.loader.exec_module(contract)

    reg = json.loads(reg_path.read_text(encoding="utf-8"))
    old_man = json.loads(man_path.read_text(encoding="utf-8"))
    aligned = (
        reg.get("runtime_commit") == commit
        and reg.get("controller_commit") == commit
        and old_man.get("release_commit") == commit
        and old_man.get("source_directory") == str(current)
        and reg.get("managed_incident_authority", {}).get("manifest_sha256")
        == hashlib.sha256(man_path.read_bytes()).hexdigest()
    )
    if aligned:
        print(f"reconcile_managed_authority=already_aligned commit={commit}")
        return 0

    backup_dir = home / "eimemory-local-cleanup-backups" / "managed-authority-reconcile"
    backup_dir.mkdir(parents=True, exist_ok=True)
    ts = time.strftime("%Y%m%dT%H%M%S")
    for src, name in (
        (reg_path, "runtime-manager.v1.json"),
        (man_path, "managed-code-incident-manifest.v1.json"),
    ):
        shutil.copy2(src, backup_dir / f"{ts}-{name}")

    compat = {
        rel: hashlib.sha256((current / rel).read_bytes()).hexdigest()
        for rel in reg["compatibility_hashes"]
    }
    for rel in compat:
        if not (current / rel).is_file():
            print(f"reconcile_managed_authority=compat_missing:{rel}", file=sys.stderr)
            return 1

    sys.path.insert(0, str(current))
    from eimemory.ops.prepare_managed_incident_authority import prepare_manifest

    prev_digest = hashlib.sha256(man_path.read_bytes()).hexdigest()
    new_man = prepare_manifest(
        database_path=reg["managed_incident_authority"]["database_path"],
        source_directory=str(current),
        scopes=old_man["scopes"],
        release_commit=commit,
        previous_manifest_digest=prev_digest,
    )
    man_raw = json.dumps(
        new_man, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode()
    man_sha = hashlib.sha256(man_raw).hexdigest()

    fresh = dict(reg)
    fresh["runtime_commit"] = commit
    fresh["controller_commit"] = commit
    fresh["compatibility_hashes"] = compat
    fresh["controller_entry_digest"] = hashlib.sha256(
        (current / contract.CONTROLLER_ENTRY).read_bytes()
    ).hexdigest()
    fresh["validation_entry_digest"] = hashlib.sha256(
        (current / contract.VALIDATION_ENTRY).read_bytes()
    ).hexdigest()
    mia = dict(fresh["managed_incident_authority"])
    mia["manifest_sha256"] = man_sha
    fresh["managed_incident_authority"] = mia
    fresh["generation"] = contract.digest(
        {k: v for k, v in fresh.items() if k != "generation"}
    )
    contract.validate_registry(fresh)
    reg_raw = contract.canonical(fresh)

    _atomic_write(man_path, man_raw, 0o640)
    _atomic_write(reg_path, reg_raw, 0o640)

    restarted = False
    if Path(f"/run/user/{os.getuid()}/eimemory-manager/bridge.sock").exists() or True:
        r = subprocess.run(
            ["systemctl", "--user", "restart", "eimemory-runtime-manager.service"],
            capture_output=True,
            text=True,
        )
        if r.returncode != 0:
            print(
                f"reconcile_managed_authority=manager_restart_failed rc={r.returncode} err={(r.stderr or r.stdout).strip()}",
                file=sys.stderr,
            )
            return 1
        restarted = True
        time.sleep(2)
        active = subprocess.run(
            ["systemctl", "--user", "is-active", "eimemory-runtime-manager.service"],
            capture_output=True,
            text=True,
        )
        if active.stdout.strip() != "active":
            print("reconcile_managed_authority=manager_not_active", file=sys.stderr)
            return 1

    print(
        json.dumps(
            {
                "reconcile_managed_authority": "updated",
                "commit": commit,
                "manifest_sha256": man_sha,
                "generation": fresh["generation"],
                "producer_files": len(new_man["producer_files"]),
                "manager_restarted": restarted,
                "previous_runtime_commit": reg.get("runtime_commit"),
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
