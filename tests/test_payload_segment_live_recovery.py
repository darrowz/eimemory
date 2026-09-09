from __future__ import annotations

import os
import subprocess
import sys

import pytest

from eimemory.storage.payload_segments import PayloadSegmentStore


@pytest.mark.parametrize("crash_at", ["header", "body", "frame", "index"])
@pytest.mark.parametrize("rotate", [False, True])
def test_surviving_writer_recovers_crashed_peer_before_appending(tmp_path, crash_at, rotate):
    root = tmp_path / "segments"
    limit = 512 if rotate else 4096
    survivor = PayloadSegmentStore(root, max_segment_bytes=limit)
    first_raw = os.urandom(400) if rotate else b"before crash"
    first = survivor.append(first_raw)
    child = r'''
import os, sys
from eimemory.storage.payload_segments import PayloadSegmentStore
root, limit, stage = sys.argv[1:]
store = PayloadSegmentStore(root, max_segment_bytes=int(limit))
original_write = store._write_all
def crash_write(fd, data):
    if stage in ("header", "body", "frame"):
        length = {"header": 5, "body": 60, "frame": len(data)}[stage]
        os.write(fd, data[:length])
        os.fsync(fd)
        os._exit(73)
    original_write(fd, data)
store._write_all = crash_write
def crash_stats(**kwargs):
    os._exit(73)
store._record_append_stats = crash_stats
store.append(b"peer crash payload" * 5)
'''
    result = subprocess.run(
        [sys.executable, "-c", child, str(root), str(limit), crash_at], check=False
    )
    assert result.returncode == 73
    after = survivor.append(b"after crash")
    restarted = PayloadSegmentStore(root, max_segment_bytes=limit)
    assert restarted.read(first) == first_raw
    assert restarted.read(after) == b"after crash"
    expected_count = 3 if crash_at in ("frame", "index") else 2
    stats = restarted.quick_stats()
    assert stats["archive_bytes"] == sum(p.stat().st_size for p in root.glob("payload-*.seg"))
    assert stats["indexed_count"] == expected_count
    assert stats["segment_count"] == len(list(root.glob("payload-*.seg")))
    assert stats["stats_exact"] is True
    if crash_at in ("frame", "index"):
        size = stats["archive_bytes"]
        peer = restarted.append(b"peer crash payload" * 5)
        assert restarted.read(peer) == b"peer crash payload" * 5
        assert restarted.quick_stats()["archive_bytes"] == size


def test_normal_appends_do_not_rescan_validated_frames(tmp_path, monkeypatch):
    store = PayloadSegmentStore(tmp_path / "segments")
    store.append(b"first")
    def unexpected(*args, **kwargs):
        raise AssertionError("normal append rescanned archive")
    monkeypatch.setattr(store, "_decompress_verified", unexpected)
    monkeypatch.setattr(store, "archive_stats", unexpected)
    store.append(b"second")
    store.append(b"third")
