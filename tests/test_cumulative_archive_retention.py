import json
import sqlite3
from threading import RLock
import pytest
from eimemory.models.records import RecordEnvelope, ScopeRef
from eimemory.storage.sqlite_store import SqliteRecordStore
from eimemory.storage.payload_segments import PayloadSegmentError


def test_failed_archival_cas_preserves_peer_committed_frame(tmp_path, monkeypatch):
    db = tmp_path / "state.sqlite"
    store = SqliteRecordStore(db, archive_writes=False, payload_archive_inline_bytes=1024)
    lock = RLock()
    store.bind_runtime_lock(lock)
    lock.acquire()
    try:
        record = RecordEnvelope.create(kind="capability_score", title="fixture", scope=ScopeRef(),
                 content={"report": "fixture payload " * 2000})
        store.upsert(record)
        original_append = store.payload_segments.append
        adopted = []
        def adopt(raw):
            pointer = original_append(raw)
            other = sqlite3.connect(db)
            try:
                other.execute("UPDATE records SET payload_pointer_json=?,payload_digest=? WHERE record_id=?",
                    (json.dumps(pointer), pointer["digest"], record.record_id))
                other.commit()
            finally:
                other.close()
            adopted.append((pointer, raw))
            return pointer
        monkeypatch.setattr(store.payload_segments, "append", adopt)
        with pytest.raises(PayloadSegmentError, match="concurrently rewritten"):
            store.apply_payload_archival_batch(batch_size=1, hot_window=0)
        assert len(adopted) == 1
        pointer, raw = adopted[0]
        assert store.payload_segments.read(pointer) == raw
        row = store.conn.execute("SELECT payload_pointer_json FROM records WHERE record_id=?", (record.record_id,)).fetchone()
        assert json.loads(row[0]) == pointer
    finally:
        store.close()
        lock.release()
