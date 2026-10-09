"""Offline DTO conversion regression; no project imports or store access."""

from dataclasses import dataclass
from pathlib import Path
import unittest


def live_watch_class():
    source = Path(__file__).resolve().parents[1] / "eimemory/governance/world_watchers.py"
    lines = []
    with source.open(encoding="utf-8") as stream:
        for line in stream:
            if line.startswith("@dataclass"):
                lines.append(line)
                break
        for line in stream:
            if line.startswith("def collect_world_signals"):
                break
            lines.append(line)
    namespace = {"dataclass": dataclass}
    exec(compile("from __future__ import annotations\n" + "".join(lines),
                 "<isolated SourceWatch>", "exec"), namespace)
    return namespace["SourceWatch"]


class ZeroLimitTests(unittest.TestCase):
    def setUp(self):
        self.watch = live_watch_class()

    def test_zero_representations_match_direct_construction(self):
        direct = self.watch(name="fixture", kind="local_state", max_items=0)
        for value in (0, 0.0, "0", -1):
            with self.subTest(value=value):
                parsed = self.watch.from_dict({"max_items": value})
                self.assertEqual(parsed.max_items, direct.max_items)
                self.assertEqual(["inert"][:parsed.max_items], [])

    def test_missing_and_falsey_compatibility(self):
        self.assertEqual(self.watch.from_dict({}).max_items, 20)
        for value in (False, None, "", [], {}):
            with self.subTest(value=value):
                self.assertEqual(self.watch.from_dict({"max_items": value}).max_items, 20)

    def test_positive_values_and_other_fields(self):
        for value, expected in ((3, 3), ("3", 3), (True, 1)):
            with self.subTest(value=value):
                parsed = self.watch.from_dict({"name": "fixture", "kind": "local_state", "max_items": value})
                self.assertEqual(parsed.max_items, expected)
                self.assertEqual((parsed.name, parsed.kind), ("fixture", "local_state"))

    def test_invalid_text_still_raises(self):
        with self.assertRaises(ValueError):
            self.watch.from_dict({"max_items": "invalid"})


if __name__ == "__main__":
    unittest.main()
