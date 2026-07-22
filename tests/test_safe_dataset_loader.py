from __future__ import annotations

from hashlib import sha256
import json
import os
from pathlib import Path
import stat

import pytest

import eimemory.scheduler.jobs as jobs
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
        "canonical_digest": sha256(
            json.dumps(dataset, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest(),
    }
    compatible = _load_json_dataset(str(path))
    assert compatible["cases"] == dataset["cases"]
    assert compatible["_secure_dataset_evidence"] == evidence


def test_list_dataset_keeps_evidence_but_is_diagnostic_only(tmp_path) -> None:
    path = tmp_path / "list.json"
    path.write_text('[{"case_id":"one"}]', encoding="utf-8")

    loaded = _load_json_dataset(str(path))

    assert loaded["cases"] == [{"case_id": "one"}]
    assert loaded["dataset_kind"] == "diagnostic"
    assert loaded["_secure_dataset_evidence"]["canonical_digest"]


def test_production_dataset_fails_closed_when_windows_handle_identity_is_unavailable(
    tmp_path, monkeypatch
) -> None:
    path = tmp_path / "production.json"
    path.write_text('{"dataset_kind":"production","cases":[]}', encoding="utf-8")
    monkeypatch.setattr(jobs, "_requires_windows_handle_verification", lambda: True)
    monkeypatch.setattr(jobs, "_windows_file_identity", lambda *_args, **_kwargs: None)

    with pytest.raises(ValueError, match="Windows handle identity"):
        load_json_dataset_with_evidence(str(path))


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
