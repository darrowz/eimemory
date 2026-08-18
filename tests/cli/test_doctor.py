"""Smoke tests for ``eimemory.cli.doctor``.

These tests boot a fresh ``Runtime`` against a temp ``EIMEMORY_ROOT`` so
they never touch real production data. They cover the headline scenarios:

* empty root produces a coherent report without crashing;
* a synthetic "all empty {}" jsonl file is detected and fails the check;
* human rendering surfaces every check name and the overall verdict;
* ``--no-l5`` / ``--no-systemd`` flags skip the optional checks;
* on Windows the systemd probe is ``SKIP`` (rather than FAIL).
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Iterator
from unittest import mock


def _make_tmp_root() -> Path:
    """Create a fresh isolated EIMEMORY_ROOT under the system temp dir."""

    return Path(tempfile.mkdtemp(prefix="eimemory_doctor_test_"))


def _runtime_for(root: Path):
    """Build a Runtime rooted at ``root`` (monkeypatches the env)."""

    # ``Runtime.create`` reads ``EIMEMORY_ROOT`` via ``default_root``.
    return _runtime_for.__wrapped__(root) if hasattr(_runtime_for, "__wrapped__") else _boot(root)


def _boot(root: Path):
    from eimemory.api.runtime import Runtime

    return Runtime.create(root=root)


class _IsolatedRuntime(unittest.TestCase):
    """Test base that creates + cleans an isolated EIMEMORY_ROOT."""

    tmp_root: Path

    def setUp(self) -> None:
        self.tmp_root = _make_tmp_root()
        self._prev_root = os.environ.get("EIMEMORY_ROOT")
        os.environ["EIMEMORY_ROOT"] = str(self.tmp_root)

    def tearDown(self) -> None:
        # Restore env so other tests see a clean slate
        if self._prev_root is None:
            os.environ.pop("EIMEMORY_ROOT", None)
        else:
            os.environ["EIMEMORY_ROOT"] = self._prev_root
        shutil.rmtree(self.tmp_root, ignore_errors=True)

    def _runtime(self):
        return _boot(self.tmp_root)


class TestDoctorSmokeEmpty(_IsolatedRuntime):
    """An empty root must produce a structured report without crashing."""

    def test_run_doctor_on_empty_root(self) -> None:
        from eimemory.cli.doctor import run_doctor

        runtime = self._runtime()
        try:
            report = run_doctor(runtime, scope={}, include_l5=False, include_systemd=False)
        finally:
            runtime.close()

        self.assertEqual(report["report_type"], "doctor_report")
        self.assertEqual(report["schema_version"], "doctor.v1")
        self.assertIn(report["overall_status"], {"HEALTHY", "DEGRADED", "UNKNOWN"})
        # Required checks are present
        self.assertIn("sqlite_integrity", report["checks"])
        self.assertIn("storage_disk", report["checks"])
        self.assertIn("jsonl_health", report["checks"])
        self.assertIn("record_sampling", report["checks"])
        # l5/systemd were disabled
        self.assertNotIn("l5_readiness", report["checks"])
        self.assertNotIn("systemd_services", report["checks"])
        # ``ok`` is a boolean
        self.assertIsInstance(report["ok"], bool)
        # recommendations is a list
        self.assertIsInstance(report["recommendations"], list)


class TestDoctorDetectsEmptyDictBug(_IsolatedRuntime):
    """The events.jsonl "all empty {}" honxin bug must FAIL the check."""

    def test_events_jsonl_all_empty_dicts_is_flagged(self) -> None:
        # Write a synthetic events.jsonl that mirrors the honxin regression:
        # many lines, every one is exactly ``{}``.
        state_dir = self.tmp_root / "state"
        state_dir.mkdir(parents=True, exist_ok=True)
        events = state_dir / "events.jsonl"
        with events.open("w", encoding="utf-8") as handle:
            for _ in range(50):
                handle.write("{}\n")

        from eimemory.cli.doctor import check_jsonl_health

        runtime = self._runtime()
        try:
            result = check_jsonl_health(runtime)
        finally:
            runtime.close()

        self.assertEqual(result.status, "FAIL", msg=result.details)
        self.assertIn("events.jsonl", result.details)
        self.assertIn("empty", result.details.lower())
        # The metrics record the per-stream breakdown for audit
        streams = result.metrics["streams"]
        self.assertEqual(streams["events"]["empty_dict_count"], 50)
        self.assertEqual(streams["events"]["non_empty_count"], 0)


class TestDoctorStorageDisk(_IsolatedRuntime):
    """Storage check should surface jsonl size and release-snapshots count."""

    def test_storage_disk_reports_records_jsonl(self) -> None:
        # Drop a small records.jsonl so the storage check has something to report
        records = self.tmp_root / "records.jsonl"
        with records.open("w", encoding="utf-8") as handle:
            for i in range(5):
                handle.write(json.dumps({"record_id": f"r{i}", "kind": "demo"}) + "\n")
        # And a couple of release-snapshot subdirectories
        snap_root = self.tmp_root / "state" / "release-snapshots"
        snap_root.mkdir(parents=True, exist_ok=True)
        for i in range(3):
            (snap_root / f"snap-{i}").mkdir()
            (snap_root / f"snap-{i}" / "manifest.json").write_text("{}")

        from eimemory.cli.doctor import check_storage_disk

        runtime = self._runtime()
        try:
            result = check_storage_disk(runtime)
        finally:
            runtime.close()

        self.assertIn(result.status, {"PASS", "WARN", "FAIL"})
        self.assertGreaterEqual(result.metrics["records_jsonl"]["active_bytes"], 5)
        self.assertEqual(result.metrics["release_snapshots"]["count"], 3)


class TestDoctorHumanRendering(unittest.TestCase):
    """The human block must surface every check by name and the overall verdict."""

    def test_render_human_includes_all_check_names(self) -> None:
        from eimemory.cli.doctor import CheckResult, render_human

        report = {
            "overall_status": "DEGRADED",
            "checks": {
                "sqlite_integrity": CheckResult("PASS", "ok").to_dict(),
                "storage_disk": CheckResult("WARN", "disk 86% used").to_dict(),
                "jsonl_health": CheckResult("FAIL", "events.jsonl 100% empty").to_dict(),
                "record_sampling": CheckResult("PASS", "sampled 3 records").to_dict(),
                "l5_readiness": CheckResult("WARN", "current=L3.5").to_dict(),
            },
            "recommendations": ["[jsonl_health] investigate events.jsonl writer"],
        }
        text = render_human(report)
        self.assertIn("eimemory doctor", text)
        self.assertIn("DEGRADED", text)
        for name in (
            "sqlite_integrity",
            "storage_disk",
            "jsonl_health",
            "record_sampling",
            "l5_readiness",
        ):
            self.assertIn(name, text)
        # Recommendations block is present
        self.assertIn("Recommendations", text)
        # Glyphs (rough: each check line should have at least one emoji codepoint)
        # We don't lock the exact glyph; we just confirm there's at least one
        # non-ASCII character that is not a regular letter.
        self.assertTrue(any(ord(c) > 127 for c in text))


class TestDoctorOptionalFlags(_IsolatedRuntime):
    """--no-l5 and --no-systemd must omit the corresponding check."""

    def test_no_l5_skips_l5_readiness(self) -> None:
        from eimemory.cli.doctor import run_doctor

        runtime = self._runtime()
        try:
            report = run_doctor(
                runtime,
                scope={},
                include_l5=False,
                include_systemd=False,
            )
        finally:
            runtime.close()
        self.assertNotIn("l5_readiness", report["checks"])
        self.assertNotIn("systemd_services", report["checks"])

    def test_with_l5_includes_l5_readiness(self) -> None:
        from eimemory.cli.doctor import run_doctor

        runtime = self._runtime()
        try:
            report = run_doctor(
                runtime,
                scope={},
                include_l5=True,
                include_systemd=False,
            )
        finally:
            runtime.close()
        self.assertIn("l5_readiness", report["checks"])


class TestDoctorSystemdOnWindows(_IsolatedRuntime):
    """On Windows the systemd probe must report SKIP, never FAIL."""

    def test_systemd_skipped_on_windows(self) -> None:
        from eimemory.cli.doctor import check_systemd_services

        runtime = self._runtime()
        try:
            with mock.patch.object(sys, "platform", "win32"):
                result = check_systemd_services(runtime, scope={})
        finally:
            runtime.close()
        self.assertEqual(result.status, "SKIP")
        self.assertIn("linux", result.details.lower())


class TestDoctorOverallStatusLogic(unittest.TestCase):
    """The overall status must propagate FAIL / WARN correctly."""

    def test_one_fail_makes_unhealthy(self) -> None:
        from eimemory.cli.doctor import CheckResult, _overall_status

        checks = {
            "sqlite_integrity": CheckResult("PASS", "ok"),
            "jsonl_health": CheckResult("FAIL", "all empty"),
        }
        self.assertEqual(_overall_status(checks), "UNHEALTHY")

    def test_one_warn_makes_degraded(self) -> None:
        from eimemory.cli.doctor import CheckResult, _overall_status

        checks = {
            "sqlite_integrity": CheckResult("PASS", "ok"),
            "l5_readiness": CheckResult("WARN", "held"),
        }
        self.assertEqual(_overall_status(checks), "DEGRADED")

    def test_all_pass_is_healthy(self) -> None:
        from eimemory.cli.doctor import CheckResult, _overall_status

        checks = {
            "sqlite_integrity": CheckResult("PASS", "ok"),
            "storage_disk": CheckResult("PASS", "ok"),
        }
        self.assertEqual(_overall_status(checks), "HEALTHY")

    def test_all_skip_is_unknown(self) -> None:
        from eimemory.cli.doctor import CheckResult, _overall_status

        checks = {
            "a": CheckResult("SKIP", "n/a"),
            "b": CheckResult("SKIP", "n/a"),
        }
        self.assertEqual(_overall_status(checks), "UNKNOWN")


class TestDoctorJsonlHelpers(unittest.TestCase):
    """The low-level JSONL helpers must work for missing/blank files."""

    def test_count_lines_missing(self) -> None:
        from eimemory.cli.doctor import _count_lines

        self.assertEqual(_count_lines(Path("/this/does/not/exist.jsonl")), 0)

    def test_head_parse_missing(self) -> None:
        from eimemory.cli.doctor import _head_parse_check

        info = _head_parse_check(Path("/this/does/not/exist.jsonl"))
        self.assertFalse(info["exists"])
        self.assertFalse(info["head_parse_ok"])
        self.assertEqual(info["first_error"], "file missing")

    def test_head_parse_blank_file(self) -> None:
        from eimemory.cli.doctor import _head_parse_check

        with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False) as f:
            f.write("\n\n\n")
            path = Path(f.name)
        try:
            info = _head_parse_check(path)
        finally:
            path.unlink()
        self.assertTrue(info["exists"])
        self.assertEqual(info["lines"], 3)
        self.assertEqual(info["blank_count"], 3)
        self.assertEqual(info["sample_lines"], 0)
        self.assertTrue(info["head_parse_ok"])

    def test_head_parse_mixed(self) -> None:
        from eimemory.cli.doctor import _head_parse_check

        with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False) as f:
            f.write(json.dumps({"a": 1}) + "\n")
            f.write("{}\n")  # empty dict
            f.write("not-json\n")  # broken
            f.write("\n")
            f.write(json.dumps([1, 2, 3]) + "\n")
            path = Path(f.name)
        try:
            info = _head_parse_check(path)
        finally:
            path.unlink()
        self.assertEqual(info["lines"], 5)
        self.assertEqual(info["non_empty_count"], 2)
        self.assertEqual(info["empty_dict_count"], 1)
        self.assertEqual(info["blank_count"], 1)
        self.assertFalse(info["head_parse_ok"])
        self.assertIsNotNone(info["first_error"])


if __name__ == "__main__":
    unittest.main()
