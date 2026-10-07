"""Filesystem-only installer regression tests; no Hermes/provider imports.

Run from any working directory with Python's unittest discovery. The installer
is located relative to this file, not argv or the process working directory.
JSON fixtures form the deliberately small YAML subset needed by these tests.
"""
from contextlib import contextmanager
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import types
import unittest
from unittest.mock import patch


SOURCE = Path(__file__).resolve().parents[1] / "deploy" / "install_hermes_integration.py"
SPEC = importlib.util.spec_from_file_location("installer_current_root_under_test", SOURCE)
INSTALLER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(INSTALLER)
IDENTITY = {
    "capability_id": "code.implementation",
    "revision_id": "fixture-revision",
    "binding_id": "fixture-binding",
    "implementation_digest": "fixture-digest",
}


@contextmanager
def working_directory(path):
    before = Path.cwd()
    os.chdir(path)
    try:
        yield
    finally:
        os.chdir(before)


class CurrentRootTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.cwd = self.root / "caller"
        self.cwd.mkdir()
        self.home = self.root / "separate-hermes-home"
        self.home.mkdir()
        (self.home / "config.yaml").write_text(json.dumps({
            "memory": {"existing": True},
            "plugins": {"enabled": ["other"], "disabled": ["unrelated"]},
        }), encoding="utf-8")
        self.release = self.make_release("release-one", "one")
        self.current = self.cwd / "current"
        self.current.symlink_to(self.release, target_is_directory=True)
        fake_yaml = types.ModuleType("yaml")
        fake_yaml.safe_load = json.loads
        fake_yaml.safe_dump = lambda value, stream, **kwargs: json.dump(value, stream)
        yaml_patch = patch.dict("sys.modules", {"yaml": fake_yaml})
        yaml_patch.start()
        self.addCleanup(yaml_patch.stop)
        identity_patch = patch.object(INSTALLER, "provider_identity", return_value=dict(IDENTITY))
        self.identity = identity_patch.start()
        self.addCleanup(identity_patch.stop)

    def make_release(self, name, marker):
        release = self.root / name
        release.mkdir()
        (release / "pyproject.toml").write_text('[project]\nversion = "1.2.3"\n', encoding="utf-8")
        for relative in INSTALLER.PLUGIN_LAYOUT.values():
            plugin = release / relative
            plugin.mkdir(parents=True)
            (plugin / "__init__.py").write_text("# fixture only\n", encoding="utf-8")
            (plugin / "plugin.yaml").write_text('{"version": "1.2.3"}', encoding="utf-8")
            (plugin / "marker.txt").write_text(marker, encoding="utf-8")
        return release

    def install(self, **kwargs):
        with working_directory(self.cwd):
            return INSTALLER.install_hermes_integration(
                release_root=self.release, hermes_home=self.home, **kwargs
            )

    def link(self, name="eimemory"):
        return self.home / "plugins" / name

    def assert_targets(self, current):
        for name, relative in INSTALLER.PLUGIN_LAYOUT.items():
            self.assertEqual(os.readlink(self.link(name)), str(current / relative))

    def test_relative_current_uses_callers_cwd_not_plugins_parent(self):
        self.install(current_root="current")
        self.assert_targets(self.current)
        self.assertEqual((self.link() / "marker.txt").read_text(), "one")

    def test_default_absolute_current_is_unchanged(self):
        self.install()
        self.assert_targets(Path("/opt/eimemory/current"))

    def test_absolute_alias_is_not_dereferenced_and_follows_release_switch(self):
        self.install(current_root=self.current)
        self.assert_targets(self.current)
        self.assertEqual((self.link() / "marker.txt").read_text(), "one")
        second = self.make_release("release-two", "two")
        self.current.unlink()
        self.current.symlink_to(second, target_is_directory=True)
        self.assert_targets(self.current)
        self.assertEqual((self.link() / "marker.txt").read_text(), "two")

    def test_provider_only_cleanup_uses_the_same_absolute_target(self):
        self.install(current_root=self.current)
        hook = self.release / INSTALLER.PLUGIN_LAYOUT["eimemory_hook"] / "__init__.py"
        hook.unlink()
        result = self.install(current_root="current", allow_provider_only=True)
        self.assertEqual(result["links"], {"eimemory": "unchanged", "eimemory_hook": "removed"})
        self.assertFalse(self.link("eimemory_hook").is_symlink())
        self.assertFalse(result["hook_enabled"])
        self.assertEqual(result["code_implementation"]["revision_id"], "")

    def test_provider_only_preserves_legacy_relative_hook_as_unmanaged(self):
        hook_link = self.link("eimemory_hook")
        hook_link.parent.mkdir(parents=True)
        legacy_target = str(Path("current") / INSTALLER.PLUGIN_LAYOUT["eimemory_hook"])
        hook_link.symlink_to(legacy_target, target_is_directory=True)
        (self.release / INSTALLER.PLUGIN_LAYOUT["eimemory_hook"] / "__init__.py").unlink()
        result = self.install(current_root="current", allow_provider_only=True)
        self.assertEqual(result["links"]["eimemory_hook"], "unmanaged")
        self.assertTrue(hook_link.is_symlink())
        self.assertEqual(os.readlink(hook_link), legacy_target)

    def test_repeated_relative_install_does_not_migrate_again(self):
        for name in INSTALLER.PLUGIN_LAYOUT:
            self.link(name).mkdir(parents=True)
            (self.link(name) / "original.txt").write_text(name, encoding="utf-8")
        first = self.install(current_root="current")
        second = self.install(current_root="current")
        self.assertEqual(set(first["links"].values()), {"migrated"})
        self.assertEqual(set(second["links"].values()), {"unchanged"})
        self.assert_targets(self.current)
        backup = self.home / ".eimemory-plugin-backups"
        self.assertEqual(sorted(p.name for p in backup.iterdir()), ["eimemory.pre-managed", "eimemory_hook.pre-managed"])
        for name in INSTALLER.PLUGIN_LAYOUT:
            self.assertEqual((backup / f"{name}.pre-managed" / "original.txt").read_text(), name)

    def test_tilde_expands_using_explicit_temporary_home(self):
        with patch.dict(os.environ, {"HOME": str(self.cwd), "USERPROFILE": str(self.cwd)}):
            self.install(current_root="~/current")
        self.assert_targets(self.current)
        self.assertEqual((self.link() / "marker.txt").read_text(), "one")

    def test_version_config_and_provider_identity_contracts_unchanged(self):
        result = self.install(current_root=self.current)
        self.assertEqual(result["version"], "1.2.3")
        self.assertEqual(result["code_implementation"], IDENTITY)
        self.identity.assert_called_once_with(self.release)
        self.assertTrue(result["ok"])
        self.assertTrue(result["hook_enabled"])
        config = json.loads((self.home / "config.yaml").read_text())
        self.assertEqual(config, {
            "memory": {"existing": True, "provider": "eimemory"},
            "plugins": {"enabled": ["other", "eimemory-hook"], "disabled": ["unrelated"]},
        })


if __name__ == "__main__":
    unittest.main()
