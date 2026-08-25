from __future__ import annotations

from hashlib import sha256
import json
import os
from pathlib import Path
import stat

import pytest

import eimemory.scheduler.jobs as jobs
from eimemory.scheduler.jobs import _load_json_dataset, load_json_dataset_with_evidence


pytestmark = pytest.mark.usefixtures("trusted_dataset_path_ancestors")


def test_secure_dataset_loader_returns_fd_bound_evidence_and_compat_wrapper_embeds_it(tmp_path) -> None:
    path = tmp_path / "production_recall.json"
    raw = json.dumps({"cases": [{"case_id": "safe"}]}, sort_keys=True).encode("utf-8")
    path.write_bytes(raw)
    path.chmod(stat.S_IRUSR | stat.S_IWUSR)

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


@pytest.mark.skipif(os.name == "nt", reason="POSIX effective ownership is not authoritative on Windows")
def test_secure_dataset_loader_uses_effective_uid_as_the_trusted_process_owner(
    tmp_path,
    monkeypatch,
) -> None:
    path = tmp_path / "effective-owner.json"
    path.write_text('{"cases": []}', encoding="utf-8")
    path.chmod(stat.S_IRUSR | stat.S_IWUSR)
    # The loader rejects group/world-writable files regardless of ownership;
    # normalize the mode so this test exercises the effective-uid contract
    # instead of depending on the invoking shell's umask.
    path.chmod(stat.S_IRUSR | stat.S_IWUSR)
    effective_uid = os.geteuid()
    foreign_real_uid = effective_uid + 1 if effective_uid != 0 else 1
    monkeypatch.setattr(jobs.os, "getuid", lambda: foreign_real_uid)
    monkeypatch.setattr(jobs.os, "geteuid", lambda: effective_uid)

    dataset, _evidence = load_json_dataset_with_evidence(str(path))

    assert dataset == {"cases": []}


@pytest.mark.skipif(os.name == "nt", reason="POSIX effective ownership is not authoritative on Windows")
def test_secure_dataset_loader_passes_effective_not_real_uid_to_parent_validation(
    tmp_path,
    monkeypatch,
) -> None:
    path = tmp_path / "effective-owner-selection.json"
    effective_uid = 424_242
    real_uid = 313_131
    captured: dict[str, set[int]] = {}

    def capture_trusted_uids(_parent: Path, *, trusted_uids: set[int]):
        captured["trusted_uids"] = set(trusted_uids)
        raise jobs.DatasetUnreadableError("stop after trusted uid capture")

    monkeypatch.setattr(jobs.os, "getuid", lambda: real_uid)
    monkeypatch.setattr(jobs.os, "geteuid", lambda: effective_uid)
    monkeypatch.setattr(jobs, "_validate_dataset_parent_chain", capture_trusted_uids)

    with pytest.raises(jobs.DatasetUnreadableError, match="trusted uid capture"):
        load_json_dataset_with_evidence(str(path))

    assert captured["trusted_uids"] == {0, effective_uid}
    assert real_uid not in captured["trusted_uids"]


def test_list_dataset_keeps_evidence_but_is_diagnostic_only(tmp_path) -> None:
    path = tmp_path / "list.json"
    path.write_text('[{"case_id":"one"}]', encoding="utf-8")
    path.chmod(stat.S_IRUSR | stat.S_IWUSR)

    loaded = _load_json_dataset(str(path))

    assert loaded["cases"] == [{"case_id": "one"}]
    assert loaded["dataset_kind"] == "diagnostic"
    assert loaded["_secure_dataset_evidence"]["canonical_digest"]


def test_production_dataset_fails_closed_when_windows_handle_identity_is_unavailable(
    tmp_path, monkeypatch
) -> None:
    path = tmp_path / "production.json"
    path.write_text('{"dataset_kind":"production","cases":[]}', encoding="utf-8")
    path.chmod(stat.S_IRUSR | stat.S_IWUSR)
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


@pytest.mark.skipif(os.name == "nt", reason="POSIX ownership is not authoritative on Windows")
@pytest.mark.parametrize(
    "mode",
    [
        stat.S_IRUSR | stat.S_IXUSR | stat.S_IRGRP | stat.S_IXGRP | stat.S_IROTH | stat.S_IXOTH,
        stat.S_IRWXU | stat.S_IRWXG | stat.S_IRWXO | stat.S_ISVTX,
    ],
    ids=["read-only-0555", "sticky-01777"],
)
def test_secure_dataset_loader_rejects_foreign_owned_parent_without_mode_bypass(
    tmp_path,
    monkeypatch,
    mode: int,
) -> None:
    parent = tmp_path / "foreign-parent"
    parent.mkdir()
    path = parent / "dataset.json"
    path.write_text('{"cases": []}', encoding="utf-8")
    parent.chmod(mode)
    real_lstat = Path.lstat
    effective_uid = os.geteuid()
    foreign_uid = effective_uid + 1 if effective_uid != 0 else 1

    def foreign_parent_lstat(path: Path):
        metadata = real_lstat(path)
        if path != parent:
            return metadata
        values = list(metadata)
        values[4] = foreign_uid
        foreign_metadata = os.stat_result(values)
        assert foreign_metadata.st_uid not in {0, effective_uid}
        return foreign_metadata

    monkeypatch.setattr(Path, "lstat", foreign_parent_lstat)
    try:
        with pytest.raises(jobs.DatasetUnreadableError, match="owner is not trusted"):
            load_json_dataset_with_evidence(str(path))
    finally:
        parent.chmod(stat.S_IRWXU)


@pytest.mark.skipif(os.name == "nt", reason="symlink creation and POSIX trust chain are required")
def test_secure_dataset_loader_rejects_symlink_in_parent_ancestor_chain(tmp_path) -> None:
    real = tmp_path / "real"
    nested = real / "nested"
    nested.mkdir(parents=True)
    dataset = nested / "dataset.json"
    dataset.write_text('{"cases": []}', encoding="utf-8")
    # Normalize modes: the loader rejects group/world-writable files and
    # ancestors before the symlink check; this test targets the symlink contract,
    # not the umask-dependent tmp permissions.
    dataset.chmod(stat.S_IRUSR | stat.S_IWUSR)
    nested.chmod(stat.S_IRWXU)
    real.chmod(stat.S_IRWXU)
    linked = tmp_path / "linked"
    linked.symlink_to(real, target_is_directory=True)

    with pytest.raises(ValueError, match="parent chain"):
        load_json_dataset_with_evidence(str(linked / "nested" / "dataset.json"))
