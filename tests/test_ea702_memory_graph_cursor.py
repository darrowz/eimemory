"""Selected live cursor functions with an inert in-memory store."""

from pathlib import Path
from types import SimpleNamespace as NS
import unittest


def selected_function(name):
    path = Path(__file__).resolve().parents[1] / "eimemory/governance/learning/memory_graph.py"
    lines = []
    with path.open(encoding="utf-8") as stream:
        for line in stream:
            if line.startswith("def " + name + "("):
                lines.append(line)
                break
        else:
            raise AssertionError("missing selected function: " + name)
        for line in stream:
            if line.startswith("def "):
                break
            lines.append(line)
    return "".join(lines)


class InertStore:
    def __init__(self):
        self.cursor = None
        self.appends = 0
        self.rewrites = 0

    def list_records(self, **kwargs):
        return [self.cursor] if self.cursor is not None else []

    def get_by_id(self, record_id, **kwargs):
        return self.cursor

    def append(self, record):
        self.cursor = record
        self.appends += 1

    def rewrite(self, record):
        self.cursor = record
        self.rewrites += 1


class CursorTests(unittest.TestCase):
    def setUp(self):
        self.store = InertStore()
        self.runtime = NS(store=self.store)
        self.scope = NS(agent_id="fixture", workspace_id="fixture", user_id="fixture")
        namespace = {
            "business_metadata": lambda meta: meta,
            "stable_semantic_key": lambda *args: "inert-key",
            "_GRAPH_CURSOR_TITLE": "inert-title",
            "RecordEnvelope": NS(create=lambda **fields: NS(record_id="cursor", touch=lambda: None, **fields)),
        }
        for name in ("_record_time", "_high_watermark", "_load_graph_cursor", "_save_graph_cursor", "_record_after_cursor"):
            exec(compile("from __future__ import annotations\n" + selected_function(name),
                         "<isolated " + name + ">", "exec"), namespace)
        self.functions = namespace

    def record(self, record_id, timestamp="2026-01-01T00:00:00Z"):
        return NS(record_id=record_id, time=NS(updated_at=timestamp, occurred_at="", created_at=""))

    def save(self, records):
        self.functions["_save_graph_cursor"](self.runtime, scope=self.scope, records=records)

    def pending(self, records):
        cursor = self.functions["_load_graph_cursor"](self.runtime, scope=self.scope)
        return [record for record in records if self.functions["_record_after_cursor"](record, cursor)]

    def test_tied_records_are_processed_once_across_three_batches(self):
        records = [self.record(item) for item in ("a", "b", "c")]
        for expected in ("a", "b", "c"):
            batch = self.pending(records)[:1]
            self.assertEqual([record.record_id for record in batch], [expected])
            self.save(batch)
        self.assertEqual(self.pending(records), [])
        self.assertEqual(self.store.cursor.content["seen_record_ids"], ["a", "b", "c"])
        self.assertEqual((self.store.appends, self.store.rewrites), (1, 2))

    def test_same_watermark_union_is_unique_and_preserves_metadata(self):
        a, b = self.record("a"), self.record("b")
        self.save([a])
        self.store.cursor.meta["fixture"] = "retained"
        self.save([b, b])
        self.assertEqual(self.store.cursor.content["seen_record_ids"], ["a", "b"])
        self.assertEqual(self.store.cursor.meta["fixture"], "retained")

    def test_advancing_watermark_resets_previous_ids(self):
        a, b = self.record("a"), self.record("b", "2026-01-02T00:00:00Z")
        self.save([a])
        self.save([b])
        self.assertEqual(self.store.cursor.content, {"last_seen": b.time.updated_at, "seen_record_ids": ["b"]})
        self.assertEqual(self.pending([a, b]), [])

    def test_first_mixed_batch_only_tracks_ids_at_high_watermark(self):
        a, b = self.record("a"), self.record("b", "2026-01-02T00:00:00Z")
        self.save([a, b])
        self.assertEqual(self.store.cursor.content, {"last_seen": b.time.updated_at, "seen_record_ids": ["b"]})
        self.assertEqual((self.store.appends, self.store.rewrites), (1, 0))


if __name__ == "__main__":
    unittest.main()
