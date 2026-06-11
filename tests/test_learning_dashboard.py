from __future__ import annotations

import eimemory.governance.learning_dashboard as learning_dashboard

from eimemory.api.runtime import Runtime
from eimemory.governance.capability_seeding import SEEDED_CAPABILITIES, ensure_all_seeded
from eimemory.governance.learning_dashboard import build_weekly_dashboard


def test_capability_seed_is_idempotent_and_dashboard_lists_all_capabilities(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    scope = {"agent_id": "hongtu"}

    first = ensure_all_seeded(runtime, scope=scope)
    second = ensure_all_seeded(runtime, scope=scope)
    report = build_weekly_dashboard(runtime, scope=scope, persist=True)

    assert first["created_count"] == len(SEEDED_CAPABILITIES)
    assert second["created_count"] == 0
    assert report["ok"] is True
    for capability in SEEDED_CAPABILITIES:
        assert capability in report["markdown"]
    assert "| Score | Average | Trend |" in report["markdown"]
    assert "- Learned:" in report["markdown"]
    assert "- Applied:" in report["markdown"]
    assert "- Blocked:" in report["markdown"]
    assert "- Next validation:" in report["markdown"]
    assert "## Failure Focus" in report["markdown"]
    assert report["persisted_record_id"]
    assert report["output_error"] == ""


def test_build_weekly_dashboard_writes_output_on_success(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    report = build_weekly_dashboard(
        runtime,
        scope={"agent_id": "hongtu"},
        output_path=tmp_path / "reports" / "autonomous-learning-dashboard.md",
        persist=False,
    )

    assert report["ok"] is True
    assert report["output_error"] == ""
    assert report["output_path"] == str(tmp_path / "reports" / "autonomous-learning-dashboard.md")
    assert "## Capability Ledger" in report["markdown"]
    assert (tmp_path / "reports" / "autonomous-learning-dashboard.md").read_text(encoding="utf-8") == report["markdown"]


def test_dashboard_defaults_to_daily_autonomy_summary(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)

    report = build_weekly_dashboard(runtime, scope={"agent_id": "hongtu"}, persist=False)

    assert report["report_type"] == "autonomous_learning_daily_dashboard"
    assert report["period_type"] == "daily"
    assert "## Autonomy Summary" in report["markdown"]
    assert "## ROI Components" in report["markdown"]


def test_dashboard_weekly_flag_preserves_weekly_report_type(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)

    report = build_weekly_dashboard(runtime, scope={"agent_id": "hongtu"}, persist=False, weekly=True)

    assert report["report_type"] == "autonomous_learning_weekly_dashboard"
    assert report["period_type"] == "weekly"


def test_build_weekly_dashboard_survives_output_write_permission_error(tmp_path, monkeypatch) -> None:
    runtime = Runtime.create(root=tmp_path)

    output_path = tmp_path / "readonly" / "autonomous-learning-dashboard.md"

    def _raise_permission_error(self, *_args, **_kwargs) -> None:
        raise PermissionError("permission denied for dashboard output")

    monkeypatch.setattr(learning_dashboard.Path, "write_text", _raise_permission_error)
    report = build_weekly_dashboard(
        runtime,
        scope={"agent_id": "hongtu"},
        output_path=output_path,
        persist=True,
    )

    assert report["ok"] is True
    assert report["output_path"] == str(output_path)
    assert report["output_error"]["type"] == "PermissionError"
    assert "permission denied for dashboard output" in report["output_error"]["detail"]
    assert report["persisted_record_id"]
