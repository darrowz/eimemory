from __future__ import annotations

import os
from pathlib import Path
import stat

import pytest


@pytest.fixture(autouse=True)
def isolate_eimemory_config_environment(monkeypatch, tmp_path: Path) -> None:
    """Keep tests independent from operator configuration and live data roots.

    Individual configuration tests set these variables explicitly after this
    fixture runs.  Clearing inherited values prevents an intentionally empty
    isolated test directory from turning unrelated CLI tests into production
    configuration checks.  Default runtime and OpenClaw loop writes must also
    remain below pytest's managed root so subprocess and hook tests cannot add
    synthetic records to a real operator store or task ledger.
    """

    monkeypatch.delenv("EIMEMORY_CONFIG_DIR", raising=False)
    monkeypatch.delenv("EIMEMORY_CONFIG_PATH", raising=False)
    monkeypatch.setenv("EIMEMORY_ROOT", str(tmp_path / "eimemory-root"))
    monkeypatch.setenv("OPENCLAW_LOOP_HOME", str(tmp_path / "openclaw-loop"))


@pytest.fixture
def trusted_dataset_path_ancestors(tmp_path, monkeypatch) -> None:
    """Model conventional root-owned ancestors in user-namespace sandboxes."""

    if os.name == "nt":
        return
    effective_uid = os.geteuid()
    trusted_uids = {0, effective_uid}
    ancestors = set(tmp_path.parents)
    real_lstat = Path.lstat

    def trusted_ancestor_lstat(path: Path):
        metadata = real_lstat(path)
        if path not in ancestors:
            return metadata
        values = list(metadata)
        if metadata.st_uid not in trusted_uids:
            values[4] = 0
        if not metadata.st_mode & stat.S_ISVTX:
            values[0] = int(metadata.st_mode) & ~(stat.S_IWGRP | stat.S_IWOTH)
        return os.stat_result(values)

    monkeypatch.setattr(Path, "lstat", trusted_ancestor_lstat)
