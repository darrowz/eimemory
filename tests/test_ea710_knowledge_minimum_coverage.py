"""Offline actual text helpers; no ingress, admission or store execution."""

from hashlib import sha256
from pathlib import Path
import re
import unittest


def live_helpers():
    names = {"_extract_units", "_ensure_minimum_coverage", "_fallback_text", "_first_match_sentence",
             "_candidate_texts", "_classify_unit_type", "_sentences", "_is_heading", "_is_list_item",
             "_is_code_like", "_to_summary", "_clean_text", "_unit_record_id"}
    path = Path(__file__).resolve().parents[1] / "eimemory/knowledge/ingest.py"
    selected = []
    collecting = False
    with path.open(encoding="utf-8") as stream:
        for line in stream:
            if line.startswith("def "):
                collecting = line.split("def ", 1)[1].split("(", 1)[0] in names
            if collecting:
                selected.append(line)
    namespace = {"re": re, "sha256": sha256, "SUPPORTED_SOURCE_KINDS": {"docs"}}
    exec(compile("from __future__ import annotations\n" + "".join(selected),
                 "<isolated knowledge text helpers>", "exec"), namespace)
    return namespace


class MinimumCoverageTests(unittest.TestCase):
    def setUp(self):
        self.helpers = live_helpers()

    def extract(self, title, text):
        return self.helpers["_extract_units"](title=title, text=text, source_kind="docs")

    def assert_coverage(self, units):
        self.assertTrue({"concept", "procedure", "verification"}.issubset({u["unit_type"] for u in units}))
        self.assertEqual(len({(u["unit_type"], u["text"]) for u in units}), len(units))

    def test_overlapping_procedure_and_verification_text(self):
        for text in ("Run tests.", "Configure and verify."):
            with self.subTest(text=text):
                units = self.extract("Topic", text)
                self.assert_coverage(units)
                self.assertIn({"title": "Topic", "unit_type": "verification", "text": text}, units)
                self.assertIn({"title": "Topic", "unit_type": "procedure", "text": text}, units)

    def test_title_body_overlap_still_covers_required_types(self):
        self.assert_coverage(self.extract("Run tests.", "Run tests."))

    def test_neutral_and_empty_sources_keep_coverage(self):
        for title, text in (("Topic", "A compact source concept."), ("", "")):
            with self.subTest(title=title, text=text):
                self.assert_coverage(self.extract(title, text))

    def test_existing_complete_types_are_not_modified(self):
        units = [{"title": "Topic", "unit_type": kind, "text": "shared"}
                 for kind in ("concept", "procedure", "verification")]
        original = [dict(unit) for unit in units]
        self.helpers["_ensure_minimum_coverage"](units=units, seen={"shared"}, source_text="Run tests.", title="Topic")
        self.assertEqual(units, original)

    def test_shared_text_has_type_distinct_local_ids(self):
        ids = [self.helpers["_unit_record_id"](source_uri="urn:inert", source_kind="docs",
               unit_type=kind, text="Run tests.", title="Topic") for kind in ("procedure", "verification")]
        self.assertNotEqual(ids[0], ids[1])


if __name__ == "__main__":
    unittest.main()
