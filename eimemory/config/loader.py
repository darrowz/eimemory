from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from eimemory.config.defaults import default_root
from eimemory.config.schema import Settings


def _load_file_payload(path: Path, *, required: bool = False) -> dict[str, Any]:
    if not path.exists():
        if required:
            raise FileNotFoundError(f"missing eimemory config file: {path}")
        return {}
    return dict(json.loads(path.read_text(encoding="utf-8")))


def load_settings() -> Settings:
    config_path_value = os.environ.get("EIMEMORY_CONFIG_PATH", "").strip()
    config_dir_value = os.environ.get("EIMEMORY_CONFIG_DIR", "").strip()
    payload: dict[str, Any] = {}
    if config_path_value:
        payload = _load_file_payload(Path(config_path_value), required=True)
    elif config_dir_value:
        payload = _load_file_payload(Path(config_dir_value) / "settings.json", required=True)
    root_value = os.environ.get("EIMEMORY_ROOT", "").strip()
    root = Path(root_value) if root_value else default_root(payload.get("root"))
    loopback_health_port = payload.get("rpc_loopback_health_port")
    return Settings(
        root=root,
        default_agent_id=str(payload.get("default_agent_id", "main")),
        default_workspace_id=str(payload.get("default_workspace_id", "")),
        rpc_host=str(payload.get("rpc_host", "127.0.0.1")),
        rpc_port=int(payload.get("rpc_port", 8091)),
        rpc_loopback_health_host=str(payload.get("rpc_loopback_health_host", "")),
        rpc_loopback_health_port=int(loopback_health_port) if loopback_health_port not in {None, ""} else None,
    )
