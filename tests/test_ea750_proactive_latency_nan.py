"""Offline numeric DTO checks; no project imports or authenticity claims."""
from pathlib import Path
import textwrap
import unittest


def load_validator():
    source = (Path(__file__).resolve().parents[1] /
              "eimemory/retrieval/proactive.py").read_text()
    start = source.index("    def _validated_terminal_outcome(")
    end = source.index("\n    def close(", start)
    namespace = {}
    exec(compile("from __future__ import annotations\n" +
                 textwrap.dedent(source[start:end]), "<numeric DTO validator>", "exec"),
         namespace)
    return namespace["_validated_terminal_outcome"]


class LatencyValidationTests(unittest.TestCase):
    def setUp(self):
        self.validate = load_validator()

    def outcome(self, latency):
        return {"verified": True, "success": True, "latency_ms": latency}

    def test_nan_is_not_nonnegative(self):
        with self.assertRaisesRegex(ValueError, "non-negative"):
            self.validate(self.outcome(float("nan")))

    def test_nonnegative_values_preserved(self):
        for latency in (0, 0.0, 1, 12.5, float("inf")):
            with self.subTest(latency=latency):
                result = self.validate(self.outcome(latency))
                self.assertEqual(result, {"verified": True, "success": True,
                                          "latency_ms": float(latency)})

    def test_negative_values_rejected(self):
        for latency in (-1, -0.5, float("-inf")):
            with self.subTest(latency=latency):
                with self.assertRaisesRegex(ValueError, "non-negative"):
                    self.validate(self.outcome(latency))

    def test_optional_latency_omitted(self):
        expected = {"verified": True, "success": True}
        self.assertEqual(self.validate(expected), expected)
        self.assertEqual(self.validate(self.outcome(None)), expected)

    def test_nonnumeric_latency_rejected(self):
        for latency in (True, False, "1", []):
            with self.subTest(latency=latency):
                with self.assertRaisesRegex(ValueError, "numeric"):
                    self.validate(self.outcome(latency))


if __name__ == "__main__":
    unittest.main()
