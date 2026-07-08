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


def test_timer_monitor_defaults_to_single_nightly_orchestrator(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    calls: list[list[str]] = []

    def runner(args: list[str]) -> str:
        calls.append(args)
        return "\n".join(
            [
                "LoadState=loaded",
                "ActiveState=active",
                "SubState=active",
                "UnitFileState=enabled",
                "LastTriggerUSec=2026-06-30T09:59:00+00:00",
                "NextElapseUSecRealtime=2026-07-01T03:30:00+00:00",
                "Result=success",
            ]
        )

    report = check_user_systemd_timers(
        runtime,
        scope=SCOPE,
        now="2026-06-30T10:00:00+00:00",
        runner=runner,
        persist=False,
    )

    checked_units = [args[args.index("show") + 1] for args in calls if "show" in args]
    assert report["ok"] is True
    assert checked_units == ["eimemory-nightly.timer", "eimemory-nightly.service"]


def test_timer_monitor_can_include_legacy_learning_timers_when_explicit(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    calls: list[list[str]] = []

    def runner(args: list[str]) -> str:
        calls.append(args)
        return "\n".join(
            [
                "LoadState=loaded",
                "ActiveState=active",
                "SubState=active",
                "UnitFileState=enabled",
                "LastTriggerUSec=2026-06-30T09:59:00+00:00",
                "NextElapseUSecRealtime=2026-07-01T03:30:00+00:00",
                "Result=success",
            ]
        )

    report = check_user_systemd_timers(
        runtime,
        scope=SCOPE,
        now="2026-06-30T10:00:00+00:00",
        runner=runner,
        persist=False,
        include_legacy_learning_timers=True,
    )

    checked_units = [args[args.index("show") + 1] for args in calls if "show" in args]
    assert report["ok"] is True
    assert "eimemory-nightly.timer" in checked_units
    assert "eimemory-learn-watch.timer" in checked_units
    assert "eimemory-learn-think.timer" in checked_units
    assert "eimemory-learn-dashboard.timer" in checked_units
