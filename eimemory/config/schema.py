from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(slots=True)
class Settings:
    root: Path
    default_agent_id: str = "main"
    default_workspace_id: str = ""
    rpc_host: str = "127.0.0.1"
    rpc_port: int = 8091
