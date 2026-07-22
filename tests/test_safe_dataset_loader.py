from __future__ import annotations

from hashlib import sha256
import json
import os
from pathlib import Path
import stat

import pytest

from eimemory.scheduler.jobs import _load_json_dataset, load_json_dataset_with_evidence


def test_secure_dataset_loader_returns_fd_bound_evidence_and_compat_wrapper_embeds_it(tmp_path) -> None:
    path = tmp_path / "production_recall.json"
    raw = json.dumps({"cases": [{"case_id": "safe"}]}, sort_keys=True).encode("utf-8")
    path.write_bytes(raw)

    dataset, evidence = load_json_dataset_with_evidence(str(path))

    assert dataset == {"cases": [{"case_id": "safe"}]}
    assert evidence == {
        "schema": "secure_dataset_fingerprint.v1",
        "sha256": sha256(raw).hexdigest(),
        "digest": sha256(raw).hexdigest(),
        "size": len(raw),
        "device": path.stat().st_dev,
        "inode": path.stat().st_ino,
    }
    compatible = _load_json_dataset(str(path))
    assert compatible["cases"] == dataset["cases"]
    assert compatible["_secure_dataset_evidence"] == evidence


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits are not authoritative on Windows")
@pytest.mark.parametrize("mode", [stat.S_IRUSR | stat.S_IWUSR | stat.S_IWGRP, stat.S_IRUSR | stat.S_IWUSR | stat.S_IWOTH])
def test_secure_dataset_loader_rejects_group_or_world_writable_file(tmp_path, mode) -> None:
    path = tmp_path / "unsafe.json"
    path.write_text('{"cases": []}', encoding="utf-8")
    path.chmod(mode)

    with pytest.raises(ValueError, match="writable"):
        load_json_dataset_with_evidence(str(path))


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits are not authoritative on Windows")
def test_secure_dataset_loader_rejects_untrusted_writable_parent(tmp_path) -> None:
    parent = tmp_path / "unsafe-parent"
    parent.mkdir()
    path = parent / "dataset.json"
    path.write_text('{"cases": []}', encoding="utf-8")
    parent.chmod(stat.S_IRWXU | stat.S_IRWXG)
    try:
        with pytest.raises(ValueError, match="parent"):
            load_json_dataset_with_evidence(str(path))
    finally:
        parent.chmod(stat.S_IRWXU)


@pytest.mark.skipif(os.name == "nt", reason="symlink creation and POSIX trust chain are required")
def test_secure_dataset_loader_rejects_symlink_in_parent_ancestor_chain(tmp_path) -> None:
    real = tmp_path / "real"
    nested = real / "nested"
    nested.mkdir(parents=True)
    (nested / "dataset.json").write_text('{"cases": []}', encoding="utf-8")
    linked = tmp_path / "linked"
    linked.symlink_to(real, target_is_directory=True)

    with pytest.raises(ValueError, match="parent chain"):
        load_json_dataset_with_evidence(str(linked / "nested" / "dataset.json"))
