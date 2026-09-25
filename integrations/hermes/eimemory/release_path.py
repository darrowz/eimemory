"""Put the immutable release that owns a Hermes plugin back on ``sys.path``.

Hermes isolated launchers delete ``PYTHONPATH`` before directory plugins
load. The plugin file is the reliable location of the release.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


def _is_eimemory_release(candidate: Path) -> bool:
    """The Hermes plugin directory also contains an ``eimemory`` package.

    That package is the memory provider, not the release library, and it has
    no ``eimemory.adapters``.
    """

    return (
        candidate / "eimemory" / "adapters" / "hermes" / "provider_core.py"
    ).is_file()


def release_root_candidates(origin: Path) -> list[Path]:
    """Release roots that contain this plugin, then any PYTHONPATH root."""

    roots: list[Path] = []
    for candidate in origin.resolve().parents:
        if _is_eimemory_release(candidate):
            roots.append(candidate)
            break
    for entry in os.environ.get("PYTHONPATH", "").split(os.pathsep):
        candidate = Path(entry).expanduser()
        if not candidate.is_absolute():
            continue
        if not _is_eimemory_release(candidate):
            continue
        roots.append(candidate)
    return roots


def ensure_release_on_path(origin: Path | None = None) -> None:
    """Insert the release root that owns *origin* at the front of ``sys.path``."""

    start = origin or Path(__file__)
    for candidate in release_root_candidates(start):
        resolved = str(candidate.resolve())
        if resolved not in sys.path:
            sys.path.insert(0, resolved)
        return
