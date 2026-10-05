"""EA-090 isolated ordinary helpers: stdlib only, no project imports or gate."""

import ast
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
import unittest


SOURCE = Path(__file__).resolve().parents[1] / "eimemory/knowledge/synthesis.py"
HELPERS = {
    "_recent_records", "_record_date_key", "_publication_date", "_record_date",
    "_top_papers", "_notable_claims", "_page_type", "_paper_source_id", "_summary",
}


def load_helpers(path):
    """Compile only reviewed pure functions; skip every original import."""
    tree = ast.parse(path.read_text())
    nodes = [ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0)]
    nodes.extend(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in HELPERS)
    namespace = {"datetime": datetime, "timezone": timezone}
    exec(compile(ast.fix_missing_locations(ast.Module(body=nodes, type_ignores=[])), str(path), "exec"), namespace)
    return namespace


HELPER = load_helpers(SOURCE)


def record(record_id, published_at="", *, provenance=None, confidence=0.8):
    return SimpleNamespace(
        record_id=record_id, title=record_id, summary="", kind="paper_source",
        content={"published_at": published_at, "claim_text": f"Finding from {record_id}"},
        provenance=provenance or {}, meta={},
        time=SimpleNamespace(created_at="", updated_at="", occurred_at=""),
        confidence=confidence,
    )


def claim_evidence(records):
    """Explicit synthetic ranking inputs, not an evidence-admission simulation."""
    return [(item, {
        "confidence": item.confidence, "source": "synthetic", "published_at": "synthetic",
        "evidence_tier": "synthetic", "conflict_check": "synthetic",
    }) for item in records]


DATE_PAIRS = [
    ("2026-10-05T09:00:00+02:00", "2026-10-05T08:00:00Z"),
    ("2026-10-05T01:00:00+02:00", "2026-10-05T00:30:00Z"),
    ("2026-10-05T08:00:00Z", "2026-10-05T08:00:00.500Z"),
]


class SynthesisDateTests(unittest.TestCase):
    def test_top_paper_uses_chronology_for_offsets_and_precision(self):
        for earlier, later in DATE_PAIRS:
            with self.subTest(earlier=earlier, later=later):
                old, new = record("old", earlier), record("new", later)
                ranked = HELPER["_recent_records"]([old, new])
                result = HELPER["_top_papers"](ranked, [], 1)
                self.assertEqual(result[0]["record_id"], "new")
                self.assertEqual(result[0]["published_at"], later)

    def test_equal_confidence_claims_use_chronology(self):
        for earlier, later in DATE_PAIRS:
            with self.subTest(earlier=earlier, later=later):
                old, new = record("old", earlier), record("new", later)
                result = HELPER["_notable_claims"](claim_evidence([old, new]), 1)
                self.assertEqual(result[0]["claim_id"], "new")

    def test_confidence_still_precedes_recency_for_claims(self):
        old = record("old", "2020-01-01", confidence=0.9)
        new = record("new", "2026-10-05", confidence=0.8)
        result = HELPER["_notable_claims"](claim_evidence([old, new]), 1)
        self.assertEqual(result[0]["claim_id"], "old")

    def test_equivalent_dates_retain_record_id_tie_break(self):
        for equivalent in ["2026-10-05", "2026-10-05T00:00:00", "2026-10-05T02:00:00+02:00"]:
            with self.subTest(equivalent=equivalent):
                a = record("a", equivalent)
                z = record("z", "2026-10-05T00:00:00Z")
                self.assertEqual([r.record_id for r in HELPER["_recent_records"]([a, z])], ["z", "a"])
                self.assertEqual([r.record_id for r in HELPER["_recent_records"]([z, a])], ["z", "a"])

    def test_naive_and_date_only_values_are_utc(self):
        expected = (True, datetime(2026, 10, 5, tzinfo=timezone.utc))
        for value in ["2026-10-05", "2026-10-05T00:00:00"]:
            with self.subTest(value=value):
                self.assertEqual(HELPER["_record_date_key"](record("paper", value)), expected)

    def test_equal_claim_instants_retain_record_id_tie_break(self):
        a = record("a", "2026-10-05T02:00:00+02:00")
        z = record("z", "2026-10-05T00:00:00Z")
        result = HELPER["_notable_claims"](claim_evidence([a, z]), 1)
        self.assertEqual(result[0]["claim_id"], "z")

    def test_invalid_and_missing_sort_dates_are_last_and_deterministic(self):
        valid = record("a", "0001-01-01T00:00:00Z")
        invalid, missing = record("z", "not-a-date"), record("y")
        actual = HELPER["_recent_records"]([missing, invalid, valid])
        self.assertEqual([r.record_id for r in actual], ["a", "z", "y"])

    def test_top_paper_preserves_publication_date_and_existing_precedence(self):
        for content_date in ["", "2026-10-04T19:00:00-04:00"]:
            with self.subTest(content_date=content_date):
                provenance_date = "2026-10-05T08:00:00Z"
                paper = record("paper", content_date, provenance={"published_at": provenance_date})
                result = HELPER["_top_papers"]([paper], [], 1)
                self.assertEqual(result[0]["published_at"], content_date or provenance_date)

    def test_record_time_fallback_is_not_presented_as_paper_publication_date(self):
        old, new = record("old"), record("new")
        old.time = SimpleNamespace(updated_at="2026-10-06", occurred_at="2026-10-04")
        new.time = SimpleNamespace(updated_at="2026-10-05", occurred_at="")
        result = HELPER["_top_papers"](HELPER["_recent_records"]([old, new]), [], 1)
        self.assertEqual(result[0]["record_id"], "new")
        self.assertEqual(result[0]["published_at"], "")

    def test_knowledge_page_fallback_still_uses_record_date(self):
        page = record("page")
        page.content["page_type"] = "paper"
        page.time = SimpleNamespace(updated_at="2026-10-03", occurred_at="2026-10-02")
        result = HELPER["_top_papers"]([], [page], 1)
        self.assertEqual(result[0]["published_at"], "2026-10-02")
        self.assertEqual(result[0]["source_kind"], "knowledge_page")

    def test_ambiguous_summary_behavior_is_unchanged(self):
        summary = HELPER["_summary"]([], [{"name": "AI"}], [], ["What next?"])
        self.assertEqual(summary, "No recent paper knowledge available for synthesis.")


if __name__ == "__main__":
    unittest.main()
