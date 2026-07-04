import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import openclaw_loop as loop


class OpenClawLoopTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.old_home = os.environ.get("OPENCLAW_LOOP_HOME")
        os.environ["OPENCLAW_LOOP_HOME"] = str(self.root)
        loop.reset_clock_for_tests()

    def tearDown(self):
        if self.old_home is None:
            os.environ.pop("OPENCLAW_LOOP_HOME", None)
        else:
            os.environ["OPENCLAW_LOOP_HOME"] = self.old_home
        self.tmp.cleanup()

    def test_create_task_is_idempotent_for_active_dedupe_key(self):
        first = loop.create_task(title="deploy", objective="deploy once", source="user", dedupe_key="msg-1")
        second = loop.create_task(title="deploy duplicate", objective="deploy once", source="user", dedupe_key="msg-1")

        self.assertEqual(first["task_id"], second["task_id"])
        self.assertTrue(second["reused"])
        tasks = loop.load_tasks()
        self.assertEqual(len(tasks), 1)

    def test_heartbeat_lease_drives_stale_detection(self):
        task = loop.create_task(title="long run", objective="stay alive", source="user")
        loop.update_task(task["task_id"], status="running", current_step="benchmark")
        loop.record_heartbeat(task["task_id"], lease_seconds=1, progress="sample 10")
        self.assertEqual(loop.find_stale_tasks(now=loop.now_epoch() + 0.5), [])

        stale = loop.find_stale_tasks(now=loop.now_epoch() + 2)
        self.assertEqual([item["task_id"] for item in stale], [task["task_id"]])
        self.assertEqual(stale[0]["stale_reason"], "lease_expired")

    def test_config_drift_detects_gateway_token_mismatch(self):
        config = self.root / "openclaw.json"
        config.write_text(json.dumps({
            "gateway": {
                "auth": {"mode": "token", "token": "server-token"},
                "remote": {"url": "ws://127.0.0.1:18789", "token": "client-token"},
            }
        }), encoding="utf-8")

        result = loop.check_config_drift(config_path=config, run_live_checks=False)

        self.assertFalse(result["ok"])
        self.assertIn("gateway_token_mismatch", result["codes"])
        self.assertIn("gateway_remote_loopback", result["codes"])

    def test_smoke_creates_closed_loop_records(self):
        config = self.root / "openclaw.json"
        config.write_text(json.dumps({"gateway": {}}), encoding="utf-8")

        result = loop.run_smoke(config_path=config, run_live_checks=False)

        self.assertTrue(result["ok"])
        self.assertEqual(result["verifications"], 1)
        self.assertEqual(result["reports"], 1)
        task = loop.get_task(result["task_id"])
        self.assertEqual(task["status"], "done")
        self.assertTrue(task["evidence_refs"])

    def test_watch_creates_blocked_task_for_config_drift(self):
        config = self.root / "openclaw.json"
        config.write_text(json.dumps({
            "gateway": {
                "auth": {"mode": "token", "token": "server-token"},
                "remote": {"url": "ws://127.0.0.1:18789", "token": "client-token"},
            }
        }), encoding="utf-8")

        result = loop.run_watch(config_path=config, run_live_checks=False)

        self.assertFalse(result["ok"])
        self.assertEqual(result["tasks_created"], 1)
        task = loop.get_task(result["task_id"])
        self.assertEqual(task["status"], "blocked")
        self.assertIn("gateway_token_mismatch", task["result_summary"])
        self.assertEqual(len(loop.read_jsonl("watch.jsonl")), 1)

    def test_watch_reuses_existing_blocked_task_for_same_drift(self):
        config = self.root / "openclaw.json"
        config.write_text(json.dumps({
            "gateway": {
                "auth": {"mode": "token", "token": "server-token"},
                "remote": {"url": "ws://127.0.0.1:18789", "token": "client-token"},
            }
        }), encoding="utf-8")

        first = loop.run_watch(config_path=config, run_live_checks=False)
        second = loop.run_watch(config_path=config, run_live_checks=False)

        self.assertEqual(first["task_id"], second["task_id"])
        self.assertEqual(second["tasks_created"], 0)
        self.assertEqual(len(loop.load_tasks()), 1)

    def test_read_jsonl_skips_corrupt_lines_and_records_quarantine(self):
        path = loop.path_for("tasks.jsonl")
        path.write_text(
            '{"task_id":"ok-1","status":"planned"}\n'
            '{"task_id":\n'
            '{"task_id":"ok-2","status":"done"}\n',
            encoding="utf-8",
        )

        rows = loop.read_jsonl("tasks.jsonl")

        self.assertEqual([row["task_id"] for row in rows], ["ok-1", "ok-2"])
        corrupt = loop.read_jsonl("corrupt.jsonl")
        self.assertEqual(len(corrupt), 1)
        self.assertEqual(corrupt[0]["source_file"], "tasks.jsonl")

    def test_append_jsonl_uses_lock_file(self):
        loop.append_jsonl("tasks.jsonl", {"task_id": "locked", "status": "planned"})

        self.assertTrue(loop.path_for("tasks.jsonl.lock").exists())

    def test_done_requires_latest_verification_to_pass_unless_forced(self):
        task = loop.create_task(title="deploy", objective="deploy safely")
        loop.record_verification(task["task_id"], verifier="unit", checks={}, passed=False)

        with self.assertRaises(RuntimeError):
            loop.finish_task(task["task_id"], status="done", summary="done")

        forced = loop.finish_task(task["task_id"], status="done", summary="forced", force=True)
        self.assertEqual(forced["status"], "done")

    def test_report_policy_controls_internal_report_records(self):
        silent = loop.create_task(title="quiet", objective="do not report", report_policy="silent")
        loop.record_verification(silent["task_id"], verifier="unit", checks={}, passed=True)
        loop.finish_task(silent["task_id"], status="done", summary="quiet")
        self.assertEqual(loop.read_jsonl("reports.jsonl"), [])

        on_blocked = loop.create_task(title="blocked", objective="report only when blocked", report_policy="on_blocked")
        loop.finish_task(on_blocked["task_id"], status="blocked", summary="blocked")
        self.assertEqual(len(loop.read_jsonl("reports.jsonl")), 1)

    def test_doctor_cli_accepts_config_path(self):
        config = self.root / "openclaw.json"
        config.write_text(json.dumps({"gateway": {}}), encoding="utf-8")

        result = loop.main(["doctor", "--config", str(config), "--no-live"])

        self.assertEqual(result, 0)

    def test_watch_cli_accepts_config_path(self):
        config = self.root / "openclaw.json"
        config.write_text(json.dumps({"gateway": {}}), encoding="utf-8")

        result = loop.main(["watch", "--config", str(config), "--no-live"])

        self.assertEqual(result, 0)


if __name__ == "__main__":
    unittest.main()
