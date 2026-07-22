from __future__ import annotations

import pytest

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
    assert "## Module Activation" in report["markdown"]
    assert "External collection" in report["markdown"]
    assert "Paper intake" in report["markdown"]
    assert "Autonomous learning" in report["markdown"]
    assert "Autonomous evolution" in report["markdown"]
    assert "Code sandbox" in report["markdown"]
    assert "Knowledge ingest" in report["markdown"]
    assert "Skill candidates" in report["markdown"]
    assert "Autonomy goal queue" in report["markdown"]
    assert report["module_status"]["external_collection"]["enabled"] is True
    assert report["module_status"]["paper_intake"]["enabled"] is True
    assert report["module_status"]["autonomous_learning"]["enabled"] is True
    assert report["module_status"]["autonomous_evolution"]["enabled"] is True
    assert report["module_status"]["code_sandbox"]["enabled"] is True
    assert report["module_status"]["knowledge_ingest"]["enabled"] is True
    assert report["module_status"]["skill_candidates"]["enabled"] is True
    assert report["module_status"]["autonomy_goal_queue"]["enabled"] is True
    assert report["persisted_record_id"]
    assert report["output_error"] == ""


def test_capability_seeding_uses_compact_score_projection(tmp_path, monkeypatch) -> None:
    runtime = Runtime.create(root=tmp_path)
    compact_calls: list[dict[str, object]] = []
    try:
        ensure_all_seeded(runtime, scope={"agent_id": "hongtu"})
        archived_count = runtime.store.sqlite.conn.execute(
            "SELECT COUNT(*) FROM records WHERE kind='capability_score' AND payload_pointer_json!=''"
        ).fetchone()[0]
        assert archived_count == 0
        original_compact = runtime.store.list_capability_scores_compact
        original = runtime.store.list_records

        def tracked_compact(*args, **kwargs):
            compact_calls.append(dict(kwargs))
            return original_compact(*args, **kwargs)

        def reject_full_score_load(*args, **kwargs):
            if kwargs.get("kinds") == ["capability_score"]:
                raise AssertionError("capability seeding loaded full score payloads")
            return original(*args, **kwargs)

        monkeypatch.setattr(runtime.store, "list_capability_scores_compact", tracked_compact)
        monkeypatch.setattr(runtime.store, "list_records", reject_full_score_load)
        monkeypatch.setattr(
            runtime.store.sqlite.payload_segments,
            "read",
            lambda _pointer: (_ for _ in ()).throw(AssertionError("inline seed payload was hydrated")),
        )
        report = ensure_all_seeded(runtime, scope={"agent_id": "hongtu"})
    finally:
        runtime.close()

    assert report["created_count"] == 0
    assert [call["limit"] for call in compact_calls] == [1000]


def test_capability_seeding_fails_closed_without_compact_projection(tmp_path, monkeypatch) -> None:
    runtime = Runtime.create(root=tmp_path)
    list_calls = 0

    def tracked_full_load(*_args, **_kwargs):
        nonlocal list_calls
        list_calls += 1
        return []

    try:
        monkeypatch.setattr(runtime.store, "list_capability_scores_compact", None)
        monkeypatch.setattr(runtime.store, "list_records", tracked_full_load)
        with pytest.raises(RuntimeError, match="compact capability-score projection is unavailable"):
            ensure_all_seeded(runtime, scope={"agent_id": "hongtu"})
    finally:
        runtime.close()

    assert list_calls == 0


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
    assert "## Module Activation" in report["markdown"]
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
