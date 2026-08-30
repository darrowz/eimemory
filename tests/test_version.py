from __future__ import annotations

import tomllib
from pathlib import Path

from eimemory.version import __version__


def test_package_version_matches_pyproject() -> None:
    pyproject = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))

    assert __version__ == pyproject["project"]["version"]


def test_release_version_has_changelog_entry() -> None:
    changelog = Path("CHANGELOG.md").read_text(encoding="utf-8")

    assert f"## [{__version__}]" in changelog


def test_managed_hermes_plugin_versions_match_release() -> None:
    from deploy.install_hermes_integration import PLUGIN_LAYOUT, _plugin_version

    for source in PLUGIN_LAYOUT.values():
        assert _plugin_version(Path(source) / "plugin.yaml") == __version__
