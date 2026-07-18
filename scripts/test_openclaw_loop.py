import json
import os
import sys
import tempfile
import time
import unittest
import gzip
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import openclaw_loop as loop
from eimemory.ops import openclaw_loop as loop_impl


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

    def test_config_drift_defers_structured_secret_refs_to_runtime_validation(self):
        config = self.root / "openclaw.json"
        config.write_text(
            json.dumps(
                {
                    "gateway": {
                        "auth": {"mode": "token", "token": {"source": "env", "id": "GATEWAY_AUTH_TOKEN"}},
                        "remote": {
                            "url": "ws://100.105.189.120:18789",
                            "token": {"source": "env", "id": "GATEWAY_REMOTE_TOKEN"},
                        },
                    }
                }
            ),
            encoding="utf-8",
        )

        result = loop.check_config_drift(config_path=config, run_live_checks=False)

        self.assertTrue(result["ok"])
        self.assertNotIn("gateway_token_mismatch", result["codes"])

    def test_config_drift_requires_loopback_gateway_health(self):
        config = self.root / "openclaw.json"
        config.write_text(json.dumps({"gateway": {}}), encoding="utf-8")

        def fake_http_json(url, timeout=3.0):
            if "127.0.0.1:18789" in url:
                raise TimeoutError("loopback gateway timeout")
            return {"ok": True}

        old_http_json = loop_impl._http_json
        old_proxy_state = loop_impl.check_openclaw_loopback_proxy_user_service
        loop_impl._http_json = fake_http_json
        loop_impl.check_openclaw_loopback_proxy_user_service = lambda: {"ok": True}
        try:
            result = loop.check_config_drift(config_path=config, run_live_checks=True)
        finally:
            loop_impl._http_json = old_http_json
            loop_impl.check_openclaw_loopback_proxy_user_service = old_proxy_state

        self.assertFalse(result["ok"])
        self.assertIn("openclaw_loopback_health_failed", result["codes"])

    def test_config_drift_requires_loopback_proxy_user_service(self):
        config = self.root / "openclaw.json"
        config.write_text(json.dumps({"gateway": {}}), encoding="utf-8")

        old_http_json = loop_impl._http_json
        old_proxy_state = loop_impl.check_openclaw_loopback_proxy_user_service
        loop_impl._http_json = lambda url, timeout=3.0: {"ok": True}
        loop_impl.check_openclaw_loopback_proxy_user_service = lambda: {
            "ok": False,
            "reason": "openclaw_loopback_proxy_inactive",
            "active": "inactive",
            "enabled": "enabled",
        }
        try:
            result = loop.check_config_drift(config_path=config, run_live_checks=True)
        finally:
            loop_impl._http_json = old_http_json
            loop_impl.check_openclaw_loopback_proxy_user_service = old_proxy_state

        self.assertFalse(result["ok"])
        self.assertIn("openclaw_loopback_proxy_inactive", result["codes"])

    def test_config_drift_requires_loopback_proxy_user_service_enabled(self):
        config = self.root / "openclaw.json"
        config.write_text(json.dumps({"gateway": {}}), encoding="utf-8")

        old_http_json = loop_impl._http_json
        old_proxy_state = loop_impl.check_openclaw_loopback_proxy_user_service
        loop_impl._http_json = lambda url, timeout=3.0: {"ok": True}
        loop_impl.check_openclaw_loopback_proxy_user_service = lambda: {
            "ok": False,
            "reason": "openclaw_loopback_proxy_not_enabled",
            "active": "active",
            "enabled": "disabled",
        }
        try:
            result = loop.check_config_drift(config_path=config, run_live_checks=True)
        finally:
            loop_impl._http_json = old_http_json
            loop_impl.check_openclaw_loopback_proxy_user_service = old_proxy_state

        self.assertFalse(result["ok"])
        self.assertIn("openclaw_loopback_proxy_not_enabled", result["codes"])

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

    def test_watch_records_bounded_stale_summary_instead_of_full_tasks(self):
        config = self.root / "openclaw.json"
        config.write_text(json.dumps({"gateway": {}}), encoding="utf-8")
        for index in range(30):
            task = loop.create_task(title=f"stale-{index}", objective="expire")
            loop.record_heartbeat(task["task_id"], lease_seconds=1, progress="started")
        loop_impl._TEST_NOW = loop.now_epoch() + 2

        result = loop.run_watch(config_path=config, run_live_checks=False)

        self.assertFalse(result["ok"])
        self.assertEqual(result["stale_count"], 30)
        self.assertEqual(result["codes"], ["stale:lease_expired"])
        watch = loop.read_jsonl("watch.jsonl")[-1]
        self.assertEqual(watch["stale_count"], 30)
        self.assertEqual(watch["stale_reason_counts"], {"lease_expired": 30})
        self.assertLessEqual(len(watch["stale_task_ids"]), 20)
        verification = loop.read_jsonl("verifications.jsonl")[-1]
        self.assertNotIn("stale_tasks", verification["checks"])
        self.assertEqual(verification["checks"]["stale_summary"]["count"], 30)
        lesson = loop.read_jsonl("lesson_candidates.jsonl")[-1]
        self.assertLess(len(json.dumps(lesson)), 16_384)

    def test_reconcile_stale_is_dry_run_by_default_and_apply_closes_tasks(self):
        task_ids = []
        for index in range(2):
            task = loop.create_task(title=f"stale-{index}", objective="expire")
            task_ids.append(task["task_id"])
            loop.record_heartbeat(task["task_id"], lease_seconds=1, progress="started")
        loop_impl._TEST_NOW = loop.now_epoch() + 2

        preview = loop.reconcile_stale_tasks(apply=False)

        self.assertFalse(preview["applied"])
        self.assertEqual(preview["stale_count"], 2)
        self.assertTrue(all(loop.get_task(task_id)["status"] == "running" for task_id in task_ids))

        applied = loop.reconcile_stale_tasks(apply=True)

        self.assertTrue(applied["applied"])
        self.assertEqual(applied["reconciled_count"], 2)
        self.assertEqual(loop.find_stale_tasks(), [])
        for task_id in task_ids:
            task = loop.get_task(task_id)
            self.assertEqual(task["status"], "failed")
            self.assertEqual(task["failure_class"], "lease_expired_reconciled")

    def test_compact_ledgers_archives_original_and_bounds_oversized_checks(self):
        task = loop.create_task(title="large", objective="compact")
        loop.update_task(task["task_id"], status="running")
        loop.append_jsonl(
            "verifications.jsonl",
            {
                "verification_id": "legacy-large",
                "task_id": task["task_id"],
                "verifier": "watch",
                "checks": {"stale_tasks": [{"task_id": f"task-{index}", "payload": "x" * 2000} for index in range(40)]},
                "passed": False,
                "failure_reason": "stale," * 20_000,
                "created_at": loop.iso_ts(),
            },
        )
        archive_dir = self.root / "archives"

        result = loop.compact_ledgers(archive_dir=archive_dir)

        self.assertTrue(result["ok"])
        archive = Path(result["archive_path"])
        self.assertTrue(archive.exists())
        self.assertEqual(archive.suffix, ".gz")
        with gzip.open(archive, "rt", encoding="utf-8") as handle:
            self.assertIn('"stale_tasks"', handle.read())
        self.assertEqual(len(loop.read_jsonl("tasks.jsonl")), 1)
        compacted = loop.read_jsonl("verifications.jsonl")[-1]
        self.assertNotIn("stale_tasks", compacted["checks"])
        self.assertLess(len(json.dumps(compacted)), 16_384)

    def test_compact_ledgers_cold_archives_expired_terminal_tasks(self):
        now = 1_800_000_000.0
        loop_impl._TEST_NOW = now
        old_terminal = {
            "task_id": "task-old-done",
            "status": "done",
            "started_at": loop.iso_ts(now - 20 * 86400),
            "updated_at": loop.iso_ts(now - 8 * 86400),
        }
        recent_terminal = {
            "task_id": "task-recent-done",
            "status": "done",
            "started_at": loop.iso_ts(now - 2 * 86400),
            "updated_at": loop.iso_ts(now - 86400),
        }
        old_active = {
            "task_id": "task-old-running",
            "status": "running",
            "started_at": loop.iso_ts(now - 20 * 86400),
            "updated_at": loop.iso_ts(now - 8 * 86400),
        }
        for task in (old_terminal, recent_terminal, old_active):
            loop.append_jsonl("tasks.jsonl", task)

        result = loop.compact_ledgers(
            archive_dir=self.root / "archives",
            terminal_retention_days=7,
        )

        retained_ids = {task["task_id"] for task in loop.load_tasks()}
        self.assertEqual(retained_ids, {"task-recent-done", "task-old-running"})
        self.assertEqual(result["terminal_tasks_archived"], 1)
        with gzip.open(result["archive_path"], "rt", encoding="utf-8") as handle:
            self.assertIn("task-old-done", handle.read())

    def test_compact_ledgers_streams_non_task_files_without_read_jsonl(self):
        loop.append_jsonl(
            "verifications.jsonl",
            {
                "verification_id": "legacy-stream",
                "task_id": "task-stream",
                "checks": {"stale_tasks": [{"task_id": "stale"}]},
            },
        )
        original_read_jsonl = loop_impl.read_jsonl

        def guarded_read_jsonl(name):
            if name != "tasks.jsonl":
                raise AssertionError(f"non-task ledger loaded eagerly: {name}")
            return original_read_jsonl(name)

        loop_impl.read_jsonl = guarded_read_jsonl
        try:
            result = loop.compact_ledgers(archive_dir=self.root / "archives")
        finally:
            loop_impl.read_jsonl = original_read_jsonl

        self.assertTrue(result["ok"])
        self.assertEqual(result["rows_after"]["verifications.jsonl"], 1)

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

    def test_report_policy_delivers_to_configured_outbox(self):
        outbox = self.root / "report-outbox.jsonl"
        old_outbox = os.environ.get("OPENCLAW_LOOP_REPORT_OUTBOX")
        os.environ["OPENCLAW_LOOP_REPORT_OUTBOX"] = str(outbox)
        try:
            task = loop.create_task(title="report", objective="send report", report_policy="always")
            loop.record_verification(task["task_id"], verifier="unit", checks={}, passed=True)
            loop.finish_task(task["task_id"], status="done", summary="sent")
        finally:
            if old_outbox is None:
                os.environ.pop("OPENCLAW_LOOP_REPORT_OUTBOX", None)
            else:
                os.environ["OPENCLAW_LOOP_REPORT_OUTBOX"] = old_outbox

        delivered = [json.loads(line) for line in outbox.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(delivered[0]["channel"], "feishu")
        self.assertEqual(delivered[0]["summary"], "sent")
        report = loop.read_jsonl("reports.jsonl")[0]
        self.assertTrue(report["delivery"]["delivered"])

    def test_doctor_cli_accepts_config_path(self):
        config = self.root / "openclaw.json"
        config.write_text(json.dumps({"gateway": {}}), encoding="utf-8")

        result = loop.main(["doctor", "--config", str(config), "--no-live"])

        self.assertEqual(result, 0)

    def test_doctor_cli_returns_failure_when_live_gate_fails(self):
        config = self.root / "openclaw.json"
        config.write_text(json.dumps({"gateway": {}}), encoding="utf-8")

        old_http_json = loop_impl._http_json
        old_proxy_state = loop_impl.check_openclaw_loopback_proxy_user_service
        loop_impl._http_json = lambda url, timeout=3.0: {"ok": True}
        loop_impl.check_openclaw_loopback_proxy_user_service = lambda: {
            "ok": False,
            "reason": "openclaw_loopback_proxy_inactive",
            "active": "inactive",
            "enabled": "enabled",
        }
        try:
            result = loop.main(["doctor", "--config", str(config)])
        finally:
            loop_impl._http_json = old_http_json
            loop_impl.check_openclaw_loopback_proxy_user_service = old_proxy_state

        self.assertEqual(result, 2)

    def test_watch_cli_accepts_config_path(self):
        config = self.root / "openclaw.json"
        config.write_text(json.dumps({"gateway": {}}), encoding="utf-8")

        result = loop.main(["watch", "--config", str(config), "--no-live"])

        self.assertEqual(result, 0)

    def test_failed_verification_records_lesson_candidate(self):
        task = loop.create_task(title="deploy", objective="deploy safely", source="system")

        verification = loop.record_verification(
            task["task_id"],
            verifier="deploy_smoke",
            checks={"health": "timeout"},
            passed=False,
            failure_reason="health timeout",
            next_action="repair",
        )

        lessons = loop.read_jsonl("lesson_candidates.jsonl")
        self.assertEqual(len(lessons), 1)
        self.assertEqual(lessons[0]["task_id"], task["task_id"])
        self.assertEqual(lessons[0]["verification_id"], verification["verification_id"])
        self.assertEqual(lessons[0]["failure_reason"], "health timeout")
        self.assertEqual(lessons[0]["source"], "openclaw_loop.verification_failed")

    def test_record_dispatch_heartbeats_and_actions_background_work(self):
        task = loop.create_task(title="cron", objective="nightly run", source="cron")

        dispatch = loop.record_dispatch(
            task["task_id"],
            dispatch_type="cron",
            command_or_tool="eimemory nightly",
            lease_seconds=900,
            progress="nightly started",
        )

        latest = loop.get_task(task["task_id"])
        self.assertEqual(dispatch["action"]["action_type"], "dispatch")
        self.assertEqual(dispatch["heartbeat"]["heartbeat_source"], "cron")
        self.assertEqual(latest["status"], "running")
        self.assertEqual(latest["current_step"], "acting")

    def test_dispatch_cli_does_not_clobber_subcommand_name_with_cmd_option(self):
        task = loop.create_task(title="cron", objective="nightly run", source="cron")

        result = loop.main([
            "dispatch",
            task["task_id"],
            "--type",
            "cron",
            "--cmd",
            "eimemory-nightly",
            "--progress",
            "nightly-started",
        ])

        self.assertEqual(result, 0)
        self.assertEqual(loop.read_jsonl("actions.jsonl")[0]["command_or_tool"], "eimemory-nightly")

    def test_deploy_verify_creates_verification_evidence_for_release(self):
        config = self.root / "openclaw.json"
        config.write_text(json.dumps({"gateway": {}}), encoding="utf-8")

        result = loop.run_deploy_verify(
            commit="abc1234",
            release_path="/opt/eimemory/releases/abc1234",
            config_path=config,
            run_live_checks=False,
        )

        self.assertTrue(result["ok"])
        task = loop.get_task(result["task_id"])
        self.assertEqual(task["status"], "done")
        self.assertIn("deploy:abc1234", task["dedupe_key"])
        self.assertEqual(loop.read_jsonl("actions.jsonl")[-1]["action_type"], "dispatch")
        self.assertTrue(loop.read_jsonl("verifications.jsonl")[-1]["passed"])

    def test_deploy_verify_records_rpc_user_systemd_owner_check(self):
        config = self.root / "openclaw.json"
        config.write_text(json.dumps({"gateway": {}}), encoding="utf-8")

        result = loop.run_deploy_verify(
            commit="abc1234",
            release_path="/opt/eimemory/releases/abc1234",
            config_path=config,
            run_live_checks=False,
            service_owner_checker=lambda: {
                "ok": True,
                "system_owner_active": "inactive",
                "system_owner_enabled": "not-found",
                "user_owner_active": "active",
                "user_owner_enabled": "enabled",
            },
        )

        self.assertTrue(result["ok"])
        checks = loop.read_jsonl("verifications.jsonl")[-1]["checks"]
        self.assertEqual(checks["rpc_service_owner"]["user_owner_active"], "active")
        self.assertEqual(checks["rpc_service_owner"]["user_owner_enabled"], "enabled")

    def test_deploy_verify_blocks_when_rpc_owner_check_fails(self):
        config = self.root / "openclaw.json"
        config.write_text(json.dumps({"gateway": {}}), encoding="utf-8")

        result = loop.run_deploy_verify(
            commit="abc1234",
            release_path="/opt/eimemory/releases/abc1234",
            config_path=config,
            run_live_checks=False,
            service_owner_checker=lambda: {
                "ok": False,
                "reason": "user_rpc_service_not_enabled",
                "system_owner_active": "inactive",
                "system_owner_enabled": "not-found",
                "user_owner_active": "active",
                "user_owner_enabled": "disabled",
            },
        )

        self.assertFalse(result["ok"])
        task = loop.get_task(result["task_id"])
        self.assertEqual(task["status"], "blocked")
        verification = loop.read_jsonl("verifications.jsonl")[-1]
        self.assertFalse(verification["passed"])
        self.assertIn("user_rpc_service_not_enabled", verification["failure_reason"])


if __name__ == "__main__":
    unittest.main()
