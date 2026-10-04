"""Finite archive-failure checks using one extracted method and memory-only fakes.

No project import, SQLite connection, archive helper, file-backed data store, or
subprocess is used. SQL text is classified by a fake; it is never executed.
This is an inert control-flow harness, not an integration or durability test.
"""
from __future__ import annotations

import argparse
import ast
import copy
from hashlib import sha256
import json
from pathlib import Path
import unittest


class InertPayloadError(Exception):
    pass


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def extract_archival_method(path):
    """Compile only the selected method; all its collaborators are inert fakes."""
    tree = ast.parse(path.read_text())
    store_class = next(node for node in tree.body
                       if isinstance(node, ast.ClassDef) and node.name == "SqliteRecordStore")
    node = next(node for node in store_class.body
                if isinstance(node, ast.FunctionDef) and node.name == "apply_payload_archival_batch")
    assert not node.decorator_list
    assert not any(isinstance(child, (ast.Import, ast.ImportFrom, ast.ClassDef))
                   for child in ast.walk(node))
    # Allow only the collaborators supplied below. Prevent this harness from
    # silently becoming an executor for added production dependencies.
    allowed_calls = {
        "max", "min", "int", "str", "len", "dict", "sha256",
        "canonical_payload_json", "PayloadSegmentError",
        "self.payload_archival_complete", "self.conn.execute",
        "self._payload_dict_from_json", "payload.get",
        "canonical_payload_json(payload).encode",
        "sha256(canonical).hexdigest", "self.payload_segments.append",
        "written_pointers.append", "self._compact_record_payload", "compact.get",
        "prepared.append", "json.dumps", "self._save_migration_cursor",
        "_PAYLOAD_ARCHIVE_KINDS.index", "self._complete_deferred_migration",
        "self.conn.commit", "self.conn.rollback",
        "self.payload_segments.reclaim_uncommitted_appends",
    }
    for child in ast.walk(node):
        if isinstance(child, ast.Call):
            name = ast.unparse(child.func)
            if name.endswith(".fetchone") or name.endswith(".fetchall"):
                assert isinstance(child.func.value, ast.Call)
                assert ast.unparse(child.func.value.func) == "self.conn.execute"
            else:
                assert name in allowed_calls, name
    module = ast.Module(body=[node], type_ignores=[])
    ast.fix_missing_locations(module)
    namespace = {
        "Any": object, "json": json, "sha256": sha256,
        "canonical_payload_json": canonical,
        "PayloadSegmentError": InertPayloadError,
        "_PAYLOAD_ARCHIVE_KINDS": ("capability_score", "recall_view"),
        "_PAYLOAD_ARCHIVE_MIGRATION": "records.payload_archive.v1",
    }
    exec(compile(module, "<inert-extracted-archival-method>", "exec"), namespace)
    return namespace["apply_payload_archival_batch"]


class Cursor:
    def __init__(self, *, one=None, rows=None, count=0):
        self.one, self.rows, self.rowcount = one, rows or [], count

    def fetchone(self):
        return self.one

    def fetchall(self):
        return self.rows


class MemorySegments:
    """Synthetic digest frames; reclaim mutates only this Python dictionary."""
    max_payload_bytes = 100_000

    def __init__(self):
        self.frames = {}
        self.append_calls = 0
        self.new_frames = 0
        self.reclaim_calls = 0
        self.after_append = None
        self.cleanup_error = None

    def seed(self, raw):
        digest = sha256(raw).hexdigest()
        if digest not in self.frames:
            self.frames[digest] = {"raw": raw, "pointer": {"digest": digest, "offset": self.new_frames}}
            self.new_frames += 1
        return dict(self.frames[digest]["pointer"])

    def append(self, raw):
        self.append_calls += 1
        pointer = self.seed(raw)
        hook, self.after_append = self.after_append, None
        if hook is not None:
            hook(pointer)
        return pointer

    def reclaim_uncommitted_appends(self, pointers):
        self.reclaim_calls += 1
        if self.cleanup_error:
            raise self.cleanup_error
        for pointer in sorted(pointers, key=lambda item: item["offset"], reverse=True):
            if not self.frames:
                continue
            tail = max(self.frames.values(), key=lambda item: item["pointer"]["offset"])
            if tail["pointer"] == pointer:
                del self.frames[pointer["digest"]]


