from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
import time

import pytest

from eimemory.ops import openclaw_loop as loop
from eimemory.ops import timer_monitor
from eimemory.cli import doctor


@pytest.fixture
def task(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENCLAW_LOOP_HOME", str(tmp_path))
    return loop.create_task(title="audit", objective="test", report_policy="silent")


def test_acceptance_must_follow_latest_action(task):
    loop.record_action(task["task_id"], action_type="deploy", command_or_tool="first")
    loop.record_verification(task["task_id"], verifier="acceptance", checks={}, passed=True)
    loop.record_action(task["task_id"], action_type="deploy", command_or_tool="second", result="failed", exit_code=1)
    with pytest.raises(RuntimeError, match="verification"):
        loop.finish_task(task["task_id"])


def test_force_completion_requires_recorded_reason(task):
    with pytest.raises(ValueError, match="reason"):
        loop.finish_task(task["task_id"], force=True)
    result = loop.finish_task(task["task_id"], force=True, force_reason="operator accepted missing probe")
    assert result["completion_override"]["reason"] == "operator accepted missing probe"


def test_terminal_tasks_require_explicit_reopen_and_current_lease(task):
    tid = task["task_id"]
    loop.record_verification(tid, verifier="acceptance", checks={}, passed=True)
    loop.finish_task(tid)
    with pytest.raises(RuntimeError, match="terminal"):
        loop.record_heartbeat(tid)
    with pytest.raises(RuntimeError, match="terminal"):
        loop.update_task(tid, status="running")
    reopened = loop.reopen_task(tid, owner="main", generation=0, reason="new attempt")
    with pytest.raises(RuntimeError, match="generation"):
        loop.record_heartbeat(tid, owner="main", generation=0)
    with pytest.raises(RuntimeError, match="owner"):
        loop.record_heartbeat(tid, owner="other", generation=reopened["generation"])
    loop.record_heartbeat(tid, owner="main", generation=reopened["generation"])
    with pytest.raises(RuntimeError, match="verification"):
        loop.finish_task(tid)


def test_concurrent_task_creation_deduplicates(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENCLAW_LOOP_HOME", str(tmp_path))
    original = loop.load_tasks
    def slow_read():
        rows = original()
        time.sleep(0.02)
        return rows
    monkeypatch.setattr(loop, "load_tasks", slow_read)
    with ThreadPoolExecutor(max_workers=12) as pool:
        results = list(pool.map(lambda _: loop.create_task(title="race", objective="test", dedupe_key="same"), range(36)))
    assert len({result["task_id"] for result in results}) == 1
    assert len(loop.load_tasks()) == 1


@pytest.mark.parametrize("state", ["failed", "inactive", "masked", "not-found", "disabled"])
def test_doctor_reports_unhealthy_timer_fields(monkeypatch, state):
    monkeypatch.setattr(doctor.sys, "platform", "linux")
    monkeypatch.setattr(timer_monitor, "check_user_systemd_timers", lambda *a, **k: {
        "ok": False, "issues": [{"unit": "test.timer", "reason": state}],
        "units": [{"unit": "test.timer", "active_state": state, "load_state": "loaded"}],
    })
    assert doctor.check_systemd_services(SimpleNamespace(), {}).status == doctor.FAIL


def test_timer_diagnostic_never_notifies(monkeypatch):
    calls = []
    monkeypatch.setenv("EIMEMORY_ALERT_WEBHOOK", "https://example.invalid/hook")
    monkeypatch.setattr(timer_monitor, "_post_feishu_webhook", lambda *a: calls.append(a))
    report = timer_monitor.check_user_systemd_timers(SimpleNamespace(), unit_states=[{
        "unit": "test.timer", "active_state": "failed", "load_state": "loaded",
    }], persist=False, notify=False, notifier=lambda p: calls.append(p))
    assert not report["ok"]
    assert calls == []


def test_doctor_disables_timer_notification(monkeypatch):
    monkeypatch.setattr(doctor.sys, "platform", "linux")
    kwargs = {}
    def monitor(*a, **k):
        kwargs.update(k)
        return {"ok": True, "units": []}
    monkeypatch.setattr(timer_monitor, "check_user_systemd_timers", monitor)
    doctor.check_systemd_services(SimpleNamespace(), {})
    assert kwargs.get("notify") is False


def test_doctor_owner_unsupported_platform_skips(monkeypatch):
    monkeypatch.setattr(doctor.sys, "platform", "win32")
    assert doctor.check_code_implementation_owner(SimpleNamespace()).status == doctor.SKIP


def test_doctor_detects_filtered_corrupt_record(tmp_path):
    from eimemory.api.runtime import Runtime
    from eimemory.models.records import RecordEnvelope, ScopeRef
    runtime = Runtime.create(root=tmp_path)
    try:
        record = RecordEnvelope.create(kind="incident", title="bad", source="test", scope=ScopeRef())
        runtime.store.append(record)
        runtime.store.sqlite.conn.execute("UPDATE records SET payload_json = '{}', payload_pointer_json = '{}'")
        runtime.store.sqlite.conn.commit()
        result = doctor.check_record_sampling(runtime, {})
        assert result.status == doctor.FAIL
        assert record.record_id in str(result.metrics)
    finally:
        runtime.close()


def test_doctor_accepts_healthy_inactive_oneshot_service(monkeypatch):
    monkeypatch.setattr(doctor.sys, "platform", "linux")
    monkeypatch.setattr(timer_monitor, "check_user_systemd_timers", lambda *a, **k: {
        "ok": True, "issues": [], "units": [
            {"unit": "test.timer", "active_state": "active", "load_state": "loaded"},
            {"unit": "test.service", "active_state": "inactive", "load_state": "loaded", "result": "success"},
        ],
    })
    assert doctor.check_systemd_services(SimpleNamespace(), {}).status == doctor.PASS


def test_active_but_disabled_timer_is_unhealthy():
    report = timer_monitor.check_user_systemd_timers(SimpleNamespace(), unit_states=[{
        "unit": "test.timer", "active_state": "active", "load_state": "loaded", "unit_file_state": "disabled",
    }], persist=False, notify=False)
    assert not report["ok"]
    assert report["issues"][0]["reason"] == "disabled"


def test_explicit_notifier_does_not_also_send_webhook(monkeypatch):
    calls = []
    monkeypatch.setenv("EIMEMORY_ALERT_WEBHOOK", "https://example.invalid/hook")
    monkeypatch.setattr(timer_monitor, "_post_feishu_webhook", lambda *a: calls.append("webhook"))
    timer_monitor.check_user_systemd_timers(SimpleNamespace(), unit_states=[{
        "unit": "test.timer", "active_state": "failed", "load_state": "loaded",
    }], persist=False, notifier=lambda p: calls.append("notifier"))
    assert calls == ["notifier"]


def test_owner_module_import_does_not_require_fcntl(monkeypatch):
    import builtins
    import runpy
    from eimemory.ops import code_implementation_owner
    original_import = builtins.__import__
    def unavailable(name, *args, **kwargs):
        if name == "fcntl":
            raise ImportError("unavailable on Windows")
        return original_import(name, *args, **kwargs)
    monkeypatch.setattr(builtins, "__import__", unavailable)
    runpy.run_path(code_implementation_owner.__file__)


def test_existing_lock_byte_is_not_read_before_acquisition(task, monkeypatch):
    from pathlib import Path
    original_open = Path.open
    class LockHandle:
        def __init__(self, handle):
            self.handle = handle
        def __getattr__(self, name):
            return getattr(self.handle, name)
        def __enter__(self):
            return self
        def __exit__(self, *args):
            self.handle.close()
        def read(self, *args):
            raise AssertionError("mandatory locked byte cannot be read before acquisition")
    def guarded_open(path, mode="r", *args, **kwargs):
        handle = original_open(path, mode, *args, **kwargs)
        return LockHandle(handle) if path.name.endswith(".lock") and mode == "r+b" else handle
    monkeypatch.setattr(Path, "open", guarded_open)
    loop.record_heartbeat(task["task_id"])


def test_generic_update_cannot_bypass_acceptance_or_generation(task):
    with pytest.raises(RuntimeError, match="finish_task"):
        loop.update_task(task["task_id"], status="done")
    with pytest.raises(ValueError, match="generation"):
        loop.update_task(task["task_id"], generation=4)


def test_compaction_takes_task_lock_before_action_lock(task, monkeypatch):
    from contextlib import contextmanager
    loop.record_action(task["task_id"], action_type="deploy", command_or_tool="first")
    acquired = []
    original = loop._append_lock
    @contextmanager
    def recording(name):
        acquired.append(name)
        with original(name):
            yield
    monkeypatch.setattr(loop, "_append_lock", recording)
    loop.compact_ledgers()
    assert acquired[0] == "tasks.jsonl"


def test_cli_reopen_and_current_generation_heartbeat(task, capsys):
    tid = task["task_id"]
    loop.finish_task(tid, status="failed")
    assert loop.main(["reopen", tid, "--owner", "main", "--generation", "0", "--reason", "retry"]) == 0
    assert loop.main(["heartbeat", tid, "--owner", "main", "--generation", "1"]) == 0
