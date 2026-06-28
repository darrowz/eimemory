from __future__ import annotations

import os
from pathlib import Path


def default_root(base: str | Path | None = None) -> Path:
    if base is not None:
        return Path(base)
    env_root = os.environ.get("EIMEMORY_ROOT", "").strip()
    if env_root:
        return Path(env_root)
    return Path.home() / ".openclaw" / "memory" / "eimemory"
