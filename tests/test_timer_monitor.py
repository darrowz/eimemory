from __future__ import annotations

from eimemory.api.runtime import Runtime
from eimemory.ops.timer_monitor import (
    _parse_systemctl_show,
    _timer_issues,
    check_user_systemd_timers,
)


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
            "next_elapse_at": "2026-06-30T08:20:00+00:00",
        },
        {
            "unit": "eimemory-nightly.service",
            "load_state": "loaded",
            "active_state": "failed",
        },
        {
            "unit": "eimemory-release-closure.path",
            "load_state": "loaded",
            "active_state": "inactive",
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
    assert {issue["reason"] for issue in report["issues"]} == {
        "masked",
        "stale",
        "failed",
        "inactive",
    }
    assert {
        (issue["unit"], issue["reason"]) for issue in report["issues"]
    } >= {("eimemory-release-closure.path", "inactive")}
    assert sent and sent[0]["channel"] == "feishu"
    assert "eimemory-learn-watch.timer" in sent[0]["text"]
    incidents = runtime.store.list_records(kinds=["incident"], scope=SCOPE, limit=10)
    assert incidents
    assert incidents[0].meta["report_type"] == "ops_timer_alert"


def test_timer_monitor_uses_overdue_schedule_not_old_last_trigger() -> None:
    states = [
        _parse_systemctl_show(
            "eimemory-nightly.timer",
            "\n".join(
                [
                    "LoadState=loaded",
                    "ActiveState=active",
                    "LastTriggerUSec=Mon 2026-06-29 03:30:00 UTC",
                    "NextElapseUSecRealtime=Wed 2026-07-01 03:30:00 UTC",
                    "Result=success",
                ]
            ),
        ),
        _parse_systemctl_show(
            "eimemory-l5-effect-review.timer",
            "\n".join(
                [
                    "LoadState=loaded",
                    "ActiveState=active",
                    "LastTriggerUSec=Sun 2026-06-28 10:00:00 UTC",
                    "NextElapseUSecRealtime=",
                    "Result=success",
                ]
            ),
        ),
    ]

    assert _timer_issues(
        states,
        now="2026-06-30T10:00:00+00:00",
        stale_after_minutes=90,
    ) == []

    overdue = _parse_systemctl_show(
        "openclaw-loop-watch.timer",
        "\n".join(
            [
                "LoadState=loaded",
                "ActiveState=active",
                "NextElapseUSecRealtime=Tue 2026-06-30 08:20:00 UTC",
                "Result=success",
            ]
        ),
    )
    assert _timer_issues(
        [overdue],
        now="2026-06-30T10:00:00+00:00",
        stale_after_minutes=90,
    )[0]["reason"] == "stale"


def test_timer_monitor_reports_unavailable_service_units() -> None:
    issues = _timer_issues(
        [
            {
                "unit": "openclaw-loop-watch.service",
                "load_state": "not-found",
                "active_state": "inactive",
                "result": "",
            }
        ],
        now="2026-06-30T10:00:00+00:00",
        stale_after_minutes=90,
    )

    assert issues == [
        {
            "unit": "openclaw-loop-watch.service",
            "reason": "unavailable",
            "load_state": "not-found",
            "active_state": "inactive",
            "result": "",
        }
    ]


def test_timer_monitor_defaults_to_nightly_and_provider_lifecycle_owners(tmp_path) -> None:
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
    assert checked_units == [
        "eimemory-code-implementation-refresh.timer",
        "eimemory-nightly.timer",
        "eimemory-audit-verify.timer",
        "eimemory-timer-monitor.timer",
        "eimemory-l5-effect-review.timer",
        "openclaw-loop-watch.timer",
        "openclaw-loop-compact.timer",
        "eimemory-code-implementation-refresh.service",
        "eimemory-nightly.service",
        "eimemory-audit-verify.service",
        "eimemory-timer-monitor.service",
        "eimemory-l5-effect-review.service",
        "openclaw-loop-watch.service",
        "openclaw-loop-compact.service",
        "eimemory-release-closure.service",
        "eimemory-release-closure.path",
    ]


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


def test_timer_monitor_default_inventory_alerts_failed_openclaw_watchdog(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    calls: list[list[str]] = []

    def runner(args: list[str]) -> str:
        calls.append(args)
        unit = args[args.index("show") + 1]
        failed = unit == "openclaw-loop-watch.service"
        return "\n".join(
            [
                "LoadState=loaded",
                f"ActiveState={'failed' if failed else 'active'}",
                f"SubState={'failed' if failed else 'active'}",
                "UnitFileState=enabled",
                "LastTriggerUSec=2026-06-30T09:59:00+00:00",
                "NextElapseUSecRealtime=2026-07-01T03:30:00+00:00",
                f"Result={'failed' if failed else 'success'}",
            ]
        )

    report = check_user_systemd_timers(
        runtime,
        scope=SCOPE,
        now="2026-06-30T10:00:00+00:00",
        runner=runner,
        persist=True,
    )

    checked_units = [args[args.index("show") + 1] for args in calls if "show" in args]
    assert "openclaw-loop-watch.service" in checked_units
    assert report["ok"] is False
    assert report["issues"] == [
        {
            "unit": "openclaw-loop-watch.service",
            "reason": "failed",
            "load_state": "loaded",
            "active_state": "failed",
            "result": "failed",
        }
    ]
    incidents = runtime.store.list_records(kinds=["incident"], scope=SCOPE, limit=10)
    assert incidents and incidents[0].meta["report_type"] == "ops_timer_alert"
