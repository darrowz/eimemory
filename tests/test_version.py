from __future__ import annotations

import tomllib
from pathlib import Path

from eimemory.version import __version__


def test_package_version_matches_pyproject() -> None:
    pyproject = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))

    assert __version__ == pyproject["project"]["version"]


def test_release_version_is_1_9_51() -> None:
    assert __version__ == "1.9.51"
