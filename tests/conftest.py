from __future__ import annotations

import os
from pathlib import Path

import pytest


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
