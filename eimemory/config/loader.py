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
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("eimemory config must be a JSON object")
    return payload


def _string_setting(payload: dict[str, Any], key: str, default: str) -> str:
    value = payload.get(key, default)
    if not isinstance(value, str):
        raise ValueError(f"{key} must be a string")
    return value


def _integer_setting(value: Any, key: str) -> int:
    if type(value) is int:
        return value
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            pass
    raise ValueError(f"{key} must be an integer")


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
        default_agent_id=_string_setting(payload, "default_agent_id", "main"),
        default_workspace_id=_string_setting(payload, "default_workspace_id", ""),
        rpc_host=_string_setting(payload, "rpc_host", "127.0.0.1"),
        rpc_port=_integer_setting(payload.get("rpc_port", 8091), "rpc_port"),
        rpc_loopback_health_host=_string_setting(payload, "rpc_loopback_health_host", ""),
        rpc_loopback_health_port=(None if loopback_health_port is None or loopback_health_port == ""
                                  else _integer_setting(loopback_health_port, "rpc_loopback_health_port")),
        trusted_repository_root=str(
            os.environ.get("EIMEMORY_TRUSTED_REPOSITORY_ROOT", "").strip()
            or payload.get("trusted_repository_root", "")
        ),
        trusted_remote=str(
            os.environ.get("EIMEMORY_TRUSTED_REMOTE", "").strip()
            or payload.get("trusted_remote", "origin")
            or "origin"
        ),
        trusted_branch=str(
            os.environ.get("EIMEMORY_TRUSTED_BRANCH", "").strip()
            or payload.get("trusted_branch", "master")
            or "master"
        ),
    )
