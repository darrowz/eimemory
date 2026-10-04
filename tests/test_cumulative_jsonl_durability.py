"""Controlled temporary logs and injected filesystem failures, no real data."""
from unittest.mock import Mock

import pytest

from eimemory.storage import jsonl


def test_backup_read_does_not_publish_over_primary(tmp_path, monkeypatch):
    log = jsonl.JsonlLog(tmp_path / 'records.jsonl')
    log.manifest_backup_path.write_text('backup')
    recovered = {'version': 1, 'segments': [], 'pending_segment': None}
    monkeypatch.setattr(log, '_load_manifest', lambda path: recovered)
    write = Mock()
    monkeypatch.setattr(jsonl, 'atomic_write_json', write)
    assert log._read_manifest() == recovered
    write.assert_not_called()
    assert not log.manifest_path.exists()


def test_rotated_file_sealed_before_namespace_publication(tmp_path, monkeypatch):
    log = jsonl.JsonlLog(tmp_path / 'records.jsonl', max_segment_bytes=10)
    log.max_segment_bytes = 10  # Force the boundary; constructor enforces a production minimum.
    log.path.write_bytes(b'1234567890')
    events = []
    monkeypatch.setattr(jsonl.os, 'fsync', lambda fd: events.append('seal-file'))
    monkeypatch.setattr(jsonl.os, 'replace', lambda *args: events.append('rename'))
    monkeypatch.setattr(jsonl, '_fsync_directory', lambda path: events.append('seal-directory'))
    log._rotate_if_needed(1)
    assert events == ['seal-file', 'rename', 'seal-directory']


def test_missing_active_file_still_retries_directory_durability(tmp_path, monkeypatch):
    log = jsonl.JsonlLog(tmp_path / 'records.jsonl')
    log._last_needs_fsync = True
    monkeypatch.setattr(jsonl, '_fsync_directory', Mock(side_effect=OSError('fake fsync failure')))
    with pytest.raises(OSError, match='fake fsync failure'):
        log.flush_durable()
    assert log._last_needs_fsync is True


def test_manifest_size_validation_precedes_both_writes(tmp_path, monkeypatch):
    log = jsonl.JsonlLog(tmp_path / 'records.jsonl')
    monkeypatch.setattr(jsonl, 'MAX_MANIFEST_BYTES', 32)
    manifest = {'version': 1, 'segments': ['records.segment-' + 'a'*32 + '.jsonl']}
    with pytest.raises(ValueError, match='invalid JSON state payload'):
        log._write_manifest(manifest)
    assert not log.manifest_path.exists() and not log.manifest_backup_path.exists()
