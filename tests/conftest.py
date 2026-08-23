from __future__ import annotations

import os
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def isolate_eimemory_config_environment(monkeypatch) -> None:
    """Keep unit tests independent from an operator/suite config location.

    Individual configuration tests set these variables explicitly after this
    fixture runs.  Clearing inherited values prevents an intentionally empty
    isolated test directory from turning unrelated CLI tests into production
    configuration checks.
    """

    monkeypatch.delenv("EIMEMORY_CONFIG_DIR", raising=False)
    monkeypatch.delenv("EIMEMORY_CONFIG_PATH", raising=False)


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
        if path not in ancestors or metadata.st_uid in trusted_uids:
            return metadata
        values = list(metadata)
        values[4] = 0
        return os.stat_result(values)

    monkeypatch.setattr(Path, "lstat", trusted_ancestor_lstat)
