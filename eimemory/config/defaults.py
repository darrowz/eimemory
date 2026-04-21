from __future__ import annotations

from pathlib import Path


def default_root(base: str | Path | None = None) -> Path:
    if base is not None:
        return Path(base)
    return Path.home() / ".openclaw" / "memory" / "eimemory"
