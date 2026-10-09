"""Isolated mark_step field regression; run directly without repository imports."""

from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
import unittest


def load_mark_step():
    source = Path(__file__).parents[1] / "eimemory/governance/learning/learning_state.py"
    lines = []
    with source.open(encoding="utf-8") as stream:
        for line in stream:
            if line.startswith("def mark_step("):
                lines.append(line)
                break
        else:
            raise AssertionError("mark_step definition missing")
        for line in stream:
            if line.startswith("def "):
                break
            lines.append(line)
    namespace = {
        "_learning_store_lock": lambda runtime: nullcontext(),
        "_resolve_loop": lambda runtime, record: record,
        "TERMINAL_LOOP_STATUSES": frozenset({"completed", "failed"}),
    }
    exec(compile("from __future__ import annotations\n" + "".join(lines),
                 "<isolated mark_step>", "exec"), namespace)
    return namespace["mark_step"], namespace


class StepCreatedAtTests(unittest.TestCase):
    def setUp(self):
        self.mark_step, self.namespace = load_mark_step()
        self.times = iter(("t1", "t2"))
        self.namespace["now_iso"] = lambda: next(self.times)
        self.writes = []
        self.touches = []
        self.runtime = SimpleNamespace(store=SimpleNamespace(rewrite=self.rewrite))
        self.record = SimpleNamespace(
            status="running", content={}, meta={},
            touch=lambda: self.touches.append(True),
        )

    def rewrite(self, record):
        self.writes.append(record)
        return record

    def update(self):
        result = self.mark_step(self.runtime, self.record,
                                step_name="collect", status="done")
        self.assertIs(result, self.record)
        self.assertIs(self.writes[-1], self.record)
        self.assertEqual(len(self.touches), len(self.writes))
        return self.record.content["steps"][0]

    def test_new_step_records_creation_time(self):
        step = self.update()
        self.assertEqual(step["created_at"], "t1")
        self.assertEqual(step["updated_at"], "t1")

    def test_second_update_preserves_first_time(self):
        self.update()
        step = self.update()
        self.assertEqual(step["created_at"], "t1")
        self.assertEqual(step["updated_at"], "t2")
        self.assertEqual(len(self.record.content["steps"]), 1)

    def test_existing_creation_time_is_preserved(self):
        self.record.content = {"steps": [{"step_name": "collect",
            "created_at": "original", "updated_at": "previous", "extra": 7}]}
        step = self.update()
        self.assertEqual(step["created_at"], "original")
        self.assertEqual(step["updated_at"], "t1")
        self.assertEqual(step["extra"], 7)

    def test_historical_updated_at_supplies_creation_time(self):
        self.record.content = {"steps": [{"step_name": "collect",
            "updated_at": "previous"}]}
        step = self.update()
        self.assertEqual(step["created_at"], "previous")
        self.assertEqual(step["updated_at"], "t1")


if __name__ == "__main__":
    unittest.main()