class MemoryConnection:
    def __init__(self, owner):
        self.owner = owner
        self.snapshot = None
        self.after_select = None
        self.fail_update_number = None
        self.update_error = None
        self.commit_error = None
        self.begin_error = None
        self.update_count = 0
        self.commits = 0
        self.rollbacks = 0
        self.statements = []

    def execute(self, sql, params=()):
        self.statements.append(sql)
        if sql.startswith("SELECT phase,cursor FROM schema_migration_progress"):
            return Cursor(one=copy.deepcopy(self.owner.progress))
        if sql.startswith("WITH hot AS ("):
            phase, hot_window, repeated_phase, cursor, minimum_bytes, limit = params
            assert phase == repeated_phase and hot_window == 0
            rows = [copy.deepcopy(row) for key, row in sorted(self.owner.rows.items())
                    if row["kind"] == phase and not row["payload_pointer_json"]
                    and key > cursor and len(row["payload_json"].encode()) > minimum_bytes][:limit]
            hook, self.after_select = self.after_select, None
            if hook is not None:
                hook()
            return Cursor(rows=rows)
        if sql == "BEGIN IMMEDIATE":
            if self.begin_error:
                raise self.begin_error
            assert self.snapshot is None
            self.snapshot = copy.deepcopy((self.owner.rows, self.owner.progress, self.owner.complete))
            return Cursor()
        if sql.startswith("UPDATE records SET payload_json="):
            self.update_count += 1
            if self.fail_update_number == self.update_count:
                raise self.update_error
            compact, meta, pointer, digest, key, original = params
            row = self.owner.rows[key]
            if row["payload_pointer_json"] or row["payload_json"] != original:
                return Cursor(count=0)
            row.update(payload_json=compact, meta_json=meta,
                       payload_pointer_json=pointer, payload_digest=digest)
            return Cursor(count=1)
        raise AssertionError("Unexpected SQL text in inert classifier: " + sql)

    def commit(self):
        if self.commit_error:
            raise self.commit_error
        self.commits += 1
        self.snapshot = None

    def rollback(self):
        self.rollbacks += 1
        assert self.snapshot is not None
        self.owner.rows, self.owner.progress, self.owner.complete = self.snapshot
        self.snapshot = None


class MemoryStore:
    payload_archive_inline_bytes = 1

    def __init__(self, count=1):
        self.rows = {}
        for index in range(count):
            key = "key" + str(index)
            payload = {"kind": "capability_score", "record_id": key, "content": "synthetic text " * 10}
            self.rows[key] = {"storage_key": key, "kind": payload["kind"],
                              "payload_json": canonical(payload), "payload_pointer_json": ""}
        self.progress = {"phase": "capability_score", "cursor": ""}
        self.complete = False
        self.payload_segments = MemorySegments()
        self.conn = MemoryConnection(self)
        self.compact_error = None

    def payload_archival_complete(self):
        return self.complete

    def _payload_dict_from_json(self, raw):
        try:
            value = json.loads(raw)
        except json.JSONDecodeError:
            return None
        return value if isinstance(value, dict) else None

    def _compact_record_payload(self, payload, *, digest, raw_size):
        if self.compact_error:
            raise self.compact_error
        return {"kind": payload["kind"], "record_id": payload["record_id"],
                "meta": {"_payload_archive": {"digest": digest, "raw_size": raw_size}}}

    def _save_migration_cursor(self, migration, cursor, *, phase):
        assert migration == "records.payload_archive.v1"
        self.progress = {"phase": phase, "cursor": cursor}

    def _complete_deferred_migration(self, migration):
        assert migration == "records.payload_archive.v1"
        self.progress = None
        self.complete = True

    def peer_commit(self, pointer, key="key0"):
        self.rows[key]["payload_pointer_json"] = json.dumps(pointer)
        self.rows[key]["payload_digest"] = pointer["digest"]


ARCHIVE_METHOD = None


class ArchiveRetentionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        global ARCHIVE_METHOD
        if ARCHIVE_METHOD is None:
            source = Path(__file__).resolve().parents[1] / "eimemory/storage/sqlite_store.py"
            ARCHIVE_METHOD = extract_archival_method(source)

    def run_batch(self, store):
        return ARCHIVE_METHOD(store, batch_size=10, hot_window=0)

    def assert_pointer_survives(self, store, key="key0"):
        pointer = json.loads(store.rows[key]["payload_pointer_json"])
        self.assertIn(pointer["digest"], store.payload_segments.frames,
                      "a peer-committed pointer lost its synthetic frame")

    def test_peer_adopts_new_migration_frame_before_cas(self):
        store = MemoryStore()
        store.payload_segments.after_append = store.peer_commit
        with self.assertRaisesRegex(InertPayloadError, "concurrently rewritten"):
            self.run_batch(store)
        self.assert_pointer_survives(store)
        self.assertEqual(store.conn.rollbacks, 1)
        self.assertEqual(store.progress["cursor"], "")
        self.assertEqual(store.payload_segments.reclaim_calls, 0)

    def test_migration_receives_pointer_already_committed_by_peer(self):
        store = MemoryStore()
        raw = store.rows["key0"]["payload_json"].encode()
        store.conn.after_select = lambda: store.peer_commit(store.payload_segments.seed(raw))
        with self.assertRaisesRegex(InertPayloadError, "concurrently rewritten"):
            self.run_batch(store)
        self.assert_pointer_survives(store)
        self.assertEqual(store.payload_segments.new_frames, 1)
        self.assertEqual(store.conn.rollbacks, 1)
        self.assertEqual(store.progress["cursor"], "")

    def test_preparation_error_is_preserved_without_cleanup_masking(self):
        store = MemoryStore()
        original = RuntimeError("inert compact failure")
        store.compact_error = original
        store.payload_segments.cleanup_error = LookupError("inert cleanup failure")
        with self.assertRaises(RuntimeError) as raised:
            self.run_batch(store)
        self.assertIs(raised.exception, original)
        self.assertEqual(len(store.payload_segments.frames), 1)
        self.assertEqual(store.conn.rollbacks, 0)
        self.assertEqual(store.rows["key0"]["payload_pointer_json"], "")
        self.assertEqual(store.progress["cursor"], "")

    def test_second_update_failure_rolls_back_first_row_and_cursor(self):
        store = MemoryStore(count=2)
        before = copy.deepcopy(store.rows)
        original = RuntimeError("inert second update failure")
        store.conn.fail_update_number = 2
        store.conn.update_error = original
        with self.assertRaises(RuntimeError) as raised:
            self.run_batch(store)
        self.assertIs(raised.exception, original)
        self.assertEqual(store.rows, before)
        self.assertEqual(store.progress["cursor"], "")
        self.assertEqual(store.conn.rollbacks, 1)
        self.assertEqual(len(store.payload_segments.frames), 2)

    def test_commit_failure_rolls_back_then_retry_reuses_retained_frame(self):
        store = MemoryStore()
        before = copy.deepcopy(store.rows)
        original = RuntimeError("inert commit failure")
        store.conn.commit_error = original
        with self.assertRaises(RuntimeError) as raised:
            self.run_batch(store)
        self.assertIs(raised.exception, original)
        self.assertEqual(store.rows, before)
        self.assertEqual(store.progress["cursor"], "")
        self.assertEqual(store.conn.rollbacks, 1)
        self.assertEqual(len(store.payload_segments.frames), 1)
        store.conn.commit_error = None
        result = self.run_batch(store)
        self.assertEqual(result["processed"], 1)
        self.assertEqual(store.payload_segments.new_frames, 1)
        self.assertEqual(store.payload_segments.append_calls, 2)
        self.assert_pointer_survives(store)

    def test_success_keeps_pointer_and_advances_cursor(self):
        store = MemoryStore()
        result = self.run_batch(store)
        self.assertEqual(result, {"schema": "payload_archive_batch.v1", "processed": 1,
                                  "complete": False, "has_more": True})
        self.assert_pointer_survives(store)
        self.assertEqual(store.progress, {"phase": "capability_score", "cursor": "key0"})
        self.assertEqual(store.conn.commits, 1)
        self.assertEqual(store.conn.rollbacks, 0)
        self.assertEqual(store.payload_segments.reclaim_calls, 0)

    def test_empty_phases_complete_and_completed_retry_is_inert(self):
        store = MemoryStore(count=0)
        self.assertFalse(self.run_batch(store)["complete"])
        self.assertEqual(store.progress, {"phase": "recall_view", "cursor": ""})
        self.assertTrue(self.run_batch(store)["complete"])
        statement_count = len(store.conn.statements)
        self.assertEqual(self.run_batch(store)["processed"], 0)
        self.assertEqual(len(store.conn.statements), statement_count)
        self.assertEqual(store.payload_segments.append_calls, 0)

    def test_invalid_and_oversized_payloads_fail_before_append(self):
        for condition in ("invalid", "oversized"):
            with self.subTest(condition=condition):
                store = MemoryStore()
                if condition == "invalid":
                    store.rows["key0"]["payload_json"] = "invalid-json"
                else:
                    store.payload_segments.max_payload_bytes = 2
                with self.assertRaises(InertPayloadError):
                    self.run_batch(store)
                self.assertEqual(store.payload_segments.append_calls, 0)
                self.assertEqual(store.conn.rollbacks, 0)
                self.assertEqual(store.progress["cursor"], "")

    def test_begin_failure_preserves_error_and_retains_prepared_frame(self):
        store = MemoryStore()
        original = RuntimeError("inert begin failure")
        store.conn.begin_error = original
        with self.assertRaises(RuntimeError) as raised:
            self.run_batch(store)
        self.assertIs(raised.exception, original)
        self.assertEqual(store.conn.rollbacks, 0)
        self.assertEqual(store.progress["cursor"], "")
        self.assertEqual(store.rows["key0"]["payload_pointer_json"], "")
        self.assertEqual(len(store.payload_segments.frames), 1)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    args = parser.parse_args()
    ARCHIVE_METHOD = extract_archival_method(args.source)
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(ArchiveRetentionTests)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    raise SystemExit(0 if result.wasSuccessful() else 1)
