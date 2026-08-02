from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import secrets
import stat
import sys
import tempfile
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from eimemory.adapters.runtime.host_auth import is_strong_producer_token

if os.name == "posix":
    import grp
    import pwd


def _read_private_text(path: Path) -> str:
    if not path.exists():
        return ""
    if path.is_symlink():
        raise RuntimeError(f"credential path must not be a symlink: {path}")
    metadata = path.stat()
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
        raise RuntimeError(f"credential path must be a single regular file: {path}")
    if os.name == "posix" and metadata.st_mode & 0o077:
        raise RuntimeError(f"credential path must use mode 0600: {path}")
    return path.read_text(encoding="utf-8").strip()


def _atomic_private_write(path: Path, content: str, *, uid: int | None, gid: int | None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.parent.is_symlink():
        raise RuntimeError(f"credential parent must not be a symlink: {path.parent}")
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        if os.name == "posix" and uid is not None and gid is not None:
            os.fchown(descriptor, uid, gid)
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        if os.name == "posix":
            parent_descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(parent_descriptor)
            finally:
                os.close(parent_descriptor)
    finally:
        if temporary.exists():
            temporary.unlink()


def ensure_hermes_attestation_profile(
    *,
    registry_path: str | Path,
    hermes_token_path: str | Path,
    uid: int | None = None,
    gid: int | None = None,
) -> dict[str, Any]:
    registry = Path(registry_path).expanduser()
    token_file = Path(hermes_token_path).expanduser()
    registry_payload: dict[str, str] = {}
    raw_registry = _read_private_text(registry)
    if raw_registry:
        parsed = json.loads(raw_registry)
        if not isinstance(parsed, dict) or set(parsed) - {"codex", "hermes"}:
            raise RuntimeError("attestation registry contains unsupported producers")
        registry_payload = {str(key): str(value).strip() for key, value in parsed.items()}
        if any(not is_strong_producer_token(value) for value in registry_payload.values()):
            raise RuntimeError("attestation registry contains a weak producer token")
        if len(set(registry_payload.values())) != len(registry_payload):
            raise RuntimeError("attestation registry producer tokens must be unique")

    existing_token = _read_private_text(token_file)
    if existing_token and not is_strong_producer_token(existing_token):
        raise RuntimeError("Hermes attestation token file is weak")
    registry_token = registry_payload.get("hermes", "")
    token = existing_token or registry_token
    created = False
    if not token:
        token = secrets.token_urlsafe(48)
        while token in set(registry_payload.values()) or not is_strong_producer_token(token):
            token = secrets.token_urlsafe(48)
        created = True
    registry_payload["hermes"] = token

    _atomic_private_write(token_file, token + "\n", uid=uid, gid=gid)
    _atomic_private_write(
        registry,
        json.dumps(registry_payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
        + "\n",
        uid=uid,
        gid=gid,
    )
    return {
        "ok": True,
        "channel": "hermes",
        "created": created,
        "registry_path": str(registry),
        "token_path": str(token_file),
        "producer_count": len(registry_payload),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Provision the operator-separated Hermes attestation profile.")
    parser.add_argument("--registry", required=True)
    parser.add_argument("--hermes-token", required=True)
    parser.add_argument("--user", default="")
    parser.add_argument("--group", default="")
    args = parser.parse_args()
    uid = pwd.getpwnam(args.user).pw_uid if os.name == "posix" and args.user else None  # type: ignore[name-defined]
    gid = grp.getgrnam(args.group).gr_gid if os.name == "posix" and args.group else None  # type: ignore[name-defined]
    report = ensure_hermes_attestation_profile(
        registry_path=args.registry,
        hermes_token_path=args.hermes_token,
        uid=uid,
        gid=gid,
    )
    print(json.dumps(report, ensure_ascii=True, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
