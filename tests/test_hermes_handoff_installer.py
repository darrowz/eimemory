"""Memory installation preserves native profile paths and private queue trust."""
import importlib.util
import getpass
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys

import pytest

from eimemory.adapters.hermes.outbox import HermesOutbox


pytestmark = [pytest.mark.usefixtures("trusted_dataset_path_ancestors"), pytest.mark.linux_deployment,
              pytest.mark.skipif(os.name != "posix", reason="POSIX directory trust contract")]
SOURCE = Path(__file__).resolve().parents[1] / "deploy" / "install_hermes_integration.py"
SPEC = importlib.util.spec_from_file_location("handoff_installer_under_test", SOURCE)
INSTALLER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(INSTALLER)


def make_home(tmp_path):
    home = tmp_path / "hermes"
    home.mkdir(mode=0o700)
    profiles = home / "profiles"
    profiles.mkdir(mode=0o700)
    for name in ("hongtai", "xiaomage"):
        profile = profiles / name
        profile.mkdir(mode=0o700)
        (profile / "config.yaml").write_text("memory:\n  provider: eimemory\n")
        (profile / "state").mkdir(mode=0o700)
    return home


def test_group_writable_native_profiles_use_independent_private_outbox(tmp_path):
    home = make_home(tmp_path)
    profiles = home / "profiles"
    profiles.chmod(0o775)
    for name in ("hongtai", "xiaomage"):
        (profiles / name / "state").chmod(0o775)
    namespace = {"channel": "hermes", "scope": {"agent_id": "hongtai"}}
    before = HermesOutbox(str(profiles / "hongtai"), namespace)
    assert before.entries() == []
    result = INSTALLER.prepare_handoff_directories(home)
    assert result == {"status": "ready", "profile_count": 2,
                      "adjusted_count": 0, "created_count": 0}
    assert stat.S_IMODE(profiles.stat().st_mode) == 0o775
    for name in ("hongtai", "xiaomage"):
        profile = profiles / name
        assert stat.S_IMODE((profile / "state").stat().st_mode) == 0o775
        outbox = HermesOutbox(str(profile), namespace)
        assert outbox.entries() == []
        assert outbox.error == ""


def test_directory_repair_preserves_existing_handoff_payload_and_is_idempotent(tmp_path):
    home = make_home(tmp_path)
    profile = home / "profiles" / "hongtai"
    outbox = HermesOutbox(str(profile), {"channel": "hermes"})
    outbox.put("pending", "write", {"body": "synthetic unacknowledged payload"})
    before = outbox.path.read_bytes()
    (profile / "state").chmod(0o775)
    first = INSTALLER.prepare_handoff_directories(home)
    second = INSTALLER.prepare_handoff_directories(home)
    assert first["adjusted_count"] == 0
    assert second["adjusted_count"] == second["created_count"] == 0
    assert outbox.path.read_bytes() == before
    assert outbox.entries()[0][1]["payload"]["body"] == "synthetic unacknowledged payload"


def test_collaborative_native_ancestor_is_not_a_queue_permission_prerequisite(tmp_path):
    outside = tmp_path / "shared"
    outside.mkdir(mode=0o775)
    outside.chmod(0o775)
    home = make_home(outside)
    assert INSTALLER.prepare_handoff_directories(home)["status"] == "ready"
    assert HermesOutbox(str(home), {"channel":"hermes"}).put("one","write",{"value":1})
    assert stat.S_IMODE(outside.stat().st_mode) == 0o775
    assert not (home / "state").exists()


def test_native_profile_state_symlink_is_neither_followed_nor_changed(tmp_path):
    home = make_home(tmp_path)
    profiles = home / "profiles"
    profiles.chmod(0o775)
    target = tmp_path / "outside-state"
    target.mkdir(mode=0o755)
    # mkdir's mode is filtered by the invoking process's umask. Establish the
    # external target's exact initial permissions before testing preservation.
    target.chmod(0o755)
    target_before = target.lstat()
    state = profiles / "xiaomage" / "state"
    state.rmdir()
    state.symlink_to(target, target_is_directory=True)
    assert INSTALLER.prepare_handoff_directories(home)["status"] == "ready"
    assert stat.S_IMODE(profiles.stat().st_mode) == 0o775
    assert stat.S_IMODE(target.stat().st_mode) == 0o755
    target_after = target.lstat()
    assert (target_after.st_dev, target_after.st_ino, target_after.st_uid,
            target_after.st_gid, target_after.st_mode) == (
        target_before.st_dev, target_before.st_ino, target_before.st_uid,
        target_before.st_gid, target_before.st_mode)
    assert not (home / "state").exists()


