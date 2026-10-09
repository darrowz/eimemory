"""Offline tests of selected live functions; no project or service imports."""

from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
import textwrap
import unittest


def load_function(relative_path, name, namespace):
    source = Path(__file__).parents[1] / relative_path
    lines = []
    level = None
    with source.open(encoding="utf-8") as stream:
        for line in stream:
            stripped = line.lstrip()
            if stripped.startswith("def " + name + "("):
                level = len(line) - len(stripped)
                lines.append(line)
                break
        else:
            raise AssertionError("missing function: " + name)
        for line in stream:
            stripped = line.lstrip()
            if stripped.startswith(("def ", "class ")) and len(line) - len(stripped) <= level:
                break
            lines.append(line)
    exec(compile("from __future__ import annotations\n" + textwrap.dedent("".join(lines)),
                 "<isolated " + name + ">", "exec"), namespace)
    return namespace[name]


class ExpiryTests(unittest.TestCase):
    def setUp(self):
        self.clock = "2026-10-09T00:00:00+00:00"
        self.expired = load_function(
            "eimemory/governance/learning/learning_retention.py", "_is_expired",
            {"datetime": datetime, "now_iso": lambda: self.clock})

    def check(self, value):
        return self.expired(SimpleNamespace(meta={"expires_at": value}, content={}))

    def test_incompatible_timezone_is_not_expired(self):
        self.assertFalse(self.check("2026-01-01T00:00:00"))

    def test_aware_past_and_future(self):
        self.assertTrue(self.check("2026-01-01T00:00:00Z"))
        self.assertFalse(self.check("2027-01-01T00:00:00+00:00"))

    def test_equal_timestamp(self):
        self.assertFalse(self.check(self.clock))

    def test_naive_pair_still_compares(self):
        self.clock = "2026-10-09T00:00:00"
        self.assertTrue(self.check("2026-01-01T00:00:00"))

    def test_reverse_timezone_mismatch(self):
        self.clock = "2026-10-09T00:00:00"
        self.assertFalse(self.check("2026-01-01T00:00:00Z"))

    def test_invalid_and_empty_values(self):
        self.assertFalse(self.check("invalid"))
        self.assertFalse(self.check(""))


class IntakeLengthTests(unittest.TestCase):
    def setUp(self):
        self.calls = []
        self.allowed = True
        self.roots = ("inert-root",)

        def local_path(uri, *, allowed_roots=None):
            self.calls.append((uri, allowed_roots))
            return object() if self.allowed and allowed_roots == self.roots else None

        namespace = {
            "VALID_SOURCE_KINDS": {"document"},
            "DECISION_REJECTED": "rejected", "DECISION_QUARANTINED": "quarantined",
            "DECISION_CANDIDATE": "candidate",
            "_meaningful_source_metadata": lambda metadata: {},
            "_looks_like_prompt_injection": lambda text: False,
            "_decode_depth_exceeded": lambda text: False,
            "_looks_like_secret": lambda text: False,
            "_local_path_from_uri": local_path,
            "_alnum_text": lambda text: "".join(c for c in text if c.isalnum()),
        }
        self.screen = load_function("eimemory/intake/loop.py", "_screen", namespace)
        self.controller = SimpleNamespace(min_content_chars=4,
            _allowed_local_roots=lambda: self.roots)
        self.source = SimpleNamespace(enabled=True, source_kind="document",
            title="fixture", uri="inert-file", tags=[], metadata={})

    def run_screen(self, excerpt, reason="local_file_read"):
        material = SimpleNamespace(title="fixture", summary="summary",
            content_excerpt=excerpt, screening_text=excerpt, reason=reason)
        return self.screen(self.controller, self.source, material)

    def test_short_local_content_is_rejected_and_roots_forwarded(self):
        self.assertEqual(self.run_screen("a!"), ("rejected", "content_too_short"))
        self.assertEqual(self.calls, [(self.source.uri, self.roots)])

    def test_exact_threshold_is_accepted(self):
        self.assertEqual(self.run_screen("a b c d"), ("candidate", "accepted"))

    def test_production_threshold_boundaries_and_empty_excerpt(self):
        self.controller.min_content_chars = 32
        for size in (0, 1, 31, 32, 33):
            with self.subTest(size=size):
                expected = ("rejected", "content_too_short") if size < 32 else ("candidate", "accepted")
                self.assertEqual(self.run_screen("a" * size), expected)

    def test_punctuation_and_unicode_use_alphanumeric_length(self):
        self.controller.min_content_chars = 32
        self.assertEqual(self.run_screen("!" * 33), ("rejected", "content_too_short"))
        self.assertEqual(self.run_screen("字" * 32), ("candidate", "accepted"))

    def test_unresolved_path_keeps_metadata_behavior(self):
        self.allowed = False
        self.assertEqual(self.run_screen("a"), ("candidate", "accepted"))

    def test_missing_file_reason_precedes_length_check(self):
        self.assertEqual(self.run_screen("", "local_file_missing"),
                         ("rejected", "local_file_missing"))
        self.assertEqual(self.calls, [])

    def test_disabled_source_precedes_length_check(self):
        self.source.enabled = False
        self.assertEqual(self.run_screen(""), ("rejected", "disabled_source"))
        self.assertEqual(self.calls, [])


if __name__ == "__main__":
    unittest.main()
