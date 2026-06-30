from __future__ import annotations

from eimemory.api.runtime import Runtime
from eimemory.ops.timer_monitor import check_user_systemd_timers


SCOPE = {"agent_id": "ops", "workspace_id": "honxin", "user_id": "darrow"}


def test_timer_monitor_alerts_masked_stale_and_failed_user_units(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    sent: list[dict] = []
    states = [
        {
            "unit": "eimemory-learn-watch.timer",
            "load_state": "masked",
            "active_state": "inactive",
            "last_trigger_at": "2026-06-30T08:00:00+00:00",
        },
        {
            "unit": "eimemory-learn-think.timer",
            "load_state": "loaded",
            "active_state": "active",
            "last_trigger_at": "2026-06-30T08:00:00+00:00",
        },
        {
            "unit": "eimemory-nightly.service",
            "load_state": "loaded",
            "active_state": "failed",
        },
    ]

    report = check_user_systemd_timers(
        runtime,
        scope=SCOPE,
        unit_states=states,
        now="2026-06-30T10:00:00+00:00",
        stale_after_minutes=90,
        notifier=sent.append,
        persist=True,
    )

    assert report["ok"] is False
    assert {issue["reason"] for issue in report["issues"]} == {"masked", "stale", "failed"}
    assert sent and sent[0]["channel"] == "feishu"
    assert "eimemory-learn-watch.timer" in sent[0]["text"]
    incidents = runtime.store.list_records(kinds=["incident"], scope=SCOPE, limit=10)
    assert incidents
    assert incidents[0].meta["report_type"] == "ops_timer_alert"