def test_foreign_owner_is_refused_before_any_mode_change(tmp_path, monkeypatch):
    home = make_home(tmp_path)
    home.chmod(0o775)
    monkeypatch.setattr(INSTALLER.os, "geteuid", lambda: os.getuid() + 10000)
    with pytest.raises(RuntimeError, match="untrusted"):
        INSTALLER.prepare_handoff_directories(home)
    assert stat.S_IMODE(home.stat().st_mode) == 0o775
    assert not (home / "state").exists()


def test_read_only_profile_discovery_does_not_traverse_symlink(tmp_path):
    home = make_home(tmp_path)
    outside = tmp_path / "outside-profile"
    outside.mkdir(mode=0o775)
    outside.chmod(0o775)
    (outside / "config.yaml").write_text("memory: {}\n")
    (home / "profiles" / "alias").symlink_to(outside, target_is_directory=True)
    with pytest.raises(RuntimeError, match="handoff directory is untrusted"):
        INSTALLER.prepare_handoff_directories(home)
    assert stat.S_IMODE(outside.stat().st_mode) == 0o775
    assert not (outside / "state").exists()


@pytest.mark.parametrize("marker", ["config.yaml", ".env", "SOUL.md", "profile.yaml", "auth.json", "state.db"])
def test_live_profile_identity_markers_do_not_require_config_yaml(tmp_path, marker):
    home = make_home(tmp_path)
    profile = home / "profiles" / "hongtai"
    (profile / "config.yaml").unlink()
    (profile / marker).write_text("synthetic profile identity\n")
    profile.chmod(0o775)
    (profile / "state").chmod(0o775)
    result = INSTALLER.prepare_handoff_directories(home)
    assert result["profile_count"] == 2
    assert stat.S_IMODE(profile.stat().st_mode) == 0o775
    assert stat.S_IMODE((profile / "state").stat().st_mode) == 0o775
    assert HermesOutbox(str(profile), {"channel": "hermes"}).entries() == []


def test_dangling_identity_marker_still_identifies_a_live_profile(tmp_path):
    home = make_home(tmp_path)
    profile = home / "profiles" / "hongtai"
    (profile / "config.yaml").unlink()
    (profile / "config.yaml").symlink_to(tmp_path / "absent-config.yaml")
    (profile / "state").chmod(0o775)
    result = INSTALLER.prepare_handoff_directories(home)
    assert result["profile_count"] == 2
    assert stat.S_IMODE((profile / "state").stat().st_mode) == 0o775
    assert (profile / "config.yaml").is_symlink()
    assert not (tmp_path / "absent-config.yaml").exists()


def test_tombstoned_profile_remains_untouched(tmp_path):
    home = make_home(tmp_path)
    profile = home / "profiles" / "hongtai"
    tombstones = home / "profiles" / ".deleted"
    tombstones.mkdir(mode=0o700)
    (tombstones / profile.name).write_text("deleted\n")
    profile.chmod(0o775)
    (profile / "state").chmod(0o775)
    result = INSTALLER.prepare_handoff_directories(home)
    assert result["profile_count"] == 1
    assert stat.S_IMODE(profile.stat().st_mode) == 0o775
    assert stat.S_IMODE((profile / "state").stat().st_mode) == 0o775
    assert (tombstones / profile.name).read_text() == "deleted\n"


@pytest.mark.parametrize("name", ["default", ".hidden", "UPPER", "invalid.name", "ghost"])
def test_non_profile_directories_remain_untouched(tmp_path, name):
    home = make_home(tmp_path)
    candidate = home / "profiles" / name
    candidate.mkdir(mode=0o700)
    candidate.chmod(0o775)
    if name != "ghost":
        (candidate / "config.yaml").write_text("memory: {}\n")
    result = INSTALLER.prepare_handoff_directories(home)
    assert result["profile_count"] == 2
    assert stat.S_IMODE(candidate.stat().st_mode) == 0o775
    assert not (candidate / "state").exists()


def test_non_config_identity_does_not_admit_foreign_owned_profile(tmp_path, monkeypatch):
    home = make_home(tmp_path)
    profiles = home / "profiles"
    profiles.chmod(0o775)
    profile = profiles / "hongtai"
    (profile / "config.yaml").unlink()
    (profile / "SOUL.md").write_text("synthetic profile identity\n")
    original_lstat = Path.lstat

    def foreign_profile_lstat(path, *args, **kwargs):
        info = original_lstat(path, *args, **kwargs)
        if path == profile:
            fields = list(info)
            fields[4] = os.geteuid() + 10000
            return os.stat_result(fields)
        return info

    monkeypatch.setattr(INSTALLER.Path, "lstat", foreign_profile_lstat)
    with pytest.raises(RuntimeError, match="handoff directory is untrusted"):
        INSTALLER.prepare_handoff_directories(home)
    assert stat.S_IMODE(profiles.stat().st_mode) == 0o775
    assert not (home / "state").exists()
