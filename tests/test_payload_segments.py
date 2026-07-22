from __future__ import annotations

import os
from pathlib import Path

import pytest

import eimemory.storage.payload_segments as payload_segments
from eimemory.storage.payload_segments import PayloadSegmentError, PayloadSegmentStore


def test_segment_store_round_trips_rotates_and_creates_private_regular_files(tmp_path) -> None:
    store = PayloadSegmentStore(tmp_path / "segments", max_segment_bytes=512, max_payload_bytes=2048)
    first_payload = os.urandom(300)
    second_payload = os.urandom(300)

    first = store.append(first_payload)
    second = store.append(second_payload)

    assert first["digest"] != second["digest"]
    assert first["segment"] != second["segment"]
    assert store.read(first) == first_payload
    assert store.read(second) == second_payload
    for path in (tmp_path / "segments").glob("payload-*.seg"):
        assert path.is_file() and not path.is_symlink()
        if os.name != "nt":
            assert path.stat().st_mode & 0o077 == 0
        assert path.stat().st_size <= 512


def test_segment_store_rejects_oversize_missing_and_tampered_payloads(tmp_path) -> None:
    store = PayloadSegmentStore(tmp_path / "segments", max_segment_bytes=1024, max_payload_bytes=512)
    with pytest.raises(PayloadSegmentError, match="hard limit"):
        store.append(b"x" * 513)
    pointer = store.append(os.urandom(256))
    segment = tmp_path / "segments" / pointer["segment"]
    with segment.open("r+b") as handle:
        handle.seek(pointer["offset"] + pointer["header_size"] + 3)
        original = handle.read(1)
        handle.seek(-1, os.SEEK_CUR)
        handle.write(bytes([original[0] ^ 0xFF]))
    with pytest.raises(PayloadSegmentError, match="corrupt|digest|decompress"):
        store.read(pointer)
    segment.unlink()
    with pytest.raises(PayloadSegmentError, match="missing"):
        store.read(pointer)


def test_segment_store_rejects_symlink_segment(tmp_path) -> None:
    if not hasattr(os, "symlink"):
        pytest.skip("symlink unsupported")
    store = PayloadSegmentStore(tmp_path / "segments", max_segment_bytes=1024, max_payload_bytes=512)
    pointer = store.append(b"trusted")
    segment = tmp_path / "segments" / pointer["segment"]
    target = tmp_path / "outside.seg"
    segment.replace(target)
    try:
        segment.symlink_to(target)
    except OSError:
        pytest.skip("symlink creation unavailable")
    with pytest.raises(PayloadSegmentError, match="symlink|reparse"):
        store.read(pointer)


def test_segment_store_persists_digest_index_across_restart_without_duplicate_append(tmp_path) -> None:
    root = tmp_path / "segments"
    first_store = PayloadSegmentStore(root, max_segment_bytes=1024, max_payload_bytes=512)
    first = first_store.append(b"content-addressed")
    segment = root / first["segment"]
    size = segment.stat().st_size

    second_store = PayloadSegmentStore(root, max_segment_bytes=1024, max_payload_bytes=512)
    second = second_store.append(b"content-addressed")

    assert second == first
    assert segment.stat().st_size == size
    assert second_store.quick_stats() == {
        "segment_count": 1,
        "archive_bytes": size,
        "indexed_count": 1,
        "stats_exact": True,
    }


def test_segment_store_reports_orphans_only_during_explicit_inventory(tmp_path) -> None:
    store = PayloadSegmentStore(tmp_path / "segments", max_segment_bytes=1024, max_payload_bytes=512)
    referenced = store.append(b"referenced")
    orphaned = store.append(b"orphaned")

    report = store.orphan_report({referenced["digest"]})

    assert report["orphan_count"] == 1
    assert report["orphan_digest_sample"] == [orphaned["digest"]]
    assert report["action"] == "offline_segment_compaction_required"


def test_segment_store_recovers_torn_latest_tail_and_reindexes_complete_frame(tmp_path) -> None:
    root = tmp_path / "segments"
    store = PayloadSegmentStore(root, max_segment_bytes=1024, max_payload_bytes=512)
    pointer = store.append(b"complete-frame")
    segment = root / pointer["segment"]
    complete_size = segment.stat().st_size
    index_path = root / "index" / pointer["digest"][:2] / f"{pointer['digest']}.json"
    index_path.unlink()
    with segment.open("ab") as handle:
        handle.write(b"EIPS\x01")

    recovered = PayloadSegmentStore(root, max_segment_bytes=1024, max_payload_bytes=512)

    assert segment.stat().st_size == complete_size
    assert recovered.append(b"complete-frame") == pointer
    assert segment.stat().st_size == complete_size


def test_segment_store_rolls_back_partial_write_before_reporting_failure(tmp_path, monkeypatch) -> None:
    root = tmp_path / "segments"
    store = PayloadSegmentStore(root, max_segment_bytes=1024, max_payload_bytes=512)
    original_write = payload_segments.os.write
    calls = 0

    def partial_then_fail(descriptor, data):
        nonlocal calls
        calls += 1
        if calls == 1:
            return original_write(descriptor, bytes(data[: max(1, len(data) // 2)]))
        raise OSError("simulated interrupted write")

    monkeypatch.setattr(payload_segments.os, "write", partial_then_fail)
    with pytest.raises(OSError, match="interrupted"):
        store.append(os.urandom(256))
    segment = next(root.glob("payload-*.seg"))
    assert segment.stat().st_size == 0
