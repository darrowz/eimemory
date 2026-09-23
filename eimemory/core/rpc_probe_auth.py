"""Bearer token lookup for local operator probes of the eimemory RPC.

Unauthenticated ``/health`` is intentionally slim; deployment identity checks
need ``commit``/``paths``/``package_tree_digest`` and therefore authenticate
with the same token the RPC service loads from its environment file.
"""

from __future__ import annotations

import os
from pathlib import Path

TOKEN_ENV_NAME = "EIMEMORY_RPC_AUTH_TOKEN"
DEFAULT_RPC_ENV_FILE = "/etc/eimemory/rpc.env"
_MAX_ENV_FILE_BYTES = 64 * 1024


def rpc_probe_token() -> str:
    for name in (TOKEN_ENV_NAME, "EIMEMORY_RPC_TOKEN"):
        value = str(os.environ.get(name) or "").strip()
        if value:
            return value
    path = Path(str(os.environ.get("EIMEMORY_RPC_ENV_FILE") or DEFAULT_RPC_ENV_FILE))
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > _MAX_ENV_FILE_BYTES:
            return ""
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError):
        return ""
    prefix = f"{TOKEN_ENV_NAME}="
    for line in lines:
        stripped = line.strip()
        if stripped.startswith(prefix):
            return stripped[len(prefix):].strip()
    return ""


def rpc_probe_headers() -> dict[str, str]:
    token = rpc_probe_token()
    headers = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers
